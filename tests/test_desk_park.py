"""The desk with parked rows (PLAN-v0.0.21-park.md PK2, PK3): sections, counts and the sidebar label, Back rows
first, the prune and when it must not run, the desk as text, and the park routes."""
import importlib.util
import json

import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, ROOT, FakeForgejo, bot_account, make_token, sample_data, sub
from fb_fakes_tools_read import kit, plugin

desk, parking, state_mod, rr = sub("desk"), sub("parking"), sub("flows.state"), sub("review_requests")
read_mod = sub("tools.read")
BOT = make_token("bot")
NOW = 2_000_000_000
OTHER_SHA = "c" * 40


@pytest.fixture(autouse=True)
def fresh_cache():
    desk.clear_cache()
    yield
    desk.clear_cache()


def forge(*extra):
    """#3 from the sample data plus open pull requests ``extra`` (numbers) that ask acme too."""
    f = FakeForgejo({BOT: bot_account()}, data=sample_data())
    d = f.data[DEMO]
    for n in extra:
        d["pulls"][n] = {**d["pulls"][3], "number": n, "title": f"PR {n}", "html_url": f"{BASE}/{DEMO}/pulls/{n}",
                         "updated_at": f"2026-11-08T11:{n:02d}:00Z"}
        d["issues"][n] = {**d["issues"][3], "number": n}
        d["comments"][n] = []
    return f


def build(data_dir, f, *, you="acme", now=NOW, secrets=True):
    values = {"url": BASE, "dry_run": False, "you": you}
    sec = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT} if secrets else {}
    desk.clear_cache()
    return desk.api_desk(lambda k, d=None: values.get(k, d), sec.get, data_dir=data_dir, transport=f,
                         now=lambda: now)[1]


def park(data_dir, n, *, sha=HEAD_SHA, at=NOW - 3600, **over):
    seen = {"head_sha": sha, "comment_at": "2026-11-08T12:00:00Z", "draft": False, **over.pop("seen", {})}
    return parking.park(data_dir, DEMO, n, seen=seen, now=lambda: at, **over)[0]


def by_id(body):
    return {r["number"]: r for r in body["rows"]}


def test_a_parked_row_leaves_the_count_and_the_label(tmp_path):
    f = forge(4)
    before = build(tmp_path, f)
    assert before["counts"] == {"waiting": 2, "parked": 0, "running": 0, "blocked": 0} and before["label"] == "Forgejo · 2"
    park(tmp_path, 3, note="after staging")
    body = build(tmp_path, f)
    rows = by_id(body)
    assert body["counts"] == {"waiting": 1, "parked": 1, "running": 0, "blocked": 0} and body["label"] == "Forgejo · 1"
    assert rows[3]["section"] == "parked" and rows[3]["back"] is None and rows[4]["section"] == "now"
    assert rows[3]["parked"] == {"note": "after staging", "parked_at": NOW - 3600, "wake_commits": True,
                                 "seen": {"head_sha": HEAD_SHA, "comment_at": "2026-11-08T12:00:00Z", "draft": False},
                                 "age_s": 3600}
    assert [r["number"] for r in body["rows"]] == [4, 3]  # now rows, then parked rows


def test_all_rows_parked_gives_zero_and_the_bare_label(tmp_path):
    park(tmp_path, 3)
    body = build(tmp_path, forge())
    assert body["counts"]["waiting"] == 0 and body["counts"]["parked"] == 1 and body["label"] == "Forgejo"


@pytest.mark.parametrize("reason,setup", [
    ("commit", lambda d, f: park(d, 3, sha=OTHER_SHA)),
    ("ready", lambda d, f: park(d, 3, seen={"draft": True})),
    ("comment", lambda d, f: (park(d, 3), f.data[DEMO]["comments"][3].append(
        {"id": 11, "user": {"login": "bob"}, "body": "Any news?", "created_at": "2026-11-08T13:00:00Z",
         "html_url": f"{BASE}/{DEMO}/pulls/3#issuecomment-11"}))),
])
def test_a_parked_row_comes_back_first_with_its_reason_and_his_note(tmp_path, reason, setup):
    f = forge(4)
    setup(tmp_path, f)
    body = build(tmp_path, f)
    row = by_id(body)[3]
    assert row["section"] == "now" and row["back"]["reason"] == reason and row["parked"]["note"] == ""
    assert body["rows"][0]["number"] == 3 and body["counts"]["waiting"] == 2 and body["counts"]["parked"] == 0
    url = {"commit": f"{BASE}/{DEMO}/compare/{OTHER_SHA}...{HEAD_SHA}", "ready": f"{BASE}/{DEMO}/pulls/3",
           "comment": f"{BASE}/{DEMO}/pulls/3#issuecomment-11"}[reason]
    assert row["back"]["url"] == url


def test_his_own_comment_does_not_bring_it_back(tmp_path):
    f = forge()
    park(tmp_path, 3)
    f.data[DEMO]["comments"][3].append({"id": 12, "user": {"login": "Acme"}, "body": "Note to self.",
                                        "created_at": "2026-11-08T13:00:00Z"})
    assert by_id(build(tmp_path, f))[3]["section"] == "parked"


def test_a_complete_scan_prunes_a_closed_pull_request_and_logs_it(tmp_path):
    park(tmp_path, 3)
    park(tmp_path, 99)  # not open any more
    body = build(tmp_path, forge())
    assert sorted(parking.load(tmp_path)[0]) == [f"{DEMO}#3"]
    assert body["counts"]["parked"] == 1
    lines = [json.loads(x) for x in (tmp_path / "desk-actions.jsonl").read_text().splitlines()]
    assert lines[-1] == {"at": NOW, "action": "prune", "ids": [f"{DEMO}#99"]}


def test_a_flow_row_is_never_pruned(tmp_path):
    store = state_mod.FlowStore(tmp_path)
    store.save_pr({"repo": DEMO, "pr": 42, "flow": "demo", "head_sha": HEAD_SHA, "title": "Flow PR", "url": "u",
                   "gates": [{"gate_key": "human", "type": "human", "state": "waiting", "detail": ""}]})
    park(tmp_path, 42)
    build(tmp_path, forge())
    assert f"{DEMO}#42" in parking.load(tmp_path)[0]


def test_a_park_newer_than_the_scan_survives_it(tmp_path):
    park(tmp_path, 99, at=NOW)
    build(tmp_path, forge())
    assert f"{DEMO}#99" in parking.load(tmp_path)[0]


def test_a_scan_with_a_note_never_prunes(tmp_path, monkeypatch):
    real = rr.open_pulls
    monkeypatch.setattr(rr, "open_pulls", lambda client: (real(client)[0], ["acme/other: could not be read"]))
    park(tmp_path, 99)
    build(tmp_path, forge())
    assert f"{DEMO}#99" in parking.load(tmp_path)[0]


def test_a_repo_with_more_than_one_page_of_open_pull_requests_is_a_note():
    class Page:
        def __init__(self, items, has_more):
            self.items, self.has_more = items, has_more

    class Client:
        def paginate(self, path, params, max_items):
            if path == "/repos/issues/search":
                return Page([{"repository": {"full_name": DEMO}}], False)
            return Page([{"number": n} for n in range(100)], True)

    pulls, notes = rr.open_pulls(Client())
    assert len(pulls[DEMO]) == 100 and notes == [f"{DEMO}: only its 100 most recent open pull requests were read."]


def test_a_failed_scan_never_prunes(tmp_path, monkeypatch):
    def down(*args):
        raise OSError("forge down")

    monkeypatch.setattr(rr, "reviews_reader", down)  # the scan ran, the review rows after it failed
    monkeypatch.setattr(rr, "asked", lambda pr, you: False)
    park(tmp_path, 99)
    build(tmp_path, forge())
    assert f"{DEMO}#99" in parking.load(tmp_path)[0]


@pytest.mark.parametrize("you,secrets", [("", True), ("acme", False)])
def test_no_scan_means_no_prune(tmp_path, you, secrets):
    park(tmp_path, 99)
    build(tmp_path, forge(), you=you, secrets=secrets)
    assert f"{DEMO}#99" in parking.load(tmp_path)[0]


def test_the_agent_s_desk_read_never_prunes_but_shows_the_sections(tmp_path):
    park(tmp_path, 3, note="later", at=1000)
    park(tmp_path, 99, at=1000)  # long before the read's real clock: only the missing prune flag keeps it
    forge_, gs, sec = kit(settings={"you": "acme"})
    out = json.loads(plugin.make_read_handler("forge_list", gs, sec, transport=forge_, data_dir=tmp_path)({"kind": "desk"}))
    assert f"{DEMO}#99" in parking.load(tmp_path)[0]
    assert out["counts"] == {"waiting": 0, "parked": 1, "running": 0, "blocked": 0}
    assert out["rows"][0]["section"] == "parked" and out["rows"][0]["parked_note"] == "later"


def test_an_unreadable_store_is_a_note_and_nothing_is_parked(tmp_path):
    (tmp_path / parking.FILE).write_text("{", encoding="utf-8")
    body = build(tmp_path, forge())
    assert "Parked rows could not be read." in body["notes"] and body["counts"]["parked"] == 0
    assert (tmp_path / parking.FILE).read_text() == "{"


def test_drafts_and_failed_checks_still_count(tmp_path):
    f = forge(4)
    f.data[DEMO]["pulls"][4]["draft"] = True  # #3 has a failed check in the sample data
    body = build(tmp_path, f)
    rows = by_id(body)
    assert rows[4]["draft"] is True and rows[3]["checks"]["state"] == "failure"
    assert body["counts"]["waiting"] == 2


def test_a_parked_row_outside_waiting_stays_in_its_tab(tmp_path):
    store = state_mod.FlowStore(tmp_path)
    store.save_pr({"repo": DEMO, "pr": 42, "flow": "demo", "head_sha": HEAD_SHA, "title": "Flow PR", "url": "u",
                   "gates": [{"gate_key": "review", "type": "review", "state": "running", "detail": ""}]})
    park(tmp_path, 42)
    row = by_id(build(tmp_path, forge()))[42]
    assert row["tab"] == "running" and row["section"] == "now" and row["parked"] is None


# The desk as text.

def text_desk(rows, counts):
    return read_mod.desk_as_text({"you": "acme", "counts": counts, "rows": rows})


def test_the_cli_line_names_the_parked_rows():
    out = {"you": "studioluftbruecke", "counts": {"waiting": 6, "parked": 1, "running": 0, "blocked": 2}, "rows": []}
    assert read_mod.format_desk(out) == "Forgejo desk for studioluftbruecke: 6 waiting (1 parked), 0 running, 2 blocked"
    out["counts"]["parked"] = 0
    assert read_mod.format_desk(out) == "Forgejo desk for studioluftbruecke: 6 waiting, 0 running, 2 blocked"


def test_a_parked_and_a_back_row_read_as_text():
    rows = [{"tab": "waiting", "section": "parked", "repo": DEMO, "number": 3, "title": "T", "url": "u",
             "parked": {"note": "after staging"}},
            {"tab": "waiting", "section": "now", "repo": DEMO, "number": 4, "title": "B", "url": "v",
             "parked": {"note": ""}, "back": {"text": "It is marked ready now."}}]
    out = text_desk(rows, {"waiting": 1, "parked": 1, "running": 0, "blocked": 0})
    assert out["rows"][0]["parked_note"] == "after staging" and out["rows"][1]["back"] == "It is marked ready now."
    lines = read_mod.format_desk(out).splitlines()
    assert lines[1].startswith("parked") and "(note: after staging)" in lines[1]
    assert lines[2].startswith("waiting") and "(It is marked ready now.)" in lines[2]


def test_the_repo_filter_recounts_by_tab_and_section():
    rows = [{"tab": "waiting", "section": "parked", "repo": DEMO, "number": 3},
            {"tab": "waiting", "section": "now", "repo": DEMO, "number": 4},
            {"tab": "blocked", "section": "now", "repo": DEMO, "number": 5},
            {"tab": "waiting", "section": "now", "repo": "acme/other", "number": 1}]
    out = read_mod.desk_as_text({"you": "acme", "counts": {"waiting": 2, "parked": 1, "running": 0, "blocked": 1},
                                 "rows": rows}, repo=DEMO.upper())
    assert out["counts"] == {"waiting": 1, "parked": 1, "running": 0, "blocked": 1} and out["total"] == 3


# The park routes (PK3).

SEEN = {"head_sha": HEAD_SHA, "comment_at": None, "draft": False}


@pytest.mark.parametrize("body,word", [
    ({"repo": "nope", "number": 3, "wake_commits": True, "seen": SEEN}, "repository name"),
    ({"repo": DEMO, "number": 0, "wake_commits": True, "seen": SEEN}, "number"),
    ({"repo": DEMO, "number": True, "wake_commits": True, "seen": SEEN}, "number"),
    ({"repo": DEMO, "number": "3", "wake_commits": True, "seen": SEEN}, "number"),
    ({"repo": DEMO, "number": 3, "seen": SEEN}, "wake_commits"),
    ({"repo": DEMO, "number": 3, "wake_commits": "yes", "seen": SEEN}, "wake_commits"),
    ({"repo": DEMO, "number": 3, "wake_commits": True}, "seen"),
    ({"repo": DEMO, "number": 3, "wake_commits": True, "seen": {"head_sha": "zz"}}, "sha"),
    ({"repo": DEMO, "number": 3, "wake_commits": True, "seen": SEEN, "note": 5}, "note"),
    ("not a dict", "repository name"),
])
def test_park_refuses_bad_input_with_a_sentence_and_writes_nothing(tmp_path, body, word):
    status, out = desk.api_park(body, data_dir=tmp_path)
    assert status == 400 and out["ok"] is False and out["error"] == "bad_request" and word in out["message"]
    assert not (tmp_path / parking.FILE).exists()


def test_park_and_unpark_answer_and_log(tmp_path):
    status, out = desk.api_park({"repo": DEMO, "number": 3, "note": " after\nstaging ", "wake_commits": False,
                                 "seen": SEEN}, data_dir=tmp_path, now=lambda: 50)
    assert status == 200 and out == {"ok": True, "parked": {"repo": DEMO, "number": 3, "note": "after staging",
                                                            "parked_at": 50, "wake_commits": False, "seen": SEEN}}
    assert "label" not in out  # B1: the next desk read sets the sidebar label
    assert desk.api_unpark({"repo": DEMO, "number": 3}, data_dir=tmp_path, now=lambda: 60) == (200, {"ok": True, "removed": True})
    assert desk.api_unpark({"repo": DEMO, "number": 3}, data_dir=tmp_path)[1]["removed"] is False
    assert desk.api_unpark({"repo": DEMO}, data_dir=tmp_path)[0] == 400
    actions = [json.loads(x)["action"] for x in (tmp_path / "desk-actions.jsonl").read_text().splitlines()]
    assert actions == ["park", "unpark", "unpark"]


def test_a_park_drops_the_cached_desk_but_keeps_the_write_token_facts(tmp_path):
    with desk._lock:
        desk._cache.update({("desk", str(tmp_path)): (0, {}), ("desk", "other"): (0, {}),
                            ("write_token", "abc"): (0, {"can_merge": True}), ("settings",): (0, {})})
    desk.api_park({"repo": DEMO, "number": 3, "wake_commits": True, "seen": SEEN}, data_dir=tmp_path)
    assert sorted(desk._cache) == [("settings",), ("write_token", "abc")]
    desk._cache[("desk", "x")] = (0, {})
    desk.api_unpark({"repo": DEMO, "number": 3}, data_dir=tmp_path)
    assert ("desk", "x") not in desk._cache and ("write_token", "abc") in desk._cache


def test_a_park_shows_on_the_next_desk_read_at_once(tmp_path):
    f = forge(4)
    assert build(tmp_path, f)["counts"]["waiting"] == 2
    values = {"url": BASE, "dry_run": False, "you": "acme"}
    sec = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}
    desk.api_desk(lambda k, d=None: values.get(k, d), sec.get, data_dir=tmp_path, transport=f, now=lambda: NOW)
    desk.api_park({"repo": DEMO, "number": 3, "wake_commits": True, "seen": SEEN}, data_dir=tmp_path, now=lambda: 1000)
    body = desk.api_desk(lambda k, d=None: values.get(k, d), sec.get, data_dir=tmp_path, transport=f, now=lambda: NOW)[1]
    assert body["counts"]["waiting"] == 1 and body["counts"]["parked"] == 1  # not the 15 s cached answer


def test_the_park_routes_create_the_data_folder(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("fb_plugin_api_park_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(api, "_home", lambda: tmp_path)
    assert api._data_dir() is None
    made = api._data_dir(create=True)
    assert made.is_dir() and made.parent == tmp_path / "plugin-data" and made.name.startswith("agent-plugin-forgebruecke-")
    routes = {(r.path, tuple(sorted(r.methods))) for r in api.router.routes}
    assert ("/park", ("POST",)) in routes and ("/unpark", ("POST",)) in routes
    source = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")
    assert "api_park(body, data_dir=_data_dir(create=True))" in source
    assert "api_unpark(body, data_dir=_data_dir(create=True))" in source
