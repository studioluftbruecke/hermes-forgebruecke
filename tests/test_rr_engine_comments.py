"""A comment the forge stored but answered with an error is not posted again: the next tick finds it on the
pull request and goes on from it. Found means posted by Hermes' account, so a copy by someone else counts
for nothing."""
from fb_fakes_engine import Kit
from fb_fakes_rr_engine import list_posted_comments, stored_but_failed

COMMENT_POST = (lambda m, p: m == "POST" and p.endswith("/issues/3/comments"))


def status_comments(k) -> list:
    return [c for c in k.comments() if "Forgebrücke · flow" in c]


def test_a_status_comment_answered_502_is_posted_once_and_then_edited(tmp_path):
    k = Kit(tmp_path)
    list_posted_comments(k)
    stored_but_failed(k, COMMENT_POST)
    for _ in range(5):
        k.tick()
    assert len(status_comments(k)) == 1
    k.verdict("pass")
    k.tick()
    assert len(status_comments(k)) == 1
    assert any("/issues/comments/" in p for _, p, _ in k.sent("", "PATCH"))


def test_a_failure_notify_answered_502_is_posted_once(tmp_path):
    k = Kit(tmp_path)
    list_posted_comments(k)
    k.tick()
    k.verdict("fail", summary="mul is wrong")
    stored_but_failed(k, COMMENT_POST)
    for _ in range(4):
        k.tick()
    assert len(k.notifies()) == 1


def test_a_copy_of_the_status_comment_by_another_account_is_not_taken_over(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    copied = {"id": 77, "user": {"login": "mallory"}, "body": status_comments(k)[0],
              "html_url": "https://forge.example.test/x/issues/3#c77"}
    k2 = Kit(tmp_path / "second")
    list_posted_comments(k2, others=[copied])
    k2.tick()
    assert len(status_comments(k2)) == 1
    assert not any(p.endswith("/issues/comments/77") for _, p, _ in k2.sent("", "PATCH"))
