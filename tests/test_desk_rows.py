"""What a desk row says at a glance: one state sentence, its age, the last comment a person wrote, and
repo cards that put the repos with work first."""
import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, make_token, repo, sample_data, sub
from fb_fakes_desktop import UsersForge

desk = sub("desk")
state_mod = sub("flows.state")
BOT = make_token("bot")
STATUS_FOOTER = "<sub>Kept up to date by Forgebrücke (Hermes). The merge into the target branch is yours.</sub>"


@pytest.fixture(autouse=True)
def fresh_cache():
    desk.clear_cache()
    yield
    desk.clear_cache()


def forge(n_repos=1):
    return FakeForgejo({BOT: bot_account(n_repos)}, data=sample_data())


def gate(key, state, detail=""):
    return {"key": key, "type": key, "segment": desk.segment(state), "state": state, "detail": detail}


def flow_row(*gates, tab="running", **extra):
    return {"flow": "demo", "tab": tab, "base": "main", "gates": list(gates), "reason": None, "checks": None,
            "mergeable": None, "waiting_on": None, **extra}


def review_row(reason, **extra):
    return {"flow": None, "tab": "waiting", "base": "main", "gates": [], "reason": reason, "checks": None,
            "mergeable": None, "waiting_on": None, **extra}


def checks(**states):
    return {"state": "x", "statuses": [{"context": c.replace("_", "/"), "state": s} for c, s in states.items()]}


@pytest.mark.parametrize("row,you,tone,text", [
    (flow_row(gate("review", "passed"), gate("human", "waiting"), tab="waiting"), "acme", "ok",
     "Ready. Every gate passed, your merge is next."),
    (flow_row(gate("review", "passed"), gate("human", "waiting"), tab="waiting", waiting_on="acme"), "Acme", "ok",
     "Ready. Every gate passed, your merge is next."),
    (flow_row(gate("review", "passed"), gate("human", "waiting"), waiting_on="bob"), "acme", "wait",
     "Every gate passed. Waiting for bob to merge."),
    (review_row("review requested"), "acme", "wait", "Waiting for your review."),
    (review_row("review not requested yet"), "acme", "wait", "Open, and nobody asked you to review it yet."),
    (review_row("review requested", mergeable=False), "acme", "bad", "Waiting for your review. Cannot be merged yet."),
    (review_row("review requested", mergeable=False, draft=True), "acme", "wait", "Draft. Waiting for your review."),
    (review_row("review not requested yet", draft=True), "acme", "wait", "Draft."),
    (review_row("review requested", checks=checks(devops_tests="failure", lint="success")), "acme", "bad",
     "Check devops/tests failed."),
    (review_row("review requested", checks=checks(a="failure", b="error", c="success")), "acme", "bad",
     "2 checks failed."),
    (flow_row(gate("review", "failed", "review by coding: The patch breaks mul. It also drops a test."),
              gate("human", "pending"), tab="blocked"), "acme", "bad", "Review failed: The patch breaks mul."),
    (flow_row(gate("review", "error", "the review run ended without writing a verdict"), tab="blocked"), "acme",
     "bad", "Review failed: the review run ended without writing a verdict."),
    (flow_row(gate("review", "passed"), gate("check", "running"), gate("human", "pending")), "acme", "run",
     "Check is running."),
    (flow_row(gate("review", "passed"), gate("check", "pending"), gate("human", "pending")), "acme", "run",
     "Check is next."),
])
def test_one_sentence_says_where_a_row_stands(row, you, tone, text):
    assert desk.summarise(row, you) == {"tone": tone, "text": text}


def test_a_failed_gate_outranks_a_conflict_and_a_long_detail_is_cut():
    row = flow_row(gate("review", "failed", "x" * 400), tab="blocked", mergeable=False)
    out = desk.summarise(row, "acme")
    assert out["tone"] == "bad" and out["text"].startswith("Review failed: xxx") and len(out["text"]) <= 140


def pr_state(n=3, states=("passed", "passed", "waiting"), detail="review by coding: Looks right. Ship it."):
    keys = ("review", "check", "human")
    details = {"review": detail, "human": "waiting for acme to merge in Forgejo"}
    return {"repo": DEMO, "pr": n, "flow": "demo", "head_sha": HEAD_SHA, "title": f"PR {n}", "base": "main",
            "url": f"{BASE}/{DEMO}/pulls/{n}", "updated_at": 400,
            "gates": [{"gate_key": k, "type": k, "state": s, "detail": details.get(k, "")}
                      for k, s in zip(keys, states)]}


def build(tmp_path, f, *states, you="acme", now=1000):
    store = state_mod.FlowStore(tmp_path)
    for st in states:
        store.save_pr(st)
    values = {"url": BASE, "dry_run": False, "you": you, "writes": [{"repo_glob": DEMO, "flows": ["demo"]}]}
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}
    return desk.api_desk(lambda k, d=None: values.get(k, d), secrets.get, data_dir=tmp_path, transport=f,
                         now=lambda: now)[1]


def test_rows_carry_their_summary_age_mergeable_and_the_reviews_sentence(tmp_path):
    f = forge()
    f.data[DEMO]["status"] = {}
    store = state_mod.FlowStore(tmp_path)
    store.save_pr(pr_state(3))
    saved_at = store.all_prs()[0]["updated_at"]
    body = build(tmp_path, f, now=saved_at + 600)
    row = body["rows"][0]
    assert row["summary"] == {"tone": "ok", "text": "Ready. Every gate passed, your merge is next."}
    assert row["age_s"] == 600 and row["mergeable"] is True
    assert row["review_summary"] == "Review by coding: Looks right."
    for key in ("id", "tab", "repo", "number", "title", "url", "author", "base", "head_ref", "head_sha", "flow",
                "reason", "gates", "checks", "last_comment", "waiting_on", "can_merge", "updated_at"):
        assert key in row, key


def test_a_review_row_reads_mergeable_from_the_pull_request_and_has_no_review_sentence(tmp_path):
    f = forge()
    f.data[DEMO]["pulls"][3]["mergeable"] = False
    row = build(tmp_path, f)["rows"][0]
    assert row["flow"] is None and row["mergeable"] is False and row["review_summary"] is None
    assert row["summary"] == {"tone": "bad", "text": "Waiting for your review. Cannot be merged yet."}
    assert row["age_s"] is not None and row["age_s"] >= 0


def test_a_review_gate_that_is_not_done_gives_no_review_sentence(tmp_path):
    row = build(tmp_path, forge(), pr_state(3, ("failed", "pending", "pending")))["rows"][0]
    assert row["review_summary"] is None and row["summary"]["tone"] == "bad"


def comment(i, author, body, minute):
    return {"id": i, "user": {"login": author}, "body": body, "created_at": f"2026-11-08T12:{minute:02d}:00Z",
            "html_url": f"{BASE}/{DEMO}/pulls/3#issuecomment-{i}"}


def test_the_last_comment_is_the_last_one_a_person_wrote(tmp_path):
    f = forge()
    f.data[DEMO]["comments"][3] = [
        comment(1, "acme", "Please rename the helper. " + "y" * 300, 1),
        comment(2, "forgebruecke-bot", "| Gate | State |\n| --- | --- |\n| review | passed |\n\n" + STATUS_FOOTER, 2),
        comment(3, "forgebruecke-bot", "Done.\n\n<sub>Posted by Hermes through Forgebrücke</sub>", 3)]
    now = desk._epoch("2026-11-08T12:11:00Z")
    last = build(tmp_path, f, now=now)["rows"][0]["last_comment"]
    assert last["author"] == "acme" and last["body"].startswith("Please rename the helper.")
    assert len(last["body"]) <= 140 and last["age_s"] == 600 and last["url"].endswith("issuecomment-1")


def test_only_own_comments_means_no_last_comment(tmp_path):
    f = forge()
    f.data[DEMO]["comments"][3] = [comment(2, "forgebruecke-bot", "status\n\n" + STATUS_FOOTER, 2)]
    assert build(tmp_path, f)["rows"][0]["last_comment"] is None


def test_a_person_s_comment_behind_a_page_of_own_comments_is_still_found(tmp_path):
    f = forge()
    own = [comment(10 + i, "forgebruecke-bot", f"tick {i}\n\n{STATUS_FOOTER}", i % 60) for i in range(14)]
    f.data[DEMO]["comments"][3] = [comment(1, "acme", "First words.", 0)] + own
    assert build(tmp_path, f)["rows"][0]["last_comment"]["body"] == "First words."


def test_repo_cards_count_open_pull_requests_and_put_work_first(tmp_path):
    f = forge(3)
    body = build(tmp_path, f)
    cards = body["repos"]
    assert [c["repo"] for c in cards] == [DEMO, "acme/extra-0", "acme/extra-1"]
    assert cards[0]["open_prs"] == 1 and cards[0]["flows"] == ["demo"] and cards[0]["open_rows"] == 0
    assert [c["open_prs"] for c in cards[1:]] == [0, 0]
    f = FakeForgejo({BOT: {**bot_account(), "repos": [repo("acme/alpha", "write"), repo("acme/zeta", "write"),
                                                     repo(DEMO, "write")]}}, data=sample_data())
    desk.clear_cache()
    sub("access").clear_rights()
    assert [c["repo"] for c in build(tmp_path, f, you="")["repos"]] == [DEMO, "acme/alpha", "acme/zeta"]


def test_the_note_about_you_is_a_question_with_a_user_never_an_organisation(tmp_path):
    f = FakeForgejo({BOT: {**bot_account(), "repos": [repo("studio/a", "write"), repo("studio/b", "write"),
                                                     repo("kay/notes", "write")]}}, data={})
    wrapped = UsersForge(f, users={"kay"}, orgs={"studio"})
    note = build(tmp_path, wrapped, you="")["waiting_note"]
    assert note["message"] == "Which Forgejo user are you?"
    assert note["suggestion"] == "kay" and note["source"] == "repo_owner"
    assert note["settings_href"] == desk.SETTINGS_HREF
