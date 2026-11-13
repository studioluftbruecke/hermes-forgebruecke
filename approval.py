"""The agent lane for writes: Hermes' approval gate, one-use tickets, and the unattended refusal.

Three lanes: the user's click (the desk), the agent's tool call (this module), unattended runs
(refused here until a flow names a write for its stage).

The ``pre_tool_call`` hook either blocks with a sentence, or returns Hermes' public directive
``{"action": "approve", "message", "rule_key"}`` so Hermes asks the human with the plan's text. The
plugin never approves anything itself. Hermes skips a hook that raises, so the hook leaves a
one-use ticket for exactly the plan it sent to the gate, and the handler refuses a call without
one. A denied or timed-out approval revokes the ticket (``post_approval_response``). With
``dry_run`` on, the hook lets the call through without asking and the handler only previews.

Restated from Hermes' own rule (not a public import): a run is unattended when it is a cron job, a
single-query ``hermes chat -q``/``-z`` run, or a session on the webhook, msgraph_webhook or api_server
surface, or on this plugin's own mention channel. Inside Hermes' plugin host (``plugins.isolation:
host``) the process environment is the host's, so a command-line surface counts as unattended there.
When Hermes cannot say whether a turn is a single-query one, writes are refused. ``hermes -z``
auto-approves inside Hermes' gate, which is why this check runs first.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib
import itertools
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


HOST_ONESHOT_SURFACES = frozenset({"", "cli", "oneshot"})  # what a `-z` or `-q` turn reports in the plugin host


class RunContext:
    def __init__(self, cron: bool = False, single_query: bool = False, platform: str = "",
                 unknown: bool = False, chat_id: str = "", source: str = "", *,
                 origin_unknown: bool = False, host_oneshot: bool = False) -> None:
        self.cron, self.single_query, self.platform = cron, single_query, (platform or "").strip().lower()
        self.source = (source or "").strip().lower()
        self.origin_unknown = origin_unknown  # the session's surface could not be read: who started the turn?
        self.version_silent = unknown  # this Hermes does not say whether the turn is a single-query one
        self.unknown = unknown or origin_unknown  # no telling whether a person is present: fail closed
        self.host_oneshot = host_oneshot
        self.chat_id = chat_id or ""

    def lane(self) -> "tuple[str, str]":
        """``(lane, scope_repo)`` for reads (access.py): a mention turn reads only its thread's repo."""
        if self.source == REVIEW_SOURCE or self.origin_unknown:
            return "mention", ""  # no thread repo, so the turn reads nothing (load_access refuses)
        if self.platform == MENTION_PLATFORM:
            return "mention", _mod("access").thread_repo(self.chat_id)
        return "agent", ""

    def unattended_reason(self) -> Optional[str]:
        if self.origin_unknown:
            return "Forgebrücke cannot tell whether a person is present (it cannot read who started this turn)"
        if self.version_silent:
            return ("Forgebrücke cannot tell whether a person is present: this Hermes version does not say, so "
                    "writes are refused (update Forgebrücke)")
        if self.host_oneshot:
            return ("this turn runs in Hermes' plugin host from the command line, where Forgebrücke cannot see "
                    "whether a person is present (plugins.isolation: in_process brings the approval card back)")
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

    If it moves, writes are refused rather than left to Hermes' gate, which auto-approves single-query runs."""
    try:
        # Private on purpose: Hermes 0.23.1 has no public signal for a gateway-admitted `chat -q`/`-z` turn.
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
    surface: Optional[str] = None
    try:
        surface = str(ctx.session_surface() or "").strip().lower()
        platform = surface or _session_env("HERMES_SESSION_PLATFORM")
    except Exception:
        platform = _session_env("HERMES_SESSION_PLATFORM")
        origin_unknown = not platform
    # In the plugin host the environment is the host's: only the surface ctx forwards tells a `-z` turn apart.
    in_host = os.environ.get("HERMES_PLUGIN_HOST_PROCESS") == "1"
    host_oneshot = in_host and (surface is None or surface in HOST_ONESHOT_SURFACES)
    return RunContext(cron, single, platform, unknown=finite is None, chat_id=_session_env("HERMES_SESSION_CHAT_ID"),
                      source=REVIEW_SOURCE if _review_worker() else _session_env("HERMES_SESSION_SOURCE"),
                      origin_unknown=origin_unknown, host_oneshot=host_oneshot)


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


_STANDING_PLANS = frozenset({("forgejo_flows", "set"), ("forgejo_flows", "resume"), ("forgejo_repos", "preset")})


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
        # "Always" in Hermes covers every later call with this key, so the key names the action too. A flow and
        # a protection preset decide what runs unattended later, so their key also names the plan itself.
        action = p.extra.get("action")
        rule_key = f"{RULE_NS}{tool_name}:{action}:{p.repo}" if action else f"{RULE_NS}{tool_name}:{p.repo}"
        if (tool_name, action) in _STANDING_PLANS:
            plan = json.dumps({"extra": p.extra, "text": p.preview}, sort_keys=True, default=str)
            rule_key += ":" + hashlib.sha256(plan.encode("utf-8")).hexdigest()[:12]
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
# reader. Second, only for a command that may write: where do the names in it point (_named)? A relative name
# is read in the folder the call starts in (SessionFolders); when that folder is unknown, it may be the profile.
#
# A folder above a guarded place (a holder: the profile folder, the gateway's HOME, a checkout the profile loads
# the plugin from) counts in two cases. A command replaces it: the folder of `tar -x -C`/`unzip -d`, the target
# of `cp -r` and `rsync`, what `rm`, `mv` or `find -delete` remove (_replaced), or the start folder itself when
# `tar -x` names no folder. Or the command is read the strict way, as the released guard read every command
# with a `workdir`, a change of folder or one of its words (_HINTS: hermes, forgebruecke, plugin-data,
# config.yaml, .env, a settings key): then any holder it names counts. A holder that is only a read source or
# a tool's working folder (`git -C`, `make -C`, `npm --prefix`) in a command without those words does not.

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
    "yq": re.compile(r"-[a-zA-Z]*[is].*", re.S),  # -i edits in place, -s splits into files
}
# Long options that write, per command. GNU tools take any unambiguous start of a long option (`sed --in`
# is `--in-place`), so an argument counts when its name is the start of one of these.
_LONG_WRITERS: dict = {
    "sed": ("in-place", "file"),
    "sort": ("output",),
    "awk": ("include", "file", "load"),
    "git": ("output", "open-files-in-pager", "config-env", "git-dir", "work-tree"),
    "docker-compose": ("output",),
    "yq": ("inplace", "split-exp"),
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
_CD_RE = re.compile(r"(?<![\w./-])(?:cd|pushd|popd)(?![\w./-])|OLDPWD")
_MOVE_RE = re.compile(_CD_RE.pattern + r"|(?<![\w-])-C(?!\s*['\"]?[/~$])")
_LEADING_CD_RE = re.compile(r"\s*cd\s+(\S+)\s*(?:&&|$)")
# After one of these a part of the command runs although the `cd` before it failed, or in another shell.
_RUNS_ANYWAY_RE = re.compile(r"[;\n()]|\|\||(?<![&>])&(?![&>])")
# Between `hermes` and its verb: flags, each with at most one value (`--yolo`, `-p coding`).
_HERMES_FLAGS = r"(?P<flags>(?:\s+--?\w[\w-]*(?:=\S+)?(?:\s+[^\s-]\S*)?)*)\s+"
_HERMES_READ_RE = re.compile(r"hermes" + _HERMES_FLAGS
                             + r"(?:config\s+(?:get|show|path|env-path)|plugins\s+(?:list|show))\b")
# One `hermes` call that changes settings: the profile it names (if any), and for a plugins or tools verb its
# arguments. `plugins update` replaces the loaded code; `tools enable` sets what a platform's turns may use;
# `import` restores config.yaml and .env from a backup zip. The module form (`python -m hermes_cli.main`) is the
# same command.
_HERMES_CALL_RE = re.compile(r"(?:\bhermes(?![\w.-])|\bhermes_cli(?:\.main)?(?![\w.]))" + _HERMES_FLAGS
                             + r"(?:import\b|config\s+(?:set|unset|edit|migrate|reset)\b|"
                             r"plugins\s+(?:disable|enable|install|remove|uninstall|update)\b(?P<plugin>[^|;&\n]*)|"
                             r"tools\s+(?:enable|disable)\b(?P<tools>[^|;&\n]*))")
_PROFILE_FLAG_RE = re.compile(r"(?:-p|--profile)(?:\s+|=)([\w.-]+)")
_MENTION_TOOLS_RE = re.compile(r"--platform(?:\s+|=)" + MENTION_PLATFORM + r"\b")
# The page's routes that change something on the user's click (dashboard/plugin_api.py): save a setting, merge
# or comment with the user's own token, create a reviewer profile, reload Forgebrücke's tokens, park and
# unpark a pull request. `/settings` and `/desk` only read.
_PAGE_ROUTE_RE = re.compile(rf"/api/plugins/{PLUGIN_DIR}/"
                            r"(setting|merge|comment|reviewer-profile|reload-tokens|park|unpark)(?![\w-])")


PLUGIN_ROOT = Path(__file__).resolve().parent
# The released guard's words: a shell command that holds one of them is read the strict way (_named_once).
_HINTS = (PLUGIN_DIR, PLUGIN_ROOT.name.lower(), "plugin-data", "hermes", *_SETTINGS_FILES, *_SETTINGS_KEYS)


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


def _user_homes(home: Optional[Path]) -> list:
    """What `~` and `$HOME` may mean in the agent's shell. In a container Hermes gives agent shells the profile's
    own home folder (`<profile>/home`, when it exists); elsewhere they keep the process HOME. Both are checked."""
    homes = [str(Path.home())]
    if home is not None and (Path(home) / "home").is_dir():
        homes.append(str(Path(home) / "home"))
    return homes


def _tilde(word: str, user_home: str) -> str:
    """`~` and `~/...` with ``user_home``; `~user/...` as the system names it."""
    if word == "~" or word.startswith("~/"):
        return user_home + word[1:]
    return os.path.expanduser(word)


def _expand_vars(text: str, home: Path, user_home: str) -> str:
    """`$HERMES_HOME` and `$HOME` written out (also with a default, `${HERMES_HOME:-...}`). `$PWD` and `$OLDPWD`
    become `.`, a relative path like any other (a move of folder already makes the start folder unknown)."""
    values = {"HERMES_HOME": str(home), "HOME": user_home, "PWD": ".", "OLDPWD": "."}
    return _SHELL_VAR_RE.sub(lambda m: values[m.group(2)], text)


def _expand(word: str, home: Path, user_home: str) -> str:
    return _tilde(_expand_vars(word, home, user_home), user_home)


def _start_folder(command: str, workdir: Any, home: Path, user_home: str, moves_re: "re.Pattern" = _MOVE_RE) -> str:
    """The folder every part of a shell command runs in, or "" when its text cannot tell.

    That is the call's absolute workdir when nothing in the text changes folder. One `cd` may lead: in
    `cd <folder> && ...` the rest runs only there, as long as no `;`, newline, `||`, single `&` or bracket
    follows, after which a part runs although the `cd` failed. With ``moves_re=_CD_RE`` it is the shell's
    folder, which a tool's own `-C` does not move."""
    folder = _tilde(str(workdir or ""), user_home)
    moves = len(moves_re.findall(command))
    if moves:
        lead = _LEADING_CD_RE.match(command)
        if not lead or moves > 1 or _RUNS_ANYWAY_RE.search(command, lead.end()):
            return ""
        target = _expand(lead.group(1).translate(_QUOTES), home, user_home)
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


def _words(flat: str, user_home: str) -> set:
    """The path words of a command: braces written out, commas split, `~` expanded. `{}` belong to a word only
    for `${...}`, already written out; a brace at an end is awk's or find's (`{print > "config.yaml"}`)."""
    out = set()
    for word in set(_PATH_WORD_RE.findall(flat)):
        for whole in _braces(word):
            for part in whole.split(","):
                part = part.strip("{}")
                if part:
                    out.add(_tilde(part, user_home))
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
        self.roots = _code_roots(home)

    def glob(self, pattern: str) -> set:
        """Which kinds a glob pattern (absolute, lowercased) can name. As in the shell, `*` and `?` stay inside
        one folder name; a pattern that ends in a folder above a guarded place names that folder."""
        kinds = (("settings", self.files), ("data", self.data), ("code", self.code), ("holder", self.holders))
        parts = pattern.split("/")
        return {kind for kind, spellings in kinds
                if any(_glob_hit(s.split("/"), parts) for s in spellings)}

    def at(self, place: str, home: Path) -> set:
        """Which kinds an absolute path (or glob) names, holders included."""
        if _GLOB_CHARS.intersection(place):
            return self.glob(os.path.normpath(place).lower())
        path, spellings = Path(place), _spellings(Path(place))
        return {kind for kind, hit in (
            ("code", _is_plugin_code(path, home)), ("data", _is_plugin_data(path, home)),
            ("settings", bool(spellings & self.files)), ("holder", bool(spellings & self.holders))) if hit}

    def names(self) -> set:
        """Every file and folder name a guarded place or a holder has, the loaded code's own files included."""
        out = {Path(s).name for s in self.files | self.data | self.code | self.holders}
        for root in self.roots:  # Python's caches below the code are no guardrail
            for _, folders, files in itertools.islice(os.walk(root), 200):
                out.update(n.lower() for n in folders + files if n != "__pycache__" and not n.endswith(".pyc"))
        return out


def _glob_hit(name: list, pattern: list) -> bool:
    return len(name) == len(pattern) and all(fnmatch.fnmatchcase(n, p) for n, p in zip(name, pattern))


def _named(text: str, home: Optional[Path], workdir: Any = None, session: Any = None, shell: bool = False
           ) -> Optional[str]:
    """What of this profile a command or script names, read with each meaning `~` may have in the agent's shell."""
    if home is None:
        return _named_once(text, None, workdir, str(Path.home()))
    found = {_named_once(text, Path(home), workdir, user_home, session, shell) for user_home in _user_homes(home)}
    return next((kind for kind in _KINDS if kind in found), None)


_KINDS = ("code", "data", "settings", "holder")


def _named_once(text: str, home: Optional[Path], workdir: Any, user_home: str, session: Any = None,
                shell: bool = False) -> Optional[str]:
    """What of this profile a command or script names: "code", "data", "settings", "holder" or None.

    A shell command counts for what it replaces (_replaced), and, read the strict way (see _HINTS), for every
    place it names. A known start folder is exact: a relative word means start/word. Every absolute word is a
    folder too (`git -C`, `cd`), so a relative word is also read against each. An unknown start folder may be
    the profile, so a relative name the settings path ends with counts, and so does a relative path into the
    plugin's data folder. A relative path into the plugin's code from an unknown folder does not: that is
    every test run and `git add` of a checkout of this plugin. The folder the call itself names to work in (its
    `workdir`, a leading `cd`) is no holder when named again: a command may work in the profile folder without
    touching what the guard watches. The session's folder only places relative names."""
    flat = text.translate(_QUOTES)
    if home is None:  # any profile's
        low = flat.lower()
        return ("code" if str(PLUGIN_ROOT).lower() in low else "data" if any(f in low for f in _DATA_FOLDERS)
                else "settings" if any(n in low for n in _SETTINGS_FILES) else None)
    home = Path(home)
    targets = _Targets(home)
    readable = not shell or _names_readable(text, scripts=False)  # a hidden `cd` (`c\d`, `'c'd`): start unknown
    start = _start_folder(text, workdir or session, home, user_home) if readable else ""
    shell_start = _start_folder(text, workdir or session, home, user_home, _CD_RE) if shell and readable else ""
    found = _replaced_kinds(text, home, user_home, shell_start, targets) if shell else set()
    moves = bool(_MOVE_RE.search(text))
    if shell and not (workdir or moves or any(h in text.lower() for h in _HINTS)):
        return next((kind for kind in _KINDS if kind in found), None)
    written = _written_only(text) if shell else None
    if written is not None:  # a lone `cp` or `tar c` is read for the place it writes
        text, flat = written, written.translate(_QUOTES)
    own = start if (workdir or moves) else ""  # the folder the call names to work in
    words = _words(_expand_vars(flat, home, user_home), user_home) | ({own} if own else set())
    plain = {w for w in words if not _GLOB_CHARS.intersection(w)}
    variants = {w: _variants(Path(w)) for w in plain if os.path.isabs(w)}
    named_folders = set().union(*variants.values())
    folders = named_folders | (_variants(Path(start)) if start else set())
    at_start = _spellings(Path(own)) if own else set()
    hints = (PLUGIN_DIR, PLUGIN_ROOT.name.lower())

    # A folder only `mkdir` names is made, not replaced (`mkdir -p $HERMES_HOME/plugins` before linking another
    # plugin). Named by any other part of the command too, it counts.
    made: dict = {}
    for part in _pipeline(text) or []:
        for arg in part[1:]:
            key = os.path.normpath(_expand(arg, home, user_home)).lower()
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
            ("holder", any(holder(_spellings(Path(os.path.join(f, word)))) for f in named_folders))) if hit)
    return next((kind for kind in _KINDS if kind in found), None)


# Commands that replace what lies in the folders they are given (_replaced). A wrapper runs the command after it.
_REMOVERS = frozenset({"rm", "rmdir", "unlink", "shred"})
_WRAPPERS = frozenset({"sudo", "doas", "env", "nice", "nohup", "time", "command", "builtin", "exec"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_]\w*=")
_FIND_ACTIONS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})


def _commands(command: str) -> Optional[list]:
    """Each simple command of a shell text as its words: leading assignments and wrappers dropped, the file
    after a redirect dropped. None when the text cannot be split."""
    text = _HARMLESS_REDIRECT_RE.sub(" ", command).replace("\\\n", " ").replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    parts, words, skip = [], [], False
    for token in tokens:
        if skip:
            skip = False
        elif token and _SHELL_OPERATORS.issuperset(token):
            skip = bool(set(token) & set("<>"))
            if not skip:
                parts.append(words)
                words = []
        else:
            words.append(token)
    parts.append(words)
    out = []
    for words in parts:
        while words and (_ASSIGNMENT_RE.match(words[0]) or words[0].rpartition("/")[2] in _WRAPPERS):
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
        if words:
            out.append(words)
    return out


def _replaced(command: str) -> "tuple[list, bool]":
    """What a shell command writes over or removes: ``(word, names)`` pairs, where ``names`` are the entries it
    writes into the folder ``word`` (`cp report.md ~` writes ~/report.md, not ~) and None means the place as a
    whole; and whether it unpacks into the folder it runs in (`tar -x` or `unzip` without a folder)."""
    out, here = [], False
    for words in _commands(command) or []:
        name, args = words[0].rpartition("/")[2], words[1:]
        plain = [a for a in args if not a.startswith("-") or a == "-"]
        if name in ("xargs", "parallel"):  # its arguments come from the input: every word may be one
            inner = _xargs_command(args)
            if inner and not _part_reads(inner):
                out += [(w, None) for part in _commands(command) or [] for w in part[1:] if not w.startswith("-")]
        elif name in _REMOVERS:
            out += [(w, None) for w in plain]
        elif name in ("cp", "mv", "ln", "install"):
            out += _copied(name, args)
        elif name == "rsync" and len(plain) >= 2:
            out.append((plain[-1], None))
        elif name == "tar" and _tar_extracts(args):
            folder = _option_value(args, ("-C", "--directory"))
            here = here or folder is None
            out += [(folder, None)] if folder else []
        elif name == "unzip" and not {"-l", "-t", "-v", "-Z"} & set(args):
            folder = _option_value(args, ("-d",))
            here = here or folder is None
            out += [(folder, None)] if folder else []
        elif name == "find" and _FIND_ACTIONS & set(args):
            paths = list(itertools.takewhile(lambda a: not a.startswith(("-", "(", "!", ")")), args))
            out += [(p, _find_names(args)) for p in paths or ["."]]
    return out, here


# xargs and parallel options that take the next word as their value.
_XARGS_VALUED = frozenset({"-I", "-n", "-P", "-L", "-l", "-s", "-d", "-E", "-e", "-a", "-j", "--arg-file",
                           "--delimiter", "--max-args", "--max-procs", "--max-lines", "--max-chars", "--jobs"})


def _xargs_command(args: list) -> list:
    """The command xargs or parallel runs, as words: [] when none is named (xargs then runs `echo`)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _XARGS_VALUED else 1
    return args[i:]


def _copied(name: str, args: list) -> list:
    """The places `cp`, `mv`, `ln` or `install` write. A folder target as a whole only when `cp -r` copies into it
    or a source is a glob; else the entries named after each source. `mv` also removes its sources."""
    folder = _option_value(args, ("-t", "--target-directory"))
    plain, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in ("-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group"):
            skip = True
        elif not arg.startswith("-") or arg == "-":
            plain.append(arg)
    if folder is None:
        if len(plain) < 2:
            return []
        folder, sources = plain[-1], plain[:-1]
    else:
        sources = plain
    flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    whole = name == "cp" and (bool(set(flags) & set("rRa")) or "--recursive" in args or "--archive" in args)
    whole = whole or any(_GLOB_CHARS.intersection(s) for s in sources)
    out = [(s, None) for s in sources] if name == "mv" else []
    names = None if whole else tuple(os.path.basename(s.rstrip("/")) or s for s in sources)
    return out + [(folder, names)]


def _option_value(args: list, options: tuple) -> Optional[str]:
    """The value of the last of ``options`` in ``args`` (`-C dir`, `--directory=dir`), or None."""
    value = None
    for i, arg in enumerate(args):
        if arg in options and i + 1 < len(args):
            value = args[i + 1]
        for option in options:
            if option.startswith("--") and arg.startswith(option + "="):
                value = arg.split("=", 1)[1]
    return value


def _tar_extracts(args: list) -> bool:
    first = args[0] if args else ""
    return (("x" in first and not first.startswith("-"))
            or any(a.startswith("-") and not a.startswith("--") and "x" in a for a in args)
            or bool({"--extract", "--get"} & set(args)))


class _OnlyNamed(tuple):
    """The name patterns a `find` expression requires of every entry it acts on."""


def _find_names(args: list) -> "Optional[_OnlyNamed]":
    """The `-name`/`-iname` patterns of a `find` expression, or None when it may act on any entry (no such test,
    or an `-o`, a `!` or a path test)."""
    if {"-o", "-or", "!", "-not", "-path", "-wholename", "-regex", "-iregex", "-ipath"} & set(args):
        return None
    names = [args[i + 1].lower() for i, a in enumerate(args[:-1]) if a in ("-name", "-iname")]
    return _OnlyNamed(names) if names else None


def _replaced_kinds(command: str, home: Path, user_home: str, start: str, targets: _Targets) -> set:
    """Which kinds the places a shell command replaces are (_replaced), relative ones read in the shell's folder
    ``start`` (`tar -C rel` too), or in each folder the command names when that is unknown."""
    replaced, here = _replaced(command)
    named = {w for w in _words(_expand_vars(command.translate(_QUOTES), home, user_home), user_home)
             if os.path.isabs(w) and not _GLOB_CHARS.intersection(w)}
    bases = [start] if start else sorted(named)
    found = targets.at(start, home) if here and start else set()
    for word, names in replaced:
        word = _expand(word, home, user_home)
        for place in [word] if os.path.isabs(word) else [os.path.join(b, word) for b in bases]:
            place = os.path.normpath(place)
            if isinstance(names, _OnlyNamed):  # `find -name x -delete` spares every other name
                guarded = targets.names()
                if any(fnmatch.fnmatchcase(n, pattern) for n in guarded for pattern in names):
                    found |= targets.at(place, home)
            elif names is None:
                found |= targets.at(place, home)
            elif os.path.isdir(place) or word.endswith("/"):
                found |= set().union(*(targets.at(os.path.join(place, n), home) for n in names))
            else:
                found |= targets.at(place, home)
    return found


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
    "reload-tokens": "reloads Forgebrücke's tokens through Forgebrücke's page route",
    "holder": "runs a command that can change a folder that holds Forgebrücke's settings, data or code",
}


# `cp` and `tar` (creating an archive) read their sources and write one place. With only these options, the
# written place is all that counts; any other option (`--remove-files`, `-t`, `--suffix`) leaves the whole
# command to the usual reading. So does a `cp` that may leave a link to its source: `-s`, `-l`, `-P`, `-d`, `-a`,
# or `-r` without `-L` (it copies a linked folder, such as the profile's plugin link, as a link), as a write
# through the link would change the source.
_CP_FLAGS = frozenset("RrpfinvuxLHT")
_CP_LONG = frozenset({"--recursive", "--force", "--verbose", "--update", "--no-clobber", "--interactive",
                      "--dereference", "--parents", "--preserve", "--one-file-system"})
_TAR_FLAGS = frozenset("czjJvp")
_TAR_LONG = frozenset({"--create", "--gzip", "--bzip2", "--xz", "--zstd", "--verbose", "--exclude-vcs"})


def _cp_written(args: list) -> Optional[list]:
    plain = []
    for arg in args:
        if arg.startswith("--") and len(arg) > 2:
            if arg not in _CP_LONG and not arg.startswith(("--preserve=", "--no-preserve=")):
                return None
        elif arg.startswith("-") and len(arg) > 1:
            if not set(arg[1:]) <= _CP_FLAGS:
                return None
        else:
            plain.append(arg)
    flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    recursive = bool(set(flags) & set("rR")) or "--recursive" in args
    if recursive and "L" not in flags and "--dereference" not in args:
        return None
    return [plain[-1]] if len(plain) >= 2 else None


def _tar_written(args: list) -> Optional[list]:
    args, archive, create = list(args), None, False
    if args and not args[0].startswith("-"):  # the old form: `tar czf ARCHIVE ...`
        letters = args.pop(0)
        if not set(letters) <= _TAR_FLAGS | {"f"} or letters.count("f") > 1:
            return None
        create = "c" in letters
        if "f" in letters:
            if not args:
                return None
            archive = args.pop(0)
    while args:
        arg = args.pop(0)
        if arg in ("-C", "--directory", "-f", "--file"):
            if not args:
                return None
            value = args.pop(0)
            archive = value if arg in ("-f", "--file") else archive
        elif arg.startswith("--file="):
            archive = arg.split("=", 1)[1]
        elif arg.startswith(("--directory=", "--exclude=")) or arg in _TAR_LONG:
            create = create or arg == "--create"
        elif arg.startswith("-") and len(arg) > 1 and not arg.startswith("--"):
            letters = arg[1:]
            if not set(letters) <= _TAR_FLAGS | {"f"} or ("f" in letters and not letters.endswith("f")):
                return None
            create = create or "c" in letters
            if letters.endswith("f"):
                if not args:
                    return None
                archive = args.pop(0)
        elif arg.startswith("-"):
            return None
    if not create or archive is None:
        return None
    return [] if archive == "-" else [archive]


def _written_only(command: str) -> Optional[str]:
    """For a lone `cp` or `tar c` with known options, the places it writes as a command text; else None."""
    parts = _pipeline(command)
    if not parts or len(parts) != 1:
        return None
    words = parts[0]
    name = words[0].rpartition("/")[2]
    written = _cp_written(words[1:]) if name == "cp" else _tar_written(words[1:]) if name == "tar" else None
    return None if written is None else " ".join(shlex.quote(w) for w in written)


# Command names that can change the shell's folder without a `cd` in the text: a script read into the shell, or
# text run as a command. Shell words that only lead to the command name stay before it.
_FOLDER_UNSEEN = frozenset({"source", ".", "eval", "exec", "alias"})
_SHELL_WORDS = frozenset({"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "for", "case", "esac",
                          "select", "function", "time", "!", "{", "}"})
_PLAIN_NAME_RE = re.compile(r"[\w./+:@%\[-]+")


def _names_readable(command: str, scripts: bool = True) -> bool:
    """Whether the guard can read every command name in a shell text: each is a plain word (no quote, backslash or
    `$`, which can spell `cd` as `c\\d`, `'c'd` or `$go`) and, with ``scripts``, none can change the folder unseen
    (the folder for the session's next call; within a call the released guard trusted `source .env && ...`)."""
    words = _raw_words(command)
    if words is None:
        return False
    first = True
    for word in words:
        if word in _SHELL_OPERATORS or word == "\n":
            first = word not in "<>"  # after a redirect comes its file
        elif first and not (_ASSIGNMENT_RE.match(word) or word in _SHELL_WORDS):
            if not _PLAIN_NAME_RE.fullmatch(word) or (scripts and word in _FOLDER_UNSEEN):
                return False
            first = False
    return True


def _raw_words(command: str) -> Optional[list]:
    """The words of a shell text as written (quotes and backslashes kept) and its operators, one character each;
    None when a quote is left open."""
    out, word, quote, i = [], "", "", 0
    text = command.replace("\\\n", " ")
    while i < len(text):
        c = text[i]
        if c == "\\" and quote != "'" and i + 1 < len(text):
            word, i = word + text[i:i + 2], i + 2
            continue
        if quote:
            quote = "" if c == quote else quote
            word += c
        elif c in "'\"":
            quote, word = c, word + c
        elif c.isspace() or c in _SHELL_OPERATORS:
            out += [word] if word else []
            out += [c] if c == "\n" or c in _SHELL_OPERATORS else []
            word = ""
        else:
            word += c
        i += 1
    return None if quote else out + ([word] if word else [])


class SessionFolders:
    """Where a session's next call starts when it names no ``workdir``: Hermes runs each terminal command, script and
    relative file path in the session's recorded folder, which every earlier `cd` moves. The plugin can read only
    the folder the session started in (``ctx.session_cwd()``), so this follows the session's own terminal calls:
    after `cd <folder>` into a folder that exists the next call starts there, after one that may have failed in
    either folder; after a change of folder it cannot follow, the start is unknown and relative names count as the
    profile's. A session first seen after a plugin reload
    starts from the recorded folder again."""

    LIMIT = 512

    def __init__(self, declared: Optional[Callable[[], str]] = None) -> None:
        self._declared = declared
        self._seen: dict = {}
        self._lock = threading.Lock()

    def where(self, key: str) -> Optional[frozenset]:
        with self._lock:
            if key in self._seen:
                return self._seen[key]
        try:
            folder = str(self._declared() or "").strip() if self._declared else ""
        except Exception:
            folder = ""
        return frozenset({os.path.normpath(folder)}) if os.path.isabs(folder) else None

    def forget(self, key: str) -> None:
        """The start of ``key``'s next call is unknown."""
        with self._lock:
            self._seen[key] = None

    def follow(self, key: str, tool_name: str, args: Any, home: Optional[Path]) -> None:
        """Note a terminal call: Hermes moves the session's folder only for a call without a ``workdir``."""
        if tool_name != "terminal" or not isinstance(args, dict) or args.get("workdir") or not key:
            return
        command = str(args.get("command") or "")
        readable = _names_readable(command)
        if readable and not _CD_RE.search(command):
            return
        before = self.where(key)
        after: Optional[frozenset] = None
        if readable and before is not None and home is not None:
            moves = {(folder, _start_folder(command, folder, Path(home), user_home, _CD_RE))
                     for folder in before for user_home in _user_homes(home)}
            if all(target for _, target in moves):
                # a `cd` into a folder that exists and may be entered does not fail; after another the shell
                # may have stayed
                after = frozenset().union(*({target} if os.path.isdir(target) and os.access(target, os.X_OK)
                                            else {folder, target} for folder, target in moves))
        with self._lock:
            self._seen.pop(key, None)
            self._seen[key] = after
            while len(self._seen) > self.LIMIT:
                self._seen.pop(next(iter(self._seen)))


def settings_touch(tool_name: str, args: Any, home: Optional[Path], *,
                   cwds: Optional[frozenset] = None) -> Optional[str]:
    """What a tool call would change in this profile's guardrails, or None. A heuristic over the call's
    text: it catches the direct routes (editing config.yaml or .env, `hermes config set`, toggling the
    plugin), not a determined script, which is why Hermes' own approvals stay on for the shell. ``cwds`` are
    the folders a call without a ``workdir`` may start in (``SessionFolders``); None when unknown."""
    if tool_name not in GUARDED_TOOLS or not isinstance(args, dict):
        return None
    if tool_name in ("write_file", "patch"):
        raw = str(args.get("path") or "")
        if not raw:
            return None
        for user_home in _user_homes(home):
            path = Path(_tilde(raw, user_home))
            places = [path] if path.is_absolute() or not cwds else [Path(c) / path for c in sorted(cwds)]
            for place in places:
                what = _file_touch(raw, place, home)
                if what:
                    return what
        return None
    text = str(args.get("command") or args.get("code") or "")
    workdir = args.get("workdir") if tool_name == "terminal" else None
    shell = tool_name == "terminal"
    if shell and _only_reads(text):
        return None
    written = _written_only(text) if shell else None
    lowered = (text if written is None else written).lower()
    sessions = sorted(cwds) if cwds and not workdir else [None]
    found = {_named(text, home, workdir, session, shell) for session in sessions}
    kind = next((k for k in _KINDS if k in found), None)
    if kind is None and _names_settings_key(lowered, tool_name):
        kind = "settings"
    if kind is None and "hermes" in lowered and _hermes_changes_here(lowered, home):
        kind = "hermes"
    if kind is None:
        route = _PAGE_ROUTE_RE.search(lowered)
        kind = route.group(1) if route else None
    return _WHAT[kind] if kind else None


def _file_touch(raw: str, path: Path, home: Optional[Path]) -> Optional[str]:
    """What a write_file or patch on ``path`` (``raw`` as the call wrote it) would change, or None."""
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


def make_settings_guard(run_context: Callable[[], RunContext], enabled_here: Callable[[], bool],
                        home: Callable[[], Optional[Path]], session_cwd: Optional[Callable[[], str]] = None):
    """Hermes skips a hook that raises, so nothing here may raise: a check that fails asks, and a run
    context that cannot be read counts as nobody there. ``session_cwd`` is ``ctx.session_cwd``."""
    folders = SessionFolders(session_cwd)

    def forgebruecke_settings_guard(tool_name: str = "", args: Any = None, session_id: str = "", task_id: str = "",
                                    **_: Any):
        if tool_name not in GUARDED_TOOLS:
            return None
        try:
            here = home()
        except Exception:
            here = None  # any profile's settings count
        key = str(task_id or session_id or "")
        try:
            what = settings_touch(tool_name, args, here, cwds=folders.where(key) if key else None)
        except Exception:
            what = "could not be checked by Forgebrücke's settings guard"
        try:
            folders.follow(key, tool_name, args, here)
        except Exception:
            folders.forget(key)
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


def make_review_lockdown(review_worker: Callable[[], bool] = _review_worker):
    """A review worker answers its verdict and calls no tool. Hermes adds the profile's plugin toolsets to the
    worker's ``-t no_mcp`` selection, so this hook refuses every call such a session makes. Nothing here may
    raise: Hermes skips a hook that does."""
    def forgebruecke_review_lockdown(tool_name: str = "", **_: Any):
        try:
            worker = review_worker()
        except Exception:
            worker = False
        if not worker:
            return None
        return _block(_refusal("guardrail", f"A Forgebrücke review worker calls no tool, so {tool_name or 'this call'} "
                                            "was refused. Answer with the verdict JSON only."))

    return forgebruecke_review_lockdown
