"""When the loops tick again, and what a tick that could not start leaves behind."""
from fb_fakes import DEMO, sub
from fb_fakes_engine import FLOW, Kit, engine_env  # noqa: F401

loop = sub("flows.loop")


def test_a_tick_on_one_pull_request_does_not_postpone_the_full_tick(tmp_path):
    k = Kit(tmp_path)
    k.tick(only=(DEMO, 3))
    assert k.store.engine() == {}
    assert loop.due(k.store, 120, now=k.clock.t + 60)
    k.tick()
    assert k.store.engine()["at"] == int(k.clock.t)
    assert not loop.due(k.store, 120, now=k.clock.t + 60)


def test_a_stamp_from_the_future_does_not_stop_the_loops(tmp_path):
    k = Kit(tmp_path)
    k.clock.t += 400 * 24 * 3600
    k.tick()
    assert loop.due(k.store, 120, now=k.clock.t - 399 * 24 * 3600)


def test_a_tick_that_cannot_read_the_repositories_reports_it(tmp_path):
    k = Kit(tmp_path)
    k.transport.fail(lambda m, p: m == "GET" and p == "/user/repos")
    report = k.tick()
    assert report["ok"] is False and report["prs"] == []
    assert len(report["errors"]) == 1 and report["errors"][0].startswith("The tick could not read which flows run where (")
    assert k.store.engine()["ok"] is False and k.store.engine()["errors"] == report["errors"]


def test_a_repo_with_more_open_pull_requests_than_one_tick_advances_says_so(tmp_path):
    k = Kit(tmp_path, flow={**FLOW, "gates": FLOW["gates"][1:]})
    for n in range(4, 124):
        k.add_pr(n)
    report = k.tick()
    assert len(report["prs"]) == 100
    assert report["notes"] == [f"{DEMO}: 121 pull requests are open. The flow runs on the first 100 only."]
