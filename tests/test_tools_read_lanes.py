"""A mention turn stays inside its thread's repo in forgejo_flows and forgejo_doctor, as the lane table says."""
import json
import sys
import types

from fb_fakes import BASE, DEMO, HEAD_SHA, DictState, sub
from fb_fakes_tools_read import BOT, BOT_ENV, OTHER, approval, kit, mention, plugin

flows_tool, state_mod = sub("tools.flows"), sub("flows.state")
FLOW = {"flow": "demo", "repos": [DEMO], "gates": [{"review": {"by": "forgebruecke-dev"}}, {"human": {"who": "acme"}}]}


def flows_handler(tmp_path, run):
    forge, gs, sec = kit(settings={"dry_run": False, "writes": [{"repo_glob": DEMO, "flows": ["demo"]}]})
    store = state_mod.FlowStore(tmp_path)
    store.write_override("demo", json.dumps(FLOW))
    store.save_pr({"repo": OTHER, "pr": 7, "flow": "demo", "head_sha": HEAD_SHA, "title": "other repo's PR",
                   "gates": [{"gate_key": "review", "type": "review", "state": "failed", "detail": "secret finding"}]})
    handler = plugin.make_flows_handler(gs, lambda: store, lambda: run, flows_tool.make_spec(lambda: store), sec,
                                        transport=forge)
    return (lambda args: json.loads(handler(args))), store, forge


def test_a_mention_turn_reads_no_other_repos_flow_state(tmp_path):
    call, _, forge = flows_handler(tmp_path, mention(DEMO))
    for action in ("status", "get"):
        out = call({"action": action, "repo": OTHER, "number": 7})
        assert out["ok"] is False and out["error"] == "lane_scope", action
        assert "secret finding" not in json.dumps(out)
    assert forge.calls == []


def test_a_mention_turn_and_a_review_worker_cannot_pause_or_run_a_flow(tmp_path):
    for run in (mention(DEMO), approval.RunContext(source=approval.REVIEW_SOURCE), approval.RunContext(cron=True)):
        call, store, forge = flows_handler(tmp_path, run)
        for args in ({"action": "pause", "flow": "demo"}, {"action": "run", "repo": DEMO, "number": 3}):
            out = call(args)
            assert out["ok"] is False and out["error"] in ("unattended", "lane_scope"), (args, out)
        assert not store.control().get("paused") and forge.writes == []


def test_the_users_own_chat_still_reads_and_pauses(tmp_path):
    call, store, _ = flows_handler(tmp_path, approval.RunContext())
    out = call({"action": "status", "repo": OTHER, "number": 7})
    assert out["ok"] and out["gates"][0]["detail"] == "secret finding"
    assert call({"action": "pause", "flow": "demo"})["paused"] == ["demo"]


def test_the_doctor_answers_only_in_the_users_own_chat():
    forge, gs, sec = kit()
    handler = plugin.make_doctor_handler(gs, sec, lane_fn=mention(DEMO).lane)
    text = handler({})
    out = json.loads(text)
    assert out["ok"] is False and out["error"] == "lane_scope" and out["message"]
    assert "acme/" not in text and forge.calls == []


class Ctx:
    def __init__(self, tmp_path):
        self.tools = {}
        self.state = DictState(tmp_path)

    def get_config(self, key, default=None):
        return {"url": BASE}.get(key, default)

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw

    def register_cli_command(self, **kw):
        pass

    def register_hook(self, name, callback):
        pass


def test_register_gives_both_tools_the_turns_lane(tmp_path, monkeypatch):
    secrets = types.ModuleType("agent.secret_scope")
    secrets.get_secret = {BOT_ENV: BOT}.get
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secrets)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "forgebruecke")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", f"{DEMO}#1")
    ctx = Ctx(tmp_path)
    plugin.register(ctx)
    doctor = json.loads(ctx.tools["forgejo_doctor"]["handler"]({}))
    assert doctor["error"] == "lane_scope"
    status = json.loads(ctx.tools["forgejo_flows"]["handler"]({"action": "status", "repo": OTHER, "number": 7}))
    assert status["error"] == "lane_scope" and OTHER in status["message"]
