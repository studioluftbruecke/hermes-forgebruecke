"""What the engine hands to the review runner and the review-request sweep, including the keywords newer
versions of them take: ``diff_too_large``, ``log_path`` and ``data_dir``."""
from fb_fakes import sub
from fb_fakes_engine import FakeRunner, Kit, engine_env  # noqa: F401

runner_mod = sub("flows.runner")
review_requests = sub("review_requests")


def test_a_diff_the_runner_calls_too_large_blocks_the_review_and_starts_no_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "diff_too_large", lambda diff: len(diff) > 1000, raising=False)
    k = Kit(tmp_path)
    k.data["diff"][3] = "+" * 1500
    k.tick()
    review = k.gates()["review"]
    assert review["state"] == "blocked" and k.runner.started == []
    assert review["detail"] == ("The diff is too large to review (1500 characters). "
                                "Review it yourself or split the pull request.")
    assert k.statuses()["forgebruecke/demo/review"] == "failure" and len(k.notifies()) == 1


def test_a_diff_within_the_limit_is_reviewed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "diff_too_large", lambda diff: len(diff) > 1000, raising=False)
    k = Kit(tmp_path)
    k.tick()
    assert k.gates()["review"]["state"] == "running" and len(k.runner.started) == 1


class RunnerWithLogs(FakeRunner):
    def __init__(self):
        super().__init__()
        self.asked = []

    def alive(self, pid, *, log_path=None):
        self.asked.append(("alive", pid, log_path))
        return True

    def stop(self, pid, *, log_path=None):
        self.asked.append(("stop", pid, log_path))


def test_a_runner_that_takes_the_log_path_gets_it_with_every_pid(tmp_path):
    k = Kit(tmp_path)
    k.runner = RunnerWithLogs()
    k.tick()
    log = k.runner.started[0][1].parent / (k.runner.started[0][1].name.replace(".prompt.md", ".log"))
    k.tick()
    k.push()
    k.tick()
    assert k.runner.asked == [("alive", 1001, log), ("stop", 1001, log)]


def test_the_sweep_gets_the_data_folder_and_none_in_dry_run(tmp_path, monkeypatch):
    seen = []

    def sweep(access, request, *, data_dir=None):
        seen.append(data_dir)
        return {"requested": [], "notes": [], "errors": []}

    monkeypatch.setattr(review_requests, "sweep", sweep)
    k = Kit(tmp_path)
    k.tick()
    k.values["dry_run"] = True
    k.tick()
    assert seen == [k.store.root, None]


def test_an_error_inside_the_sweep_is_reported_and_not_retried_without_the_folder(tmp_path, monkeypatch):
    calls = []

    def sweep(access, request, *, data_dir=None):
        calls.append(data_dir)
        raise TypeError("a bug inside the sweep")

    monkeypatch.setattr(review_requests, "sweep", sweep)
    k = Kit(tmp_path)
    report = k.tick()
    assert calls == [k.store.root]
    assert "review requests: TypeError: a bug inside the sweep" in report["errors"]
