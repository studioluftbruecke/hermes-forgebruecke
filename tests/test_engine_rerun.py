"""A tick that stopped at any line is safe to run again: one worker per head, one notify per failure, one
merge per promotion, and every status the forge missed is sent by a later tick."""
from fb_fakes import DEMO
from fb_fakes_engine import FLOW, PROMOTE, Kit, Stopped, engine_env  # noqa: F401


def comment_post(method, path):
    return method == "POST" and path.endswith("/issues/3/comments")


def comment_edit(method, path):
    return method == "PATCH" and "/issues/comments/" in path


def status_post(method, path):
    return method == "POST" and "/statuses/" in path


def test_a_failing_status_comment_does_not_start_a_second_worker(tmp_path):
    k = Kit(tmp_path)
    k.transport.fail(comment_post)
    reports = [k.tick() for _ in range(3)]
    assert len(k.runner.started) == 1
    assert k.gates()["review"]["state"] == "running"
    assert len(k.status_posts("pending")) == 1
    assert all(any("status comment" in e for e in r["errors"]) for r in reports)
    k.transport.heal()
    assert k.tick()["errors"] == [] and len(k.comments()) == 1


def test_a_failing_status_post_is_sent_by_a_later_tick_and_starts_no_second_worker(tmp_path):
    k = Kit(tmp_path)
    k.transport.fail(status_post)
    assert any("a check could not be written" in e for e in k.tick()["errors"])
    k.tick()
    assert len(k.runner.started) == 1 and k.status_posts() == []
    k.verdict("pass", summary="fine")
    k.devops("pending")
    k.tick()
    assert k.gates()["review"]["state"] == "passed"
    k.transport.heal()
    k.tick()
    assert k.statuses()["forgebruecke/demo/review"] == "success"
    assert k.statuses()["forgebruecke/demo/check"] == "pending"
    sent = len(k.status_posts())
    k.tick()
    assert len(k.status_posts()) == sent and len(k.runner.started) == 1


def test_a_failing_comment_edit_does_not_repeat_the_failure_notify(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("fail")
    k.transport.fail(comment_edit)
    for _ in range(3):
        k.tick()
    assert len(k.notifies()) == 1
    assert len(k.status_posts("failure")) == 1


def test_a_failed_notify_is_sent_once_by_a_later_tick(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("fail")
    k.transport.fail(comment_post)
    report = k.tick()
    assert k.notifies() == [] and any("notify" in e for e in report["errors"])
    assert k.gates()["review"]["state"] == "failed"
    k.transport.heal()
    k.tick()
    k.tick()
    assert len(k.notifies()) == 1


def test_a_notify_gate_comments_once_when_the_status_comment_fails(tmp_path):
    k = Kit(tmp_path, flow={**FLOW, "gates": [{"notify": {"who": "acme", "message": "ready"}}, {"human": {}}]})

    def refuse_the_status_comment(inner, method, url, headers, body, timeout):
        if b"| Gate |" in body:
            return 500, {"content-type": "application/json"}, b'{"message": "boom"}'
        return inner(method, url, headers, body, timeout)

    k.transport.on(comment_post, refuse_the_status_comment)
    for _ in range(3):
        k.tick()
    assert len([c for c in k.comments() if "ready" in c]) == 1


def test_a_failing_branch_read_after_the_merge_does_not_merge_again(tmp_path):
    k = Kit(tmp_path, flow=PROMOTE)
    k.staging()
    k.transport.fail(lambda m, p: m == "GET" and p.endswith("/branches/staging") and bool(k.sent("/merge")))
    for _ in range(3):
        k.tick()
    assert len(k.sent("/merge", "POST")) == 1 and len(k.sent("/pulls", "POST")) == 1
    assert k.gates()["promote"]["state"] == "running"
    k.transport.heal()
    k.tick()
    assert k.gates()["promote"]["state"] == "passed" and len(k.sent("/merge", "POST")) == 1


def test_a_tick_that_died_during_the_merge_call_asks_the_forge_before_merging_again(tmp_path):
    k = Kit(tmp_path, flow=PROMOTE)
    k.staging()

    def merge_then_die(inner, method, url, headers, body, timeout):
        inner(method, url, headers, body, timeout)
        raise Stopped("the process was stopped")

    k.keep_opened_pulls()
    k.transport.on(lambda m, p: m == "POST" and p.endswith("/merge"), merge_then_die)
    try:
        k.tick()
    except Stopped:
        pass
    assert len(k.sent("/merge", "POST")) == 1 and k.gates()["promote"]["state"] == "running"
    k.tick()
    assert len(k.sent("/merge", "POST")) == 1 and len(k.sent("/pulls", "POST")) == 1
    assert k.gates()["promote"]["state"] == "passed"


def test_state_is_on_disk_before_the_first_forge_write_of_a_new_review(tmp_path):
    k = Kit(tmp_path)
    seen = []

    def look(inner, method, url, headers, body, timeout):
        st = k.state()
        seen.append(st and st["gates"][0]["state"])
        return inner(method, url, headers, body, timeout)

    k.transport.on(lambda m, p: m != "GET" and DEMO in p, look)
    k.tick()
    assert seen and seen[0] == "running"
