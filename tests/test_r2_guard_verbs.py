"""The settings guard and the `hermes` command: which verbs change what a mention turn may use or the
plugin's loaded code, and which belong to another platform, profile or plugin."""
import pathlib

import pytest

from fb_fakes import sub

approval = sub("approval")
HOME = pathlib.Path("/srv/no-such-root/profiles/coding")


def shell(command):
    return approval.settings_touch("terminal", {"command": command}, HOME)


@pytest.mark.parametrize("command", [
    "hermes tools enable terminal --platform forgebruecke",
    "hermes -p coding tools enable terminal file --platform forgebruecke",
    "hermes tools enable terminal --platform=forgebruecke",
    "hermes tools disable forgebruecke --platform forgebruecke",
    "hermes --yolo tools enable --platform forgebruecke terminal",
])
def test_changing_the_tools_of_a_mention_turn_asks_first(command):
    assert shell(command)


@pytest.mark.parametrize("command", [
    "hermes plugins update forgebruecke",
    "hermes plugins update",
    "hermes --yolo plugins update --yes",
])
def test_replacing_the_loaded_plugin_code_asks_first(command):
    assert shell(command)


@pytest.mark.parametrize("command", [
    "hermes tools enable web",
    "hermes tools disable terminal --platform telegram",
    "hermes -p devops tools enable terminal --platform forgebruecke",
    "hermes plugins update shaderbruecke",
    "hermes tools list",
    "hermes tools enable terminal --platform forgebruecke --help",
])
def test_tools_of_another_platform_or_profile_and_another_plugin_pass(command):
    assert shell(command) is None
