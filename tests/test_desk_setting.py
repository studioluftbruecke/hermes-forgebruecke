"""The settings page's backend: the five settings it may save on a click, the one next action, who `you`
probably is, and two routes that answer with a sentence when something goes wrong."""
import json
import subprocess

import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, make_token, owner_account, repo, sample_data, sub
from fb_fakes_desktop import UsersForge

desk = sub("desk")
state_mod = sub("flows.state")
BOT, OWNER = make_token("bot"), make_token("owner")
ENV = {"bot": ("FORGEBRUECKE_BOT_ACCOUNT_TOKEN", BOT), "owner": ("FORGEBRUECKE_PERSONAL_WRITE_TOKEN", OWNER)}


@pytest.fixture(autouse=True)
def fresh_cache():
    desk.clear_cache()
    yield
    desk.clear_cache()


def owner_write_account():
    return {**owner_account(), "scopes": {"write:repository", "write:issue", "read:organization", "read:user"}}


def forge(**kw):
    inner = FakeForgejo({BOT: bot_account(), OWNER: owner_write_account()}, data=sample_data())
    return UsersForge(inner, **{"users": {"acme", "kay"}, "orgs": {"studio"}, **kw})


def getters(tokens=("bot",), **values):
    secrets = dict(ENV[t] for t in tokens)
    values = {"url": BASE, **values}
    return (lambda k, d=None: values.get(k, d)), secrets.get


class Saver:
    def __init__(self, error=None):
        self.saved, self.error = [], error

    def __call__(self, key, value):
        if self.error:
            raise self.error
        self.saved.append((key, value))


def set_setting(body, tmp_path=None, save=None, f=None, tokens=("bot",), **values):
    gs, sec = getters(tokens, **values)
    save = save if save is not None else Saver()
    out = desk.api_set_setting(body, save=save, get_setting=gs, get_secret=sec, transport=f or forge(),
                               data_dir=tmp_path, now=lambda: 1000)
    return out, save


# --- POST /setting ------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["dry_run", "review_every_pr", "learn_mode", "audit_enabled"])
def test_a_switch_saves_a_real_boolean_logs_it_and_clears_the_cache(tmp_path, key):
    desk._cache[("settings",)] = (10**12, {"stale": True})
    (status, body), save = set_setting({"key": key, "value": False}, tmp_path)
    assert (status, body) == (200, {"ok": True, "key": key, "value": False})
    assert save.saved == [(key, False)] and desk._cache == {}
    log = [json.loads(x) for x in (tmp_path / "desk-actions.jsonl").read_text().splitlines()]
    assert log == [{"at": 1000, "action": "setting", "key": key, "value": False}]


@pytest.mark.parametrize("body", [
    {"key": "dry_run", "value": "false"}, {"key": "dry_run", "value": 0}, {"key": "dry_run"},
    {"key": "url", "value": "https://elsewhere.example"}, {"key": "writes", "value": []},
    {"key": "owner_token", "value": "x"}, {"key": "you", "value": 7}, {"key": "you", "value": "not a name"},
    {}, None, "dry_run",
])
def test_bad_input_answers_400_with_a_sentence_and_saves_nothing(tmp_path, body):
    (status, out), save = set_setting(body, tmp_path)
    assert status == 400 and out["ok"] is False and out["error"] == "bad_request"
    assert out["message"].endswith(".") and len(out["message"]) <= 140
    assert save.saved == [] and not (tmp_path / "desk-actions.jsonl").exists()


def test_you_is_checked_on_the_forge_and_saved_as_forgejo_spells_it(tmp_path):
    f = forge()
    (status, body), save = set_setting({"key": "you", "value": " @ACME "}, tmp_path, f=f)
    assert (status, body) == (200, {"ok": True, "key": "you", "value": "acme"})
    assert save.saved == [("you", "acme")] and f.asked == ["users/ACME", "orgs/ACME"]


def test_you_must_exist_and_must_not_be_an_organisation(tmp_path):
    (status, body), save = set_setting({"key": "you", "value": "nobody"}, tmp_path)
    assert status == 400 and body["message"] == "Forgejo has no user nobody."
    (status, body), save2 = set_setting({"key": "you", "value": "studio"}, tmp_path)
    assert status == 400 and body["message"] == "studio is an organisation. Enter your own user name."
    assert save.saved == [] and save2.saved == []


def test_an_empty_you_clears_it_without_asking_the_forge(tmp_path):
    f = forge()
    (status, body), save = set_setting({"key": "you", "value": ""}, tmp_path, f=f)
    assert status == 200 and body["value"] == "" and save.saved == [("you", "")] and f.asked == []


def test_a_forge_that_fails_answers_502_and_saves_nothing(tmp_path):
    (status, body), save = set_setting({"key": "you", "value": "acme"}, tmp_path, f=forge(down=True))
    assert status == 502 and body["error"] == "forge_error" and body["message"] and save.saved == []


def test_you_without_an_agent_token_says_what_is_missing(tmp_path):
    (status, body), save = set_setting({"key": "you", "value": "acme"}, tmp_path, tokens=())
    assert status == 400 and body["error"] == "not_configured" and save.saved == []


def test_a_refused_or_failing_save_answers_with_a_sentence(tmp_path):
    (status, body), _ = set_setting({"key": "learn_mode", "value": True}, tmp_path,
                                    save=Saver(PermissionError("Plugin settings cannot be changed in a managed install")))
    assert status == 403 and body["message"] == "Plugin settings cannot be changed in a managed install"
    (status, body), _ = set_setting({"key": "learn_mode", "value": True}, tmp_path, save=Saver(OSError("disk")))
    assert status == 500 and body["error"] == "save_failed" and "OSError" in body["message"]
    assert not (tmp_path / "desk-actions.jsonl").exists()


def test_a_setting_is_saved_even_when_the_action_log_cannot_be_written(tmp_path, monkeypatch):
    monkeypatch.setattr(desk, "_log", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    (status, body), save = set_setting({"key": "learn_mode", "value": True}, tmp_path)
    assert status == 200 and body["ok"] and save.saved == [("learn_mode", True)] and "log" in body["note"]


# --- GET /settings ------------------------------------------------------------------------------------

def settings_body(tmp_path, tokens=("bot",), f=None, **values):
    gs, sec = getters(tokens, **values)
    return desk.api_settings(gs, sec, transport=f or forge(), home=tmp_path, now=lambda: 1000)[1]


def test_the_settings_body_carries_the_four_switches_as_they_are_set(tmp_path):
    body = settings_body(tmp_path, dry_run=False, learn_mode=True)
    assert body["switches"] == {"dry_run": False, "review_every_pr": True, "learn_mode": True, "audit_enabled": False}
    assert desk.SWITCHES == tuple(body["switches"])


@pytest.mark.parametrize("tokens,values,action,state", [
    ((), {"url": ""}, {"id": "set_url", "text": "Enter your Forgejo address."}, "setup"),
    ((), {}, {"id": "set_token", "text": "Add the token of Hermes' account."}, "setup"),
    (("bot",), {}, {"id": "set_you", "text": "Which Forgejo user are you?", "login": "acme"}, "setup"),
    (("bot",), {"you": "acme"}, {"id": "dry_run", "text": "Preview only: Hermes shows each write and sends none."},
     "preview"),
    (("bot",), {"you": "acme", "dry_run": False}, {"id": "none", "text": ""}, "ready"),
])
def test_one_next_action_in_the_plans_order(tmp_path, tokens, values, action, state):
    body = settings_body(tmp_path, tokens, **values)
    said = {k: v for k, v in body["next_action"].items() if k not in ("button", "row")}
    assert said == action and body["state"] == state


def test_a_refused_token_is_the_next_action_with_what_forgejo_said(tmp_path):
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": make_token("stranger")}
    body = desk.api_settings(lambda k, d=None: {"url": BASE}.get(k, d), secrets.get, transport=forge(),
                             home=tmp_path, now=lambda: 1000)[1]
    action = body["next_action"]
    assert action["id"] == "fix_token" and action["text"] == "Forgejo refused Hermes' token (401). Replace it."
    assert body["state"] == "setup"


def test_the_page_reads_the_doctors_facts_not_its_sentence(tmp_path, monkeypatch):
    root = desk._plugin_root()
    real = root.doctor_report

    def with_action(*a, **k):
        rep = real(*a, **k)
        rep["next_action"] = {"id": "none", "text": "Ready."}
        rep["you"] = {**rep["you"], "source": "repo_owner"}
        return rep
    monkeypatch.setattr(root, "doctor_report", with_action)
    body = settings_body(tmp_path)
    assert body["next_action"]["id"] == "set_you" and body["next_action"]["login"] == "acme"
    assert body["state"] == "setup" and body["you_source"] == "repo_owner"


def test_the_write_tokens_own_login_is_the_suggestion_for_you(tmp_path):
    inner = FakeForgejo({BOT: {**bot_account(), "repos": [repo("kay/notes", "write"), repo("kay/site", "write")]},
                         OWNER: owner_write_account()}, data={})
    body = settings_body(tmp_path, ("bot", "owner"), f=UsersForge(inner, users={"acme", "kay"}))
    assert body["you_suggestion"] == "acme" and body["you_source"] == "write_token"
    assert body["next_action"]["login"] == "acme"


def test_the_suggestion_from_repo_owners_skips_organisations(tmp_path):
    inner = FakeForgejo({BOT: {**bot_account(), "repos": [repo("studio/a", "write"), repo("studio/b", "write"),
                                                         repo("kay/notes", "write")]}}, data={})
    body = settings_body(tmp_path, f=UsersForge(inner, users={"kay"}, orgs={"studio"}))
    assert body["you_suggestion"] == "kay" and body["you_source"] == "repo_owner"
    only_orgs = FakeForgejo({BOT: {**bot_account(), "repos": [repo("studio/a", "write")]}}, data={})
    desk.clear_cache()
    sub("access").clear_rights()
    body = settings_body(tmp_path, f=UsersForge(only_orgs, orgs={"studio"}))
    assert body["you_suggestion"] is None and body["you_source"] is None
    assert body["next_action"]["id"] == "set_you" and "login" not in body["next_action"]


def test_an_owner_whose_kind_cannot_be_read_is_not_suggested(tmp_path):
    inner = FakeForgejo({BOT: {**bot_account(), "repos": [repo("studio/a", "write")]}}, data={})
    body = settings_body(tmp_path, f=UsersForge(inner, orgs={"studio"}, down=True))
    assert body["you_suggestion"] is None


# --- POST /merge and POST /reviewer-profile -------------------------------------------------------------

def stored_pr(tmp_path):
    state_mod.FlowStore(tmp_path).save_pr({
        "repo": DEMO, "pr": 3, "flow": "demo", "head_sha": HEAD_SHA, "title": "PR 3", "updated_at": 103,
        "url": f"{BASE}/{DEMO}/pulls/3",
        "gates": [{"gate_key": "human", "type": "human", "state": "waiting", "detail": "waiting for acme"}]})


@pytest.mark.parametrize("body", [
    {"repo": DEMO, "number": "abc", "head_sha": HEAD_SHA}, {"repo": "not a repo", "number": 3, "head_sha": HEAD_SHA},
    {"repo": DEMO, "number": 3, "head_sha": "abc"}, {"repo": DEMO, "number": [3], "head_sha": HEAD_SHA},
    {"repo": DEMO, "number": 0, "head_sha": HEAD_SHA}, None,
])
def test_merge_answers_400_for_input_it_cannot_parse_and_asks_the_forge_nothing(tmp_path, body):
    f = forge()
    gs, sec = getters(("bot", "owner"))
    status, out = desk.api_merge(gs, sec, body, data_dir=tmp_path, transport=f)
    assert status == 400 and out["error"] == "bad_request" and out["message"].endswith(".")
    assert f.inner.calls == []


def test_a_merge_that_landed_answers_200_when_the_action_log_fails(tmp_path, monkeypatch):
    f = forge()
    stored_pr(tmp_path)
    monkeypatch.setattr(desk, "_log", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    gs, sec = getters(("bot", "owner"), you="acme")
    status, out = desk.api_merge(gs, sec, {"repo": DEMO, "number": 3, "head_sha": HEAD_SHA}, data_dir=tmp_path,
                                 transport=f)
    assert status == 200 and out["merged"] is True and "log" in out["note"]
    assert [w for w in f.inner.writes if w[1].endswith("/merge")]


def reviewer(tmp_path, run):
    home = tmp_path / "profiles" / "coding"
    home.mkdir(parents=True, exist_ok=True)
    return desk.api_create_reviewer({}, source="coding", home=home, run=run, hermes="/bin/hermes")


def test_a_hanging_hermes_answers_with_the_command(tmp_path):
    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    status, body = reviewer(tmp_path, hang)
    assert status == 200 and body["ok"] is False and body["error"] == "create_failed"
    assert body["command"] == desk.reviewer_command("coding") and body["message"].endswith(".")


def test_a_file_error_while_creating_the_reviewer_answers_with_the_command(tmp_path):
    def cannot_start(cmd, **kw):
        raise OSError("exec format error")
    status, body = reviewer(tmp_path, cannot_start)
    assert status == 200 and body["error"] == "create_failed" and body["command"]

    def created_but_unwritable(cmd, **kw):
        target = tmp_path / "profiles" / desk.REVIEWER
        target.mkdir()
        (target / "SOUL.md").mkdir()  # a folder where the file goes: the write fails
        return subprocess.CompletedProcess(cmd, 0, "", "")
    status, body = reviewer(tmp_path, created_but_unwritable)
    assert status == 200 and body["ok"] is False and body["error"] == "create_failed" and body["command"]
