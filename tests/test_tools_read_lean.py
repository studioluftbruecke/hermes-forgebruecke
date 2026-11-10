"""Read results carry what differs from the usual, not every field: no nulls, no empty lists, no echoes."""
import json

from fb_fakes import BASE, DEMO, HEAD_SHA, sample_data
from fb_fakes_tools_read import kit, plugin, read


def twenty_one_open_pulls():
    data = sample_data()
    for i in range(10, 30):
        data[DEMO]["issues"][i] = {**data[DEMO]["issues"][3], "number": i,
                                   "title": f"Tighten the retry budget of the sync worker ({i})",
                                   "html_url": f"{BASE}/{DEMO}/pulls/{i}"}
    return data


def holes(value, path="result"):
    """Paths of every null, empty string, empty list and empty dict inside ``value``."""
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in holes(v, f"{path}.{k}")]
    if isinstance(value, list) and value:
        return [p for i, v in enumerate(value) for p in holes(v, f"{path}[{i}]")]
    return [path] if value is None or value == "" or value == [] or value == {} else []


def test_a_page_of_twenty_pull_requests_stays_under_6000_characters():
    forge, gs, sec = kit(data=twenty_one_open_pulls())
    text = plugin.make_read_handler("forge_list", gs, sec, transport=forge)({"kind": "prs", "repo": DEMO})
    out = json.loads(text)
    assert len(out["items"]) == 20 and out["total"] == 21 and out["has_more"] is True and out["next_page"] == 2
    assert len(text) < 6000, len(text)
    assert out["repo"] == DEMO and out["state"] == "open"
    assert all(not {"repo", "state", "is_pr"} & set(row) for row in out["items"])
    assert holes(out) == []
    row = out["items"][0]
    assert row["number"] and row["title"] and row["url"] and row["author"] and row["labels"] == ["bug"]


def test_rows_from_a_search_across_repos_keep_their_repo():
    out, _ = read("forge_list", {"kind": "prs"}, data=twenty_one_open_pulls())
    assert len(out["items"]) == 20 and all(row["repo"] == DEMO for row in out["items"]) and "repo" not in out
    assert holes(out) == []
    out, _ = read("forge_list", {"kind": "prs", "repo": DEMO, "state": "all"}, data=twenty_one_open_pulls())
    assert "state" not in out and all(row["state"] == "open" for row in out["items"])


def test_an_empty_page_still_says_so():
    out, _ = read("forge_list", {"kind": "issues", "repo": DEMO, "labels": ["docs"]})
    assert out["ok"] and out["items"] == [] and out["total"] == 0 and out["has_more"] is False


def test_a_pull_request_carries_its_last_verdict_once_and_no_review_request_rows():
    out, _ = read("forge_get", {"kind": "pr", "repo": DEMO, "number": 3})
    pr = out["pr"]
    assert holes({k: v for k, v in out.items()}) == []
    assert "is_pr" not in pr and "files_more" not in pr and "draft" not in pr
    reviews = pr["reviews"]
    assert reviews["last_verdict"]["state"] == "REQUEST_CHANGES" and "stale" not in reviews["last_verdict"]
    assert [r["state"] for r in reviews["items"]] == ["COMMENT"] and reviews["total"] == 3
    assert json.dumps(out).count("use *") == 1


def test_what_differs_from_the_usual_is_kept():
    data = sample_data()
    data[DEMO]["pulls"][3].update(draft=True, mergeable=False)
    data[DEMO]["reviews"][3][1]["stale"] = True
    data[DEMO]["status"][HEAD_SHA]["statuses"][0]["target_url"] = "https://ci.example.test/1"
    pr, _ = read("forge_get", {"kind": "pr", "repo": DEMO, "number": 3}, data=data)
    pr = pr["pr"]
    assert pr["draft"] is True and pr["mergeable"] is False and pr["reviews"]["last_verdict"]["stale"] is True
    assert pr["checks"]["statuses"][0]["target_url"] == "https://ci.example.test/1"
    branch, _ = read("forge_get", {"kind": "branch", "repo": DEMO, "ref": "main"})
    assert branch["branch"]["protected"] is False and branch["branch"]["required_approvals"] == 0
