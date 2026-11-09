"""forgejo_doctor: what the forge is, what each token is and may do, and which features that unlocks.

Report shape (``run_doctor``):

    {"ok", "instance": {url, version, product, supported, reason, api_operations, tested_with},
     "tokens": [{role, env, set, identity, is_admin, scopes{scope: yes|no|unknown}, scope_notes,
                 repos{visible, admin, write, read, sample[]}, protection, error}],
     "features": [{id, label, status: available|off|dark, reason}],
     "flows": {status, detail}, "settings": {..., warnings[]}, "next_step"}

Scope detection has no side effects: reads probe a GET; write scopes are probed with requests the
forge rejects after its scope check (an empty-body POST answers 422, a write to a thread or commit
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
                return NO, f"the forge says the token lacks {', '.join(e.scopes) or probe.scope}"
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
    notes = {"read": f"Hermes reads as its own Forgejo account ({_repos(n)} visible: write {repos.get('write', 0)}, "
                     f"read {repos.get('read', 0)}, admin {repos.get('admin', 0)}); mention turns and flows read only "
                     "in their own repo"}
    if "admin:repo" in found:
        admin_on = repos.get("admin_on") or []
        where = f"on {_repos(repos.get('admin', len(admin_on)))} where Hermes' account is admin" + \
                (f" ({', '.join(admin_on[:5])})" if admin_on else "")
        for fid in ("protection_view", "presets", "audit"):
            notes[fid] = f"only {where}; dark on the others"
    tools = ", ".join((channel or {}).get("toolsets") or []) or "as configured"
    notes["mentions"] = (f"Hermes answers mentions by people with write access, in repos where its account may "
                         f"write and no rule switches mentions off; mention turns get: {tools}")
    out = registry.resolve(found, settings, channel=channel, ops_missing=ops_missing, notes=notes)
    return out, warnings, found


def run_doctor(settings, creds, *, client_factory: Callable[[str, str], Any], channel: Optional[dict] = None) -> dict:
    """Never raises: every failure becomes part of the report."""
    client_mod = _mod("client")
    report: dict = {"ok": False, "instance": {"url": settings.url or None}, "tokens": [],
                    "features": [], "settings": {**settings.public(), "warnings": list(settings.warnings)},
                    "flows": {"status": "see_tool", "detail": "forgejo_flows action=get names each repo's flows, "
                                                              "where they come from and the last engine tick."},
                    "next_step": ""}
    env = _mod("settings")
    if not settings.url:
        report["next_step"] = ("Set the address of your Forgejo (setting url, for example "
                               "https://forgejo.example.com) in the plugin settings.")
        return report
    caps = None
    if not creds.token:
        report["tokens"].append({"role": "bot", "env": env.TOKEN_ENV, "set": False})
    try:
        probe_client = client_factory(settings.url, creds.token or creds.owner_token)
        caps = probe_client.capabilities(refresh=True)
        report["instance"].update(caps.to_dict())
    except client_mod.ForgejoError as e:
        report["instance"]["error"] = e.to_dict()
        report["next_step"] = f"Check the address {settings.url}: {e.message}"
        return report
    bot = owner = None
    if creds.token:
        bot = token_report(client_factory(settings.url, creds.token), "bot", env.TOKEN_ENV)
        report["tokens"].append(bot)
    if creds.owner_token:
        owner = token_report(client_factory(settings.url, creds.owner_token), "owner", env.OWNER_TOKEN_ENV)
        report["tokens"].append(owner)
    else:
        report["tokens"].append({"role": "owner", "env": env.OWNER_TOKEN_ENV, "set": False,
                                 "note": "optional: a write token of your own account, only for the desk's Merge button"})
    reports = {k: v for k, v in (("bot", bot), ("owner", owner)) if v is not None}
    report["features"], report["token_warnings"], found = features(reports, settings, caps, channel)
    report["capabilities"] = found
    report["setups"] = _mod("capabilities").setups_report()
    agent = (bot or {}).get("identity") or ""
    owners = ((bot or {}).get("repos") or {}).get("owners") or {}
    report["you"] = {"set": settings.you, "suggestion": _mod("access").suggest_you(owners, agent)}
    if not caps.supported:
        report["next_step"] = caps.reason
    elif not creds.token:
        report["next_step"] = ("Store the token of Hermes' own Forgejo account as FORGEBRUECKE_BOT_ACCOUNT_TOKEN in "
                               "the plugin settings (a token created while signed in as that account).")
    elif bot and bot.get("error"):
        report["next_step"] = f"Fix the agent account token: {bot['error']['message']}"
    elif owner and owner.get("error"):
        report["next_step"] = f"Fix the write token of your account: {owner['error']['message']}"
    else:
        report["ok"] = True
        dark = [f["label"] for f in report["features"] if f["status"] == "dark"]
        report["next_step"] = ("Ready. " + (f"Not available with this setup: {', '.join(dark)}." if dark else
                                            "Every feature this build has is available."))
        if not settings.you:
            hint = report["you"]["suggestion"]
            report["next_step"] += (" Set your Forgejo login (setting you" + (f", probably {hint}" if hint else "")
                                    + ") so the desk lists the pull requests that request your review.")
    return report


def format_text(report: dict) -> str:
    """The same report as plain lines, for the CLI."""
    inst = report.get("instance") or {}
    lines = [f"Forgebrücke doctor: {'OK' if report.get('ok') else 'needs attention'}",
             f"  forge: {inst.get('url') or '(no url set)'}  {inst.get('version') or ''}  "
             f"{'supported' if inst.get('supported') else ''}".rstrip()]
    if inst.get("api_operations") is not None:
        lines.append(f"    API: {inst['api_operations']} operations in the instance's swagger")
    if inst.get("reason"):
        lines.append(f"    {inst['reason']}")
    if inst.get("error"):
        lines.append(f"    error: {inst['error']['message']}")
    for t in report.get("tokens") or []:
        head = f"  token {t['role']} ({t['env']}): "
        if not t.get("set"):
            lines.append(head + "not set" + (f", {t['note']}" if t.get("note") else ""))
            continue
        if t.get("error"):
            lines.append(head + f"error: {t['error']['message']}")
            continue
        r = t.get("repos") or {}
        lines.append(head + f"{t.get('identity')}{' (site admin)' if t.get('is_admin') else ''}, "
                     f"{_repos(r.get('visible', 0))} (admin {r.get('admin', 0)}, write {r.get('write', 0)}, read {r.get('read', 0)})")
        lines.append("    scopes: " + ", ".join(f"{s}={v}" for s, v in t["scopes"].items()))
    for w in report.get("token_warnings") or []:
        lines.append(f"  token warning: {w}")
    lines.append("  features:")
    for f in report.get("features") or []:
        lines.append(f"    [{f['status']}] {f['label']}: {f['reason']}")
    if report.get("setups"):
        lines.append("  setups (what each would light):")
        for s in report["setups"]:
            mint = "; ".join(f"{env}: {', '.join(sc)}" for env, sc in s["mint"].items())
            lines.append(f"    {s['label']}: {', '.join(s['lights'])}")
            lines.append(f"      mint: {mint}")
    for w in (report.get("settings") or {}).get("warnings") or []:
        lines.append(f"  setting warning: {w}")
    lines.append(f"  flows: {report['flows']['detail']}")
    lines.append(f"  next: {report.get('next_step')}")
    return "\n".join(lines)
