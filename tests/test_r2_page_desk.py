"""The desk says only what is true for the row and the read: whose move a row is, which button is that
move, how old the read is, and what could not be read."""
import json
import re
import shutil
import subprocess

import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, ROOT, FakeForgejo, bot_account, make_token, owner_account, sample_data, sub

desk = sub("desk")
state_mod = sub("flows.state")
access_mod = sub("access")
client_mod = sub("client")
BOT, OWNER = make_token("bot"), make_token("owner")
SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(client_mod, "BACKOFF", (0.0, 0.0, 0.0))
    desk.clear_cache()
    access_mod.clear_rights()
    yield
    desk.clear_cache()
    access_mod.clear_rights()


class Clock:
    def __init__(self, t=1000):
        self.t = t

    def __call__(self):
        return self.t


def forge():
    owner = {**owner_account(), "scopes": {"write:repository", "write:issue", "read:organization", "read:user"}}
    f = FakeForgejo({BOT: bot_account(), OWNER: owner}, data=sample_data())
    f.data[DEMO]["status"] = {}
    return f


def pr_state(n=3, states=("passed", "passed", "waiting"), human="waiting for acme to merge in Forgejo", repo=DEMO,
             keys=("review", "check", "human"), review="review by coding: Looks right. Ship it."):
    details = {"review": review, "human": human}
    return {"repo": repo, "pr": n, "flow": "demo", "head_sha": HEAD_SHA, "title": f"PR {n}", "base": "main",
            "url": f"{BASE}/{repo}/pulls/{n}", "updated_at": 400,
            "gates": [{"gate_key": k, "type": k, "state": s, "detail": details.get(k, "")} for k, s in zip(keys, states)]}


def read(tmp_path, f, *states, tokens=("bot",), now=None, engine_age=None, refresh=False, **values):
    store = state_mod.FlowStore(tmp_path)
    for st in states:
        store.save_pr(st)
    clock = now or Clock()
    if engine_age is not None:
        store.write_engine({"at": clock() - engine_age, "source": "gateway", "errors": []})
    env = {"bot": ("FORGEBRUECKE_BOT_ACCOUNT_TOKEN", BOT), "owner": ("FORGEBRUECKE_PERSONAL_WRITE_TOKEN", OWNER)}
    secrets = dict(env[t] for t in tokens)
    values = {"url": BASE, "dry_run": False, "you": "acme", "writes": [{"repo_glob": DEMO, "flows": ["demo"]}], **values}
    return desk.api_desk(lambda k, d=None: values.get(k, d), secrets.get, data_dir=tmp_path, transport=f, now=clock,
                         refresh=refresh)[1]


def js_function(name):
    return re.search(r"export function " + name + r"\(.*?\n\}", SOURCE, re.S).group(0).replace("export ", "")


def run_js(script):
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --- how old the read is ----------------------------------------------------------------------------

def test_the_desk_answers_the_age_of_its_own_read_whatever_the_engine_did(tmp_path):
    clock = Clock()
    body = read(tmp_path, forge(), pr_state(3), now=clock, engine_age=7200)
    assert body["age_s"] == 0 and body["engine"]["age_s"] == 7200
    clock.t += 10
    assert read(tmp_path, forge(), now=clock)["age_s"] == 10  # the cached read, ten seconds on
    clock.t += 60
    assert read(tmp_path, forge(), now=clock)["age_s"] == 0


def test_a_late_tick_is_a_note_and_a_tick_on_time_is_none(tmp_path):
    late = read(tmp_path / "a", forge(), pr_state(3), engine_age=7200)
    assert "Flows last ran 2 h ago. Rows of a flow may be out of date." in late["notes"]
    on_time = read(tmp_path / "b", forge(), pr_state(3), engine_age=100, refresh=True)
    assert not any("last ran" in n for n in on_time["notes"])
    slow_poll = read(tmp_path / "c", forge(), pr_state(3), engine_age=7200, refresh=True, poll_interval=3600)
    assert not any("last ran" in n for n in slow_poll["notes"])


def test_an_old_tick_is_no_note_when_no_tick_has_work(tmp_path):
    body = read(tmp_path, forge(), engine_age=7200, you="", writes=[])
    assert not any("last ran" in n for n in body["notes"])


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_header_counts_from_the_read_not_from_the_engine():
    script = js_function("readAge") + "\nprocess.stdout.write(JSON.stringify([readAge(0, 1000, 31000), " \
                                      "readAge(12, 1000, 4000), readAge(0, 0, 5000), readAge(undefined, 1000, 1400)]))"
    assert run_js(script) == [30, 15, None, 0]
    header = SOURCE[SOURCE.index("function Header("):SOURCE.index("function Loading(")]
    assert "readAge(data.age_s, updatedAt, now)" in header and "engine" not in header


# --- whose move a row is, and the button that is that move ------------------------------------------

BOTH = ("bot", "owner")


def merge_writes(f):
    return [w for w in f.writes if w[1].endswith("/merge")]


def test_a_flow_row_that_waits_for_someone_else_is_not_under_waiting_for_me(tmp_path):
    body = read(tmp_path, forge(), pr_state(3, human="waiting for bob to merge in Forgejo"), tokens=BOTH)
    row = body["rows"][0]
    assert row["tab"] == "running" and body["counts"] == {"waiting": 0, "parked": 0, "running": 1, "blocked": 0}
    assert body["label"] == "Forgebrücke"
    assert row["summary"] == {"tone": "wait", "text": "Every gate passed. Waiting for bob to merge."}
    assert row["waiting_on"] == "bob" and row["can_merge"] is False and row["next"] == "open_in_forgejo"


def test_the_merge_route_refuses_a_row_that_waits_for_someone_else(tmp_path):
    f = forge()
    state_mod.FlowStore(tmp_path).save_pr(pr_state(3, human="waiting for bob to merge in Forgejo"))
    values = {"url": BASE, "dry_run": False, "you": "acme"}
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": OWNER}
    status, body = desk.api_merge(lambda k, d=None: values.get(k, d), secrets.get,
                                  {"repo": DEMO, "number": 3, "head_sha": HEAD_SHA}, data_dir=tmp_path, transport=f)
    assert status == 409 and body["error"] == "not_waiting" and "Nothing was merged." in body["message"]
    assert merge_writes(f) == []


@pytest.mark.parametrize("human,you,tab", [
    ("waiting for acme to merge in Forgejo", "acme", "waiting"),
    ("waiting for Acme to merge in Forgejo", "acme", "waiting"),
    ("waiting for you to merge in Forgejo", "acme", "waiting"),
    ("waiting for you to merge in Forgejo", "", "waiting"),
    ("waiting for acme to merge in Forgejo", "", "running"),
    ("waiting for bob to merge in Forgejo", "acme", "running"),
])
def test_a_row_is_mine_only_when_the_flow_names_me_or_nobody(tmp_path, human, you, tab):
    row = read(tmp_path, forge(), pr_state(3, human=human), you=you)["rows"][0]
    assert row["tab"] == tab and (row["summary"]["tone"] == "ok") == (tab == "waiting")
    assert desk.tab_of(pr_state(3, human=human), you=you) == tab
    assert desk.tab_of(pr_state(3, human=human)) == "waiting"  # without a name to compare, as before


def test_my_flow_row_has_the_merge_as_its_main_button(tmp_path):
    row = read(tmp_path / "a", forge(), pr_state(3))["rows"][0]
    assert (row["tab"], row["next"], row["can_merge"]) == ("waiting", "merge_in_forgejo", False)
    row = read(tmp_path / "b", forge(), pr_state(3), tokens=BOTH)["rows"][0]
    assert (row["tab"], row["next"], row["can_merge"]) == ("waiting", "merge", True)


def test_a_review_row_keeps_review_as_its_main_button_also_with_a_write_token(tmp_path):
    row = read(tmp_path / "a", forge(), tokens=BOTH)["rows"][0]
    assert row["summary"]["text"] == "Waiting for your review."
    assert (row["next"], row["can_merge"]) == ("review_in_forgejo", True)
    row = read(tmp_path / "b", forge())["rows"][0]
    assert (row["next"], row["can_merge"]) == ("review_in_forgejo", False)


def test_no_merge_button_on_a_pull_request_forgejo_will_not_merge(tmp_path):
    f = forge()
    f.data[DEMO]["pulls"][3].update(draft=True, mergeable=False)
    row = read(tmp_path / "a", f, tokens=BOTH)["rows"][0]
    assert (row["next"], row["can_merge"]) == ("review_in_forgejo", False)
    row = read(tmp_path / "b", f, pr_state(3), tokens=BOTH, refresh=True)["rows"][0]
    assert (row["tab"], row["next"], row["can_merge"]) == ("waiting", "merge_in_forgejo", False)
    f.data[DEMO]["pulls"][3].update(draft=False, mergeable=True)
    row = read(tmp_path / "b", f, tokens=BOTH, refresh=True)["rows"][0]
    assert (row["next"], row["can_merge"]) == ("merge", True)


def test_rows_that_are_not_my_move_open_forgejo(tmp_path):
    body = read(tmp_path, forge(), pr_state(3, ("failed", "pending", "pending")),
                pr_state(4, ("passed", "running", "pending")), tokens=BOTH)
    assert {r["number"]: (r["tab"], r["next"], r["can_merge"]) for r in body["rows"]} == {
        3: ("blocked", "open_in_forgejo", False), 4: ("running", "open_in_forgejo", False)}


def test_the_row_draws_the_backends_move_and_merge_is_second_on_a_review_row():
    row = SOURCE[SOURCE.index("const MOVE = {"):SOURCE.index("// ── the parking area")]
    moves = SOURCE[SOURCE.index("const MOVE = {"):SOURCE.index("}", SOURCE.index("const MOVE = {"))]
    assert set(re.findall(r"^  (\w+): ", moves, re.M)) == set(desk.MOVES)
    main = row[row.index("  const main = "):row.index("  const comment = ")]  # the main button: the backend's move only
    assert "MOVE[row.next]" in main and "row.tab" not in main and "row.flow" not in row
    assert "row.can_merge && row.next !== 'merge'" in row


# --- what could not be read -------------------------------------------------------------------------

def no_answer(method, url, headers, body, timeout):
    raise client_mod.ForgejoError(None, "unreachable", "No answer.")


def failing(method, url, headers, body, timeout):
    return 500, {"content-type": "application/json"}, b'{"message": "internal error"}'


def test_one_outage_is_one_note_and_the_repo_list_says_it_could_not_be_read(tmp_path):
    body = read(tmp_path / "a", no_answer, pr_state(3))
    assert body["ok"] and [r["number"] for r in body["rows"]] == [3]
    assert body["notes"] == ["Forgejo did not answer. Review requests and repos are missing."]
    assert body["repos"] == [] and body["repos_error"] == "The repo list could not be read."
    body = read(tmp_path / "b", failing, pr_state(3))
    assert body["notes"] == ["Forgejo answered 500. Review requests and repos are missing."]


def test_an_empty_repo_list_that_was_read_is_no_error(tmp_path):
    f = FakeForgejo({BOT: {**bot_account(), "repos": []}}, data={})
    body = read(tmp_path, f)
    assert body["repos"] == [] and body["repos_error"] is None and body["notes"] == []


def test_without_hermes_token_the_desk_says_what_to_add_not_that_forgejo_is_silent(tmp_path):
    body = read(tmp_path, forge(), pr_state(3), tokens=("owner",))
    assert body["ok"] and body["rows"][0]["number"] == 3
    assert body["forge_error"].endswith("Checks, comments and review requests are missing.")
    assert "FORGEBRUECKE" not in body["forge_error"] and len(body["forge_error"]) <= 140
    assert body["repos_error"] == "The repo list could not be read."
    assert "'Forgejo did not answer, so" not in SOURCE


def test_the_empty_repos_tab_opens_forgejo_or_tries_again():
    repos = SOURCE[SOURCE.index("function Repos("):SOURCE.index("function MergeDialog(")]
    assert "openFields" not in repos and "'Open Forgejo'" in repos and "openExternal(forgeUrl)" in repos
    assert "title: error" in repos and "'Try again'" in repos
    assert "el(Repos, { error: data.repos_error, forgeUrl: data.forge_url, onRetry: refresh, repos })" in SOURCE


# --- the state sentence -----------------------------------------------------------------------------

def test_a_flow_without_a_merge_gate_that_finished_says_every_gate_passed(tmp_path):
    row = read(tmp_path, forge(), pr_state(3, ("passed",), keys=("review",)), review_every_pr=False)["rows"][0]
    assert row["tab"] == "running" and row["summary"] == {"tone": "ok", "text": "Every gate passed."}


def test_the_last_gate_is_called_merge(tmp_path):
    row = read(tmp_path, forge(), pr_state(3, ("passed", "passed", "pending")))["rows"][0]
    assert row["summary"] == {"tone": "run", "text": "Merge is next."}


def test_a_draft_says_draft_and_an_unmergeable_pull_request_guesses_no_conflict(tmp_path):
    f = forge()
    f.data[DEMO]["pulls"][3].update(draft=True, mergeable=False, title="WIP: half a change")
    row = read(tmp_path / "a", f)["rows"][0]
    assert row["draft"] is True and row["summary"] == {"tone": "wait", "text": "Draft. Waiting for your review."}
    row = read(tmp_path / "b", f, pr_state(3), refresh=True)["rows"][0]
    assert row["flow"] == "demo" and row["draft"] is True and row["summary"] == {"tone": "wait", "text": "Draft."}
    f = forge()
    f.data[DEMO]["pulls"][3]["mergeable"] = False
    row = read(tmp_path / "c", f)["rows"][0]
    assert row["draft"] is False and row["summary"] == {"tone": "bad",
                                                         "text": "Waiting for your review. Cannot be merged yet."}
    assert "Conflicts with" not in (ROOT / "desk.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("detail,text", [
    ("review by coding: Why does mul add? Fix it.", "Review failed: Why does mul add?"),
    ("review by coding: Stop! Now.", "Review failed: Stop!"),
    ("review by coding: The patch breaks mul. More.", "Review failed: The patch breaks mul."),
    ("the review run ended without writing a verdict", "Review failed: the review run ended without writing a verdict."),
    ("", "Review failed."),
])
def test_a_failed_gates_reason_keeps_its_own_end_mark(detail, text):
    row = {"flow": "demo", "tab": "blocked", "reason": None, "checks": None, "mergeable": None,
           "gates": [{"key": "review", "type": "review", "segment": "failed", "state": "failed", "detail": detail}]}
    assert desk.summarise(row)["text"] == text


def test_a_repo_name_in_another_case_is_still_one_pull_request(tmp_path):
    body = read(tmp_path, forge(), pr_state(3, ("failed", "pending", "pending"), repo="Acme/Forgebruecke-Demo"))
    assert [(r["number"], r["tab"]) for r in body["rows"]] == [(3, "blocked")]
    assert body["counts"] == {"waiting": 0, "parked": 0, "running": 0, "blocked": 1} and body["rows"][0]["mergeable"] is True


def test_a_comment_without_an_author_is_drawn_with_a_word(tmp_path):
    f = forge()
    f.data[DEMO]["comments"][3] = [{"id": 1, "user": None, "body": "Looks fine.", "created_at": "2026-11-08T12:00:00Z"}]
    comment = read(tmp_path, f)["rows"][0]["last_comment"]
    assert comment["author"] is None and comment["body"] == "Looks fine."
    assert "(comment.author || 'Someone')" in SOURCE


# --- a desk that cannot load ------------------------------------------------------------------------

def test_an_install_without_a_data_folder_still_shows_what_forgejo_knows():
    values = {"url": BASE, "you": "acme"}
    body = desk.api_desk(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                         data_dir=None, transport=forge(), now=Clock())[1]
    assert body["ok"] and body["counts"] == {"waiting": 1, "parked": 0, "running": 0, "blocked": 0}
    assert body["rows"][0]["reason"] == "review requested" and body["engine"]["age_s"] is None


@pytest.mark.parametrize("values,secrets,step", [
    ({}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}, "set_url"),
    ({"url": BASE}, {}, "set_token"),
])
def test_a_missing_setting_is_named_as_the_setup_step_it_is(tmp_path, values, secrets, step):
    body = desk.api_desk(lambda k, d=None: values.get(k, d), secrets.get, data_dir=tmp_path, transport=forge())[1]
    assert body == {"ok": False, "error": "not_configured", "title": "Set up Forgebrücke first",
                    "message": desk.NEXT_TEXT[step], "action": "settings", "setting": body["setting"],
                    "settings_href": desk.SETTINGS_HREF}


def test_a_read_that_broke_offers_another_try_not_the_settings(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(desk, "build_desk", broken)
    body = read(tmp_path, forge())
    assert body["ok"] is False and body["action"] == "retry" and "title" not in body and body["message"]


def test_the_problem_view_draws_the_backends_title_and_button():
    assert "'Forgebrücke needs one more setting'" not in SOURCE
    problem = SOURCE[SOURCE.index("function Refused("):SOURCE.index("const tabLabel")]
    assert "data.action === 'settings'" in problem and "data.title ||" in problem and "data.message" in problem
    assert SOURCE.count("el(Refused, {") == 2


# --- what the sidebar's poll costs ------------------------------------------------------------------

@pytest.mark.parametrize("you", ["acme", ""])
def test_an_hour_of_polls_probes_the_tokens_once(tmp_path, you):
    f, clock = forge(), Clock()
    for _ in range(60):
        body = read(tmp_path, f, tokens=BOTH, now=clock, you=you)
        clock.t += 60
    probes = [c for c in f.calls if c[0] != "GET"]
    assert 0 < len(probes) <= 8, len(probes)  # one doctor run: four write-scope probes for each token
    assert body["can_merge"] is True
    if not you:
        assert body["waiting_note"]["suggestion"] == "acme" and body["waiting_note"]["source"] == "write_token"


def test_a_new_write_token_and_a_click_on_refresh_are_probed_again(tmp_path):
    f, clock = forge(), Clock()
    read(tmp_path, f, tokens=BOTH, now=clock)
    first = len([c for c in f.calls if c[0] != "GET"])
    clock.t += 60
    read(tmp_path, f, tokens=BOTH, now=clock, refresh=True)
    assert len([c for c in f.calls if c[0] != "GET"]) == 2 * first
    clock.t += 60
    other = make_token("other")
    f.accounts[other] = f.accounts[OWNER]
    values = {"url": BASE, "dry_run": False, "you": "acme"}
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": other}
    desk.api_desk(lambda k, d=None: values.get(k, d), secrets.get, data_dir=tmp_path, transport=f, now=clock)
    assert len([c for c in f.calls if c[0] != "GET"]) == 3 * first


def test_a_check_forgejo_could_not_finish_is_not_remembered(tmp_path):
    from fb_fakes_desktop import OneRouteFails
    f, clock = forge(), Clock()
    assert read(tmp_path, OneRouteFails(f, "/api/v1/user", OWNER), tokens=BOTH, now=clock)["can_merge"] is False
    clock.t += 120
    assert read(tmp_path, f, tokens=BOTH, now=clock)["can_merge"] is True
