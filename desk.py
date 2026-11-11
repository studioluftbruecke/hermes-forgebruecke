"""The desk's backend. Pure functions over the flow store and the forge; the FastAPI adapter is
``dashboard/plugin_api.py``. The Desktop pane reads only these, through ``ctx.rest``, so no token ever
sits on the client.

Shapes (what the pane renders):

    desk     {ok, label, counts{waiting, parked, running, blocked} (waiting: section "now" only), rows[Row],
              repos[RepoCard], engine{...},
              can_merge, can_comment (his write token can comment: the feature owner_comment), you,
              waiting_note{message, suggestion, settings_href} | null, notes[],
              repos_error (a sentence when the repo list could not be read) | null,
              forge_error? (a sentence: Hermes' token is missing), generated_at,
              age_s (how old this read is when the route answers)}
    Row      {id, tab, repo, number, title, url, author, base, head_ref, head_sha, flow (null: no flow),
              reason (null for a flow row; "review requested" for a pull request that requests your review),
              gates[{key, type, segment, state, detail}], checks{state, statuses[...]} | null,
              last_comment{author, at, body, url, age_s} | null (the last one a person wrote), waiting_on,
              can_merge, next (one of MOVES: the main button), updated_at, age_s, mergeable (null: unknown),
              draft (null: unknown),
              summary{tone: ok | wait | run | bad, text: one sentence}, review_summary | null,
              wake_comment{author, at, url} | null (the newest comment not by you and not a flow status),
              section ("now" | "parked"; always "now" outside the waiting tab),
              parked{note, parked_at, wake_commits, seen, age_s} | null,
              back{reason (ready | commit | comment), text, url} | null (a parked row that came back)}
    RepoCard {repo, right (admin | write | read), url, collaboration_url, private, flows[], mentions, admin,
              open_rows, open_prs}: every repo the agent account sees, those with open pull requests or a
              flow first (its Forgejo right is the only permission layer; the collaboration page is where
              the user changes it)
    settings {ok, state (setup | unchecked | preview | ready), next_action{id, text, button, row, login?},
              setup[{id (url | bot | you | owner), state, value, problem, button}], forge_url, forge_error,
              tokens[Token], features[...], token_warnings[], next_step, profiles[], you, you_suggestion,
              you_source (write_token | repo_owner | null), switches{dry_run, review_every_pr, learn_mode,
              audit_enabled}, default_review_profile, reviewer{exists, name, command}, settings_href}
    Token    one row of ``capabilities.TOKENS`` (role, env, label, name, required, account, mint[], unlocks,
             never, risk?) plus what the probe found: set, login, scopes{} | null, error | null, is_admin, and
             renamed_from (the older name still holding a value while the new one is empty) | null

Waiting for me = flow rows whose human gate waits for you (not for a person the flow names), plus open pull requests whose
``requested_reviewers`` contain the ``you`` setting, plus (with ``review_every_pr``, the default) open pull
requests that should ask you and do not yet, marked "review not requested yet": the flows tick asks you
there, and the row shows even when that write cannot happen. The scan and the rule live in
``review_requests.py``. Without ``you`` the desk says so (``waiting_note``) and lists flow rows only.

Lanes: the desk reads in the agent lane, with the agent token, so it sees exactly what Hermes' Forgejo
account sees. The merge button is the owner lane: his write token, on his click, and only when the
registry says ``owner_merge`` is available and a flow's human gate waits; every other row links to Forgejo.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

TABS = ("waiting", "running", "blocked")
# A row's main button: the user's own move when the row waits for him, a link otherwise.
MOVES = ("merge", "merge_in_forgejo", "review_in_forgejo", "open_in_forgejo")
DESK_TTL_S = 15
SETTINGS_TTL_S = 60
WRITE_TOKEN_TTL_S = 3600
LATE_TICKS = 3
LATE_FLOOR_S = 600
MAX_COMMENT = 140
MAX_SUMMARY = 140
COMMENT_PAGE = 10
SWITCHES = ("dry_run", "review_every_pr", "learn_mode", "audit_enabled")
SETTABLE = ("you",) + SWITCHES
REVIEWER = "forgebruecke-reviewer"
SETTINGS_HREF = "/settings?tab=plugins&agent=forgebruecke"
REVIEWER_SOUL = """# Forgebrücke reviewer

You review one pull request when Forgebrücke asks you to. Read the prompt and the diff it gives you,
decide pass or fail, and write the verdict file the prompt names. Nothing else: no Forgejo writes, no
pushes, no comments, no other files. If you cannot decide, write fail with the reason.
"""

_SEGMENT = {"passed": "done", "skipped": "done", "running": "running", "pending": "pending",
            "waiting": "waiting", "failed": "failed", "error": "failed", "blocked": "failed"}
_cache: dict = {}
_lock = threading.Lock()


def _mod(name: str):
    return importlib.import_module(f"{__package__}.{name}")


def nav_label(waiting: int) -> str:
    """The sidebar row's label: the brand, plus the count of pull requests waiting for the user (D1 of the
    sidebar-rows plan). Desktop 0.0.22 and newer compose the row from `counts.waiting` the same way, so this
    string is for older Desktop halves and the desk answer's readers."""
    if waiting <= 0:
        return "Forgebrücke"
    return f"Forgebrücke · {waiting if waiting < 100 else '99+'}"


def segment(state: str) -> str:
    return _SEGMENT.get(state, "pending")


def tab_of(st: dict, *, you: Optional[str] = None) -> Optional[str]:
    """Which desk tab a PR's flow state belongs to; None for a closed or merged PR. With ``you`` (the
    setting, also when empty) a merge the flow gives to another person is not waiting for the user."""
    if st.get("closed") or st.get("merged"):
        return None
    states = [g.get("state") for g in st.get("gates") or []]
    if any(segment(s) == "failed" for s in states):
        return "blocked"
    if "waiting" in states:
        who = _waiting_on(_human(st))
        return "running" if you is not None and who and who.lower() != you.lower() else "waiting"
    return "running"


def _move(mine: bool, flow: Any, can_merge: bool) -> str:
    """A row's main button (one of ``MOVES``). ``mine``: the row sits under Waiting for me."""
    if not mine:
        return "open_in_forgejo"
    if not flow:
        return "review_in_forgejo"
    return "merge" if can_merge else "merge_in_forgejo"


def _human(st: dict) -> Optional[dict]:
    return next((g for g in st.get("gates") or [] if g.get("type") == "human"), None)


def _cap(text: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _ago(seconds: int) -> str:
    """An age in the words the pages use."""
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{round(seconds / size)} {unit} ago"
    return f"{seconds} s ago"


def _checks(client, repo: str, sha: str) -> Optional[dict]:
    try:
        combined = client.get(f"/repos/{repo}/commits/{sha}/status").data or {}
    except Exception:
        return None
    return _mod("tools.read").compact_status(combined)


def _last_comment(client, repo: str, number: int, *, now: Callable[[], float] = time.time,
                  you: Optional[str] = None) -> "tuple[Optional[dict], Optional[dict]]":
    """``(last_comment, wake_comment)`` from the same pages (the last two of ten).

    ``last_comment``: the newest comment a person wrote. Forgebrücke's own comments (status and tool comments,
    marked by their footer) are skipped: the row already shows what they say.
    ``wake_comment`` {author, at, url}: the newest comment not by ``you`` and not a flow status comment; a
    comment Hermes posted with ``forgejo_comment`` counts (L14, provisional). A parked row wakes on it.
    A failed read gives ``(None, None)``, which wakes nothing."""
    path = f"/repos/{repo}/issues/{number}/comments"
    me = (you or "").lower()

    def waker(c: dict) -> bool:
        author = str((c.get("user") or {}).get("login") or "").lower()
        return bool(author) and author != me and _FOOTERS[0] not in str(c.get("body") or "")

    try:
        first = client.get(path, {"limit": COMMENT_PAGE, "page": 1})
        total = int((first.headers or {}).get("x-total-count") or 0) if hasattr(first, "headers") else 0
        last_page = max(1, -(-total // COMMENT_PAGE))
        found = wakes = None
        for page in (last_page, last_page - 1):
            if page < 1:
                break
            items = (first if page == 1 else client.get(path, {"limit": COMMENT_PAGE, "page": page})).data or []
            found = found or next((c for c in reversed(items) if not _own(c)), None)
            wakes = wakes or next((c for c in reversed(items) if waker(c)), None)
            if found is not None and wakes is not None:
                break
    except Exception:
        return None, None
    wake = ({"author": (wakes.get("user") or {}).get("login"), "at": wakes.get("created_at"),
             "url": wakes.get("html_url")} if wakes is not None else None)
    if found is None:
        return None, wake
    at = _epoch(found.get("created_at"))
    return ({"author": (found.get("user") or {}).get("login"), "at": found.get("created_at"),
             "body": _cap(plain_text(found.get("body")), MAX_COMMENT), "url": found.get("html_url"),
             "age_s": max(0, int(now() - at)) if at is not None else None}, wake)


_FOOTERS = ("Kept up to date by Forgebrücke", "Posted by Hermes through Forgebrücke")


def _own(comment: dict) -> bool:
    return any(footer in str(comment.get("body") or "") for footer in _FOOTERS)


def plain_text(body: Any) -> str:
    """A comment as one line of plain text for the desk: no HTML tags, no Markdown emphasis or code
    marks, table rows as "a · b · c", separator rows and Forgebrücke's own footers dropped. The desk
    shows text, never markup (seen on the Mac: a status comment arrived as raw `| --- |` and `<sub>`)."""
    out = []
    for line in str(body or "").splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or re.fullmatch(r"\|?[\s:|-]+\|?", line) or line.startswith(_FOOTERS):
            continue
        if line.startswith("|"):
            line = " · ".join(cell.strip() for cell in line.strip("|").split("|") if cell.strip())
        out.append(re.sub(r"\*\*|`", "", line))
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def _waiting_on(human: Optional[dict]) -> Optional[str]:
    m = re.match(r"waiting for (\S+)", str((human or {}).get("detail") or ""))
    return m.group(1) if m and m.group(1) != "you" else None


def _sentence(text: Any) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    found = re.match(r"(.+?[.!?])(?:\s|$)", s)
    return found.group(1) if found else s


def _failed_checks(r: dict) -> list:
    return [s for s in (r.get("checks") or {}).get("statuses") or [] if s.get("state") in ("failure", "error")]


def summarise(r: dict, you: str = "") -> dict:
    """Where a row stands, as one sentence and a tone. What stops a merge comes first: a failed gate, a
    conflict, a failed check. Then what runs, then what waits. Whose move a waiting row is was decided
    by its tab (``tab_of``); ``you`` is no longer read."""
    gates = r.get("gates") or []

    def name(g: dict) -> str:
        return "Merge" if g.get("type") == "human" else str(g.get("key") or g.get("type") or "gate").capitalize()

    def say(tone: str, text: str) -> dict:
        return {"tone": tone, "text": _cap(text, MAX_SUMMARY)}

    failed = next((g for g in gates if g.get("segment") == "failed"), None)
    if failed:
        detail = re.sub(r"^\S+ by \S+:\s*", "", str(failed.get("detail") or ""))
        detail = _sentence(detail)
        end = "" if detail.endswith((".", "!", "?")) else "."
        return say("bad", f"{name(failed)} failed: {detail}{end}" if detail else f"{name(failed)} failed.")
    rr = _mod("review_requests")
    if r.get("draft"):
        return say("wait", "Draft. Waiting for your review." if r.get("reason") == rr.ASKED else "Draft.")
    if r.get("mergeable") is False:  # a conflict, or Forgejo has not finished checking: it does not say which
        return say("bad", "Waiting for your review. Cannot be merged yet." if r.get("reason") == rr.ASKED
                   else "Cannot be merged yet.")
    red = _failed_checks(r)
    if red:
        return say("bad", f"Check {red[0].get('context')} failed." if len(red) == 1 else f"{len(red)} checks failed.")
    running = next((g for g in gates if g.get("segment") == "running"), None)
    if running:
        return say("run", f"{name(running)} is running.")
    if r.get("flow") and any(g.get("segment") == "waiting" for g in gates):
        if r.get("tab") == "waiting":
            return say("ok", "Ready. Every gate passed, your merge is next.")
        return say("wait", f"Every gate passed. Waiting for {r.get('waiting_on') or 'someone else'} to merge.")
    pending = next((g for g in gates if g.get("segment") == "pending"), None)
    if pending:
        return say("run", f"{name(pending)} is next.")
    if r.get("reason") == rr.ASKED:
        return say("wait", "Waiting for your review.")
    if r.get("reason") == rr.NOT_ASKED:
        return say("wait", "Open, and nobody asked you to review it yet.")
    if gates:
        return say("ok", "Every gate passed.")
    return say("wait", "Open.")


def _review_summary(r: dict) -> Optional[str]:
    done = next((g for g in r.get("gates") or [] if g.get("type") == "review" and g.get("segment") == "done"), None)
    text = _sentence((done or {}).get("detail"))
    return _cap(text[:1].upper() + text[1:], 200) if text else None


def row(st: dict, client: Any, *, can_merge: bool, now: Callable[[], float] = time.time,
        you: Optional[str] = None) -> Optional[dict]:
    tab = tab_of(st, you=you)
    if tab is None:
        return None
    mine = tab == "waiting"
    repo, n, sha = st["repo"], int(st["pr"]), st.get("head_sha") or ""
    gates = [{"key": g.get("gate_key"), "type": g.get("type"), "state": g.get("state"),
              "segment": segment(g.get("state")), "detail": _cap(g.get("detail"), 200)}
             for g in st.get("gates") or []]
    human = _human(st)
    last, wake = _last_comment(client, repo, n, now=now, you=you) if client is not None else (None, None)
    return {"id": f"{repo}#{n}", "tab": tab, "repo": repo, "number": n, "title": st.get("title") or f"PR #{n}",
            "url": st.get("url") or "", "author": st.get("author"), "base": st.get("base"),
            "head_ref": st.get("head_ref"), "head_sha": sha, "flow": st.get("flow"), "reason": None, "gates": gates,
            "checks": _checks(client, repo, sha) if client is not None and sha else None,
            "last_comment": last, "wake_comment": wake,
            "waiting_on": _waiting_on(human) if (human or {}).get("state") == "waiting" else None,
            "can_merge": bool(can_merge and mine), "updated_at": st.get("updated_at"), "mergeable": None,
            "draft": None,
            "next": _move(mine, st.get("flow") or True, bool(can_merge))}


def repo_cards(access: Any, rows: list, *, pulls: Optional[dict] = None) -> list:
    """Every repo the agent account sees, with its Forgejo right and the switches the rules set there.
    Repos with open pull requests or a flow come first. ``pulls`` is the desk's own scan ({repo: [pull]});
    a repo outside it takes Forgejo's counter from the repo list."""
    settings, acc = access.settings, _mod("access")
    scanned = {k.lower(): len(v) for k, v in (pulls or {}).items()}
    out = []
    for r in access.repos():
        repo, right = r["repo"], r["right"]
        rule = settings.rule_for(repo)
        out.append({"repo": repo, "right": right, "url": r.get("url") or f"{settings.url.rstrip('/')}/{repo}",
                    "collaboration_url": acc.collaboration_url(settings.url, repo), "private": r.get("private"),
                    "flows": list(rule.flows), "mentions": bool(rule.mentions and right in acc.WRITE_RIGHTS),
                    "admin": right == "admin",
                    "open_rows": sum(1 for x in rows if x.get("flow") and x["repo"].lower() == repo.lower()),
                    "open_prs": scanned.get(repo.lower(), int(r.get("open_prs") or 0))})
    out.sort(key=lambda c: (not (c["open_prs"] or c["flows"]), c["repo"].lower()))
    return out


def _epoch(iso: Any) -> Optional[int]:
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def review_rows(client: Any, you: str, known: set, can_merge: bool = False, every_pr: bool = False, *,
                pulls: Optional[dict] = None, now: Callable[[], float] = time.time) -> "tuple[list, list]":
    """Open pull requests that ask ``you`` to review them, and with ``every_pr`` those that should and do not
    yet, as Waiting rows without a flow, plus notes for what was cut or unreadable. ``known`` holds the
    ids of flow rows in lower case (a flow row wins; Forgejo's names ignore case). ``can_merge``: his write token can merge, so the row offers the Merge
    button. ``pulls`` is an ``open_pulls`` scan the caller already ran; without it the scan runs here."""
    rr = _mod("review_requests")
    pulls, notes = (pulls, []) if pulls is not None else rr.open_pulls(client)
    rows = []
    for repo, prs in pulls.items():
        for pr in prs:
            n = int(pr.get("number") or 0)
            if not n or f"{repo}#{n}".lower() in known:
                continue
            if rr.asked(pr, you):
                reason = rr.ASKED
            elif every_pr and rr.needs_request(pr, you, rr.reviews_reader(client, repo, n)):
                reason = rr.NOT_ASKED
            else:
                continue
            sha = (pr.get("head") or {}).get("sha") or ""
            last, wake = _last_comment(client, repo, n, now=now, you=you)
            rows.append({"id": f"{repo}#{n}", "tab": "waiting", "repo": repo, "number": n,
                         "title": pr.get("title") or f"PR #{n}", "url": pr.get("html_url") or "",
                         "author": (pr.get("user") or {}).get("login"), "base": (pr.get("base") or {}).get("ref"),
                         "head_ref": (pr.get("head") or {}).get("ref"), "head_sha": sha, "flow": None,
                         "reason": reason, "gates": [],
                         "checks": _checks(client, repo, sha) if sha else None,
                         "last_comment": last, "wake_comment": wake, "waiting_on": None,
                         "can_merge": bool(can_merge and reason == rr.ASKED), "next": _move(True, None, False),
                         "updated_at": _epoch(pr.get("updated_at")), "mergeable": _mergeable(pr),
                         "draft": _draft(pr)})
    return rows, notes


def _mergeable(pr: Optional[dict]) -> Optional[bool]:
    value = (pr or {}).get("mergeable")
    return value if isinstance(value, bool) else None


def _draft(pr: Optional[dict]) -> Optional[bool]:
    value = (pr or {}).get("draft")
    return value if isinstance(value, bool) else None


def _account_kind(client: Any, login: str) -> Optional[str]:
    """``user``, ``org``, or None when Forgejo did not say. Forgejo answers ``/users/{login}`` for an
    organisation too, so the organisation route decides."""
    try:
        client.get(f"/orgs/{login}")
        return "org"
    except Exception as exc:
        return "user" if getattr(exc, "status", None) == 404 else None


def suggest_login(client: Any, counts: dict, agent: str) -> str:
    """Who the user probably is: the most common owner of the agent account's repos that is a person. An
    organisation, or an owner whose kind Forgejo did not tell, is never suggested."""
    acc = _mod("access")
    skip: list = []
    for _ in range(4):
        try:
            name = acc.suggest_you(counts, agent, skip=tuple(skip))
        except TypeError:  # suggest_you without skip
            name = acc.suggest_you({o: n for o, n in (counts or {}).items() if o not in skip}, agent)
        if not name:
            return ""
        if _account_kind(client, name) == "user":
            return name
        skip.append(name)
    return ""


def build_desk(access: Any, store: Any, *, can_merge: bool = False, now: Callable[[], float] = time.time,
               you_hint: Optional[dict] = None, data_dir: Any = None, prune: bool = False) -> dict:
    """``you_hint`` ({login, source}) is who the settings route already found the user to be; without it the
    desk asks the forge itself when ``you`` is empty. ``data_dir`` holds the parked rows (``parking.py``);
    without it nothing is parked. ``prune`` (only the page's ``GET /desk``) drops parked entries of pull
    requests that are no longer open, and only after a complete scan."""
    forge_error = None
    try:
        client = access.reader()
    except Exception:  # no token for Hermes' account: the stored rows still render
        client = None
        forge_error = NEXT_TEXT["set_token"] + " Checks, comments and review requests are missing."
    you = access.settings.you
    rows = [r for r in (row(st, client, can_merge=can_merge, now=now, you=you) for st in store.all_prs())
            if r is not None]
    notes: list = []
    missing: list = []  # what a failing Forgejo left out: one note for one outage
    failure: Optional[Exception] = None
    waiting_note = None
    pulls: dict = {}
    complete_scan, scan_at = False, now()
    if client is not None and you:
        try:
            pulls, notes = _mod("review_requests").open_pulls(client)
            complete_scan = not notes  # a cut search, a cut repo list or an unreadable repo: never prune
            extra, _ = review_rows(client, you, {r["id"].lower() for r in rows}, can_merge=can_merge,
                                   every_pr=access.settings.review_every_pr, pulls=pulls, now=now)
            by_id = {f"{repo}#{pr.get('number')}".lower(): pr for repo, prs in pulls.items() for pr in prs}
            for r in rows:
                pr = by_id.get(r["id"].lower())
                r["mergeable"], r["draft"] = _mergeable(pr), _draft(pr)
            rows += extra
        except Exception as exc:  # the flow rows still render when the search fails
            missing.append("review requests")
            failure, complete_scan = exc, False
    elif not you:
        hint = dict(you_hint or {})
        if not hint and client is not None:
            try:
                acc = _mod("access")
                hint = {"login": suggest_login(client, acc.owner_counts(access.repos()), acc.account_login(client)),
                        "source": "repo_owner"}
            except Exception:
                hint = {}
        login = hint.get("login") or None
        waiting_note = {"message": "Which Forgejo user are you?", "suggestion": login,
                        "source": hint.get("source") if login else None, "settings_href": SETTINGS_HREF}
    repos, repos_error = [], None if client is not None else REPOS_UNREAD
    if client is not None:
        try:
            repos = repo_cards(access, rows, pulls=pulls)
        except Exception as exc:
            missing.append("repos")
            failure, repos_error = failure or exc, REPOS_UNREAD
    if missing:
        notes.append(f"{_observed(failure)} {' and '.join(missing).capitalize()} are missing.")
    park_mod = _mod("parking")
    parked, unreadable = park_mod.load(data_dir)
    if unreadable:
        notes.append(park_mod.CORRUPT_NOTE)
    elif prune and complete_scan and data_dir:
        keep = {f"{repo}#{pr.get('number')}" for repo, prs in pulls.items() for pr in prs}
        gone = park_mod.prune(data_dir, keep | {r["id"] for r in rows if r.get("flow")}, scan_at=scan_at, now=now)
        parked = {k: v for k, v in parked.items() if k not in gone}
    for r in rows:
        entry = parked.get(r["id"].lower()) if r["tab"] == "waiting" else None
        r["back"] = park_mod.wake(entry, r, now, forge_url=access.settings.url) if entry else None
        r["parked"] = ({"note": entry.get("note") or "", "parked_at": entry.get("parked_at"),
                        "wake_commits": bool(entry.get("wake_commits")), "seen": entry.get("seen"),
                        "age_s": max(0, int(now() - int(entry.get("parked_at") or 0)))} if entry else None)
        r["section"] = "parked" if entry and not r["back"] else "now"
    for r in rows:
        r["age_s"] = max(0, int(now() - r["updated_at"])) if r.get("updated_at") else None
        if r["can_merge"] and (r.get("draft") or r.get("mergeable") is False):  # Forgejo would refuse the merge
            r["can_merge"], r["next"] = False, _move(r["tab"] == "waiting", r.get("flow"), False)
        r["summary"] = summarise(r, you)
        r["review_summary"] = _review_summary(r)
    order = {t: i for i, t in enumerate(TABS)}
    rows.sort(key=lambda r: (order[r["tab"]], r["section"] == "parked", r["back"] is None,
                             -((r["parked"] or {}).get("parked_at") or 0) if r["section"] == "parked"
                             else -(r.get("updated_at") or 0)))
    counts = {t: sum(1 for r in rows if r["tab"] == t and r["section"] == "now") for t in TABS}
    counts = {"waiting": counts["waiting"], "parked": sum(1 for r in rows if r["section"] == "parked"),
              "running": counts["running"], "blocked": counts["blocked"]}
    engine = store.engine() or {}
    tick_age = int(now() - engine["at"]) if engine.get("at") else None
    if late_tick(engine, access.settings, now) is not None and any(r.get("flow") for r in rows):
        notes.append(f"Flows last ran {_ago(tick_age)}. Rows of a flow may be out of date.")
    out = {"ok": True, "label": nav_label(counts["waiting"]), "counts": counts, "rows": rows,
           "repos": repos, "repos_error": repos_error, "can_merge": bool(can_merge), "you": you or None,
           "can_comment": False, "waiting_note": waiting_note, "notes": notes,
           "engine": {"at": engine.get("at"), "source": engine.get("source"), "age_s": tick_age,
                      "errors": (engine.get("errors") or [])[:3]},
           "forge_url": access.settings.url, "version": _mod("version").VERSION, "generated_at": int(now())}
    if forge_error:
        out["forge_error"] = forge_error
    return out


def late_tick(engine: dict, settings: Any, now: Callable[[], float]) -> Optional[int]:
    """Seconds since the last flows tick when that is late and a tick has work, else None."""
    if not (engine or {}).get("at"):
        return None
    age = int(now() - engine["at"])
    late_after = max(LATE_FLOOR_S, LATE_TICKS * settings.poll_interval)
    return age if age > late_after and _mod("flows.loop").has_work(settings) else None


UPDATE_PENDING = "channel-update-pending.json"  # written by channel/adapter.py
STOPPED_FIX = "Restart Hermes' gateway, or check the Forgebrücke tick job."


def background(data_dir: Any, settings: Any, now: Callable[[], float]) -> Optional[dict]:
    """What stopped in the background, for the settings page: ``{text, fix, since}`` or None. A mention channel
    that stopped for a half-finished update says so; otherwise a late flows tick (it also asks for reviews)."""
    if not data_dir:
        return None
    try:
        marker = json.loads((Path(data_dir) / UPDATE_PENDING).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        marker = None
    if isinstance(marker, dict) and marker.get("at"):
        age = max(0, int(now() - float(marker["at"])))
        return {"text": f"Mentions and review requests stopped {_ago(age)}.",
                "fix": "Restart the gateway to finish the update.", "since": int(marker["at"])}
    engine = _mod("flows.state").FlowStore(data_dir).engine() or {}
    age = late_tick(engine, settings, now)
    if age is None:
        return None
    return {"text": f"Mentions and review requests stopped {_ago(age)}.", "fix": STOPPED_FIX,
            "since": int(engine["at"])}


MAX_PROTECTION_READS = 10


def unprotected_mains(access: Any) -> list:
    """With Security audit on: repos where Hermes' account is admin (only an admin can read branch rules) and
    the default branch has no protection rule. ``[{repo, branch, url}]``, at most MAX_PROTECTION_READS reads."""
    if not access.settings.audit_enabled:
        return []
    out = []
    admin = [r for r in access.repos() if r.get("right") == "admin" and not r.get("archived")]
    for r in admin[:MAX_PROTECTION_READS]:
        branch = r.get("default_branch") or "main"
        try:
            found = _mod("tools.repos")._protection(access, r["repo"], branch)
        except Exception:
            continue
        if found.get("protected") is False:
            base = r.get("url") or f"{access.settings.url.rstrip('/')}/{r['repo']}"
            out.append({"repo": r["repo"], "branch": branch, "url": f"{base}/settings/branches"})
    return out


# The routes of a profile other than the default run in that profile's plugin host child, which reads the
# profile's secrets once, when it starts (Hermes hermes_cli/plugin_host.py). A token saved later stays unseen
# until the child restarts. Hermes marks the child with this variable.
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
_IMPORTED = time.time()


def _process_started() -> float:
    """This process's start time: /proc on Linux, else when this module was imported (later, so a token saved
    in between reads as seen)."""
    try:
        fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                     if line.startswith("btime "))
        return btime + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return _IMPORTED


def in_plugin_host() -> bool:
    return os.environ.get(HOST_PROCESS_ENV) == "1"


def tokens_unseen(home: Any) -> bool:
    """True when this plugin host child started before the profile's .env last changed."""
    if not home or not in_plugin_host():
        return False
    try:
        return (Path(home) / ".env").stat().st_mtime > _process_started()
    except OSError:
        return False


RELOAD_TEXT = "Hermes has not picked up a token saved after it started."


REPOS_UNREAD = "The repo list could not be read."


def _observed(exc: Optional[Exception]) -> str:
    """What a failed read showed, without a guessed cause."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return f"Forgejo answered {status}."
    return "Forgejo did not answer." if hasattr(exc, "kind") else f"A read failed ({type(exc).__name__})."


# A refusal for a setting that is still empty is a setup step, said in the settings page's own words.
_SETUP_STEP = {("not_configured", "url"): "set_url", ("not_configured", "token"): "set_token",
               ("no_bot_token", "token"): "set_token"}


def _refusal_body(exc: Any) -> dict:
    """A route's answer when it could not do its work. ``action`` says which button helps: ``settings``
    for a missing setting (with ``title``), ``retry`` for everything else."""
    code, setting = getattr(exc, "code", "error"), getattr(exc, "setting", "")
    out = {"ok": False, "error": code, "message": getattr(exc, "message", str(exc)), "action": "retry",
           "setting": setting, "settings_href": SETTINGS_HREF}
    step = _SETUP_STEP.get((code, setting))
    if step:
        out.update(title="Set up Forgebrücke first", message=NEXT_TEXT[step], action="settings")
    return out


def _cached(key: tuple, ttl: float, refresh: bool, make: Callable[[], dict], now: Callable[[], float]) -> dict:
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and now() - hit[0] < ttl:
            return hit[1]
    value = make()
    with _lock:
        _cache[key] = (now(), value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def clear_desk_cache() -> None:
    """Drop only the cached desk answers. The hour-long write-token facts stay: a park changes no token, and
    dropping them would rerun the doctor's write-scope probes on the next desk read."""
    with _lock:
        for k in [k for k in _cache if k and k[0] == "desk"]:
            del _cache[k]


# --- the routes' functions: each returns (http_status, body) ---------------------------------------

def api_settings(get_setting, get_secret, *, transport: Any = None, refresh: bool = False,
                 now: Callable[[], float] = time.time, home: Any = None, data_dir: Any = None) -> "tuple[int, dict]":
    def make() -> dict:
        rep = _plugin_root().doctor_report(get_setting, get_secret, transport=transport)
        profile = _mod("profile")
        profiles = profile.list_profiles(home) or []
        settings_mod = _mod("settings")
        presence = settings_mod.load_credentials(get_secret).presence()
        probed = {t.get("role"): t for t in rep.get("tokens") or []}
        tokens = []
        for field in _mod("capabilities").TOKENS:
            probe = probed.get(field["role"]) or {}
            old = settings_mod.RENAMED_FROM.get(field["env"])
            renamed = bool(old) and not presence[field["env"]] and bool((get_secret(old) or "").strip())
            tokens.append({**{k: v for k, v in field.items() if k != "mint"}, "mint": list(field["mint"]),
                           "set": presence[field["env"]], "renamed_from": old if renamed else None,
                           "login": probe.get("identity"),
                           "scopes": probe.get("scopes"), "error": probe.get("error"),
                           "is_admin": probe.get("is_admin", False)})
        default = settings_mod.load_settings(get_setting, lambda: profile.current_name(home))
        instance = rep.get("instance") or {}
        login, source = _who(rep, probed, default, get_secret, transport)
        verdicts = token_verdicts(rep, {t["role"]: t["set"] for t in tokens})
        action = next_action(rep, default, login=login, verdicts=verdicts)
        rows = setup_rows(rep, default, action, verdicts)
        said = {action["text"], *(r["problem"] for r in rows)}
        unprotected: list = []
        if default.audit_enabled and not instance.get("error"):
            try:
                unprotected = unprotected_mains(_mod("access").load_access(get_setting, get_secret, transport=transport,
                                                                           lane="agent"))
            except Exception:
                unprotected = []
        return {"ok": True, "state": ACTIONS[action["id"]][0], "next_action": action, "setup": rows,
                "forge_url": instance.get("url"), "forge_error": (instance.get("error") or {}).get("message"),
                "tokens": tokens, "features": rep.get("features") or [],
                "token_warnings": [w for w in rep.get("token_warnings") or [] if w not in said],
                "next_step": rep.get("next_step"), "profiles": profiles,
                "default_review_profile": default.default_review_profile,
                "you": default.you or None, "you_suggestion": login or None, "you_source": source,
                "switches": {key: bool(getattr(default, key)) for key in SWITCHES},
                "reviewer": {"name": REVIEWER, "exists": REVIEWER in profiles,
                             "command": reviewer_command(profile.current_name(home))},
                "background": background(data_dir, default, now), "unprotected": unprotected,
                "version": _mod("version").VERSION,
                "settings_href": SETTINGS_HREF, "generated_at": int(now())}
    try:
        body = _cached(("settings",), SETTINGS_TTL_S, refresh, make, now)
        # Read on every answer, never cached: a token saved a second ago must show at once.
        unseen = tokens_unseen(home)
        return 200, {**body, "reload": {"needed": unseen, "text": RELOAD_TEXT if unseen else None}}
    except Exception as exc:
        return 200, _refusal_body(exc)


def _plugin_root():
    return importlib.import_module(__package__)


def _who(rep: dict, probed: dict, settings: Any, get_secret, transport: Any) -> "tuple[str, Optional[str]]":
    """``(login, source)`` of who the user probably is. The write token's own login is certain
    (``write_token``); otherwise the owner of the repos Hermes sees (``repo_owner``), never an organisation."""
    bot, owner = probed.get("bot") or {}, probed.get("owner") or {}
    mine = owner.get("identity")
    if owner.get("set") and mine and not owner.get("error") and mine != bot.get("identity"):
        return mine, "write_token"
    token = _mod("settings").load_credentials(get_secret).token
    counts = (bot.get("repos") or {}).get("owners") or {}
    if not (settings.url and token and counts and not bot.get("error")):
        return "", None
    client = _mod("client").ForgejoClient(settings.url, token, transport=transport)
    login = suggest_login(client, counts, bot.get("identity") or "")
    return (login, (rep.get("you") or {}).get("source") or "repo_owner") if login else ("", None)


NEXT_TEXT = {"set_url": "Enter your Forgejo address.", "set_token": "Add the token of Hermes' account.",
             "set_you": "Which Forgejo user are you?",
             "dry_run": "Preview only: Hermes shows each write and sends none.", "none": ""}
# Every next step: the badge it means, the button beside its sentence, the setup row it belongs to.
# ``ready`` belongs to ``none`` alone, so the badge cannot say ready while a step is open.
ACTIONS = {
    "set_url": ("setup", "settings", "url"),
    "set_token": ("setup", "settings", "bot"),
    "fix_token": ("setup", "settings", "bot"),
    "fix_scopes": ("setup", "settings", "bot"),
    "add_repo": ("setup", "forgejo", "bot"),
    "set_you": ("setup", "who", "you"),
    "fix_owner_token": ("setup", "settings", "owner"),
    "retry": ("unchecked", "retry", None),
    "dry_run": ("preview", "write", None),
    "none": ("ready", None, None),
}
_TOKEN_ACTION = {
    "bot": {"unset": "set_token", "refused": "fix_token", "lacks_scopes": "fix_scopes", "no_repo": "add_repo",
            "unchecked": "retry"},
    "owner": {"refused": "fix_owner_token", "lacks_scopes": "fix_owner_token", "wrong_account": "fix_owner_token",
              "unchecked": "retry"},
}


def token_verdicts(rep: dict, presence: Optional[dict] = None) -> dict:
    """``{role: capabilities.token_verdict}`` for both tokens. ``presence`` ({role: bool}) says which fields
    hold a value when the report stopped before it probed them."""
    probed = {t.get("role"): t for t in rep.get("tokens") or []}
    caps = _mod("capabilities")
    out = {}
    for role in ("bot", "owner"):
        token = {"role": role, **(probed.get(role) or {})}
        if presence is not None:
            token["set"] = bool(presence.get(role))
        out[role] = caps.token_verdict(token)
    return out


def next_action(rep: dict, settings: Any, *, login: str = "", verdicts: Optional[dict] = None) -> dict:
    """The one thing to do next, read off the doctor's facts in the order the setup is done:
    ``{id, text, button, row, login?}``. ``ACTIONS`` maps the id to the badge."""
    instance = rep.get("instance") or {}
    verdicts = verdicts or token_verdicts(rep)

    def act(key: str, text: str = "", row: Optional[str] = None) -> dict:
        _badge, button, own_row = ACTIONS[key]
        out = {"id": key, "text": _cap(text or NEXT_TEXT[key], 140), "button": button, "row": row or own_row}
        if key == "set_you" and login:
            out["login"] = login
        return out

    def token_step(role: str) -> Optional[dict]:
        verdict = verdicts[role]
        key = _TOKEN_ACTION[role].get(verdict["state"])
        return act(key, verdict["text"] or "", row=role) if key else None

    url, error = instance.get("url"), instance.get("error")
    if not url:
        return act("set_url")
    if error:
        status = error.get("status")
        if status in (401, 403) and verdicts["bot"]["state"] != "unset":
            return act("fix_token", f"Forgejo refused Hermes' token ({status}). Replace it.")
        if status and status >= 500:
            return act("retry", f"Forgejo failed ({status}). Try again.", row="url")
        if status:
            return act("set_url", f"No Forgejo answered at {url} ({status}). Check your Forgejo address.")
        return act("set_url", f"Hermes cannot reach {url}. Check your Forgejo address.")
    if instance.get("supported") is False:
        return act("set_url", instance.get("reason") or "This Forgejo version is not supported.")
    return (token_step("bot") or (None if settings.you else act("set_you")) or token_step("owner")
            or act("dry_run" if settings.dry_run else "none"))


def setup_rows(rep: dict, settings: Any, action: dict, verdicts: dict) -> list:
    """The four setup rows as the page draws them: ``[{id, state, value, problem, button}]``. ``state`` is
    done, todo, problem, unchecked (Forgejo could not say) or optional. ``problem`` is the row's sentence
    unless the next step already says it; ``button`` is None for the row whose button the next step carries."""
    instance = rep.get("instance") or {}
    url = instance.get("url") or ""
    bot, owner = verdicts["bot"], verdicts["owner"]
    url_state = "todo" if not url else "done"
    if url and (instance.get("error") or instance.get("supported") is False):
        url_state = "unchecked" if action["id"] == "retry" else "problem"
    rows = [
        ("url", url_state, re.sub(r"^https?://", "", url).rstrip("/"), None),
        ("bot", {"unset": "todo", "ok": "done", "unchecked": "unchecked"}.get(bot["state"], "problem"), bot["value"], bot),
        ("you", "done" if settings.you else "todo", f"@{settings.you}" if settings.you else "", None),
        ("owner", {"unset": "optional", "ok": "done", "unchecked": "unchecked"}.get(owner["state"], "problem"),
         owner["value"] or "off", owner),
    ]
    out = []
    for row_id, state, value, verdict in rows:
        problem = (verdict or {}).get("text")
        button = "who" if row_id == "you" and state == "todo" else "settings"
        if verdict and verdict["state"] == "unchecked":
            button = "retry"
        if verdict and verdict["state"] == "no_repo":
            button = "forgejo"
        mine = action.get("row") == row_id
        out.append({"id": row_id, "state": state, "value": value,
                    "problem": None if mine or problem == action.get("text") else problem,
                    "button": None if mine else button})
    return out


def _write_token_facts(get_setting, get_secret, *, transport: Any = None, now: Callable[[], float] = time.time,
                       refresh: bool = False) -> dict:
    """What the user's write token gives: ``{can_merge, login, source}``. Finding out runs the doctor, whose
    scope probes are requests that are not reads, so the answer is kept for an hour per pair of tokens: a
    poll with nobody clicking must not repeat them. A check Forgejo could not finish is not kept."""
    creds = _mod("settings").load_credentials(get_secret)
    if not creds.owner_token:  # never probe for a field that is empty
        return {"can_merge": False, "can_comment": False, "login": None, "source": None}
    held = "\0".join((str(get_setting("url", "") or ""), creds.token or "", creds.owner_token))
    key = ("write_token", hashlib.sha256(held.encode("utf-8")).hexdigest())
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and now() - hit[0] < WRITE_TOKEN_TTL_S:
            return hit[1]
    body = api_settings(get_setting, get_secret, transport=transport, now=now, refresh=refresh)[1]
    lit = {f.get("id") for f in body.get("features") or [] if f.get("status") == "available"}
    facts = {"can_merge": "owner_merge" in lit, "can_comment": "owner_comment" in lit,
             "login": body.get("you_suggestion"), "source": body.get("you_source")}
    owner = next((t for t in body.get("tokens") or [] if t.get("role") == "owner"), None)
    if body.get("features") and _mod("capabilities").token_verdict(owner)["state"] != "unchecked":
        with _lock:
            _cache[key] = (now(), facts)
    return facts


def owner_can_merge(get_setting, get_secret, *, transport: Any = None, now: Callable[[], float] = time.time,
                    refresh: bool = False) -> bool:
    return _write_token_facts(get_setting, get_secret, transport=transport, now=now, refresh=refresh)["can_merge"]


class _NoFlowState:
    """The flow store of an install whose data folder does not exist yet: no flow has run there."""

    def all_prs(self) -> list:
        return []

    def engine(self) -> dict:
        return {}


def api_desk(get_setting, get_secret, *, data_dir: Any, transport: Any = None, refresh: bool = False,
             now: Callable[[], float] = time.time) -> "tuple[int, dict]":
    def make() -> dict:
        access = _mod("access").load_access(get_setting, get_secret, transport=transport, lane="agent")
        store = _mod("flows.state").FlowStore(data_dir) if data_dir else _NoFlowState()
        mine = _write_token_facts(get_setting, get_secret, transport=transport, now=now, refresh=refresh)
        hint = None
        if not access.settings.you and mine["login"]:
            hint = {"login": mine["login"], "source": mine["source"]}
        out = build_desk(access, store, can_merge=mine["can_merge"], now=now, you_hint=hint, data_dir=data_dir,
                         prune=True)
        return {**out, "can_comment": bool(mine.get("can_comment"))}
    try:
        body = _cached(("desk", str(data_dir)), DESK_TTL_S, refresh, make, now)
    except Exception as exc:
        return 200, _refusal_body(exc)
    return 200, {**body, "age_s": max(0, int(now() - body["generated_at"]))}


def api_merge(get_setting, get_secret, body: Any, *, data_dir: Any, transport: Any = None,
              now: Callable[[], float] = time.time) -> "tuple[int, dict]":
    """The desk's merge button: the user's click, his write token (owner lane), pinned to the head SHA the
    desk showed. A pull request with a flow merges only while the flow's human gate waits for him; one
    without a flow only while it is open and still requests his review (``you``). Forgejo's branch
    protection and required checks apply either way."""
    body = body if isinstance(body, dict) else {}
    acc = _mod("access")
    try:
        repo = acc.parse_repo(body.get("repo"))
        number = body.get("number")
        number = int(number) if isinstance(number, (int, str)) and not isinstance(number, bool) else 0
        sha = str(body.get("head_sha") or "")
    except (acc.Refusal, ValueError):
        repo, number, sha = "", 0, ""
    if not repo or number <= 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return _bad("repo (owner/repo), number and the 40-digit head_sha are required.")
    try:
        access = acc.load_access(get_setting, get_secret, transport=transport, lane="owner")
        writer = access.owner_writer()  # refuses with the dark sentence when the field is empty
        if not owner_can_merge(get_setting, get_secret, transport=transport, now=now):
            raise acc.Refusal("dark", _mod("capabilities").dark_sentence("owner_merge"), "owner_token")
        st = _mod("flows.state").FlowStore(data_dir).load_pr(repo, number) if data_dir else None
        mine = tab_of(st, you=access.settings.you) == "waiting" if st else False
        if st and not st.get("closed") and not st.get("merged") and not mine:
            return 409, {"ok": False, "error": "not_waiting",
                         "message": f"{repo}#{number} does not wait for your merge. Nothing was merged."}
        flow_waiting = mine
        pr = writer.get(f"/repos/{repo}/pulls/{number}").data or {}
        head = (pr.get("head") or {}).get("sha") or ""
        if pr.get("merged") or pr.get("state") != "open":
            return 409, {"ok": False, "error": "not_open", "message": f"{repo}#{number} is no longer open."}
        if not flow_waiting:
            you = str(access.settings.you or "").lower()
            asked = {str((u or {}).get("login") or "").lower() for u in pr.get("requested_reviewers") or []}
            if not you or you not in asked:
                return 409, {"ok": False, "error": "not_requested", "message": f"{repo}#{number} has no flow and does "
                             "not request your review, so the desk does not merge it. Nothing was merged."}
        if head != sha or (flow_waiting and st.get("head_sha") != sha):
            return 409, {"ok": False, "error": "head_moved", "message": f"The head moved to {head[:12]} after the desk "
                         "showed it; " + ("the flow checks the new head first." if flow_waiting else "reload the desk and look at the new commits first.") + " Nothing was merged."}
        writer.post(f"/repos/{repo}/pulls/{number}/merge", {"Do": "merge", "head_commit_id": sha})
        after = writer.get(f"/repos/{repo}/pulls/{number}").data or {}
    except acc.Refusal as exc:
        return 403, _refusal_body(exc)
    except Exception as exc:  # Forgejo answered with an error: say what it said
        return 502, {"ok": False, "error": "forge_error", "message": getattr(exc, "message", str(exc))}
    merged = bool(after.get("merged"))
    clear_cache()
    note = _logged(data_dir, {"at": int(now()), "action": "merge", "repo": repo, "number": number, "head_sha": sha,
                              "merged": merged, "merge_sha": after.get("merge_commit_sha"), "lane": "owner"})
    return 200, {"ok": merged, "merged": merged, "merge_sha": after.get("merge_commit_sha"),
                 "message": (f"Merged {repo}#{number} at {sha[:12]} with your account." if merged else
                             "Forgejo accepted the request but the pull request is not merged. Open it in Forgejo."),
                 **note}


MAX_COMMENT_BODY = 10_000


def api_comment(get_setting, get_secret, body: Any, *, data_dir: Any, transport: Any = None,
                now: Callable[[], float] = time.time) -> "tuple[int, dict]":
    """The desk's Comment button: his words, posted with his write token (owner lane), on his click, as him.
    Hermes' approval setting and the preview switch do not apply (like Merge). No footer. The answer says
    "posted" only after Forgejo reads the comment back with the same body."""
    body = body if isinstance(body, dict) else {}
    acc = _mod("access")
    try:
        repo, number = _pr_of(body)
        text = body.get("body")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            raise ValueError("Write a comment first.")
        if len(text) > MAX_COMMENT_BODY:
            raise ValueError(f"A comment holds at most {MAX_COMMENT_BODY:,} characters.")
    except ValueError as exc:
        return _bad(str(exc))
    try:
        mine = _write_token_facts(get_setting, get_secret, transport=transport, now=now)
        if not mine.get("can_comment"):  # before owner_writer: its own refusal names the Merge button
            raise acc.Refusal("dark", _mod("capabilities").dark_sentence("owner_comment"), "owner_token")
        writer = acc.load_access(get_setting, get_secret, transport=transport, lane="owner").owner_writer()
        pr = writer.get(f"/repos/{repo}/pulls/{number}").data or {}
        if pr.get("merged") or pr.get("state") != "open":
            return 409, {"ok": False, "error": "not_open", "message": f"{repo}#{number} is no longer open. Nothing was posted."}
        sent = writer.post(f"/repos/{repo}/issues/{number}/comments", {"body": text}).data or {}
        back = writer.get(f"/repos/{repo}/issues/comments/{int(sent.get('id') or 0)}").data or {}
    except acc.Refusal as exc:
        return 403, _refusal_body(exc)
    except Exception as exc:  # Forgejo answered with an error: say what it said
        return 502, {"ok": False, "error": "forge_error", "message": getattr(exc, "message", str(exc))}
    who = (back.get("user") or {}).get("login") or "you"
    ok = str(back.get("body") or "").strip() == text
    clear_desk_cache()
    note = _logged(data_dir, {"at": int(now()), "action": "comment", "repo": repo, "number": number,
                              "id": sent.get("id"), "chars": len(text), "lane": "owner", "read_back": ok})
    return 200, {"ok": ok, "id": sent.get("id"), "url": back.get("html_url") or sent.get("html_url"),
                 "message": (f"Comment posted as @{who}." if ok else
                             "Forgejo took the comment but reads back a different text. Open it in Forgejo."), **note}


def api_reload_tokens(*, data_dir: Any = None, exit_later: Optional[Callable[[], None]] = None,
                      now: Callable[[], float] = time.time) -> "tuple[int, dict]":
    """The settings page's "Load it now": ends this profile's plugin host child after the answer is sent. Hermes
    starts a new one on the next request, which reads the profile's .env again. Refused outside a plugin host
    child: there the routes run inside Hermes' dashboard itself, which must not end."""
    if not in_plugin_host():
        return 409, {"ok": False, "error": "not_here",
                     "message": "This page runs inside Hermes' dashboard. Restart the dashboard to load a new token."}
    note = _logged(data_dir, {"at": int(now()), "action": "reload_tokens", "pid": os.getpid()})
    if exit_later is None:
        timer = threading.Timer(0.5, os._exit, (0,))
        timer.daemon = True
        exit_later = timer.start
    exit_later()
    return 200, {"ok": True, "message": "Hermes reloads this profile's plugins now.", **note}


def _bad(message: str) -> "tuple[int, dict]":
    return 400, {"ok": False, "error": "bad_request", "message": message}


def _pr_of(body: Any) -> "tuple[str, int]":
    """``(repo, number)`` from a route body; ``ValueError`` with the sentence to show."""
    acc = _mod("access")
    try:
        repo = acc.parse_repo(body.get("repo"))
    except acc.Refusal as exc:
        raise ValueError(str(getattr(exc, "message", "") or "repo must be owner/name.")) from None
    number = body.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("number must be a pull request number.")
    return repo, number


def api_park(body: Any, *, data_dir: Any, now: Callable[[], float] = time.time) -> "tuple[int, dict]":
    """The desk's Park button: his click sets one pull request aside under Waiting for me, with an optional
    note only he sees. Nothing goes to Forgejo. ``seen`` is the row as the desk showed it; the desk compares
    later reads against it (``parking.wake``). The answer carries no sidebar label: the next desk read sets it."""
    body = body if isinstance(body, dict) else {}
    try:
        repo, number = _pr_of(body)
        note = body.get("note", "")
        if not isinstance(note, str):
            raise ValueError("note must be text.")
        if not isinstance(body.get("wake_commits"), bool):
            raise ValueError("wake_commits must be true or false.")
        if not isinstance(body.get("seen"), dict):
            raise ValueError("seen must be the row's head_sha, comment_at and draft.")
        if not data_dir:
            raise ValueError("The plugin's data folder is missing.")
        entry, logged = _mod("parking").park(data_dir, repo, number, note=note, wake_commits=body["wake_commits"],
                                             seen=body["seen"], now=now)
    except ValueError as exc:
        return _bad(str(exc))
    clear_desk_cache()
    return 200, {"ok": True, "parked": entry, **logged}


def api_unpark(body: Any, *, data_dir: Any, now: Callable[[], float] = time.time) -> "tuple[int, dict]":
    """Unpark and Undo: the pull request is back in Waiting for me. Unparking one that is not parked is fine."""
    body = body if isinstance(body, dict) else {}
    try:
        repo, number = _pr_of(body)
        if not data_dir:
            raise ValueError("The plugin's data folder is missing.")
    except ValueError as exc:
        return _bad(str(exc))
    removed, logged = _mod("parking").unpark(data_dir, repo, number, now=now)
    clear_desk_cache()
    return 200, {"ok": True, "removed": removed, **logged}


def api_set_setting(body: Any, *, save: Callable[[str, Any], None], get_setting, get_secret, transport: Any = None,
                    data_dir: Any = None, now: Callable[[], float] = time.time) -> "tuple[int, dict]":
    """The settings page's switches and its "Use this login" button: saves one of ``SETTABLE`` on the user's
    click. ``save(key, value)`` writes the profile's plugin setting. ``you`` must be a user Forgejo knows, not
    an organisation; an empty string clears it. Tokens and every other setting stay in Hermes' own form."""
    body = body if isinstance(body, dict) else {}
    key, value = body.get("key"), body.get("value")
    if key not in SETTABLE:
        return _bad("This page saves only " + ", ".join(SETTABLE[:-1]) + " and " + SETTABLE[-1] + ".")
    if key != "you":
        if not isinstance(value, bool):
            return _bad(f"{key} must be true or false.")
    else:
        if not isinstance(value, str):
            return _bad("Enter a Forgejo user name.")
        value = value.strip().lstrip("@")
        if value:
            if not _mod("settings")._USER_RE.match(value):
                return _bad(f"{_cap(value, 60)!r} is not a Forgejo user name.")
            acc = _mod("access")
            try:
                client = acc.load_access(get_setting, get_secret, transport=transport, lane="agent").reader()
            except acc.Refusal as exc:  # no address or agent token yet: the name cannot be checked
                return 400, _refusal_body(exc)
            try:
                found = client.get(f"/users/{value}").data or {}
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    return _bad(f"Forgejo has no user {value}.")
                return 502, {"ok": False, "error": "forge_error", "message": getattr(exc, "message", str(exc))}
            kind = _account_kind(client, value)
            if kind == "org":
                return _bad(f"{value} is an organisation. Enter your own user name.")
            if kind is None:
                return 502, {"ok": False, "error": "forge_error",
                             "message": f"Forgejo did not say whether {value} is a user or an organisation."}
            value = str(found.get("login") or value)
    try:
        save(key, value)
    except PermissionError as exc:
        return 403, {"ok": False, "error": "not_allowed", "message": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": "save_failed",
                     "message": f"The setting was not saved ({type(exc).__name__})."}
    clear_cache()
    return 200, {"ok": True, "key": key, "value": value,
                 **_logged(data_dir, {"at": int(now()), "action": "setting", "key": key, "value": value})}


def _logged(data_dir: Any, record: dict) -> dict:
    """Append to the action log. The action itself already happened, so a log that cannot be written is a
    note on the answer, never a failed answer."""
    try:
        _log(data_dir, record)
    except OSError as exc:
        return {"note": f"Done, but the action log could not be written ({type(exc).__name__})."}
    return {}


def _log(data_dir: Any, record: dict) -> None:
    if not data_dir:
        return
    path = Path(data_dir) / "desk-actions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def reviewer_command(source: str) -> str:
    src = source or "<your profile>"
    return f"hermes profile create {REVIEWER} --clone-from {src} --no-alias"


def api_create_reviewer(body: Any, *, source: str, home: Any = None, run: Callable = subprocess.run,
                        hermes: Optional[str] = None) -> "tuple[int, dict]":
    """On the user's click in the desk's settings: clone his current profile as a reviewer
    profile, give it a reviewer SOUL.md, and strip every Forgebrücke token from its .env (a reviewer writes
    its verdict file only). When the route cannot run Hermes, answer with the exact command."""
    profile = _mod("profile")
    existing = profile.list_profiles(home) or []
    command = reviewer_command(source)
    if REVIEWER in existing:
        return 200, {"ok": True, "created": False, "name": REVIEWER, "message": f"{REVIEWER} already exists."}
    exe = hermes if hermes is not None else shutil.which("hermes")
    if not exe or not source:
        return 200, {"ok": False, "error": "run_it_yourself", "command": command,
                     "message": "Run this command in a terminal on the Hermes host. Then pick the profile in "
                                "Hermes' settings."}
    def failed(message: str) -> "tuple[int, dict]":
        clear_cache()
        return 200, {"ok": False, "error": "create_failed", "command": command, "message": message}
    try:
        done = run([exe, "profile", "create", REVIEWER, "--clone-from", source, "--no-alias",
                    "--description", "Forgebrücke reviewer: writes verdict files only"],
                   capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return failed("Hermes did not finish within 120 seconds. Run the command in a terminal on the Hermes host.")
    except OSError as exc:
        return failed(f"Hermes could not be started ({type(exc).__name__}). Run the command in a terminal on the "
                      "Hermes host.")
    if getattr(done, "returncode", 1) != 0:
        return failed(_cap(getattr(done, "stderr", "") or getattr(done, "stdout", ""), 400))
    root = profile.profiles_root(home)
    target = Path(root) / REVIEWER if root else None
    try:
        if target is not None and target.is_dir():
            (target / "SOUL.md").write_text(REVIEWER_SOUL, encoding="utf-8")
            env = target / ".env"
            if env.exists():
                kept = [ln for ln in env.read_text(encoding="utf-8").splitlines()
                        if not ln.strip().startswith("FORGEBRUECKE_")]
                env.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except OSError as exc:
        return failed(f"{REVIEWER} exists now, but its SOUL.md or .env could not be written "
                      f"({type(exc).__name__}). Check both files before a flow uses it.")
    clear_cache()
    return 200, {"ok": True, "created": True, "name": REVIEWER,
                 "message": f"Created {REVIEWER}. Next, pick it as the profile that runs agent reviews in Hermes' "
                            "settings."}
