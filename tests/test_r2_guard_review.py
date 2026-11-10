"""Findings of the independent review of the guard redesign, each reproduced before the fix: abbreviated long
options, git's own folder options, `${HERMES_HOME:-...}`, globs and braces, a folder that holds the guarded
places, a regex that took seconds, and a path the operating system refuses."""
import time

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


@pytest.mark.parametrize("command", [
    "sed --in 's/a/b/' {home}/config.yaml",
    "sed --in-pl=.bak 's/a/b/' {home}/config.yaml",
    "sed --fi edit.sed {home}/config.yaml",
    "sort --out={home}/config.yaml /tmp/x",
    "sort --outp {home}/.env /tmp/x",
    "awk --incl inplace '{{print}}' {home}/.env",
    "git --git-dir={home}/plugin-data/forgebruecke/.git add -A",
    "git --work-tree={home} add -A",
])
def test_an_abbreviated_long_option_or_gits_own_folder_is_no_reader(home, command):
    assert shell(home, command)


@pytest.mark.parametrize("command", [
    "sed --quiet 1p {home}/config.yaml",
    "sort --reverse {home}/config.yaml",
    "git add -A && git commit -m wip",
    "sed --expression=1p {home}/config.yaml",
])
def test_long_options_that_only_read_still_read(home, command):
    assert shell(home, command, "{repo}") is None


@pytest.mark.parametrize("command", [
    SED + '"${{HERMES_HOME:-$HOME/.hermes}}/config.yaml"',
    SED + "${{HERMES_HOME:?}}/config.yaml",
    "echo A=1 >> ${{HERMES_HOME:=/x}}/.env",
])
def test_a_home_with_a_default_is_still_the_home(home, command):
    assert shell(home, command, "{repo}")


@pytest.mark.parametrize("command, workdir", [
    (SED + "$HERMES_HOME/*.yaml", "{repo}"),
    ("cp x $HERMES_HOME/config.y*ml", "{repo}"),
    ('mv "$HERMES_HOME"/config.yaml{{.new,}}', "{repo}"),
    ("sed -i 's/a/b/' *.yaml", "{home}"),
    ("rm -f .e?v", "{home}"),
])
def test_a_glob_or_a_brace_that_names_a_settings_file_asks(home, command, workdir):
    assert shell(home, command, workdir)


def test_a_glob_in_a_project_folder_passes(home):
    assert shell(home, "sed -i 's/a/b/' *.yaml", "{repo}") is None
    assert shell(home, "rm -f build/*.o", "{repo}") is None


@pytest.mark.parametrize("command, workdir", [
    ("rm -rf $HERMES_HOME/plugin-data", "{repo}"),
    ("rm -rf $HERMES_HOME/plugins", "{repo}"),
    ("tar xzf backup.tgz -C {home}", "{repo}"),
    ("rsync -a /tmp/backup/ {home}/", "{repo}"),
    ("cp -r /tmp/backup/. $HERMES_HOME/", "{repo}"),
    ("rm -rf plugin-data", "{home}"),
])
def test_a_folder_that_holds_the_guarded_places_asks(home, command, workdir):
    assert "holds" in shell(home, command, workdir)


def test_a_folder_beside_the_guarded_places_and_the_start_folder_alone_pass(home):
    assert shell(home, "rm -rf {home}/cache/scratch", "{repo}") is None
    assert shell(home, "mkdir -p logs", "{home}") is None
    assert shell(home, "rm -rf plugin-data", "{repo}") is None
    assert shell(home, "mkdir -p $HERMES_HOME/plugins && ln -sfn /srv/x $HERMES_HOME/plugins/x", "{repo}") is None
    assert shell(home, "mkdir -p $HERMES_HOME/plugins && rm -rf $HERMES_HOME/plugins", "{repo}")


def test_many_flags_after_hermes_cost_no_time(home):
    flags = " ".join(f"--f{i}" for i in range(40))
    start = time.monotonic()
    assert shell(home, f"hermes {flags} ; touch x") is None
    assert shell(home, f"ls ~/.hermes {' '.join(f'--f{i} v' for i in range(40))} | xargs echo") is None
    assert time.monotonic() - start < 1.0


def test_a_path_the_system_refuses_is_judged_not_raised(home):
    assert approval.settings_touch("write_file", {"path": "/x/a\x00b"}, home) is None
    assert approval.settings_touch("terminal", {"command": "rm /x/a\x00b", "workdir": "/srv/r"}, home) is None
