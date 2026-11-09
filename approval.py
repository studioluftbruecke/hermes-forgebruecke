"""The agent lane for writes: Hermes' approval gate, one-use tickets, and the unattended refusal.

Three lanes (PRODUCT.md, Safety lanes): the user's click (the desk, M7), the agent's tool call
(this module), unattended runs (refused here until a flow names a write for its stage, M6).

The ``pre_tool_call`` hook either blocks with a sentence, or returns Hermes' public directive
``{"action": "approve", "message", "rule_key"}`` so Hermes asks the human with the plan's text. The
plugin never approves anything itself. Hermes skips a hook that raises, so the hook leaves a
one-use ticket for exactly the plan it sent to the gate, and the handler refuses a call without
one. A denied or timed-out approval revokes the ticket (``post_approval_response``). With
``dry_run`` on, the hook lets the call through without asking and the handler only previews.

Restated from Hermes' own rule (``tools/approval_context.py``, not a public import): a run is
unattended when it is a cron job, a single-query ``hermes chat -q``/``-z`` run, or a session on the
webhook, msgraph_webhook or api_server surface. ``hermes -z`` auto-approves inside Hermes' gate,
which is why this check runs first.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

UNATTENDED_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})
MENTION_PLATFORM = "forgebruecke"  # the plugin's own channel: a forge user started the turn
REVIEW_SOURCE = "forgebruecke-review"
# flows/runner.py starts each review worker with an ephemeral system prompt that begins with this marker.
# Hermes carries that prompt with the session (agent.session_client_env), so the turn can tell it is a
# worker. The marker only narrows what a turn may read; nobody outside the host can set it.
REVIEW_MARKER = "[forgebruecke-review]"
RULE_PREFIX = "plugin_rule:"  # Hermes' namespace for a plugin rule_key (tools/approval.py)
RULE_NS = "forgebruecke:"


def _mod(name: str):
    return importlib.import_module(f".{name}", __package__ or __name__.rpartition(".")[0])


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


class RunContext:
    def __init__(self, cron: bool = False, single_query: bool = False, platform: str = "",
                 unknown: bool = False, chat_id: str = "", source: str = "") -> None:
        self.cron, self.single_query, self.platform = cron, single_query, (platform or "").strip().lower()
        self.source = (source or "").strip().lower()
        self.unknown = unknown  # Hermes' one-shot marker could not be read: fail closed
        self.chat_id = chat_id or ""

    def lane(self) -> "tuple[str, str]":
        """``(lane, scope_repo)`` for reads (access.py): a mention turn reads only its thread's repo."""
        if self.source == REVIEW_SOURCE:
            return "mention", ""  # a review worker: no thread repo, so it reads nothing (load_access refuses)
        if self.platform == MENTION_PLATFORM:
            return "mention", _mod("access").thread_repo(self.chat_id)
        return "agent", ""

    def unattended_reason(self) -> Optional[str]:
        if self.unknown:
            return ("Forgebrücke cannot tell whether a person is present (this Hermes version moved "
                    "gateway.session_finite; update Forgebrücke)")
        if self.cron:
            return "this is a cron job"
        if self.single_query:
            return "this is a single-query run (hermes chat -q or -z)"
        if self.platform in UNATTENDED_PLATFORMS:
            return f"this session runs on the unattended {self.platform} surface"
        if self.platform == MENTION_PLATFORM:
            return ("this turn answers a Forgejo mention, where the answer itself is the only write "
                    "(say in the answer what should change)")
        return None


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env(name, "") or ""
    except Exception:
        import os
        return os.environ.get(name, "") or ""


def _review_worker() -> bool:
    try:
        from agent.session_client_env import session_env
        prompt = session_env("HERMES_EPHEMERAL_SYSTEM_PROMPT", "") or ""
    except Exception:
        import os
        prompt = os.environ.get("HERMES_EPHEMERAL_SYSTEM_PROMPT", "") or ""
    return prompt.lstrip().startswith(REVIEW_MARKER)


def _finite_turn() -> Optional[bool]:
    """True on a gateway-admitted ``chat -q``/``-z`` turn; None when the marker cannot be read.

    A private Hermes seam (no public API says "single-query" for a daemon turn, which never carries
    HERMES_SINGLE_QUERY_SESSION). If it moves, writes are refused rather than left to Hermes' gate,
    which auto-approves single-query runs."""
    try:
        from gateway.session_finite import finite_turn_required
    except ImportError:
        return None
    try:
        return finite_turn_required() is True
    except Exception:
        return None


def run_context_from(ctx) -> RunContext:
    cron = False
    try:
        cron = ctx.current_cron_execution() is not None
    except Exception:
        pass
    cron = cron or _truthy(_session_env("HERMES_CRON_SESSION"))
    finite = _finite_turn()
    single = bool(finite) or _truthy(_session_env("HERMES_SINGLE_QUERY_SESSION"))
    try:
        platform = ctx.session_surface() or _session_env("HERMES_SESSION_PLATFORM")
    except Exception:
        platform = _session_env("HERMES_SESSION_PLATFORM")
    return RunContext(cron, single, platform, unknown=finite is None, chat_id=_session_env("HERMES_SESSION_CHAT_ID"),
                      source=REVIEW_SOURCE if _review_worker() else _session_env("HERMES_SESSION_SOURCE"))


class ApprovalTickets:
    APPROVED_CHOICES = frozenset({"once", "session", "always"})

    def __init__(self, ttl_s: float = 900.0, clock: Optional[Callable[[], float]] = None) -> None:
        self.ttl_s = ttl_s
        self._clock = clock or time.monotonic
        self._tickets: dict = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(tool: str, session_id: Any, args: Any) -> str:
        material = json.dumps({"t": tool, "s": str(session_id or ""), "a": args if isinstance(args, dict) else {}},
                              sort_keys=True, default=str)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def revoke(self, key: str) -> None:
        with self._lock:
            self._tickets.pop(key, None)

    def revoke_rule(self, rule_key: str) -> int:
        with self._lock:
            dropped = 0
            for key in list(self._tickets):
                kept = [t for t in self._tickets[key] if t[2] != rule_key]
                dropped += len(self._tickets[key]) - len(kept)
                if kept:
                    self._tickets[key] = kept
                else:
                    del self._tickets[key]
            return dropped

    def issue(self, key: str, signature: tuple, rule_key: str) -> None:
        with self._lock:
            self._tickets.setdefault(key, []).append((self._clock(), signature, rule_key))

    def take(self, key: str) -> Optional[tuple]:
        with self._lock:
            now = self._clock()
            live = [t for t in self._tickets.get(key, []) if now - t[0] <= self.ttl_s]
            if not live:
                self._tickets.pop(key, None)
                return None
            ticket = live.pop(0)
            if live:
                self._tickets[key] = live
            else:
                self._tickets.pop(key, None)
            return ticket[1]


TICKETS = ApprovalTickets()


def _refusal(code: str, message: str, setting: str = "") -> dict:
    out = {"ok": False, "error": code, "message": message, "sent": False}
    if setting:
        out["setting"] = setting
    return out


def precheck(run: RunContext) -> Optional[dict]:
    reason = run.unattended_reason()
    if reason:
        return _refusal("unattended", f"Writes to the forge need a person to approve them, and {reason}, so no "
                                      "one can. Nothing was sent. Reads (forgejo_get, forgejo_list) still work; "
                                      "flows will name the writes a stage may make unattended.")
    return None


def _block(body: dict) -> dict:
    text = f"refused ({body['error']}): {body['message']}"
    if body.get("setting"):
        text += f" Setting: {body['setting']}."
    if "Nothing was sent" not in text:
        text += " Nothing was sent to the forge."
    return {"action": "block", "message": text}


def make_gate(load_access: Callable[[], Any], run_context: Callable[[], RunContext],
              tickets: ApprovalTickets = TICKETS, extra: Optional[dict] = None):
    """``extra`` maps more tool names to a spec with ``gated(args)``, ``plan(access, args)`` and
    ``execute(access, plan)`` (forgejo_flows' set and resume); they share the one ticket path."""
    write = _mod("tools.write")
    acc = _mod("access")
    extra = extra or {}

    def forgebruecke_write_gate(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any):
        spec = extra.get(tool_name)
        if tool_name not in write.WRITE_TOOLS and (spec is None or not spec.gated(args)):
            return None
        args = args if isinstance(args, dict) else {}
        key = tickets.key(tool_name, session_id, args)
        tickets.revoke(key)  # first, so nothing below can leave an older ticket usable
        refused = precheck(run_context())
        if refused:
            return _block(refused)
        try:
            access = load_access()
            p = spec.plan(access, args) if spec is not None else write.plan(access, tool_name, args)
        except acc.Refusal as r:
            return _block(r.to_dict())
        except _mod("client").ForgejoError as e:
            return _block(_refusal(e.kind, e.message))
        except ValueError as e:  # a flow file that does not parse
            return _block(_refusal("bad_args", str(e)))
        except Exception as e:  # fail closed
            return _block(_refusal("internal", type(e).__name__))
        if access.settings.dry_run:
            return None  # the handler previews; nothing to approve
        rule_key = f"{RULE_NS}{tool_name}:{p.repo}"
        tickets.issue(key, p.signature(), rule_key)
        return {"action": "approve", "message": p.approval_text("Hermes' Forgejo account (FORGEBRUECKE_BOT_ACCOUNT_TOKEN)"),
                "rule_key": rule_key}

    return forgebruecke_write_gate


def make_observer(tickets: ApprovalTickets = TICKETS):
    def forgebruecke_approval_observer(pattern_key: str = "", choice: Any = None, **_: Any) -> None:
        rule = str(pattern_key or "")
        rule = rule[len(RULE_PREFIX):] if rule.startswith(RULE_PREFIX) else rule
        if rule.startswith(RULE_NS) and str(choice or "") not in ApprovalTickets.APPROVED_CHOICES:
            tickets.revoke_rule(rule)

    return forgebruecke_approval_observer


def run_write(tool: str, args: dict, session_id: Any, load_access: Callable[[], Any], run: RunContext,
              tickets: ApprovalTickets = TICKETS, spec: Any = None) -> dict:
    """The handler body: only a plan the gate sent to Hermes' approval runs, re-planned and compared."""
    write = _mod("tools.write")
    acc = _mod("access")
    key = tickets.key(tool, session_id, args)
    refused = precheck(run)
    if refused:
        tickets.revoke(key)
        return refused
    try:
        access = load_access()
        p = spec.plan(access, args) if spec is not None else write.plan(access, tool, args)
    except acc.Refusal as r:
        tickets.revoke(key)
        return r.to_dict()
    except ValueError as e:
        tickets.revoke(key)
        return _refusal("bad_args", str(e))
    if access.settings.dry_run:
        tickets.revoke(key)
        return write.preview(p)
    approved = tickets.take(key)
    if approved is None:
        return _refusal("not_approved", "This call did not pass Forgebrücke's approval hook, so nothing was sent. "
                                        "Writes run only after the user approves them in Hermes.")
    if p.signature() != approved:
        return _refusal("not_approved", "The write changed between the approval and now (the forge moved); "
                                        "nothing was sent. Ask again.")
    return spec.execute(access, p) if spec is not None else write.execute(access, p)


# --- The agent may not quietly loosen its own guardrails ----------------------------------------------
#
# The guard watches this profile's own settings: its config.yaml and .env, the plugin copy it loaded, and
# `hermes` commands that change this profile or this plugin. The same names elsewhere (another profile's
# config, a repo's .env, a checkout of this plugin that is not the loaded one, another plugin) are not
# its business, so a profile that develops code, Forgebrücke included, can keep the plugin on.

GUARDED_TOOLS = ("write_file", "patch", "terminal", "execute_code")
PLUGIN_DIR = "forgebruecke"
_SETTINGS_FILES = ("config.yaml", ".env")
_SETTINGS_KEYS = ("plugins.entries.forgebruecke", "platform_toolsets", "approvals.mode")
# config.yaml or .env named bare (or as ./name): relative to a folder this hook cannot see, which may be
# the profile's own. Inside another path (docker/.env, process.env, .env.example) it is not.
_BARE_FILE_RE = re.compile(r"(?:^|[\s'\"=(<>|;&,])(?:\./)?(?:config\.yaml|\.env)(?=$|[\s'\"),;|&>])")
_HARMLESS_REDIRECT_RE = re.compile(r"\d*>&\d+|\d*>\s*/dev/null")
_HERMES_SET_RE = re.compile(r"\bconfig\s+(set|unset|edit|migrate|reset)\b|\bplugins\s+(disable|enable|install|remove|uninstall)\b")
# One `hermes` call that changes settings: the profile it names (if any), and for a plugins verb its arguments.
_HERMES_CALL_RE = re.compile(r"\bhermes\b(?:\s+(?:-p|--profile)(?:\s+|=)(?P<profile>[\w.-]+))?\s+(?:config\s+(?:set|unset|edit|"
                             r"migrate|reset)\b|plugins\s+(?:disable|enable|install|remove|uninstall)\b(?P<plugin>[^|;&\n]*))")
# A shell command that can write: a redirect, an in-place editor, a copy/move/delete, an interpreter,
# or a Hermes subcommand that changes settings. Anything else that only names the files is a read.
_WRITE_RE = re.compile(r">|\btee\b|\bsed\b[^|;&]*\s-i|\b(rm|mv|cp|truncate|chmod|install|dd|ln)\b|"
                       r"\b(python3?|perl|ruby|node|bash|sh|zsh)\b|" + _HERMES_SET_RE.pattern)


PLUGIN_ROOT = Path(__file__).resolve().parent


def _code_roots(home: Optional[Path]) -> list:
    """The folders this profile loads the plugin from: this file's own, and the profile's plugins/ link."""
    roots = [PLUGIN_ROOT]
    if home is not None:
        link = Path(home) / "plugins" / PLUGIN_DIR
        roots.append(link)
        try:
            roots.append(link.resolve())
        except OSError:
            pass
    return roots


def _home_spellings(home: Optional[Path]) -> list:
    """How a command may name this profile's home: its path, $HERMES_HOME (the agent's shell has it set
    to this home), and the ~ form."""
    if home is None:
        return []
    home = Path(home)
    names = {str(home), "$hermes_home", "${hermes_home}"}
    try:
        names.add(str(home.resolve()))
        names.add("~/" + str(home.resolve().relative_to(Path.home().resolve())))
    except (OSError, ValueError, RuntimeError):
        pass
    return [n.lower() for n in names]


def _is_plugin_code(path: Path, home: Optional[Path]) -> bool:
    """A path inside the plugin copy this profile loaded: its guardrails are code, so editing them is a
    settings change. Measured 2026-11-09: a forgebruecke-dev chat patched tools/repos.py to widen a tool
    schema. Another checkout of the plugin (a developer's worktree) is not the loaded code."""
    if not path.is_absolute():  # relative to a working directory this hook cannot see: fail closed
        return f"plugins/{PLUGIN_DIR}/" in f"{path.as_posix()}/"
    candidates = [path]
    try:
        candidates.append(path.resolve())
    except OSError:
        pass
    for p in candidates:
        for root in _code_roots(home):
            try:
                p.relative_to(root)
                return True
            except ValueError:
                continue
    return False


def _code_markers(home: Optional[Path]) -> list:
    return ([str(r).lower() for r in _code_roots(home)]
            + [f"{h}/plugins/{PLUGIN_DIR}" for h in _home_spellings(home)])


def _names_settings_file(lowered: str, home: Optional[Path]) -> bool:
    """Whether a command names this profile's config.yaml or .env. Without a known home, any mention."""
    if home is None:
        return any(n in lowered for n in _SETTINGS_FILES)
    if _BARE_FILE_RE.search(lowered):
        return True
    return any(f"{h}/{n}" in lowered for h in _home_spellings(home) for n in _SETTINGS_FILES)


def _hermes_changes_here(lowered: str, home: Optional[Path]) -> bool:
    """Whether a `hermes` call changes this profile's settings or this plugin: `-p <another profile>` and
    a plugins verb on another plugin do not."""
    here = _mod("profile").current_name(home) if home is not None else ""
    for m in _HERMES_CALL_RE.finditer(lowered):
        if here and m.group("profile") and m.group("profile") != here:
            continue
        args = m.group("plugin")
        if args is not None:
            names = [a for a in args.split() if not a.startswith("-")]
            if names and not any(PLUGIN_DIR in a for a in names):
                continue
        return True
    return False


def settings_touch(tool_name: str, args: Any, home: Optional[Path]) -> Optional[str]:
    """What a tool call would change in this profile's guardrails, or None. A heuristic over the call's
    text: it catches the direct routes (editing config.yaml or .env, `hermes config set`, toggling the
    plugin), not a determined script, which is why Hermes' own approvals stay on for the shell."""
    if tool_name not in GUARDED_TOOLS or not isinstance(args, dict):
        return None
    if tool_name in ("write_file", "patch"):
        raw = str(args.get("path") or "")
        if not raw:
            return None
        path = Path(raw).expanduser()
        if _is_plugin_code(path, home):
            return f"edits Forgebrücke's own code ({raw})"
        names = set(_SETTINGS_FILES)
        if home is not None and path.is_absolute():
            if path.resolve() in {(home / n).resolve() for n in names}:
                return f"edits {path}"
            return None
        return f"edits {raw}" if path.name in names else None
    text = str(args.get("command") or args.get("code") or "")
    lowered = text.lower()
    if tool_name == "terminal" and (not _WRITE_RE.search(_HARMLESS_REDIRECT_RE.sub(" ", lowered)) or "--help" in lowered):
        return None  # reading the settings (cat, grep, hermes config get) is fine
    if any(marker in lowered for marker in _code_markers(home)):
        return "runs a command that can change Forgebrücke's own code"
    if _names_settings_file(lowered, home) or any(key in lowered for key in _SETTINGS_KEYS):
        return "runs a command that touches config.yaml, .env or the plugin's settings"
    if "hermes" in lowered and _hermes_changes_here(lowered, home):
        return "changes Hermes settings through the hermes command"
    return None


def make_settings_guard(run_context: Callable[[], RunContext], enabled_here: Callable[[], bool],
                        home: Callable[[], Optional[Path]]):
    def forgebruecke_settings_guard(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any):
        if tool_name not in GUARDED_TOOLS:
            return None
        try:
            if not enabled_here():
                return None
            what = settings_touch(tool_name, args, home())
        except Exception:
            return None
        if not what:
            return None
        reason = run_context().unattended_reason()
        if reason:
            return _block(_refusal("guardrail", f"This call {what}, which can change Forgebrücke's own rules, and "
                                                f"{reason}, so no one can approve it.", "writes"))
        return {"action": "approve", "rule_key": f"{RULE_NS}settings:{tool_name}",
                "message": (f"This call {what}. That can change Forgebrücke's own guardrails (which repos it may "
                            "write to, which tools a mention may use, what a tool accepts). Approve only if you "
                            "asked for it.")}

    return forgebruecke_settings_guard
