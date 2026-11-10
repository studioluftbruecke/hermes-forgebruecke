"""The engine's promise, checked at every forge request of a whole pull request's life: a tick that fails
or dies there is run again and ends where an undisturbed run ends. A refused request, or a death before the
request reached the forge, must change nothing at all. A death after the request landed, with the answer
lost, may at most repeat one comment."""
import pytest

from fb_fakes import HEAD_SHA, sub
from fb_fakes_engine import FLOW, NEW_SHA, PROMOTE, Kit, Stopped, engine_env  # noqa: F401

RETRIES = sub("client").RETRIES


class Dying:
    """Passes requests on, except request number ``at``. ``how`` says what happens there: the process stops
    before the forge saw the request (``unsent``) or after it landed (``lost``), or the forge answers 500 to
    it and to the client's retries (``refused``)."""

    def __init__(self, inner, at=None, how=""):
        self.inner, self.at, self.how, self.seen = inner, at, how, 0

    def __call__(self, *request):
        self.seen += 1
        if self.at is None or not 0 <= self.seen - self.at < (RETRIES + 1 if self.how == "refused" else 1):
            return self.inner(*request)
        if self.how == "refused":
            return 500, {"content-type": "application/json"}, b'{"message": "boom"}'
        if self.how == "lost":
            self.inner(*request)
        raise Stopped(f"at request {self.at}")


THIRD_SHA = "c" * 40


def review_life(k: Kit) -> list:
    """A push during the review, a fail, a push, a pass: every transition of the review flow."""
    return [k.tick, k.push, k.tick, lambda: k.verdict("fail", sha=NEW_SHA), k.tick, lambda: k.push(THIRD_SHA), k.tick,
            lambda: (k.verdict("pass", sha=THIRD_SHA), k.devops("success", THIRD_SHA)), k.tick]


def promote_life(k: Kit) -> list:
    return [k.tick]


def live(tmp_path, flow, life, at=None, how="") -> "tuple[dict, int]":
    k = Kit(tmp_path, flow=flow)
    k.staging()
    k.keep_opened_pulls()
    k.transport = dying = Dying(k.transport, at, how)
    for step in life(k):
        try:
            report = step()
        except Stopped:
            report = step()
        if isinstance(report, dict) and report.get("errors"):
            step()
    requests = dying.seen
    for _ in range(2):
        assert k.tick()["errors"] == []
    return {"gates": {key: g["state"] for key, g in k.gates().items()},
            "statuses": {sha: k.statuses(sha) for sha in (HEAD_SHA, NEW_SHA, THIRD_SHA)},
            "workers": len(k.runner.started), "merges": len(k.sent("/merge", "POST")),
            "promotion_prs": len(k.sent("/pulls", "POST")),
            "notifies": len(k.notifies()), "status_comments": len(k.comments()) - len(k.notifies())}, requests


@pytest.mark.parametrize("flow,life", [(FLOW, review_life), (PROMOTE, promote_life)], ids=["review", "promote"])
def test_a_tick_that_fails_or_dies_at_any_forge_request_ends_where_an_undisturbed_run_ends(tmp_path, flow, life):
    calm, requests = live(tmp_path / "calm", flow, life)
    assert calm["workers"] == (3 if flow is FLOW else 0) and calm["merges"] == (0 if flow is FLOW else 1)
    for at in range(1, requests + 1):
        assert live(tmp_path / f"unsent-{at}", flow, life, at, "unsent")[0] == calm, f"unsent at request {at}"
        refused, _ = live(tmp_path / f"refused-{at}", flow, life, at, "refused")
        if refused["gates"].get("promote") == "failed":  # a refused promotion or merge fails the gate for good
            assert refused["merges"] == 0 and refused["notifies"] == 1, f"refused at request {at}"
        else:
            assert refused == calm, f"refused at request {at}"
        lost, _ = live(tmp_path / f"lost-{at}", flow, life, at, "lost")
        repeated = {key: lost[key] - calm[key] for key in ("notifies", "status_comments")}
        assert {**lost, **{key: calm[key] for key in repeated}} == calm, f"lost at request {at}"
        assert sorted(repeated.values()) in ([0, 0], [0, 1]), f"lost at request {at}: {repeated}"
