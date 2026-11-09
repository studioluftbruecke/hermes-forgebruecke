"""The three tick sources share one ``tick()`` (PLAN.md M6, "Tick source"):

- the gateway: a supervised ``ctx.spawn_task`` loop when ``register()`` runs on an event loop, and the
  mention channel's poll loop (it already runs on the gateway's loop with the profile's token);
- ``hermes forgebruecke tick``: a cron job or a person;
- ``forgejo_flows action=run``: one pull request, now.

The store's lock makes an overlap harmless, and ``due()`` skips a loop tick when another source ticked
within the poll interval, so two loops do not double the forge traffic.
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
    last = (store.engine() or {}).get("at")
    now = time.time() if now is None else now
    return not isinstance(last, (int, float)) or now - last >= interval_s * 0.8


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
    if not any(r.flows for r in settings.writes):
        return None  # no repo runs a flow: no forge traffic at all
    creds = _mod("settings").Credentials(token)
    access = _mod("access").Access(settings, creds)
    return run_tick(access, data_dir, source)


async def gateway_loop(load_access: Callable[[], Any], get_interval: Callable[[], int], data_dir: Any,
                       enabled_here: Callable[[], bool]) -> None:
    """The spawn_task loop. Skips quietly when this context has no token (it says why in engine.json)."""
    store = _mod("flows.state").FlowStore(data_dir)
    while True:
        interval = 120
        try:
            interval = max(30, int(get_interval()))
            if enabled_here() and due(store, interval):
                try:
                    access = load_access()
                except Exception as e:
                    store.write_engine({"at": int(time.time()), "source": "gateway-loop", "ok": False,
                                        "errors": [f"no forge access in this context: {getattr(e, 'message', e)}"]})
                    access = None
                if access is not None and any(r.flows for r in access.settings.writes):
                    await asyncio.to_thread(run_tick, access, data_dir, "gateway-loop")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("forgebruecke flows loop tick failed", exc_info=True)
        await asyncio.sleep(interval)
