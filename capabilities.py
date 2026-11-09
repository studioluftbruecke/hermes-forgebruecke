"""The capability registry (M4b, N25; 0.0.14): which feature needs what, and what this setup can do.

One source of truth for the doctor, the tools' refusals, the settings page and the desk:

- ``TOKENS``: the two key fields: the agent account's token (required) and the optional write token of
  the user's own account (D30: only the desk's Merge button, on his click).
- ``CAPS``: the capabilities a token can give (``read:repos``, ``admin:repo``, ``owner:merge``,
  ``notifications``, ``mark_read``, ``write:agent``), each with the sentence that says which token, with
  which scope, would give it. Repo access itself is not a token matter: it is the Forgejo collaborator
  right of the agent account on each repo (D27), which the desk's Repos tab shows per repo.
- ``FEATURES``: every user-visible feature, with the capability groups it needs (any one group lights
  it) and the setting that can switch it off.
- ``SETUPS``: the two ready-made setups, each with the scopes to mint.
- ``probe_caps(reports)``: probed tokens (the doctor's side-effect free ``token_report``s) to the set
  of capabilities and which token gives each. ``resolve(...)``: per feature ``available``, ``off``
  (with the setting) or ``dark`` (with the one sentence that would light it).

Tokens are judged by what they can do, never by the field they sit in: a token of the agent's own account
in the field meant for the user's account is flagged and ignored.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

YES = "yes"
AGENT, OWNER = "bot", "owner"
BOT_SCOPES = ("read:notification", "write:issue", "write:repository", "read:user")
OWNER_SCOPES = ("write:repository", "read:user")
OWNER_RISK = ("Every agent shell on the Hermes host can read this token and could merge into main as you. Leave "
              "it empty unless you accept that; you can always merge in Forgejo instead.")

# The two key fields, one row each. plugin.yaml's config_schema carries the same label and env
# (tests/test_settings.py holds them equal); the settings page draws its setup steps from these rows.
TOKENS = (
    {"role": AGENT, "key": "token", "env": "FORGEBRUECKE_BOT_ACCOUNT_TOKEN", "label": "Agent account token",
     "name": "the agent account token", "required": True, "account": "agent", "mint": BOT_SCOPES,
     "unlocks": "Everything Hermes does on Forgejo: reads, comments, pull requests, statuses, mention answers and "
                "flow runs, in the repos you share with its account and with the right you give it there.",
     "never": "Anything as you."},
    {"role": OWNER, "key": "owner_token", "env": "FORGEBRUECKE_PERSONAL_WRITE_TOKEN",
     "label": "Your write token (optional)", "name": "your write token", "required": False, "account": "you",
     "mint": OWNER_SCOPES,
     "unlocks": "Only the desk's Merge button, on your click, merging as you.",
     "never": "Agent tool writes, mention answers or flows.",
     "risk": OWNER_RISK},
)
FIELD = {t["role"]: (t["label"], t["env"]) for t in TOKENS}
WRITE_SCOPES = ("write:issue", "write:repository", "write:notification", "write:organization", "write:user",
                "write:admin", "write:package", "write:activitypub", "write:misc")


def _name(role: str) -> str:
    return next(t["name"] for t in TOKENS if t["role"] == role)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _field(role: str) -> str:
    return f"{_name(role)} ({FIELD[role][1]})"


CAPS = {
    "read:repos": f"{_field(AGENT)} with read:repository and read:issue (write scopes include them)",
    "admin:repo": ("admin right of Hermes' Forgejo account on the repo (in Forgejo: the repo's Settings → "
                   "Collaborators); it lights per repo, only where the account is admin"),
    "owner:merge": f"{_field(OWNER)} with write:repository",
    "notifications": (f"{_field(AGENT)} with read:notification, created for all repos (a "
                      "repo-scoped token cannot read notifications)"),
    "mark_read": f"{_field(AGENT)} with write:notification (optional; a ledger is used instead)",
    "write:agent": f"{_field(AGENT)} with write:issue and write:repository",
}

# id, label, needs (any one group lights the feature), off(settings, channel) -> reason or None, milestone
FEATURES = (
    ("read", "Read issues, PRs, checks", (("read:repos",),), None, "M2"),
    ("mentions", "Mention channel", (("notifications", "write:agent"),), "mentions", "M4"),
    ("mark_read", "Mark notifications read", (("mark_read",),), None, "M4"),
    ("writes", "Comments, PRs and statuses by Hermes", (("write:agent",),), "writes", "M3"),
    ("flows", "Flows (review, checks, staging, your merge)", (("write:agent",),), "flows", "M6"),
    ("protection_view", "Branch protection on the repos page", (("admin:repo",),), None, "M7"),
    ("audit", "Security audit", (("admin:repo",),), "audit", "M8"),
    ("presets", "Protection presets", (("admin:repo",),), None, "M8"),
    ("owner_merge", "Merge button in the desk (with your account)", (("owner:merge",),), None, "M7"),
    ("import", "Import a project into the agent's namespace", (("write:agent",),), None, "M9"),
)

SETUPS = (
    {"id": "agent_only", "label": "Agent account only (recommended)", "tokens": {AGENT: BOT_SCOPES},
     "summary": "Hermes works as its own Forgejo account. You decide per repo whether it may read or write "
                "(Settings → Collaborators in Forgejo), and you merge in Forgejo."},
    {"id": "agent_owner", "label": "Agent account plus your write token", "tokens": {AGENT: BOT_SCOPES,
                                                                                     OWNER: OWNER_SCOPES},
     "summary": "Adds the desk's Merge button, on your click. " + OWNER_RISK},
)


def _ok(report: Optional[dict], *scopes: str) -> bool:
    if not report or report.get("error") or not report.get("set", True):
        return False
    have = report.get("scopes") or {}

    def one(s: str) -> bool:
        if have.get(s) == YES:
            return True
        return s.startswith("read:") and have.get("write:" + s[5:]) == YES
    return all(one(s) for s in scopes)


def probe_caps(reports: dict) -> "tuple[dict, list]":
    """``({cap: role that gives it}, warnings)`` from token reports keyed by role (bot, owner)."""
    bot, owner = reports.get(AGENT), reports.get(OWNER)
    bot_id = (bot or {}).get("identity")
    caps: dict = {}
    warnings: list = []
    if owner and not owner.get("error") and bot_id and owner.get("identity") == bot_id:
        warnings.append(f"{_cap(_field(OWNER))} belongs to {bot_id}, Hermes' own account. It should be a token of "
                        "your own account; it is ignored until it is replaced.")
        reports[OWNER] = owner = {**owner, "error": {"message": "same account as the agent account token"}}
    if bot and not bot.get("error") and bot.get("is_admin"):
        warnings.append(f"The agent account token belongs to a site admin ({bot_id}). Every agent write could reach "
                        "any repo; a plain user is safer.")
    if _ok(bot, "read:repository", "read:issue"):
        caps["read:repos"] = AGENT
    if bot and not bot.get("error") and (bot.get("protection") or {}).get("readable"):
        caps["admin:repo"] = AGENT
    if _ok(owner, "write:repository"):
        caps["owner:merge"] = OWNER
    if _ok(bot, "read:notification"):
        caps["notifications"] = AGENT
    if _ok(bot, "write:notification"):
        caps["mark_read"] = AGENT
    if _ok(bot, "write:issue", "write:repository"):
        caps["write:agent"] = AGENT
    return caps, warnings


def setup_caps(setup: dict) -> set:
    """The capabilities a setup can give when every token is minted as the setup says (admin features
    light per repo, so they are not part of a setup)."""
    reports = {role: {"identity": role, "scopes": {s: YES for s in scopes}} for role, scopes in setup["tokens"].items()}
    caps, _ = probe_caps(reports)
    return set(caps)


def _lit(needs: tuple, caps: Any) -> Optional[tuple]:
    return next((group for group in needs if all(c in caps for c in group)), None)


def setups_report() -> list:
    out = []
    for s in SETUPS:
        caps = setup_caps(s)
        out.append({"id": s["id"], "label": s["label"], "summary": s["summary"],
                    "mint": {FIELD[role][1]: list(scopes) for role, scopes in s["tokens"].items()},
                    "lights": [label for fid, label, needs, _off, _m in FEATURES if _lit(needs, caps)]})
    return out


def _off_reason(kind: Optional[str], settings: Any, channel: Optional[dict]) -> Optional[str]:
    if kind == "mentions":
        if channel and channel.get("blockers"):
            return "; ".join(channel["blockers"])
    if kind == "writes" and settings.dry_run:
        return "dry_run is on: writes are previewed, not sent"
    if kind == "flows" and not any(r.flows for r in settings.writes):
        return "no per-repo rule lists a flow id in flows"
    if kind == "audit" and not settings.audit_enabled:
        return "audit_enabled is off (the default)"
    return None


def resolve(caps: dict, settings: Any, *, channel: Optional[dict] = None, ops_missing: Optional[Callable] = None,
            notes: Optional[dict] = None) -> list:
    """Per feature: ``{id, label, status, reason, uses, light_with, milestone}``."""
    out = []
    for fid, label, needs, off_kind, milestone in FEATURES:
        group = _lit(needs, caps)
        entry: dict = {"id": fid, "label": label, "milestone": milestone}
        missing_ops = ops_missing(fid) if ops_missing else []
        if missing_ops:
            entry.update(status="dark", reason=f"this Forgejo lacks {', '.join(missing_ops)}")
        elif group is None:
            first = needs[0]
            need = [c for c in first if c not in caps]
            entry.update(status="dark", reason="needs " + "; and ".join(CAPS[c] for c in need),
                         light_with=[FIELD_FOR.get(c, AGENT) for c in need])
        else:
            uses = sorted({caps[c] for c in group})
            off = _off_reason(off_kind, settings, channel)
            note = (notes or {}).get(fid, "")
            if off:
                entry.update(status="off", reason=off, uses=uses)
            else:
                entry.update(status="available", reason=note or "uses " + " and ".join(_name(r) for r in uses),
                             uses=uses)
        out.append(entry)
    return out


FIELD_FOR = {"read:repos": AGENT, "admin:repo": AGENT, "owner:merge": OWNER, "notifications": AGENT,
             "mark_read": AGENT, "write:agent": AGENT}


def dark_sentence(feature_id: str) -> str:
    """The sentence a refusal uses when a feature's capability is missing (no probe needed)."""
    for fid, label, needs, _off, _m in FEATURES:
        if fid == feature_id:
            return f"{label} needs " + "; and ".join(CAPS[c] for c in needs[0]) + "."
    raise KeyError(feature_id)
