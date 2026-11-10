"""A paused flow starts nothing new, and a review that already runs finishes: its verdict counts."""
from fb_fakes import DEMO, bot_account, sub
from fb_fakes_engine import FLOW, Kit, engine_env  # noqa: F401

flows_tool = sub("tools.flows")
approval = sub("approval")
OTHER = "acme/extra-0"


def pause(k: Kit) -> dict:
    return flows_tool.handle(k.access(), k.store, {"action": "pause", "flow": "demo"},
                             engine_factory=lambda access, store: None, run=approval.RunContext())


def test_pause_says_running_reviews_finish_and_the_next_ticks_let_one_finish(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    assert "Running reviews finish" in pause(k)["message"]
    k.tick()
    assert k.runner.stopped == [] and k.gates()["review"]["state"] == "running"
    k.verdict("pass", summary="fine")
    report = k.tick()
    review = k.gates()["review"]
    assert review["state"] == "passed" and review["detail"] == "review by forgebruecke-dev: fine"
    assert k.statuses()["forgebruecke/demo/review"] == "success" and k.runner.stopped == []
    assert [p["pr"] for p in report["prs"]] == [3]
    assert "| review by forgebruecke-dev | ✅ passed |" in k.sent("", "PATCH")[-1][2]["body"]


def test_a_paused_flow_starts_no_gate_after_the_review_that_finished(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.store.set_paused("demo", True)
    k.verdict("pass")
    k.tick()
    k.tick()
    assert k.gates()["check"]["state"] == "pending" and len(k.runner.started) == 1
    k.store.set_paused("demo", False)
    k.tick()
    assert k.gates()["check"]["state"] == "passed" and k.gates()["human"]["state"] == "waiting"


def test_a_review_that_asked_for_a_fix_during_a_pause_counts_a_round_and_notifies_once(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.store.set_paused("demo", True)
    k.verdict("fail")
    k.tick()
    k.tick()
    assert k.gates()["review"]["state"] == "failed" and k.state()["rounds"] == {"review": 1}
    assert k.statuses()["forgebruecke/demo/review"] == "failure" and len(k.notifies()) == 1


def test_a_paused_tick_with_nothing_new_writes_nothing(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.store.set_paused("demo", True)
    sent, saved = len(k.forge.writes), k.store.pr_path(DEMO, 3).read_text()
    report = k.tick()
    assert report["prs"] == [] and len(k.forge.writes) == sent and k.store.pr_path(DEMO, 3).read_text() == saved


def test_a_paused_review_whose_worker_is_gone_ends_in_error_and_frees_the_slot(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.store.set_paused("demo", True)
    k.runner.dead.add(1001)
    k.tick()
    review = k.gates()["review"]
    assert review["state"] == "error" and "ended without writing a verdict" in review["detail"]
    assert k.store.running_reviews() == 0 and k.statuses()["forgebruecke/demo/review"] == "error"
    sent = len(k.forge.writes)
    assert k.tick()["prs"] == [] and len(k.forge.writes) == sent


def test_a_paused_review_that_runs_out_of_time_is_stopped(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.store.set_paused("demo", True)
    k.clock.t += 11 * 60
    k.tick()
    assert k.runner.stopped == [1001] and "timed out after 10 min" in k.gates()["review"]["detail"]


def test_a_gate_edited_during_a_pause_stops_the_review_that_ran_under_the_old_one(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.store.set_paused("demo", True)
    k.set_flow({**FLOW, "gates": [{"review": {"by": "coding"}}, *FLOW["gates"][1:]]})
    k.tick()
    assert k.runner.stopped == [1001] and k.gates()["review"]["state"] == "pending"
    assert len(k.runner.started) == 1 and k.store.running_reviews() == 0


def test_reviews_in_another_repo_start_once_the_paused_ones_finished(tmp_path):
    rules = [{"repo_glob": DEMO, "flows": ["demo"]}, {"repo_glob": OTHER, "flows": ["other"]}]
    k = Kit(tmp_path, account=bot_account(2), rules=rules)
    k.set_flow({**FLOW, "flow": "other", "repos": [OTHER]})
    k.add_pr(4)
    k.all[OTHER] = {"info": {"default_branch": "main"}, "diff": {7: "diff --git a/y b/y\n"}, "status": {},
                    "pulls": {7: {**k.data["pulls"][3], "number": 7, "head": {"ref": "b7", "sha": "7" * 40}}}}
    k.tick()
    k.store.set_paused("demo", True)
    k.tick()
    assert k.gates(7, OTHER)["review"]["state"] == "pending" and k.runner.stopped == []
    k.verdict("pass")
    k.verdict("pass", sha=f"{4:040x}", pr=4)
    k.tick()
    k.tick()
    assert k.gates(7, OTHER)["review"]["state"] == "running"
