"""Comment as the user from the desk: the route, its refusals, the read-back, and the
capability "Comment as you" that the user's write token's write:issue lights."""
import json

import pytest

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, owner_account, sample_data, sub

desk, caps = sub("desk"), sub("capabilities")
BOT, OWNER = make_token("bot"), make_token("owner")
FULL = {"write:repository", "write:issue", "read:organization", "read:user"}
DARK = "Comment as you needs your write token (FORGEBRUECKE_PERSONAL_WRITE_TOKEN) with write:issue."


@pytest.fixture(autouse=True)
def fresh_cache():
    desk.clear_cache()
    yield
    desk.clear_cache()


def kit(scopes=FULL, owner=True):
    f = FakeForgejo({BOT: bot_account(), OWNER: {**owner_account(), "scopes": set(scopes)}}, data=sample_data())
    values = {"url": BASE, "you": "acme", "dry_run": True}
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, **({"FORGEBRUECKE_PERSONAL_WRITE_TOKEN": OWNER} if owner else {})}
    return f, (lambda k, d=None: values.get(k, d)), secrets.get


def comment(f, gs, sec, data_dir, **body):
    return desk.api_comment(gs, sec, {"repo": DEMO, "number": 3, "body": "Looks good to me.", **body},
                            data_dir=data_dir, transport=f, now=lambda: 77)


def test_his_comment_is_posted_as_him_and_read_back(tmp_path):
    f, gs, sec = kit()
    status, out = comment(f, gs, sec, tmp_path, body="  Looks good to me.\n")
    assert status == 200 and out["ok"] is True and out["message"] == "Comment posted as @acme."
    posts = [w for w in f.writes if w[0] == "POST"]
    assert posts == [("POST", f"/repos/{DEMO}/issues/3/comments", {"body": "Looks good to me."})]  # no footer
    assert out["url"] and out["id"]
    line = json.loads((tmp_path / "desk-actions.jsonl").read_text().splitlines()[-1])
    assert line == {"at": 77, "action": "comment", "repo": DEMO, "number": 3, "id": out["id"], "chars": 17,
                    "lane": "owner", "read_back": True}  # no body in the log


def test_preview_and_approvals_do_not_hold_his_own_comment(tmp_path):
    f, gs, sec = kit()  # dry_run is on in kit(): the owner lane ignores it, like Merge
    assert comment(f, gs, sec, tmp_path)[1]["ok"] is True


def test_a_read_back_that_differs_is_not_posted(tmp_path):
    f, gs, sec = kit()
    f.data[DEMO]["read_back_body"] = "something else"
    status, out = comment(f, gs, sec, tmp_path)
    assert status == 200 and out["ok"] is False and "different text" in out["message"]


def test_without_write_issue_the_feature_is_dark_and_nothing_is_sent(tmp_path):
    f, gs, sec = kit(scopes={"write:repository", "read:user"})
    status, out = comment(f, gs, sec, tmp_path)
    assert status == 403 and out["message"] == DARK and caps.dark_sentence("owner_comment") == DARK
    assert not [w for w in f.writes if w[0] == "POST"]


def test_without_his_write_token_the_feature_is_dark(tmp_path):
    f, gs, sec = kit(owner=False)
    status, out = comment(f, gs, sec, tmp_path)
    assert status == 403 and out["message"] == DARK and not f.writes


def test_a_closed_pull_request_gets_no_comment(tmp_path):
    f, gs, sec = kit()
    f.data[DEMO]["pulls"][3]["state"] = "closed"
    status, out = comment(f, gs, sec, tmp_path)
    assert status == 409 and out["error"] == "not_open" and not [w for w in f.writes if w[0] == "POST"]


@pytest.mark.parametrize("text,word", [("", "Write a comment"), ("   \n", "Write a comment"), (None, "Write a comment"),
                                       ("x" * 10_001, "at most 10,000")])
def test_an_empty_or_too_long_comment_is_refused(tmp_path, text, word):
    f, gs, sec = kit()
    status, out = comment(f, gs, sec, tmp_path, body=text)
    assert status == 400 and word in out["message"] and not f.writes


def test_ten_thousand_characters_are_fine(tmp_path):
    f, gs, sec = kit()
    assert comment(f, gs, sec, tmp_path, body="x" * 10_000)[1]["ok"] is True


def test_the_desk_says_whether_he_can_comment(tmp_path):
    for scopes, can in ((FULL, True), ({"write:repository", "read:user"}, False)):
        desk.clear_cache()
        f, gs, sec = kit(scopes=scopes)
        body = desk.api_desk(gs, sec, data_dir=tmp_path, transport=f, refresh=True)[1]
        assert body["can_comment"] is can and body["can_merge"] is True


# The capability.

def report(*scopes):
    return {"identity": "acme", "set": True, "scopes": {s: caps.YES for s in scopes}}


def test_write_issue_on_his_token_gives_comment():
    found, _ = caps.probe_caps({caps.OWNER: report("write:repository", "read:user", "write:issue")})
    assert found["owner:comment"] == caps.OWNER and found["owner:merge"] == caps.OWNER


def test_a_merge_only_token_keeps_its_verdict_and_merge_with_comment_dark():
    owner = {**report("write:repository", "read:user"), "role": caps.OWNER}
    assert caps.token_verdict(owner)["state"] == "ok"
    found, _ = caps.probe_caps({caps.OWNER: owner})
    assert "owner:comment" not in found and found["owner:merge"] == caps.OWNER
    assert caps.OWNER_SCOPES == ("write:repository", "read:user")


def test_the_recommended_mint_names_write_issue():
    owner_row = next(t for t in caps.TOKENS if t["role"] == caps.OWNER)
    assert owner_row["mint"] == ("write:repository", "read:user", "write:issue")
    setup = next(s for s in caps.setups_report() if s["id"] == "agent_owner")
    assert setup["mint"]["FORGEBRUECKE_PERSONAL_WRITE_TOKEN"] == ["write:repository", "read:user", "write:issue"]
    assert "Comment as you" in setup["lights"] and "Merge button" in setup["lights"]
