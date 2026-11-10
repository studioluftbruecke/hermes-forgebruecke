"""Dry run: the engine reports what it would do and leaves nothing behind, on the forge or on disk."""
from fb_fakes import DEMO
from fb_fakes_engine import PROMOTE, Kit, Stopped, engine_env  # noqa: F401


def test_dry_run_starts_no_worker_and_saves_no_state(tmp_path):
    k = Kit(tmp_path, dry_run=True)
    k.tick()
    report = k.tick()
    assert k.runner.started == [] and k.state() is None
    assert [c for c in k.forge.calls if c[0] != "GET"] == []
    assert f"review_start {DEMO}#3 (dry run)" in report["done"]
    assert report["prs"][0]["gates"][0] == {"key": "review", "state": "running",
                                            "detail": "dry run: would start a review by forgebruecke-dev"}


def test_going_live_after_dry_run_acts_as_on_a_fresh_pull_request(tmp_path):
    k = Kit(tmp_path, dry_run=True, flow=PROMOTE)
    k.staging()
    k.tick()
    report = k.tick()
    assert report["prs"][0]["gates"][0]["detail"] == "dry run: would merge fix-mul into staging"
    assert k.sent("/merge") == []
    k.values["dry_run"] = False
    k.tick()
    assert len(k.sent("/merge", "POST")) == 1
    assert k.statuses()["forgebruecke/demo/promote"] == "success" and len(k.comments()) == 1


def test_a_dry_run_tick_leaves_a_running_review_alone(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    before = k.state()
    k.values["dry_run"] = True
    k.clock.t += 11 * 60
    report = k.tick()
    assert report["prs"][0]["gates"][0]["state"] == "error"
    assert k.runner.stopped == [] and k.state() == before


def test_a_dry_run_tick_does_not_finish_a_promote_a_live_tick_began(tmp_path):
    k = Kit(tmp_path, flow=PROMOTE)
    k.staging()

    def die_before_the_merge(inner, method, url, headers, body, timeout):
        raise Stopped("the process was stopped")

    k.keep_opened_pulls()
    k.transport.on(lambda m, p: m == "POST" and p.endswith("/merge"), die_before_the_merge)
    try:
        k.tick()
    except Stopped:
        pass
    k.transport.heal()
    k.keep_opened_pulls()
    assert k.gates()["promote"]["state"] == "running" and len(k.sent("/pulls", "POST")) == 1
    k.values["dry_run"] = True
    report = k.tick()
    assert k.sent("/merge") == [] and f"promote_merge {DEMO} staging (dry run)" in report["done"]
    k.values["dry_run"] = False
    k.tick()
    assert len(k.sent("/merge", "POST")) == 1 and k.gates()["promote"]["state"] == "passed"
