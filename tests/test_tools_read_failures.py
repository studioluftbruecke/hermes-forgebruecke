"""One failure shape for every tool, and a 404 that says what is missing."""
import json

import pytest

from fb_fakes import DEMO, bot_account
from fb_fakes_tools_read import BOT, kit, plugin, read


def test_every_failure_has_a_code_and_a_sentence():
    refusal, _ = read("forge_get", {"kind": "pull", "repo": DEMO})
    missing, _ = read("forge_get", {"kind": "pr", "repo": DEMO, "number": 99})
    forge, gs, sec = kit()
    forge.accounts[BOT] = {**bot_account(), "scopes": {"read:repository", "read:user"}}
    scope = json.loads(plugin.make_read_handler("forge_get", gs, sec, transport=forge)(
        {"kind": "issue", "repo": DEMO, "number": 1}))
    not_ready = plugin.make_doctor_handler(lambda k, d=None: d, {}.get)
    short, full = json.loads(not_ready({})), json.loads(not_ready({"detail": True}))
    for out in (refusal, missing, scope, short, full):
        assert out["ok"] is False and isinstance(out["error"], str) and out["error"] and out["message"], out
    assert (refusal["error"], missing["error"], scope["error"], short["error"], full["error"]) == (
        "bad_args", "not_found", "missing_scope", "not_ready", "not_ready")
    assert short["message"] == full["message"] == short["next_action"]["text"] == "Enter your Forgejo address."
    assert scope["status"] == 403 and scope["missing_scopes"] == ["read:issue"] and "read:issue" in scope["message"]
    assert "kind" not in scope


def test_a_missing_pull_request_is_named_and_an_issue_number_is_pointed_to_its_kind():
    out, _ = read("forge_get", {"kind": "pr", "repo": DEMO, "number": 99})
    assert out["error"] == "not_found" and out["message"] == f"{DEMO} has no pull request #99."
    out, _ = read("forge_get", {"kind": "pr", "repo": DEMO, "number": 1})
    assert out["message"] == f"{DEMO} has no pull request #1. It is an issue; use kind=issue."


@pytest.mark.parametrize("args,names", [
    ({"kind": "issue", "number": 99}, "issue #99"), ({"kind": "branch", "ref": "nope"}, "branch nope"),
    ({"kind": "commit", "ref": "abc1234"}, "commit abc1234"), ({"kind": "release", "ref": "v9"}, "release v9"),
])
def test_other_missing_objects_in_a_readable_repo_are_named_too(args, names):
    out, _ = read("forge_get", {"repo": DEMO, **args})
    assert out["ok"] is False and out["error"] == "not_found"
    assert DEMO in out["message"] and names in out["message"] and "visible" not in out["message"]
