"""The mention channel's real connect(): the first cursor comes from Forgejo's clock, not the host's, and a token
that cannot go into a header is refused by its field name, never by its value."""
import asyncio
import json
import sys
import types

import pytest

from fb_fakes import BASE, FakeForgejo, bot_account, make_token, sample_data, sub
from fb_fakes_workers import gateway_stubs

poller = sub("channel.poller")
settings_mod = sub("settings")
client_mod = sub("client")
BOT = make_token("bot")
FORGE_DATE = "Sun, 08 Nov 2026 13:00:00 GMT"
FORGE_NOW = poller.parse_ts("2026-11-08T13:00:00Z")


@pytest.fixture
def adapter_mod(monkeypatch):
    for name, module in gateway_stubs().items():
        monkeypatch.setitem(sys.modules, name, module)
    module = sub("channel.adapter")
    yield module
    sys.modules.pop(module.__name__, None)


class Dated:
    """The stand-in Forgejo with the Date header a real one sends on every answer."""

    def __init__(self, forge, date):
        self.forge, self.date = forge, date

    def __call__(self, method, url, headers, body, timeout):
        status, out, payload = self.forge(method, url, headers, body, timeout)
        return status, ({**out, "date": self.date} if self.date else out), payload


def connect(adapter_mod, tmp_path, monkeypatch, *, date, host_now, secret=BOT):
    forge = FakeForgejo({BOT: bot_account()}, data=sample_data())
    monkeypatch.setattr(client_mod, "default_transport", Dated(forge, date))
    adapter = adapter_mod.make_adapter(types.SimpleNamespace(extra={}), home=tmp_path / "home",
                                       data_dir=tmp_path / "state",
                                       get_secret={"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": secret}.get)
    adapter._config = lambda: {"plugins": {"enabled": ["forgebruecke"], "disabled": []},
                               "platforms": {"forgebruecke": {"enabled": True}},
                               "platform_toolsets": {"forgebruecke": ["forgebruecke"]}}
    adapter._settings = lambda config=None: settings_mod.load_settings(lambda k, d=None: {"url": BASE}.get(k, d))
    adapter._now = lambda: host_now

    async def run():
        ok = await adapter.connect()
        if ok:
            await adapter.disconnect()
        return ok

    return adapter, asyncio.run(run())


def events(adapter, name):
    lines = adapter.events_path.read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == name]


@pytest.mark.parametrize("skew", [300, -300, 5])
def test_the_first_cursor_is_forgejos_time_whatever_the_host_clock_says(adapter_mod, tmp_path, monkeypatch, skew):
    adapter, ok = connect(adapter_mod, tmp_path, monkeypatch, date=FORGE_DATE, host_now=FORGE_NOW + skew)
    assert ok
    assert poller.Ledger.load(adapter.ledger_path).since == "2026-11-08T13:00:00Z"


@pytest.mark.parametrize("date", [None, "not a date", "Sun, 08 Nov 2026 13:00:00"])
def test_without_a_readable_date_the_host_clock_is_the_first_cursor(adapter_mod, tmp_path, monkeypatch, date):
    adapter, ok = connect(adapter_mod, tmp_path, monkeypatch, date=date, host_now=FORGE_NOW + 60)
    assert ok
    assert poller.Ledger.load(adapter.ledger_path).since == "2026-11-08T13:01:00Z"


def test_a_cursor_already_on_disk_is_kept(adapter_mod, tmp_path, monkeypatch):
    path = tmp_path / "state" / "channel-ledger.json"
    path.parent.mkdir(parents=True)
    poller.Ledger({"since": "2026-11-01T00:00:00Z"}).save(path)
    adapter, ok = connect(adapter_mod, tmp_path, monkeypatch, date=FORGE_DATE, host_now=FORGE_NOW)
    assert ok and poller.Ledger.load(adapter.ledger_path).since == "2026-11-01T00:00:00Z"


@pytest.mark.parametrize("secret, found", [(BOT[:20] + "\n" + BOT[20:], "a space or line break"),
                                           (BOT[:20] + "\u00e9" + BOT[20:], "a character no token has")])
def test_a_token_that_cannot_go_into_a_header_is_refused_by_its_name(adapter_mod, tmp_path, monkeypatch,
                                                                       secret, found):
    adapter, ok = connect(adapter_mod, tmp_path, monkeypatch, date=FORGE_DATE, host_now=FORGE_NOW, secret=secret)
    assert not ok
    [refused] = events(adapter, "connect_refused")
    assert refused["reason"] == f"Hermes' token contains {found}. Enter it again."
    assert BOT[:20] not in adapter.events_path.read_text(encoding="utf-8")


def test_an_unset_token_is_named_as_unset(adapter_mod, tmp_path, monkeypatch):
    adapter, ok = connect(adapter_mod, tmp_path, monkeypatch, date=FORGE_DATE, host_now=FORGE_NOW, secret=None)
    assert not ok and events(adapter, "connect_refused")[0]["reason"] == "FORGEBRUECKE_BOT_ACCOUNT_TOKEN is not set"
