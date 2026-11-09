"""Plugin state: small pointers in ``ctx.state``, bulky records as files under its ``data_dir``.

Keys (PLAN.md M1): ``version`` (state schema), ``threads`` (repo#n -> session id), ``seen`` (the last
500 handled notification or comment keys), ``gates``, ``claims``, ``repos``, ``capabilities``. Run
records and logs never go into the key/value store (it has a 10 MB quota); they go to
``<data_dir>/runs/``.

``StateStore`` takes any object with ``get(key, default)``, ``set(key, value)`` and ``data_dir``, so
tests pass a dict-backed fake and ``register()`` passes ``ctx.state``.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
SEEN_MAX = 500
DEFAULT_KEYS: dict = {"threads": {}, "seen": [], "gates": {}, "claims": {}, "repos": {}, "capabilities": {}}


class StateStore:
    def __init__(self, kv: Any) -> None:
        self._kv = kv

    @property
    def data_dir(self) -> Path:
        return Path(self._kv.data_dir)

    def runs_dir(self) -> Path:
        d = self.data_dir / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def ensure(self) -> int:
        """Create missing keys and stamp the schema version; returns the version found before."""
        found = self._kv.get("version", 0) or 0
        for key, default in DEFAULT_KEYS.items():
            if self._kv.get(key, None) is None:
                self._kv.set(key, default)
        if found != SCHEMA_VERSION:
            self._kv.set("version", SCHEMA_VERSION)
        return found

    def get(self, key: str, default: Any = None) -> Any:
        return self._kv.get(key, default)

    def seen(self) -> list:
        return list(self._kv.get("seen", []) or [])

    def remember_seen(self, keys: Iterable[str]) -> list:
        """Append new keys, keep order, keep only the last ``SEEN_MAX``."""
        current = self.seen()
        have = set(current)
        for k in keys:
            if k not in have:
                current.append(k)
                have.add(k)
        current = current[-SEEN_MAX:]
        self._kv.set("seen", current)
        return current

    def save_capabilities(self, base_url: str, caps: dict) -> None:
        self._kv.set("capabilities", {"base_url": base_url, "checked_at": int(time.time()), **caps})
