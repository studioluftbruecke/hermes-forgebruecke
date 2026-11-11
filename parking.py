"""The desk's parking area: pull requests he set aside under Waiting for me. They stay open in Forgejo, leave
the count, and come back on their own (PLAN-v0.0.21-park.md § 3 to § 5).

Store ``<data_dir>/parked.json``, private to this profile (decision D-48)::

    {"version": 1, "parked": {"owner/repo#n": {repo, number, note, parked_at, wake_commits,
                                                seen{head_sha, comment_at, draft}}}}

The key is ``owner/repo#n`` in lower case; ``repo`` keeps Forgejo's spelling. ``seen`` is the row as he saw it
when he parked it. Only his click parks (no tool can, L1). Writers (the park routes and the desk's prune) take
``fcntl.flock`` on ``parked.lock``, re-read the file under the lock, apply only their own change and replace
the file atomically; reads take no lock. Each write logs one line to ``desk-actions.jsonl`` (L7).

Waking is computed on every desk read and never written (``wake``): a draft marked ready, new commits (when
``wake_commits``), or a newer comment by someone other than him, first match wins (L4).
"""
from __future__ import annotations

import contextlib
import importlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

FILE = "parked.json"
LOCK = "parked.lock"
MAX_NOTE = 280
VERSION = 1
CORRUPT_NOTE = "Parked rows could not be read."
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _mod(name: str):
    return importlib.import_module(f"{__package__}.{name}")


def key(repo: str, number: int) -> str:
    return f"{repo}#{int(number)}".lower()


def clean_note(text: Any) -> str:
    """One line of plain text: control characters (newlines included) become spaces, at most ``MAX_NOTE``."""
    s = re.sub(r" {2,}", " ", _CONTROL.sub(" ", str(text or ""))).strip()
    return s[:MAX_NOTE].rstrip()


def load(data_dir: Any) -> "tuple[dict, bool]":
    """``(entries, unreadable)``. A missing file or folder is no entries; a file that does not parse is no
    entries and ``unreadable`` (the desk says so and never prunes on it)."""
    if not data_dir:
        return {}, False
    try:
        text = (Path(data_dir) / FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, False
    except OSError:
        return {}, True
    try:
        data = json.loads(text)
        parked = data["parked"]
        if not isinstance(parked, dict) or not all(isinstance(v, dict) for v in parked.values()):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return {}, True
    return parked, False


@contextlib.contextmanager
def _locked(data_dir: Any):
    import fcntl

    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / LOCK).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _change(data_dir: Any, apply: Callable[[dict], Any]) -> Any:
    """Re-read under the lock, apply one change, replace the file. Returns what ``apply`` returned."""
    with _locked(data_dir):
        entries, _ = load(data_dir)
        result = apply(entries)
        text = json.dumps({"version": VERSION, "parked": entries}, ensure_ascii=False, indent=1, sort_keys=True)
        _mod("flows.state").atomic_write(Path(data_dir) / FILE, text + "\n")
    return result


def _log(data_dir: Any, record: dict) -> dict:
    return _mod("desk")._logged(data_dir, record)


def seen_of(row: dict) -> dict:
    """The fingerprint of a desk row as he sees it: what a later read compares against."""
    wake = row.get("wake_comment") or {}
    draft = row.get("draft")
    return {"head_sha": str(row.get("head_sha") or ""), "comment_at": wake.get("at"),
            "draft": draft if isinstance(draft, bool) else None}


def _seen(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    sha = str(raw.get("head_sha") or "")
    if sha and not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
        raise ValueError("seen.head_sha is not a commit sha.")
    at = raw.get("comment_at")
    if at is not None and _epoch(at) is None:
        raise ValueError("seen.comment_at is not a time.")
    draft = raw.get("draft")
    return {"head_sha": sha.lower(), "comment_at": at, "draft": draft if isinstance(draft, bool) else None}


def park(data_dir: Any, repo: str, number: int, *, note: Any = "", wake_commits: bool = True, seen: Any = None,
         now: Callable[[], float] = time.time) -> "tuple[dict, dict]":
    """Park one pull request, or park it again (note and fingerprint replaced). Returns ``(entry, log note)``.
    Raises ``ValueError`` for a bad ``seen``."""
    entry = {"repo": repo, "number": int(number), "note": clean_note(note), "parked_at": int(now()),
             "wake_commits": bool(wake_commits), "seen": _seen(seen)}
    _change(data_dir, lambda entries: entries.__setitem__(key(repo, number), entry))
    logged = _log(data_dir, {"at": entry["parked_at"], "action": "park", "repo": repo, "number": int(number),
                             "note_chars": len(entry["note"]), "wake_commits": entry["wake_commits"]})
    return entry, logged


def unpark(data_dir: Any, repo: str, number: int, *, now: Callable[[], float] = time.time) -> "tuple[bool, dict]":
    """Bring one pull request back. Returns ``(removed, log note)``; unparking a row that is not parked is fine."""
    removed = _change(data_dir, lambda entries: entries.pop(key(repo, number), None) is not None)
    logged = _log(data_dir, {"at": int(now()), "action": "unpark", "repo": repo, "number": int(number),
                             "removed": removed})
    return removed, logged


def prune(data_dir: Any, keep: Iterable[str], *, scan_at: float, now: Callable[[], float] = time.time) -> list:
    """Drop entries whose id is not in ``keep`` (the open pull requests of a complete scan plus the flow rows).
    An entry parked at or after ``scan_at`` survives: the scan could not have seen it. Returns the dropped ids;
    writes and logs only when there is something to drop."""
    keep = {k.lower() for k in keep}
    entries, unreadable = load(data_dir)
    if unreadable or not [k for k, e in entries.items() if k not in keep and int(e.get("parked_at") or 0) < scan_at]:
        return []

    def apply(current: dict) -> list:
        gone = sorted(k for k, e in current.items() if k not in keep and int(e.get("parked_at") or 0) < scan_at)
        for k in gone:
            del current[k]
        return gone

    gone = _change(data_dir, apply)
    if gone:
        _log(data_dir, {"at": int(now()), "action": "prune", "ids": gone})
    return gone


def _epoch(iso: Any) -> Optional[int]:
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def age_words(seconds: int) -> str:
    """``desk._ago`` without "ago": "3 d", "5 min"."""
    return _mod("desk")._ago(max(0, int(seconds))).removesuffix(" ago")


def wake(entry: dict, row: dict, now: Callable[[], float] = time.time, *, forge_url: str = "") -> Optional[dict]:
    """Why a parked row is back, or None. First match wins: ``ready``, ``commit`` (only with ``wake_commits``),
    ``comment``. A failed read never wakes: an unknown draft state is None, a failed comment read is None."""
    seen = entry.get("seen") or {}
    age = age_words(int(now()) - int(entry.get("parked_at") or 0))
    if seen.get("draft") is True and row.get("draft") is False:
        return {"reason": "ready", "text": "It is marked ready now.", "url": row.get("url") or ""}
    old, new = str(seen.get("head_sha") or ""), str(row.get("head_sha") or "")
    if entry.get("wake_commits") and old and new and new.lower() != old.lower():  # an unknown sha never wakes
        url = f"{forge_url.rstrip('/')}/{row['repo']}/compare/{old}...{new}" if forge_url else row.get("url") or ""
        return {"reason": "commit", "text": f"New commits since you parked it {age} ago.", "url": url}
    comment = row.get("wake_comment") or {}
    at = _epoch(comment.get("at"))
    since = _epoch(seen.get("comment_at")) if seen.get("comment_at") else int(entry.get("parked_at") or 0)
    if at is not None and since is not None and at > since:
        return {"reason": "comment", "text": f"@{comment.get('author')} commented since you parked it {age} ago.",
                "url": comment.get("url") or row.get("url") or ""}
    return None
