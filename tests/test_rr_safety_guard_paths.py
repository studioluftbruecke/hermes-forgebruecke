"""The settings guard on a profile whose path holds none of the guard's words (`/srv/data/profiles/coding` on
the official image): commands that replace the profile folder by its absolute path ask first, without a
`workdir` too. Each one changes the profile in a real shell."""
import shutil
import tempfile
from pathlib import Path

import pytest

from fb_fakes import sub
from fb_fakes_rr_safety import Layout

approval = sub("approval")


@pytest.fixture
def layout(monkeypatch):
    # Not pytest's tmp_path: its folder carries the user name, which may be the guard's word "hermes".
    base = Path(tempfile.mkdtemp(prefix="fbs-"))
    lay = Layout(base)
    monkeypatch.setenv("HOME", str(lay.root))
    yield lay
    shutil.rmtree(base, ignore_errors=True)


def verdict(lay, command, **args):
    hook = approval.make_settings_guard(lambda: approval.RunContext(), lambda: True, lambda: lay.home)
    return hook(tool_name="terminal", args={"command": command, **args}, session_id="s")


@pytest.mark.parametrize("command", [
    "tar xzf {outside}/evil.tgz -C {home}",
    "cp -r {outside}/evil/. {home}/",
    "find {home} -maxdepth 1 -name 'c*' -delete",
    "rsync -a {outside}/evil/ {home}/",
    "cd {root} && tar xzf {outside}/evil.tgz -C profiles/coding",
])
def test_a_command_on_the_profile_folder_by_its_path_asks_first(layout, command):
    command = command.format(outside=layout.outside, home=layout.home, root=layout.root)
    if shutil.which(command.split()[0]) or command.startswith("cd "):
        assert layout.run(command), "the premise: in a real agent shell this command changes the profile"
    found = verdict(layout, command)
    assert found is not None and found["action"] == "approve"


def test_the_profile_path_alone_in_a_harmless_command_passes(layout):
    assert verdict(layout, f"ls -la {layout.home}") is None
    assert verdict(layout, f"du -sh {layout.home}") is None
    assert verdict(layout, "make -j4 test") is None
