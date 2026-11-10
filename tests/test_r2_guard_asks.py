"""The settings guard, the asking half: a shell command that can change this profile's settings, the
plugin's data or its loaded code asks first, however the command spells the path or gets to the folder."""
import pytest

from fb_fakes import sub

approval = sub("approval")
SED = "sed -i 's/dry_run: true/dry_run: false/' "


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "profiles" / "coding"
    (home / "plugins").mkdir(parents=True)
    (home / "plugin-data" / "forgebruecke" / "flows").mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [forgebruecke]\n", encoding="utf-8")
    (tmp_path / "repo").mkdir()
    return home


def shell(home, command, workdir=None):
    repo = home.parent.parent / "repo"
    args = {"command": command.format(home=home, repo=repo)}
    if workdir:
        args["workdir"] = workdir.format(home=home, repo=repo)
    return approval.settings_touch("terminal", args, home)


@pytest.mark.parametrize("command, workdir", [
    ("cd /nonexistent; " + SED + "config.yaml", None),
    ("cd /srv/some-repo; cp .env.example .env", None),
    ("cd {repo} && cd \"$OLDPWD\" && " + SED + "config.yaml", None),
    ("cd {repo} && cd - && " + SED + "config.yaml", None),
    ("cd {repo} && true; " + SED + "config.yaml", None),
    ("cd {repo} && true || " + SED + "config.yaml", None),
    ("cd {repo} & " + SED + "config.yaml", None),
    ("ls; cd ../profiles/coding && " + SED + "config.yaml", "{repo}"),
    ("cd .. && cd profiles/coding && " + SED + "config.yaml", "{repo}"),
    ("cd \"$OLDPWD\" && " + SED + "config.yaml", "{repo}"),
    ("pushd ../profiles/coding && echo B=2 >> .env", "{repo}"),
    ("pushd /tmp && popd && echo B=2 >> .env", "{repo}"),
    ("bash -c 'cd ../profiles/coding && echo B=2 >> .env'", "{repo}"),
    ("git -C ../profiles/coding checkout -- config.yaml", "{repo}"),
])
def test_a_directory_change_inside_the_command_means_the_start_folder_proves_nothing(home, command, workdir):
    assert shell(home, command, workdir)


@pytest.mark.parametrize("command", [
    SED + '"$HERMES_HOME"/config.yaml',
    'echo B=2 >> "$HERMES_HOME"/.env',
    SED + '"{home}"/config.yaml',
    SED + "'{home}'/config.yaml",
    SED + '"${{HERMES_HOME}}/config.yaml"',
    SED + "$PWD/config.yaml",
])
def test_quotes_around_the_folder_do_not_hide_the_settings_file(home, command):
    workdir = None if "$PWD" in command else "{repo}"
    assert shell(home, command, workdir)


def test_pwd_is_the_start_folder(home):
    assert shell(home, SED + "$PWD/config.yaml", "{home}")
    assert shell(home, SED + "$PWD/config.yaml", "{repo}") is None


@pytest.mark.parametrize("command, workdir", [
    ("echo '{{}}' > plugin-data/forgebruecke/flows/control.json", None),
    ("rm -rf ./plugin-data/agent-plugin-forgebruecke-d5cd4631/flows", None),
    ("cd {home} && rm -rf plugin-data/forgebruecke/flows", "{repo}"),
    ("cd {home}/plugin-data && rm -rf forgebruecke/flows", "{repo}"),
    ("rm -rf plugin-data/forgebruecke/flows", "{home}"),
    ("rm -rf flows", "{home}/plugin-data/forgebruecke"),
    ("echo '{{}}' > \"$HERMES_HOME\"/plugin-data/forgebruecke/flows/control.json", "{repo}"),
])
def test_the_data_folder_is_guarded_by_a_relative_path_and_behind_quotes(home, command, workdir):
    assert "own data" in shell(home, command, workdir)


def test_a_data_folder_of_another_place_is_not_this_profiles(home):
    assert shell(home, "rm -rf plugin-data/forgebruecke/flows", "{repo}") is None
    assert shell(home, "cd {repo} && rm -rf plugin-data/forgebruecke/flows") is None


def test_the_loaded_code_is_guarded_behind_quotes_and_from_the_profile_folder(home):
    (home / "plugins" / "forgebruecke").symlink_to(approval.PLUGIN_ROOT)
    assert "own code" in shell(home, 'sed -i s/a/b/ "$HERMES_HOME"/plugins/forgebruecke/approval.py', "{repo}")
    assert "own code" in shell(home, "cd {home} && sed -i s/a/b/ plugins/forgebruecke/approval.py", "{repo}")
    assert "own code" in shell(home, "sed -i s/a/b/ plugins/forgebruecke/approval.py", "{home}")
    assert "own code" in shell(home, "sed -i s/a/b/ approval.py", "{home}/plugins/forgebruecke")
    assert shell(home, "sed -i s/a/b/ plugins/forgebruecke/approval.py", "{repo}") is None
