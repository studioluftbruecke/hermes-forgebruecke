"""Every open pull request asks you to review it, whoever opened it and however.

``forgejo_create_pr`` already asks ``you``, but a pull request opened any other way (a raw API call,
the Forgejo web page, another profile, another tool) asked nobody, and the desk never showed it. Two
halves close that, both keyed on one rule, ``needs_request``:

- the flows engine's tick (gateway loop, mention channel loop, ``hermes forgebruecke tick``) calls
  ``sweep``: it asks ``you`` on every open pull request the agent account may write to that lacks the
  request. This is the write; it is what Forgejo's own review list, mail and notifications see;
- the desk lists the same pull requests under Waiting for me, marked "review not requested yet", so one
  still shows when the write cannot happen (dry run, a repo where the agent account has read access,
  a tick that has not run yet).

A pull request is left alone when ``you`` opened it (Forgejo refuses a request to the author with 422),
when ``you`` is already asked, or when ``you`` already submitted a review on it: after a review the next
request is the author's call, so the tick never re-asks every 30 seconds. The sweep also remembers every
pull request that asked ``you`` (``asked.json``), so it asks once and a request ``you`` removed stays
removed. The setting ``review_every_pr`` (on by default) switches both halves off; ``forgejo_create_pr``
keeps asking either way.
"""
from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from typing import Any, Callable

MAX_SEARCH = 200    # open pull requests the search reads
MAX_REPOS = 30      # pulls lists per scan
ASKED_FILE = "asked.json"
KEEP_ASKED = 2000   # entries the memory keeps, newest first
SUBMITTED = {"APPROVED", "REQUEST_CHANGES", "COMMENT"}
ASKED, NOT_ASKED = "review requested", "review not requested yet"


def _mod(name: str):
    return importlib.import_module(f"{__package__}.{name}")


def open_pulls(client: Any, *, limit: int = 1) -> "tuple[dict, list]":
    """``({repo: [pull, ...]}, notes)`` for every repo with an open pull request the account sees: one issue
    search, then one pulls list per repo (the search's items carry no reviewers; measured on Forgejo 15).
    ``limit`` lists run at once: the desk overlaps them, the tick keeps one so a 429 ends it at that request."""
    ForgejoError = _mod("client").ForgejoError
    found = client.paginate("/repos/issues/search", {"type": "pulls", "state": "open"}, max_items=MAX_SEARCH)
    repos: list = []
    for item in found.items:
        repo = (item.get("repository") or {}).get("full_name")
        if repo and repo not in repos:
            repos.append(repo)
    notes = []
    if found.has_more:
        notes.append(f"Only the {MAX_SEARCH} most recent open pull requests were checked for review requests.")
    if len(repos) > MAX_REPOS:
        notes.append(f"Review requests were checked in {MAX_REPOS} of {len(repos)} repos with open pull requests.")
        repos = repos[:MAX_REPOS]

    def read(repo: str) -> "tuple[Any, str]":
        try:
            page = client.paginate(f"/repos/{repo}/pulls", {"state": "open"}, max_items=100)
        except ForgejoError as e:
            return None, f"{repo}: its open pull requests could not be read ({e.message})"
        # an incomplete scan: the desk never prunes parked rows on it
        return page.items, f"{repo}: only its 100 most recent open pull requests were read." if page.has_more else ""

    out: dict = {}
    for repo, (items, note) in zip(repos, _mod("client").parallel_map(read, repos, limit)):
        if items is not None:
            out[repo] = items
        if note:
            notes.append(note)
    return out, notes


def _login(user: Any) -> str:
    return str((user or {}).get("login") or "").lower()


def asked(pr: dict, you: str) -> bool:
    return you.lower() in {_login(u) for u in pr.get("requested_reviewers") or []}


def needs_request(pr: dict, you: str, reviews: Callable[[], list]) -> bool:
    """True when ``you`` should be asked on ``pr`` and is not. ``reviews`` reads the PR's reviews; it is called
    only for a pull request that passes the cheap checks."""
    if not you or not int(pr.get("number") or 0) or _login(pr.get("user")) == you.lower() or asked(pr, you):
        return False
    return not any(_login(r.get("user")) == you.lower() and r.get("state") in SUBMITTED and not r.get("dismissed")
                   for r in reviews() or [])


def reviews_reader(client: Any, repo: str, number: int) -> Callable[[], list]:
    return lambda: client.paginate(f"/repos/{repo}/pulls/{number}/reviews", {}, max_items=100).items


def _read_asked(data_dir: Any) -> dict:
    try:
        data = json.loads((Path(data_dir) / ASKED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_asked(data_dir: Any, asked_at: dict) -> None:
    newest = sorted(asked_at, key=lambda ref: asked_at[ref], reverse=True)[:KEEP_ASKED]
    _mod("flows.state").atomic_write(Path(data_dir) / ASKED_FILE,
                                     json.dumps({ref: asked_at[ref] for ref in newest}))


def sweep(access: Any, request: Callable[[str, int, str], None], *, data_dir: Any = None,
          now: Callable[[], float] = time.time) -> dict:
    """Ask ``settings.you`` on every open pull request that needs it, where the agent account may write.
    ``request(repo, number, you)`` is the write (the engine's ``FlowWriter``, so dry run applies). Returns
    ``{requested: ["repo#n"], notes: [...], errors: [...]}``.

    With ``data_dir`` a pull request asks a user once: ``asked.json`` there holds ``{"owner/repo#n@user": epoch}``
    for every pull request that asked that user, whoever sent the request. Removing the request is respected.
    A changed ``you`` has no entries yet, so the new user is asked everywhere. A dry run leaves the file alone."""
    settings = access.settings
    report: dict = {"requested": [], "notes": [], "errors": []}
    if not settings.review_every_pr or not settings.you:
        return report
    ForgejoError, acc = _mod("client").ForgejoError, _mod("access")
    reader = access.reader()
    pulls, report["notes"] = open_pulls(reader)
    remembered = _read_asked(data_dir) if data_dir else {}
    asked_now: dict = {}

    def memory_key(repo: str, n: int) -> str:
        return f"{repo}#{n}@{settings.you.lower()}"

    for repo, prs in pulls.items():
        due = []
        for pr in prs:
            n = int(pr.get("number") or 0)
            if memory_key(repo, n) in remembered:
                continue
            if asked(pr, settings.you):
                asked_now[memory_key(repo, n)] = now()
                continue
            try:
                if needs_request(pr, settings.you, reviews_reader(reader, repo, n)):
                    due.append(n)
            except ForgejoError as e:
                report["errors"].append(f"{repo}#{n}: its reviews could not be read ({e.message})")
        if not due:
            continue
        right = access.right(repo)
        if right not in acc.WRITE_RIGHTS:
            report["notes"].append(f"{repo}: {len(due)} open pull request(s) do not ask {settings.you} to review. "
                                   f"Hermes' account has {right or 'no'} access there, so it cannot ask. Change that "
                                   f"at {acc.collaboration_url(settings.url, repo)}. The Forgejo desk lists them anyway.")
            continue
        for n in due:
            try:
                request(repo, n, settings.you)
                report["requested"].append(f"{repo}#{n}")
            except ForgejoError as e:
                report["errors"].append(f"{repo}#{n}: asking {settings.you} to review failed ({e.message})")
                continue
            asked_now[memory_key(repo, n)] = now()
    if data_dir and asked_now and not settings.dry_run:
        _write_asked(data_dir, {**remembered, **asked_now})
    return report
