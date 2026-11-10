"""The flows engine: on every tick, move each open pull request one step along its flow.

PRODUCT.md § Rules the engine enforces, as code:

1. One PR, one head commit, ordered gates. A push when no gate is failed, error or blocked resets every
   gate, so the commit that waits for the merge is the one that was reviewed and checked. A push after a
   failure resets the gates from the failed one to the end. Gates before it carry over, and their success
   status is posted again on the new head, saying where it came from. A check or a promote never carries
   over. ``on_push: reset_all`` resets everything.
2. Gate order is data: a later gate never starts before every earlier one passed.
3. The engine posts statuses only for its own gates (``forgebruecke/<flow>/<key>``) and reads the rest;
   a check gate can name the accounts allowed to post a context (``creators``), and others are flagged.
4. No gate merges into ``main``: the validator refuses ``to: main``, and before merging the engine
   re-reads the target branch and refuses a protected branch or the repo's default branch.
5. Two ticks never act at once (``FlowStore.tick_lock``). A review whose process is gone, or a ``pending``
   status no running gate owns, is repaired to ``error``.
6. Every gate the engine starts ends in ``success``, ``failure`` or ``error``, never ``pending`` for good,
   because the demo's protection requires ``forgebruecke/*`` and a stuck status blocks the merge.
7. A tick that stopped at any line is safe to run again. The state is saved right after each step that must
   not repeat (a worker start, the merge call, a notify). Statuses and comments are sent after that, from the
   saved state, and a later tick sends what the forge missed.
8. A rate-limited forge ends the tick at that request (``RateLimited``): nothing waits under the tick lock, and
   the next tick goes on.
9. A gate's stored result belongs to the definition it ran under. A new ``version`` or another list of gates
   runs every gate again. An edited gate runs that gate and the ones after it, and the passes before it stay.
10. A paused flow starts no gate. A review that already runs finishes, and its verdict counts.

Unattended by nature: the engine does not call the agent's write tools (those refuse unattended runs). It
writes through ``FlowWriter``, which can do exactly what a flow names for a gate: a status in the flow's
own namespace, the one status comment per PR (edited in place), a notify comment, and, in ``promote``
only, opening and merging a pull request into the unprotected branch the flow names. Outside any flow, a
tick also asks ``you`` to review every open pull request that does not ask yet (``review_requests.py``).
With ``dry_run`` on it records those writes, sends nothing, starts no review worker and saves no state.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import importlib
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

MAX_PARALLEL_REVIEWS = 2
MAX_OPEN_PRS = 100  # per repo and tick
FOOTER = "\n\n<sub>Kept up to date by Forgebrücke (Hermes). The merge into the target branch is yours.</sub>"
ICONS = {"passed": "✅", "failed": "❌", "error": "⚠️", "running": "⏳", "pending": "·", "waiting": "👤",
         "blocked": "⛔", "skipped": "↷"}
STOPPED_STATES = ("failed", "error", "blocked")
FORGE_STATE = {"running": "pending", "passed": "success", "failed": "failure", "blocked": "failure", "error": "error"}
SILENT_GATES = ("human", "notify")  # these gates post no status
HEAD_BOUND_GATES = ("check", "promote")  # what they passed is true for one commit, so a push runs them again

logger = logging.getLogger(__name__)


def _mod(name: str):
    pkg = (__package__ or "").rpartition(".")[0] or __package__
    return importlib.import_module(f".{name}", pkg)


def _desc(text: str) -> str:
    return " ".join(str(text or "").split())[:140]


def _accepts(call: Callable, keyword: str) -> bool:
    """True when ``call`` takes ``keyword``. The runner and the review-request sweep gained keywords over
    time, and the engine also runs against the ones without them."""
    try:
        return keyword in inspect.signature(call).parameters
    except (TypeError, ValueError):
        return False


class RateLimited(BaseException):
    """Forgejo answered 429. Not an ``Exception``, so no handler between the request and the tick can go on
    to the next request."""


def _ending_the_tick_at_429(transport: Callable) -> Callable:
    """The transport for every client of a tick. A request answered 429 changed nothing, so stopping there
    is safe by rule 7, and the client never sleeps for a retry while the tick holds its lock."""
    def send(method, url, headers, body, timeout):
        answer = transport(method, url, headers, body, timeout)
        if answer[0] == 429:
            raise RateLimited()
        return answer
    return send


class FlowWriter:
    """The only writes an unattended flow run can make."""

    def __init__(self, access: Any, flow_id: str, *, dry_run: bool, log: list) -> None:
        self._access = access
        self.flow_id = flow_id
        self.dry_run = dry_run
        self.log = log

    def _client(self):
        return self._access.writer()

    def _record(self, what: str, **data: Any) -> None:
        self.log.append({"write": what, "dry_run": self.dry_run, **data})

    def status(self, repo: str, sha: str, key: str, state: str, description: str, target_url: str = "") -> None:
        context = f"forgebruecke/{self.flow_id}/{key}"
        body = {"context": context, "state": state, "description": _desc(description)}
        if target_url:
            body["target_url"] = target_url
        self._record("status", repo=repo, sha=sha[:12], context=context, state=state)
        if not self.dry_run:
            self._client().post(f"/repos/{repo}/statuses/{sha}", body)

    def comment(self, repo: str, number: int, body: str) -> Optional[int]:
        self._record("comment", repo=repo, number=number, chars=len(body))
        if self.dry_run:
            return None
        return (self._client().post(f"/repos/{repo}/issues/{number}/comments", {"body": body}).data or {}).get("id")

    def request_review(self, repo: str, number: int, login: str) -> None:
        self._record("review_request", repo=repo, number=number, reviewer=login)
        if not self.dry_run:
            self._client().post(f"/repos/{repo}/pulls/{number}/requested_reviewers", {"reviewers": [login]})

    def edit_comment(self, repo: str, comment_id: int, body: str) -> bool:
        """False when the comment cannot be edited, so the caller posts a new one and keeps its id: gone
        (404, deleted by a person), or written by another account (403: a non-admin collaborator may edit only
        its own comments, measured on Forgejo 15, e.g. a status comment the retired bot account posted)."""
        self._record("edit_comment", repo=repo, comment_id=comment_id, chars=len(body))
        if self.dry_run:
            return True
        ForgejoError = _mod("client").ForgejoError
        try:
            self._client().patch(f"/repos/{repo}/issues/comments/{comment_id}", {"body": body})
            return True
        except ForgejoError as e:
            if e.status in (403, 404):
                return False
            raise


def write_line(w: dict) -> str:
    """One write of a tick as a short line for the channel log, so a write count names its target:
    ``review_request acme/app#7 (dry run)``. Never a body or a token."""
    where = w.get("repo", "")
    if w.get("number") is not None:
        where += f"#{w['number']}"
    elif w.get("sha"):
        where += f"@{w['sha']}"
    extra = w.get("state") or w.get("to") or ""
    return " ".join(p for p in (w.get("write", "?"), where, extra, "(dry run)" if w.get("dry_run") else "") if p)


class Engine:
    def __init__(self, access: Any, store: Any, *, runner: Any = None, now: Callable[[], float] = time.time,
                 source: str = "tick") -> None:
        flow_access = access.for_lane("flow")  # the agent token only, never the user's tokens
        transport = flow_access.transport or _mod("client").default_transport
        self.access = dataclasses.replace(flow_access, transport=_ending_the_tick_at_429(transport))
        self.settings = access.settings
        self.store = store
        self.runner = runner if runner is not None else _mod("flows.runner").ReviewRunner()
        self.now = now
        self.source = source
        self.writes: list = []
        self.errors: list = []
        self.models = _mod("models")

    # --- the tick ------------------------------------------------------------------------------

    def tick(self, only: Optional[tuple] = None) -> dict:
        state_mod = _mod("flows.state")
        started = self.now()
        try:
            with self.store.tick_lock():
                report = self._tick(only)
        except state_mod.Busy:
            return {"ok": False, "busy": True, "message": "Another flows tick is running; nothing was done twice."}
        report.update(ok=not report.get("errors"), source=self.source, seconds=round(self.now() - started, 2),
                      writes=len(self.writes), done=[write_line(w) for w in self.writes],
                      dry_run=self.settings.dry_run)
        if only is None:  # the loops read this stamp to decide when the next full tick is due
            try:
                self.store.write_engine({"at": int(self.now()), "source": self.source, "ok": report["ok"],
                                         "prs": len(report["prs"]), "writes": len(self.writes),
                                         "errors": report.get("errors", [])[:5], "notes": report.get("notes", [])[:10]})
            except OSError:
                pass
        return report

    def _tick(self, only: Optional[tuple]) -> dict:
        self.errors = []
        report: dict = {"at": int(self.now()), "flows": [], "prs": [], "notes": [], "errors": self.errors}
        try:
            self._advance_all(only, report)
        except RateLimited:
            report["errors"].append("Forgejo is rate limiting Hermes. This tick stopped here; the next one goes on.")
        return report

    def _advance_all(self, only: Optional[tuple], report: dict) -> None:
        state_mod = _mod("flows.state")
        try:
            reader = self.access.reader()
            plan = _mod("flows.sources").plan(reader, self.settings, self.store, only_repo=only[0] if only else "")
        except Exception as e:
            logger.warning("forgebruecke flows: the tick could not start", exc_info=True)
            report["errors"].append(f"The tick could not read which flows run where ({getattr(e, 'message', e)}). "
                                    "The next tick tries again.")
            return
        report.update(flows=[f"{flow.id}@{repo}" for repo, flow in plan.active], notes=list(plan.notes))
        listed: dict = {}  # (repo, flow id) -> (flow, numbers of its open pull requests), where the list was read
        for repo, flow in plan.active:
            try:
                page = reader.paginate(f"/repos/{repo}/pulls", {"state": "open"}, max_items=MAX_OPEN_PRS)
            except Exception as e:
                report["errors"].append(f"{repo}: the open pull requests could not be read ({getattr(e, 'message', e)})")
                continue
            if page.has_more:
                report["notes"].append(f"{repo}: {page.total or f'more than {MAX_OPEN_PRS}'} pull requests are open. "
                                       f"The flow runs on the first {MAX_OPEN_PRS} only.")
            listed[(repo, flow.id)] = (flow, {int(pr.get("number") or 0) for pr in page.items})
            promote_targets = {g.to for g in flow.gates if g.type == "promote"}
            for pr in page.items:
                n = int(pr.get("number") or 0)
                if only and n != int(only[1]):
                    continue
                try:
                    if (pr.get("base") or {}).get("ref") in promote_targets:
                        stored = self.store.load_pr(repo, n)  # gates an older version started on it
                        if stored is not None:
                            self._retire(stored, "this pull request promotes another one, so it runs no gates.")
                        continue
                    report["prs"].append(self.advance(repo, flow, pr))
                except state_mod.Unreadable as e:
                    report["errors"].append(f"{repo}#{n}: {e}")
                except Exception as e:
                    logger.warning("forgebruecke flows: %s#%s failed", repo, n, exc_info=True)
                    report["errors"].append(f"{repo}#{n}: {type(e).__name__}: {getattr(e, 'message', e)}")
        self._visit_stored(plan, listed, only, report)
        if only and not report["prs"]:
            report["notes"].append(f"{only[0]}#{only[1]} is not an open pull request under an active flow")
        if not only:
            self._request_reviews(report)

    def _visit_stored(self, plan, listed: dict, only: Optional[tuple], report: dict) -> None:
        """One pass over the stored states the loop over open pull requests did not reach. A pull request that
        left its repo's open list is closed out. A paused flow's running reviews finish. State whose flow no
        longer runs on its repo (rule changed, file invalid or gone) ends its running gates here: no tick
        advances it, and a running review would keep its slot."""
        active = {(repo, flow.id) for repo, flow in plan.active}
        paused = {(repo, flow.id): flow for repo, flow in plan.paused}
        for st in self.store.all_prs():
            key, n = (st.get("repo"), st.get("flow")), int(st.get("pr") or 0)
            if st.get("closed") or (only and n != int(only[1])):
                continue
            try:
                if key in listed:
                    flow, open_numbers = listed[key]
                    if n not in open_numbers:
                        report["prs"].append(self.close_out(st, flow))
                elif key in paused and not only:
                    changed = self._finish_reviews(st, paused[key])
                    if changed is not None:
                        report["prs"].append(self.summary(changed))
                elif key not in active and not only and key[0] not in plan.unread:
                    if self._retire(st, f"flow {key[1]} is not active here, so this gate stopped. "
                                        "Fix the flow or its rule, then push."):
                        report["prs"].append(self.summary(st))
            except Exception as e:
                report["errors"].append(f"{key[0]}#{n}: {type(e).__name__}: {getattr(e, 'message', e)}")

    def _request_reviews(self, report: dict) -> None:
        writer = FlowWriter(self.access, "review", dry_run=self.settings.dry_run, log=self.writes)
        sweep = _mod("review_requests").sweep
        # A dry run must not remember a request it only recorded, so it hands the sweep no folder to remember in.
        memory = {"data_dir": None if self.settings.dry_run else self.store.root} if _accepts(sweep, "data_dir") else {}
        try:
            swept = sweep(self.access, writer.request_review, **memory)
        except Exception as e:  # the flow rows of this tick still count
            report["errors"].append(f"review requests: {type(e).__name__}: {getattr(e, 'message', e)}")
            return
        report["review_requests"] = swept["requested"]
        report["notes"] += swept["notes"]
        report["errors"] += swept["errors"]

    # --- one pull request ----------------------------------------------------------------------

    def _fresh_gates(self, flow, repo: str, n: int, head: str) -> list:
        GateRun = self.models.GateRun
        return [GateRun(self.models.run_id(repo, n, head, g.key), flow.id, repo, n, head, i, g.key, g.type).to_dict()
                for i, g in enumerate(flow.gates)]

    @staticmethod
    def _only_recorded(gate: dict) -> bool:
        """True for a stored run that passed in a dry run: it says so, or it is a notify without a comment."""
        no_comment = gate.get("type") == "notify" and not (gate.get("evidence") or {}).get("comment_id")
        return str(gate.get("detail") or "").startswith("dry run:") or (gate.get("state") == "passed" and no_comment)

    def _rerun_from(self, old: dict, flow) -> Optional[int]:
        """The first gate of a stored state that must run again under the flow as it is now: 0 for every
        gate, None for none. Another ``version`` or another list of gates is every gate. An edited gate is
        that gate and the ones after it, which ran on what it let through; the passes before it stay."""
        keys = [gate.get("gate_key") for gate in old["gates"]]
        if old.get("flow_version") != flow.version or keys != [gate.key for gate in flow.gates]:
            return 0
        if "gate_hashes" not in old:
            # Stored before the hashes existed, when version and gate keys were the test. That release saved dry
            # runs too, so a gate whose write was only recorded runs again.
            return next((i for i, gate in enumerate(old["gates"]) if self._only_recorded(gate)), None)
        return next((i for i, now in enumerate(flow.gate_hashes()) if old["gate_hashes"][i:i + 1] != [now]), None)

    def _state(self, repo: str, n: int, flow, head: str) -> dict:
        """The stored state with every gate that must run again reset, or a fresh state. A running gate ends
        before its run is replaced, so a review worker never outlives the state that knows its pid."""
        old = self.store.load_pr(repo, n, strict=True)
        same_flow = old is not None and old.get("flow") == flow.id
        first = self._rerun_from(old, flow) if same_flow else 0
        reason = "this gate starts over, so the run it had was stopped."
        made_under = {"flow_version": flow.version, "gate_hashes": flow.gate_hashes()}
        if old is not None and first != 0:
            if "gate_hashes" not in old:
                old.pop("status_comment_hash", None)  # a dry run stored the hash of a comment it never sent
                if not old.get("status_comment_id"):
                    old["notified"] = []  # no comment was ever sent, so no failure notify was either
            if first is not None:
                self._retire(old, reason, from_gate=first)
                old["gates"][first:] = self._fresh_gates(flow, repo, n, old["head_sha"])[first:]
            old.update(made_under)
            return old
        kept = {"status_comment_id": None, "rounds": {}}
        if old is not None:
            self._retire(old, reason)
            kept = {key: old.get(key) or kept[key] for key in kept}
            if same_flow and old.get("flow_version") != flow.version:
                kept["rounds"] = {}  # a new version is how a person starts a pull request over
        return {"repo": repo, "pr": n, "flow": flow.id, **made_under, "head_sha": head, **kept,
                "notified": [], "history": [], "gates": self._fresh_gates(flow, repo, n, head)}

    def advance(self, repo: str, flow, pr: dict) -> dict:
        n = int(pr["number"])
        head = str((pr.get("head") or {}).get("sha") or "")
        st = self._state(repo, n, flow, head)
        st.update(title=pr.get("title"), url=pr.get("html_url"), author=(pr.get("user") or {}).get("login"),
                  base=(pr.get("base") or {}).get("ref"), head_ref=(pr.get("head") or {}).get("ref"),
                  closed=False, merged=False)  # it is in the open list, so a stored "closed" is a reopen
        if st["head_sha"] != head:
            self.on_new_head(st, flow, head)
            self._save(st)
        statuses = self._statuses(repo, head)
        runs = [self.models.GateRun.from_dict(g) for g in st["gates"]]
        for gate, run in zip(flow.gates, runs):
            if run.state in self.models.DONE_STATES:
                continue
            if run.state in STOPPED_STATES:
                live_check = gate.type == "check" and run.state == "failed" and not run.evidence.get("timed_out")
                if not live_check:
                    break  # an earlier gate failed: nothing later starts (rule 2)
            getattr(self, f"_gate_{gate.type}")(st, flow, gate, run, statuses)
            if run.state not in self.models.DONE_STATES:
                break
        self._save(st, *runs)
        self._publish(st, flow, runs, statuses)
        return self.summary(st)

    def _publish(self, st: dict, flow, runs: list, statuses: dict) -> None:
        """Tell the forge what the saved state says: statuses, the failure notify, the status comment."""
        self._try(st, "a check", lambda: self._sync_statuses(st, flow.id, runs, statuses))
        self._try(st, "the failure notify", lambda: self._notify_failures(st, flow, runs))
        self._try(st, "the status comment", lambda: self._status_comment(st, flow, runs))

    def _finish_reviews(self, stored: dict, flow) -> Optional[dict]:
        """What a tick does for a pull request of a paused flow: no gate starts, and a review that already
        runs ends as it would without the pause, with its verdict, or in ``error`` when its worker is gone or
        out of time. A run whose gate was edited since ends like any run of a changed flow. Returns the
        state when it changed."""
        st = self._state(stored["repo"], int(stored["pr"]), flow, stored["head_sha"])
        runs = [self.models.GateRun.from_dict(g) for g in st["gates"]]
        for gate, run in zip(flow.gates, runs):
            if gate.type == "review" and run.state == "running":
                self._gate_review(st, flow, gate, run, {})
        if [run.to_dict() for run in runs] == [self.models.GateRun.from_dict(g).to_dict() for g in stored["gates"]]:
            return None
        self._save(st, *runs)
        self._publish(st, flow, runs, {})
        return st

    def _save(self, st: dict, *runs) -> None:
        """Write the pull request's state now, so a tick that stops after this line repeats nothing before it.
        A dry run keeps the state in memory only: going live then starts from what is really on disk."""
        for run in runs:
            st["gates"][run.gate_index] = run.to_dict()
        if not self.settings.dry_run:
            self.store.save_pr(st)

    def _worker(self, ask: str, run):
        """``runner.alive`` or ``runner.stop`` for a review run's worker. A runner that takes ``log_path``
        gets the run's log, beside which it keeps what tells this worker from another process with its pid."""
        call, pid, run_file = getattr(self.runner, ask), run.evidence.get("pid"), run.evidence.get("run_file")
        if run_file and _accepts(call, "log_path"):
            return call(pid, log_path=Path(run_file).with_suffix(".log"))
        return call(pid)

    def _stop_review(self, run) -> None:
        if not self.settings.dry_run:
            self._worker("stop", run)

    def _retire(self, st: dict, reason: str, *, from_gate: int = 0) -> bool:
        """End the running gates of a state that will not be advanced as it is, from gate ``from_gate`` on.
        A review whose verdict is already written keeps it; every other running gate ends in ``error`` with
        the reason. True when a gate ended."""
        running = [r for r in (self.models.GateRun.from_dict(g) for g in st["gates"])
                   if r.state == "running" and r.gate_index >= from_gate]
        for run in running:
            if run.type == "review":
                if self._read_verdict(st, run):
                    continue
                self._stop_review(run)
            self._end(run, "error", reason)
        if running:
            self._save(st, *running)
            self._try(st, "a check", lambda: self._sync_statuses(st, st["flow"], running, {}))
        return bool(running)

    def _try(self, st: dict, what: str, write: Callable[[], None]) -> None:
        """One forge write that the gates do not depend on. It is sent after the state is saved; a failure
        lands in the report and the next tick sends it again."""
        try:
            write()
        except Exception as e:
            logger.warning("forgebruecke flows: %s#%s: %s failed", st["repo"], st["pr"], what, exc_info=True)
            self.errors.append(f"{st['repo']}#{st['pr']}: {what} could not be written ({getattr(e, 'message', e)}). "
                               "The next tick tries again.")

    @staticmethod
    def summary(st: dict) -> dict:
        return {"repo": st["repo"], "pr": st["pr"], "flow": st["flow"], "head": str(st.get("head_sha") or "")[:12],
                "closed": st.get("closed", False), "merged": st.get("merged", False),
                "gates": [{"key": g["gate_key"], "state": g["state"], "detail": g.get("detail", "")} for g in st["gates"]]}

    def _statuses(self, repo: str, sha: str) -> dict:
        data = self.access.reader().get(f"/repos/{repo}/commits/{sha}/status").data or {}
        out = {}
        for s in data.get("statuses") or []:
            ctx = s.get("context")
            if ctx and ctx not in out:  # the combined status lists the latest per context
                out[ctx] = {"state": s.get("status") or s.get("state"), "description": s.get("description") or "",
                            "creator": (s.get("creator") or {}).get("login")}
        return out

    def _writer(self, flow) -> FlowWriter:
        return FlowWriter(self.access, flow.id, dry_run=self.settings.dry_run, log=self.writes)

    def on_new_head(self, st: dict, flow, head: str) -> None:
        old = st["head_sha"]
        runs = [self.models.GateRun.from_dict(g) for g in st["gates"]]
        stopped = next((i for i, r in enumerate(runs) if r.state in STOPPED_STATES), None)
        head_bound = next((i for i, g in enumerate(flow.gates) if g.type in HEAD_BOUND_GATES), len(runs))
        first = 0 if stopped is None or flow.on_push == "reset_all" else min(stopped, head_bound)
        w = self._writer(flow)
        reset, carried = [], []
        for i, (gate, run) in enumerate(zip(flow.gates, runs)):
            if i < first:
                run.carried_from = run.carried_from or old
                run.head_sha = head
                run.id = self.models.run_id(st["repo"], st["pr"], head, gate.key)
                carried.append(gate.key)
                continue
            if run.state == "running":
                if gate.type == "review":
                    self._stop_review(run)
                # Sent before the new head is saved: if it fails, the next tick finds the old head and sends it again.
                w.status(st["repo"], old, gate.key, "error", f"superseded by a push ({head[:12]})", st.get("url") or "")
            fresh = self.models.GateRun(self.models.run_id(st["repo"], st["pr"], head, gate.key), flow.id, st["repo"],
                                        st["pr"], head, i, gate.key, gate.type)
            fresh.attempts = run.attempts
            runs[i] = fresh
            reset.append(gate.key)
        st["gates"] = [r.to_dict() for r in runs]
        st["history"] = (st.get("history") or [])[-19:] + [{"from": old[:12], "to": head[:12], "at": int(self.now()),
                                                             "reset": reset, "carried": carried}]
        st["head_sha"] = head

    @staticmethod
    def _forge_status(run) -> Optional[tuple]:
        """``(state, description)`` the forge should show for a run, or None for a run that posts nothing."""
        if run.type in SILENT_GATES or run.state not in FORGE_STATE:
            return None
        carried = run.state == "passed" and run.carried_from
        detail = f"passed at {run.carried_from[:12]}, carried over after a push" if carried else run.detail
        return FORGE_STATE[run.state], _desc(detail)

    def _sync_statuses(self, st: dict, flow_id: str, runs: list, statuses: dict) -> None:
        """Post every status the forge does not show yet. Each run keeps what was sent for it, so a tick that
        failed here sends the rest later and a tick with nothing new sends nothing. A run without that record
        (stored by an older version, or the answer to its post was lost) trusts the state the forge shows.
        A ``pending`` status that no running gate owns ends in ``error``, or in the gate's own state."""
        w = FlowWriter(self.access, flow_id, dry_run=self.settings.dry_run, log=self.writes)
        repo, url, prefix = st["repo"], st.get("url") or "", f"forgebruecke/{flow_id}/"
        pending = {c[len(prefix):] for c, s in statuses.items() if c.startswith(prefix) and s.get("state") == "pending"}
        for run in runs:
            want = self._forge_status(run)
            if want is None:
                continue
            stale = run.gate_key in pending and run.state != "running"
            pending.discard(run.gate_key)
            sent = " ".join((run.head_sha[:12], *want))
            if run.posted == sent and not stale:
                continue
            if run.posted or (statuses.get(prefix + run.gate_key) or {}).get("state") != want[0]:
                w.status(repo, run.head_sha, run.gate_key, *want, url)
            run.posted = sent
            self._save(st, run)
        for key in sorted(pending):
            w.status(repo, st["head_sha"], key, "error", "a pending check no run owns; repaired by Forgebrücke", url)

    # --- gates ---------------------------------------------------------------------------------

    def _start(self, run, detail: str) -> None:
        run.state, run.started_at, run.detail = "running", int(self.now()), detail

    def _end(self, run, state: str, detail: str) -> None:
        run.state, run.detail, run.ended_at = state, detail, int(self.now())

    def _gate_review(self, st: dict, flow, gate, run, statuses: dict) -> None:
        runner_mod = _mod("flows.runner")
        if run.state == "pending":
            rounds = int((st.get("rounds") or {}).get(gate.key, 0))
            if rounds >= gate.max_fix_rounds:
                self._end(run, "blocked", f"review rounds used up ({rounds} of {gate.max_fix_rounds}). "
                                          "To start over, raise version in the flow file.")
                return
            if self.store.running_reviews() >= MAX_PARALLEL_REVIEWS:
                run.detail = f"queued: {MAX_PARALLEL_REVIEWS} reviews are already running"
                return
            diff_resp = self.access.reader().get(f"/repos/{st['repo']}/pulls/{st['pr']}.diff", raw=True)
            diff = diff_resp.data.decode("utf-8", "replace") if isinstance(diff_resp.data, bytes) else str(diff_resp.data)
            too_large = getattr(runner_mod, "diff_too_large", None)
            if too_large is not None and too_large(diff):
                self._end(run, "blocked", f"The diff is too large to review ({len(diff)} characters). "
                                          "Review it yourself or split the pull request.")
                return
            if self.settings.dry_run:
                self.writes.append({"write": "review_start", "dry_run": True, "repo": st["repo"], "number": st["pr"]})
                self._start(run, f"dry run: would start a review by {gate.by}")
                return
            verdict = self.store.verdict_path(flow.id, st["repo"], st["pr"], st["head_sha"], gate.key)
            log_path = verdict.with_suffix(".log")
            # A tick that died between a worker's start and the save below left a worker no state knows. The
            # runner's record beside the log does, so that worker is taken over and no second one starts.
            recorded = getattr(self.runner, "recorded_pid", None)
            pid = recorded(log_path) if recorded is not None else None
            if pid is None:
                pr = self.access.reader().get(f"/repos/{st['repo']}/pulls/{st['pr']}").data or {}
                verdict.parent.mkdir(parents=True, exist_ok=True)
                if verdict.exists():
                    verdict.unlink()
                prompt = verdict.with_suffix(".prompt.md")
                prompt.write_text(runner_mod.build_prompt(
                    flow_id=flow.id, gate_key=gate.key, lens=gate.lens, repo=st["repo"], pr=st["pr"],
                    title=str(pr.get("title") or ""), body=str(pr.get("body") or ""), head_sha=st["head_sha"],
                    diff=diff, verdict_path=verdict, fix_round=rounds + 1, max_rounds=gate.max_fix_rounds),
                    encoding="utf-8")
                pid = self.runner.start(profile=gate.by, prompt_path=prompt, log_path=log_path)
            run.attempts += 1
            run.evidence.update(pid=pid, run_file=str(verdict), profile=gate.by)
            self._start(run, f"review by {gate.by} running (round {rounds + 1} of {gate.max_fix_rounds})")
            self._save(st, run)
            return
        if run.state != "running" or self._read_verdict(st, run):
            return
        if not self._worker("alive", run):
            self._end(run, "error", f"the review run by {gate.by} ended without writing a verdict")
        elif self.now() - (run.started_at or self.now()) > gate.timeout_min * 60:
            self._stop_review(run)
            self._end(run, "error", f"the review by {gate.by} timed out after {gate.timeout_min} min")

    def _read_verdict(self, st: dict, run) -> bool:
        """Apply the worker's verdict file to its review run. False while the worker has not written it.
        A verdict that asks for a fix is one round of ``max_fix_rounds``; a push alone is none."""
        by = run.evidence.get("profile")
        state, summary, findings, problem = _mod("flows.runner").read_verdict(
            Path(run.evidence.get("run_file") or ""), run.head_sha)
        if not state:
            return False
        run.evidence.update(summary=summary, findings=findings)
        if state == "error":
            self._end(run, "error", f"review by {by}: {problem}")
        else:
            run.evidence["verdict"] = "pass" if state == "passed" else "fail"
            self._end(run, state, f"review by {by}: {summary or state}")
        if state == "failed":
            rounds = st.setdefault("rounds", {})
            rounds[run.gate_key] = int(rounds.get(run.gate_key, 0)) + 1
        return True

    def _match(self, gate_contexts: tuple, statuses: dict, own_prefix: str) -> "tuple[list, list, list, dict]":
        ok, bad, missing, matched = [], [], [], {}
        for pattern in gate_contexts:
            hits = {c: s for c, s in statuses.items() if fnmatch.fnmatchcase(c, pattern) and not c.startswith(own_prefix)}
            matched.update(hits)
            if not hits:
                missing.append(pattern)
            elif any(s.get("state") in ("failure", "error") for s in hits.values()):
                bad.extend(c for c, s in hits.items() if s.get("state") in ("failure", "error"))
            elif all(s.get("state") == "success" for s in hits.values()):
                ok.append(pattern)
            else:
                missing.append(pattern)
        return ok, bad, missing, matched

    def _gate_check(self, st: dict, flow, gate, run, statuses: dict) -> None:
        own = f"forgebruecke/{flow.id}/"
        ok, bad, missing, matched = self._match(gate.contexts, statuses, own)
        run.evidence["contexts"] = {c: {"state": s.get("state"), "creator": s.get("creator")} for c, s in matched.items()}
        if gate.creators:
            run.evidence["flags"] = [f"{c} was posted by {s.get('creator')}, not by {', '.join(gate.creators)}"
                                     for c, s in matched.items() if s.get("creator") not in gate.creators]
        if run.state == "pending":
            self._start(run, "waiting for " + ", ".join(gate.contexts))
        if bad:
            detail = "failed: " + ", ".join(f"{c} ({matched[c].get('description') or matched[c].get('state')})" for c in bad)
            if run.state != "failed" or run.detail != detail:
                self._end(run, "failed", detail)
            return
        if not missing:
            self._end(run, "passed", "passed: " + ", ".join(sorted(matched)))
            return
        if self.now() - (run.started_at or self.now()) > gate.timeout_min * 60:
            run.evidence["timed_out"] = True
            self._end(run, "failed", f"timed out after {gate.timeout_min} min waiting for {', '.join(missing)}")
            return
        run.state = "running"
        run.detail = "waiting for " + ", ".join(missing)

    def _gate_human(self, st: dict, flow, gate, run, statuses: dict) -> None:
        if run.state != "waiting":
            run.state, run.started_at = "waiting", int(self.now())
        run.detail = f"waiting for {gate.who or 'you'} to merge in Forgejo"

    def _gate_notify(self, st: dict, flow, gate, run, statuses: dict) -> None:
        text = gate.message or f"pull request #{st['pr']} passed every gate before this one."
        run.evidence["comment_id"] = self._writer(flow).comment(st["repo"], st["pr"], f"@{gate.who} {text}" + FOOTER)
        self._end(run, "passed", f"notified {gate.who} with a comment")
        self._save(st, run)

    def _promote_refusal(self, repo: str, gate, promo: Optional[dict] = None) -> str:
        """Why the flow may not merge into ``gate.to``, read from the forge now; empty when it may. ``promo`` is
        the promotion pull request an earlier tick opened: it must still target ``gate.to``."""
        if promo is not None and (promo.get("base") or {}).get("ref") != gate.to:
            return f"pull request #{promo.get('number')} no longer targets {gate.to}, so it was not merged"
        reader = self.access.reader()
        info = reader.get(f"/repos/{repo}").data or {}
        if gate.to == info.get("default_branch") or gate.to.lower() in self.models.PROTECTED_NAMES:
            return f"{gate.to} is the default branch; a flow never merges there"
        try:
            branch = reader.get(f"/repos/{repo}/branches/{gate.to}").data or {}
        except _mod("client").ForgejoError as e:
            if e.status == 404:
                return f"the branch {gate.to} does not exist; create it first"
            raise
        if branch.get("protected"):
            return f"{gate.to} is protected; a flow only merges into unprotected branches"
        return ""

    def _gate_promote(self, st: dict, flow, gate, run, statuses: dict) -> None:
        """Open the promotion pull request, merge it, read the branch. Each step leaves its evidence on disk
        before the next one starts, and a later tick goes on at the first step without evidence."""
        ForgejoError = _mod("client").ForgejoError
        repo, reader, done = st["repo"], self.access.reader(), run.evidence
        if run.state == "pending":
            refusal = self._promote_refusal(repo, gate)
            if refusal:
                self._end(run, "failed", refusal)
                return
            self._start(run, f"promoting to {gate.to}")
        if run.state != "running":
            return
        if "promoted_sha" not in done:
            if self.settings.dry_run:
                self.writes.append({"write": "promote_merge", "dry_run": True, "repo": repo, "to": gate.to})
                self._end(run, "passed", f"dry run: would merge {st.get('head_ref')} into {gate.to}")
                return
            writer = self.access.writer()
            resumed = "promote_pr" in done
            if not resumed:
                open_prs = reader.paginate(f"/repos/{repo}/pulls", {"state": "open"}, max_items=100).items
                promo = next((p for p in open_prs if (p.get("base") or {}).get("ref") == gate.to
                              and (p.get("head") or {}).get("ref") == st.get("head_ref")), None)
                if promo is None:
                    try:
                        promo = writer.post(f"/repos/{repo}/pulls", {
                            "head": st.get("head_ref"), "base": gate.to, "title": f"Promote #{st['pr']} to {gate.to}",
                            "body": f"Opened by the Forgebrücke flow `{flow.id}` for #{st['pr']} at {st['head_sha'][:12]}." + FOOTER}).data or {}
                    except ForgejoError as e:
                        self._end(run, "failed", f"the pull request into {gate.to} could not be opened: {e.message}")
                        return
                    self.writes.append({"write": "promote_pr", "repo": repo, "number": promo.get("number")})
                done["promote_pr"] = promo.get("number")
                self._save(st, run)
            if not done.get("merged"):
                # A tick that stopped during the merge call left no answer, so the forge says whether it landed.
                promo = (reader.get(f"/repos/{repo}/pulls/{done['promote_pr']}").data or {}) if resumed else {}
                if not promo.get("merged"):
                    # The target was last read by the tick that opened the pull request, and may have changed since.
                    refusal = self._promote_refusal(repo, gate, promo) if resumed else ""
                    if refusal:
                        self._end(run, "failed", refusal)
                        return
                    try:
                        writer.post(f"/repos/{repo}/pulls/{done['promote_pr']}/merge",
                                    {"Do": "merge", "head_commit_id": st["head_sha"],
                                     "merge_message_field": f"Promote #{st['pr']} ({st['head_sha'][:12]}) to {gate.to}"})
                    except ForgejoError as e:
                        self._end(run, "failed", f"the merge into {gate.to} was refused: {e.message}")
                        return
                    self.writes.append({"write": "promote_merge", "repo": repo, "number": done["promote_pr"], "to": gate.to})
                done["merged"] = True
                self._save(st, run)
            promoted = (reader.get(f"/repos/{repo}/branches/{gate.to}").data or {}).get("commit", {}).get("id") or ""
            done["promoted_sha"] = promoted
            if gate.then != "verify_deploy":
                self._end(run, "passed", f"merged into {gate.to} at {promoted[:12]}")
                return
            run.detail = f"merged into {gate.to} at {promoted[:12]}; waiting for {', '.join(gate.contexts)}"
            return
        ok, bad, missing, matched = self._match(gate.contexts, self._statuses(repo, done["promoted_sha"]),
                                                f"forgebruecke/{flow.id}/")
        if bad:
            self._end(run, "failed", f"the deploy check failed on {gate.to}: {', '.join(bad)}")
        elif not missing:
            self._end(run, "passed", f"deployed from {gate.to}: {', '.join(sorted(matched))}")
        elif self.now() - (run.started_at or self.now()) > gate.timeout_min * 60:
            self._end(run, "failed", f"no deploy check on {gate.to} after {gate.timeout_min} min")

    # --- comment, notify, close ----------------------------------------------------------------

    def render_comment(self, st: dict, flow, runs: list) -> str:
        lines = [f"**Forgebrücke · flow `{flow.id}` · head `{st['head_sha'][:12]}`**", "",
                 "| Gate | State | What happened |", "| --- | --- | --- |"]
        for gate, run in zip(flow.gates, runs):
            lines.append(f"| {gate.label()} | {ICONS.get(run.state, '')} {run.state} | {run.detail} |")
        review = next((r for g, r in zip(flow.gates, runs) if g.type == "review" and r.evidence.get("findings")), None)
        if review is not None:
            lines += ["", "**Review findings**"] + [
                f"- `{f.get('file')}`{(':' + str(f['line'])) if f.get('line') else ''}: {f.get('problem')}"
                for f in review.evidence["findings"]]
        flags = [flag for run in runs for flag in run.evidence.get("flags") or []]
        if flags:
            lines += ["", "**Posted by an unexpected account**"] + [f"- {flag}" for flag in flags]
        nxt = next(((g, r) for g, r in zip(flow.gates, runs) if r.state not in self.models.DONE_STATES), None)
        if nxt is None:
            lines += ["", "Every gate passed."]
        elif nxt[1].state == "waiting":
            lines += ["", f"**What happens next:** {nxt[0].who or 'you'} merges in Forgejo. Nothing merges on its own."]
        elif nxt[1].state in ("failed", "error"):  # a blocked gate says in its own row what a person can do
            lines += ["", f"**What happens next:** fix the cause and push; the {nxt[0].key} gate runs again on the new head."]
        return "\n".join(lines) + FOOTER

    def _status_comment(self, st: dict, flow, runs: list) -> None:
        body = self.render_comment(st, flow, runs)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        if digest == st.get("status_comment_hash"):
            return
        w = self._writer(flow)
        cid = st.get("status_comment_id")
        if not (cid and w.edit_comment(st["repo"], cid, body)):
            st["status_comment_id"] = w.comment(st["repo"], st["pr"], body)
        st["status_comment_hash"] = digest
        self._save(st)

    def _notify_failures(self, st: dict, flow, runs: list) -> None:
        if flow.on_failure != "notify":
            return
        human = flow.human()
        who = (human.who if human and human.who else st["repo"].split("/")[0])
        notified = st.setdefault("notified", [])
        for gate, run in zip(flow.gates, runs):
            if run.state in STOPPED_STATES and run.id not in notified:
                word = {"failed": "failed", "error": "stopped with an error", "blocked": "is blocked"}[run.state]
                text = (f"@{who} the {gate.key} gate {word} on `{run.head_sha[:12]}`. {run.detail.rstrip('.')}. "
                        "Nothing was merged. The status comment above shows every gate.")
                self._writer(flow).comment(st["repo"], st["pr"], text + FOOTER)
                notified.append(run.id)
                del notified[:-50]
                self._save(st)

    def close_out(self, st: dict, flow) -> dict:
        pr = self.access.reader().get(f"/repos/{st['repo']}/pulls/{st['pr']}").data or {}
        if pr.get("state") == "open":
            return self.summary(st)
        runs = [self.models.GateRun.from_dict(g) for g in st["gates"]]
        for gate, run in zip(flow.gates, runs):
            if run.state == "running" and gate.type == "review":
                self._stop_review(run)
                self._end(run, "error", "the pull request was closed while the review ran")
            if gate.type == "human" and pr.get("merged"):
                by = (pr.get("merged_by") or {}).get("login")
                run.state, run.ended_at = "passed", int(self.now())
                run.detail = f"merged by {by or 'a person'} at {str(pr.get('merge_commit_sha') or '')[:12]}"
                run.evidence.update(merged_by=by, merge_commit=pr.get("merge_commit_sha"))
        st.update(closed=True, merged=bool(pr.get("merged")))
        self._save(st, *runs)
        self._try(st, "a check", lambda: self._sync_statuses(st, flow.id, runs, {}))
        return self.summary(st)
