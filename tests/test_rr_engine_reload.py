""""Load it now" on the settings page reads Forgebrücke's two tokens again through Hermes' own secret loader
and uses them in this process. It never ends the process: the plugin host it runs in serves every other
plugin of the profile too."""
import os

import pytest

from fb_fakes import BASE, make_token, sub
from test_r2_page_desk import forge

desk = sub("desk")
access_mod = sub("access")
client_mod = sub("client")
BOT = make_token("bot")
TOKEN = "FORGEBRUECKE_BOT_ACCOUNT_TOKEN"


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(client_mod, "BACKOFF", (0.0, 0.0, 0.0))
    monkeypatch.setattr(desk, "_RELOADED", {}, raising=False)
    monkeypatch.setattr(desk, "_RELOADED_AT", 0.0, raising=False)
    monkeypatch.setenv(desk.HOST_PROCESS_ENV, "1")
    desk.clear_cache()
    access_mod.clear_rights()
    yield
    desk.clear_cache()
    access_mod.clear_rights()


def never_exit(monkeypatch) -> list:
    ended = []
    monkeypatch.setattr(desk.os, "_exit", lambda code: ended.append(code))
    monkeypatch.setattr(desk.threading, "Timer", lambda delay, fn, args=(): ended.append(("timer", fn)) or _Started())
    return ended


class _Started:
    daemon = True

    def start(self):
        pass


def settings(tmp_path, home, old_secrets: dict) -> dict:
    values = {"url": BASE, "dry_run": False, "you": "acme"}
    return desk.api_settings(lambda k, d=None: values.get(k, d), old_secrets.get, transport=forge(),
                             data_dir=tmp_path, home=home, refresh=True)[1]


def test_load_it_now_uses_a_new_token_without_ending_the_process(tmp_path, monkeypatch):
    ended = never_exit(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr(desk, "_process_started", lambda: os.path.getmtime(home / ".env") - 10)
    before = settings(tmp_path, home, {})
    assert before["reload"]["needed"] is True and not before["tokens"][0]["set"]

    status, body = desk.api_reload_tokens(home=home, data_dir=tmp_path,
                                          load=lambda h: {TOKEN: BOT, "OPENAI_API_KEY": "sk-other"})
    assert status == 200 and body["ok"] and ended == []
    after = settings(tmp_path, home, {})
    assert after["tokens"][0]["set"] is True and after["reload"]["needed"] is False
    assert desk._RELOADED == {TOKEN: BOT}  # no other secret is kept


def test_a_token_removed_since_is_gone_after_load_it_now(tmp_path, monkeypatch):
    never_exit(monkeypatch)
    desk.api_reload_tokens(home=tmp_path, data_dir=tmp_path, load=lambda h: {})
    assert settings(tmp_path, tmp_path, {TOKEN: BOT})["tokens"][0]["set"] is False


def test_hermes_loader_reads_the_profiles_secrets(tmp_path, monkeypatch):
    pytest.importorskip("agent.secret_scope")
    never_exit(monkeypatch)
    (tmp_path / ".env").write_text(f"{TOKEN}={BOT}\n", encoding="utf-8")
    status, _ = desk.api_reload_tokens(home=tmp_path, data_dir=tmp_path)
    assert status == 200 and desk._RELOADED == {TOKEN: BOT}
