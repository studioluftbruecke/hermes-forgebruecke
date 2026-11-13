"""A gateway loop that cannot load Forgejo access does not pass for a tick: the settings page keeps saying
that the background stopped, now with the reason, and the mention channel's tick is still due."""
import asyncio

from fb_fakes import sub
from fb_fakes_engine import Kit

loop_mod, desk = sub("flows.loop"), sub("desk")
NO_SCOPE = "get_secret('FORGEBRUECKE_BOT_ACCOUNT_TOKEN') called with no profile secret scope active"


def one_loop_pass(data_dir, now: float, monkeypatch) -> None:
    def broken_access():
        raise RuntimeError(NO_SCOPE)

    async def stop(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(loop_mod.asyncio, "sleep", stop)
    monkeypatch.setattr(loop_mod.time, "time", lambda: now)
    try:
        asyncio.run(loop_mod.gateway_loop(broken_access, lambda: 30, data_dir, lambda: True))
    except asyncio.CancelledError:
        pass


def test_a_loop_without_access_keeps_the_stopped_note_and_says_why(tmp_path, monkeypatch):
    k = Kit(tmp_path)
    k.tick()
    record = k.store.engine()
    record["at"] = int(k.clock() - 3600)
    k.store.write_engine(record)
    now = float(record["at"] + 3600)
    settings = k.access().settings
    assert desk.background(tmp_path, settings, lambda: now)["text"].startswith("Mentions and review requests stopped")

    one_loop_pass(tmp_path, now, monkeypatch)

    after = desk.background(tmp_path, settings, lambda: now + 5)
    assert after is not None and after["text"] == "Mentions and review requests stopped 1 h ago."
    assert "no Forgejo access" in after["fix"] and "secret scope" in after["fix"]
    assert loop_mod.due(k.store, 30, now + 5) is True
    assert k.store.engine()["at"] == record["at"]


def test_a_tick_that_ran_clears_the_reason(tmp_path, monkeypatch):
    k = Kit(tmp_path)
    one_loop_pass(tmp_path, k.clock(), monkeypatch)
    assert k.store.engine()["failure"]["source"] == "gateway-loop"
    k.tick()
    assert "failure" not in k.store.engine()
