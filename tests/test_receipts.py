"""Receipts (M5): a true push, an invented SHA, a PR claim on the wrong repo, no claim at all."""
import json

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, Scripted, bot_account, make_token, sample_data, sub

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
    assert f"❌ pushed 1234567abc to fix-mul in {DEMO}: not found on the forge: fix-mul is at {HEAD_SHA[:12]}" in out
    out2 = hook(forge)(response_text=f"I committed 99999999aa in {DEMO}.")
    assert f"❌ pushed 99999999aa in {DEMO}: not found on the forge (no such commit)" in out2


def test_pr_claim_on_the_wrong_repo():
    forge = forge_with_branch()
    # A PR that does not exist on the ruled repo is a cross ...
    out = hook(forge)(response_text=f"I opened PR #42 on {DEMO}.")
    assert f"❌ opened PR #42 in {DEMO}: not found on the forge (no PR #42)" in out
    # ... and a claim about a repo no rule names gets no receipt and no request at all.
    calls = len(forge.calls)
    assert hook(forge)(response_text="I opened PR #3 on acme/website.") is None
    assert len(forge.calls) == calls


def test_true_pr_and_merge_claims():
    forge = forge_with_branch()
    out = hook(forge)(response_text=f"Opened PR #3 in {DEMO}. Then I merged PR #3.")
    lines = out.split(receipts.HEADER + "\n", 1)[1].splitlines()
    assert lines[0].startswith(f"✓ opened PR #3 in {DEMO}: verified at {HEAD_SHA[:12]}")
    assert lines[1] == f"❌ merged PR #3 in {DEMO}: not merged on the forge (PR #3 is open)"


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
    """0.0.14: the scope is what Hermes' Forgejo account sees, not the per-repo rules."""
    forge = forge_with_branch()
    assert hook(forge, rules=[])(response_text=f"I opened PR #3 in {DEMO}.") is not None
    assert hook(forge, rules=[])(response_text="I opened PR #3 in acme/secret.") is None


def test_forge_down_says_not_verified():
    forge = Scripted(*[(503, {"message": "down"})] * 8)
    out = hook(forge)(response_text=f"I opened PR #3 in {DEMO}.")
    assert f"not verified: opened PR #3 in {DEMO}: the forge did not answer" in out


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
    """The exact reply of a live -q turn (runs/m6.jsonl, receipts-false-cross): it reported a flow's promote
    gate, and the old parser read it as 'merged PR #3' and wrote a false cross."""
    forge = forge_with_branch()
    reply = (f"status: PR #3 in {DEMO} (flow \"demo\", head c28760253dcc, open) — review passed (2 attempts), check "
             "passed (devops/tests), promote passed (merged into staging at 2b66daea50b7), human waiting on "
             f"studioluftbruecke to merge: https://forge.example.test/{DEMO}/pulls/3")
    assert hook(forge)(response_text=reply) is None
    out = hook(forge)(response_text=f"I merged PR #3 into main in {DEMO}.")
    assert f"❌ merged PR #3 in {DEMO}: not merged on the forge" in out
