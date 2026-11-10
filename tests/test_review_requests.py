"""D33: every open pull request asks you to review it. The tick asks where a pull request does not (whatever
opened it), exactly once, and never where you opened it, already reviewed it, or Hermes may only read."""
import json

import pytest

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, repo, sample_data, sub

review_requests = sub("review_requests")
state_mod = sub("flows.state")
engine_mod = sub("flows.engine")
loop = sub("flows.loop")
access_mod = sub("access")
settings_mod = sub("settings")
client_mod = sub("client")
BOT = make_token("bot")
NOTES = "acme/notes"


@pytest.fixture(autouse=True)
def fresh_rights(monkeypatch):
    access_mod.clear_rights()
    monkeypatch.setattr(client_mod, "BACKOFF", (0.0, 0.0, 0.0))  # a 500 is still retried, without the waiting
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


def test_one_failing_reviews_read_skips_that_pull_request_only(tmp_path):
    forge, access = kit()
    orig = forge.__call__

    def flaky(method, url, headers, body, timeout):
        if method == "GET" and "/pulls/11/reviews" in url:
            return 500, {"content-type": "application/json"}, b'{"message": "boom"}'
        return orig(method, url, headers, body, timeout)
    report = tick(access_mod.Access(access.settings, access.creds, flaky), tmp_path)
    assert report["review_requests"] == [f"{DEMO}#14"]
    assert [e for e in report["errors"] if e.startswith(f"{DEMO}#11: ")] and len(report["errors"]) == 1


def test_the_loops_tick_without_any_flow_when_you_is_set():
    def s(**v):
        return settings_mod.load_settings(lambda k, d=None: {"url": BASE, **v}.get(k, d), lambda: "coding")
    assert loop.has_work(s(you="acme")) is True
    assert loop.has_work(s(you="acme", review_every_pr=False)) is False
    assert loop.has_work(s()) is False
    assert loop.has_work(s(writes=[{"repo_glob": DEMO, "flows": ["demo"]}])) is True
    assert s().review_every_pr is True  # on by default


def test_a_tick_names_each_write_so_the_channel_log_says_what_it_did(tmp_path):
    # The channel logged only "writes: 1"; finding the pull request took a timeline search on the forge.
    forge, access = kit()
    assert tick(access, tmp_path)["done"] == [f"review_request {DEMO}#11", f"review_request {DEMO}#14"]
    forge, access = kit(dry_run=True)
    assert tick(access, tmp_path / "dry")["done"][0] == f"review_request {DEMO}#11 (dry run)"


# --- the sweep remembers what it asked ----------------------------------------------------------------

def sweep(access, data_dir, at=1_800_000_000.0):
    """The sweep as the engine runs it: the request is one POST with the agent token, skipped in dry run."""
    def request(repo_name, number, you):
        if not access.settings.dry_run:
            access.writer().post(f"/repos/{repo_name}/pulls/{number}/requested_reviewers", {"reviewers": [you]})
    return review_requests.sweep(access, request, data_dir=data_dir, now=lambda: at)


def requests_for(forge, number):
    return sum(1 for path, _ in asks(forge) if f"/pulls/{number}/" in path)


def test_a_pull_request_is_asked_once_and_removing_the_request_is_respected(tmp_path):
    forge, access = kit()
    assert sweep(access, tmp_path)["requested"] == [f"{DEMO}#11", f"{DEMO}#14"]
    for _ in range(3):
        forge.data[DEMO]["pulls"][11]["requested_reviewers"] = []  # you removed the request without reviewing
        assert sweep(access, tmp_path)["requested"] == []
    assert requests_for(forge, 11) == 1
    assert json.loads((tmp_path / "asked.json").read_text())[f"{DEMO}#11@acme"] == 1_800_000_000.0


def access_as(forge, you):
    settings = {"url": BASE, "dry_run": False, "you": you, "writes": []}
    return access_mod.load_access(lambda k, d=None: settings.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                                  transport=forge)


def test_after_you_changed_the_new_you_is_asked_where_the_old_you_was(tmp_path):
    forge, acme = kit()
    forge.data[DEMO]["perms"] = {"Carol": "write"}
    sweep(acme, tmp_path)  # acme is asked on #11 and #14, and #12 asked acme before
    carol = access_as(forge, "Carol")
    every_open_pr = [f"{DEMO}#{n}" for n in (11, 12, 13, 14, 15)]
    assert sweep(carol, tmp_path)["requested"] == every_open_pr
    for n in (11, 12):
        forge.data[DEMO]["pulls"][n]["requested_reviewers"] = []  # both removed their requests without reviewing
    assert sweep(access_as(forge, "carol"), tmp_path)["requested"] == []  # a user name is one user in any case
    assert sweep(acme, tmp_path)["requested"] == []  # what acme was asked is still remembered


def test_a_request_that_was_there_before_the_sweep_is_respected_when_removed(tmp_path):
    forge, access = kit()
    sweep(access, tmp_path)
    forge.data[DEMO]["pulls"][12]["requested_reviewers"] = []  # #12 asked you before any sweep ran
    assert sweep(access, tmp_path)["requested"] == [] and requests_for(forge, 12) == 0


def test_without_a_folder_the_sweep_has_no_memory(tmp_path):
    forge, access = kit()
    sweep(access, None)
    forge.data[DEMO]["pulls"][11]["requested_reviewers"] = []
    assert sweep(access, None)["requested"] == [f"{DEMO}#11"] and requests_for(forge, 11) == 2


def test_a_dry_run_or_failed_request_is_not_remembered(tmp_path):
    forge, access = kit(dry_run=True)
    assert sweep(access, tmp_path)["requested"] == [f"{DEMO}#11", f"{DEMO}#14"] and asks(forge) == []
    forge, access = kit()
    orig = forge.__call__

    def flaky(method, url, headers, body, timeout):
        if method == "POST" and url.endswith("/pulls/11/requested_reviewers"):
            return 403, {"content-type": "application/json"}, b'{"message": "no"}'
        return orig(method, url, headers, body, timeout)
    report = sweep(access_mod.Access(access.settings, access.creds, flaky), tmp_path)
    assert report["requested"] == [f"{DEMO}#14"] and len(report["errors"]) == 1
    assert sweep(access, tmp_path)["requested"] == [f"{DEMO}#11"]


def test_the_memory_keeps_the_newest_entries_only(tmp_path, monkeypatch):
    monkeypatch.setattr(review_requests, "KEEP_ASKED", 3)
    (tmp_path / "asked.json").write_text(json.dumps({f"old/repo#{n}": float(n) for n in range(1, 4)}))
    forge, access = kit()
    sweep(access, tmp_path, at=100.0)
    kept = json.loads((tmp_path / "asked.json").read_text())
    assert sorted(kept) == [f"{DEMO}#11@acme", f"{DEMO}#12@acme", f"{DEMO}#14@acme"]
