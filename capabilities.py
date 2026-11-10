"""The capability registry: which feature needs what, and what this setup can do.

One source of truth for the doctor, the tools' refusals, the settings page and the desk:

- ``TOKENS``: the two key fields: the token of Hermes' account (required) and the optional write token of
  the user's own account (only the desk's Merge button, on his click).
- ``CAPS``: the capabilities a token can give (``read:repos``, ``admin:repo``, ``owner:merge``,
  ``notifications``, ``mark_read``, ``write:agent``), each with the token and the scopes that give it.
  Repo access itself is not a token matter: it is the Forgejo access of Hermes' account on each repo,
  which the desk's Repos tab shows per repo.
- ``FEATURES``: every user-visible feature, with the capability groups it needs (any one group is
  enough) and the setting that can switch it off.
- ``SETUPS``: the two ready-made setups, each with the scopes to mint.
- ``probe_caps(reports)``: probed tokens (the doctor's side-effect free ``token_report``s) to the set
  of capabilities and which token gives each. ``resolve(...)``: per feature ``available``, ``off``
  (with the setting) or ``dark`` (not available, with the one sentence that says what it needs).
- ``token_verdict(token)``: the one verdict the pages show for a token.

Tokens are judged by what they can do, never by the field they sit in: a token of Hermes' own account
in the field meant for the user's account is flagged and ignored.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

YES, NO = "yes", "no"
AGENT, OWNER = "bot", "owner"
BOT_SCOPES = ("read:notification", "write:issue", "write:repository", "read:user")
OWNER_SCOPES = ("write:repository", "read:user")
OWNER_RISK = "Every agent shell on the Hermes host can read this token and could merge into main as you."

# The two key fields, one row each. plugin.yaml's config_schema carries the same label and env
# (tests/test_settings.py holds them equal); the settings page draws its setup steps from these rows.
TOKENS = (
    {"role": AGENT, "key": "token", "env": "FORGEBRUECKE_BOT_ACCOUNT_TOKEN", "label": "Hermes' account token",
     "name": "Hermes' token", "required": True, "account": "agent", "mint": BOT_SCOPES,
     "unlocks": "Everything Hermes does on Forgejo, with the access its account has per repo.",
     "never": "Anything as you."},
    {"role": OWNER, "key": "owner_token", "env": "FORGEBRUECKE_PERSONAL_WRITE_TOKEN",
     "label": "Your write token (optional)", "name": "your write token", "required": False, "account": "you",
     "mint": OWNER_SCOPES,
     "unlocks": "Adds a Merge button to the desk.",
     "never": "Agent tools, mention answers or flows.",
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


# What gives a capability: (the token's role, its scopes, a condition on the token), or the words for what
# no token gives.
CAPS = {
    "read:repos": (AGENT, ("read:repository", "read:issue"), ""),
    "admin:repo": "Hermes' account as an admin of the repo (Settings, Collaborators in Forgejo)",
    "owner:merge": (OWNER, ("write:repository",), ""),
    "notifications": (AGENT, ("read:notification",), "for all repositories"),
    "mark_read": (AGENT, ("write:notification",), ""),
    "write:agent": (AGENT, ("write:issue", "write:repository"), ""),
}
FIELD_FOR = {cap: need[0] if isinstance(need, tuple) else AGENT for cap, need in CAPS.items()}


def needs_sentence(caps: Iterable[str]) -> str:
    """What the missing capabilities need, as one sentence: each token once, with all its scopes."""
    scopes: dict = {}
    notes: dict = {}
    other = []
    for cap in caps:
        need = CAPS[cap]
        if not isinstance(need, tuple):
            other.append(need)
            continue
        role, wanted, note = need
        scopes.setdefault(role, []).extend(s for s in wanted if s not in scopes.get(role, []))
        if note:
            notes[role] = note
    parts = [f"{_field(role) if role == OWNER else _name(role)} with {_listed(found)}"
             + (f", {notes[role]}" if role in notes else "") for role, found in scopes.items()]
    return "Needs " + "; and ".join(parts + other) + "."


# id, label, needs (any one group is enough), what can switch it off (see _off_reason)
FEATURES = (
    ("read", "Reading", (("read:repos",),), None),
    ("mentions", "Answers to mentions", (("notifications", "write:agent"),), "mentions"),
    ("mark_read", "Mark notifications read", (("mark_read",),), None),
    ("writes", "Writing (comments, pull requests, checks)", (("write:agent",),), "writes"),
    ("flows", "Flows", (("write:agent",),), "flows"),
    ("protection_view", "Branch protection view", (("admin:repo",),), None),
    ("audit", "Security audit", (("admin:repo",),), "audit"),
    ("presets", "Protection presets", (("admin:repo",),), None),
    ("owner_merge", "Merge button", (("owner:merge",),), None),
    ("import", "Project import", (("write:agent",),), None),
)

SETUPS = (
    {"id": "agent_only", "label": "Hermes' account only (recommended)", "tokens": {AGENT: BOT_SCOPES},
     "summary": "Hermes works as its own Forgejo account. You merge in Forgejo."},
    {"id": "agent_owner", "label": "Hermes' account plus your write token", "tokens": {AGENT: BOT_SCOPES,
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


def _has(scopes: dict, scope: str) -> Optional[str]:
    """``yes``, ``no`` or what the probe said. A write scope also gives its read scope."""
    if scopes.get(scope) == YES or (scope.startswith("read:") and scopes.get("write:" + scope[5:]) == YES):
        return YES
    return scopes.get(scope)


def _listed(words: list) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def token_verdict(token: Optional[dict]) -> dict:
    """One verdict per token: ``{state, value, text}``. The setup row, the next step and the badge all read
    this, so a token never gets two. ``state`` is unset, ok, refused (401, 403), unchecked (Forgejo failed or
    did not answer), wrong_account, lacks_scopes or no_repo; ``value`` is what the row shows beside its
    title; ``text`` says what was seen and the fix, None when there is nothing to do."""
    token = token or {}
    role = token.get("role") or AGENT
    name = _name(role)

    def out(state: str, value: str = "", text: Optional[str] = None) -> dict:
        return {"state": state, "value": value, "text": text}

    def lacks(scopes: list) -> dict:
        return out("lacks_scopes", "scopes missing", f"{_cap(name)} lacks {_listed(scopes)}. Replace it with one "
                                                     f"that has {'it' if len(scopes) == 1 else 'them'}.")
    if not token.get("set"):
        return out("unset")
    error = token.get("error")
    if error:
        status = error.get("status")
        if error.get("kind") == "wrong_account":
            return out("wrong_account", "wrong account", error.get("message"))
        if error.get("missing_scopes"):
            return lacks(list(error["missing_scopes"]))
        if status in (401, 403):
            return out("refused", "token refused", f"Forgejo refused {name} ({status}). Replace it.")
        saw = f"Forgejo failed ({status})" if status else "Forgejo did not answer"
        return out("unchecked", "not checked", f"{saw} while checking {name}. Try again.")
    scopes = token.get("scopes") or {}
    missing = [s for s in (BOT_SCOPES if role == AGENT else OWNER_SCOPES) if _has(scopes, s) == NO]
    if missing:
        return lacks(missing)
    login = "@" + str(token.get("identity") or token.get("login")) if token.get("identity") or token.get("login") else ""
    if role == AGENT and scopes and not all(_has(scopes, s) == YES for s in ("write:issue", "write:repository")):
        repos = token.get("repos") or {}
        if not repos.get("error") and not (repos.get("write") or repos.get("admin")):
            return out("no_repo", "no write access", "Hermes' account cannot write to any repo yet. Give it Write on "
                                                     "a repo in Forgejo.")
        return out("unchecked", "not checked", "Forgejo did not say whether Hermes' token may write. Try again.")
    return out("ok", login)


def probe_caps(reports: dict) -> "tuple[dict, list]":
    """``({cap: role that gives it}, warnings)`` from token reports keyed by role (bot, owner)."""
    bot, owner = reports.get(AGENT), reports.get(OWNER)
    bot_id = (bot or {}).get("identity")
    caps: dict = {}
    warnings: list = []
    if owner and not owner.get("error") and bot_id and owner.get("identity") == bot_id:
        text = (f"{_cap(_name(OWNER))} belongs to {bot_id}, Hermes' own account. Replace it with a token of your own "
                "account.")
        warnings.append(text)
        # Written onto the token the report carries, so the doctor and the page give it one verdict.
        owner["error"] = {"status": None, "kind": "wrong_account", "message": text}
    if bot and not bot.get("error") and bot.get("is_admin"):
        warnings.append(f"Hermes' token belongs to a site admin ({bot_id}). Every write by Hermes could reach any "
                        "repo; a plain user is safer.")
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
    depend on the repo, so they are not part of a setup)."""
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
                    "lights": [label for fid, label, needs, _off in FEATURES if _lit(needs, caps)]})
    return out


def _off_reason(kind: Optional[str], settings: Any, channel: Optional[dict]) -> Optional[str]:
    if kind == "mentions":
        if channel and channel.get("blockers"):
            return "; ".join(channel["blockers"])
    if kind == "writes" and settings.dry_run:
        return "Preview only. Switch on Let Hermes write."
    if kind == "flows" and not any(r.flows for r in settings.writes):
        return "None yet. Add one: ask Hermes to set up a flow for a repo."
    if kind == "audit" and not settings.audit_enabled:
        return "Off. Switch on Security audit on the Forgebrücke page."
    return None


def resolve(caps: dict, settings: Any, *, channel: Optional[dict] = None, ops_missing: Optional[Callable] = None,
            notes: Optional[dict] = None) -> list:
    """Per feature: ``{id, label, status, reason, uses, light_with}``."""
    out = []
    for fid, label, needs, off_kind in FEATURES:
        group = _lit(needs, caps)
        entry: dict = {"id": fid, "label": label}
        missing_ops = ops_missing(fid) if ops_missing else []
        if missing_ops:
            entry.update(status="dark", reason="This Forgejo version does not offer it.", missing=missing_ops)
        elif group is None:
            first = needs[0]
            need = [c for c in first if c not in caps]
            entry.update(status="dark", reason=needs_sentence(need),
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


def dark_sentence(feature_id: str) -> str:
    """The sentence a refusal uses when a feature's capability is missing (no probe needed)."""
    for fid, label, needs, _off in FEATURES:
        if fid == feature_id:
            sentence = needs_sentence(needs[0])
            return f"{label} {sentence[:1].lower()}{sentence[1:]}"
    raise KeyError(feature_id)
