"""What a change of the flow file runs again on an open pull request: a new ``version`` every gate, an edited
gate that gate and the ones after it, another list or order of gates every gate."""
from fb_fakes_engine import FLOW, Kit, engine_env  # noqa: F401

REVIEW, CHECK, HUMAN = FLOW["gates"]
CHECK_EDITED = {"check": {"contexts": ["devops/tests", "devops/deploy"], "timeout_min": 60}}
ONE_ROUND = {**FLOW, "gates": [{"review": {"by": "forgebruecke-dev", "max_fix_rounds": 1}}, HUMAN], "version": 1}


def passed_review_and_check(tmp_path) -> Kit:
    k = Kit(tmp_path)
    k.tick()
    k.verdict("pass", summary="fine")
    k.tick()
    assert k.gates()["review"]["state"] == k.gates()["check"]["state"] == "passed"
    return k


def test_a_new_version_runs_a_review_again_whose_rounds_were_used_up(tmp_path):
    k = Kit(tmp_path, flow=ONE_ROUND)
    k.tick()
    k.verdict("fail")
    k.tick()
    k.push()
    k.tick()
    blocked = k.gates()["review"]
    assert blocked["state"] == "blocked"
    assert blocked["detail"] == "review rounds used up (1 of 1). To start over, raise version in the flow file."
    k.set_flow({**ONE_ROUND, "version": 2})
    k.tick()
    review = k.gates()["review"]
    assert review["state"] == "running" and review["detail"].endswith("(round 1 of 1)")
    assert len(k.runner.started) == 2 and k.state()["rounds"] == {}


def test_a_new_version_runs_every_gate_again(tmp_path):
    k = passed_review_and_check(tmp_path)
    k.set_flow({**FLOW, "version": 2})
    k.tick()
    assert k.gates()["review"]["state"] == "running" and k.gates()["check"]["state"] == "pending"
    assert len(k.runner.started) == 2
    k.tick()
    assert len(k.runner.started) == 2


def test_a_new_version_stops_the_review_that_ran_under_the_old_one(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.set_flow({**FLOW, "version": 2})
    k.tick()
    assert k.runner.stopped == [1001] and len(k.runner.started) == 2


def test_an_edited_gate_runs_again_and_the_pass_before_it_stays(tmp_path):
    k = passed_review_and_check(tmp_path)
    posts = len([b for b in k.status_posts() if b["context"].endswith("/review")])
    k.set_flow({**FLOW, "gates": [REVIEW, CHECK_EDITED, HUMAN]})
    k.tick()
    review, check = k.gates()["review"], k.gates()["check"]
    assert review["state"] == "passed" and review["detail"] == "review by forgebruecke-dev: fine"
    assert check["state"] == "running" and check["detail"] == "waiting for devops/deploy"
    assert len(k.runner.started) == 1 and k.gates()["human"]["state"] == "pending"
    assert len([b for b in k.status_posts() if b["context"].endswith("/review")]) == posts


def test_an_edit_to_a_later_gate_leaves_a_running_review_alone(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.set_flow({**FLOW, "gates": [REVIEW, CHECK_EDITED, HUMAN]})
    k.tick()
    k.verdict("pass")
    k.tick()
    assert k.runner.stopped == [] and len(k.runner.started) == 1
    assert k.gates()["review"]["state"] == "passed" and k.gates()["check"]["detail"] == "waiting for devops/deploy"


def test_an_edited_first_gate_runs_every_gate_again(tmp_path):
    k = passed_review_and_check(tmp_path)
    k.set_flow({**FLOW, "gates": [{"review": {"by": "coding", "timeout_min": 10}}, CHECK, HUMAN]})
    k.tick()
    assert k.runner.started[-1][0] == "coding" and len(k.runner.started) == 2
    assert k.gates()["review"]["state"] == "running" and k.gates()["check"]["state"] == "pending"


def test_another_order_of_the_gates_runs_every_gate_again(tmp_path):
    k = passed_review_and_check(tmp_path)
    k.devops("pending")
    k.set_flow({**FLOW, "gates": [CHECK, REVIEW, HUMAN]})
    k.tick()
    assert [g["state"] for g in k.gates().values()] == ["running", "pending", "pending"]
    assert list(k.gates()) == ["check", "review", "human"]


def test_an_edit_and_a_push_between_two_ticks_end_on_the_new_head(tmp_path):
    k = passed_review_and_check(tmp_path)
    k.set_flow({**FLOW, "gates": [REVIEW, CHECK_EDITED, HUMAN]})
    k.push()
    k.tick()
    assert {g["head_sha"] for g in k.gates().values()} == {"b" * 40}
    assert k.gates()["review"]["state"] == "running" and len(k.runner.started) == 2


def test_a_tick_with_the_same_flow_file_saves_the_same_state(tmp_path):
    k = passed_review_and_check(tmp_path)
    saved = {key: value for key, value in k.state().items() if key != "updated_at"}
    k.tick()
    assert {key: value for key, value in k.state().items() if key != "updated_at"} == saved
    assert saved["flow_version"] == 1 and len(saved["gate_hashes"]) == 3
