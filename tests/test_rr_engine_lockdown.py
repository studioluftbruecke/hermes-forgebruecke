"""A review worker's session calls no tool: Hermes still offers it the profile's plugin toolsets, so
Forgebrücke's hook refuses each call such a session makes, and leaves every other session alone."""
from fb_fakes import load_plugin, sub

approval = sub("approval")
runner_mod = sub("flows.runner")


def test_every_tool_call_of_a_review_worker_is_refused(monkeypatch):
    monkeypatch.setenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", runner_mod.REVIEW_SYSTEM)
    hook = approval.make_review_lockdown()
    for tool in ("forgejo_get", "coolify_action", "tool_call", "memory", "read_file"):
        answer = hook(tool_name=tool, args={}, session_id="w")
        assert answer["action"] == "block" and tool in answer["message"]


def test_other_sessions_pass_and_a_failing_check_blocks_nothing(monkeypatch):
    monkeypatch.delenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", raising=False)
    assert approval.make_review_lockdown()(tool_name="read_file", args={}) is None

    def broken():
        raise RuntimeError("no session")
    assert approval.make_review_lockdown(broken)(tool_name="read_file") is None


def test_register_adds_the_lockdown_hook():
    names = []

    class Ctx:
        def __getattr__(self, name):
            if name == "register_hook":
                return lambda kind, fn, *a, **k: names.append((kind, getattr(fn, "__name__", "")))
            return lambda *a, **k: None
    load_plugin()._register_writes(Ctx(), lambda key, default=None: default)
    assert ("pre_tool_call", "forgebruecke_review_lockdown") in names
