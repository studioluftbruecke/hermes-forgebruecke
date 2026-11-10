"""The settings guard, the passing half: a shell command that cannot write is never asked, whatever it
names and wherever it runs. A reader with an argument that makes it write is no reader."""
import pathlib

import pytest

from fb_fakes import sub

approval = sub("approval")
HOME = pathlib.Path("/srv/no-such-root/profiles/coding")
REPO = "/srv/some-repo"


def shell(command, workdir=None):
    args = {"command": command, **({"workdir": workdir} if workdir else {})}
    return approval.settings_touch("terminal", args, HOME)


@pytest.mark.parametrize("command", [
    "git diff -- config.yaml",
    "git status --short .env",
    "find . -name config.yaml",
    "test -f .env && echo present",
    "[ -f .env ] && echo present",
    "sed -n 1,5p config.yaml",
    "sed -n -e 1p -e '/^plugins/,+3p' config.yaml",
    "awk -F= '{print $1}' .env",
    "sort config.yaml | uniq -c",
    "docker compose --env-file .env config",
    "docker-compose --env-file .env ps",
    "cd /srv/some-repo\ngit diff -- config.yaml",
    "git --no-pager log --oneline -- config.yaml",
    "git -C /srv/some-repo show HEAD:config.yaml | head -20",
    "git add config.yaml .env.example && git commit -m 'config: defaults'",
])
def test_a_command_that_cannot_write_passes_without_a_workdir(command):
    assert shell(command) is None


@pytest.mark.parametrize("command", [
    "find . -name config.yaml -delete",
    "find . -name .env -exec rm {} +",
    "sed -i s/a/b/ config.yaml",
    "sed -n -i.bak p config.yaml",
    "sed --in-place=.bak s/a/b/ config.yaml",
    "sed -n 'w config.yaml' other.txt",
    "sed -f edit.sed config.yaml",
    "awk '{print > \"config.yaml\"}' in.txt",
    "awk -i inplace '{print}' .env",
    "awk 'BEGIN{system(\"rm .env\")}'",
    "sort -o config.yaml config.yaml",
    "sort --output=.env .env",
    "uniq in.txt config.yaml",
    "git checkout -- config.yaml",
    "git -C . restore .env",
    "git diff --output=config.yaml",
    "git stash -- .env",
    "git config -f config.yaml a.b c",
    "docker compose --env-file .env up -d",
    "docker compose config -o config.yaml",
    "docker cp box:/app/.env .env",
    "test -f .env || cp .env.example .env",
    "git status; python tool.py config.yaml",
])
def test_a_reader_with_a_writing_argument_and_a_writer_beside_readers_ask(command):
    assert shell(command)


@pytest.mark.parametrize("command", [
    "git grep -n approvals.mode",
    "git log -S platform_toolsets --oneline",
    "git commit -m 'docs: explain approvals.mode'",
    "git commit -m 'docs: explain approvals.mode' && git push",
    "sed -n '/platform_toolsets/,+5p' README.md",
    "git diff --stat -- docs/plugins.entries.forgebruecke.md",
    "gh issue create --title 'platform_toolsets is unclear'",
    "echo approvals.mode >> NOTES.md",
])
def test_a_settings_key_alone_is_only_a_word(command):
    assert shell(command, REPO) is None and shell(command) is None


def test_a_settings_key_counts_beside_a_settings_file_and_in_code():
    assert shell("sed -i '/approvals.mode/d' config.yaml", REPO)
    assert shell("python3 fix.py --key platform_toolsets --file ./config.yaml", REPO)
    assert approval.settings_touch("execute_code", {"code": "cfg['approvals.mode'] = 'off'\nsave(cfg)"}, HOME)
