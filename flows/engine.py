"""The flows engine: on every tick, move each open pull request one step along its flow.

PRODUCT.md § Rules the engine enforces, as code:

1. One PR, one head commit, ordered gates. A push resets the gates from the first one that is not done
   (the failed one, or a running one) to the end; gates before it carry over and their success status is
   posted again on the new head, saying where it came from. ``on_push: reset_all`` resets everything.
2. Gate order is data: a later gate never starts before every earlier one passed.
3. The engine posts statuses only for its own gates (``forgebruecke/<flow>/<key>``) and reads the rest;
   a check gate can name the accounts allowed to post a context (``creators``), and others are flagged.
4. No gate merges into ``main``: the validator refuses ``to: main``, and before merging the engine
   re-reads the target branch and refuses a protected branch or the repo's default branch.
5. Two ticks never act at once (``FlowStore.tick_lock``); a review run is claimed with a lease, and a run
   whose process is gone, or a ``pending`` status no run owns, is repaired to ``error``.
6. Every gate the engine starts ends in ``success``, ``failure`` or ``error``, never ``pending`` for good,
   because the demo's protection requires ``forgebruecke/*`` and a stuck status blocks the merge.

Unattended by nature: the engine does not call the agent's write tools (those refuse unattended runs). It
writes through ``FlowWriter``, which can do exactly what a flow names for a gate: a status in the flow's
own namespace, the one status comment per PR (edited in place), a notify comment, and, in ``promote``
only, opening and merging a pull request into the unprotected branch the flow names. With ``dry_run`` on
it records those writes and sends nothing.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib
import logging
import time
from typing import Any, Callable, Optional

LEASE_S = 15 * 60
MAX_PARALLEL_REVIEWS = 2
FOOTER = "\n\n<sub>Kept up to date by Forgebrücke (Hermes). The merge into the target branch is yours.</sub>"
ICONS = {"passed": "✅", "failed": "❌", "error": "⚠️", "running": "⏳", "pending": "·", "waiting": "👤",
         "blocked": "⛔", "skipped": "↷"}

logger = logging.getLogger(__name__)


def _mod(name: str):
    pkg = (__package__ or "").rpartition(".")[0] or __package__
    return importlib.import_module(f".{name}", pkg)


def _desc(text: str) -> str:
    return " ".join(str(text or "").split())[:140]


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


class Engine:
    def __init__(self, access: Any, store: Any, *, runner: Any = None, now: Callable[[], float] = time.time,
                 source: str = "tick") -> None:
        self.access = access.for_lane("flow")  # the agent token only, never the user's tokens (M4b)
        self.settings = access.settings
        self.store = store
        self.runner = runner if runner is not None else _mod("flows.runner").ReviewRunner()
        self.now = now
        self.source = source
        self.writes: list = []
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
                      writes=len(self.writes), dry_run=self.settings.dry_run)
        try:
            self.store.write_engine({"at": int(self.now()), "source": self.source, "ok": report["ok"],
                                     "prs": len(report["prs"]), "writes": len(self.writes),
                                     "errors": report.get("errors", [])[:5], "notes": report.get("notes", [])[:10]})
        except OSError:
            pass
        return report

    def _tick(self, only: Optional[tuple]) -> dict:
        reader = self.access.reader()
        plan = _mod("flows.sources").plan(reader, self.settings, self.store, only_repo=only[0] if only else "")
        report: dict = {"at": int(self.now()), "flows": [f"{fid}@{repo}" for repo, fid in
                                                         ((r, f.id) for r, f in plan.active)],
                        "prs": [], "notes": list(plan.notes), "errors": []}
        for repo, flow in plan.active:
            try:
                prs = reader.paginate(f"/repos/{repo}/pulls", {"state": "open"}, max_items=100).items
            except Exception as e:
                report["errors"].append(f"{repo}: the open pull requests could not be read ({getattr(e, 'message', e)})")
                continue
            open_numbers = set()
            for pr in prs:
                n = int(pr.get("number") or 0)
                open_numbers.add(n)
                if only and n != int(only[1]):
                    continue
                try:
                    report["prs"].append(self.advance(repo, flow, pr))
                except Exception as e:
                    logger.warning("forgebruecke flows: %s#%s failed", repo, n, exc_info=True)
                    report["errors"].append(f"{repo}#{n}: {type(e).__name__}: {getattr(e, 'message', e)}")
            for st in self.store.all_prs():
                if (st.get("repo") == repo and st.get("flow") == flow.id and not st.get("closed")
                        and int(st.get("pr") or 0) not in open_numbers and (not only or int(only[1]) == st.get("pr"))):
                    try:
                        report["prs"].append(self.close_out(st, flow))
                    except Exception as e:
                        report["errors"].append(f"{repo}#{st.get('pr')}: {type(e).__name__}: {getattr(e, 'message', e)}")
        if only and not report["prs"]:
            report["notes"].append(f"{only[0]}#{only[1]} is not an open pull request under an active flow")
        return report

    # --- one pull request ----------------------------------------------------------------------

    def _fresh_gates(self, flow, repo: str, n: int, head: str) -> list:
        GateRun = self.models.GateRun
        return [GateRun(self.models.run_id(repo, n, head, g.key), flow.id, repo, n, head, i, g.key, g.type).to_dict()
                for i, g in enumerate(flow.gates)]

    def advance(self, repo: str, flow, pr: dict) -> dict:
        n = int(pr["number"])
        head = str((pr.get("head") or {}).get("sha") or "")
        st = self.store.load_pr(repo, n)
        signature = [g.key for g in flow.gates]
        if (st is None or st.get("flow") != flow.id or st.get("flow_version") != flow.version
                or [g.get("gate_key") for g in st.get("gates") or []] != signature):
            st = {"repo": repo, "pr": n, "flow": flow.id, "flow_version": flow.version, "head_sha": head,
                  "status_comment_id": (st or {}).get("status_comment_id"), "notified": [], "history": [],
                  "rounds": {}, "gates": self._fresh_gates(flow, repo, n, head), "closed": False, "merged": False}
        st.update(title=pr.get("title"), url=pr.get("html_url"), author=(pr.get("user") or {}).get("login"),
                  base=(pr.get("base") or {}).get("ref"), head_ref=(pr.get("head") or {}).get("ref"))
        if st["head_sha"] != head:
            self.on_new_head(st, flow, head)
        statuses = self._statuses(repo, head)
        self._repair_stale(st, flow, statuses)
        runs = [self.models.GateRun.from_dict(g) for g in st["gates"]]
        for gate, run in zip(flow.gates, runs):
            if run.state in self.models.DONE_STATES:
                continue
            if run.state in ("failed", "error", "blocked"):
                live_check = gate.type == "check" and run.state == "failed" and not run.evidence.get("timed_out")
                if not live_check:
                    break  # an earlier gate failed: nothing later starts (rule 2)
            getattr(self, f"_gate_{gate.type}")(st, flow, gate, run, statuses)
            if run.state not in self.models.DONE_STATES:
                break
        st["gates"] = [r.to_dict() for r in runs]
        self._notify_failures(st, flow, runs)
        self._status_comment(st, flow, runs)
        self.store.save_pr(st)
        return self.summary(st)

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
        first = next((i for i, r in enumerate(runs) if r.state not in self.models.DONE_STATES), len(runs))
        if flow.on_push == "reset_all":
            first = 0
        w = self._writer(flow)
        reset, carried = [], []
        for i, (gate, run) in enumerate(zip(flow.gates, runs)):
            if i < first:
                run.carried_from = run.carried_from or old
                run.head_sha = head
                run.id = self.models.run_id(st["repo"], st["pr"], head, gate.key)
                if gate.type != "human":
                    w.status(st["repo"], head, gate.key, "success", f"passed at {old[:12]}, carried over after a push",
                             st.get("url") or "")
                carried.append(gate.key)
                continue
            if run.state == "running":
                if gate.type == "review":
                    self.runner.stop(run.evidence.get("pid"))
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

    def _repair_stale(self, st: dict, flow, statuses: dict) -> None:
        prefix = f"forgebruecke/{flow.id}/"
        by_key = {g["gate_key"]: g for g in st["gates"]}
        w = self._writer(flow)
        for ctx, s in statuses.items():
            if not ctx.startswith(prefix) or s.get("state") != "pending":
                continue
            run = by_key.get(ctx[len(prefix):])
            if run is None or run.get("state") != "running":
                w.status(st["repo"], st["head_sha"], ctx[len(prefix):], "error",
                         "a pending status no run owns; repaired by Forgebrücke", st.get("url") or "")

    # --- gates ---------------------------------------------------------------------------------

    def _start(self, run, detail: str) -> None:
        run.state, run.started_at, run.detail = "running", int(self.now()), detail
        run.claim_until = int(self.now()) + LEASE_S

    def _end(self, st: dict, flow, gate, run, state: str, detail: str, *, post: bool = True) -> None:
        run.state, run.detail, run.ended_at, run.claim_until = state, detail, int(self.now()), None
        if post:
            forge_state = {"passed": "success", "failed": "failure", "blocked": "failure"}.get(state, "error")
            self._writer(flow).status(st["repo"], st["head_sha"], gate.key, forge_state, detail, st.get("url") or "")

    def _gate_review(self, st: dict, flow, gate, run, statuses: dict) -> None:
        runner_mod = _mod("flows.runner")
        if run.state == "pending":
            rounds = int((st.get("rounds") or {}).get(gate.key, 0))
            if rounds >= gate.max_fix_rounds:
                self._end(st, flow, gate, run, "blocked",
                          f"review rounds used up ({rounds} of {gate.max_fix_rounds}); a person decides what next")
                return
            if self.store.running_reviews() >= MAX_PARALLEL_REVIEWS:
                run.detail = f"queued: {MAX_PARALLEL_REVIEWS} reviews are already running"
                return
            diff_resp = self.access.reader().get(f"/repos/{st['repo']}/pulls/{st['pr']}.diff", raw=True)
            diff = diff_resp.data.decode("utf-8", "replace") if isinstance(diff_resp.data, bytes) else str(diff_resp.data)
            pr = self.access.reader().get(f"/repos/{st['repo']}/pulls/{st['pr']}").data or {}
            verdict = self.store.verdict_path(flow.id, st["repo"], st["pr"], st["head_sha"], gate.key)
            verdict.parent.mkdir(parents=True, exist_ok=True)
            if verdict.exists():
                verdict.unlink()
            prompt = verdict.with_suffix(".prompt.md")
            prompt.write_text(runner_mod.build_prompt(
                flow_id=flow.id, gate_key=gate.key, lens=gate.lens, repo=st["repo"], pr=st["pr"],
                title=str(pr.get("title") or ""), body=str(pr.get("body") or ""), head_sha=st["head_sha"], diff=diff,
                verdict_path=verdict, fix_round=rounds + 1, max_rounds=gate.max_fix_rounds), encoding="utf-8")
            pid = self.runner.start(profile=gate.by, prompt_path=prompt, log_path=verdict.with_suffix(".log"))
            st.setdefault("rounds", {})[gate.key] = rounds + 1
            run.attempts += 1
            run.evidence.update(pid=pid, run_file=str(verdict), profile=gate.by)
            self._start(run, f"review by {gate.by} running (round {rounds + 1} of {gate.max_fix_rounds})")
            self._writer(flow).status(st["repo"], st["head_sha"], gate.key, "pending", run.detail, st.get("url") or "")
            return
        if run.state != "running":
            return
        from pathlib import Path
        state, summary, findings, problem = runner_mod.read_verdict(Path(run.evidence.get("run_file") or ""),
                                                                   st["head_sha"])
        if state in ("passed", "failed"):
            run.evidence.update(verdict="pass" if state == "passed" else "fail", summary=summary, findings=findings)
            self._end(st, flow, gate, run, state, f"review by {gate.by}: {summary or state}")
        elif state == "error":
            run.evidence.update(summary=summary, findings=findings)
            self._end(st, flow, gate, run, "error", f"review by {gate.by}: {problem}")
        elif not self.runner.alive(run.evidence.get("pid")):
            self._end(st, flow, gate, run, "error", f"the review run by {gate.by} ended without writing a verdict")
        elif self.now() - (run.started_at or self.now()) > gate.timeout_min * 60:
            self.runner.stop(run.evidence.get("pid"))
            self._end(st, flow, gate, run, "error", f"the review by {gate.by} timed out after {gate.timeout_min} min")
        else:
            run.claim_until = int(self.now()) + LEASE_S  # heartbeat: the worker is alive

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
            self._writer(flow).status(st["repo"], st["head_sha"], gate.key, "pending", run.detail, st.get("url") or "")
        if bad:
            detail = "failed: " + ", ".join(f"{c} ({matched[c].get('description') or matched[c].get('state')})" for c in bad)
            if run.state != "failed" or run.detail != detail:
                self._end(st, flow, gate, run, "failed", detail)
            return
        if not missing:
            self._end(st, flow, gate, run, "passed", "passed: " + ", ".join(sorted(matched)))
            return
        if self.now() - (run.started_at or self.now()) > gate.timeout_min * 60:
            run.evidence["timed_out"] = True
            self._end(st, flow, gate, run, "failed", f"timed out after {gate.timeout_min} min waiting for {', '.join(missing)}")
            return
        run.state = "running"
        run.detail = "waiting for " + ", ".join(missing)
        run.claim_until = int(self.now()) + LEASE_S

    def _gate_human(self, st: dict, flow, gate, run, statuses: dict) -> None:
        if run.state != "waiting":
            run.state, run.started_at = "waiting", int(self.now())
        run.detail = f"waiting for {gate.who or 'you'} to merge in Forgejo"

    def _gate_notify(self, st: dict, flow, gate, run, statuses: dict) -> None:
        text = gate.message or f"pull request #{st['pr']} passed every gate before this one."
        cid = self._writer(flow).comment(st["repo"], st["pr"], f"@{gate.who} {text}" + FOOTER)
        run.evidence["comment_id"] = cid
        self._end(st, flow, gate, run, "passed", f"notified {gate.who} with a comment", post=False)

    def _gate_promote(self, st: dict, flow, gate, run, statuses: dict) -> None:
        ForgejoError = _mod("client").ForgejoError
        repo, reader = st["repo"], self.access.reader()
        if run.state == "pending":
            info = reader.get(f"/repos/{repo}").data or {}
            if gate.to == info.get("default_branch") or gate.to.lower() in self.models.PROTECTED_NAMES:
                self._end(st, flow, gate, run, "failed", f"{gate.to} is the default branch; a flow never merges there")
                return
            try:
                branch = reader.get(f"/repos/{repo}/branches/{gate.to}").data or {}
            except ForgejoError as e:
                if e.status == 404:
                    self._end(st, flow, gate, run, "failed", f"the branch {gate.to} does not exist; create it first")
                    return
                raise
            if branch.get("protected"):
                self._end(st, flow, gate, run, "failed", f"{gate.to} is protected; a flow only merges into unprotected branches")
                return
            writer = self.access.writer()
            self._start(run, f"promoting to {gate.to}")
            self._writer(flow).status(repo, st["head_sha"], gate.key, "pending", run.detail, st.get("url") or "")
            if self.settings.dry_run:
                self.writes.append({"write": "promote_merge", "dry_run": True, "repo": repo, "to": gate.to})
                self._end(st, flow, gate, run, "passed", f"dry run: would merge {st.get('head_ref')} into {gate.to}")
                return
            open_prs = reader.paginate(f"/repos/{repo}/pulls", {"state": "open"}, max_items=100).items
            promo = next((p for p in open_prs if (p.get("base") or {}).get("ref") == gate.to
                          and (p.get("head") or {}).get("ref") == st.get("head_ref")), None)
            if promo is None:
                promo = writer.post(f"/repos/{repo}/pulls", {
                    "head": st.get("head_ref"), "base": gate.to, "title": f"Promote #{st['pr']} to {gate.to}",
                    "body": f"Opened by the Forgebrücke flow `{flow.id}` for #{st['pr']} at {st['head_sha'][:12]}." + FOOTER}).data or {}
                self.writes.append({"write": "promote_pr", "repo": repo, "number": promo.get("number")})
            try:
                writer.post(f"/repos/{repo}/pulls/{promo['number']}/merge",
                            {"Do": "merge", "head_commit_id": st["head_sha"],
                             "merge_message_field": f"Promote #{st['pr']} ({st['head_sha'][:12]}) to {gate.to}"})
                self.writes.append({"write": "promote_merge", "repo": repo, "number": promo.get("number"), "to": gate.to})
            except ForgejoError as e:
                self._end(st, flow, gate, run, "failed", f"the merge into {gate.to} was refused: {e.message}")
                return
            promoted = (reader.get(f"/repos/{repo}/branches/{gate.to}").data or {}).get("commit", {}).get("id") or ""
            run.evidence.update(promote_pr=promo.get("number"), promoted_sha=promoted)
            if gate.then != "verify_deploy":
                self._end(st, flow, gate, run, "passed", f"merged into {gate.to} at {promoted[:12]}")
                return
            run.detail = f"merged into {gate.to} at {promoted[:12]}; waiting for {', '.join(gate.contexts)}"
            return
        if run.state != "running":
            return
        promoted = run.evidence.get("promoted_sha") or ""
        ok, bad, missing, matched = self._match(gate.contexts, self._statuses(repo, promoted), f"forgebruecke/{flow.id}/")
        if bad:
            self._end(st, flow, gate, run, "failed", f"the deploy check failed on {gate.to}: {', '.join(bad)}")
        elif not missing:
            self._end(st, flow, gate, run, "passed", f"deployed from {gate.to}: {', '.join(sorted(matched))}")
        elif self.now() - (run.started_at or self.now()) > gate.timeout_min * 60:
            self._end(st, flow, gate, run, "failed", f"no deploy status on {gate.to} after {gate.timeout_min} min")

    # --- comment, notify, close ----------------------------------------------------------------

    def render_comment(self, st: dict, flow, runs: list) -> str:
        lines = [f"**Forgebrücke · flow `{flow.id}` · head `{st['head_sha'][:12]}`**", "",
                 "| Gate | State | What happened |", "| --- | --- | --- |"]
        for gate, run in zip(flow.gates, runs):
            ran_by = run.evidence.get("profile") if gate.type == "review" else None  # who really reviewed
            label = f"review by {ran_by}" if ran_by else gate.label()
            lines.append(f"| {label} | {ICONS.get(run.state, '')} {run.state} | {run.detail or '—'} |")
        review = next((r for g, r in zip(flow.gates, runs) if g.type == "review" and r.evidence.get("findings")), None)
        if review is not None:
            lines += ["", "**Review findings**"] + [
                f"- `{f.get('file')}`{(':' + str(f['line'])) if f.get('line') else ''}: {f.get('problem')}"
                for f in review.evidence["findings"]]
        nxt = next(((g, r) for g, r in zip(flow.gates, runs) if r.state not in self.models.DONE_STATES), None)
        if nxt is None:
            lines += ["", "Every gate passed."]
        elif nxt[1].state == "waiting":
            lines += ["", f"**What happens next:** {nxt[0].who or 'you'} merges in Forgejo. Nothing merges on its own."]
        elif nxt[1].state in ("failed", "error", "blocked"):
            lines += ["", f"**What happens next:** fix the cause and push; the {nxt[0].key} gate runs again on the new head."]
        return "\n".join(lines) + FOOTER

    def _status_comment(self, st: dict, flow, runs: list) -> None:
        body = self.render_comment(st, flow, runs)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        if digest == st.get("status_comment_hash"):
            return
        w = self._writer(flow)
        cid = st.get("status_comment_id")
        if cid and w.edit_comment(st["repo"], cid, body):
            st["status_comment_hash"] = digest
            return
        new_id = w.comment(st["repo"], st["pr"], body)
        st["status_comment_id"], st["status_comment_hash"] = new_id, digest

    def _notify_failures(self, st: dict, flow, runs: list) -> None:
        if flow.on_failure != "notify":
            return
        human = flow.human()
        who = (human.who if human and human.who else st["repo"].split("/")[0])
        notified = st.setdefault("notified", [])
        for gate, run in zip(flow.gates, runs):
            if run.state in ("failed", "error", "blocked") and run.id not in notified:
                word = {"failed": "failed", "error": "stopped with an error", "blocked": "is blocked"}[run.state]
                text = (f"@{who} the {gate.key} gate {word} on `{run.head_sha[:12]}`. {run.detail.rstrip('.')}. "
                        "Nothing was merged. The status comment above shows every gate.")
                self._writer(flow).comment(st["repo"], st["pr"], text + FOOTER)
                notified.append(run.id)
                run.evidence["notify_route"] = "pr_comment"
        st["notified"] = notified[-50:]

    def close_out(self, st: dict, flow) -> dict:
        pr = self.access.reader().get(f"/repos/{st['repo']}/pulls/{st['pr']}").data or {}
        if pr.get("state") == "open":
            return self.summary(st)
        runs = [self.models.GateRun.from_dict(g) for g in st["gates"]]
        for gate, run in zip(flow.gates, runs):
            if run.state == "running" and gate.type == "review":
                self.runner.stop(run.evidence.get("pid"))
                self._end(st, flow, gate, run, "error", "the pull request was closed while the review ran")
            if gate.type == "human" and pr.get("merged"):
                by = (pr.get("merged_by") or {}).get("login")
                run.state, run.ended_at = "passed", int(self.now())
                run.detail = f"merged by {by or 'a person'} at {str(pr.get('merge_commit_sha') or '')[:12]}"
                run.evidence.update(merged_by=by, merge_commit=pr.get("merge_commit_sha"))
        st.update(closed=True, merged=bool(pr.get("merged")), gates=[r.to_dict() for r in runs])
        self.store.save_pr(st)
        return self.summary(st)
