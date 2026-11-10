"""Receipts: after a turn, re-read every forge claim the reply makes and append ✓ or ❌.

Runs on ``transform_llm_output`` (the one hook whose return value replaces the reply; ``post_llm_call``
only observes). The hook sees the reply text only, so a receipt is only ever written for a claim the
reply itself makes, never for what a tool did silently.

Shapes:

- ``Claim(kind, repo, text, sha, branch, number)``: one parsed claim. ``kind`` is ``push`` (a commit SHA;
  ``branch`` is a hint the forge may not know), ``pr`` (a pull request was opened) or ``merge`` (a pull
  request was merged).
- ``Receipt(session_id, turn_id, claim, kind, claim_sha, verified, checked_at, detail)``: one checked
  claim. ``verified`` is True (✓), False (❌, the forge contradicts the claim) or None (not verified: the
  read failed, and the line says how).

Rules: only claims about a repo Hermes' Forgejo account sees; at most three lines per
turn; when nothing was claimed the hook returns None so another plugin's rewrite is not stolen. A
failed read is said out loud ("not verified:" and what the read answered), never silent.
"""
from __future__ import annotations

import importlib
import json
import re
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

MAX_LINES = 3
DEADLINE_S = 20.0  # Hermes abandons a transform_llm_output callback after 30 s
REQUEST_TIMEOUT_S = 6.0  # each read is sent once, because a retry would not fit the deadline
LOG_MAX_BYTES = 512_000
MARK_OK, MARK_BAD, MARK_UNKNOWN = "✓", "❌", "not verified:"
HEADER = "Receipts, re-read from Forgejo:"

_SHA = r"[0-9a-f]{7,40}"
_SHA_RE = re.compile(rf"(?<![0-9A-Za-z]){_SHA}(?![0-9A-Za-z])")
# Digits alone are a commit only where a commit stands: after the verb or the word "commit", and not before
# a noun. "1048576 bytes" is a size, and about one seven-character SHA in 27 has no letter.
_DIGIT_SHA_RE = re.compile(r"\b(?:pushed|commit|sha)\s+(\d{7,40})(?=\s*(?:$|[.,;:)]|(?:to|onto|on|in|into|at|and)\b))")
_REPO_RE = re.compile(r"(?<![\w./-])([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})(?![\w/-])")
_URL_RE = re.compile(r"https?://[^\s/)>\]]+/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(pulls|commit|src/branch)/([^\s)>\]#?]+)")
_PUSH_VERB = re.compile(r"\b(pushed|force-pushed)\b", re.I)  # "committed" is not here: a local commit is no push
_PR_OPEN_VERB = re.compile(r"\b(opened|created|filed|raised)\b", re.I)
_MERGE_VERB = re.compile(r"\bmerged\b", re.I)
_PR_WORD = r"(?:PR|pull request|pull)"
# A pull request's web address, bare or as a markdown link, with the "pull request" that may introduce it.
_PULL_LINK = re.compile(rf"(?:{_PR_WORD}\s+)?(?:\[[^\]]*\]\()?https?://[^\s/)>\]]+/[\w.-]+/[\w.-]+/pulls/(\d{{1,7}})"
                        r"(?:/[^\s)>\]]*)?\)?", re.I)
# "merged main into fix-mul for PR #3" merges a branch. The pull request was merged only where it is the
# object of "merged" or the subject of "was merged".
_MERGED_PR = re.compile(rf"\bmerged\s+(?:the\s+)?(?:{_PR_WORD}\s*#?|#)(\d{{1,7}})\b", re.I)
_PR_WAS_MERGED = re.compile(rf"\b{_PR_WORD}\s*#?(\d{{1,7}})\b(?:\s+(?:in|on|of|at)\s+\S+)?,?\s+"
                            r"(?:was|is|got|(?:has|had)\s+been)(?:\s+(?:now|just|already|also))?\s+merged\b", re.I)
# Right before the pull request, these make it part of a question, a condition or another subject.
_NOT_THE_SUBJECT = re.compile(r"\b(?:when|whenever|whether|as soon as|for|of|from|with|into|to|on|in)\s+(?:the\s+)?$",
                              re.I)
# a bare "#1" right after "issue" numbers the issue, not a pull request
_PR_NUM = re.compile(r"(?:\bPR|\bpull request|\bpull)\s*#?(\d{1,7})\b|(?<![\w/])(?<!issue\s)#(\d{1,7})\b", re.I)
_BRANCH_AFTER = re.compile(r"\b(?:to|onto|on)\s+(?:the\s+)?(?:branch\s+)?([A-Za-z0-9][A-Za-z0-9._/-]{0,199})", re.I)
# Words that turn a sentence into a plan, a condition or a negation rather than a report.
_NOT_A_REPORT = re.compile(r"\b(not|never|n't|will|would|should|could|can|cannot|please|once|after|if|"
                           r"until|unless|before|going to|plan to|want to|need to|ready to|yet)\b", re.I)
# An underscore is markup only at a word edge. Inside fix_mul or acme/my_repo it is part of the name.
_MARKUP = re.compile(r"[`*]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _mod(name: str):
    pkg = __package__ or __name__.rpartition(".")[0]
    return importlib.import_module(f".{name}", pkg)


@dataclass(frozen=True)
class Claim:
    kind: str           # push | pr | merge
    repo: str
    text: str           # the claim in short words, used in the receipt line
    sha: str = ""
    branch: str = ""
    number: int = 0

    def key(self) -> tuple:
        return (self.kind, self.repo.lower(), self.sha[:7], self.branch, self.number)


@dataclass
class Receipt:
    session_id: str
    turn_id: str
    claim: str
    kind: str
    claim_sha: str
    verified: Optional[bool]
    checked_at: int
    detail: str
    repo: str = ""

    def line(self) -> str:
        if self.verified is True:
            return f"{MARK_OK} {self.claim} in {self.repo}: verified at {self.detail}"
        if self.verified is False:
            return f"{MARK_BAD} {self.claim} in {self.repo}: {self.detail}"
        return f"{MARK_UNKNOWN} {self.claim} in {self.repo}: {self.detail}"


# --- parsing ---------------------------------------------------------------------------------------

def _sentences(text: str) -> list:
    out = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith((MARK_OK, MARK_BAD, MARK_UNKNOWN, HEADER)):
            continue  # an earlier receipt quoted back is not a new claim
        out.extend(s for s in _SENTENCE_SPLIT.split(stripped) if s.strip())
    return out


def _repos_in(sentence: str, is_ruled: Callable[[str], bool]) -> list:
    found = [m.group(1).rstrip(".").removesuffix(".git") for m in _REPO_RE.finditer(sentence)]
    found += [m.group(1).rstrip(".") for m in _URL_RE.finditer(sentence)]
    seen, out = set(), []
    for r in found:
        if r.lower() not in seen and is_ruled(r):
            seen.add(r.lower())
            out.append(r)
    return out


def _verb_is_reported(sentence: str, verb: re.Match) -> bool:
    """The verb is a report of something done, not a plan, a condition or a negation."""
    return not _NOT_A_REPORT.search(sentence[: verb.start()])


def _pr_number(sentence: str) -> int:
    url = next((m for m in _URL_RE.finditer(sentence) if m.group(2) == "pulls"), None)
    if url and url.group(3).isdigit():
        return int(url.group(3))
    m = _PR_NUM.search(sentence)
    return int(m.group(1) or m.group(2)) if m else 0


def _merged_pr(sentence: str) -> int:
    """The number of the pull request this sentence reports as merged, or 0."""
    named = _PULL_LINK.sub(r"PR #\1", sentence)
    active = _MERGED_PR.search(named)
    if active and _verb_is_reported(named, active):
        return int(active.group(1))
    passive = _PR_WAS_MERGED.search(named)
    if passive and _verb_is_reported(named, passive) and not _NOT_THE_SUBJECT.search(named[: passive.start()]):
        return int(passive.group(1))
    return 0


def _sha_in(sentence: str) -> str:
    low = sentence.lower()
    for m in _SHA_RE.finditer(low):
        # "defaced" and "accede" are words, and digits alone are a number until _DIGIT_SHA_RE says otherwise
        if re.search(r"\d", m.group(0)) and re.search(r"[a-f]", m.group(0)):
            return m.group(0)
    digits = _DIGIT_SHA_RE.search(low)
    return digits.group(1) if digits else ""


def _reported(s: str, is_repo: Callable[[str], bool]) -> list:
    """The merge, push and opened pull request one sentence reports, each still without its repo."""
    found = []
    merged_n = _merged_pr(s)
    if merged_n:
        found.append(Claim("merge", "", f"merged PR #{merged_n}", number=merged_n))
    push = _PUSH_VERB.search(s)
    if push and _verb_is_reported(s, push):
        sha = _sha_in(s)
        if sha:
            b = _BRANCH_AFTER.search(s[push.end():])
            branch = b.group(1).rstrip(".,;:)") if b else ""
            if branch.lower() in {"the", "a", "it", "this", "that"} or (branch and is_repo(branch)):
                branch = ""  # "pushed abc to owner/repo" names the repo, not a branch
            found.append(Claim("push", "", f"pushed {sha[:12]}" + (f" to {branch}" if branch else ""), sha=sha,
                               branch=branch))
    opened = _PR_OPEN_VERB.search(s)
    if opened and _verb_is_reported(s, opened) and re.search(r"\b(PR|pull request|pull)\b", s, re.I):
        n = _pr_number(s)
        if n and n != merged_n:
            found.append(Claim("pr", "", f"opened PR #{n}", number=n))
    return found


def claims_something(text: str) -> bool:
    """The reply reports a push, a merge or an opened pull request and names something that can be a repo.
    Which words are repos is a question for the forge, so a file path beside the repo must not decide this."""
    sentences = [_MARKUP.sub("", s) for s in _sentences(text)]
    return (any(_reported(s, lambda _word: False) for s in sentences)
            and bool(_repos_in(" ".join(sentences), lambda _word: True)))


def find_claims(text: str, is_ruled: Callable[[str], bool]) -> list:
    """Every push, PR and merge the reply reports, on repos ``is_ruled`` accepts, in reply order."""
    sentences = [_MARKUP.sub("", s) for s in _sentences(text)]
    everywhere = _repos_in(" ".join(sentences), is_ruled)
    default_repo = everywhere[0] if len(everywhere) == 1 else ""
    claims, keys = [], set()
    for s in sentences:
        here = _repos_in(s, is_ruled)
        repo = here[0] if here else default_repo
        for reported in _reported(s, is_ruled) if repo else ():
            claim = replace(reported, repo=repo)
            if claim.key() not in keys:
                keys.add(claim.key())
                claims.append(claim)
    return claims


# --- verification ----------------------------------------------------------------------------------

def _short(sha: Any) -> str:
    return str(sha or "")[:12]


def _branch_head(client, repo: str, branch: str) -> str:
    """The head SHA of ``branch``, or "" when the forge has no branch of that name."""
    try:
        b = client.get(f"/repos/{repo}/branches/{branch}").data or {}
    except _mod("client").ForgejoError as e:
        if e.status == 404 or e.kind == "bad_path":
            return ""
        raise
    return str((b.get("commit") or {}).get("id") or "")


def _commit(client, repo: str, sha: str) -> str:
    """The full SHA of the commit ``sha`` abbreviates, or "" when the forge has no such commit."""
    try:
        c = client.get(f"/repos/{repo}/git/commits/{sha}").data or {}
    except _mod("client").ForgejoError as e:
        if e.status in (404, 422):
            return ""
        raise
    full = str(c.get("sha") or "")
    return full if full.lower().startswith(sha.lower()) else ""


def _verify_push(client, claim: Claim) -> "tuple[Optional[bool], str, str, str]":
    """The claim is that the commit is on the forge. The branch is only a hint, the word after "to" or "on",
    so the receipt names it only when the forge has a branch of that name."""
    head = _branch_head(client, claim.repo, claim.branch) if claim.branch else ""
    text = f"pushed {claim.sha[:12]}" + (f" to {claim.branch}" if head else "")
    if head.lower().startswith(claim.sha.lower()):
        return True, _short(head), head, text
    sha = _commit(client, claim.repo, claim.sha)
    if sha:
        return True, _short(sha) + (f" ({claim.branch} is at {_short(head)})" if head else ""), sha, text
    if head:
        return False, f"not found on Forgejo: {claim.branch} is at {_short(head)}", head, text
    return False, "not found on Forgejo (no such commit)", "", text


def verify(client, claim: Claim) -> "tuple[Optional[bool], str, str, str]":
    """``(verified, detail, sha_read, text)``; ``text`` is the claim as the receipt prints it. Raises
    ForgejoError only for failures that are not answers."""
    if claim.kind == "push":
        return _verify_push(client, claim)
    return (*_verify_pr(client, claim), claim.text)


def _verify_pr(client, claim: Claim) -> "tuple[Optional[bool], str, str]":
    ForgejoError = _mod("client").ForgejoError
    try:
        pr = client.get(f"/repos/{claim.repo}/pulls/{claim.number}").data or {}
    except ForgejoError as e:
        if e.status == 404:
            return False, f"not found on Forgejo (no PR #{claim.number})", ""
        raise
    head = str((pr.get("head") or {}).get("sha") or "")
    if claim.kind == "merge":
        if pr.get("merged"):
            sha = str(pr.get("merge_commit_sha") or head)
            return True, _short(sha), sha
        return False, f"not merged on Forgejo (PR #{claim.number} is {pr.get('state') or 'open'})", head
    return True, f"{_short(head)} (\"{str(pr.get('title') or '')[:60]}\", {pr.get('state') or 'open'})", head


class _OutOfTime(Exception):
    """The hook's time is spent: no further request is sent."""


def _within(transport, clock: Callable[[], float], deadline: float):
    """``transport`` that sends nothing after ``deadline`` and waits for no answer past it."""
    def send(method, url, headers, body, timeout):
        left = deadline - clock()
        if left <= 0:
            raise _OutOfTime()
        return transport(method, url, headers, body, min(timeout, left))
    return send


def build_receipts(text: str, access, *, session_id: str = "", turn_id: str = "",
                   clock: Callable[[], float] = time.monotonic, now: Callable[[], float] = time.time) -> list:
    settings = access.settings
    client_mod = _mod("client")
    ForgejoError = client_mod.ForgejoError
    # The deadline covers the repo listing and every read, so it starts before the first request.
    transport = _within(access.transport or client_mod.default_transport, clock, clock() + DEADLINE_S)
    client = client_mod.ForgejoClient(settings.url, access.read_token_choice()[0], transport=transport,
                                      timeout=REQUEST_TIMEOUT_S)
    client.retries = 0
    visible: Optional[set] = None

    def in_scope(repo: str) -> bool:
        nonlocal visible
        try:
            access.check_scope(repo)  # a mention turn's receipts read only the thread's repo
        except Exception:
            return False
        if visible is None:  # every repo Hermes' Forgejo account sees, one cached listing
            try:
                visible = {r["repo"].lower() for r in _mod("access").agent_repos(client)}
            except Exception:
                visible = set(["*"])  # the forge did not answer: verify anyway and say so per claim
        return "*" in visible or repo.lower() in visible

    if not claims_something(text):
        return []  # nothing claimed: no forge traffic at all
    claims = find_claims(text, in_scope)[:MAX_LINES]
    if not claims:
        return []
    out = []
    for c in claims:
        claim_text = c.text
        try:
            verified, detail, sha, claim_text = verify(client, c)
        except _OutOfTime:
            verified, detail, sha = None, "Forgejo did not answer in time", ""
        except ForgejoError as e:
            verified, detail, sha = None, e.message.rstrip("."), ""
        except Exception as e:  # a receipt must never break the reply
            verified, detail, sha = None, f"the check failed ({type(e).__name__})", ""
        out.append(Receipt(session_id, turn_id, claim_text, c.kind, c.sha or sha, verified, int(now()), detail,
                           c.repo))
    return out


def render(text: str, receipts: list) -> Optional[str]:
    if not receipts:
        return None
    lines = "\n".join(r.line() for r in receipts[:MAX_LINES])
    return f"{text.rstrip()}\n\n{HEADER}\n{lines}"


def append_log(data_dir: Any, receipts: list) -> None:
    if not data_dir or not receipts:
        return
    path = Path(data_dir) / "receipts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        keep = path.read_text(encoding="utf-8").splitlines()[-500:]
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        for r in receipts:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")


def make_hook(load_access: Callable[[], Any], enabled_here: Callable[[], bool], data_dir: Any = None,
              logger: Any = None, user_key: Optional[Callable[[], str]] = None):
    """The ``transform_llm_output`` callback. Returns the reply plus receipts (and, in learn mode, at most one
    lesson card), or None when there is nothing to add."""
    lessons = _mod("lessons")

    def forgebruecke_receipts(response_text: str = "", session_id: str = "", turn_id: str = "", **_: Any):
        text = response_text if isinstance(response_text, str) else ""
        verb = bool(text) and bool(_PUSH_VERB.search(text) or _PR_OPEN_VERB.search(text) or _MERGE_VERB.search(text))
        # Parsing the reply costs less than enabled_here(), which reads the profile's settings file.
        claims_possible = verb and claims_something(text)
        if not text or not (claims_possible or lessons.might_trigger(text)):
            return None  # cheap exit: no claim, no lesson trigger, no settings read, no request
        try:
            if not enabled_here():
                return None
            access = load_access()
        except Exception:
            return None  # not configured here: nothing was verifiable, and nothing is claimed about it
        receipts: list = []
        if claims_possible:
            try:
                receipts = build_receipts(text, access, session_id=str(session_id or ""), turn_id=str(turn_id or ""))
            except Exception:
                if logger is not None:
                    logger.warning("forgebruecke receipts failed", exc_info=True)
                receipts = []
            try:
                append_log(data_dir, receipts)
            except Exception:
                pass
        out = render(text, receipts)
        card = None
        if getattr(access.settings, "learn_mode", False) and getattr(access, "lane", "agent") == "agent":
            try:
                card = lessons.pick(text, receipts, lessons.LessonStore(data_dir),
                                    (user_key or lessons.user_key)())
            except Exception:
                card = None
        if card:
            out = f"{(out or text).rstrip()}\n\n{card}"
        return out

    return forgebruecke_receipts
