"""A failure notify is sent once per run of a gate. A gate that runs again on the same head (a new flow version,
an edited gate, or a gate after an edited one) and fails again notifies again; a notify the forge stored but
answered with an error is still not posted twice."""
from fb_fakes_engine import FLOW, Kit
from fb_fakes_rr_engine import list_posted_comments, stored_but_failed

COMMENT_POST = (lambda m, p: m == "POST" and p.endswith("/issues/3/comments"))


def failed_review(k) -> None:
    k.tick()  # starts the worker
    k.verdict("fail", summary="mul is wrong")
    k.tick()  # reads the verdict, notifies


def test_a_rerun_after_a_new_version_that_fails_again_notifies_again(tmp_path):
    k = Kit(tmp_path)
    list_posted_comments(k)
    failed_review(k)
    assert len(k.notifies()) == 1
    k.set_flow({**FLOW, "version": 2})
    failed_review(k)
    assert len(k.runner.started) == 2
    assert k.gates()["review"]["state"] == "failed"
    assert len(k.notifies()) == 2


def test_an_edited_gate_that_fails_again_notifies_again_and_the_passes_before_it_stay(tmp_path):
    k = Kit(tmp_path)
    list_posted_comments(k)
    k.devops("failure")
    k.tick()
    k.verdict("pass")
    k.tick()  # review passes, the check fails
    assert k.gates()["check"]["state"] == "failed"
    assert len(k.notifies()) == 1
    edited = {**FLOW, "gates": [FLOW["gates"][0], {"check": {"contexts": ["devops/tests"], "timeout_min": 90}},
                                FLOW["gates"][2]]}
    k.set_flow(edited)
    k.tick()
    assert len(k.runner.started) == 1
    assert k.gates()["review"]["state"] == "passed"
    assert k.gates()["check"]["state"] == "failed"
    assert len(k.notifies()) == 2


def test_one_failing_run_notifies_once_over_many_ticks(tmp_path):
    k = Kit(tmp_path)
    list_posted_comments(k)
    failed_review(k)
    for _ in range(4):
        k.tick()
    assert len(k.notifies()) == 1


def test_a_rerun_notify_answered_502_is_still_posted_once(tmp_path):
    k = Kit(tmp_path)
    list_posted_comments(k)
    failed_review(k)
    k.set_flow({**FLOW, "version": 2})
    k.tick()
    k.verdict("fail", summary="mul is still wrong")
    stored_but_failed(k, COMMENT_POST)
    for _ in range(4):
        k.tick()
    assert len(k.notifies()) == 2
