"""Flow state on disk: one JSON file per pull request, written atomically under one lock.

Layout under the plugin's ``data_dir`` (``ctx.state.data_dir``; never built by hand):

    flows/prs/<owner>__<repo>/<pr>.json     the PR's flow state (shape below)
    flows/overrides/<flow>.yml              per-profile flow files (they win over the repo file)
    flows/control.json                      {"paused": ["<flow>", "<flow>@<owner/repo>", ...]}
    flows/engine.json                       the last tick: when, from where, what it did, errors
    flows/.lock                             fcntl lock: one tick at a time, across processes
    runs/<flow>/<owner>__<repo>-<pr>-<sha12>-<gate>.json   a review worker's verdict file
    runs/<flow>/<...>.prompt.md / .log      the worker's prompt and output

PR state: ``{repo, pr, flow, flow_version, gate_hashes[], head_sha, title, url, author, base, head_ref,
updated_at, closed, merged, status_comment_id, status_comment_hash, notified[], history[], rounds{},
gates[GateRun]}``. ``flow_version`` and ``gate_hashes`` say which definition of the flow the gates ran under.

Two ticks never run at once (the gateway loop, a cron ``hermes forgebruecke tick`` and a
``forgejo_flows action=run`` can overlap): ``tick_lock()`` takes the lock without waiting and the
second tick reports "busy" instead of acting. A crashed tick leaves no lock (the kernel drops it).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Optional

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def repo_slug(repo: str) -> str:
    owner, _, name = repo.partition("/")
    slug = f"{_SAFE.sub('_', owner)}__{_SAFE.sub('_', name)}"
    if not slug.strip("_.") or ".." in slug:
        raise ValueError(f"unsafe repo name {repo!r}")
    return slug


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class Busy(Exception):
    """Another tick holds the lock."""


class Unreadable(Exception):
    """A pull request's state file exists and is not a state. The message says which file and what to do."""


class FlowStore:
    def __init__(self, data_dir: Any) -> None:
        if not data_dir:
            raise ValueError("FlowStore needs the plugin's data_dir")
        self.root = Path(data_dir)
        self.flows = self.root / "flows"
        self.prs_dir = self.flows / "prs"
        self.overrides = self.flows / "overrides"
        self.runs = self.root / "runs"

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve()
        if self.root.resolve() not in resolved.parents and resolved != self.root.resolve():
            raise ValueError(f"path {path} leaves the plugin's data folder")
        return path

    @contextlib.contextmanager
    def tick_lock(self) -> Iterator[None]:
        self.flows.mkdir(parents=True, exist_ok=True)
        fh = open(self.flows / ".lock", "a+")
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy() from None
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    # --- PR state -----------------------------------------------------------------------------

    def pr_path(self, repo: str, pr: int) -> Path:
        return self._inside(self.prs_dir / repo_slug(repo) / f"{int(pr)}.json")

    def load_pr(self, repo: str, pr: int, *, strict: bool = False) -> Optional[dict]:
        """The stored state, or None when there is none. A file that does not parse also reads as None,
        unless ``strict``: the engine must not take a damaged file for a pull request it never saw."""
        path = self.pr_path(repo, pr)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("gates"), list):
                return state
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            pass
        if strict:
            raise Unreadable("its state file does not parse, so it was skipped. "
                             f"Delete flows/prs/{path.parent.name}/{path.name} to start its gates over.")
        return None

    def save_pr(self, state: dict) -> None:
        state["updated_at"] = int(time.time())
        atomic_write(self.pr_path(state["repo"], state["pr"]), json.dumps(state, ensure_ascii=False, indent=1))

    def all_prs(self) -> list:
        out = []
        if not self.prs_dir.exists():
            return out
        for path in sorted(self.prs_dir.glob("*/*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def running_reviews(self) -> int:
        return sum(1 for s in self.all_prs() if not s.get("closed")
                   for g in s.get("gates") or [] if g.get("type") == "review" and g.get("state") == "running")

    # --- control and overrides ----------------------------------------------------------------

    def control(self) -> dict:
        try:
            data = json.loads((self.flows / "control.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def set_paused(self, key: str, paused: bool) -> list:
        data = self.control()
        current = [k for k in data.get("paused") or [] if isinstance(k, str)]
        if paused and key not in current:
            current.append(key)
        if not paused:
            current = [k for k in current if k != key]
        data["paused"] = current
        atomic_write(self.flows / "control.json", json.dumps(data, indent=1))
        return current

    def is_paused(self, flow_id: str, repo: str) -> bool:
        paused = set(self.control().get("paused") or [])
        return flow_id in paused or f"{flow_id}@{repo}" in paused

    def override_files(self) -> list:
        if not self.overrides.exists():
            return []
        return sorted(p for p in self.overrides.iterdir() if p.suffix in (".yml", ".yaml", ".json"))

    def write_override(self, flow_id: str, text: str) -> Path:
        path = self._inside(self.overrides / f"{flow_id}.yml")
        atomic_write(path, text)
        return path

    # --- runs and the engine record -----------------------------------------------------------

    def verdict_path(self, flow_id: str, repo: str, pr: int, sha: str, gate_key: str) -> Path:
        return self._inside(self.runs / flow_id / f"{repo_slug(repo)}-{int(pr)}-{sha[:12]}-{gate_key}.json")

    def write_engine(self, record: dict) -> None:
        atomic_write(self.flows / "engine.json", json.dumps(record, ensure_ascii=False, indent=1))

    def engine(self) -> dict:
        try:
            return json.loads((self.flows / "engine.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
