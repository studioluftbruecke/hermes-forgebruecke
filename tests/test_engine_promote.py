"""The promote gate's own pull request, and what happens when the forge refuses to open it."""
from fb_fakes import HEAD_SHA
from fb_fakes_engine import FLOW, PROMOTE, Kit, engine_env  # noqa: F401


def test_a_pull_request_into_the_promote_target_gets_no_gates(tmp_path):
    k = Kit(tmp_path, flow=PROMOTE)
    k.staging()
    k.add_pr(102, title="Promote #3 to staging", base={"ref": "staging"}, head={"ref": "fix-mul", "sha": HEAD_SHA})
    k.transport.fail(lambda m, p: m == "POST" and p.endswith("/merge"), status=405, message="Please try again later")
    report = k.tick()
    k.tick()
    assert k.state(102) is None and k.comments(102) == []
    assert [p["pr"] for p in report["prs"]] == [3]


def test_a_review_an_older_version_started_on_a_promotion_pull_request_ends(tmp_path):
    k = Kit(tmp_path)
    k.add_pr(102, base={"ref": "staging"})
    k.tick()
    assert k.gates(102)["review"]["state"] == "running"
    k.set_flow({**FLOW, "gates": [*FLOW["gates"][:2], PROMOTE["gates"][0], FLOW["gates"][2]]})
    k.tick()
    k.tick()
    assert k.gates(102)["review"]["detail"] == "this pull request promotes another one, so it runs no gates."
    assert k.runner.stopped == [1001, 1002] and k.store.running_reviews() == 1


def test_a_refused_promotion_pull_request_fails_the_gate_once(tmp_path):
    k = Kit(tmp_path, flow=PROMOTE)
    k.staging()
    k.transport.fail(lambda m, p: m == "POST" and p.endswith("/pulls"), status=422, message="head branch does not exist")
    reports = [k.tick() for _ in range(3)]
    promote = k.gates()["promote"]
    assert promote["state"] == "failed"
    assert promote["detail"].startswith("the pull request into staging could not be opened: ")
    assert [b["state"] for b in k.status_posts()] == ["failure"] and len(k.notifies()) == 1
    assert [r["errors"] for r in reports] == [[], [], []]


def test_no_merge_when_the_target_branch_is_missing_or_cannot_be_read(tmp_path):
    k = Kit(tmp_path / "missing", flow=PROMOTE)
    k.tick()
    assert k.gates()["promote"]["detail"] == "the branch staging does not exist; create it first"
    assert k.sent("/merge") == [] and k.sent("/pulls", "POST") == []
    k = Kit(tmp_path / "unreadable", flow=PROMOTE)
    k.staging()
    k.transport.fail(lambda m, p: m == "GET" and p.endswith("/branches/staging"))
    report = k.tick()
    assert k.sent("/merge") == [] and k.sent("/pulls", "POST") == [] and len(report["errors"]) == 1
    k.transport.heal()
    k.tick()
    assert len(k.sent("/merge", "POST")) == 1 and k.gates()["promote"]["state"] == "passed"
