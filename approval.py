"""The agent lane for writes: Hermes' approval gate, one-use tickets, and the unattended refusal.

Three lanes: the user's click (the desk), the agent's tool call (this module), unattended runs
(refused here until a flow names a write for its stage).

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

import fnmatch
import hashlib
import importlib
import json
import os
import re
import shlex
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
                 unknown: bool = False, chat_id: str = "", source: str = "", *,
                 origin_unknown: bool = False) -> None:
        self.cron, self.single_query, self.platform = cron, single_query, (platform or "").strip().lower()
        self.source = (source or "").strip().lower()
        self.origin_unknown = origin_unknown  # the session's surface could not be read: who started the turn?
        self.unknown = unknown or origin_unknown  # no telling whether a person is present: fail closed
        self.chat_id = chat_id or ""

    def lane(self) -> "tuple[str, str]":
        """``(lane, scope_repo)`` for reads (access.py): a mention turn reads only its thread's repo."""
        if self.source == REVIEW_SOURCE or self.origin_unknown:
            return "mention", ""  # no thread repo, so the turn reads nothing (load_access refuses)
        if self.platform == MENTION_PLATFORM:
            return "mention", _mod("access").thread_repo(self.chat_id)
        return "agent", ""

    def unattended_reason(self) -> Optional[str]:
        if self.unknown:
            return ("Forgebrücke cannot tell whether a person is present (it cannot read this Hermes "
                    "session; update Forgebrücke)")
        if self.cron:
            return "this is a cron job"
        if self.single_query:
            return "this is a single-query run (hermes chat -q or -z)"
        if self.source == REVIEW_SOURCE:
            return "this turn is a Forgebrücke review worker"
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
    origin_unknown = False
    try:
        platform = ctx.session_surface() or _session_env("HERMES_SESSION_PLATFORM")
    except Exception:
        platform = _session_env("HERMES_SESSION_PLATFORM")
        origin_unknown = not platform
    return RunContext(cron, single, platform, unknown=finite is None, chat_id=_session_env("HERMES_SESSION_CHAT_ID"),
                      source=REVIEW_SOURCE if _review_worker() else _session_env("HERMES_SESSION_SOURCE"),
                      origin_unknown=origin_unknown)


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
        return _refusal("unattended", f"A write needs a person to approve it, and {reason}. Nothing was sent. "
                                      "Reads still work.")
    return None


def _block(body: dict) -> dict:
    text = f"refused ({body['error']}): {body['message']}"
    if body.get("setting"):
        text += f" Setting: {body['setting']}."
    if "Nothing was sent" not in text:
        text += " Nothing was sent to Forgejo."
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
        # "Always" in Hermes covers every later call with this key, so the key names the action too.
        action = p.extra.get("action")
        rule_key = f"{RULE_NS}{tool_name}:{action}:{p.repo}" if action else f"{RULE_NS}{tool_name}:{p.repo}"
        tickets.issue(key, p.signature(), rule_key)
        return {"action": "approve", "message": p.approval_text("Hermes' account"),
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
    except _mod("client").ForgejoError as e:
        tickets.revoke(key)
        return _refusal(e.kind, e.message)
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
        return _refusal("not_approved", "The write changed between the approval and now (Forgejo changed in between); "
                                        "nothing was sent. Ask again.")
    return spec.execute(access, p) if spec is not None else write.execute(access, p)


# --- The agent may not quietly loosen its own guardrails ----------------------------------------------
#
# The guard watches this profile's own settings: its config.yaml and .env, the plugin copy it loaded,
# `hermes` commands that change this profile or this plugin, and the page's route that saves a setting.
# The same names elsewhere (another profile's config, a repo's .env, a checkout of this plugin that is
# not the loaded one, another plugin) are not its business, so a profile that develops code, Forgebrücke
# included, can keep the plugin on.
#
# A shell command is judged in two steps. First: can it write? It cannot when every part of its pipeline is
# a reader (_READERS, _SUBCOMMAND_READERS) without a writing argument and nothing is redirected into a file.
# One part that may write makes the whole command one that may write. Code for execute_code is never a
# reader. Second, only for a command that may write: where do the names in it point (_named)?

GUARDED_TOOLS = ("write_file", "patch", "terminal", "execute_code")
PLUGIN_DIR = "forgebruecke"
# Hermes names the data folder after the plugin, or `agent-plugin-<name>-<hash>` for a portable plugin.
_DATA_FOLDERS = (f"plugin-data/{PLUGIN_DIR}", f"plugin-data/agent-plugin-{PLUGIN_DIR}-")
_SETTINGS_FILES = ("config.yaml", ".env")
_SETTINGS_KEYS = ("plugins.entries.forgebruecke", "platform_toolsets", "approvals.mode")
# A word of a command that may be a path. `=`, `:`, quotes and brackets end it, so `--env-file=.env` and
# `open('.env')` give `.env`, while `process.env` and `.env.example` stay whole and are other names. Glob
# characters and the comma of a brace list stay in the word; _words expands braces and _named matches globs.
_PATH_WORD_RE = re.compile(r"[\w.~${}/+@%*?,-]+")
_GLOB_CHARS = frozenset("*?[")
_BRACE_RE = re.compile(r"(?<!\$)\{([^{}]*,[^{}]*)\}")
_HARMLESS_REDIRECT_RE = re.compile(r"\d*>&\d+|\d*>\s*/dev/null")
# None of these can change a file, unless an argument matches the pattern beside the name.
_READERS: dict = {
    **dict.fromkeys(("cat", "head", "tail", "less", "grep", "rg", "ls", "stat", "wc", "diff", "file",
                     "cd", "pwd", "echo", "test", "[",
                     "cut", "tr", "nl", "jq", "du")),
    "find": re.compile(r"-(?:delete|exec|execdir|ok|okdir|fprint0?|fprintf|fls)"),
    "sort": re.compile(r"-[a-zA-Z]*o.*", re.S),
    "uniq": re.compile(r"[^-].*", re.S),  # reads only as a filter: a second file name would be its output
    "sed": re.compile(r"-[a-zA-Z]*[if].*|.*(?:^|[^\w.-])[wWe](?:\s+\S|$).*", re.S),
    "awk": re.compile(r"-[if].*|.*(?:>|\||system).*", re.S),
}
# Long options that write, per command. GNU tools take any unambiguous start of a long option (`sed --in`
# is `--in-place`), so an argument counts when its name is the start of one of these.
_LONG_WRITERS: dict = {
    "sed": ("in-place", "file"),
    "sort": ("output",),
    "awk": ("include", "file", "load"),
    "git": ("output", "open-files-in-pager", "config-env", "git-dir", "work-tree"),
    "docker-compose": ("output",),
}
# These read only with one of the named subcommands: (their options that take a value, the subcommands, a
# writing argument). `git add` and `git commit` write inside .git only, unless --git-dir or --work-tree
# move it; `git -c` can name a program to run.
_SUBCOMMAND_READERS: dict = {
    "git": (frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"}),
            frozenset({"status", "diff", "log", "show", "grep", "blame", "ls-files", "ls-tree", "rev-parse",
                       "describe", "cat-file", "shortlog", "check-ignore", "add", "commit"}),
            re.compile(r"-O.*|-c", re.S)),
    "docker-compose": (frozenset({"-f", "--file", "-p", "--project-name", "--project-directory", "--profile",
                                  "--env-file", "--ansi", "--progress", "--parallel"}),
                       frozenset({"config", "ps", "logs", "ls", "images", "top", "version"}),
                       re.compile(r"-o", re.S)),
}
_SHELL_OPERATORS = frozenset("();<>|&")
_QUOTES = str.maketrans("", "", "'\"")
# `$HERMES_HOME`, `${HERMES_HOME}` and the forms with a default or a check (`${HERMES_HOME:-...}`, `:?`, `:=`):
# each is the variable's value whenever it is set, which in the agent's shell it is.
_SHELL_VAR_RE = re.compile(r"\$(\{)?(HERMES_HOME|HOME|OLDPWD|PWD)(?(1)(?::?[-=?][^}]*)?\}|(?![A-Za-z0-9_]))")
# A change of folder anywhere in the text: after one, the folder the call started in proves nothing. `-C` with
# a relative folder (`git -C ../x`, `make -C sub`) moves too; with an absolute one its words are read there.
_MOVE_RE = re.compile(r"(?<![\w./-])(?:cd|pushd|popd)(?![\w./-])|OLDPWD|(?<![\w-])-C(?!\s*['\"]?[/~$])")
_LEADING_CD_RE = re.compile(r"\s*cd\s+(\S+)\s*&&")
# After one of these a part of the command runs although the `cd` before it failed, or in another shell.
_RUNS_ANYWAY_RE = re.compile(r"[;\n()]|\|\||(?<![&>])&(?![&>])")
# Between `hermes` and its verb: flags, each with at most one value (`--yolo`, `-p coding`).
_HERMES_FLAGS = r"(?P<flags>(?:\s+--?\w[\w-]*(?:=\S+)?(?:\s+[^\s-]\S*)?)*)\s+"
_HERMES_READ_RE = re.compile(r"hermes" + _HERMES_FLAGS
                             + r"(?:config\s+(?:get|show|path|env-path)|plugins\s+(?:list|show))\b")
# One `hermes` call that changes settings: the profile it names (if any), and for a plugins or tools verb its
# arguments. `plugins update` replaces the loaded code; `tools enable` sets what a platform's turns may use.
_HERMES_CALL_RE = re.compile(r"\bhermes\b" + _HERMES_FLAGS + r"(?:config\s+(?:set|unset|edit|migrate|reset)\b|"
                             r"plugins\s+(?:disable|enable|install|remove|uninstall|update)\b(?P<plugin>[^|;&\n]*)|"
                             r"tools\s+(?:enable|disable)\b(?P<tools>[^|;&\n]*))")
_PROFILE_FLAG_RE = re.compile(r"(?:-p|--profile)(?:\s+|=)([\w.-]+)")
_MENTION_TOOLS_RE = re.compile(r"--platform(?:\s+|=)" + MENTION_PLATFORM + r"\b")
# The page's routes that change something on the user's click (dashboard/plugin_api.py): save a setting, merge
# or comment with the user's own token, create a reviewer profile, restart the profile's plugin host, park and
# unpark a pull request. `/settings` and `/desk` only read.
_PAGE_ROUTE_RE = re.compile(rf"/api/plugins/{PLUGIN_DIR}/"
                            r"(setting|merge|comment|reviewer-profile|reload-tokens|park|unpark)(?![\w-])")


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


def _is_plugin_code(path: Path, home: Optional[Path]) -> bool:
    """A path inside the plugin copy this profile loaded: its guardrails are code, so editing them is a
    settings change (a chat once patched tools/repos.py to widen a tool's schema). Another checkout of
    the plugin (a developer's worktree) is not the loaded code."""
    if not path.is_absolute():  # relative to a working directory this hook cannot see: fail closed
        return f"plugins/{PLUGIN_DIR}/" in f"{path.as_posix()}/"
    candidates = [path]
    try:
        candidates.append(path.resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    for p in candidates:
        for root in _code_roots(home):
            try:
                p.relative_to(root)
                return True
            except ValueError:
                continue
    return False


def _variants(path: Path) -> set:
    """A path as written and with its links resolved."""
    out = {os.path.normpath(str(path))}
    try:
        out.add(str(path.resolve()))
    except (OSError, RuntimeError, ValueError):  # ValueError: a NUL byte the system refuses
        pass
    return out


def _spellings(path: Path) -> set:
    """A path as written and with its links resolved, lowercased (a Mac volume ignores case)."""
    return {v.lower() for v in _variants(path)}


def _is_plugin_data(path: Path, home: Optional[Path]) -> bool:
    """A path in the plugin's data folder: flow overrides, the pause list and review verdicts live there,
    and the tools that change them ask first. A relative path may start in the profile: fail closed."""
    if home is None or not path.is_absolute():
        markers = list(_DATA_FOLDERS)
    else:
        markers = [f"{h}/{d}" for h in _spellings(Path(home)) for d in _DATA_FOLDERS]
    return any(marker in spelling for spelling in _spellings(path) for marker in markers)


def _expand_vars(text: str, home: Path) -> str:
    """`$HERMES_HOME` and `$HOME` written out (also with a default, `${HERMES_HOME:-...}`). `$PWD` and `$OLDPWD`
    become `.`, a relative path like any other (a move of folder already makes the start folder unknown)."""
    values = {"HERMES_HOME": str(home), "HOME": str(Path.home()), "PWD": ".", "OLDPWD": "."}
    return _SHELL_VAR_RE.sub(lambda m: values[m.group(2)], text)


def _expand(word: str, home: Path) -> str:
    return os.path.expanduser(_expand_vars(word, home))


def _start_folder(command: str, workdir: Any, home: Path) -> str:
    """The folder every part of a shell command runs in, or "" when its text cannot tell.

    That is the call's absolute workdir when nothing in the text changes folder. One `cd` may lead: in
    `cd <folder> && ...` the rest runs only there, as long as no `;`, newline, `||`, single `&` or bracket
    follows, after which a part runs although the `cd` failed."""
    folder = os.path.expanduser(str(workdir or ""))
    moves = len(_MOVE_RE.findall(command))
    if moves:
        lead = _LEADING_CD_RE.match(command)
        if not lead or moves > 1 or _RUNS_ANYWAY_RE.search(command, lead.end()):
            return ""
        target = _expand(lead.group(1).translate(_QUOTES), home)
        if target.startswith(("-", "~")) or "$" in target or "`" in target:
            return ""
        folder = os.path.join(folder, target)
    return os.path.normpath(folder) if os.path.isabs(folder) else ""


def _relative_data(word: str) -> bool:
    return any(d in os.path.normpath(word).lower() for d in _DATA_FOLDERS)


def _braces(word: str, limit: int = 64) -> list:
    """A word with its brace lists written out, as the shell does (`config.yaml{.new,}`)."""
    out, todo = [], [word]
    while todo and len(out) + len(todo) <= limit:
        w = todo.pop()
        m = _BRACE_RE.search(w)
        if not m:
            out.append(w)
            continue
        todo.extend(w[:m.start()] + alt + w[m.end():] for alt in m.group(1).split(","))
    return out + todo


def _words(flat: str) -> set:
    """The path words of a command: braces written out, commas split, `~` expanded. `{}` belong to a word only
    for `${...}`, already written out; a brace at an end is awk's or find's (`{print > "config.yaml"}`)."""
    out = set()
    for word in set(_PATH_WORD_RE.findall(flat)):
        for whole in _braces(word):
            for part in whole.split(","):
                part = part.strip("{}")
                if part:
                    out.add(os.path.expanduser(part))
    return out


class _Targets:
    """This profile's guarded places, as lowercased spellings: the settings files, the data folders, the loaded
    code, and every folder above one of them (a `rm -rf`, `tar -C` or `rsync` into such a folder replaces what
    lies below). The root folder is left out: Hermes' own approvals cover a command on `/`."""

    def __init__(self, home: Path) -> None:
        data = [home / "plugin-data" / PLUGIN_DIR]
        try:
            data += sorted((home / "plugin-data").glob(f"agent-plugin-{PLUGIN_DIR}-*"))
        except (OSError, ValueError):
            pass
        self.files = set().union(*(_spellings(home / n) for n in _SETTINGS_FILES))
        self.data = set().union(*(_spellings(d) for d in data))
        self.code = set().union(*(_spellings(r) for r in _code_roots(home)))
        self.holders = {str(parent) for s in self.files | self.data | self.code
                        for parent in Path(s).parents if str(parent) != "/"}

    def glob(self, pattern: str) -> set:
        """Which kinds a glob pattern (absolute, lowercased) can name."""
        kinds = (("settings", self.files), ("data", self.data), ("code", self.code), ("holder", self.holders))
        return {kind for kind, spellings in kinds if any(fnmatch.fnmatchcase(s, pattern) for s in spellings)}


def _named(text: str, home: Optional[Path], workdir: Any = None) -> Optional[str]:
    """What of this profile a command or script names: "code", "data", "settings", "holder" or None.

    A known start folder is exact: a relative word means start/word. Every absolute word is a folder too
    (`git -C`, `cd`), so a relative word is also read against each. An unknown start folder may be the
    profile, so a relative name the settings path ends with counts, and so does a relative path into the
    plugin's data folder. A relative path into the plugin's code from an unknown folder does not: that is
    every test run and `git add` of a checkout of this plugin. The start folder itself is no holder: a
    command may work in the profile folder without touching what the guard watches."""
    flat = text.translate(_QUOTES)
    if home is None:  # any profile's
        low = flat.lower()
        return ("code" if str(PLUGIN_ROOT).lower() in low else "data" if any(f in low for f in _DATA_FOLDERS)
                else "settings" if any(n in low for n in _SETTINGS_FILES) else None)
    home = Path(home)
    start = _start_folder(text, workdir, home)
    words = _words(_expand_vars(flat, home)) | ({start} if start else set())
    plain = {w for w in words if not _GLOB_CHARS.intersection(w)}
    variants = {w: _variants(Path(w)) for w in plain if os.path.isabs(w)}
    folders = set().union(*variants.values())
    targets = _Targets(home)
    at_start = _spellings(Path(start)) if start else set()
    hints = (PLUGIN_DIR, PLUGIN_ROOT.name.lower())
    found = set()

    # A folder only `mkdir` names is made, not replaced (`mkdir -p $HERMES_HOME/plugins` before linking another
    # plugin). Named by any other part of the command too, it counts.
    made: dict = {}
    for part in _pipeline(text) or []:
        for arg in part[1:]:
            key = os.path.normpath(_expand(arg, home)).lower()
            made[key] = made.get(key, True) and part[0].rpartition("/")[2] == "mkdir"
    only_made = {k for k, v in made.items() if v}

    def holder(spellings: set) -> bool:
        return bool(spellings & targets.holders) and not spellings & at_start and not spellings & only_made

    for word in words:
        low = os.path.normpath(word).lower()
        if word not in plain:  # a glob: match it against every guarded place
            bases = [""] if os.path.isabs(word) else folders
            for base in bases:
                found |= targets.glob(os.path.normpath(os.path.join(base, word)).lower())
            if not start and not os.path.isabs(word):
                tail = "*/" + low.replace("../", "")
                found |= {"settings"} if any(fnmatch.fnmatchcase(f, tail) for f in targets.files) else set()
            continue
        if word in variants:
            path = Path(word)
            spellings = {v.lower() for v in variants[word]}
            found.update(kind for kind, hit in (
                ("code", _is_plugin_code(path, home)), ("data", _is_plugin_data(path, home)),
                ("settings", bool(spellings & targets.files)), ("holder", holder(spellings))) if hit)
            continue
        first = low.split("/", 1)[0]
        if not (os.path.basename(low) in _SETTINGS_FILES or any(h in low for h in hints)
                or first in ("plugin-data", "plugins") or set(low.split("/")) <= {".", ".."}):
            continue
        places = [Path(os.path.join(folder, word)) for folder in folders]
        tail = "/" + low.replace("../", "")
        found.update(kind for kind, hit in (
            ("code", any(_is_plugin_code(p, home) for p in places)),
            ("data", any(_is_plugin_data(p, home) for p in places) or (not start and _relative_data(word))),
            ("settings", any(_spellings(p) & targets.files for p in places)
             or (not start and any(f.endswith(tail) for f in targets.files))),
            ("holder", any(holder(_spellings(p)) for p in places))) if hit)
    return next((kind for kind in ("code", "data", "settings", "holder") if kind in found), None)


def _names_settings_key(lowered: str, tool_name: str) -> bool:
    """In a shell command a settings key alone is only a word (a commit message, a grep). It counts beside the
    name of a settings file. In code it counts alone, as code is never judged a reader."""
    return (any(key in lowered for key in _SETTINGS_KEYS)
            and (tool_name != "terminal" or any(name in lowered for name in _SETTINGS_FILES)))


def _hermes_changes_here(lowered: str, home: Optional[Path]) -> bool:
    """Whether a `hermes` call changes this profile's settings or this plugin: `-p <another profile>`, a
    plugins verb on another plugin and a tools verb on another platform do not."""
    here = _mod("profile").current_name(home) if home is not None else ""
    for m in _HERMES_CALL_RE.finditer(lowered):
        profile = _PROFILE_FLAG_RE.search(m.group("flags"))
        if here and profile and profile.group(1) != here:
            continue
        tools = m.group("tools")
        if tools is not None and not _MENTION_TOOLS_RE.search(tools):
            continue
        args = m.group("plugin")
        if args is not None:
            names = [a for a in args.split() if not a.startswith("-")]
            if names and not any(PLUGIN_DIR in a for a in names):
                continue
        return True
    return False


def _pipeline(command: str) -> Optional[list]:
    """The command's parts between `|`, `;` and `&&`, each a list of words. None when it redirects into a
    file, substitutes a command, opens a subshell or cannot be split."""
    text = _HARMLESS_REDIRECT_RE.sub(" ", command).replace("\\\n", " ").replace("\n", " ; ")
    if "`" in text or "$(" in text:
        return None
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""  # shlex would cut `a#b; sed -i ...` at the `#`, the shell does not
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    parts: list = [[]]
    for token in tokens:
        if not _SHELL_OPERATORS.issuperset(token):
            parts[-1].append(token)
        elif _SHELL_OPERATORS.intersection(token) - {"|", ";", "&"}:
            return None
        else:
            parts.append([])
    return [p for p in parts if p]


def _subcommand(args: list, valued: frozenset) -> str:
    """The first argument that is neither an option nor an option's value."""
    skip = False
    for arg in args:
        if skip or arg.startswith("-"):
            skip = not skip and arg in valued
            continue
        return arg
    return ""


def _part_reads(words: list) -> bool:
    """Whether one part of a pipeline can only read: a reader without a writing argument, `hermes config get`,
    or a `hermes` call that only prints its help."""
    name, args = words[0].rpartition("/")[2], words[1:]
    if name == "hermes":
        call = " ".join(["hermes", *args]).lower()
        return bool(_HERMES_READ_RE.match(call)) or ("--help" in args and "--" not in args)
    if name == "docker" and args[:1] == ["compose"]:
        name, args = "docker-compose", args[1:]
    if name in _SUBCOMMAND_READERS:
        valued, subcommands, writes = _SUBCOMMAND_READERS[name]
        if _subcommand(args, valued) not in subcommands:
            return False
    elif name in _READERS:
        writes = _READERS[name]
    else:
        return False
    if _long_writes(args, _LONG_WRITERS.get(name, ())):
        return False
    return writes is None or not any(writes.fullmatch(arg) for arg in args)


def _long_writes(args: list, longs: tuple) -> bool:
    """Whether an argument is a writing long option or the start of one (`--in` for `--in-place`)."""
    for arg in args:
        if arg.startswith("--") and len(arg) > 2:
            given = arg[2:].split("=", 1)[0]
            if any(option.startswith(given) for option in longs):
                return True
    return False


def _only_reads(command: str) -> bool:
    """Whether a shell command can only read: every part of its pipeline reads and nothing is redirected into
    a file. Judging part by part would let `ls .env | xargs rm` pass."""
    parts = _pipeline(command)
    return parts is not None and all(_part_reads(words) for words in parts)


# Each kind of thing a call can change, in the order the guard names them, with its sentence.
_WHAT = {
    "code": "runs a command that can change Forgebrücke's own code",
    "data": "runs a command that can change Forgebrücke's own data",
    "settings": "runs a command that touches config.yaml, .env or the plugin's settings",
    "hermes": "changes Hermes settings through the hermes command",
    "setting": "saves a Forgebrücke setting through its page's route",
    "merge": "merges a pull request through Forgebrücke's page route, with your own token",
    "comment": "posts a comment as you through Forgebrücke's page route, with your own token",
    "park": "parks a pull request on Forgebrücke's desk through its page route",
    "unpark": "brings a parked pull request back through Forgebrücke's page route",
    "reviewer-profile": "creates a reviewer profile through Forgebrücke's page route",
    "reload-tokens": "restarts this profile's plugin host through Forgebrücke's page route",
    "holder": "runs a command that can change a folder that holds Forgebrücke's settings, data or code",
}
_HINTS = (PLUGIN_DIR, PLUGIN_ROOT.name.lower(), "plugin-data", "hermes", *_SETTINGS_FILES, *_SETTINGS_KEYS)


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
        if _is_plugin_data(path, home):
            return f"edits Forgebrücke's own data ({raw})"
        names = set(_SETTINGS_FILES)
        if home is not None and path.is_absolute():
            if _spellings(path) & set().union(*(_spellings(home / n) for n in names)):
                return f"edits {path}"
            return None
        return f"edits {raw}" if path.name in names else None
    text = str(args.get("command") or args.get("code") or "")
    workdir = args.get("workdir") if tool_name == "terminal" else None
    lowered = text.lower()
    # One cheap pass first: a call with no hint word and no folder to stand in names nothing guarded.
    if not workdir and not any(h in lowered for h in _HINTS) and not _MOVE_RE.search(text):
        return None
    if tool_name == "terminal" and _only_reads(text):
        return None
    kind = _named(text, home, workdir)
    if kind is None and _names_settings_key(lowered, tool_name):
        kind = "settings"
    if kind is None and "hermes" in lowered and _hermes_changes_here(lowered, home):
        kind = "hermes"
    if kind is None:
        route = _PAGE_ROUTE_RE.search(lowered)
        kind = route.group(1) if route else None
    return _WHAT[kind] if kind else None


def make_settings_guard(run_context: Callable[[], RunContext], enabled_here: Callable[[], bool],
                        home: Callable[[], Optional[Path]]):
    """Hermes skips a hook that raises, so nothing here may raise: a check that fails asks, and a run
    context that cannot be read counts as nobody there."""
    def forgebruecke_settings_guard(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any):
        if tool_name not in GUARDED_TOOLS:
            return None
        try:
            here = home()
        except Exception:
            here = None  # any profile's settings count
        try:
            what = settings_touch(tool_name, args, here)
        except Exception:
            what = "could not be checked by Forgebrücke's settings guard"
        if not what:
            return None
        try:
            if not enabled_here():  # after the text check: enabled_here parses the profile's YAML
                return None
        except Exception:
            pass  # the guard stays on
        try:
            reason = run_context().unattended_reason()
        except Exception:
            reason = RunContext(unknown=True).unattended_reason()
        if reason:
            return _block(_refusal("guardrail", f"This call {what}, which can change Forgebrücke's own rules, and "
                                                f"{reason}, so no one can approve it.", "writes"))
        given = args if isinstance(args, dict) else {}
        subject = str(given.get("path") or given.get("command") or given.get("code") or "")
        digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:12]
        return {"action": "approve", "rule_key": f"{RULE_NS}settings:{tool_name}:{digest}",
                "message": (f"This call {what}. That can change Forgebrücke's own guardrails (which repos it may "
                            "write to, which tools a mention may use, what a tool accepts). Approve only if you "
                            "asked for it.")}

    return forgebruecke_settings_guard
