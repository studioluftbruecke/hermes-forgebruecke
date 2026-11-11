"""Version skew in a long-lived mention channel (0.0.21 plan V12). On a server the gateway kept a 0.0.17 adapter
running across the update to 0.0.19: it held a ledger object built from the old ``Ledger`` class, while
``_mod`` handed it the new poller, whose ``find_mentions`` reads ``ledger.overlap_due``. Every poll then failed
with ``'Ledger' object has no attribute 'overlap_due'``, every 30 s, for hours. These tests rebuild that state.
"""
import asyncio
import json
import sys
import types

from fb_fakes import sub
from test_adapter import Channel, adapter_mod  # noqa: F401 (adapter_mod is a fixture)

poller = sub("channel.poller")


class LedgerFrom0017:
    """The 0.0.17 ledger's shape (``git show 82c64bd:plugins/forgebruecke/channel/poller.py``): no
    ``overlap_due``, and ``rate_ok`` takes two arguments."""

    def __init__(self, since):
        self.since, self.seen_at, self.answered, self.replies = since, 0.0, {}, {}

    def was_answered(self, key):
        return key in self.answered

    def rate_ok(self, thread, now):
        return len([t for t in self.replies.get(thread, []) if now - t < 3600]) < poller.RATE_PER_HOUR


def test_a_ledger_object_from_older_code_does_not_break_a_poll(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    old = LedgerFrom0017("2026-11-08T13:00:00Z")
    found = poller.find_mentions(ch.adapter._client, bot="forgebruecke-bot", settings=ch.settings(), ledger=old,
                                 now=ch.now + 60)
    assert [m.comment_id for m in found] == [900, 901]
    assert old.since == "2026-11-08T13:00:30Z" and old.overlap_due is True  # the cursor moved on the given object


def _events(ch):
    return [json.loads(line) for line in ch.adapter.events_path.read_text(encoding="utf-8").splitlines()]


def test_an_update_that_half_arrived_stops_the_poll_loop_and_says_restart(adapter_mod, tmp_path, monkeypatch):
    ch = Channel(adapter_mod, tmp_path)
    adapter = ch.adapter
    adapter._config = lambda: {}
    monkeypatch.setattr(sub("profile"), "enabled_in", lambda config: True)
    adapter._take_lease()
    adapter._alive = True
    version = types.ModuleType(sub("version").__name__)
    version.VERSION = "9.9.9"  # the plugin's modules were reloaded from a newer copy; this adapter is older
    monkeypatch.setitem(sys.modules, sub("version").__name__, version)
    polls = []
    monkeypatch.setattr(adapter, "_poll_once", lambda settings: polls.append(1) or asyncio.sleep(0))
    monkeypatch.setattr(adapter, "_flows_tick", lambda settings: asyncio.sleep(0))

    async def sleep(_s):
        if len(polls) > 3:  # the old behaviour: poll on and on
            adapter._alive = False

    class Asyncio:
        def __getattr__(self, name):
            return sleep if name == "sleep" else getattr(asyncio, name)

    monkeypatch.setattr(adapter_mod, "asyncio", Asyncio())
    asyncio.run(asyncio.wait_for(adapter._loop(), 2))
    events = [e for e in _events(ch) if e["event"] not in ("init",)]
    stale = [e for e in events if e["event"] == "update_pending"]
    assert polls == [] and adapter._alive is False
    assert stale and "Restart the gateway to finish the update" in stale[0]["message"]
    assert stale[0]["found"] == "9.9.9"
    marker = json.loads((adapter._state_dir / "channel-update-pending.json").read_text(encoding="utf-8"))
    assert marker["found"] == "9.9.9" and marker["running"] != "9.9.9"
