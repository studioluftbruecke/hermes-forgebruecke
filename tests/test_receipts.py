"""Receipts: a true push, an invented SHA, a PR claim on the wrong repo, no claim at all."""
import json

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, Scripted, bot_account, make_token, repo, sample_data, sub

receipts = sub("receipts")
access_mod = sub("access")
BOT = make_token("bot")
RULES = [{"repo_glob": DEMO, "flows": ["demo"]}]


def forge_with_branch():
    data = sample_data()
    data[DEMO]["branches"]["fix-mul"] = {"name": "fix-mul", "commit": {"id": HEAD_SHA}}
    data[DEMO]["commits"] = {HEAD_SHA: {"commit": {"message": "Fix mul"}}}
    data[DEMO]["pulls"][3]["merged"] = False
    return FakeForgejo({BOT: bot_account()}, data=data)


def hook(forge, rules=RULES, enabled=True, data_dir=None):
    values = {"url": BASE, "writes": rules}
    load = lambda: access_mod.load_access(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,  # noqa: E731
                                          transport=forge)
    return receipts.make_hook(load, lambda: enabled, data_dir)


def test_true_push_gets_a_tick_with_the_sha_read_back(tmp_path):
    forge = forge_with_branch()
    reply = f"Done. I pushed `{HEAD_SHA[:7]}` to `fix-mul` in {DEMO}."
    out = hook(forge, data_dir=tmp_path)(response_text=reply, session_id="s1", turn_id="t1")
    assert out.startswith(reply)
    lines = out.split(receipts.HEADER + "\n", 1)[1].splitlines()
    assert lines == [f"✓ pushed {HEAD_SHA[:7]} to fix-mul in {DEMO}: verified at {HEAD_SHA[:12]}"]
    logged = [json.loads(x) for x in (tmp_path / "receipts.jsonl").read_text().splitlines()]
    assert logged[0]["verified"] is True and logged[0]["claim_sha"] == HEAD_SHA[:7] and logged[0]["turn_id"] == "t1"
    assert ("GET", f"/api/v1/repos/{DEMO}/branches/fix-mul") in [(m, p) for m, p, _q, _b in forge.calls]


def test_invented_sha_gets_a_cross_with_what_the_forge_says():
    forge = forge_with_branch()
    out = hook(forge)(response_text=f"I pushed 1234567abc to fix-mul on {DEMO}.")
    assert f"❌ pushed 1234567abc to fix-mul in {DEMO}: not found on Forgejo: fix-mul is at {HEAD_SHA[:12]}" in out
    out2 = hook(forge)(response_text=f"I pushed 99999999aa in {DEMO}.")
    assert f"❌ pushed 99999999aa in {DEMO}: not found on Forgejo (no such commit)" in out2


def test_pr_claim_on_the_wrong_repo():
    forge = forge_with_branch()
    # A PR that does not exist on the ruled repo is a cross ...
    out = hook(forge)(response_text=f"I opened PR #42 on {DEMO}.")
    assert f"❌ opened PR #42 in {DEMO}: not found on Forgejo (no PR #42)" in out
    # ... and a claim about a repo no rule names gets no receipt and no request at all.
    calls = len(forge.calls)
    assert hook(forge)(response_text="I opened PR #3 on acme/website.") is None
    assert len(forge.calls) == calls


def test_true_pr_and_merge_claims():
    forge = forge_with_branch()
    out = hook(forge)(response_text=f"Opened PR #3 in {DEMO}. Then I merged PR #3.")
    lines = out.split(receipts.HEADER + "\n", 1)[1].splitlines()
    assert lines[0].startswith(f"✓ opened PR #3 in {DEMO}: verified at {HEAD_SHA[:12]}")
    assert lines[1] == f"❌ merged PR #3 in {DEMO}: not merged on Forgejo (PR #3 is open)"


def test_no_claim_returns_none_and_sends_nothing():
    forge = forge_with_branch()
    h = hook(forge)
    for text in ("Here is a summary of PR #3; the checks fail.",
                 f"I will push {HEAD_SHA[:7]} to fix-mul in {DEMO} once you agree.",
                 f"I have not pushed {HEAD_SHA[:7]} to {DEMO} yet.",
                 f"You should open a PR in {DEMO}.", ""):
        assert h(response_text=text) is None, text
    assert forge.calls == []


def test_not_enabled_in_this_profile_returns_none():
    forge = forge_with_branch()
    assert hook(forge, enabled=False)(response_text=f"I pushed {HEAD_SHA[:7]} to fix-mul in {DEMO}.") is None
    assert forge.calls == []


def test_the_profile_is_checked_only_when_the_reply_makes_a_claim():
    forge, asked = forge_with_branch(), []
    values = {"url": BASE, "writes": []}
    load = lambda: access_mod.load_access(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,  # noqa: E731
                                          transport=forge)
    h = receipts.make_hook(load, lambda: asked.append(1) or True, None)
    assert h(response_text="I created a helper, merged the two loops and opened the file again.") is None
    assert asked == [] and forge.calls == []
    assert h(response_text=f"I opened PR #3 in {DEMO}.") is not None
    assert asked == [1]


def test_at_most_three_lines_and_quoted_receipts_are_not_claims():
    forge = forge_with_branch()
    reply = "\n".join(f"I opened PR #{n} in {DEMO}." for n in range(10, 16))
    reply += f"\n✓ pushed {HEAD_SHA[:7]} in {DEMO}: verified at x"
    out = hook(forge)(response_text=reply)
    lines = out.split(receipts.HEADER + "\n", 1)[1].splitlines()
    assert len(lines) == 3 and all(x.startswith("❌ opened PR #1") for x in lines)
    assert len([c for c in forge.calls if "/pulls/" in c[1]]) == 3  # claims past the third are not even read
    assert [c[1] for c in forge.calls if "/pulls/" not in c[1]] == ["/api/v1/user/repos"]  # one scope listing


def test_claims_about_a_repo_the_agent_account_cannot_see_are_not_receipted():
    """The scope is what Hermes' Forgejo account sees, not the per-repo rules."""
    forge = forge_with_branch()
    assert hook(forge, rules=[])(response_text=f"I opened PR #3 in {DEMO}.") is not None
    assert hook(forge, rules=[])(response_text="I opened PR #3 in acme/secret.") is None


def test_forge_down_says_not_verified():
    forge = Scripted(*[(503, {"message": "down"})] * 8)
    out = hook(forge)(response_text=f"I opened PR #3 in {DEMO}.")
    assert f"not verified: opened PR #3 in {DEMO}: Forgejo is unreachable or failing (503)" in out.splitlines()


def test_a_failing_forge_gets_one_request_per_read():
    forge = Scripted(*[(502, {"message": "bad gateway"})] * 40)
    out = hook(forge)(response_text=f"I opened PR #3 in {DEMO}. I merged PR #4 in {DEMO}.")
    assert out.count("not verified:") == 2
    assert [url.split("/api/v1")[1].split("?")[0] for _m, url, _h, _b in forge.calls] == [
        "/user/repos", f"/repos/{DEMO}/pulls/3", f"/repos/{DEMO}/pulls/4"]


def test_the_time_limit_covers_the_repo_listing_and_every_read():
    """Hermes abandons the hook after 30 s. Each answer here takes 7.5 s on a clock the test owns."""
    forge, now, timeouts = forge_with_branch(), [100.0], []

    def slow(method, url, headers, body, timeout):
        timeouts.append(timeout)
        now[0] += 7.5
        return forge(method, url, headers, body, timeout)

    values = {"url": BASE, "writes": []}
    access = access_mod.load_access(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                                    transport=slow)
    reply = "\n".join(f"I opened PR #{n} in {DEMO}." for n in (10, 11, 12))
    lines = [r.line() for r in receipts.build_receipts(reply, access, clock=lambda: now[0])]
    assert [path for _m, path, _q, _b in forge.calls] == [
        "/api/v1/user/repos", f"/api/v1/repos/{DEMO}/pulls/10", f"/api/v1/repos/{DEMO}/pulls/11"]
    assert timeouts == [receipts.REQUEST_TIMEOUT_S, receipts.REQUEST_TIMEOUT_S, 5.0]
    assert lines[0].startswith("❌ opened PR #10") and lines[1].startswith("❌ opened PR #11")
    assert lines[2] == f"not verified: opened PR #12 in {DEMO}: Forgejo did not answer in time"
    assert now[0] - 100.0 <= receipts.DEADLINE_S + 7.5


def test_single_repo_named_once_is_the_context_for_later_sentences():
    forge = forge_with_branch()
    out = hook(forge)(response_text=f"Working in {DEMO}.\nPushed {HEAD_SHA[:10]} to fix-mul.")
    assert f"✓ pushed {HEAD_SHA[:10]} to fix-mul in {DEMO}" in out


def test_hook_is_registered_and_declared():
    import pathlib
    plugin = sub("receipts")  # noqa: F841 (import check)
    manifest = (pathlib.Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
    assert "transform_llm_output" in manifest


def test_a_promote_into_staging_is_no_merge_claim():
    """The exact reply of a live -q turn: it reported a flow's promote
    gate, and the old parser read it as 'merged PR #3' and wrote a false cross."""
    forge = forge_with_branch()
    reply = (f"status: PR #3 in {DEMO} (flow \"demo\", head c28760253dcc, open) — review passed (2 attempts), check "
             "passed (devops/tests), promote passed (merged into staging at 2b66daea50b7), human waiting on "
             f"studioluftbruecke to merge: https://forge.example.test/{DEMO}/pulls/3")
    assert hook(forge)(response_text=reply) is None
    out = hook(forge)(response_text=f"I merged PR #3 into main in {DEMO}.")
    assert f"❌ merged PR #3 in {DEMO}: not merged on Forgejo" in out


SHORT, HEAD12, MAIN12 = HEAD_SHA[:7], HEAD_SHA[:12], "1" * 12
UNDERSCORED = "acme/my_repo"


def receipt_lines(reply, forge=None):
    out = hook(forge or forge_with_branch(), rules=[])(response_text=reply)
    return out.split(receipts.HEADER + "\n", 1)[1].splitlines() if out else None


def test_an_underscore_in_a_branch_name_is_kept():
    forge = forge_with_branch()
    forge.data[DEMO]["branches"]["fix_mul"] = {"name": "fix_mul", "commit": {"id": HEAD_SHA}}
    assert receipt_lines(f"I pushed `{SHORT}` to `fix_mul` in {DEMO}.", forge) == [
        f"✓ pushed {SHORT} to fix_mul in {DEMO}: verified at {HEAD12}"]
    assert ("GET", f"/api/v1/repos/{DEMO}/branches/fix_mul") in [(m, p) for m, p, _q, _b in forge.calls]


def test_an_underscore_in_a_repo_name_is_kept():
    forge = forge_with_branch()
    account = forge.accounts[BOT]
    account["repos"] = account["repos"] + [repo(UNDERSCORED, "write")]
    forge.data[UNDERSCORED] = {"branches": {"main": {"name": "main", "commit": {"id": HEAD_SHA}}}}
    assert receipt_lines(f"Pushed {SHORT} to main in {UNDERSCORED}.", forge) == [
        f"✓ pushed {SHORT} to main in {UNDERSCORED}: verified at {HEAD12}"]


def test_underscore_emphasis_is_still_markup():
    assert receipt_lines(f"I pushed __{SHORT}__ to _fix-mul_ in {DEMO}.") == [
        f"✓ pushed {SHORT} to fix-mul in {DEMO}: verified at {HEAD12}"]


def test_a_word_after_to_or_on_that_is_no_branch_does_not_cross_a_true_push():
    for reply in (f"Pushed {SHORT} to the remote in {DEMO}.", f"Pushed {SHORT} on 1999-12-31 in {DEMO}."):
        assert receipt_lines(reply) == [f"✓ pushed {SHORT} in {DEMO}: verified at {HEAD12}"], reply


def test_a_commit_that_exists_under_another_head_gets_a_tick_that_says_where_the_branch_is():
    assert receipt_lines(f"Pushed {SHORT} to main in {DEMO}.") == [
        f"✓ pushed {SHORT} to main in {DEMO}: verified at {HEAD12} (main is at {MAIN12})"]


def test_an_invented_sha_with_a_word_that_is_no_branch_gets_a_cross():
    assert receipt_lines(f"Pushed 1234567abc to the remote in {DEMO}.") == [
        f"❌ pushed 1234567abc in {DEMO}: not found on Forgejo (no such commit)"]


def test_merging_a_branch_for_a_pr_is_no_claim_that_the_pr_was_merged():
    forge = forge_with_branch()
    assert receipt_lines(f"Merged main into fix-mul for PR #3 in {DEMO}.", forge) is None
    assert forge.calls == []
    assert receipt_lines(f"I merged #3 in {DEMO}.") == [
        f"❌ merged PR #3 in {DEMO}: not merged on Forgejo (PR #3 is open)"]


def test_an_issue_number_is_not_read_as_the_pull_request_number():
    forge = forge_with_branch()
    assert receipt_lines(f"Created issue #1 and opened a pull request for it in {DEMO}.", forge) is None
    assert forge.calls == []
    lines = receipt_lines(f"For issue #1 I opened PR #3 in {DEMO}.")
    assert lines and lines[0].startswith(f"✓ opened PR #3 in {DEMO}")
