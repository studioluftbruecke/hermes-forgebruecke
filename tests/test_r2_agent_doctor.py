"""The doctor as the agent and the terminal read it: who it suggests, why a feature is not available, its words."""
import json

from fb_fakes import BASE, FakeForgejo, bot_account, load_plugin, make_token, repo, sample_data, sub
from fb_fakes_desktop import UsersForge

plugin = load_plugin()
doctor, desk, access_mod = sub("tools.doctor"), sub("desk"), sub("access")
BOT = make_token("bot")
ENV = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}


def getter(**values):
    values = {"url": BASE, "dry_run": False, **values}
    return lambda key, default=None: values.get(key, default)


def studio_forge():
    """Hermes' account sees two repos of the organisation studio and one of the person kay."""
    account = {**bot_account(), "repos": [repo("studio/a", "write"), repo("studio/b", "write"), repo("kay/notes", "write")]}
    return UsersForge(FakeForgejo({BOT: account}, data={}), users={"kay"}, orgs={"studio"})


def test_the_doctor_suggests_the_person_the_page_suggests(tmp_path):
    desk.clear_cache()
    access_mod.clear_rights()
    report = plugin.doctor_report(getter(), ENV.get, transport=studio_forge())
    page = desk.api_settings(getter(), ENV.get, transport=studio_forge(), home=tmp_path, now=lambda: 1000)[1]
    assert report["you"]["suggestion"] == page["you_suggestion"] == "kay"
    assert report["next_action"] == {"id": "set_you", "text": "Which Forgejo user are you?"}
    assert report["next_step"] == 'Which Forgejo user are you? Click "Use @kay" on the Forgebrücke page.'


def test_an_organisation_is_never_the_suggestion():
    account = {**bot_account(), "repos": [repo("studio/a", "write"), repo("studio/b", "write")]}
    forge = UsersForge(FakeForgejo({BOT: account}, data={}), orgs={"studio"})
    report = plugin.doctor_report(getter(), ENV.get, transport=forge)
    assert report["you"]["suggestion"] == ""
    assert report["next_step"] == "Which Forgejo user are you? Enter your Forgejo user name in Hermes' settings."
    assert "you" not in doctor.compact(report)


def test_the_forge_is_not_asked_who_you_are_once_you_is_set():
    forge = studio_forge()
    report = plugin.doctor_report(getter(you="kay"), ENV.get, transport=forge)
    assert report["you"] == {"set": "kay", "suggestion": ""} and forge.asked == []
    assert report["next_step"] == "Ready."


def run_direct(forge, **values):
    """The doctor on a client that never sleeps, so a failing route costs no time."""
    settings_mod, client_mod = sub("settings"), sub("client")
    return doctor.run_doctor(settings_mod.load_settings(getter(**values)), settings_mod.load_credentials(ENV.get),
                             client_factory=lambda url, token: client_mod.ForgejoClient(
                                 url, token, transport=forge, sleep=lambda s: None, clock=lambda: 0.0))


def read_only_forge():
    """Hermes' token reads and lacks both write scopes."""
    account = {**bot_account(), "scopes": {"read:repository", "read:issue", "read:user"}}
    return FakeForgejo({BOT: account}, data=sample_data())


def test_the_short_answer_says_why_a_feature_is_not_available():
    full = run_direct(read_only_forge(), you="acme")
    out = doctor.compact(full)
    label = {f["id"]: f["label"] for f in full["features"]}
    reason = {f["id"]: f["reason"] for f in full["features"]}
    assert out["ok"] is False and out["works"] == [label["read"]]
    assert "write:issue" in reason["writes"] and label["writes"] in out["not_available"][reason["writes"]]
    listed = [name for names in out["not_available"].values() for name in names]
    assert sorted(listed) == sorted(f["label"] for f in full["features"] if f["status"] == "dark")
    assert all(names for names in out["not_available"].values())
    assert len(json.dumps(out, ensure_ascii=False)) < 1500


def test_the_short_answer_has_no_internal_words():
    out = doctor.compact(run_direct(read_only_forge(), you="acme"))
    text = json.dumps(out, ensure_ascii=False)
    assert [t["role"] for t in out["tokens"]] == ["hermes", "user"]
    assert "features" not in out and "dark" not in text and '"bot"' not in text and '"owner"' not in text
    assert "detail=true" in out["more"]


def test_features_that_share_a_reason_are_listed_under_it_once():
    full = run_direct(read_only_forge(), you="acme")
    admin = [f for f in full["features"] if f["id"] in ("protection_view", "audit", "presets")]
    assert len({f["reason"] for f in admin}) == 1
    assert doctor.compact(full)["not_available"][admin[0]["reason"]] == [f["label"] for f in admin]


def test_a_setup_where_everything_works_has_no_such_list():
    out = doctor.compact({"ok": True, "features": [{"label": "Reading", "status": "available", "reason": "r"}]})
    assert out == {"ok": True, "works": ["Reading"], "more": out["more"]}


class UserRoute:
    """Answers ``GET /user`` with one status and passes every other request on."""

    def __init__(self, inner, status):
        self.inner, self.status = inner, status

    def __call__(self, method, url, headers, body, timeout):
        if url.split("?")[0].endswith("/api/v1/user"):
            return self.status, {"content-type": "application/json"}, json.dumps({"message": "no"}).encode()
        return self.inner(method, url, headers, body, timeout)


def test_a_token_forgejo_could_not_check_is_not_called_refused():
    report = run_direct(UserRoute(read_only_forge(), 500), you="acme")
    assert report["next_action"] == {"id": "retry",
                                     "text": "Forgejo failed (500) while checking Hermes' token. Try again."}


def test_a_refused_token_says_so_with_the_status_and_the_fix():
    report = run_direct(UserRoute(read_only_forge(), 401), you="acme")
    assert report["next_action"] == {"id": "fix_token", "text": "Forgejo refused Hermes' token (401). Replace it."}


def test_a_token_refused_for_a_missing_scope_names_the_scope():
    account = {**bot_account(), "scopes": {"read:repository", "read:issue"}}
    report = run_direct(FakeForgejo({BOT: account}, data=sample_data()), you="acme")
    assert report["next_action"] == {"id": "fix_scopes", "text": "Hermes' token lacks read:user. Replace it with one that has it."}


def test_the_terminal_report_says_not_available_and_names_no_internal_word():
    text = doctor.format_text(run_direct(read_only_forge(), you="acme"))
    lines = text.splitlines()
    assert "[not available]" in text and "dark" not in text and "light" not in text and "swagger" not in text
    assert any(line.startswith("  Hermes' token: forgebruecke-bot, 1 repo") for line in lines)
    assert "  Your write token: not set. Optional. Only for the desk's Merge button." in lines
    assert lines[-1] == ("  next: Hermes' token lacks read:notification, write:issue and write:repository. "
                         "Replace it with one that has them.")
