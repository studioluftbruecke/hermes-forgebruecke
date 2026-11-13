"""The version both halves report, the flows note only for flow rows, what
stopped in the background, an unprotected main under Security audit, and a token the plugin host has not
picked up yet."""
import json
import os

import pytest

from fb_fakes import BASE, make_token, sub
from test_r2_page_desk import Clock, forge, pr_state, read  # noqa: F401 (fresh_cache is autouse there only)

desk = sub("desk")
access_mod = sub("access")
client_mod = sub("client")
approval = sub("approval")
state_mod = sub("flows.state")
VERSION = sub("version").VERSION
BOT = make_token("bot")


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(client_mod, "BACKOFF", (0.0, 0.0, 0.0))
    desk.clear_cache()
    access_mod.clear_rights()
    yield
    desk.clear_cache()
    access_mod.clear_rights()


def settings_body(tmp_path, f, *, home=None, **values):
    values = {"url": BASE, "dry_run": False, "you": "acme", **values}
    return desk.api_settings(lambda k, d=None: values.get(k, d),
                             {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get, transport=f, data_dir=tmp_path,
                             home=home, refresh=True)[1]


# --- version ---------------------------------------------------------------------------------------

def test_desk_and_settings_say_which_version_answers(tmp_path):
    assert read(tmp_path, forge())["version"] == VERSION
    assert settings_body(tmp_path, forge())["version"] == VERSION


# --- flows note ----------------------------------------------------------------------------------

def test_a_late_tick_says_nothing_about_flows_when_no_flow_row_is_on_the_desk(tmp_path):
    body = read(tmp_path, forge(), engine_age=7200)  # review_every_pr gives the tick work, no flow row
    assert not body["rows"] or not any(r.get("flow") for r in body["rows"])
    assert not any("last ran" in n for n in body["notes"])
    with_flow = read(tmp_path / "b", forge(), pr_state(3), engine_age=7200, refresh=True)
    assert "Flows last ran 2 h ago. Rows of a flow may be out of date." in with_flow["notes"]


# --- background ----------------------------------------------------------------------------------

def test_the_settings_page_says_when_the_background_stopped(tmp_path):
    clock = Clock(10_000)
    s = sub("settings").load_settings(lambda k, d=None: {"url": BASE, "you": "acme"}.get(k, d))
    assert desk.background(tmp_path, s, clock) is None  # no tick yet
    state_mod.FlowStore(tmp_path).write_engine({"at": clock() - 3420, "source": "gateway", "errors": []})
    late = desk.background(tmp_path, s, clock)
    assert late == {"text": "Mentions and review requests stopped 57 min ago.", "fix": desk.STOPPED_FIX,
                    "since": clock() - 3420}
    (tmp_path / desk.UPDATE_PENDING).write_text(json.dumps({"at": clock() - 600, "running": "0.0.20",
                                                            "found": VERSION}), encoding="utf-8")
    pending = desk.background(tmp_path, s, clock)
    assert pending["fix"] == "Restart the gateway to finish the update." and "10 min ago" in pending["text"]
    on_time = sub("settings").load_settings(lambda k, d=None: {"url": BASE}.get(k, d))  # no flows, no you
    (tmp_path / desk.UPDATE_PENDING).unlink()
    assert desk.background(tmp_path, on_time, clock) is None
    assert desk.background(None, s, clock) is None


def test_the_settings_answer_carries_the_background_line(tmp_path):
    state_mod.FlowStore(tmp_path).write_engine({"at": 1, "source": "gateway", "errors": []})
    body = settings_body(tmp_path, forge())
    assert body["background"]["text"].startswith("Mentions and review requests stopped")


# --- unprotected main ----------------------------------------------------------------------------

def test_audit_off_reads_no_branch_rule(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sub("tools.repos"), "_protection", lambda *a: calls.append(a) or {"protected": False})
    access = type("A", (), {"settings": type("S", (), {"audit_enabled": False})(), "repos": lambda self: []})()
    assert desk.unprotected_mains(access) == [] and calls == []


def test_audit_on_lists_admin_repos_whose_default_branch_has_no_rule(monkeypatch):
    repos = [{"repo": "acme/a", "right": "admin", "url": f"{BASE}/acme/a", "default_branch": "main"},
             {"repo": "acme/b", "right": "admin", "url": f"{BASE}/acme/b", "default_branch": "trunk"},
             {"repo": "acme/c", "right": "write", "url": f"{BASE}/acme/c", "default_branch": "main"},
             {"repo": "acme/d", "right": "admin", "url": f"{BASE}/acme/d", "archived": True}]
    rules = {"acme/a": {"protected": False}, "acme/b": {"protected": True}}
    seen = []

    def protection(access, repo, branch):
        seen.append((repo, branch))
        return {"branch": branch, **rules[repo]}

    monkeypatch.setattr(sub("tools.repos"), "_protection", protection)
    settings = type("S", (), {"audit_enabled": True, "url": BASE})()
    access = type("A", (), {"settings": settings, "repos": lambda self: repos})()
    assert desk.unprotected_mains(access) == [{"repo": "acme/a", "branch": "main",
                                               "url": f"{BASE}/acme/a/settings/branches"}]
    assert seen == [("acme/a", "main"), ("acme/b", "trunk")]  # write-only and archived repos are not read


# --- a token the plugin host has not picked up ---------------------------------------------------

def test_a_token_saved_after_the_plugin_host_started_is_reported(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr(desk, "_process_started", lambda: os.path.getmtime(env) - 10)
    monkeypatch.delenv(desk.HOST_PROCESS_ENV, raising=False)
    assert desk.tokens_unseen(tmp_path) is False  # the dashboard itself: never
    monkeypatch.setenv(desk.HOST_PROCESS_ENV, "1")
    assert desk.tokens_unseen(tmp_path) is True
    monkeypatch.setattr(desk, "_process_started", lambda: os.path.getmtime(env) + 10)
    assert desk.tokens_unseen(tmp_path) is False
    assert desk.tokens_unseen(tmp_path / "missing") is False


def test_the_settings_answer_says_reload_without_waiting_for_its_cache(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("X=1\n", encoding="utf-8")
    monkeypatch.setenv(desk.HOST_PROCESS_ENV, "1")
    monkeypatch.setattr(desk, "_process_started", lambda: 0.0)
    assert settings_body(tmp_path, forge(), home=home)["reload"] == {"needed": True, "text": desk.RELOAD_TEXT}
    monkeypatch.setattr(desk, "_process_started", lambda: 4e9)
    values = {"url": BASE, "dry_run": False, "you": "acme"}
    cached = desk.api_settings(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                               transport=forge(), data_dir=tmp_path, home=home)[1]
    assert cached["reload"] == {"needed": False, "text": None}


def test_the_process_start_is_read_from_proc_on_linux():
    started = desk._process_started()
    assert 0 < started <= desk.time.time()


def test_load_it_now_is_logged_and_keeps_only_forgebrueckes_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(desk, "_RELOADED", {})
    monkeypatch.setattr(desk, "_RELOADED_AT", 0.0)
    status, body = desk.api_reload_tokens(home=tmp_path, data_dir=tmp_path,
                                          load=lambda home: {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "OTHER": "x"})
    assert status == 200 and body["ok"]
    line = json.loads((tmp_path / "desk-actions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["action"] == "reload_tokens" and line["tokens"] == ["FORGEBRUECKE_BOT_ACCOUNT_TOKEN"]
    assert BOT not in json.dumps(line)


def test_the_settings_guard_asks_before_a_shell_reloads_the_plugin_host():
    from test_r2_guard_fails_closed import BASE as ROUTES, HOME
    command = f"curl -s -X POST {ROUTES}/reload-tokens -d '{{}}'"
    assert "page route" in approval.settings_touch("terminal", {"command": command}, HOME)
