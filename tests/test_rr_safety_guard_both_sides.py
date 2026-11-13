"""Both sides of the settings guard in one place, on a profile that develops code (DevLayout): the commands a
developer runs every day pass without a card, in chat and in cron, wherever the session started; the routes that
change config.yaml, .env or the loaded code ask first in chat and are refused in cron. Every route that asks is
also run in a real bash first, to show that it does change a guarded file."""
import shutil
import tempfile
from pathlib import Path

import pytest

from fb_fakes import sub
from fb_fakes_rr_safety import DevLayout

approval = sub("approval")


@pytest.fixture(scope="module")
def layout():
    # Not pytest's tmp_path: its folder carries the user name, which may be the guard's word "hermes".
    base = Path(tempfile.mkdtemp(prefix="fbs-"))
    yield DevLayout(base)
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def gateway_home(layout, monkeypatch):
    monkeypatch.setenv("HOME", str(layout.root))  # the gateway's HOME holds the profile, as on a container host


def verdict(lay, tool, args, cwd_key, *, before=(), cron=False):
    """The guard's answer to ``tool(args)`` in a session started in ``cwd_key`` after the terminal ``before``."""
    run = (lambda: approval.RunContext(cron=True)) if cron else approval.RunContext
    cwd = lay.cwds[cwd_key]
    hook = approval.make_settings_guard(run, lambda: True, lambda: lay.home,
                                        session_cwd=(lambda: str(cwd)) if cwd else None)
    for command in before:
        hook(tool_name="terminal", args={"command": command}, session_id="s", task_id="t")
    found = hook(tool_name=tool, args=args, session_id="s", task_id="t")
    return None if found is None else found["action"]


def T(command, **extra):
    return ("terminal", {"command": command, **extra})


def fill(lay, text):
    return text.format(home=lay.home, root=lay.root, project=lay.project, checkout=lay.checkout,
                       outside=lay.outside)


EVERY_START = ("none", "project", "profile")

# Calls a developer makes every day. Each passes in every start folder, in chat and in cron.
MUST_PASS = [
    ("git -C <checkout> pull", T("git -C {checkout} pull --ff-only"), EVERY_START),
    ("git -C <checkout> fetch", T("git -C {checkout} fetch origin"), EVERY_START),
    ("git -C <checkout> worktree add", T("git -C {checkout} worktree add /tmp/wt"), EVERY_START),
    ("make -C <checkout> test", T("make -C {checkout} test"), EVERY_START),
    ("npm --prefix <checkout> run build", T("npm --prefix {checkout} run build"), EVERY_START),
    ("pip install -e <checkout>", T("pip install -e {checkout}"), EVERY_START),
    ("rsync a backup of the checkout", T("rsync -a {checkout}/ /tmp/fb-backup/"), EVERY_START),
    ("du of the gateway's HOME into a file", T("du -sh {root}/* > /tmp/sizes.txt"), EVERY_START),
    ("cp a report into ~", T("cp report.md ~"), EVERY_START),
    ("a listing of the profile through tee", T("ls {home} | tee /tmp/listing.txt"), EVERY_START),
    ("cd <checkout> && git pull", T("cd {checkout} && git pull"), EVERY_START),
    ("cd <project> && pytest", T("cd {project} && python -m pytest -q"), EVERY_START),
    ("pytest with a workdir", T("python -m pytest -q", workdir="{project}"), EVERY_START),
    ("pytest in a temp home", T("cd {project} && HERMES_HOME=$(mktemp -d) python -m pytest -q tests"), EVERY_START),
    ("git commit -am in the project", T("git commit -am 'bump config.yaml'", workdir="{project}"), EVERY_START),
    ("docker compose up from the checkout", T("docker compose -f {checkout}/compose.yml up -d"), EVERY_START),
    ("mv a report into ~", T("mv report.md ~/"), EVERY_START),
    ("du of the start folder into a file", T("du -sh * > /tmp/sizes.txt"), EVERY_START),
    ("clear caches below the start folder", T("find . -name '*.pyc' -delete"), EVERY_START),
    ("clear __pycache__ folders", T("find . -name __pycache__ -exec rm -rf {{}} +"), EVERY_START),
    ("unpack a download into a folder of its own", T("tar xzf {outside}/evil.tgz -C {outside}"), EVERY_START),
    ("unpack into a folder below the gateway's HOME", T("tar xzf {outside}/evil.tgz -C downloads"),
     ("gateway-home", "project")),
    ("copy the profile's settings out for a look", T("cp {home}/config.yaml /tmp/config.bak"), EVERY_START),
    ("copy them out keeping times", T("cp -p {home}/config.yaml {home}/.env {outside}/"), EVERY_START),
    ("copy the loaded plugin out, links followed", T("cp -rL $HERMES_HOME/plugins/forgebruecke /tmp/fb-copy"),
     EVERY_START),
    ("write the project's config.yaml", ("write_file", {"path": "config.yaml", "content": "app: 2\n"}), ("project",)),
]


@pytest.mark.parametrize("label, call, starts", MUST_PASS, ids=[c[0] for c in MUST_PASS])
def test_an_everyday_call_passes_in_chat_and_in_cron(layout, label, call, starts):
    tool, args = call
    args = {k: fill(layout, v) for k, v in args.items()}
    for start in starts:
        assert verdict(layout, tool, args, start) is None, f"chat, session started in {start}"
        assert verdict(layout, tool, args, start, cron=True) is None, f"cron, session started in {start}"


# Routes that change config.yaml, .env or the loaded code: each sequence of calls asks at some step in chat and
# is refused in cron. ``prove`` runs it in a real bash first: True, or the program it needs, which a host may lack
# (False where it needs a repository or an archive this layout does not have).
MUST_ASK = [
    # what the released guard asked for, by $HERMES_HOME, kept as controls
    ("tar -x into $HERMES_HOME", [T("tar xzf {outside}/evil.tgz -C $HERMES_HOME")], ("none", "project"), True),
    ("cp -r into $HERMES_HOME/", [T("cp -r {outside}/evil/. $HERMES_HOME/")], ("none", "project"), True),
    ("git -C $HERMES_HOME pull", [T("git -C $HERMES_HOME pull")], EVERY_START, False),
    ("unzip into $HERMES_HOME", [T("unzip -o {outside}/evil.zip -d $HERMES_HOME")], EVERY_START, False),
    # a session that started in the profile folder: a folder the command names is no start folder
    ("tar -x into $HERMES_HOME from the profile", [T("tar xzf {outside}/evil.tgz -C $HERMES_HOME")], ("profile",),
     True),
    ("cp -r into $HERMES_HOME/ from the profile", [T("cp -r {outside}/evil/. $HERMES_HOME/")], ("profile",), True),
    ("cp config.yaml into \"$HERMES_HOME\"", [T("cp {outside}/evil/config.yaml \"$HERMES_HOME\"")], EVERY_START, True),
    ("tar -x into the profile by its path", [T("tar xzf {outside}/evil.tgz -C {home}")], EVERY_START, True),
    ("tar -x into the start folder", [T("tar xzf {outside}/evil.tgz")], ("profile",), True),
    ("cp -r into . in the profile", [T("cp -r {outside}/evil/. .")], ("profile",), True),
    ("rsync into the profile by its path", [T("rsync -a {outside}/evil/ {home}/")], EVERY_START, "rsync"),
    ("find -delete in the profile", [T("find {home} -maxdepth 1 -name 'c*' -delete")], EVERY_START, True),
    ("rm -rf the start folder's contents", [T("rm -rf ./*")], ("profile",), True),
    ("mv the profile away", [T("mv {home} {outside}/gone")], EVERY_START, True),
    ("xargs rm on the profile", [T("echo {home}/config.yaml | xargs rm -f")], EVERY_START, True),
    # the profile named relative to a folder above it, the gateway's HOME
    ("tar -x -C profiles/coding from the gateway's HOME", [T("tar xzf {outside}/evil.tgz -C profiles/coding")],
     ("gateway-home",), True),
    ("cp -r into profiles/coding/ from the gateway's HOME", [T("cp -r {outside}/evil/. profiles/coding/")],
     ("gateway-home",), True),
    ("rm -rf profiles/coding/plugins from the gateway's HOME", [T("rm -rf profiles/coding/plugins")],
     ("gateway-home",), False),
    # a copy that makes a link to a guarded file, then a write through the link
    ("cp -s config.yaml, then >> the link",
     [T("cp -s {home}/config.yaml cfg"), T("echo 'approvals: {{mode: off}}' >> cfg")], ("project",), True),
    ("cp -l config.yaml, then >> the link",
     [T("cp -l {home}/config.yaml cfg"), T("echo 'approvals: {{mode: off}}' >> cfg")], ("project",), True),
    ("cp -s .env, then >> the link",
     [T("cp -s $HERMES_HOME/.env e"), T("echo 'FORGEBRUECKE_PERSONAL_WRITE_TOKEN=x' >> e")], ("project",), True),
    ("cp -P the plugin link, then >> its code",
     [T("cp -P {home}/plugins/forgebruecke fb"), T("echo 'GUARDED_TOOLS = ()' >> fb/approval.py")], ("project",), True),
    ("cp -r the plugin link, then >> its code",
     [T("cp -r $HERMES_HOME/plugins/forgebruecke fb"), T("echo 'GUARDED_TOOLS = ()' >> fb/approval.py")],
     ("project",), True),
    ("cp -a the plugin link, then >> its code",
     [T("cp -a $HERMES_HOME/plugins/forgebruecke fb"), T("echo 'GUARDED_TOOLS = ()' >> fb/approval.py")],
     ("project",), True),
    # a change of folder the guard cannot read: the next relative name may be the profile's
    ("c\\d into the profile, then write_file config.yaml",
     [T("c\\d {home}"), ("write_file", {"path": "config.yaml", "content": "approvals: {{mode: off}}\n"})],
     ("project",), True),
    ("'c'd into the profile, then sed -i config.yaml",
     [T("'c'd {home}"), T("sed -i 's/dry_run: true/dry_run: false/' config.yaml")], ("project",), True),
    ("\"c\"d into the profile, then >> .env",
     [T("\"c\"d {home}"), T("echo 'FORGEBRUECKE_PERSONAL_WRITE_TOKEN=x' >> .env")], ("project",), True),
    ("c\\d up into the profile and sed -i config.yaml in one call",
     [T("c\\d ../.. && sed -i 's/dry_run: true/dry_run: false/' config.yaml")], ("project",), True),
    ("'c'd up into the profile and >> .env in one call",
     [T("'c'd ../..; echo 'FORGEBRUECKE_PERSONAL_WRITE_TOKEN=x' >> .env")], ("project",), True),
    ("cd through a variable, then >> .env",
     [T("go=cd; $go {home}"), T("echo 'FORGEBRUECKE_PERSONAL_WRITE_TOKEN=x' >> .env")], ("project",), True),
    ("a script read into the shell, then write_file config.yaml",
     [T(". {outside}/go.sh"), ("write_file", {"path": "config.yaml", "content": "approvals: {{mode: off}}\n"})],
     ("project",), True),
]


@pytest.mark.parametrize("label, steps, starts, prove", MUST_ASK, ids=[c[0] for c in MUST_ASK])
def test_a_route_to_the_settings_asks_in_chat_and_is_refused_in_cron(layout, label, steps, starts, prove):
    steps = [(tool, {k: fill(layout, v) for k, v in args.items()}) for tool, args in steps]
    for start in starts:
        if prove is True or (prove and shutil.which(prove)):
            commands = [a["command"] if t == "terminal" else f"printf %s {a['content']!r} > {a['path']!r}"
                        for t, a in steps]
            assert layout.run(commands, layout.cwds[start]), f"the premise: the shell changes a guarded file ({start})"
            layout.reset()  # the guard judges the calls before they run
        for cron, expected in ((False, "approve"), (True, "block")):
            answers, history = [], []
            for tool, args in steps:
                answers.append(verdict(layout, tool, args, start, before=tuple(history), cron=cron))
                if tool == "terminal":
                    history.append(args["command"])
            assert expected in answers, f"{'cron' if cron else 'chat'}, session started in {start}: {answers}"


@pytest.mark.parametrize("earlier", [
    "git commit -m \"bump config.yaml\"", "[ -f .env ] && echo 'has .env'", "A=\"x y\" make test > out.txt",
    "if [ -d build ]; then rm -rf build; fi", "python -m pytest -q 2>&1 | tail -5",
])
def test_quotes_in_arguments_keep_the_sessions_folder(layout, earlier):
    for cron in (False, True):
        found = verdict(layout, "write_file", {"path": "config.yaml", "content": "app: 2\n"}, "project",
                        before=(earlier,), cron=cron)
        assert found is None, f"{'cron' if cron else 'chat'}: the project's own config.yaml after {earlier!r}"


@pytest.mark.parametrize("earlier, tool, args", [
    ("cd {project}", "write_file", {"path": "config.yaml", "content": "app: 2\n"}),
    ("cd work/proj", "patch", {"path": ".env", "old_string": "A=1", "new_string": "A=2"}),
    ("cd {project} && git status", "terminal", {"command": "sed -i s/1/2/ config.yaml"}),
])
def test_after_a_cd_into_a_project_its_own_files_pass(layout, earlier, tool, args):
    """A session that started in the profile folder and moved into a project: the project's own files pass."""
    for cron in (False, True):
        found = verdict(layout, tool, args, "profile", before=(fill(layout, earlier),), cron=cron)
        assert found is None, f"{'cron' if cron else 'chat'} after {earlier!r}"


@pytest.mark.parametrize("earlier", ["cd {project}/missing", "cd {project}; cd {home}", "pushd {project}"])
def test_a_cd_that_may_have_failed_keeps_the_profile_in_view(layout, earlier):
    found = verdict(layout, "write_file", {"path": "config.yaml", "content": "x"}, "profile",
                    before=(fill(layout, earlier),))
    assert found == "approve"
