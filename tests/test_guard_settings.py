"""The settings guard: which shell commands change this profile's guardrails, and which only read them or
belong to a project."""
import pytest

from fb_fakes import sub

approval = sub("approval")


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "profiles" / "coding"
    (home / "plugins").mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [forgebruecke]\n", encoding="utf-8")
    return home


def shell(home, command, **args):
    return approval.settings_touch("terminal", {"command": command.format(home=home, root=home.parent), **args}, home)


@pytest.mark.parametrize("command", [
    "sed -i 's/dry_run: true/dry_run: false/' {home}/config.yaml  # --help",
    "yq -i '.approvals.mode = \"off\"' {home}/config.yaml",
    "sed --in-place 's/dry_run: true/dry_run: false/' {home}/config.yaml",
    "sed -Ei 's/dry_run: true/dry_run: false/' {home}/config.yaml",
    "git -C {home} checkout -- config.yaml",
    "cd {root} && sed -i 's/true/false/' coding/config.yaml",
    "hermes --yolo config set plugins.enabled '[]'",
    "hermes --yolo -p coding plugins disable forgebruecke",
])
def test_a_command_that_can_change_the_settings_asks_first(home, command):
    assert shell(home, command)


@pytest.mark.parametrize("command", [
    "cat {home}/config.yaml",
    "grep -n dry_run {home}/config.yaml 2>/dev/null | head -3",
    "cd {home} && ls -la .env && wc -l config.yaml",
    "/usr/bin/tail -n 5 $HERMES_HOME/config.yaml",
    "hermes --yolo -p coding config get approvals.mode",
    "hermes config show | grep -A3 platform_toolsets",
    "hermes plugins list | grep forgebruecke; echo approvals.mode",
    "grep -rn platform_toolsets /srv/hermes | cut -c1-200 | head",
    "hermes config set --help",
])
def test_a_command_that_only_reads_the_settings_passes(home, command):
    assert shell(home, command) is None


@pytest.mark.parametrize("command", [
    "cat {home}/config.yaml > {home}/.env",
    "cat {home}/config.yaml | tee {home}/.env",
    "cat $(sed -i s/a/b/ {home}/config.yaml)",
    "cat `sed -i s/a/b/ {home}/config.yaml`",
    "cat /etc/hostname\nsed -i s/a/b/ {home}/config.yaml",
    "cat a#b; sed -i s/a/b/ {home}/config.yaml",
    "grep x {home}/config.yaml && env sed -i s/a/b/ {home}/config.yaml",
    "cat '{home}/config.yaml",
    "hermes config set approvals.mode off -- --help",
])
def test_a_reading_command_does_not_cover_for_a_writing_one(home, command):
    assert shell(home, command)


PROJECT = [
    "cp .env.example .env",
    "python manage.py runserver --settings config.yaml",
    "cat config.yaml | python -m json.tool",
    "source .env && node server.js",
    "docker compose --env-file=./.env up -d",
]


@pytest.mark.parametrize("command", PROJECT)
def test_a_bare_name_in_a_project_folder_is_the_projects_file(home, command):
    assert shell(home, command, workdir="/srv/some-repo") is None


@pytest.mark.parametrize("command", PROJECT)
def test_a_bare_name_without_a_workdir_may_be_the_profiles_own(home, command):
    assert shell(home, command)


def test_the_workdir_decides_only_when_the_name_resolves_outside_the_profile(home):
    assert shell(home, "cp .env.example .env", workdir=str(home))
    assert shell(home, "sed -i s/a/b/ ../config.yaml", workdir=str(home / "plugins"))
    assert shell(home, "cd {home} && sed -i s/a/b/ config.yaml", workdir="/srv/some-repo")
    assert shell(home, "cd $HERMES_HOME; echo X=1 | tee -a .env", workdir="/srv/some-repo")
    assert shell(home, "sed -i s/a/b/ {home}/config.yaml", workdir="/srv/some-repo")
    assert shell(home, "cp .env.example .env", workdir="some-repo")
    assert shell(home, "sed -i s/a/b/ coding/config.yaml", workdir="/srv/profiles") is None
    assert shell(home, "sed -i s/a/b/ docker/.env") is None


def test_a_leading_cd_moves_the_folder_the_names_are_read_against(home):
    assert shell(home, "cd /srv/some-repo && cp .env.example .env") is None
    assert shell(home, "cd '/srv/some-repo' && cp .env.example .env") is None
    # After `;` the second part runs also when the `cd` failed, so the start folder is unknown.
    assert shell(home, "cd '/srv/some-repo'; cp .env.example .env")
    assert shell(home, "cd api && cp .env.example .env", workdir="/srv/some-repo") is None
    assert shell(home, "cd api && cp .env.example .env")
    assert shell(home, "cd coding && sed -i s/a/b/ config.yaml", workdir=str(home.parent))
    assert shell(home, "cd /srv/some-repo && cd {home} && sed -i s/a/b/ config.yaml")


def test_a_start_folder_that_holds_the_profile_or_lies_in_it_does_not_clear_a_bare_name(home):
    assert shell(home, "ls && cd coding && sed -i s/a/b/ config.yaml", workdir=str(home.parent))
    assert shell(home, "ls && cd .. && sed -i s/a/b/ config.yaml", workdir=str(home / "plugins"))


@pytest.mark.parametrize("folder", ["forgebruecke", "agent-plugin-forgebruecke-d5cd4631"])
def test_the_plugins_data_folder_is_guarded_like_its_code(home, folder):
    data = home / "plugin-data" / folder
    touch = approval.settings_touch
    assert "own data" in touch("write_file", {"path": str(data / "flows" / "overrides" / "demo.yml")}, home)
    assert "own data" in touch("patch", {"path": str(data / "flows" / "control.json")}, home)
    assert "own data" in touch("patch", {"path": f"plugin-data/{folder}/flows/control.json"}, home)
    assert "own data" in shell(home, f"echo paused > $HERMES_HOME/plugin-data/{folder}/flows/control.json")
    assert "own data" in shell(home, f"rm -rf {data}/flows")
    assert shell(home, f"cat {data}/flows/control.json | jq .paused") is None
    assert touch("write_file", {"path": str(home / "plugin-data" / "shaderbruecke" / "state.json")}, home) is None
    assert touch("write_file", {"path": f"/srv/profiles/devops/plugin-data/{folder}/state.json"}, home) is None
    assert shell(home, f"rm -rf /srv/profiles/devops/plugin-data/{folder}/runs") is None


def test_the_guard_reads_the_profile_config_only_for_a_call_that_touches_the_settings(home):
    reads = []
    guard = approval.make_settings_guard(lambda: approval.RunContext(), lambda: reads.append(1) or True, lambda: home)
    assert guard(tool_name="terminal", args={"command": "ls -la && git status"}) is None
    assert guard(tool_name="write_file", args={"path": "/srv/some-repo/notes.md", "content": "x"}) is None
    assert reads == []
    change = {"command": f"sed -i s/a/b/ {home}/config.yaml"}
    assert guard(tool_name="terminal", args=change)["action"] == "approve" and reads == [1]
    off = approval.make_settings_guard(lambda: approval.RunContext(), lambda: False, lambda: home)
    assert off(tool_name="terminal", args=change) is None


def test_always_on_one_guard_prompt_does_not_cover_another_call(home):
    guard = approval.make_settings_guard(lambda: approval.RunContext(), lambda: True, lambda: home)

    def key(tool, **args):
        return guard(tool_name=tool, args=args)["rule_key"]

    first = key("terminal", command="cp .env.example .env")
    assert first.startswith("forgebruecke:settings:terminal:")
    assert first == key("terminal", command="cp .env.example .env")
    assert first != key("terminal", command=f"sed -i s/a/b/ {home}/config.yaml")
    assert key("patch", path=str(home / "config.yaml")) != key("patch", path=str(home / ".env"))
    assert key("execute_code", code="open('config.yaml', 'w')") != key("execute_code", code="open('.env', 'w')")


def test_another_spelling_of_the_same_file_is_still_the_settings_file(home, tmp_path):
    (tmp_path / "alias").symlink_to(home)
    assert shell(home, f"sed -i s/a/b/ {tmp_path}/alias/config.yaml")
    assert shell(home, "sed -i s/a/b/ {home}/plugins/../config.yaml")
    assert shell(home, "sed -i s/a/b/ " + str(home / "Config.yaml").upper())
