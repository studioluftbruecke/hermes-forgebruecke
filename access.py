"""Access: settings, credentials and token routing by lane for one call, plus the permission checks.

Forgejo is the only permission layer (0.0.14, D27): what Hermes may read or write in a repo is the
collaborator right of its own Forgejo account there (``permissions`` on ``GET /repos/{owner}/{repo}``:
pull = read, push = write, admin = admin). The plugin asks, caches the answer for ``RIGHTS_TTL_S`` per
process (a tick or a desk build asks once per repo, not once per call), and a refusal names the right
it found plus the repo's collaborator page, where the user changes it.

Lanes (M4b; one test asserts every cell of this table):

| Lane | Who starts it | Reads with | Writes with | Repos |
| --- | --- | --- | --- | --- |
| ``agent`` | the user's own chat, cron job or ``-q`` run | the agent token | the agent token only | every repo the agent account sees; writes where it has write |
| ``mention`` | a forge user's mention | the agent token only | none through tools | the thread's repo only |
| ``flow`` | the flows engine | the agent token only | the agent token, only what the flow names | the flow's repos only |
| ``owner`` | the user's click in the desk | the write token of the user's account | the write token of the user's account | the one repo of the action |

So a commenter can never make a mention turn quote a repo outside its thread, and no agent write is ever
attributed to the human.

``Refusal(code, message, setting, link)`` is how every check says no: a sentence the user can act on,
the setting that would change the answer and, for a Forgejo right, the page where he changes it.
"""
from __future__ import annotations

import importlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
RIGHTS = ("read", "write", "admin")
WRITE_RIGHTS = ("write", "admin")
RIGHTS_TTL_S = 60.0
AUTHOR_WRITE = ("write", "admin", "owner")


def _mod(name: str):
    pkg = __package__ or __name__.rpartition(".")[0]
    return importlib.import_module(f".{name}", pkg)


class Refusal(Exception):
    def __init__(self, code: str, message: str, setting: str = "", link: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.setting, self.link = code, message, setting, link

    def to_dict(self) -> dict:
        out = {"ok": False, "error": self.code, "message": self.message, "sent": False}
        if self.setting:
            out["setting"] = self.setting
        if self.link:
            out["link"] = self.link
        return out


def parse_repo(value: Any) -> str:
    repo = str(value or "").strip().strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not _REPO_RE.match(repo):
        raise Refusal("bad_repo", f"{value!r} is not a repository name like owner/repo.")
    return repo


LANES = ("agent", "mention", "flow", "owner")


# --- the agent account's rights, as Forgejo says them -------------------------------------------------

_cache: dict = {}
_lock = threading.Lock()


def clear_rights() -> None:
    with _lock:
        _cache.clear()


def _hit(key: tuple) -> "tuple[bool, Any]":
    with _lock:
        found = _cache.get(key)
    if found is not None and time.monotonic() - found[0] < RIGHTS_TTL_S:
        return True, found[1]
    return False, None


def _put(key: tuple, value: Any) -> Any:
    with _lock:
        _cache[key] = (time.monotonic(), value)
    return value


def right_from(perms: Any) -> str:
    """``admin``, ``write`` or ``read`` from a repo's ``permissions`` (a repo the token sees is at least read)."""
    p = perms if isinstance(perms, dict) else {}
    return "admin" if p.get("admin") else "write" if p.get("push") else "read"


def collaboration_url(base_url: str, repo: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/{repo}/settings/collaboration"


def repo_right(client, repo: str) -> Optional[str]:
    """The right of ``client``'s account on ``repo``, or None when it cannot see it (404)."""
    key = (client.account_key, "repo", repo.lower())
    hit, value = _hit(key)
    if hit:
        return value
    try:
        info = client.get(f"/repos/{repo}").data or {}
    except _mod("client").ForgejoError as e:
        if e.status == 404:
            return _put(key, None)
        raise
    return _put(key, right_from(info.get("permissions")))


def agent_repos(client, *, max_items: int = 500) -> list:
    """Every repo ``client``'s account sees: ``[{repo, right, owner, private, url, open_prs, archived}]``.
    One paginated ``GET /user/repos``, which also fills the per-repo cache."""
    key = (client.account_key, "list", "")
    hit, value = _hit(key)
    if hit:
        return value
    out = []
    for r in client.paginate("/user/repos", max_items=max_items).items:
        full = r.get("full_name")
        if not full:
            continue
        right = right_from(r.get("permissions"))
        _put((client.account_key, "repo", full.lower()), right)
        out.append({"repo": full, "right": right, "owner": (r.get("owner") or {}).get("login") or full.split("/")[0],
                    "private": bool(r.get("private")), "url": r.get("html_url") or "",
                    "open_prs": r.get("open_pr_counter"), "archived": bool(r.get("archived"))})
    return _put(key, out)


def account_login(client) -> str:
    """The login of ``client``'s own account (``GET /user``), cached."""
    key = (client.account_key, "login", "")
    hit, value = _hit(key)
    if hit:
        return value
    return _put(key, str((client.get("/user").data or {}).get("login") or ""))


def owner_counts(repos: list) -> dict:
    counts: dict = {}
    for r in repos:
        owner = str(r.get("owner") or "")
        if owner:
            counts[owner] = counts.get(owner, 0) + 1
    return counts


def suggest_you(counts: dict, agent: str) -> str:
    """The most common owner among the agent account's repos (``{owner: n}``), not the agent itself: most
    likely the user who shared them. "" when there is none."""
    mine = {o: n for o, n in (counts or {}).items() if o and o.lower() != (agent or "").lower()}
    return max(sorted(mine), key=lambda o: mine[o]) if mine else ""


def author_can_write(client, repo: str, login: str) -> "tuple[bool, str]":
    """Whether ``login`` has write access (or more) to ``repo``, and how that was found.

    First ``GET /repos/{repo}/collaborators/{login}/permission``. Forgejo answers it for other accounts only
    when the asking account is admin of the repo (measured on 15.0.11: 403 "collaborators can query only their
    own"), so a non-admin agent then reads ``GET /repos/{repo}/assignees``: the accounts that may be assigned,
    which Forgejo limits to the owner and collaborators with write access or more."""
    key = (client.account_key, "author", f"{repo.lower()}|{login.lower()}")
    hit, value = _hit(key)
    if hit:
        return value
    ForgejoError = _mod("client").ForgejoError
    no_perm_key = (client.account_key, "perm-forbidden", repo.lower())
    if not _hit(no_perm_key)[1]:
        try:
            perm = str((client.get(f"/repos/{repo}/collaborators/{login}/permission").data or {}).get("permission") or "")
            return _put(key, (perm in AUTHOR_WRITE, f"Forgejo right: {perm or 'none'}"))
        except ForgejoError as e:
            if e.status == 404:
                return _put(key, (False, "not a Forgejo user"))
            if e.status != 403:
                raise
            _put(no_perm_key, True)  # ask once per repo, then read the assignable accounts
    names_key = (client.account_key, "assignees", repo.lower())
    hit, names = _hit(names_key)
    if not hit:
        names = _put(names_key, {str((u or {}).get("login") or "").lower()
                                 for u in client.paginate(f"/repos/{repo}/assignees", max_items=500).items})
    ok = login.lower() in names
    return _put(key, (ok, "has write access (Forgejo lists it as assignable)" if ok else
                      "has no write access (Forgejo does not list it as assignable)"))


@dataclass
class Access:
    settings: Any
    creds: Any
    transport: Any = None
    lane: str = "agent"
    scope_repo: str = ""  # mention and flow lanes: the only repo this call may touch

    def _client(self, token: Optional[str]):
        return _mod("client").ForgejoClient(self.settings.url, token, transport=self.transport)

    def for_lane(self, lane: str, scope_repo: str = "") -> "Access":
        if lane not in LANES:
            raise ValueError(lane)
        return Access(self.settings, self.creds, self.transport, lane, scope_repo)

    def read_token_choice(self) -> "tuple[Optional[str], str]":
        if self.lane == "owner":
            return self.creds.owner_token, "owner"
        return self.creds.token, "bot"  # agent, mention and flow: the agent token only (D29)

    @property
    def read_role(self) -> str:
        return self.read_token_choice()[1]

    def _no_token(self) -> Refusal:
        return Refusal("no_bot_token", "Forgebrücke works as Hermes' own Forgejo account, and its token "
                                       "(FORGEBRUECKE_BOT_ACCOUNT_TOKEN) is not set. Store it in the plugin settings.",
                       "token")

    def reader(self):
        token, role = self.read_token_choice()
        if not token:
            if self.lane == "owner":
                raise Refusal("dark", _mod("capabilities").dark_sentence("owner_merge"), "owner_token")
            raise self._no_token()
        return self._client(token)

    def agent_client(self):
        """The agent account's client, whatever the lane: rights are always that account's."""
        if not self.creds.token:
            raise self._no_token()
        return self._client(self.creds.token)

    def writer(self):
        """The agent token, for agent tool writes and flow runs. Never the user's token."""
        if self.lane == "owner":
            raise Refusal("wrong_lane", "Owner actions write with your account through owner_writer(), never the "
                                        "agent's writer.")
        if self.lane == "mention":
            raise Refusal("unattended", "A mention turn writes nothing through tools; its answer is the only write.")
        if not self.creds.token:
            raise self._no_token()
        return self._client(self.creds.token)

    def owner_writer(self):
        """The write token of the user's account: only in the owner lane (his click in the desk), never for
        agent tool writes, mention turns or unattended runs."""
        if self.lane != "owner":
            raise Refusal("wrong_lane", "Your own account's write token is used only on your click in the desk; "
                                        f"this call is in the {self.lane} lane.")
        if not self.creds.owner_token:
            raise Refusal("dark", _mod("capabilities").dark_sentence("owner_merge"), "owner_token")
        return self._client(self.creds.owner_token)

    # --- rights ----------------------------------------------------------------------------------

    def right(self, repo: str) -> Optional[str]:
        """The agent account's Forgejo right on ``repo``: admin, write, read, or None (cannot see it)."""
        return repo_right(self.agent_client(), repo)

    def repos(self) -> list:
        """Every repo the agent account sees, with its right (``agent_repos``)."""
        return agent_repos(self.agent_client())

    def collaboration_url(self, repo: str) -> str:
        return collaboration_url(self.settings.url, repo)

    def check_scope(self, repo: str) -> None:
        if self.lane in ("mention", "flow") and self.scope_repo and repo.lower() != self.scope_repo.lower():
            where = "answers a mention in" if self.lane == "mention" else "runs a flow on"
            raise Refusal("lane_scope", f"This turn {where} {self.scope_repo}, so it may read only {self.scope_repo}, "
                                        f"not {repo}. Ask in a chat with Hermes instead; forge users cannot widen "
                                        "what a mention may read.")

    def check_cross_repo(self) -> None:
        if self.lane in ("mention", "flow"):
            raise Refusal("lane_scope", f"A {self.lane} turn may read only {self.scope_repo or 'its own repo'}; "
                                        "searches across repos are off here.")

    def _no_access(self, repo: str) -> Refusal:
        link = self.collaboration_url(repo)
        return Refusal("no_access", f"Hermes' Forgejo account cannot see {repo} (Forgejo right: none), or the repo "
                                    f"does not exist. To share it, add the account as a collaborator with read or "
                                    f"write access: {link}", link=link)

    def check_read(self, repo: str) -> str:
        """The agent account's right on ``repo``; refuses when it has none."""
        self.check_scope(repo)
        right = self.right(repo)
        if right is None:
            raise self._no_access(repo)
        return right

    def check_write(self, repo: str, need: str = "write"):
        """Refuses unless the agent account has write access (or admin) on ``repo``. ``need`` names the write
        (``comment`` or ``write``) for the sentence; both need Forgejo's write right. Returns the repo's rule
        (its ``targets`` say where pull requests may go)."""
        self.check_scope(repo)
        right = self.right(repo)
        if right is None:
            raise self._no_access(repo)
        if right not in WRITE_RIGHTS:
            link = self.collaboration_url(repo)
            verb = "comment on" if need == "comment" else "write to"
            raise Refusal("read_only", f"Hermes may not {verb} {repo}: its Forgejo account has read access there "
                                       f"(Forgejo right: {right}). Writes need write access; change it in {link}",
                          link=link)
        return self.settings.rule_for(repo)


def thread_repo(chat_id: str) -> str:
    """``owner/repo`` from a mention session's chat id (``owner/repo#12``), else ``""``."""
    repo = str(chat_id or "").partition("#")[0].strip()
    return repo if _REPO_RE.match(repo) else ""


SettingGetter = Callable[[str, Any], Any]
SecretGetter = Callable[[str], Optional[str]]


def load_access(get_setting: SettingGetter, get_secret: SecretGetter, *, transport: Any = None,
                lane: str = "agent", scope_repo: str = "") -> Access:
    settings_mod = _mod("settings")
    settings = settings_mod.load_settings(get_setting)
    creds = settings_mod.load_credentials(get_secret)
    if not settings.url:
        raise Refusal("not_configured", "The Forgejo address is not set (setting url). Run forgejo_doctor.", "url")
    if not (creds.token or creds.owner_token):
        raise Refusal("not_configured", "No token is set: store the token of Hermes' Forgejo account as "
                                        "FORGEBRUECKE_BOT_ACCOUNT_TOKEN in the plugin settings. Run forgejo_doctor.",
                      "token")
    if lane in ("mention", "flow") and not scope_repo:
        raise Refusal("lane_scope", f"This {lane} turn has no repo of its own (a review worker, or a mention "
                                    "without a thread), so it reads nothing from the forge.")
    return Access(settings, creds, transport, lane, scope_repo)
