"""`hermes` commands that change this profile's settings ask first in every spelling: the module form
`python -m hermes_cli.main` and the restore of a backup zip, which replaces config.yaml and .env."""
import pathlib

import pytest

from fb_fakes import sub

approval = sub("approval")
HOME = pathlib.Path("/srv/data/profiles/coding")


def shell(command):
    return approval.settings_touch("terminal", {"command": command}, HOME)


@pytest.mark.parametrize("command", [
    "python -m hermes_cli.main config set plugins.entries.forgebruecke.settings.dry_run false",
    "/opt/venv/bin/python3 -m hermes_cli.main --yolo config set approvals.mode off",
    "python3 -m hermes_cli config set plugins.enabled '[]'",
    "python -m hermes_cli.main plugins disable forgebruecke",
    "hermes import /tmp/backup.zip --force",
    "hermes -p coding import backup.zip",
])
def test_a_settings_change_in_another_spelling_asks_first(command):
    assert shell(command)


@pytest.mark.parametrize("command", [
    "python -m hermes_cli.main config get approvals.mode",
    "hermes -p other import /tmp/backup.zip --force",
    "python -m hermes_cli.main plugins disable some-other-plugin",
    "pip install hermes_cli_tools",
])
def test_a_read_or_another_profile_or_plugin_passes(command):
    assert shell(command) is None
