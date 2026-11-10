"""Stored state whose flow no longer runs on its repo (rule changed, file invalid or gone): its running gates
end, and it holds no review slot. A paused flow is not one of these: see test_r2_engine_pause.py."""
import json

from fb_fakes import DEMO, bot_account
from fb_fakes_engine import FLOW, Kit, engine_env  # noqa: F401

OTHER = "acme/extra-0"
REASON = "flow demo is not active here, so this gate stopped. Fix the flow or its rule, then push."


def test_a_flow_that_left_the_rule_ends_its_running_review_once_and_frees_the_slot(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.values["writes"] = [{"repo_glob": DEMO, "flows": ["another"]}]
    k.runner.dead.add(1001)
    k.clock.t += 3 * 24 * 3600
    report = k.tick()
    review = k.gates()["review"]
    assert review["state"] == "error" and review["detail"] == REASON
    assert k.store.running_reviews() == 0
    assert k.statuses()["forgebruecke/demo/review"] == "error"
    assert [p["pr"] for p in report["prs"]] == [3]
    sent = len(k.forge.writes)
    assert k.tick()["prs"] == [] and len(k.forge.writes) == sent


def test_a_worker_of_an_inactive_flow_is_stopped(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.store.write_override("demo", "{not a flow")
    k.tick()
    assert k.runner.stopped == [1001] and k.gates()["review"]["state"] == "error"


def test_a_verdict_that_arrived_before_the_flow_stopped_still_counts(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("pass", summary="fine")
    k.store.write_override("demo", "{not a flow")
    k.tick()
    review = k.gates()["review"]
    assert review["state"] == "passed" and review["detail"] == "review by forgebruecke-dev: fine"
    assert k.statuses()["forgebruecke/demo/review"] == "success" and k.runner.stopped == []


def test_reviews_in_another_repo_start_once_the_inactive_ones_ended(tmp_path):
    rules = [{"repo_glob": DEMO, "flows": ["demo"]}, {"repo_glob": OTHER, "flows": ["other"]}]
    k = Kit(tmp_path, account=bot_account(2), rules=rules)
    k.set_flow({**FLOW, "flow": "other", "repos": [OTHER]})
    k.add_pr(4)
    k.all[OTHER] = {"info": {"default_branch": "main"}, "diff": {7: "diff --git a/y b/y\n"}, "status": {},
                    "pulls": {7: {**k.data["pulls"][3], "number": 7, "head": {"ref": "b7", "sha": "7" * 40}}}}
    k.tick()
    assert k.gates(7, OTHER)["review"]["state"] == "pending"
    k.values["writes"] = rules[1:]
    k.tick()
    k.tick()
    assert k.gates(7, OTHER)["review"]["state"] == "running"


def test_a_flow_file_that_could_not_be_read_this_tick_ends_nothing(tmp_path):
    k = Kit(tmp_path, flow={**FLOW, "flow": "local-only"})
    k.data["files_in_repo"] = {".forgebruecke/flows.yml": json.dumps(FLOW)}
    k.tick()
    assert k.gates()["review"]["state"] == "running"
    k.transport.fail(lambda m, p: m == "GET" and "/contents/" in p)
    report = k.tick()
    assert k.gates()["review"]["state"] == "running" and k.runner.stopped == []
    assert any("the flow file could not be read" in n for n in report["notes"])


def test_closed_state_of_an_inactive_flow_is_left_alone(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    st = k.state()
    st["closed"] = True
    k.store.save_pr(st)
    k.store.write_override("demo", "{not a flow")
    k.tick()
    assert k.gates()["review"]["state"] == "running" and k.runner.stopped == []
