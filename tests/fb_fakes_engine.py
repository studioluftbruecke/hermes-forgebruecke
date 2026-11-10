"""Helpers for the engine tests: one pull request under one flow on the fake forge, a transport that can
fail chosen requests, a runner that starts nothing and a clock that only moves when a test moves it."""
from __future__ import annotations

import json

import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, make_token, sample_data, sub

state_mod = sub("flows.state")
engine_mod = sub("flows.engine")
access_mod = sub("access")

BOT = make_token("bot")
NEW_SHA = "b" * 40
FLOW = {"flow": "demo", "repos": [DEMO], "gates": [
    {"review": {"by": "forgebruecke-dev", "timeout_min": 10}},
    {"check": {"contexts": ["devops/tests"], "timeout_min": 60}},
    {"human": {"who": "acme"}}], "on_failure": "notify"}
PROMOTE = {**FLOW, "gates": [{"promote": {"to": "staging"}}, {"human": {}}]}


@pytest.fixture(autouse=True)
def engine_env(monkeypatch):
    """No waiting between the client's retries, and a fixed profile for the default review profile."""
    monkeypatch.setattr(sub("client"), "BACKOFF", (0.0, 0.0, 0.0))
    monkeypatch.setattr(sub("settings"), "this_profile", lambda: "coding")


class Stopped(BaseException):
    """The process died here. Not an ``Exception``, so nothing in the engine catches it."""


class FakeRunner:
    def __init__(self):
        self.started, self.stopped, self.dead = [], [], set()

    def start(self, *, profile, prompt_path, log_path):
        self.started.append((profile, prompt_path))
        return 1000 + len(self.started)

    def alive(self, pid):
        return pid not in self.dead

    def stop(self, pid):
        self.stopped.append(pid)


class RecordingRunner(FakeRunner):
    """Answers ``recorded_pid`` as the real runner does from the record it keeps beside a worker's log: the
    pid of the worker it started for that log, while that worker lives."""

    def __init__(self):
        super().__init__()
        self.records: dict = {}

    def start(self, *, profile, prompt_path, log_path):
        self.records[str(log_path)] = pid = super().start(profile=profile, prompt_path=prompt_path, log_path=log_path)
        return pid

    def recorded_pid(self, log_path):
        pid = self.records.get(str(log_path))
        return pid if pid is not None and pid not in self.dead and pid not in self.stopped else None


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Faulty:
    """Wraps a transport. ``fail(match, status)`` answers ``status`` wherever ``match(method, path)`` is true;
    ``on(match, answer)`` hands the request to ``answer(inner, method, url, headers, body, timeout)``."""

    def __init__(self, inner):
        self.inner = inner
        self.rules: list = []
        self.hits: list = []

    def fail(self, match, status=500, message="boom"):
        payload = json.dumps({"message": message}).encode()
        self.on(match, lambda *_: (status, {"content-type": "application/json"}, payload))

    def on(self, match, answer):
        self.rules.append((match, answer))

    def heal(self):
        self.rules.clear()

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("/api/v1", 1)[-1].split("?", 1)[0]
        for match, answer in self.rules:
            if match(method, path):
                self.hits.append((method, path))
                return answer(self.inner, method, url, headers, body, timeout)
        return self.inner(method, url, headers, body, timeout)


class Kit:
    def __init__(self, tmp_path, *, flow=None, flows=("demo",), dry_run=False, account=None, rules=None, values=None):
        data = sample_data()
        data[DEMO]["pulls"][3]["state"] = "open"
        self.forge = FakeForgejo({BOT: account or bot_account()}, data=data)
        self.transport = Faulty(self.forge)
        self.all = data
        self.data = data[DEMO]
        self.values = {"url": BASE, "dry_run": dry_run,
                       "writes": rules if rules is not None else [{"repo_glob": DEMO, "flows": list(flows)}],
                       **(values or {})}
        self.store = state_mod.FlowStore(tmp_path)
        self.set_flow(flow or FLOW)
        self.runner = FakeRunner()
        self.clock = Clock()

    def set_flow(self, flow: dict) -> None:
        self.store.write_override(flow["flow"], json.dumps(flow))

    def access(self):
        return access_mod.load_access(lambda k, d=None: self.values.get(k, d),
                                      {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get, transport=self.transport)

    def tick(self, only=None) -> dict:
        return engine_mod.Engine(self.access(), self.store, runner=self.runner, now=self.clock).tick(only=only)

    def state(self, pr=3, repo=DEMO):
        return self.store.load_pr(repo, pr)

    def gates(self, pr=3, repo=DEMO) -> dict:
        return {g["gate_key"]: g for g in self.state(pr, repo)["gates"]}

    def verdict(self, verdict, sha=HEAD_SHA, summary="mul returns a + b", pr=3) -> None:
        path = self.store.verdict_path("demo", DEMO, pr, sha, "review")
        path.write_text(json.dumps({"verdict": verdict, "head_sha": sha, "summary": summary, "findings": []}))

    def statuses(self, sha=HEAD_SHA) -> dict:
        return {s["context"]: s["status"] for s in self.data["status"].get(sha, {}).get("statuses", [])}

    def push(self, sha=NEW_SHA, pr=3) -> None:
        self.data["pulls"][pr]["head"]["sha"] = sha

    def devops(self, state, sha=HEAD_SHA, creator="hermes") -> None:
        combined = self.data["status"].setdefault(sha, {"state": "", "statuses": [], "total_count": 0})
        combined["statuses"] = [{"context": "devops/tests", "status": state, "description": "tests",
                                 "creator": {"login": creator}}] + [s for s in combined["statuses"]
                                                                    if s["context"] != "devops/tests"]

    def add_pr(self, number: int, **over) -> None:
        self.data["pulls"][number] = {**self.data["pulls"][3], "number": number,
                                      "head": {"ref": f"b{number}", "sha": f"{number:040x}"}, **over}
        self.data["diff"][number] = "diff --git a/x b/x\n"

    def staging(self, **over) -> None:
        self.data["branches"]["staging"] = {"name": "staging", "protected": False, "commit": {"id": "1" * 40}, **over}

    def keep_opened_pulls(self) -> None:
        """A pull request the engine opens can then be read, as on a real forge (the fake only answers the POST)."""
        def keep(inner, method, url, headers, body, timeout):
            status, hdrs, payload = inner(method, url, headers, body, timeout)
            made, asked = json.loads(payload), json.loads(body)
            self.data["pulls"][made["number"]] = {"number": made["number"], "state": "open", "merged": False,
                                                  "base": {"ref": asked["base"]},
                                                  "head": {"ref": asked["head"], "sha": HEAD_SHA}}
            return status, hdrs, payload

        self.transport.on(lambda m, p: m == "POST" and p.endswith("/pulls"), keep)

    def sent(self, suffix="", method=None) -> list:
        return [(m, p, b) for m, p, b in self.forge.writes if p.endswith(suffix) and (method is None or m == method)]

    def status_posts(self, state=None) -> list:
        return [b for m, p, b in self.forge.writes if "/statuses/" in p and (state is None or b["state"] == state)]

    def comments(self, pr=3) -> list:
        return [b["body"] for _, _, b in self.sent(f"/issues/{pr}/comments", "POST")]

    def notifies(self, pr=3) -> list:
        return [body for body in self.comments(pr) if body.startswith("@")]
