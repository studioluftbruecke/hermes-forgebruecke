"""M7: the desk's backend. Rows and tabs from the flow store, the checks and last comment from the forge,
the sidebar count, the merge button in the owner lane only, the reviewer-profile fallback, and the review
worker's session reading nothing."""
import json
import os
import types

import pytest

from fb_fakes import (BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, load_plugin, make_token, owner_account,
                      sample_data, sub)

plugin = load_plugin()
desk = sub("desk")
state_mod = sub("flows.state")
approval = sub("approval")
access_mod = sub("access")
BOT, OWNER = make_token("bot"), make_token("owner")


@pytest.fixture(autouse=True)
def fresh_cache():
    desk.clear_cache()
    yield
    desk.clear_cache()


def owner_write_account():
    return {**owner_account(), "scopes": {"write:repository", "write:issue", "read:organization", "read:user"}}


def forge():
    return FakeForgejo({BOT: bot_account(), OWNER: owner_write_account()}, data=sample_data())


def gate(key, state, detail=""):
    return {"gate_key": key, "type": key, "state": state, "detail": detail}


def pr_state(n=3, states=("passed", "passed", "waiting"), **kw):
    keys = ("review", "check", "human")
    return {"repo": DEMO, "pr": n, "flow": "demo", "head_sha": HEAD_SHA, "title": f"PR {n}",
            "url": f"{BASE}/{DEMO}/pulls/{n}", "updated_at": 100 + n,
            "gates": [gate(k, s, "waiting for acme to merge in Forgejo" if s == "waiting" else "")
                      for k, s in zip(keys, states)], **kw}


def store_with(tmp_path, *states):
    store = state_mod.FlowStore(tmp_path)
    for st in states:
        store.save_pr(st)
    return store


def getters(tokens=("bot",), you="", **extra):
    env = {"bot": ("FORGEBRUECKE_BOT_ACCOUNT_TOKEN", BOT), "owner": ("FORGEBRUECKE_PERSONAL_WRITE_TOKEN", OWNER)}
    secrets = dict(env[t] for t in tokens)
    values = {"url": BASE, "dry_run": False, "you": you, "writes": [{"repo_glob": DEMO, "flows": ["demo"]}], **extra}
    return (lambda k, d=None: values.get(k, d)), secrets.get


@pytest.mark.parametrize("n,label", [(0, "Forgebrücke"), (3, "Forgebrücke · 3"), (99, "Forgebrücke · 99"),
                                     (100, "Forgebrücke · 99+"), (250, "Forgebrücke · 99+")])
def test_nav_label(n, label):
    assert desk.nav_label(n) == label


@pytest.mark.parametrize("states,tab", [
    (("passed", "passed", "waiting"), "waiting"),
    (("passed", "running", "pending"), "running"),
    (("failed", "pending", "pending"), "blocked"),
    (("passed", "error", "pending"), "blocked"),
    (("passed", "passed", "pending"), "running"),
])
def test_tab_of(states, tab):
    assert desk.tab_of(pr_state(states=states)) == tab


def test_closed_or_merged_prs_leave_the_desk():
    assert desk.tab_of(pr_state(merged=True)) is None
    assert desk.tab_of(pr_state(closed=True)) is None


def test_last_comment_is_plain_text_without_markup_or_our_footer():
    """Seen on the Mac (2026-11-09): the desk showed a status comment as raw `| --- |` rows and `<sub>`."""
    status = ("**Forgebrücke · flow `demo` · head `acec6dfa1003`**\n\n| Gate | State | What happened |\n"
              "| --- | --- | --- |\n| review by coding | ✅ passed | fine |\n\n"
              "<sub>Kept up to date by Forgebrücke (Hermes). The merge into the target branch is yours.</sub>")
    assert desk.plain_text(status) == ("Forgebrücke · flow demo · head acec6dfa1003 Gate · State · What happened "
                                       "review by coding · ✅ passed · fine")
    notice = ("@acme the review gate is blocked on `d7a111347543`. Nothing was merged.\n\n"
              "<sub>Kept up to date by Forgebrücke (Hermes). The merge into the target branch is yours.</sub>")
    assert desk.plain_text(notice) == "@acme the review gate is blocked on d7a111347543. Nothing was merged."
    assert desk.plain_text("a <b>bold</b> | pipe in prose") == "a bold | pipe in prose"
    assert desk.plain_text(None) == ""


def test_desk_rows_counts_checks_and_last_comment(tmp_path):
    f = forge()
    store = store_with(tmp_path, pr_state(3), pr_state(4, ("failed", "pending", "pending")),
                       pr_state(5, ("passed", "passed", "waiting"), merged=True))
    gs, sec = getters(you="acme")
    status, body = desk.api_desk(gs, sec, data_dir=tmp_path, transport=f, now=lambda: 1000)
    assert status == 200 and body["ok"]
    assert body["label"] == "Forgebrücke · 1" and body["counts"] == {"waiting": 1, "parked": 0, "running": 0, "blocked": 1}
    first = body["rows"][0]
    assert first["number"] == 3 and first["tab"] == "waiting" and first["waiting_on"] == "acme"
    assert [g["segment"] for g in first["gates"]] == ["done", "done", "waiting"]
    assert {s["context"] for s in first["checks"]["statuses"]} == {"devops/tests", "devops/review"}
    assert first["last_comment"]["author"] == "acme"
    assert first["can_merge"] is False and body["can_merge"] is False
    assert body["rows"][1]["tab"] == "blocked" and body["rows"][1]["gates"][0]["segment"] == "failed"
    assert first["flow"] == "demo" and first["reason"] is None
    assert body["repos"] == [{"repo": DEMO, "right": "write", "url": f"{BASE}/{DEMO}",
                              "collaboration_url": f"{BASE}/{DEMO}/settings/collaboration", "private": True,
                              "flows": ["demo"], "mentions": True, "admin": False, "open_rows": 2, "open_prs": 1}]
    assert not any(w[0] != "GET" for w in f.writes)  # the desk only reads


def test_without_you_the_desk_says_so_and_suggests_the_owner_of_the_agents_repos(tmp_path):
    f = forge()
    store_with(tmp_path, pr_state(4, ("failed", "pending", "pending")))
    gs, sec = getters()
    body = desk.api_desk(gs, sec, data_dir=tmp_path, transport=f, now=lambda: 1000)[1]
    note = body["waiting_note"]
    assert note == {"message": "Which Forgejo user are you?", "suggestion": "acme", "source": "repo_owner",
                    "settings_href": desk.SETTINGS_HREF}
    assert body["counts"]["waiting"] == 0 and body["label"] == "Forgebrücke" and body["you"] is None
    assert not any(c[1].endswith("/repos/issues/search") for c in f.calls)


def test_waiting_for_me_lists_prs_that_request_your_review_without_a_flow(tmp_path):
    """D28 with review_every_pr off: demo #3 requests acme's review and runs no flow; #8 requests someone else
    and stays off the desk; a flow row stays a flow row."""
    f = forge()
    other = {**f.data[DEMO]["pulls"][3], "number": 8, "title": "Not for you", "requested_reviewers": [{"login": "bob"}]}
    f.data[DEMO]["pulls"][8] = other
    f.data[DEMO]["issues"][8] = {**f.data[DEMO]["issues"][3], "number": 8}
    store_with(tmp_path, pr_state(4, ("failed", "pending", "pending")))
    gs, sec = getters(you="acme", review_every_pr=False)
    body = desk.api_desk(gs, sec, data_dir=tmp_path, transport=f, now=lambda: 1000)[1]
    waiting = [r for r in body["rows"] if r["tab"] == "waiting"]
    assert [(r["number"], r["flow"], r["reason"], r["gates"]) for r in waiting] == [(3, None, "review requested", [])]
    row = waiting[0]
    assert row["url"] == f"{BASE}/{DEMO}/pulls/3" and row["head_sha"] == HEAD_SHA and row["can_merge"] is False
    assert {s["context"] for s in row["checks"]["statuses"]} == {"devops/tests", "devops/review"}
    assert row["last_comment"]["author"] == "acme"
    assert body["label"] == "Forgebrücke · 1" and body["counts"]["waiting"] == 1 and body["waiting_note"] is None
    assert body["repos"][0]["open_rows"] == 1  # the review row is not a flow row
    searches = [c for c in f.calls if c[1].endswith("/repos/issues/search")]
    pulls = [c for c in f.calls if c[1].endswith(f"/repos/{DEMO}/pulls")]
    assert len(searches) == 1 and searches[0][2]["type"] == "pulls" and searches[0][2]["state"] == "open"
    assert len(pulls) == 1  # one pulls list per repo with open pull requests


def add_pr(f, n, *, author="hermes", asked=(), title=None):
    pr = {**f.data[DEMO]["pulls"][3], "number": n, "title": title or f"PR {n}", "user": {"login": author},
          "requested_reviewers": [{"login": a} for a in asked], "html_url": f"{BASE}/{DEMO}/pulls/{n}"}
    f.data[DEMO]["pulls"][n] = pr
    f.data[DEMO]["issues"][n] = {**f.data[DEMO]["issues"][3], "number": n}
    return pr


def test_every_open_pr_that_does_not_ask_you_yet_still_waits_for_you(tmp_path):
    """D33 (default on): #8 was opened without a review request (a raw API call, the web page) and still shows,
    marked so, without the merge button; #9 is yours and #10 you already reviewed, so neither shows."""
    f = forge()
    add_pr(f, 8, asked=("bob",))
    add_pr(f, 9, author="acme")
    add_pr(f, 10)
    f.data[DEMO]["reviews"][10] = [{"id": 5, "user": {"login": "acme"}, "state": "REQUEST_CHANGES", "dismissed": False}]
    gs, sec = getters(tokens=("bot", "owner"), you="acme")
    body = desk.api_desk(gs, sec, data_dir=tmp_path, transport=f, now=lambda: 1000)[1]
    rows = {r["number"]: r for r in body["rows"]}
    assert sorted(rows) == [3, 8]
    assert rows[3]["reason"] == "review requested" and rows[8]["reason"] == "review not requested yet"
    assert rows[8]["tab"] == "waiting" and rows[8]["can_merge"] is False
    assert body["counts"]["waiting"] == 2 and body["label"] == "Forgebrücke · 2"
    assert not any(w[0] != "GET" for w in f.writes)  # the desk only reads; the tick asks


def test_a_pr_with_a_flow_and_a_review_request_shows_once_as_its_flow_row(tmp_path):
    f = forge()
    store_with(tmp_path, pr_state(3))
    gs, sec = getters(you="acme")
    body = desk.api_desk(gs, sec, data_dir=tmp_path, transport=f, now=lambda: 1000)[1]
    assert [(r["number"], r["flow"]) for r in body["rows"]] == [(3, "demo")]
    assert body["counts"]["waiting"] == 1


def test_a_failing_review_search_keeps_the_flow_rows_and_says_why(tmp_path):
    f = forge()
    orig = f.__call__

    def broken(method, url, headers, body, timeout):
        if "/repos/issues/search" in url:
            return 403, {}, b'{"message": "forbidden"}'
        return orig(method, url, headers, body, timeout)
    store_with(tmp_path, pr_state(3))
    gs, sec = getters(you="acme")
    body = desk.build_desk(access_mod.load_access(gs, sec, transport=broken), state_mod.FlowStore(tmp_path))
    assert [r["number"] for r in body["rows"]] == [3]
    assert body["notes"] == ["Forgejo answered 403. Review requests are missing."]


def test_desk_without_tokens_explains_and_points_at_the_settings(tmp_path):
    status, body = desk.api_desk(lambda k, d=None: {"url": BASE}.get(k, d), {}.get, data_dir=tmp_path)
    assert status == 200 and body["ok"] is False and body["error"] == "not_configured"
    assert body["settings_href"] == desk.SETTINGS_HREF and body["message"] == desk.NEXT_TEXT["set_token"]


def test_desk_renders_stored_rows_when_the_forge_is_down(tmp_path):
    def down(*a, **k):
        raise OSError("connection refused")
    gs, sec = getters()
    store_with(tmp_path, pr_state(3))
    status, body = desk.api_desk(gs, sec, data_dir=tmp_path, transport=down)
    assert body["ok"] and body["rows"][0]["number"] == 3 and body["rows"][0]["checks"] is None


# --- the merge button: owner lane only ---------------------------------------------------------------

def merge(tmp_path, f, tokens, sha=HEAD_SHA, states=("passed", "passed", "waiting")):
    store_with(tmp_path, pr_state(3, states))
    gs, sec = getters(tokens, you="acme")
    return desk.api_merge(gs, sec, {"repo": DEMO, "number": 3, "head_sha": sha}, data_dir=tmp_path, transport=f)


def merges(f):
    return [w for w in f.writes if w[1].endswith("/merge")]


def test_merge_without_a_write_token_of_his_account_is_dark_and_writes_nothing(tmp_path):
    f = forge()
    status, body = merge(tmp_path, f, ("bot",))
    assert status == 403 and body["error"] == "dark" and "FORGEBRUECKE_PERSONAL_WRITE_TOKEN" in body["message"]
    assert merges(f) == []


def test_merge_with_his_write_token_pins_the_head_and_never_uses_the_bot(tmp_path):
    f = forge()
    used = []
    orig = f.__call__

    def rec(method, url, headers, body, timeout):
        if url.endswith("/merge"):
            used.append(headers.get("Authorization"))
        return orig(method, url, headers, body, timeout)
    store_with(tmp_path, pr_state(3))
    gs, sec = getters(("bot", "owner"), you="acme")
    status, body = desk.api_merge(gs, sec, {"repo": DEMO, "number": 3, "head_sha": HEAD_SHA}, data_dir=tmp_path,
                                  transport=rec)
    assert status == 200, body
    assert merges(f)[0][2] == {"Do": "merge", "head_commit_id": HEAD_SHA}
    assert used == [f"token {OWNER}"]
    log = [json.loads(x) for x in (tmp_path / "desk-actions.jsonl").read_text().splitlines()]
    assert log[0]["lane"] == "owner" and log[0]["head_sha"] == HEAD_SHA


def test_merge_refuses_a_moved_head_and_a_flow_that_is_not_waiting(tmp_path):
    f = forge()
    other = "b" * 40
    assert merge(tmp_path, f, ("bot", "owner"), sha=other)[0] == 409
    status, body = merge(tmp_path, f, ("bot", "owner"), states=("passed", "running", "pending"))
    assert status == 409 and body["error"] == "not_waiting"
    assert merges(f) == []


def test_merge_a_pr_without_a_flow_that_requests_my_review(tmp_path):
    """Seen on the Mac (2026-11-09): with his write token set, review-requested rows had no Merge button and
    /merge refused them as not_waiting. They merge now on his click, pinned to the head the desk showed."""
    f = forge()  # sample PR #3 requests a review from acme; no flow state is stored
    gs, sec = getters(("bot", "owner"), you="acme")
    _, desk_body = desk.api_desk(gs, sec, data_dir=tmp_path, transport=f, now=lambda: 1000)
    row = next(r for r in desk_body["rows"] if r["number"] == 3)
    assert row["flow"] is None and row["can_merge"] is True
    status, body = desk.api_merge(gs, sec, {"repo": DEMO, "number": 3, "head_sha": HEAD_SHA}, data_dir=tmp_path, transport=f)
    assert status == 200, body
    assert merges(f)[0][2] == {"Do": "merge", "head_commit_id": HEAD_SHA}


def test_merge_without_a_flow_refuses_when_my_review_is_not_requested_or_the_head_moved(tmp_path):
    f = forge()
    gs, sec = getters(("bot", "owner"), you="someone-else")
    status, body = desk.api_merge(gs, sec, {"repo": DEMO, "number": 3, "head_sha": HEAD_SHA}, data_dir=tmp_path, transport=f)
    assert status == 409 and body["error"] == "not_requested"
    gs, sec = getters(("bot", "owner"), you="acme")
    status, body = desk.api_merge(gs, sec, {"repo": DEMO, "number": 3, "head_sha": "b" * 40}, data_dir=tmp_path, transport=f)
    assert status == 409 and body["error"] == "head_moved"
    assert merges(f) == []


def test_review_rows_offer_no_merge_without_his_write_token(tmp_path):
    f = forge()
    gs, sec = getters(("bot",), you="acme")
    _, body = desk.api_desk(gs, sec, data_dir=tmp_path, transport=f, now=lambda: 1000)
    assert [r["can_merge"] for r in body["rows"] if r["number"] == 3] == [False]


def test_merge_rejects_a_bad_request():
    gs, sec = getters(("bot", "owner"))
    assert desk.api_merge(gs, sec, {"repo": DEMO, "number": 3, "head_sha": "abc"}, data_dir=None)[0] == 400


# --- the reviewer profile (N20 fallback) ------------------------------------------------------------

class FakeHermes:
    """`hermes profile create` and `hermes -p <name> config unset <key>` as the route sees them. The reviewer's
    secrets live in a dict, so any .env file the plugin opened would be a file this fake never wrote."""

    def __init__(self, tmp_path, keys=("OPENCODE_GO_API_KEY", "FORGEBRUECKE_BOT_ACCOUNT_TOKEN",
                                        "FORGEBRUECKE_PERSONAL_WRITE_TOKEN"), unset_error=""):
        self.root, self.keys, self.unset_error, self.calls = tmp_path / "profiles", set(keys), unset_error, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd[1:])
        if cmd[1:3] == ["profile", "create"]:
            (self.root / cmd[3]).mkdir()
            (self.root / cmd[3] / "config.yaml").write_text("x: 1")
            fd = os.open(self.root / cmd[3] / ".env", os.O_WRONLY | os.O_CREAT)  # not through open(): see no_env_file
            os.write(fd, "".join(f"{k}=v\n" for k in sorted(self.keys)).encode())
            os.close(fd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        assert cmd[1:5] == ["-p", desk.REVIEWER, "config", "unset"], cmd
        if self.unset_error:
            return types.SimpleNamespace(returncode=1, stdout="", stderr=self.unset_error)
        if cmd[5] not in self.keys:
            return types.SimpleNamespace(returncode=1, stdout="", stderr=f"Config key not set: {cmd[5]}")
        self.keys.discard(cmd[5])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def no_env_file(monkeypatch):
    """Fails the test when the plugin opens any .env file (catalog review of 0.0.22: Hermes owns that file)."""
    import builtins
    import io
    import pathlib
    real_open, real_io_open = builtins.open, io.open

    def guarded(opener):
        def open_(file, *a, **k):
            assert pathlib.Path(str(file)).name != ".env", f"the plugin opened {file}"
            return opener(file, *a, **k)
        return open_
    monkeypatch.setattr(builtins, "open", guarded(real_open))
    monkeypatch.setattr(io, "open", guarded(real_io_open))


def reviewer_home(tmp_path):
    home = tmp_path / "profiles" / "coding"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("x: 1")
    return home


def test_create_reviewer_removes_forge_tokens_through_hermes_and_writes_the_soul(tmp_path, no_env_file):
    home, hermes = reviewer_home(tmp_path), FakeHermes(tmp_path)
    status, body = desk.api_create_reviewer({}, source="coding", home=home, run=hermes, hermes="/bin/hermes")
    assert status == 200 and body["ok"] and body["created"]
    assert hermes.calls[0][:5] == ["profile", "create", desk.REVIEWER, "--clone-from", "coding"]
    assert hermes.keys == {"OPENCODE_GO_API_KEY"}
    assert "verdict file" in (tmp_path / "profiles" / desk.REVIEWER / "SOUL.md").read_text()
    again = desk.api_create_reviewer({}, source="coding", home=home, run=hermes, hermes="/bin/hermes")[1]
    assert again["created"] is False and len(hermes.calls) == 3


def test_a_source_without_the_write_token_still_gets_a_reviewer(tmp_path, no_env_file):
    hermes = FakeHermes(tmp_path, keys=("FORGEBRUECKE_BOT_ACCOUNT_TOKEN",))
    body = desk.api_create_reviewer({}, source="coding", home=reviewer_home(tmp_path), run=hermes,
                                    hermes="/bin/hermes")[1]
    assert body["ok"] and body["created"] and hermes.keys == set()


def test_a_failed_unset_answers_with_the_commands_still_to_run(tmp_path, no_env_file):
    hermes = FakeHermes(tmp_path, unset_error="✗ .env is read-only")
    status, body = desk.api_create_reviewer({}, source="coding", home=reviewer_home(tmp_path), run=hermes,
                                            hermes="/bin/hermes")
    assert status == 200 and body["ok"] is False and body["error"] == "create_failed"
    assert body["command"].splitlines() == [
        f"hermes -p {desk.REVIEWER} config unset FORGEBRUECKE_BOT_ACCOUNT_TOKEN",
        f"hermes -p {desk.REVIEWER} config unset FORGEBRUECKE_PERSONAL_WRITE_TOKEN"]
    assert "before a flow uses it" in body["message"]
    assert not (tmp_path / "profiles" / desk.REVIEWER / "SOUL.md").exists()


def test_create_reviewer_without_hermes_shows_the_commands(tmp_path):
    home = tmp_path / "profiles" / "coding"
    home.mkdir(parents=True)
    body = desk.api_create_reviewer({}, source="coding", home=home, hermes="", run=None)[1]
    assert body["error"] == "run_it_yourself"
    assert body["command"].splitlines() == [
        f"hermes profile create {desk.REVIEWER} --clone-from coding --no-alias",
        f"hermes -p {desk.REVIEWER} config unset FORGEBRUECKE_BOT_ACCOUNT_TOKEN",
        f"hermes -p {desk.REVIEWER} config unset FORGEBRUECKE_PERSONAL_WRITE_TOKEN"]


# --- the review worker reads nothing (measured leak path, 2026-11-09) ---------------------------------

def test_a_review_worker_session_reads_nothing_from_the_forge():
    run = approval.RunContext(single_query=True, platform="cli", source="forgebruecke-review")
    assert run.lane() == ("mention", "")
    f = forge()
    gs, sec = getters(("bot", "owner"))
    h = plugin.make_read_handler("forge_get", gs, sec, transport=f, lane_fn=run.lane)
    out = json.loads(h({"kind": "repo", "repo": DEMO}))
    assert out["error"] == "lane_scope" and "reads nothing" in out["message"] and f.calls == []
    assert approval.RunContext(platform="cli").lane() == ("agent", "")


def test_the_review_marker_in_the_ephemeral_prompt_makes_the_turn_a_review_worker(monkeypatch):
    class Ctx:
        def current_cron_execution(self):
            return None

        def session_surface(self):
            return "cli"
    monkeypatch.setenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", sub("flows.runner").REVIEW_SYSTEM)
    assert approval.run_context_from(Ctx()).lane() == ("mention", "")
    monkeypatch.setenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "be brief. [forgebruecke-review]")
    assert approval.run_context_from(Ctx()).lane() == ("agent", "")  # only a prompt that starts with it
    monkeypatch.delenv("HERMES_EPHEMERAL_SYSTEM_PROMPT")
    assert approval.run_context_from(Ctx()).lane() == ("agent", "")


def test_settings_route_lists_the_two_key_fields_with_what_the_probe_found(tmp_path):
    gs, sec = getters()
    status, body = desk.api_settings(gs, sec, transport=forge(), home=tmp_path, now=lambda: 1000)
    assert status == 200 and body["ok"] and body["forge_url"] == BASE
    bot, owner = body["tokens"]
    assert [t["env"] for t in body["tokens"]] == ["FORGEBRUECKE_BOT_ACCOUNT_TOKEN", "FORGEBRUECKE_PERSONAL_WRITE_TOKEN"]
    assert bot["set"] and bot["required"] and bot["login"] == "forgebruecke-bot" and bot["error"] is None
    assert bot["mint"] == ["read:notification", "write:issue", "write:repository", "read:user"]
    assert not owner["set"] and not owner["required"] and owner["mint"] == ["write:repository", "read:user", "write:issue"]
    assert "full access to your account" in owner["risk"] and "Merge and Comment buttons" in owner["unlocks"]
    assert body["you"] is None and body["you_suggestion"] == "acme"
    assert BOT not in json.dumps(body) and "READ_TOKEN" not in json.dumps(body)


def test_settings_route_without_an_address_still_names_every_step(tmp_path):
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}
    status, body = desk.api_settings(lambda k, d=None: d, secrets.get, home=tmp_path, now=lambda: 1000)
    assert status == 200 and body["forge_url"] is None
    assert [(t["role"], t["set"], t["login"]) for t in body["tokens"]] == [("bot", True, None), ("owner", False, None)]
    assert "address" in body["next_step"]


def test_a_token_under_its_old_name_is_named_but_never_used(tmp_path):
    secrets = {"FORGEBRUECKE_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_READ_TOKEN": BOT}
    status, body = desk.api_settings(lambda k, d=None: {"url": BASE}.get(k, d), secrets.get, transport=forge(),
                                     home=tmp_path, now=lambda: 1000)
    bot = body["tokens"][0]
    assert bot["set"] is False and bot["renamed_from"] == "FORGEBRUECKE_TOKEN" and bot["login"] is None
    assert [t["renamed_from"] for t in body["tokens"][1:]] == [None]
