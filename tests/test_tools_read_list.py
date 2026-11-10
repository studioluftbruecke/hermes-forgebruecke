"""forgejo_list: whose account the "my" flags mean, and what a row carries."""
import pytest

from fb_fakes import DEMO, HEAD_SHA, sample_data
from fb_fakes_tools_read import read


@pytest.mark.parametrize("flag,arg", [("assigned", "assignee"), ("created", "author"), ("mentioned_me", "mentioned")])
def test_a_my_flag_names_the_account_it_means_and_the_way_to_ask_for_the_user(flag, arg):
    out, forge = read("forge_list", {"kind": "issues", flag: True}, settings={"you": "acme"})
    assert out["ok"] and out["relative_to"] == "forgebruecke-bot"
    assert out["hint"] == f"For acme, pass repo and {arg}=acme."


def test_review_requested_names_the_account_too():
    out, _ = read("forge_list", {"kind": "prs", "review_requested": True}, settings={"you": "acme"})
    assert out["relative_to"] == "forgebruecke-bot" and "acme" in out["hint"]


def test_no_hint_when_you_is_unset_or_is_the_same_account():
    out, _ = read("forge_list", {"kind": "issues", "assigned": True})
    assert out["relative_to"] == "forgebruecke-bot" and "hint" not in out
    out, _ = read("forge_list", {"kind": "issues", "assigned": True}, settings={"you": "Forgebruecke-Bot"})
    assert "hint" not in out


def test_a_list_without_a_my_flag_names_no_account():
    out, forge = read("forge_list", {"kind": "issues"})
    assert out["ok"] and "relative_to" not in out
    assert not [c for c in forge.calls if c[1].endswith("/user")]


def closed_pulls():
    data = sample_data()
    merged = {**data[DEMO]["issues"][3], "state": "closed",
              "pull_request": {"merged": True, "merged_at": "2026-01-09T09:00:00Z"}}
    dropped = {**merged, "number": 4, "pull_request": {"merged": False, "merged_at": None}}
    still_open = {**merged, "number": 5, "state": "open", "pull_request": {"merged": False}}
    data[DEMO]["issues"].update({3: merged, 4: dropped, 5: still_open})
    return data


def test_a_closed_pull_request_says_whether_it_was_merged():
    out, _ = read("forge_list", {"kind": "prs", "repo": DEMO, "state": "closed"}, data=closed_pulls())
    rows = {r["number"]: r for r in out["items"]}
    assert rows[3]["merged"] is True and rows[3]["merged_at"] == "2026-01-09T09:00:00Z"
    assert rows[4]["merged"] is False
    out, _ = read("forge_list", {"kind": "prs", "repo": DEMO}, data=closed_pulls())
    assert [r["number"] for r in out["items"]] == [5] and "merged" not in out["items"][0]


@pytest.mark.parametrize("scope,path", [({"repo": DEMO}, f"/repos/{DEMO}/issues"), ({}, "/repos/issues/search")])
def test_since_and_sort_reach_the_forge(scope, path):
    out, forge = read("forge_list", {"kind": "prs", "state": "closed", "since": "2026-01-03", "sort": "recentupdate",
                                     **scope}, data=closed_pulls())
    assert out["ok"]
    query = next(c[2] for c in forge.calls if c[1].endswith(path))
    assert query["since"] == "2026-01-03T00:00:00Z" and query["sort"] == "recentupdate"
    out, forge = read("forge_list", {"kind": "prs", "since": "2026-01-03T10:30:00+02:00", **scope})
    assert next(c[2] for c in forge.calls if c[1].endswith(path))["since"] == "2026-01-03T10:30:00+02:00"


@pytest.mark.parametrize("args", [{"since": "last week"}, {"since": "2026-13-40"}, {"sort": "newest"}])
def test_a_since_or_sort_the_forge_would_not_understand_is_refused(args):
    out, forge = read("forge_list", {"kind": "prs", "repo": DEMO, **args})
    assert out["error"] == "bad_args" and next(iter(args)) in out["message"]
    assert not [c for c in forge.calls if c[1].endswith("/issues")]


def test_an_access_level_the_forge_does_not_have_is_refused():
    out, forge = read("forge_list", {"kind": "repos", "access": "owner"})
    assert out["error"] == "bad_args" and "read, write or admin" in out["message"]
    assert not [c for c in forge.calls if c[1].endswith("/user/repos")]


def test_the_repo_list_ends_where_the_read_ends():
    """The list reads 500 repos at most; beyond them a page is empty and must not promise another."""
    out, _ = read("forge_list", {"kind": "repos", "page": 25}, extra_repos=520)
    assert len(out["items"]) == 20 and out["has_more"] is False and "next_page" not in out
    assert "500" in out["cut"]
    out, _ = read("forge_list", {"kind": "repos", "page": 26}, extra_repos=520)
    assert out["items"] == [] and out["has_more"] is False and "next_page" not in out
    out, _ = read("forge_list", {"kind": "repos"}, extra_repos=30)
    assert out["total"] == 32 and out["has_more"] is True and "cut" not in out


def test_a_review_names_the_whole_commit_it_was_written_on():
    out, _ = read("forge_list", {"kind": "reviews", "repo": DEMO, "number": 3})
    assert {r["commit"] for r in out["items"]} == {HEAD_SHA}
    out, _ = read("forge_get", {"kind": "pr", "repo": DEMO, "number": 3})
    assert out["pr"]["reviews"]["last_verdict"]["commit"] == HEAD_SHA
