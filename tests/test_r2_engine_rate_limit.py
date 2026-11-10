"""One answer 429 anywhere in a tick ends the tick: no further request, no sleep under the tick lock. The tick
that follows goes on and ends where an undisturbed run ends."""
import copy
import json

import pytest

from fb_fakes import DEMO, bot_account, sample_data, sub
from fb_fakes_engine import FLOW, Kit, engine_env  # noqa: F401

client_mod = sub("client")
access_mod = sub("access")
REPOS = [DEMO, "acme/extra-0", "acme/extra-1"]
EVERYWHERE = {**FLOW, "repos": ["acme/*"]}


@pytest.fixture
def slept(monkeypatch) -> list:
    """Every client the tick builds records its sleeps here and waits for none."""
    sleeps: list = []
    init = client_mod.ForgejoClient.__init__
    monkeypatch.setattr(client_mod.ForgejoClient, "__init__",
                        lambda self, *args, **kw: init(self, *args, **{**kw, "sleep": sleeps.append}))
    return sleeps


def kit(tmp_path) -> Kit:
    """Three repos under one flow, two open pull requests each, and none of them asks ``you`` yet."""
    access_mod.clear_rights()  # rights are remembered per process, and every tick here must send the same requests
    k = Kit(tmp_path, flow=EVERYWHERE, account=bot_account(len(REPOS)), rules=[{"repo_glob": "acme/*", "flows": ["demo"]}],
            values={"you": "acme", "review_every_pr": True})
    base = sample_data()[DEMO]
    base["pulls"][3]["state"] = "open"
    for name in REPOS[1:]:
        k.all[name] = copy.deepcopy(base)
        for issue in k.all[name]["issues"].values():
            issue["repository"] = {"full_name": name}
    for name in REPOS:
        data = k.all[name]
        data["pulls"][4] = {**data["pulls"][3], "number": 4, "head": {"ref": "b4", "sha": f"{4:040x}"}}
        data["diff"][4] = "diff --git a/x b/x\n"
        data["issues"][4] = {**data["issues"][3], "number": 4}
        data["reviews"] = {}
        for pr in data["pulls"].values():
            pr["requested_reviewers"] = []
            pr["user"] = {"login": "hermes"}
    return k


def limit_from(k: Kit, start: int) -> None:
    """From request number ``start`` on, every request answers 429 with ``Retry-After: 30``."""
    seen = {"n": 0}

    def reached(method, path):
        seen["n"] += 1
        return seen["n"] >= start

    k.transport.on(reached, lambda *_: (429, {"content-type": "application/json", "retry-after": "30"},
                                        json.dumps({"message": "slow down"}).encode()))


def end_state(k: Kit) -> dict:
    return {"gates": {f"{name}#{n}": [g["state"] for g in k.gates(n, name).values()] for name in REPOS for n in (3, 4)},
            "workers": sorted(prompt.name for _, prompt in k.runner.started),
            "asked": {f"{name}#{n}": [u["login"] for u in k.all[name]["pulls"][n]["requested_reviewers"]]
                      for name in REPOS for n in (3, 4)},
            "status_posts": len(k.status_posts()), "comments": len(k.sent("/comments", "POST"))}


def requests_of_a_calm_tick(tmp_path) -> int:
    k = kit(tmp_path)
    count = {"n": 0}
    k.transport.on(lambda method, path: count.update(n=count["n"] + 1) or False, None)
    assert k.tick()["errors"] == []
    return count["n"]


def test_a_429_at_any_request_of_a_tick_ends_the_tick_there(tmp_path, slept):
    total = requests_of_a_calm_tick(tmp_path / "calm")
    assert total > 40
    went_on = {}
    for start in range(1, total + 1):
        k = kit(tmp_path / str(start))
        limit_from(k, start)
        report = k.tick()
        limited = [e for e in report["errors"] if "rate limiting" in e]
        if len(k.transport.hits) != 1 or slept or len(limited) != 1 or report["ok"]:
            went_on[start] = (len(k.transport.hits), sum(slept), report["errors"][:2])
        slept.clear()
    assert went_on == {}


def test_the_tick_after_a_rate_limit_ends_where_an_undisturbed_run_ends(tmp_path, slept):
    calm = kit(tmp_path / "calm")
    for _ in range(3):
        assert calm.tick()["errors"] == []
    total = requests_of_a_calm_tick(tmp_path / "count")
    differ = {}
    for start in range(1, total + 1):
        k = kit(tmp_path / str(start))
        limit_from(k, start)
        k.tick()
        k.transport.heal()
        for _ in range(3):
            assert k.tick()["errors"] == []
        if end_state(k) != end_state(calm):
            differ[start] = {key: value for key, value in end_state(k).items() if value != end_state(calm)[key]}
    assert differ == {}


def test_the_sentence_says_what_was_seen_and_what_happens_next(tmp_path, slept):
    k = kit(tmp_path)
    limit_from(k, 5)
    report = k.tick()
    assert report["errors"][-1] == "Forgejo is rate limiting Hermes. This tick stopped here; the next one goes on."
    assert k.store.engine()["ok"] is False
