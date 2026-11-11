"""forgejo_doctor: what Forgejo is, what each token is and may do, and which features that makes available.

Report shape (``run_doctor``; the tool returns it with ``detail: true``):

    {"ok", "instance": {url, version, product, supported, reason, api_operations, tested_with},
     "tokens": [{role: bot|owner, env, set, identity, is_admin, scopes{scope: yes|no|unknown}, scope_notes,
                 repos{visible, admin, write, read, sample[]}, protection, error}],
     "features": [{id, label, status: available|off|dark, reason}],
     "flows": {status, detail}, "settings": {..., warnings[]}, "token_warnings": [],
     "next_action": {id: set_url | set_token | fix_token | fix_owner_token | set_you | dry_run | none, text},
     "next_step": the same sentence plus where to do it, "you": {set, suggestion, source?}}

``you.suggestion`` is empty once ``you`` is set. ``you.source`` is ``write_token`` when the suggestion is the
login of the user's own write token (certain). Without it the suggestion is the person who owns most of the
repos Hermes' account sees (a guess, and the one the settings page offers).

The tool's default answer is ``compact(report)``, in the agent's words (``dark`` is ``not_available``, the
roles are ``hermes`` and ``user``):

    {"ok", "forge": {url, version, supported, error?}, "tokens": [{role, set, login?, repos?, error?}],
     "next_action", "you": {suggestion, source?} while unset, "works": [label], "off": {label: reason},
     "not_available": {reason: [label]}, "warnings": [], "more"}

Scope detection has no side effects: reads probe a GET; write scopes are probed with requests Forgejo
rejects after its scope check (an empty-body POST answers 422, a write to a thread or commit
that does not exist answers 404), measured on Forgejo 15.0.11. A missing scope answers 403 naming
it. Nothing here prints or returns a token.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

YES, NO, UNKNOWN = "yes", "no", "unknown"
REPO_SAMPLE = 20


def _mod(name: str):
    pkg = __package__.rsplit(".", 1)[0] if __package__ and "." in __package__ else __package__
    return importlib.import_module(f".{name}", pkg)


@dataclass(frozen=True)
class Probe:
    scope: str
    method: str
    path: str  # may contain {repo}
    params: Optional[dict] = None
    body: Any = None
    needs_writable_repo: bool = False


PROBES = (
    Probe("read:user", "GET", "/user"),
    Probe("read:repository", "GET", "/user/repos", {"limit": 1}),
    Probe("read:issue", "GET", "/repos/issues/search", {"limit": 1}),
    Probe("read:notification", "GET", "/notifications", {"limit": 1}),
    Probe("read:organization", "GET", "/user/orgs", {"limit": 1}),
    Probe("write:issue", "POST", "/repos/{repo}/issues", body={}, needs_writable_repo=True),
    Probe("write:repository", "POST", "/repos/{repo}/pulls", body={}, needs_writable_repo=True),
    Probe("write:notification", "PATCH", "/notifications/threads/999999999"),
    Probe("write:user", "POST", "/user/repos", body={}),  # 422 "[Name]: Required" after the scope check
)


def probe_scope(client, probe: Probe, writable_repo: Optional[str]) -> "tuple[str, str]":
    """``(yes|no|unknown, note)`` for one scope."""
    ForgejoError = _mod("client").ForgejoError
    if probe.needs_writable_repo and not writable_repo:
        return UNKNOWN, "no repo with push access to test against"
    path = probe.path.format(repo=writable_repo or "")
    try:
        client.request(probe.method, path, params=probe.params, json_body=probe.body)
        return YES, ""
    except ForgejoError as e:
        if e.kind == "missing_scope":
            if probe.scope in e.scopes or not e.scopes:
                return NO, f"Forgejo says the token lacks {', '.join(e.scopes) or probe.scope}"
            return UNKNOWN, f"blocked by another missing scope ({', '.join(e.scopes)})"
        if e.status in (400, 404, 409, 422):
            return YES, ""  # rejected after the scope check, so the scope is there
        if e.kind == "token_rejected":
            return NO, "token rejected"
        return UNKNOWN, e.message


def _repos(n: int) -> str:
    return f"{n} repo" if n == 1 else f"{n} repos"


def _access(repo: dict) -> str:
    p = repo.get("permissions") or {}
    return "admin" if p.get("admin") else "write" if p.get("push") else "read"


def token_report(client, role: str, env: str) -> dict:
    ForgejoError = _mod("client").ForgejoError
    out: dict = {"role": role, "env": env, "set": True, "identity": None, "is_admin": False,
                 "scopes": {}, "scope_notes": {}, "repos": None, "protection": None, "error": None}
    try:
        me = client.get("/user").data or {}
    except ForgejoError as e:
        out["error"] = e.to_dict()
        return out
    out["identity"] = me.get("login")
    out["is_admin"] = bool(me.get("is_admin"))
    try:
        page = client.paginate("/user/repos", max_items=200)
        repos = [{"repo": r.get("full_name"), "access": _access(r), "private": bool(r.get("private"))}
                 for r in page.items]
        counts = {lvl: sum(1 for r in repos if r["access"] == lvl) for lvl in ("admin", "write", "read")}
        owners: dict = {}
        for r in page.items:
            login = (r.get("owner") or {}).get("login") or str(r.get("full_name") or "/").split("/")[0]
            owners[login] = owners.get(login, 0) + 1
        out["repos"] = {"visible": page.total if page.total is not None else len(repos), **counts,
                        "listed": len(repos), "has_more": page.has_more, "sample": repos[:REPO_SAMPLE],
                        "owners": owners,
                        "admin_on": [r["repo"] for r in repos if r["access"] == "admin"][:REPO_SAMPLE]}
    except ForgejoError as e:
        repos = []
        out["repos"] = {"visible": 0, "error": e.to_dict()}
    writable = next((r["repo"] for r in repos if r["access"] in ("write", "admin")), None)
    for probe in PROBES:
        verdict, note = probe_scope(client, probe, writable)
        out["scopes"][probe.scope] = verdict
        if note:
            out["scope_notes"][probe.scope] = note
    admin_repo = next((r["repo"] for r in repos if r["access"] == "admin"), None)
    if admin_repo is None:
        out["protection"] = {"readable": False, "reason": "this account is admin on none of the repos it sees"}
    else:
        try:
            rules = client.get(f"/repos/{admin_repo}/branch_protections").data or []
            out["protection"] = {"readable": True, "checked_on": admin_repo, "rules": len(rules)}
        except ForgejoError as e:
            out["protection"] = {"readable": False, "checked_on": admin_repo, "reason": e.message}
    return out


OPS = {
    "read": ("GET /repos/issues/search", "GET /repos/{owner}/{repo}/pulls/{index}"),
    "mentions": ("GET /notifications",),
    "writes": ("POST /repos/{owner}/{repo}/statuses/{sha}",),
    "flows": ("POST /repos/{owner}/{repo}/statuses/{sha}",),
    "import": ("POST /repos/migrate",),
}


def features(reports: dict, settings, caps, channel: Optional[dict] = None) -> "tuple[list, list, dict]":
    """``(features, warnings, capabilities)`` from the capability registry (capabilities.py), the one place
    that says what a feature needs."""
    registry = _mod("capabilities")
    found, warnings = registry.probe_caps(reports)

    def ops_missing(fid: str) -> list:
        if caps is None or not caps.operations:
            return []  # unknown: do not hide features for lack of a swagger
        return [o for o in OPS.get(fid, ()) if o not in caps.operations]

    rep = reports.get("bot") or {}
    repos = rep.get("repos") or {}
    n = repos.get("visible", 0)
    notes = {"read": f"Hermes reads as its own account ({_repos(n)}: write {repos.get('write', 0)}, read "
                     f"{repos.get('read', 0)}, admin {repos.get('admin', 0)}). Mention turns and flows read only "
                     "their own repo."}
    if "admin:repo" in found:
        admin_on = repos.get("admin_on") or []
        where = f"on {_repos(repos.get('admin', len(admin_on)))} where Hermes' account is admin" + \
                (f" ({', '.join(admin_on[:5])})" if admin_on else "")
        for fid in ("protection_view", "presets", "audit"):
            notes[fid] = f"only {where}; not available on the others"
    tools = ", ".join((channel or {}).get("toolsets") or []) or "as configured"
    notes["mentions"] = ("Hermes answers people with write access, in repos where its account may write and no "
                         f"rule switches mentions off. Mention turns get: {tools}.")
    out = registry.resolve(found, settings, channel=channel, ops_missing=ops_missing, notes=notes)
    return out, warnings, found


# The sentences of the one thing to do next. The settings page shows the same ones beside a button.
NEXT = {
    "set_url": "Enter your Forgejo address.",
    "set_token": "Add the token of Hermes' account.",
    "set_you": "Which Forgejo user are you?",
    "dry_run": "Preview only: Hermes shows each write and sends none.",
    "none": "Ready.",
}


def _next(report: dict, action: str, text: str = "", hint: str = "") -> dict:
    """Set the report's next action; ``hint`` reaches only ``next_step``, which has no button beside it."""
    report["next_action"] = {"id": action, "text": text or NEXT[action]}
    report["next_step"] = report["next_action"]["text"] + hint
    return report


def run_doctor(settings, creds, *, client_factory: Callable[[str, str], Any], channel: Optional[dict] = None) -> dict:
    """Never raises: every failure becomes part of the report."""
    client_mod = _mod("client")
    problems = list(getattr(creds, "problems", list)())
    report: dict = {"ok": False, "instance": {"url": settings.url or None}, "tokens": [],
                    "features": [], "settings": {**settings.public(), "warnings": list(settings.warnings)},
                    "flows": {"status": "see_tool", "detail": "forgejo_flows action=get names each repo's flows, "
                                                              "where they come from and the last engine tick."},
                    "token_warnings": problems, "next_step": ""}
    env = _mod("settings")
    if not settings.url:
        return _next(report, "set_url")
    caps = None
    if not creds.token:
        report["tokens"].append({"role": "bot", "env": env.TOKEN_ENV, "set": False})
    try:
        probe_client = client_factory(settings.url, creds.token or creds.owner_token)
        caps = probe_client.capabilities(refresh=True)
        report["instance"].update(caps.to_dict())
    except client_mod.ForgejoError as e:
        report["instance"]["error"] = e.to_dict()
        if e.status is None and e.kind == "unreachable":  # no answer at all: the address, not the forge's state
            return _next(report, "set_url", f"Hermes cannot reach {settings.url}. Check your Forgejo address.")
        return _next(report, "set_url", f"Check your Forgejo address: {e.message}")
    bot = owner = None
    if creds.token:
        bot = token_report(client_factory(settings.url, creds.token), "bot", env.TOKEN_ENV)
        report["tokens"].append(bot)
    if creds.owner_token:
        owner = token_report(client_factory(settings.url, creds.owner_token), "owner", env.OWNER_TOKEN_ENV)
        report["tokens"].append(owner)
    else:
        report["tokens"].append({"role": "owner", "env": env.OWNER_TOKEN_ENV, "set": False,
                                 "note": "Optional. Only for the desk's Merge and Comment buttons."})
    reports = {k: v for k, v in (("bot", bot), ("owner", owner)) if v is not None}
    report["features"], warnings, found = features(reports, settings, caps, channel)
    report["token_warnings"] = problems + warnings
    if (channel or {}).get("approvals_off"):
        report["settings"]["warnings"].append("Hermes approvals are off in this profile, so Forgebrücke's writes "
                                              "go out unasked.")
    report["capabilities"] = found
    report["setups"] = _mod("capabilities").setups_report()
    agent = (bot or {}).get("identity") or ""
    owners = ((bot or {}).get("repos") or {}).get("owners") or {}
    yours = reports.get("owner") or {}  # the registry marks a write token of the agent's own account as an error
    if yours.get("identity") and not yours.get("error"):
        report["you"] = {"set": settings.you, "suggestion": yours["identity"], "source": "write_token"}
    elif settings.you or not creds.token:
        report["you"] = {"set": settings.you, "suggestion": ""}
    else:  # the settings page asks the same function, so both name the same person
        guess = _mod("desk").suggest_login(client_factory(settings.url, creds.token), owners, agent)
        report["you"] = {"set": settings.you, "suggestion": guess}
    # The settings page asks the same function, so the agent and the page name one next step.
    action = _mod("desk").next_action(report, settings)
    report["ok"] = action["id"] in ("set_you", "dry_run", "none")
    hint = ""
    if action["id"] == "set_you":
        guess = report["you"]["suggestion"]
        hint = (f' Click "Use @{guess}" on the Forgebrücke page.' if guess
                else " Enter your Forgejo user name in Hermes' settings.")
    return _next(report, action["id"], action["text"], hint=hint)


ROLE_WORD = {"bot": "hermes", "owner": "user"}
TOKEN_NAME = {"bot": "Hermes' token", "owner": "Your write token"}
STATUS_WORD = {"dark": "not available"}


def _token_state(token: dict) -> dict:
    out = {"role": ROLE_WORD.get(token.get("role"), token.get("role")), "set": bool(token.get("set"))}
    if token.get("identity"):
        out["login"] = token["identity"]
        repos = token.get("repos") or {}
        out["repos"] = {k: repos.get(k, 0) for k in ("visible", "admin", "write", "read")}
    if token.get("error"):
        out["error"] = token["error"].get("message")
    return out


def compact(report: dict) -> dict:
    """What the tool answers by default: the state, the next action, and why a feature is not available,
    one entry per reason. The report itself is ``detail: true``."""
    inst = report.get("instance") or {}
    feats = report.get("features") or []
    you = report.get("you") or {}
    forge = {k: inst[k] for k in ("url", "version", "supported") if inst.get(k) is not None}
    if inst.get("error"):
        forge["error"] = inst["error"].get("message")
    not_available: dict = {}
    for f in feats:
        if f.get("status") == "dark":
            not_available.setdefault(f.get("reason"), []).append(f.get("label"))
    out = {"ok": bool(report.get("ok")), "forge": forge,
           "tokens": [_token_state(t) for t in report.get("tokens") or []],
           "next_action": report.get("next_action"),
           "you": None if you.get("set") else {k: v for k, v in you.items() if v},
           "works": [f.get("label") for f in feats if f.get("status") == "available"],
           "off": {f.get("label"): f.get("reason") for f in feats if f.get("status") == "off"},
           "not_available": not_available,
           "warnings": [*(report.get("token_warnings") or []), *((report.get("settings") or {}).get("warnings") or [])],
           "more": "detail=true adds each token's scopes and repos."}
    return {k: v for k, v in out.items() if v not in ([], {}, None)}


def format_text(report: dict) -> str:
    """The report as plain lines for a person at a terminal: Forgejo, the tokens, each feature, the next step."""
    inst = report.get("instance") or {}
    lines = [f"Forgebrücke doctor: {'OK' if report.get('ok') else 'needs attention'}",
             f"  Forgejo: {inst.get('url') or '(no address set)'}  {inst.get('version') or ''}".rstrip()]
    if inst.get("reason") and not inst.get("supported"):
        lines.append(f"    {inst['reason']}")
    if inst.get("error"):
        lines.append(f"    error: {inst['error']['message']}")
    for t in report.get("tokens") or []:
        head = f"  {TOKEN_NAME.get(t['role'], t['role'])}: "
        if not t.get("set"):
            lines.append(head + "not set." + (f" {t['note']}" if t.get("note") else ""))
            continue
        if t.get("error"):
            lines.append(head + f"error: {t['error']['message']}")
            continue
        r = t.get("repos") or {}
        lines.append(head + f"{t.get('identity')}{' (site admin)' if t.get('is_admin') else ''}, "
                     f"{_repos(r.get('visible', 0))} (admin {r.get('admin', 0)}, write {r.get('write', 0)}, read {r.get('read', 0)})")
        lines.append("    scopes: " + ", ".join(f"{s}={v}" for s, v in t["scopes"].items()))
    for w in [*(report.get("token_warnings") or []), *((report.get("settings") or {}).get("warnings") or [])]:
        lines.append(f"  warning: {w}")
    if report.get("features"):
        lines.append("  features:")
    for f in report.get("features") or []:
        lines.append(f"    [{STATUS_WORD.get(f['status'], f['status'])}] {f['label']}: {f['reason']}")
    lines.append(f"  next: {report.get('next_step')}")
    return "\n".join(lines)
