"""forgejo_flows: the turn's lane and presence decide what an ungated action may read or change."""
import json

import pytest

from fb_fakes import DEMO, HEAD_SHA, sub
from fb_fakes_tools_write import ATTENDED, FLOW, MENTION, OTHER, FlowsKit

approval = sub("approval")
flows_tool = sub("tools.flows")
settings_mod = sub("settings")
DATA_NOTE = sub("tools.read").DATA_NOTE


@pytest.fixture(autouse=True)
def running_in_coding(monkeypatch):
    monkeypatch.setattr(settings_mod, "this_profile", lambda: "coding")


OLD_SHA = "c" * 40


def other_pr_state(kit):
    kit.store.save_pr({"repo": OTHER, "pr": 7, "flow": "demo", "head_sha": HEAD_SHA, "gates": [
        {"gate_key": "review", "type": "review", "state": "failed", "detail": "private finding",
         "head_sha": HEAD_SHA, "carried_from": OLD_SHA,
         "evidence": {"findings": [{"file": "a.py", "problem": "leaks"}]}}]})


@pytest.mark.parametrize("args", [
    {"action": "status", "repo": OTHER, "number": 7},
    {"action": "get", "repo": OTHER},
    {"action": "validate", "repo": OTHER},
    {"action": "template", "template": "solo-with-agents", "repo": OTHER},
    {"action": "run", "repo": OTHER, "number": 7},
    {"action": "pause", "flow": "demo", "repo": OTHER},
])
def test_a_mention_turn_is_refused_for_another_repo(tmp_path, args):
    k = FlowsKit(tmp_path)
    other_pr_state(k)
    out = k.call(args, MENTION, pass_run=False)
    assert out["error"] == "lane_scope" and OTHER in out["message"]
    assert "gates" not in out and k.runner.started == [] and k.store.control().get("paused", []) == []


def test_a_mention_turn_still_reads_its_own_repo(tmp_path):
    k = FlowsKit(tmp_path)
    assert k.call({"action": "get", "repo": DEMO}, MENTION)["ok"]
    assert k.call({"action": "validate", "repo": DEMO}, MENTION)["ok"]


def test_a_mention_turn_cannot_learn_which_other_repos_exist_by_validating_a_flow(tmp_path):
    k = FlowsKit(tmp_path)
    text = json.dumps({**FLOW, "repos": [OTHER]})
    assert k.call({"action": "validate", "text": text})["ok"]
    out = k.call({"action": "validate", "text": text}, MENTION)
    assert out["ok"] is False and "matches no repository" in out["flows"][0]["errors"][0]


@pytest.mark.parametrize("run", [
    approval.RunContext(cron=True),
    approval.RunContext(single_query=True),
    approval.RunContext(platform="webhook"),
    approval.RunContext(unknown=True),
    MENTION,
])
def test_pause_and_run_are_refused_when_no_person_is_present(tmp_path, run):
    k = FlowsKit(tmp_path)
    paused = k.call({"action": "pause", "flow": "demo"}, run)
    ran = k.call({"action": "run", "repo": DEMO, "number": 3}, run)
    assert paused == approval.precheck(run) and ran == approval.precheck(run)
    assert paused["error"] == "unattended" and paused["sent"] is False
    assert k.store.control().get("paused", []) == [] and k.runner.started == [] and k.forge.writes == []


def test_a_mention_lane_refuses_pause_and_run_when_the_handler_passes_no_run_context(tmp_path):
    k = FlowsKit(tmp_path)
    for args in ({"action": "pause", "flow": "demo"}, {"action": "run", "repo": DEMO, "number": 3}):
        out = k.call(args, MENTION, pass_run=False)
        assert out["error"] == "unattended" and out["sent"] is False
    assert k.store.control().get("paused", []) == [] and k.runner.started == [] and k.forge.writes == []


def test_a_person_in_the_chat_pauses_and_runs(tmp_path):
    k = FlowsKit(tmp_path)
    ran = k.call({"action": "run", "repo": DEMO, "number": 3}, ATTENDED)
    assert ran["ok"] and k.runner.started == ["forgebruecke-dev"]
    assert k.call({"action": "pause", "flow": "demo", "repo": DEMO}, ATTENDED)["paused"] == [f"demo@{DEMO}"]


def test_pause_names_a_repo_the_account_cannot_see(tmp_path):
    k = FlowsKit(tmp_path)
    out = k.call({"action": "pause", "flow": "demo", "repo": "acme/gone"}, ATTENDED)
    assert out["error"] == "no_access" and k.store.control().get("paused", []) == []


def test_a_named_template_needs_the_repo(tmp_path):
    k = FlowsKit(tmp_path)
    out = k.call({"action": "template", "template": "solo-with-agents"})
    assert out["error"] == "bad_args" and "(repo)" in out["message"]
    assert [t["name"] for t in k.call({"action": "template"})["templates"]] == [
        "solo-with-agents", "one-review-then-me", "staging-first"]


def test_set_accepts_the_text_a_template_call_returned(tmp_path):
    pytest.importorskip("yaml")
    k = FlowsKit(tmp_path)
    tpl = k.call({"action": "template", "template": "solo-with-agents", "repo": DEMO})
    args = {"action": "set", "repo": DEMO, "target": "local", "text": tpl["yaml"]}
    verdict, out = k.write("forgejo_flows", args, spec=flows_tool.make_spec(lambda: k.store))
    assert verdict["action"] == "approve" and out["ok"] and out["flow"] == tpl["flow"]


def test_an_unknown_action_is_answered_with_every_action_of_the_schema(tmp_path):
    out = FlowsKit(tmp_path).call({"action": "list"})
    actions = flows_tool.SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert out["error"] == "bad_args" and len(actions) == 8
    assert [a for a in actions if a not in out["message"]] == []


def test_results_that_quote_forge_text_carry_the_read_tools_data_note(tmp_path):
    k = FlowsKit(tmp_path, other="write")
    other_pr_state(k)
    status = k.call({"action": "status", "repo": OTHER, "number": 7})
    assert status["gates"][0]["findings"] and status["note"] == DATA_NOTE
    assert k.call({"action": "get", "repo": DEMO})["note"] == DATA_NOTE
    assert k.call({"action": "validate", "repo": DEMO})["note"] == DATA_NOTE
    assert k.call({"action": "run", "repo": DEMO, "number": 3}, ATTENDED)["note"] == DATA_NOTE


def test_results_without_forge_text_carry_no_note(tmp_path):
    k = FlowsKit(tmp_path)
    assert "note" not in k.call({"action": "status", "repo": DEMO, "number": 3})
    assert "note" not in k.call({"action": "template"})
    assert "note" not in k.call({"action": "validate", "text": json.dumps(FLOW)})
    assert "note" not in k.call({"action": "pause", "flow": "demo"}, ATTENDED)


def test_status_returns_full_shas(tmp_path):
    k = FlowsKit(tmp_path, other="write")
    other_pr_state(k)
    status = k.call({"action": "status", "repo": OTHER, "number": 7})
    assert status["head"] == HEAD_SHA
    assert (status["gates"][0]["head"], status["gates"][0]["carried_from"]) == (HEAD_SHA, OLD_SHA)
