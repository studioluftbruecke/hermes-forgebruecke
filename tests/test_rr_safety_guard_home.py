"""The settings guard reads `~` and `$HOME` as the agent's shell does: on a container install Hermes gives agent
shells HOME=<profile>/home, so `~/..` is the profile itself. Each command either asks first or, run in a real
shell, leaves the profile's settings and data unchanged."""
import pytest

from fb_fakes import sub
from fb_fakes_rr_safety import Layout

approval = sub("approval")


@pytest.fixture
def layout(tmp_path, monkeypatch):
    lay = Layout(tmp_path)
    monkeypatch.setenv("HOME", str(lay.root))  # the gateway's own home, which is not the agent shell's
    return lay


def guard(lay, tool, args):
    hook = approval.make_settings_guard(lambda: approval.RunContext(), lambda: True, lambda: lay.home)
    return hook(tool_name=tool, args=args, session_id="s")


SHELL_HOME_ROUTES = [
    "sed -i 's/dry_run: true/dry_run: false/' ~/../config.yaml",
    "sed -i 's/dry_run: true/dry_run: false/' $HOME/../config.yaml",
    "sed -i 's/dry_run: true/dry_run: false/' \"${HOME}\"/../config.yaml",
    "echo 'FORGEBRUECKE_PERSONAL_WRITE_TOKEN=stolen' >> ~/../.env",
    "cp {outside}/evil.yml ~/../plugin-data/forgebruecke/flows/overrides/demo.yml",
]


@pytest.mark.parametrize("command", SHELL_HOME_ROUTES)
def test_a_route_through_the_shells_home_asks_first(layout, command):
    command = command.replace("{outside}", str(layout.outside))
    assert layout.run(command), "the premise: in a real agent shell this command changes the profile"
    verdict = guard(layout, "terminal", {"command": command})
    assert verdict is not None and verdict["action"] == "approve"


def test_code_and_file_tools_that_name_the_shells_home_ask_first(layout):
    code = ("import os\np=os.path.expanduser('~/../config.yaml')\n"
            "s=open(p).read().replace('dry_run: true','dry_run: false')\nopen(p,'w').write(s)")
    assert guard(layout, "execute_code", {"code": code})["action"] == "approve"
    assert guard(layout, "write_file", {"path": "~/../config.yaml", "content": "x"})["action"] == "approve"


def test_the_shells_own_files_under_its_home_pass(layout):
    assert guard(layout, "terminal", {"command": "sed -i s/a/b/ ~/notes/config.yaml"}) is None
    assert guard(layout, "write_file", {"path": "~/projects/app/.env", "content": "A=1"}) is None


def test_the_gateways_home_spelling_still_counts(layout):
    """Without a profile home folder the shell's HOME is the process HOME, so ~ there is checked too."""
    (layout.root / "profiles" / "coding" / "home").rmdir()
    command = "sed -i s/a/b/ ~/profiles/coding/config.yaml"
    assert guard(layout, "terminal", {"command": command})["action"] == "approve"
