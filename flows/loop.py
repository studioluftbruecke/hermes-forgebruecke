"""The three tick sources share one ``tick()``:

- the gateway: a supervised ``ctx.spawn_task`` loop when ``register()`` runs on an event loop, and the
  mention channel's poll loop (it already runs on the gateway's loop with the profile's token);
- ``hermes forgebruecke tick``: a cron job or a person;
- ``forgejo_flows action=run``: one pull request, now.

The store's lock makes an overlap harmless, and ``due()`` skips a loop tick when another source ran a full
tick within the poll interval, so two loops do not double the forge traffic. A tick on one pull request
leaves the stamp alone.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _mod(name: str):
    pkg = (__package__ or "").rpartition(".")[0] or __package__
    return importlib.import_module(f".{name}", pkg)


def due(store, interval_s: float, now: Optional[float] = None) -> bool:
    """True when no full tick ran within the interval. A stamp from the future (the host clock was wrong
    when it was written) counts as none."""
    last = (store.engine() or {}).get("at")
    now = time.time() if now is None else now
    return not isinstance(last, (int, float)) or last > now or now - last >= interval_s * 0.8


def has_work(settings) -> bool:
    """A tick has something to do: a repo runs a flow, or open pull requests should ask you."""
    return any(r.flows for r in settings.writes) or bool(settings.review_every_pr and settings.you)


def run_tick(access, data_dir: Any, source: str, only: Optional[tuple] = None, runner: Any = None) -> dict:
    store = _mod("flows.state").FlowStore(data_dir)
    return _mod("flows.engine").Engine(access, store, runner=runner, source=source).tick(only=only)


def channel_tick(settings, token: str, data_dir: Any, source: str = "channel") -> Optional[dict]:
    """A tick from the mention channel's loop, with the token the adapter was built with."""
    if not data_dir or not token or not settings.url:
        return None
    store = _mod("flows.state").FlowStore(data_dir)
    if not due(store, settings.poll_interval):
        return None
    if not has_work(settings):
        return None  # no flow and nobody to ask: no forge traffic at all
    creds = _mod("settings").Credentials(token)
    access = _mod("access").Access(settings, creds)
    return run_tick(access, data_dir, source)


async def gateway_loop(load_access: Callable[[], Any], get_interval: Callable[[], int], data_dir: Any,
                       enabled_here: Callable[[], bool]) -> None:
    """The spawn_task loop. Skips when this context has no token, and says why under ``failure`` in engine.json."""
    store = _mod("flows.state").FlowStore(data_dir)
    while True:
        interval = 120
        try:
            interval = max(30, int(get_interval()))
            if enabled_here() and due(store, interval):
                try:
                    access = load_access()
                except Exception as e:
                    # Kept beside the last tick's stamp, never as one: a loop that cannot act must not look like
                    # a tick to due() or to the settings page.
                    store.write_engine({**(store.engine() or {}), "failure": {
                        "at": int(time.time()), "source": "gateway-loop",
                        "error": f"no Forgejo access in this context: {getattr(e, 'message', e)}"}})
                    access = None
                if access is not None and has_work(access.settings):
                    await asyncio.to_thread(run_tick, access, data_dir, "gateway-loop")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("forgebruecke flows loop tick failed", exc_info=True)
        await asyncio.sleep(interval)
