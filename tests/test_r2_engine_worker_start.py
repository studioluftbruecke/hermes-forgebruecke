"""A tick can die between the start of a review worker and the save that records its pid. The next tick asks
the runner whether a worker for that run already lives, and takes it over instead of starting a second one."""
import pytest

from fb_fakes import HEAD_SHA
from fb_fakes_engine import FLOW, NEW_SHA, Kit, RecordingRunner, Stopped, engine_env  # noqa: F401

THIRD_SHA = "c" * 40


class Deaths:
    """Counts the store's saves and the runner's starts, and stops the process at one of them."""

    def __init__(self, k: Kit, at=None, how=""):
        self.at, self.how, self.saves, self.starts = at, how, 0, 0
        save, start = k.store.save_pr, k.runner.start

        def save_pr(state):
            self.saves += 1
            if self.how == "before_save" and self.saves == self.at:
                raise Stopped()
            save(state)
            if self.how == "after_save" and self.saves == self.at:
                raise Stopped()

        def runner_start(**kw):
            pid = start(**kw)
            self.starts += 1
            if self.how == "after_start" and self.starts == self.at:
                raise Stopped()
            return pid

        k.store.save_pr, k.runner.start = save_pr, runner_start


def kit(tmp_path) -> Kit:
    k = Kit(tmp_path)
    k.runner = RecordingRunner()
    return k


def live(tmp_path, at=None, how="") -> "tuple[dict, Deaths]":
    """A push during the review, a fail, a push, a pass. A step that stopped is run again."""
    k = kit(tmp_path)
    deaths = Deaths(k, at, how)
    life = [k.tick, k.push, k.tick, lambda: k.verdict("fail", sha=NEW_SHA), k.tick, lambda: k.push(THIRD_SHA), k.tick,
            lambda: (k.verdict("pass", sha=THIRD_SHA), k.devops("success", THIRD_SHA)), k.tick, k.tick, k.tick]
    for step in life:
        try:
            step()
        except Stopped:
            step()
    return {"gates": {key: g["state"] for key, g in k.gates().items()},
            "statuses": {sha: k.statuses(sha) for sha in (HEAD_SHA, NEW_SHA, THIRD_SHA)},
            "worker_heads": [prompt.name.split("-")[-2] for _, prompt in k.runner.started]}, deaths


def test_a_tick_that_died_right_after_the_start_takes_the_living_worker_over(tmp_path):
    k = kit(tmp_path)
    Deaths(k, 1, "after_start")
    with pytest.raises(Stopped):
        k.tick()
    assert k.state() is None
    k.tick()
    review = k.gates()["review"]
    assert len(k.runner.started) == 1 and review["state"] == "running"
    assert review["evidence"]["pid"] == 1001 and review["attempts"] == 1
    k.verdict("pass", summary="fine")
    k.tick()
    assert k.gates()["review"]["detail"] == "review by forgebruecke-dev: fine" and len(k.runner.started) == 1


def test_a_verdict_the_living_worker_wrote_before_the_next_tick_is_kept(tmp_path):
    k = kit(tmp_path)
    Deaths(k, 1, "after_start")
    with pytest.raises(Stopped):
        k.tick()
    k.verdict("pass", summary="fine")
    k.tick()
    k.tick()
    assert k.gates()["review"]["state"] == "passed" and len(k.runner.started) == 1


def test_a_recorded_worker_that_is_gone_is_replaced(tmp_path):
    k = kit(tmp_path)
    Deaths(k, 1, "after_start")
    with pytest.raises(Stopped):
        k.tick()
    k.runner.dead.add(1001)
    k.tick()
    assert len(k.runner.started) == 2 and k.gates()["review"]["evidence"]["pid"] == 1002


def test_a_tick_that_dies_at_any_save_or_worker_start_ends_where_an_undisturbed_run_ends(tmp_path):
    calm, counted = live(tmp_path / "calm")
    assert calm["worker_heads"] == [HEAD_SHA[:12], NEW_SHA[:12], THIRD_SHA[:12]] and counted.starts == 3
    assert calm["gates"] == {"review": "passed", "check": "passed", "human": "waiting"}
    points = [(how, at) for how, n in (("before_save", counted.saves), ("after_save", counted.saves),
                                        ("after_start", counted.starts)) for at in range(1, n + 1)]
    differ = {}
    for how, at in points:
        end, _ = live(tmp_path / f"{how}-{at}", at, how)
        if end != calm:
            differ[(how, at)] = {key: end[key] for key in calm if end[key] != calm[key]}
    assert differ == {}
