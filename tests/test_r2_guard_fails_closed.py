"""Hermes skips a hook that raises. So the settings guard never raises: a check that fails asks, and a run
context that cannot be read counts as nobody there. The page's routes that change something ask like the one
that saves a setting."""
import pathlib

import pytest

from fb_fakes import sub

approval = sub("approval")
HOME = pathlib.Path("/srv/no-such-root/profiles/coding")
BASE = "http://127.0.0.1:9119/api/plugins/forgebruecke"
CHANGE = {"command": f"sed -i s/a/b/ {HOME}/config.yaml"}


def guard(run=approval.RunContext, enabled=lambda: True, home=lambda: HOME):
    return approval.make_settings_guard(run, enabled, home)


def test_a_check_that_raises_asks(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bad text")

    monkeypatch.setattr(approval, "settings_touch", boom)
    asked = guard()(tool_name="terminal", args={"command": "ls"})
    assert asked["action"] == "approve" and "could not be checked" in asked["message"]


def test_a_profile_check_that_raises_keeps_the_guard_on():
    def broken():
        raise ValueError("yaml")

    assert guard(enabled=broken)(tool_name="terminal", args=CHANGE)["action"] == "approve"


def test_a_run_context_that_cannot_be_read_is_nobody_there():
    def broken():
        raise RuntimeError("no session")

    refused = guard(run=broken)(tool_name="terminal", args=CHANGE)
    assert refused["action"] == "block" and "guardrail" in refused["message"]


def test_a_home_that_cannot_be_read_still_guards_any_profiles_settings():
    def broken():
        raise OSError("gone")

    assert guard(home=broken)(tool_name="terminal", args=CHANGE)["action"] == "approve"
    assert guard(home=broken)(tool_name="terminal", args={"command": "ls -la && git status"}) is None


@pytest.mark.parametrize("route, word", [("merge", "merges"), ("reviewer-profile", "reviewer profile"),
                                         ("comment", "posts a comment as you"), ("park", "parks a pull request"),
                                         ("unpark", "brings a parked pull request back")])
def test_the_routes_that_merge_or_create_a_profile_ask(route, word):
    command = f"curl -s -X POST {BASE}/{route} -H 'Content-Type: application/json' -d '{{}}'"
    assert word in approval.settings_touch("terminal", {"command": command}, HOME)
    code = f"import urllib.request\nurllib.request.urlopen(urllib.request.Request('{BASE}/{route}', data=b'{{}}'))"
    assert word in approval.settings_touch("execute_code", {"code": code}, HOME)
    assert approval.settings_touch("terminal", {"command": f"grep -rn {BASE}/{route} plugins/"}, HOME) is None
