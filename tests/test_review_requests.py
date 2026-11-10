"""D33: every open pull request asks you to review it. The tick asks where a pull request does not (whatever
opened it), exactly once, and never where you opened it, already reviewed it, or Hermes may only read."""
import pytest

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, repo, sample_data, sub

state_mod = sub("flows.state")
engine_mod = sub("flows.engine")
loop = sub("flows.loop")
access_mod = sub("access")
settings_mod = sub("settings")
BOT = make_token("bot")
NOTES = "acme/notes"


@pytest.fixture(autouse=True)
def fresh_rights():
    access_mod.clear_rights()
    yield
    access_mod.clear_rights()


def pr(data, full, n, *, author="hermes", asked=()):
    base = sample_data()[DEMO]
    item = {**base["pulls"][3], "number": n, "state": "open", "title": f"PR {n}", "user": {"login": author},
            "requested_reviewers": [{"login": a} for a in asked], "html_url": f"{BASE}/{full}/pulls/{n}",
            "repository": {"full_name": full}}
    data.setdefault("pulls", {})[n] = item
    data.setdefault("issues", {})[n] = {**base["issues"][3], "number": n, "repository": {"full_name": full}}
    return item


def kit(**values):
    data = {DEMO: {"info": {"default_branch": "main"}, "reviews": {}}, NOTES: {"info": {"default_branch": "main"}}}
    pr(data[DEMO], DEMO, 11)                          # opened by a raw API call: asks nobody
    pr(data[DEMO], DEMO, 12, asked=("acme",))         # asks you already
    pr(data[DEMO], DEMO, 13, author="acme")           # yours: Forgejo refuses asking the author (422)
    pr(data[DEMO], DEMO, 14, asked=("bob",))          # asks someone else only
    pr(data[DEMO], DEMO, 15)                          # you already reviewed it
    data[DEMO]["reviews"][15] = [{"id": 1, "user": {"login": "acme"}, "state": "REQUEST_CHANGES", "dismissed": False}]
    pr(data[NOTES], NOTES, 2)                         # a repo where Hermes may only read
    account = bot_account()
    account["repos"].append(repo(NOTES, "read"))
    forge = FakeForgejo({BOT: account}, data=data)
    settings = {"url": BASE, "dry_run": False, "you": "acme", "writes": [], **values}
    access = access_mod.load_access(lambda k, d=None: settings.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                                    transport=forge)
    return forge, access


def asks(forge):
    return [(p, w["reviewers"]) for m, p, w in forge.writes if p.endswith("/requested_reviewers")]


def tick(access, tmp_path):
    return engine_mod.Engine(access, state_mod.FlowStore(tmp_path)).tick()


def test_a_tick_asks_you_on_every_open_pr_that_does_not_ask_yet_and_only_there(tmp_path):
    forge, access = kit()
    report = tick(access, tmp_path)
    assert asks(forge) == [(f"/repos/{DEMO}/pulls/11/requested_reviewers", ["acme"]),
                           (f"/repos/{DEMO}/pulls/14/requested_reviewers", ["acme"])]
    assert report["review_requests"] == [f"{DEMO}#11", f"{DEMO}#14"] and report["ok"] and report["writes"] == 2
    assert any(NOTES in n and "read access" in n and f"{BASE}/{NOTES}/settings/collaboration" in n
               for n in report["notes"])
    assert {u["login"] for u in forge.data[DEMO]["pulls"][14]["requested_reviewers"]} == {"bob", "acme"}


def test_a_second_tick_asks_nobody_again(tmp_path):
    forge, access = kit()
    tick(access, tmp_path)
    before = len(forge.writes)
    report = tick(access, tmp_path)
    assert len(forge.writes) == before and report["review_requests"] == [] and report["writes"] == 0


def test_dry_run_records_the_requests_and_sends_none(tmp_path):
    forge, access = kit(dry_run=True)
    report = tick(access, tmp_path)
    assert asks(forge) == [] and report["writes"] == 2 and report["dry_run"] is True


@pytest.mark.parametrize("values", [{"review_every_pr": False}, {"you": ""}])
def test_switched_off_or_without_you_the_tick_asks_nobody(tmp_path, values):
    forge, access = kit(**values)
    assert tick(access, tmp_path).get("review_requests") == [] and asks(forge) == []


def test_a_failed_request_is_an_error_and_the_rest_still_go_out(tmp_path):
    forge, access = kit()
    orig = forge.__call__

    def flaky(method, url, headers, body, timeout):
        if method == "POST" and url.endswith("/pulls/11/requested_reviewers"):
            return 500, {"content-type": "application/json"}, b'{"message": "boom"}'
        return orig(method, url, headers, body, timeout)
    access = access_mod.Access(access.settings, access.creds, flaky)
    report = tick(access, tmp_path)
    assert report["review_requests"] == [f"{DEMO}#14"]
    assert any(e.startswith(f"{DEMO}#11: asking acme to review failed") for e in report["errors"])


def test_the_loops_tick_without_any_flow_when_you_is_set():
    def s(**v):
        return settings_mod.load_settings(lambda k, d=None: {"url": BASE, **v}.get(k, d), lambda: "coding")
    assert loop.has_work(s(you="acme")) is True
    assert loop.has_work(s(you="acme", review_every_pr=False)) is False
    assert loop.has_work(s()) is False
    assert loop.has_work(s(writes=[{"repo_glob": DEMO, "flows": ["demo"]}])) is True
    assert s().review_every_pr is True  # on by default
