"""The engine with the real review runner and a real process, on a host without /proc (macOS). A tick of
another process than the one that started the worker still stops that worker."""
import shutil

import pytest

from fb_fakes import sub
from fb_fakes_engine import Kit, engine_env  # noqa: F401

runner_mod = sub("flows.runner")


@pytest.fixture
def worker_kit(tmp_path, monkeypatch):
    if shutil.which("ps") is None:
        pytest.skip("this host has no ps")
    monkeypatch.setattr(runner_mod, "_PROC", tmp_path / "no-proc")
    stand_in = tmp_path / "hermes-stand-in.sh"
    stand_in.write_text("#!/bin/sh\nexec sleep 60\n")
    stand_in.chmod(0o755)
    k = Kit(tmp_path / "state")
    k.runner = runner_mod.ReviewRunner(hermes_bin=str(stand_in))
    started = []

    def forget_the_worker():
        """What a gateway restart does: the process that ticks next never started this worker."""
        pid = k.gates()["review"]["evidence"]["pid"]
        started.append(runner_mod._PROCS.pop(pid))
        return started[-1]
    yield k, forget_the_worker
    for proc in started + list(runner_mod._PROCS.values()):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
    runner_mod._PROCS.clear()


def test_without_proc_a_restarted_gateway_stops_the_worker_of_a_review_that_timed_out(worker_kit):
    k, forget_the_worker = worker_kit
    k.tick()
    worker = forget_the_worker()
    k.clock.t += 11 * 60
    k.tick()
    assert k.gates()["review"]["state"] == "error"
    assert worker.wait(timeout=5) == -15


def test_without_proc_a_restarted_gateway_stops_the_worker_of_the_old_head_after_a_push(worker_kit):
    k, forget_the_worker = worker_kit
    k.tick()
    old = forget_the_worker()
    k.push()
    k.tick()
    assert old.wait(timeout=5) == -15
    new = forget_the_worker()
    assert new.pid != old.pid and new.poll() is None
