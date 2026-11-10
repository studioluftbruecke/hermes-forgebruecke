"""forgejo_get kind=pr and kind=issue: reviews beyond the first page, the real last comments."""
from fb_fakes import DEMO, HEAD_SHA, sample_data
from fb_fakes_tools_read import PagedReviews, paths, read


def review(i, state, login="acme", **extra):
    return {"id": i, "user": {"login": login}, "state": state, "body": f"review {i}", "commit_id": HEAD_SHA,
            "submitted_at": f"2026-01-01T10:{i:02d}:00Z", **extra}


def test_the_last_verdict_and_the_total_count_every_page_of_reviews():
    data = sample_data()
    data[DEMO]["reviews"][3] = ([review(0, "REQUEST_CHANGES")] + [review(i, "COMMENT", "hermes") for i in range(1, 35)]
                                + [review(35, "APPROVED")])
    out, _ = read("forge_get", {"kind": "pr", "repo": DEMO, "number": 3}, forge_cls=PagedReviews, data=data)
    reviews = out["pr"]["reviews"]
    assert reviews["last_verdict"]["state"] == "APPROVED" and reviews["total"] == 36
    assert reviews["last_verdict"]["body"] == "review 35"
    assert [r["body"] for r in reviews["items"]] == [f"review {i}" for i in (31, 32, 33, 34)]


def test_a_dismissed_review_is_no_verdict():
    data = sample_data()
    data[DEMO]["reviews"][3] = [review(0, "REQUEST_CHANGES"), review(1, "APPROVED", dismissed=True)]
    out, _ = read("forge_get", {"kind": "pr", "repo": DEMO, "number": 3}, data=data)
    reviews = out["pr"]["reviews"]
    assert reviews["last_verdict"]["state"] == "REQUEST_CHANGES" and reviews["last_verdict"]["body"] == "review 0"
    assert "items" not in reviews and "review 1" not in str(reviews)


def thread(n):
    return [{"id": i, "user": {"login": "acme"}, "body": f"comment {i}", "created_at": "2026-01-01T11:00:00Z"}
            for i in range(1, n + 1)]


def test_the_last_comments_of_a_long_thread_are_its_last_and_cost_two_requests():
    data = sample_data()
    data[DEMO]["comments"][1] = thread(520)
    out, forge = read("forge_get", {"kind": "issue", "repo": DEMO, "number": 1}, data=data)
    issue = out["issue"]
    assert issue["comments_total"] == 520
    assert [c["body"] for c in issue["last_comments"]] == ["comment 518", "comment 519", "comment 520"]
    assert len(paths(forge, "/issues/1/comments")) <= 2


def test_short_threads_keep_their_last_three_in_order():
    for n, want in ((0, []), (1, ["comment 1"]), (2, ["comment 1", "comment 2"]),
                    (4, ["comment 2", "comment 3", "comment 4"]), (7, ["comment 5", "comment 6", "comment 7"])):
        data = sample_data()
        data[DEMO]["comments"][1] = thread(n)
        out, _ = read("forge_get", {"kind": "issue", "repo": DEMO, "number": 1}, data=data)
        assert [c["body"] for c in out["issue"].get("last_comments") or []] == want, n
        assert (out["issue"].get("comments_total") or 0) == n
