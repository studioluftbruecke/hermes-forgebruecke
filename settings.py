"""Typed settings for Forgebrücke, with defaults, validation and per-repo rules.

Shapes:

- ``Rule(repo_glob, mentions, flows, targets)``: one row of the ``writes`` setting (label "Per-repo
  rules"). It says where flows run, whether Hermes answers mentions there and which branches a pull
  request may target. It grants nothing: what Hermes may read or write in a repo is the Forgejo
  collaborator right of its account there, asked in ``access.py``. ``mentions`` is on
  unless a rule switches it off. What cannot be read switches it off too, as it may be the opt-out: a
  value that is neither true nor false, an unknown key in a rule that does not state ``mentions``, and a
  rule without a readable ``repo_glob`` (then for every later rule and every repo no rule names).
  ``targets`` empty means the repo's default branch. The keys ``read``,
  ``comment`` and ``write`` of earlier versions are ignored with a warning.
- ``Settings``: every non-secret setting from ``config_schema``, already validated, plus the
  ``warnings`` produced while validating (an invalid value falls back to its default and says so).
  ``you`` is the user's own Forgejo login: the desk's Waiting for me lists the pull requests that
  request the user's review, and ``forgejo_create_pr`` requests that review by default. With
  ``review_every_pr`` (on by default) the flows tick asks the user on every other open pull request too, and
  the desk lists one that does not ask the user yet (``review_requests.py``).
- ``Credentials(token, owner_token)``: the agent account's token (required) and an optional write token
  of the user's own account (only the desk's Merge and Comment buttons, on the user's click). Their ``repr`` never
  shows a value, and nothing in this plugin logs them. A token with a space, a line break or a
  non-ASCII character counts as unset, and ``problems()`` says which field to enter again. Which token a
  call uses is decided by its lane (``access.py``), not by the caller.

Settings live in ``config.yaml`` under ``plugins.entries.forgebruecke.settings``; the two secrets live in
the profile's ``.env`` as ``FORGEBRUECKE_BOT_ACCOUNT_TOKEN`` and ``FORGEBRUECKE_PERSONAL_WRITE_TOKEN``
(plugin prefixed on purpose: a host may already export ``FORGEJO_TOKEN`` for another account).
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

TOKEN_ENV = "FORGEBRUECKE_BOT_ACCOUNT_TOKEN"
OWNER_TOKEN_ENV = "FORGEBRUECKE_PERSONAL_WRITE_TOKEN"
# Settings and rule keys of earlier versions, read only to say they are ignored now.
RETIRED_SETTINGS = ("read_scope", "badge_meaning")
RETIRED_RULE_KEYS = ("read", "comment", "write")
RULE_KEYS = ("repo_glob", "mentions", "flows", "targets")

_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
POLL_MIN, POLL_MAX = 30, 3600
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REPO_GLOB_RE = re.compile(r"^[A-Za-z0-9_.*?\[\]-]+/[A-Za-z0-9_.*?\[\]-]+$")

DEFAULTS: dict = {
    "url": "",
    "you": "",
    "poll_interval": 30,
    "mention_authors": [],
    "writes": [],
    "flows_path": ".forgebruecke/flows.yml",
    "audit_enabled": False,
    "learn_mode": False,
    "default_review_profile": "",  # empty: the profile Forgebrücke runs in
    "dry_run": True,
    "review_every_pr": True,
}

SettingGetter = Callable[[str, Any], Any]
SecretGetter = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class Rule:
    repo_glob: str
    mentions: bool = True
    flows: tuple = ()
    targets: tuple = ()

    def matches(self, repo: str) -> bool:
        return fnmatch.fnmatchcase(repo.lower(), self.repo_glob.lower())

    def to_dict(self) -> dict:
        return {"repo_glob": self.repo_glob, "mentions": self.mentions, "flows": list(self.flows),
                "targets": list(self.targets)}


@dataclass(frozen=True)
class Settings:
    url: str = ""
    you: str = ""
    poll_interval: int = 30
    mention_authors: tuple = ()
    writes: tuple = ()
    flows_path: str = ".forgebruecke/flows.yml"
    audit_enabled: bool = False
    learn_mode: bool = False
    default_review_profile: str = ""
    dry_run: bool = True
    review_every_pr: bool = True
    warnings: tuple = field(default=(), compare=False)
    mentions_elsewhere: bool = True  # in a repo no rule names; off when a rule could not be read

    def rule_for(self, repo: str) -> Rule:
        """First rule whose glob matches, else the default (mentions on, no flow, default branch only)."""
        for rule in self.writes:
            if rule.matches(repo):
                return rule
        return Rule(repo_glob=repo, mentions=self.mentions_elsewhere)

    def explicit_rule(self, repo: str) -> Optional[Rule]:
        return next((r for r in self.writes if r.matches(repo)), None)

    def public(self) -> dict:
        """Every setting, safe to show (there are no secrets in Settings)."""
        return {"url": self.url, "you": self.you, "poll_interval": self.poll_interval,
                "mention_authors": list(self.mention_authors), "writes": [r.to_dict() for r in self.writes],
                "flows_path": self.flows_path, "audit_enabled": self.audit_enabled,
                "learn_mode": self.learn_mode, "default_review_profile": self.default_review_profile,
                "dry_run": self.dry_run, "review_every_pr": self.review_every_pr}


_TOKEN_RE = re.compile(r"[\x21-\x7e]+")


def _usable_token(value: Optional[str], name: str, problems: list) -> Optional[str]:
    value = (value or "").strip()
    if not value or _TOKEN_RE.fullmatch(value):
        return value or None
    found = "a space or line break" if re.search(r"\s", value) else "a character no token has"
    problems.append(f"{name} contains {found}. Enter it again.")
    return None


class Credentials:
    """The agent account's token and the optional write token of the user's own account. Never printable.

    A value that is not printable ASCII cannot go into an HTTP header, and urllib's error for it quotes the
    value. Such a token counts as unset, and ``problems()`` names the field."""

    __slots__ = ("_token", "_owner_token", "_problems")

    def __init__(self, token: Optional[str], owner_token: Optional[str] = None) -> None:
        self._problems: list = []
        self._token = _usable_token(token, "Hermes' token", self._problems)
        self._owner_token = _usable_token(owner_token, "Your write token", self._problems)

    @property
    def token(self) -> Optional[str]:
        return self._token

    @property
    def owner_token(self) -> Optional[str]:
        return self._owner_token

    def presence(self) -> dict:
        return {TOKEN_ENV: self._token is not None, OWNER_TOKEN_ENV: self._owner_token is not None}

    def problems(self) -> list:
        """One sentence per token that was dropped; the value is never part of it."""
        return list(self._problems)

    def __repr__(self) -> str:
        p = self.presence()
        return (f"Credentials(token={'set' if p[TOKEN_ENV] else 'unset'}, "
                f"owner_token={'set' if p[OWNER_TOKEN_ENV] else 'unset'})")

    __str__ = __repr__


def load_credentials(get_secret: SecretGetter) -> Credentials:
    return Credentials(get_secret(TOKEN_ENV), get_secret(OWNER_TOKEN_ENV))


_BOOL_WORDS = {"true": True, "yes": True, "on": True, "1": True, "false": False, "no": False, "off": False, "0": False}


def _read_bool(value: Any) -> Optional[bool]:
    """True or False for a bool or one of the words a ``hermes config set`` stores, else None."""
    if isinstance(value, bool):
        return value
    return _BOOL_WORDS.get(value.strip().lower()) if isinstance(value, str) else None


def _as_bool(key: str, value: Any, warnings: list) -> bool:
    read = _read_bool(value)
    if read is None:
        warnings.append(f"{key}: {value!r} is not true or false; using {DEFAULTS[key]}.")
        return DEFAULTS[key]
    return read


_OFF_FROM_HERE = "Until then mentions are off wherever no earlier rule applies."


def _parse_rule(i: int, raw: Any, warnings: list) -> Optional[Rule]:
    where = f"writes[{i}]"
    if not isinstance(raw, dict):
        warnings.append(f"{where}: a rule must be a mapping like {{repo_glob: owner/repo}}. {_OFF_FROM_HERE}")
        return None
    glob = str(raw.get("repo_glob") or "").strip()
    if glob == "*":
        glob = "*/*"
    if not _REPO_GLOB_RE.match(glob):
        warnings.append(f"{where}: repo_glob {glob!r} must look like owner/repo (wildcards allowed). {_OFF_FROM_HERE}")
        return None
    retired = [k for k in RETIRED_RULE_KEYS if k in raw]
    if retired:
        warnings.append(f"{where}: {', '.join(retired)} no longer apply and are ignored. What Hermes may read or "
                        "write in a repo is the access of Hermes' account there (the repo's Settings → Collaborators).")
    unknown = sorted(str(k) for k in raw if k not in RULE_KEYS and k not in RETIRED_RULE_KEYS)
    mentions = _read_bool(raw.get("mentions", True))
    if mentions is None:
        warnings.append(f"{where}.mentions: {raw['mentions']!r} is not true or false; mentions are off for this rule.")
        mentions = False
    if unknown and "mentions" not in raw:  # one of them may be the opt-out, misspelt
        warnings.append(f"{where}: unknown keys {', '.join(unknown)} ignored. Mentions are off for this rule until "
                        "it states mentions: true or false.")
        mentions = False
    elif unknown:
        warnings.append(f"{where}: unknown keys {', '.join(unknown)} ignored.")
    flows = raw.get("flows") or []
    if not isinstance(flows, list) or not all(isinstance(f, str) and f for f in flows):
        warnings.append(f"{where}.flows: must be a list of flow names; ignored.")
        flows = []
    targets = raw.get("targets") or []
    if not isinstance(targets, list) or not all(isinstance(t, str) and _BRANCH_RE.match(t) for t in targets):
        warnings.append(f"{where}.targets: must be a list of branch names; using the default branch only.")
        targets = []
    return Rule(repo_glob=glob, mentions=mentions, flows=tuple(flows), targets=tuple(targets))


def this_profile() -> str:
    """The name of the profile Forgebrücke runs in (``profile.current_name``), or "" outside Hermes."""
    try:
        import importlib
        return importlib.import_module(".profile", __package__).current_name() if __package__ else ""
    except Exception:  # a settings read must never fail on this
        return ""


def load_settings(get_setting: SettingGetter, here: Optional[Callable[[], str]] = None) -> Settings:
    """Read and validate every setting; never raises on bad input. ``here`` names the running profile,
    the review profile when the setting is empty."""
    w: list = []

    def raw(key: str) -> Any:
        try:
            v = get_setting(key, DEFAULTS[key])
        except Exception:  # a broken config read must not break the plugin
            v = DEFAULTS[key]
        return DEFAULTS[key] if v is None else v

    for key in RETIRED_SETTINGS:
        try:
            old = get_setting(key, None)
        except Exception:
            old = None
        if old is not None:
            w.append(f"{key} no longer applies and is ignored. Hermes sees what Hermes' account sees, and the "
                     "sidebar counts Waiting for me. You can remove it from Hermes' settings.")

    url = str(raw("url") or "").strip().rstrip("/")
    if "@" in url.split("://", 1)[-1].split("/", 1)[0]:  # never echoed: the part before the @ may be a password
        w.append("url: the address holds a user name or password; ignored. Enter it without them.")
        url = ""
    elif url and not url.startswith(("https://", "http://")):
        w.append(f"url: {url!r} must start with https://; ignored.")
        url = ""
    if url.startswith("http://") and urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        w.append("url: plain HTTP sends Forgejo tokens without encryption; use HTTPS outside localhost.")

    you = str(raw("you") or "").strip().lstrip("@")
    if you and not _USER_RE.match(you):
        w.append(f"you: {you!r} is not a Forgejo user name; ignored.")
        you = ""

    poll_raw = raw("poll_interval")
    try:
        poll = int(poll_raw)
        if isinstance(poll_raw, bool):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        w.append(f"poll_interval: {poll_raw!r} is not a number of seconds; using {DEFAULTS['poll_interval']}.")
        poll = DEFAULTS["poll_interval"]
    if not POLL_MIN <= poll <= POLL_MAX:
        clamped = min(max(poll, POLL_MIN), POLL_MAX)
        w.append(f"poll_interval: {poll} s is outside {POLL_MIN} to {POLL_MAX}; using {clamped}.")
        poll = clamped

    authors_raw = raw("mention_authors")
    if isinstance(authors_raw, str):
        authors_raw = [a for a in re.split(r"[\s,]+", authors_raw) if a]
    if not isinstance(authors_raw, list) or not all(isinstance(a, str) for a in authors_raw):
        w.append("mention_authors: must be a list of user names; using an empty list (anyone with write access).")
        authors_raw = []

    rules_raw = raw("writes")
    if isinstance(rules_raw, str):  # a `hermes config set` can store the list as its JSON text
        try:
            rules_raw = json.loads(rules_raw) if rules_raw.strip() else []
        except ValueError:
            pass
    if isinstance(rules_raw, dict):  # one rule, written without the list around it
        rules_raw = [rules_raw]
    # A rule that cannot be read may be the one that switched mentions off somewhere, and nobody can tell
    # where. So from that rule on they are off: in every later rule and in every repo no rule names.
    mentions_elsewhere = True
    if not isinstance(rules_raw, list):
        w.append("writes: must be a list of rules. Until it is one, no rule applies and mentions are off in every repo.")
        rules_raw, mentions_elsewhere = [], False
    rules = []
    for i, x in enumerate(rules_raw):
        rule = _parse_rule(i, x, w)
        if rule is None:
            mentions_elsewhere = False
        else:
            rules.append(rule if mentions_elsewhere else replace(rule, mentions=False))

    flows_path = str(raw("flows_path") or DEFAULTS["flows_path"]).strip()
    if flows_path.startswith("/") or ".." in flows_path.split("/"):
        w.append(f"flows_path: {flows_path!r} must be a path inside the repo; using {DEFAULTS['flows_path']}.")
        flows_path = DEFAULTS["flows_path"]

    profile = str(raw("default_review_profile")).strip()
    if profile and not _PROFILE_RE.match(profile):
        w.append(f"default_review_profile: {profile!r} is not a profile name; using this profile.")
        profile = ""
    profile = profile or (here or this_profile)()

    return Settings(
        url=url, you=you, poll_interval=poll,
        mention_authors=tuple(a.strip().lstrip("@") for a in authors_raw if a.strip()),
        writes=tuple(rules), flows_path=flows_path,
        audit_enabled=_as_bool("audit_enabled", raw("audit_enabled"), w),
        learn_mode=_as_bool("learn_mode", raw("learn_mode"), w),
        default_review_profile=profile,
        dry_run=_as_bool("dry_run", raw("dry_run"), w),
        review_every_pr=_as_bool("review_every_pr", raw("review_every_pr"), w),
        warnings=tuple(w), mentions_elsewhere=mentions_elsewhere,
    )
