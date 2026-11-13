"""The settings guard knows where a call starts: the session's recorded folder (``ctx.session_cwd()``), followed
across the session's own `cd`. A project's own config.yaml and .env pass there, read-only `yq` passes, and a
copy or an archive that only reads the profile passes. Whatever may still start in the profile asks."""
import shutil
import tempfile
from pathlib import Path

import pytest

from fb_fakes import sub
from fb_fakes_rr_safety import Layout, RecordingCtx

approval = sub("approval")


@pytest.fixture
def layout(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="fbs-"))
    lay = Layout(base)
    monkeypatch.setenv("HOME", str(lay.root))
    yield lay
    shutil.rmtree(base, ignore_errors=True)


def make_guard(lay, cwd, *, cron=False):
    run = (lambda: approval.RunContext(cron=True)) if cron else approval.RunContext
    return approval.make_settings_guard(run, lambda: True, lambda: lay.home, session_cwd=lambda: str(cwd))


def call(hook, tool, session="s", **args):
    return hook(tool_name=tool, args=args, session_id=session, task_id=session)


@pytest.mark.parametrize("tool, args", [
    ("write_file", {"path": "config.yaml", "content": "app: 2\n"}),
    ("patch", {"path": ".env", "old_string": "A=1", "new_string": "A=2"}),
    ("execute_code", {"code": "import yaml\nprint(yaml.safe_load(open('config.yaml')))"}),
    ("terminal", {"command": "cp .env.example .env"}),
    ("terminal", {"command": "sed -i s/1/2/ config.yaml"}),
])
def test_a_projects_own_files_pass_in_the_project_and_in_cron(layout, tool, args):
    assert call(make_guard(layout, layout.project), tool, **args) is None
    assert call(make_guard(layout, layout.project, cron=True), tool, **args) is None


@pytest.mark.parametrize("tool, args", [
    ("write_file", {"path": "config.yaml", "content": "x"}),
    ("patch", {"path": ".env", "old_string": "x", "new_string": "y"}),
    ("terminal", {"command": "sed -i 's/dry_run: true/dry_run: false/' config.yaml"}),
])
def test_the_same_calls_in_the_profile_folder_ask(layout, tool, args):
    assert call(make_guard(layout, layout.home), tool, **args)["action"] == "approve"


def test_a_cd_into_the_profile_in_an_earlier_call_is_followed(layout):
    hook = make_guard(layout, layout.project)
    assert call(hook, "terminal", command=f"cd {layout.home}") is None  # a cd only reads
    found = call(hook, "terminal", command="sed -i 's/dry_run: true/dry_run: false/' config.yaml")
    assert found["action"] == "approve"
    assert call(hook, "write_file", path=".env", content="x")["action"] == "approve"
    assert call(hook, "terminal", session="other", command="sed -i s/1/2/ config.yaml") is None


def test_a_cd_into_a_project_in_an_earlier_call_is_followed(layout):
    hook = make_guard(layout, layout.outside)
    call(hook, "terminal", command=f"cd {layout.project}")
    assert call(hook, "terminal", command="cp .env.example .env") is None


def test_a_folder_change_the_guard_cannot_follow_makes_the_start_unknown(layout):
    hook = make_guard(layout, layout.project)
    call(hook, "terminal", command="cd \"$(cat /tmp/where)\"")
    assert call(hook, "write_file", path="config.yaml", content="x")["action"] == "approve"


def test_a_workdir_call_does_not_move_the_session(layout):
    hook = make_guard(layout, layout.project)
    call(hook, "terminal", command=f"cd {layout.home}", workdir=str(layout.outside))
    assert call(hook, "write_file", path="config.yaml", content="x") is None


@pytest.mark.parametrize("command", [
    "yq '.plugins' {home}/config.yaml",
    "yq -o=json eval '.plugins' {home}/config.yaml",
    "cp {home}/config.yaml /tmp/config.bak",
    "cp -p {home}/.env {home}/config.yaml {outside}/",
    "tar czf /tmp/profile.tgz -C {home} .",
    "tar --create --gzip --file=/tmp/profile.tgz -C {home} .",
])
def test_reading_the_profile_into_a_copy_passes(layout, command):
    command = command.format(home=layout.home, outside=layout.outside)
    assert call(make_guard(layout, layout.project), "terminal", command=command) is None


@pytest.mark.parametrize("command", [
    "yq -i '.approvals.mode = \"off\"' {home}/config.yaml",
    "yq --inplace '.a = 1' {home}/config.yaml",
    "cp {outside}/evil/config.yaml {home}/config.yaml",
    "cp -a {home}/.env {home}/config.yaml {outside}/",  # -a copies a linked config.yaml as a link
    "cp -t {home} {outside}/evil/config.yaml",
    "cp --backup=numbered {outside}/evil/config.yaml {home}/",
    "tar czf {home}/config.yaml -C /tmp .",
    "tar czf /tmp/x.tgz --remove-files -C {home} config.yaml",
    "tar xzf {outside}/evil.tgz -C {home}",
])
def test_a_copy_or_archive_that_writes_the_profile_asks(layout, command):
    command = command.format(home=layout.home, outside=layout.outside)
    assert call(make_guard(layout, layout.project), "terminal", command=command)["action"] == "approve"


def test_register_hands_the_sessions_folder_to_the_guard(layout, monkeypatch):
    plugin = sub("__init__")
    monkeypatch.setattr(sub("profile"), "current_home", lambda: layout.home)
    ctx = RecordingCtx(cwd=str(layout.project))
    plugin.register(ctx)
    guard = ctx.hooks["pre_tool_call"][1]
    assert guard(tool_name="write_file", args={"path": "config.yaml", "content": "x"}, session_id="s") is None
    ctx.cwd = str(layout.home)
    found = guard(tool_name="write_file", args={"path": "config.yaml", "content": "x"}, session_id="t")
    assert found["action"] == "approve"
