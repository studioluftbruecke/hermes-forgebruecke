"""A tick that dies between a review worker's start and the save that records it starts no second worker:
a worker still running is taken over, and one that already answered has its verdict applied."""
from fb_fakes import DEMO, HEAD_SHA
from fb_fakes_engine import FLOW, Kit, RecordingRunner, Stopped


def die_at_the_save_after_start(k) -> None:
    start, save = k.runner.start, k.store.save_pr
    armed = []

    def start_(**kw):
        pid = start(**kw)
        armed.append(True)
        return pid

    def save_(st):
        if armed:
            armed.clear()
            raise Stopped()
        return save(st)

    k.runner.start, k.store.save_pr = start_, save_


def test_a_worker_that_answered_before_the_next_tick_is_taken_at_its_word(tmp_path):
    k = Kit(tmp_path)
    k.runner = RecordingRunner()
    die_at_the_save_after_start(k)
    try:
        k.tick()
    except Stopped:
        pass
    k.verdict("pass")  # the worker answered, then ended
    k.runner.dead.add(1001)
    k.tick()
    assert len(k.runner.started) == 1
    assert k.gates()["review"]["state"] == "passed"


def test_a_verdict_left_by_an_earlier_run_of_the_same_commit_is_not_taken(tmp_path):
    k = Kit(tmp_path)
    k.runner = RecordingRunner()
    k.tick()
    k.verdict("fail")
    k.runner.dead.add(1001)
    k.tick()
    assert k.gates()["review"]["state"] == "failed"
    k.set_flow({**FLOW, "version": 2})  # a new version runs every gate again on the same commit
    k.tick()
    assert len(k.runner.started) == 2
    review = k.gates()["review"]
    assert review["state"] == "running"
    assert not k.store.verdict_path("demo", DEMO, 3, HEAD_SHA, "review").exists()
