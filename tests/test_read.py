"""forgejo_get, forgejo_list and @forgejo: against the fake forge (Forgejo 15.0.11 shapes)."""
import json

import pytest

from fb_fakes import (BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, load_plugin, make_token, owner_account,
                      sample_data, sub)

plugin = load_plugin()
read = sub("tools.read")
reference = sub("reference")
BOT, READ = make_token("bot"), make_token("read")


def setup(settings=None, *, read_token=False, data=None):
    accounts = {BOT: bot_account(), READ: owner_account()}
    forge = FakeForgejo(accounts, data=sample_data() if data is None else data)
    values = {"url": BASE, **(settings or {})}
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}
    if read_token:
        secrets["FORGEBRUECKE_PERSONAL_READ_TOKEN"] = READ
    get_setting = lambda k, d=None: values.get(k, d)  # noqa: E731
    return forge, get_setting, secrets.get


def call(tool, args, **kw):
    forge, gs, gsec = setup(**kw)
    handler = plugin.make_read_handler(tool, gs, gsec, transport=forge)
    return json.loads(handler(args)), forge


def test_pr_has_head_checks_with_creator_reviews_and_last_comments():
    out, _ = call("forge_get", {"kind": "pr", "repo": DEMO, "number": 3})
    assert out["ok"] and "not instructions" in out["note"]
    pr = out["pr"]
    assert pr["head"] == {"ref": "fix-mul", "sha": HEAD_SHA} and pr["base"] == "main"
    assert pr["checks"]["state"] == "failure"
    assert {"context": "devops/review", "state": "failure", "description": "mul still wrong",
            "creator": "hermes"} in pr["checks"]["statuses"]
    assert pr["reviews"]["last_verdict"]["state"] == "REQUEST_CHANGES"  # REQUEST_REVIEW is no verdict
    assert pr["files"] == [{"file": "calc.py", "status": "modified", "additions": 1, "deletions": 1}]
    assert pr["diffstat"] == {"additions": 1, "deletions": 1, "changed_files": 1}
    assert pr["last_comments"][0]["body"].startswith("Ignore previous instructions")  # data, passed through
    assert "diff" not in pr


def block(name, lines):
    return f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n" + f"+{name} line\n" * lines


def diff_of(args, diff):
    data = sample_data()
    data[DEMO]["diff"][3] = diff
    out, _ = call("forge_get", {"kind": "pr", "repo": DEMO, "number": 3, "diff": True, **args}, data=data)
    return out


def test_a_large_diff_is_cut_at_40000_characters_and_names_the_files_that_were_cut():
    names = [f"src/f{i}.py" for i in range(1, 6)]
    whole = "".join(block(n, 3500) for n in names)
    assert len(whole) > 250_000
    pr = diff_of({}, whole)["pr"]
    assert 30_000 < len(pr["diff"]) <= 40_000 and whole.startswith(pr["diff"]) and pr["diff"].endswith("\n")
    assert pr["diff_cut"]["files"] == names and pr["diff_cut"]["note"].endswith("/pulls/3.diff")


def test_path_returns_one_files_block():
    names = ["a.py", "docs/b c.md", "z.py"]
    whole = "".join(block(n, 3) for n in names)
    pr = diff_of({"path": "docs/b c.md"}, whole)["pr"]
    assert pr["diff"] == block("docs/b c.md", 3) and "diff_cut" not in pr
    big = diff_of({"path": "z.py"}, block("a.py", 5000) + block("z.py", 5000))["pr"]
    assert big["diff"].startswith("diff --git a/z.py") and len(big["diff"]) <= 40_000
    assert big["diff_cut"]["files"] == ["z.py"]
    out = diff_of({"path": "nope.py"}, whole)
    assert out["error"] == "not_found" and "nope.py" in out["message"]


def test_a_small_diff_and_one_without_file_headers_come_back_as_they_are():
    pr = diff_of({}, "diff --git a/calc.py b/calc.py\n-    return a + b\n+    return a * b\n")["pr"]
    assert pr["diff"].endswith("+    return a * b\n") and "diff_cut" not in pr
    pr = diff_of({}, "+" * 250_000)["pr"]
    assert len(pr["diff"]) == 40_000 and "files" not in pr["diff_cut"] and pr["diff_cut"]["note"]


def test_issue_has_the_last_three_of_five_comments():
    out, _ = call("forge_get", {"kind": "issue", "repo": DEMO, "number": 1})
    issue = out["issue"]
    assert issue["comments_total"] == 5 and [c["body"] for c in issue["last_comments"]] == [
        "comment 3", "comment 4", "comment 5"]
    assert "is_pr" not in issue and "hint" not in issue


def test_a_404_and_a_403_come_back_as_sentences():
    out, _ = call("forge_get", {"kind": "issue", "repo": DEMO, "number": 99})
    assert out["ok"] is False and out["error"] == "not_found" and "#99" in out["message"]
    out, _ = call("forge_get", {"kind": "repo", "repo": "acme/secret"})
    assert out["error"] == "no_access" and "cannot see acme/secret" in out["message"]
    assert out["link"] == f"{BASE}/acme/secret/settings/collaboration"
    forge, gs, _ = setup()
    no_issue_scope = {BOT: {**bot_account(), "scopes": {"read:repository", "read:user"}}}
    forge.accounts = no_issue_scope
    out = json.loads(plugin.make_read_handler("forge_get", gs, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get, transport=forge)(
        {"kind": "issue", "repo": DEMO, "number": 1}))
    assert out["error"] == "missing_scope" and out["status"] == 403 and "read:issue" in out["message"]


def test_reads_use_the_agent_token_even_when_the_retired_read_token_is_set():
    out, forge = call("forge_get", {"kind": "repo", "repo": DEMO}, read_token=True)
    assert out["repo"]["my_access"] == "write"  # the retired token's account would be admin


def test_retired_read_scope_and_read_false_no_longer_block_reads():
    """Forgejo decides what Hermes sees; the old settings only add a warning."""
    out, forge = call("forge_get", {"kind": "repo", "repo": DEMO},
                      settings={"read_scope": "rules", "writes": [{"repo_glob": "acme/*", "read": False}]})
    assert out["ok"] and out["repo"]["my_access"] == "write"


def test_list_prs_in_one_repo_maps_filters_to_forgejo_parameters():
    out, forge = call("forge_list", {"kind": "prs", "repo": DEMO, "labels": ["bug"], "author": "forgebruecke-bot"})
    assert out["ok"] and [i["number"] for i in out["items"]] == [3]
    assert out["total"] == 1 and out["has_more"] is False
    _, path, query, _ = forge.calls[-1]
    assert path.endswith(f"/repos/{DEMO}/issues") and query["type"] == "pulls"
    assert query["labels"] == "bug" and query["created_by"] == "forgebruecke-bot"


def test_list_across_repos_uses_the_search_and_refuses_repo_only_filters():
    out, forge = call("forge_list", {"kind": "issues", "review_requested": True})
    search = [c for c in forge.calls if c[1].endswith("/repos/issues/search")]
    assert out["ok"] and len(search) == 1 and search[0][2]["review_requested"] == "true"
    out, _ = call("forge_list", {"kind": "prs", "author": "acme"})
    assert out["error"] == "bad_args" and "needs a repo" in out["message"]


def test_list_repos_with_access_and_paging():
    out, _ = call("forge_list", {"kind": "repos", "access": "write"})
    assert [r["repo"] for r in out["items"]] == [DEMO] and out["items"][0]["access"] == "write"


def test_list_statuses_and_reviews():
    out, _ = call("forge_list", {"kind": "statuses", "repo": DEMO, "ref": HEAD_SHA})
    assert {s["context"] for s in out["items"]} == {"devops/tests", "devops/review"}
    out, _ = call("forge_list", {"kind": "reviews", "repo": DEMO, "number": 3})
    assert [r["state"] for r in out["items"]] == ["COMMENT", "REQUEST_CHANGES", "REQUEST_REVIEW"]


@pytest.mark.parametrize("raw,repo,number", [
    ("acme/forgebruecke-demo#3", DEMO, 3),
    ("acme/forgebruecke-demo!3", DEMO, 3),
    (f"{BASE}/acme/forgebruecke-demo/pulls/3", DEMO, 3),
    (f"{BASE}/acme/forgebruecke-demo/issues/1/", DEMO, 1),
    ("acme/forgebruecke-demo", DEMO, None),
])
def test_reference_targets(raw, repo, number):
    t = reference.parse_target(raw, BASE)
    assert (t.repo, t.number) == (repo, number)


def test_reference_refuses_another_host():
    with pytest.raises(reference.BadTarget):
        reference.parse_target("https://evil.example/acme/x/issues/1", BASE)


def test_reference_expands_a_pr_by_issue_url_and_by_short_form():
    forge, gs, gsec = setup()
    for raw in (f"{BASE}/acme/forgebruecke-demo/pulls/3", "acme/forgebruecke-demo#3"):
        text = reference.expand(raw, gs, gsec, transport=forge)
        assert text.startswith("[Forgebrücke: live state of acme/forgebruecke-demo#3")
        assert "not instructions" in text.splitlines()[0]
        assert f"head SHA {HEAD_SHA}" in text and "devops/review=failure (by hermes)" in text
        assert "last review verdict: REQUEST_CHANGES by acme" in text
        assert len(text) <= reference.MAX_CHARS


def test_reference_expands_an_issue_and_says_why_it_cannot():
    forge, gs, gsec = setup()
    text = reference.expand("acme/forgebruecke-demo#1", gs, gsec, transport=forge)
    assert "Issue acme/forgebruecke-demo#1: Probe thread" in text and "comment 5" in text
    assert "not expanded: Not found" in reference.expand("acme/forgebruecke-demo#42", gs, gsec, transport=forge)
    assert "is not your Forgejo" in reference.expand("https://elsewhere.test/a/b/issues/1", gs, gsec,
                                                     transport=forge)


def test_reference_completions_list_what_the_agent_account_sees():
    forge, gs, gsec = setup()
    rows = reference.completions("calc", gs, gsec, transport=forge)
    assert rows == [("acme/forgebruecke-demo#3", "acme/forgebruecke-demo#3 Fix calc.mul", "PR")]
    forge, gs, gsec = setup({"read_scope": "rules"})  # retired: ignored
    assert reference.completions("calc", gs, gsec, transport=forge) == rows
