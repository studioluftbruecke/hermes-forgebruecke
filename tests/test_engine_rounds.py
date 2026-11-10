"""Review rounds: a round is a review that asked for a fix, and only those count toward ``max_fix_rounds``."""
from fb_fakes_engine import FLOW, NEW_SHA, Kit, engine_env  # noqa: F401

TWO_ROUNDS = {**FLOW, "gates": [{"review": {"by": "forgebruecke-dev", "max_fix_rounds": 2}}, {"human": {"who": "acme"}}]}
THIRD_SHA = "c" * 40


def test_pushes_during_a_running_review_use_up_no_round(tmp_path):
    k = Kit(tmp_path, flow=TWO_ROUNDS)
    k.tick()
    k.push(NEW_SHA)
    k.tick()
    k.push(THIRD_SHA)
    k.tick()
    review = k.gates()["review"]
    assert review["state"] == "running" and review["head_sha"] == THIRD_SHA and len(k.runner.started) == 3
    assert review["detail"] == "review by forgebruecke-dev running (round 1 of 2)"


def test_a_review_that_passed_uses_up_no_round(tmp_path):
    k = Kit(tmp_path, flow=TWO_ROUNDS)
    k.tick()
    for sha in (NEW_SHA, THIRD_SHA):
        k.verdict("pass", sha=k.state()["head_sha"])
        k.tick()
        k.push(sha)
        k.tick()
    assert k.gates()["review"]["state"] == "running" and len(k.runner.started) == 3
    assert k.state()["rounds"] == {}


def test_two_reviews_that_asked_for_a_fix_block_the_third(tmp_path):
    k = Kit(tmp_path, flow=TWO_ROUNDS)
    k.tick()
    for sha in (NEW_SHA, THIRD_SHA):
        k.verdict("fail", sha=k.state()["head_sha"])
        k.tick()
        k.push(sha)
        k.tick()
    review = k.gates()["review"]
    assert review["state"] == "blocked" and "review rounds used up (2 of 2)" in review["detail"]
    assert len(k.runner.started) == 2 and k.state()["rounds"] == {"review": 2}
