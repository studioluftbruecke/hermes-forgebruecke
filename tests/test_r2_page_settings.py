"""The settings page's one verdict: the badge, the next step and each setup row come from the same facts,
so `ready` means nothing is left to do and a write can go out."""
import json
import re

import pytest

from fb_fakes import BASE, DEMO, ROOT, FakeForgejo, bot_account, make_token, owner_account, repo, sample_data, sub
from fb_fakes_desktop import OneRouteFails, UsersForge

desk = sub("desk")
caps = sub("capabilities")
access_mod = sub("access")
client_mod = sub("client")
BOT, OWNER = make_token("bot"), make_token("owner")
BOT_ENV, OWNER_ENV = "FORGEBRUECKE_BOT_ACCOUNT_TOKEN", "FORGEBRUECKE_PERSONAL_WRITE_TOKEN"
READ_ONLY = {"read:repository", "read:issue", "read:user"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(client_mod, "BACKOFF", (0.0, 0.0, 0.0))
    desk.clear_cache()
    access_mod.clear_rights()
    yield
    desk.clear_cache()
    access_mod.clear_rights()


def forge(bot=None, data=None):
    owner = {**owner_account(), "scopes": {"write:repository", "write:issue", "read:organization", "read:user"}}
    inner = FakeForgejo({BOT: bot or bot_account(), OWNER: owner}, data=sample_data() if data is None else data)
    return UsersForge(inner, users={"acme", "kay"}, orgs={"studio"})


def unreachable(method, url, headers, body, timeout):
    raise client_mod.ForgejoError(None, "unreachable", "No answer.")


def page(tmp_path, transport=None, secrets=None, **values):
    secrets = {BOT_ENV: BOT} if secrets is None else secrets
    values = {"url": BASE, "you": "acme", "dry_run": False, **values}
    return desk.api_settings(lambda k, d=None: values.get(k, d), secrets.get, transport=transport or forge(),
                             home=tmp_path, now=lambda: 1000, refresh=True)[1]


def row(body, row_id):
    return next(r for r in body["setup"] if r["id"] == row_id)


STATES = {
    "ready": lambda: {},
    "preview": lambda: {"dry_run": True},
    "you_unset": lambda: {"you": ""},
    "no_token": lambda: {"secrets": {}},
    "token_refused": lambda: {"secrets": {BOT_ENV: "not-a-token"}},
    "token_check_failed": lambda: {"transport": OneRouteFails(forge(), "/api/v1/user", BOT)},
    "token_without_write_scopes": lambda: {"transport": forge({**bot_account(), "scopes": READ_ONLY})},
    "token_without_write_scopes_preview": lambda: {"transport": forge({**bot_account(), "scopes": READ_ONLY}),
                                                   "dry_run": True},
    "no_repo_to_write_to": lambda: {"transport": forge({**bot_account(), "repos": [repo(DEMO, "read")]})},
    "write_token_refused": lambda: {"secrets": {BOT_ENV: BOT, OWNER_ENV: "not-a-token"}},
    "write_token_refused_you_unset": lambda: {"secrets": {BOT_ENV: BOT, OWNER_ENV: "not-a-token"}, "you": ""},
    "address_unreachable": lambda: {"transport": unreachable},
    "forgejo_failing": lambda: {"transport": OneRouteFails(forge(), "/api/v1/version")},
}


@pytest.mark.parametrize("name", sorted(STATES))
def test_the_badge_follows_the_next_step_and_ready_means_a_write_can_go_out(tmp_path, name):
    body = page(tmp_path, **STATES[name]())
    action = body["next_action"]
    assert body["state"] == desk.ACTIONS[action["id"]][0]
    assert (body["state"] == "ready") == (action["id"] == "none") == (name == "ready")
    assert (action["text"] == "") == (action["id"] == "none") and len(action["text"]) <= 140
    writes = next(f for f in body["features"] if f["id"] == "writes") if body["features"] else {"status": "dark"}
    if body["state"] == "ready":
        assert writes["status"] == "available" and all(r["state"] in ("done", "optional") for r in body["setup"])
    assert (body["state"] == "unchecked") == any(r["state"] == "unchecked" for r in body["setup"])
    if body["state"] == "preview":
        assert writes["status"] == "off" and action["id"] == "dry_run"


def test_a_token_without_the_write_scopes_is_setup_work_with_its_scopes_named(tmp_path):
    for dry_run in (False, True):
        body = page(tmp_path, forge({**bot_account(), "scopes": READ_ONLY}), dry_run=dry_run)
        assert body["state"] == "setup"
        assert body["next_action"] == {
            "id": "fix_scopes", "button": "settings", "row": "bot",
            "text": "Hermes' token lacks read:notification, write:issue and write:repository. Replace it with one "
                    "that has them."}
        assert row(body, "bot") == {"id": "bot", "state": "problem", "value": "scopes missing", "problem": None,
                                    "button": None}


def test_an_account_that_may_write_nowhere_is_setup_work_that_forgejo_fixes(tmp_path):
    body = page(tmp_path, forge({**bot_account(), "repos": [repo(DEMO, "read")]}))
    assert body["state"] == "setup" and body["next_action"]["id"] == "add_repo"
    assert body["next_action"]["button"] == "forgejo"
    assert body["next_action"]["text"] == ("Hermes' account cannot write to any repo yet. Give it Write on a repo in "
                                           "Forgejo.")


def test_you_is_asked_before_the_optional_token_and_the_badge_stays_setup(tmp_path):
    refused = {BOT_ENV: BOT, OWNER_ENV: "not-a-token"}
    body = page(tmp_path, secrets=refused, you="")
    assert body["state"] == "setup"
    assert body["next_action"] == {"id": "set_you", "text": "Which Forgejo user are you?", "button": "who",
                                   "row": "you", "login": "acme"}
    assert row(body, "you")["state"] == "todo"
    assert row(body, "owner") == {"id": "owner", "state": "problem", "value": "token refused", "button": "settings",
                                  "problem": "Forgejo refused your write token (401). Replace it."}
    body = page(tmp_path, secrets=refused)
    assert body["state"] == "setup" and body["next_action"]["id"] == "fix_owner_token"
    assert body["next_action"]["text"] == "Forgejo refused your write token (401). Replace it."
    assert row(body, "owner")["problem"] is None  # the next step already says it


def test_a_token_forgejo_could_not_check_is_not_called_refused_anywhere(tmp_path):
    body = page(tmp_path, OneRouteFails(forge(), "/api/v1/user", BOT))
    assert body["state"] == "unchecked"
    assert body["next_action"] == {"id": "retry", "button": "retry", "row": "bot",
                                   "text": "Forgejo failed (500) while checking Hermes' token. Try again."}
    assert row(body, "bot") == {"id": "bot", "state": "unchecked", "value": "not checked", "problem": None,
                                "button": None}
    assert "refused" not in json.dumps({k: body[k] for k in ("next_action", "setup", "token_warnings")})


def test_a_refused_token_says_so_once_with_the_status(tmp_path):
    body = page(tmp_path, secrets={BOT_ENV: "not-a-token"})
    assert body["state"] == "setup"
    assert body["next_action"] == {"id": "fix_token", "button": "settings", "row": "bot",
                                   "text": "Forgejo refused Hermes' token (401). Replace it."}
    assert row(body, "bot")["value"] == "token refused" and row(body, "bot")["problem"] is None


def test_an_address_nothing_answers_at_and_a_failing_forgejo_are_told_apart(tmp_path):
    body = page(tmp_path, unreachable)
    assert body["state"] == "setup" and body["next_action"]["id"] == "set_url"
    assert body["next_action"]["text"] == f"Hermes cannot reach {BASE}. Check your Forgejo address."
    assert row(body, "url") == {"id": "url", "state": "problem", "value": BASE.split("//")[1], "problem": None,
                                "button": None}
    body = page(tmp_path, OneRouteFails(forge(), "/api/v1/version"))
    assert body["state"] == "unchecked"
    assert body["next_action"] == {"id": "retry", "button": "retry", "row": "url",
                                   "text": "Forgejo failed (500). Try again."}
    assert row(body, "url")["state"] == "unchecked"


def test_a_finished_setup_has_four_quiet_rows(tmp_path):
    body = page(tmp_path, secrets={BOT_ENV: BOT, OWNER_ENV: OWNER})
    assert body["state"] == "ready" and body["next_action"] == {"id": "none", "text": "", "button": None, "row": None}
    assert body["setup"] == [
        {"id": "url", "state": "done", "value": BASE.split("//")[1], "problem": None, "button": "settings"},
        {"id": "bot", "state": "done", "value": "@forgebruecke-bot", "problem": None, "button": "settings"},
        {"id": "you", "state": "done", "value": "@acme", "problem": None, "button": "settings"},
        {"id": "owner", "state": "done", "value": "@acme", "problem": None, "button": "settings"}]
    assert row(page(tmp_path), "owner") == {"id": "owner", "state": "optional", "value": "off", "problem": None,
                                            "button": "settings"}


@pytest.mark.parametrize("token,state,value", [
    ({"role": "bot", "set": False}, "unset", ""),
    ({"role": "bot", "set": True, "identity": "hermes", "scopes": {}}, "ok", "@hermes"),
    ({"role": "bot", "set": True, "error": {"status": 403, "kind": "missing_scope", "message": "x",
                                             "missing_scopes": ["read:user"]}}, "lacks_scopes", "scopes missing"),
    ({"role": "bot", "set": True, "error": {"status": 403, "kind": "forbidden", "message": "x"}}, "refused",
     "token refused"),
    ({"role": "bot", "set": True, "error": {"status": None, "kind": "unreachable", "message": "x"}}, "unchecked",
     "not checked"),
    ({"role": "owner", "set": True, "identity": "acme", "scopes": {"write:repository": "no", "read:user": "yes"}},
     "lacks_scopes", "scopes missing"),
    ({"role": "owner", "set": True, "identity": "acme", "scopes": {"write:repository": "unknown"}}, "ok", "@acme"),
    ({"role": "bot", "set": True, "identity": "hermes", "repos": {"write": 1},
      "scopes": {"write:issue": "unknown", "write:repository": "yes"}}, "unchecked", "not checked"),
])
def test_one_token_has_one_verdict(token, state, value):
    verdict = caps.token_verdict(token)
    assert (verdict["state"], verdict["value"]) == (state, value)
    assert (verdict["text"] is None) == (state in ("unset", "ok"))
    assert verdict["text"] is None or (verdict["text"].endswith(".") and len(verdict["text"]) <= 140)


WRONG_ACCOUNT = ("Your write token belongs to forgebruecke-bot, Hermes' own account. Replace it with a token of your "
                 "own account.")


def test_a_write_token_of_hermes_own_account_is_a_problem_said_once(tmp_path):
    body = page(tmp_path, secrets={BOT_ENV: BOT, OWNER_ENV: BOT})
    assert body["state"] == "setup"
    assert body["next_action"] == {"id": "fix_owner_token", "button": "settings", "row": "owner", "text": WRONG_ACCOUNT}
    assert row(body, "owner") == {"id": "owner", "state": "problem", "value": "wrong account", "problem": None,
                                  "button": None}
    assert body["token_warnings"] == [] and body["tokens"][1]["error"]["kind"] == "wrong_account"
    assert next(f for f in body["features"] if f["id"] == "owner_merge")["status"] == "dark"
    body = page(tmp_path, secrets={BOT_ENV: BOT, OWNER_ENV: BOT}, you="")
    assert body["next_action"]["id"] == "set_you" and "login" in body["next_action"]
    assert row(body, "owner")["problem"] == WRONG_ACCOUNT and body["token_warnings"] == []


def test_the_registry_marks_the_token_the_report_carries():
    bot = {"role": "bot", "set": True, "identity": "hermes", "scopes": {}}
    owner = {"role": "owner", "set": True, "identity": "hermes", "scopes": {"write:repository": "yes"}}
    found, warnings = caps.probe_caps({"bot": bot, "owner": owner})
    assert owner["error"]["kind"] == "wrong_account" and "owner:merge" not in found
    assert warnings == [owner["error"]["message"]] and len(warnings[0]) <= 140
    assert caps.token_verdict(owner) == {"state": "wrong_account", "value": "wrong account", "text": warnings[0]}


# --- one word per thing: writes held back are "preview", the form is "Hermes' settings" ------------------

def test_no_sentence_a_person_reads_says_dry_run_or_names_a_button_the_desk_lacks():
    for path in ("desk.py", "capabilities.py", "dashboard/plugin_api.py", "desktop/plugin.js"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert not re.search(r"[\"'`][^\"'`\n]*\b[Dd]ry run\b", text), path
    route = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    assert "Use All settings" not in route and "Set it in Hermes' settings." in route
