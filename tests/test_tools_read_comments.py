"""forgejo_list kind=comments: a whole thread, newest first, and a pull request's review line comments."""
import pytest

from fb_fakes import DEMO, FakeForgejo, HEAD_SHA, sample_data
from fb_fakes_tools_read import CappedAt30, NoPaging, OTHER, ThreadForge, mention, paths, read


def thread(n, body=lambda i: f"comment {i}"):
    return [{"id": i, "user": {"login": "acme"}, "body": body(i), "created_at": "2026-01-01T11:00:00Z"}
            for i in range(1, n + 1)]


def numbers(out):
    return [int(c["body"].split()[1]) for c in out["items"]]


def comments(n, args=None, forge_cls=ThreadForge, **kw):
    data = sample_data()
    data[DEMO]["comments"][1] = thread(n)
    return read("forge_list", {"kind": "comments", "repo": DEMO, "number": 1, **(args or {})}, forge_cls=forge_cls,
                data=data, **kw)


@pytest.mark.parametrize("forge_cls", [FakeForgejo, ThreadForge, CappedAt30, NoPaging])
def test_every_page_of_a_long_thread_newest_first(forge_cls):
    out, _ = comments(520, forge_cls=forge_cls)
    assert out["ok"] and numbers(out) == list(range(520, 500, -1))
    assert out["total"] == 520 and out["has_more"] is True and out["next_page"] == 2
    out, _ = comments(520, {"page": 2}, forge_cls=forge_cls)
    assert numbers(out) == list(range(500, 480, -1))
    out, _ = comments(520, {"page": 26}, forge_cls=forge_cls)
    assert numbers(out) == list(range(20, 0, -1)) and out["has_more"] is False
    out, _ = comments(520, {"page": 27}, forge_cls=forge_cls)
    assert out["ok"] and out["items"] == [] and out["total"] == 520


@pytest.mark.parametrize("forge_cls", [ThreadForge, CappedAt30, NoPaging])
@pytest.mark.parametrize("total", [0, 1, 19, 20, 21, 49, 50, 51, 61, 99, 100, 101, 149])
def test_windows_join_without_gaps_or_repeats(forge_cls, total):
    seen = []
    for page in range(1, 12):
        out, _ = comments(total, {"page": page, "limit": 15}, forge_cls=forge_cls)
        assert out["total"] == total
        seen += numbers(out)
        if not out["has_more"]:
            break
    assert seen == list(range(total, 0, -1))


def test_a_page_of_a_long_thread_costs_at_most_two_requests():
    for page in (1, 2, 7, 26):
        _, forge = comments(520, {"page": page})
        assert len(paths(forge, "/issues/1/comments")) <= 2, page


def test_a_comment_the_summary_cut_is_readable_here():
    data = sample_data()
    data[DEMO]["comments"][1] = thread(1, body=lambda i: "x" * 2000)
    out, _ = read("forge_list", {"kind": "comments", "repo": DEMO, "number": 1}, data=data)
    assert len(out["items"][0]["body"]) == 2000


def test_a_pull_requests_reviews_carry_their_line_comments():
    data = sample_data()
    data[DEMO]["reviews"][3] = [
        {"id": 1, "user": {"login": "hermes"}, "state": "COMMENT", "body": "looking", "commit_id": HEAD_SHA,
         "submitted_at": "2026-01-01T12:01:00Z", "comments_count": 0},
        {"id": 2, "user": {"login": "acme"}, "state": "REQUEST_CHANGES", "body": "see lines", "commit_id": HEAD_SHA,
         "submitted_at": "2026-01-01T12:05:00Z", "comments_count": 2},
        {"id": 3, "user": {"login": "acme"}, "state": "REQUEST_REVIEW", "body": "", "commit_id": HEAD_SHA},
    ]
    data[DEMO]["review_comments"] = {2: [
        {"id": 7, "path": "calc.py", "position": 12, "body": "use *", "user": {"login": "acme"}},
        {"id": 8, "path": "calc.py", "position": 0, "original_position": 4, "body": "why gone?", "user": {"login": "acme"}}]}
    out, forge = read("forge_list", {"kind": "comments", "repo": DEMO, "number": 3}, forge_cls=ThreadForge, data=data)
    newest, older = out["reviews"]
    assert newest["state"] == "REQUEST_CHANGES" and newest["author"] == "acme"
    assert newest["comments"] == [{"path": "calc.py", "line": 12, "body": "use *"},
                                  {"path": "calc.py", "line": 4, "body": "why gone?"}]
    assert older["state"] == "COMMENT" and "comments" not in older
    assert len(paths(forge, "/reviews/2/comments")) == 1 and not paths(forge, "/reviews/1/comments")
    out, _ = read("forge_list", {"kind": "comments", "repo": DEMO, "number": 3, "page": 2}, forge_cls=ThreadForge,
                  data=data)
    assert "reviews" not in out


def test_an_issue_has_no_reviews_and_a_mention_turn_stays_in_its_repo():
    out, _ = comments(5)
    assert out["ok"] and "reviews" not in out
    out, forge = read("forge_list", {"kind": "comments", "repo": OTHER, "number": 1}, lane=mention(DEMO).lane())
    assert out["error"] == "lane_scope" and forge.calls == []
    out, _ = read("forge_list", {"kind": "comments", "repo": DEMO})
    assert out["error"] == "bad_args"
