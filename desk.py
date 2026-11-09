"""M7: the Merge desk's backend. Pure functions over the flow store and the forge; the FastAPI adapter is
``dashboard/plugin_api.py``. The Desktop pane reads only these, through ``ctx.rest``, so no token ever
sits on the client.

Shapes (what the pane renders):

    desk     {ok, label, counts{waiting, running, blocked}, rows[Row], repos[RepoCard], engine{...},
              can_merge, you, waiting_note{message, suggestion, settings_href} | null, notes[],
              forge_error?, generated_at}
    Row      {id, tab, repo, number, title, url, author, base, head_ref, head_sha, flow (null: no flow),
              reason (null for a flow row; "review requested" for a pull request that requests your review),
              gates[{key, type, segment, state, detail}], checks{state, statuses[...]} | null,
              last_comment{author, at, body} | null, waiting_on, can_merge, updated_at}
    RepoCard {repo, right (admin | write | read), url, collaboration_url, private, flows[], mentions, admin,
              open_rows}: every repo the agent account sees (D27: its Forgejo right is the only permission
              layer; the collaboration page is where the user changes it)
    settings {ok, forge_url, forge_error, tokens[Token], features[...], token_warnings[], next_step, profiles[],
              you, you_suggestion, default_review_profile, reviewer{exists, name, command}, settings_href}
    Token    one row of ``capabilities.TOKENS`` (role, env, label, name, required, account, mint[], unlocks,
             never, risk?) plus what the probe found: set, login, scopes{} | null, error | null, is_admin, and
             renamed_from (the pre-0.0.12 name still holding a value while the new one is empty) | null

Waiting for me (D28) = flow rows whose human gate waits for you, plus open pull requests whose
``requested_reviewers`` contain the ``you`` setting: one issue search for open pull requests, then one pulls
list per repo that has one (the search's items carry no reviewers; measured on Forgejo 15). Without ``you``
the desk says so (``waiting_note``) and lists flow rows only.

Lanes (M4b): the desk reads in the agent lane, with the agent token, so it sees exactly what Hermes' Forgejo
account sees (D29). The merge button is the owner lane: his write token, on his click, and only when the
registry says ``owner_merge`` is available and a flow's human gate waits; every other row links to Forgejo.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

TABS = ("waiting", "running", "blocked")
DESK_TTL_S = 15
SETTINGS_TTL_S = 60
MAX_COMMENT = 280
REVIEWER = "forgebruecke-reviewer"
SETTINGS_HREF = "/settings?tab=plugins&agent=forgebruecke"
REVIEWER_SOUL = """# Forgebrücke reviewer

You review one pull request when Forgebrücke asks you to. Read the prompt and the diff it gives you,
decide pass or fail, and write the verdict file the prompt names. Nothing else: no forge writes, no
pushes, no comments, no other files. If you cannot decide, write fail with the reason.
"""

_SEGMENT = {"passed": "done", "skipped": "done", "running": "running", "pending": "pending",
            "waiting": "waiting", "failed": "failed", "error": "failed", "blocked": "failed"}
_cache: dict = {}
_lock = threading.Lock()


def _mod(name: str):
    return importlib.import_module(f"{__package__}.{name}")


def nav_label(waiting: int) -> str:
    """The sidebar row's label (N23): the count of pull requests waiting for the user."""
    if waiting <= 0:
        return "Forgejo"
    return f"Forgejo · {waiting if waiting < 100 else '99+'}"


def segment(state: str) -> str:
    return _SEGMENT.get(state, "pending")


def tab_of(st: dict) -> Optional[str]:
    """Which desk tab a PR's flow state belongs to; None for a closed or merged PR."""
    if st.get("closed") or st.get("merged"):
        return None
    states = [g.get("state") for g in st.get("gates") or []]
    if any(segment(s) == "failed" for s in states):
        return "blocked"
    if "waiting" in states:
        return "waiting"
    return "running"


def _cap(text: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _checks(client, repo: str, sha: str) -> Optional[dict]:
    try:
        combined = client.get(f"/repos/{repo}/commits/{sha}/status").data or {}
    except Exception:
        return None
    return _mod("tools.read").compact_status(combined)


def _last_comment(client, repo: str, number: int) -> Optional[dict]:
    try:
        first = client.get(f"/repos/{repo}/issues/{number}/comments", {"limit": 1, "page": 1})
        total = int((first.headers or {}).get("x-total-count") or 0) if hasattr(first, "headers") else 0
        items = first.data or []
        if total > 1:
            items = client.get(f"/repos/{repo}/issues/{number}/comments", {"limit": 1, "page": total}).data or []
    except Exception:
        return None
    if not items:
        return None
    c = items[-1]
    return {"author": (c.get("user") or {}).get("login"), "at": c.get("created_at"),
            "body": _cap(plain_text(c.get("body")), MAX_COMMENT), "url": c.get("html_url")}


_FOOTERS = ("Kept up to date by Forgebrücke", "Posted by Hermes through Forgebrücke")


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


def row(st: dict, client: Any, *, can_merge: bool) -> Optional[dict]:
    tab = tab_of(st)
    if tab is None:
        return None
    repo, n, sha = st["repo"], int(st["pr"]), st.get("head_sha") or ""
    gates = [{"key": g.get("gate_key"), "type": g.get("type"), "state": g.get("state"),
              "segment": segment(g.get("state")), "detail": _cap(g.get("detail"), 200)}
             for g in st.get("gates") or []]
    human = next((g for g in st.get("gates") or [] if g.get("type") == "human"), None)
    return {"id": f"{repo}#{n}", "tab": tab, "repo": repo, "number": n, "title": st.get("title") or f"PR #{n}",
            "url": st.get("url") or "", "author": st.get("author"), "base": st.get("base"),
            "head_ref": st.get("head_ref"), "head_sha": sha, "flow": st.get("flow"), "reason": None, "gates": gates,
            "checks": _checks(client, repo, sha) if client is not None and sha else None,
            "last_comment": _last_comment(client, repo, n) if client is not None else None,
            "waiting_on": _waiting_on(human) if tab == "waiting" else None,
            "can_merge": bool(can_merge and tab == "waiting"), "updated_at": st.get("updated_at")}


def repo_cards(access: Any, rows: list) -> list:
    """Every repo the agent account sees, with its Forgejo right and the switches the rules set there."""
    settings, acc = access.settings, _mod("access")
    out = []
    for r in access.repos():
        repo, right = r["repo"], r["right"]
        rule = settings.rule_for(repo)
        out.append({"repo": repo, "right": right, "url": r.get("url") or f"{settings.url.rstrip('/')}/{repo}",
                    "collaboration_url": acc.collaboration_url(settings.url, repo), "private": r.get("private"),
                    "flows": list(rule.flows), "mentions": bool(rule.mentions and right in acc.WRITE_RIGHTS),
                    "admin": right == "admin",
                    "open_rows": sum(1 for x in rows if x.get("flow") and x["repo"].lower() == repo.lower())})
    return out


MAX_REVIEW_REPOS = 30   # pulls lists per desk build
MAX_SEARCH = 200        # open pull requests the search reads
REVIEW_REASON = "review requested"


def _epoch(iso: Any) -> Optional[int]:
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def review_rows(client: Any, you: str, known: set, can_merge: bool = False) -> "tuple[list, list]":
    """Open pull requests whose ``requested_reviewers`` name ``you``, as Waiting rows without a flow, and notes
    for what was cut or unreadable. ``known`` holds the ids of flow rows (a flow row wins). ``can_merge``: his
    write token can merge, so the row offers the Merge button (0.0.15)."""
    ForgejoError = _mod("client").ForgejoError
    found = client.paginate("/repos/issues/search", {"type": "pulls", "state": "open"}, max_items=MAX_SEARCH)
    repos: list = []
    for item in found.items:
        repo = (item.get("repository") or {}).get("full_name")
        if repo and repo not in repos:
            repos.append(repo)
    notes = []
    if found.has_more:
        notes.append(f"Only the {MAX_SEARCH} most recent open pull requests were checked for review requests.")
    if len(repos) > MAX_REVIEW_REPOS:
        notes.append(f"Review requests were checked in {MAX_REVIEW_REPOS} of {len(repos)} repos with open pull requests.")
        repos = repos[:MAX_REVIEW_REPOS]
    rows = []
    for repo in repos:
        try:
            pulls = client.paginate(f"/repos/{repo}/pulls", {"state": "open"}, max_items=100).items
        except ForgejoError as e:
            notes.append(f"{repo}: its open pull requests could not be read ({e.message})")
            continue
        for pr in pulls:
            n = int(pr.get("number") or 0)
            asked = {str((u or {}).get("login") or "").lower() for u in pr.get("requested_reviewers") or []}
            if not n or f"{repo}#{n}" in known or you.lower() not in asked:
                continue
            sha = (pr.get("head") or {}).get("sha") or ""
            rows.append({"id": f"{repo}#{n}", "tab": "waiting", "repo": repo, "number": n,
                         "title": pr.get("title") or f"PR #{n}", "url": pr.get("html_url") or "",
                         "author": (pr.get("user") or {}).get("login"), "base": (pr.get("base") or {}).get("ref"),
                         "head_ref": (pr.get("head") or {}).get("ref"), "head_sha": sha, "flow": None,
                         "reason": REVIEW_REASON, "gates": [],
                         "checks": _checks(client, repo, sha) if sha else None,
                         "last_comment": _last_comment(client, repo, n), "waiting_on": None, "can_merge": bool(can_merge),
                         "updated_at": _epoch(pr.get("updated_at"))})
    return rows, notes


def build_desk(access: Any, store: Any, *, can_merge: bool = False, now: Callable[[], float] = time.time) -> dict:
    forge_error = None
    try:
        client = access.reader()
    except Exception as exc:  # stored state still renders when the forge or a token is missing
        client, forge_error = None, getattr(exc, "message", str(exc))
    rows = [r for r in (row(st, client, can_merge=can_merge) for st in store.all_prs()) if r is not None]
    you = access.settings.you
    notes: list = []
    waiting_note = None
    if client is not None and you:
        try:
            extra, notes = review_rows(client, you, {r["id"] for r in rows}, can_merge=can_merge)
            rows += extra
        except Exception as exc:  # the flow rows still render when the search fails
            notes.append("Pull requests that request your review could not be read: " + getattr(exc, "message", str(exc)))
    elif not you:
        suggestion = ""
        if client is not None:
            try:
                acc = _mod("access")
                suggestion = acc.suggest_you(acc.owner_counts(access.repos()), acc.account_login(client))
            except Exception:
                suggestion = ""
        waiting_note = {"message": "Set your Forgejo login (setting you) to see the pull requests that request your "
                                   "review here. Until then, Waiting for me lists only pull requests whose flow "
                                   "reached you.", "suggestion": suggestion or None, "settings_href": SETTINGS_HREF}
    try:
        repos = repo_cards(access, rows) if client is not None else []
    except Exception as exc:
        repos = []
        notes.append("The repos Hermes' Forgejo account sees could not be read: " + getattr(exc, "message", str(exc)))
    order = {t: i for i, t in enumerate(TABS)}
    rows.sort(key=lambda r: (order[r["tab"]], -(r.get("updated_at") or 0)))
    counts = {t: sum(1 for r in rows if r["tab"] == t) for t in TABS}
    engine = store.engine() or {}
    out = {"ok": True, "label": nav_label(counts["waiting"]), "counts": counts, "rows": rows,
           "repos": repos, "can_merge": bool(can_merge), "you": you or None, "waiting_note": waiting_note,
           "notes": notes,
           "engine": {"at": engine.get("at"), "source": engine.get("source"),
                      "age_s": int(now() - engine["at"]) if engine.get("at") else None,
                      "errors": (engine.get("errors") or [])[:3]},
           "forge_url": access.settings.url, "generated_at": int(now())}
    if forge_error:
        out["forge_error"] = forge_error
    return out


def _refusal_body(exc: Any) -> dict:
    return {"ok": False, "error": getattr(exc, "code", "error"), "message": getattr(exc, "message", str(exc)),
            "setting": getattr(exc, "setting", ""), "settings_href": SETTINGS_HREF}


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


# --- the routes' functions: each returns (http_status, body) ---------------------------------------

def api_settings(get_setting, get_secret, *, transport: Any = None, refresh: bool = False,
                 now: Callable[[], float] = time.time, home: Any = None) -> "tuple[int, dict]":
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
        you = rep.get("you") or {}
        return {"ok": True, "forge_url": instance.get("url"), "forge_error": (instance.get("error") or {}).get("message"),
                "tokens": tokens, "features": rep.get("features") or [],
                "token_warnings": rep.get("token_warnings") or [],
                "next_step": rep.get("next_step"), "profiles": profiles,
                "default_review_profile": default.default_review_profile,
                "you": default.you or None, "you_suggestion": you.get("suggestion") or None,
                "reviewer": {"name": REVIEWER, "exists": REVIEWER in profiles,
                             "command": reviewer_command(profile.current_name(home))},
                "settings_href": SETTINGS_HREF, "generated_at": int(now())}
    try:
        return 200, _cached(("settings",), SETTINGS_TTL_S, refresh, make, now)
    except Exception as exc:
        return 200, _refusal_body(exc)


def _plugin_root():
    return importlib.import_module(__package__)


def owner_can_merge(get_setting, get_secret, *, transport: Any = None, now: Callable[[], float] = time.time) -> bool:
    creds = _mod("settings").load_credentials(get_secret)
    if not creds.owner_token:
        return False  # never probe for a field that is empty
    status, body = api_settings(get_setting, get_secret, transport=transport, now=now)
    feat = next((f for f in body.get("features") or [] if f.get("id") == "owner_merge"), None)
    return bool(feat and feat.get("status") == "available")


def api_desk(get_setting, get_secret, *, data_dir: Any, transport: Any = None, refresh: bool = False,
             now: Callable[[], float] = time.time) -> "tuple[int, dict]":
    if not data_dir:
        return 200, {"ok": False, "error": "no_data_dir", "message": "The plugin's data folder is not available, so "
                     "there is no flow state to show.", "settings_href": SETTINGS_HREF}

    def make() -> dict:
        access = _mod("access").load_access(get_setting, get_secret, transport=transport, lane="agent")
        store = _mod("flows.state").FlowStore(data_dir)
        return build_desk(access, store, can_merge=owner_can_merge(get_setting, get_secret, transport=transport,
                                                                   now=now), now=now)
    try:
        return 200, _cached(("desk", str(data_dir)), DESK_TTL_S, refresh, make, now)
    except Exception as exc:
        return 200, _refusal_body(exc)


def api_merge(get_setting, get_secret, body: Any, *, data_dir: Any, transport: Any = None,
              now: Callable[[], float] = time.time) -> "tuple[int, dict]":
    """The desk's merge button: the user's click, his write token (owner lane), pinned to the head SHA the
    desk showed. A pull request with a flow merges only while the flow's human gate waits for him; one
    without a flow only while it is open and still requests his review (``you``, 0.0.15). Forgejo's branch
    protection and required checks apply either way."""
    body = body if isinstance(body, dict) else {}
    acc = _mod("access")
    try:
        repo = acc.parse_repo(body.get("repo"))
        number = int(body.get("number") or 0)
        sha = str(body.get("head_sha") or "")
        if number <= 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return 400, {"ok": False, "error": "bad_request", "message": "repo, number and the 40-digit head_sha "
                                                                         "are required."}
        access = acc.load_access(get_setting, get_secret, transport=transport, lane="owner")
        writer = access.owner_writer()  # refuses with the dark sentence when the field is empty
        if not owner_can_merge(get_setting, get_secret, transport=transport, now=now):
            raise acc.Refusal("dark", _mod("capabilities").dark_sentence("owner_merge"), "owner_token")
        st = _mod("flows.state").FlowStore(data_dir).load_pr(repo, number) if data_dir else None
        if st and not st.get("closed") and not st.get("merged") and tab_of(st) != "waiting":
            return 409, {"ok": False, "error": "not_waiting", "message": f"{repo}#{number} is not waiting for you: "
                         "its flow has a gate that is not done yet. Nothing was merged."}
        flow_waiting = bool(st) and tab_of(st) == "waiting"
        pr = writer.get(f"/repos/{repo}/pulls/{number}").data or {}
        head = (pr.get("head") or {}).get("sha") or ""
        if pr.get("merged") or pr.get("state") != "open":
            return 409, {"ok": False, "error": "not_open", "message": f"{repo}#{number} is no longer open."}
        if not flow_waiting:
            you = str(access.settings.you or "").lower()
            asked = {str((u or {}).get("login") or "").lower() for u in pr.get("requested_reviewers") or []}
            if not you or you not in asked:
                return 409, {"ok": False, "error": "not_requested", "message": f"{repo}#{number} has no flow and does "
                             f"not request your review ({access.settings.you or 'setting you is empty'}), so the desk "
                             "does not merge it. Nothing was merged."}
        if head != sha or (flow_waiting and st.get("head_sha") != sha):
            return 409, {"ok": False, "error": "head_moved", "message": f"The head moved to {head[:12]} after the desk "
                         "showed it; " + ("the flow checks the new head first." if flow_waiting else "reload the desk and look at the new commits first.") + " Nothing was merged."}
        writer.post(f"/repos/{repo}/pulls/{number}/merge", {"Do": "merge", "head_commit_id": sha})
        after = writer.get(f"/repos/{repo}/pulls/{number}").data or {}
    except acc.Refusal as exc:
        return 403, _refusal_body(exc)
    except Exception as exc:  # a forge error: say what it said
        return 502, {"ok": False, "error": "forge_error", "message": getattr(exc, "message", str(exc))}
    merged = bool(after.get("merged"))
    _log(data_dir, {"at": int(now()), "action": "merge", "repo": repo, "number": number, "head_sha": sha,
                    "merged": merged, "merge_sha": after.get("merge_commit_sha"), "lane": "owner"})
    clear_cache()
    return 200, {"ok": merged, "merged": merged, "merge_sha": after.get("merge_commit_sha"),
                 "message": (f"Merged {repo}#{number} at {sha[:12]} with your account." if merged else
                             "The forge accepted the request but the pull request is not merged; open it in Forgejo.")}


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
    """N20 fallback, on the user's click in the desk's settings: clone his current profile as a reviewer
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
                     "message": "This host cannot run Hermes from the desk. Run the command in a terminal on the "
                                "Hermes host, then pick the profile here."}
    done = run([exe, "profile", "create", REVIEWER, "--clone-from", source, "--no-alias",
                "--description", "Forgebrücke reviewer: writes verdict files only"],
               capture_output=True, text=True, timeout=120)
    if getattr(done, "returncode", 1) != 0:
        return 200, {"ok": False, "error": "create_failed", "command": command,
                     "message": _cap(getattr(done, "stderr", "") or getattr(done, "stdout", ""), 400)}
    root = profile.profiles_root(home)
    target = Path(root) / REVIEWER if root else None
    if target is not None and target.is_dir():
        (target / "SOUL.md").write_text(REVIEWER_SOUL, encoding="utf-8")
        env = target / ".env"
        if env.exists():
            kept = [ln for ln in env.read_text(encoding="utf-8").splitlines()
                    if not ln.strip().startswith("FORGEBRUECKE_")]
            env.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    clear_cache()
    return 200, {"ok": True, "created": True, "name": REVIEWER,
                 "message": f"Created {REVIEWER} from {source}, with a reviewer SOUL.md and no Forgebrücke tokens. "
                            "Set it as the profile that runs agent reviews in the plugin settings."}
