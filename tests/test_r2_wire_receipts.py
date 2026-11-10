"""Receipts cross what the reply claims, and only that."""
import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, make_token, sample_data, sub

receipts = sub("receipts")
access_mod = sub("access")
BOT = make_token("bot")
SHORT = HEAD_SHA[:7]
PULL_3 = f"{BASE}/{DEMO}/pulls/3"


def forge_with_branch(*, merged=False):
    data = sample_data()
    data[DEMO]["branches"]["fix-mul"] = {"name": "fix-mul", "commit": {"id": HEAD_SHA}}
    data[DEMO]["commits"] = {HEAD_SHA: {"commit": {"message": "Fix mul"}}}
    data[DEMO]["pulls"][3].update(merged=merged, merge_commit_sha="e" * 40 if merged else None)
    return FakeForgejo({BOT: bot_account()}, data=data)


def receipt_lines(reply, forge=None):
    forge = forge or forge_with_branch()
    values = {"url": BASE}

    def load():
        return access_mod.load_access(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                                      transport=forge)

    out = receipts.make_hook(load, lambda: True)(response_text=reply, session_id="s", turn_id="t")
    return out.split(receipts.HEADER + "\n", 1)[1].splitlines() if out else []


MERGED_REPLIES = [
    f"PR #3 in {DEMO} was merged.",
    f"PR #3 in {DEMO} is merged now.",
    f"Pull request #3 in {DEMO} has been merged.",
    f"PR #3 of {DEMO} got merged.",
    f"PR #3 in {DEMO} is now merged.",
    f"Merged {PULL_3} just now.",
    f"Merged the pull request {PULL_3}.",
    f"I merged [PR #3]({PULL_3}).",
    f"[PR #3]({PULL_3}) was merged.",
    f"{PULL_3} was merged.",
]


@pytest.mark.parametrize("reply", MERGED_REPLIES)
def test_a_false_was_merged_gets_a_cross(reply):
    lines = receipt_lines(reply)
    assert len(lines) == 1 and lines[0].startswith(f"❌ merged PR #3 in {DEMO}:"), lines
    assert "PR #3 is open" in lines[0]


@pytest.mark.parametrize("reply", MERGED_REPLIES)
def test_a_true_was_merged_gets_a_tick(reply):
    lines = receipt_lines(reply, forge_with_branch(merged=True))
    assert lines == [f"✓ merged PR #3 in {DEMO}: verified at {'e' * 12}"]


@pytest.mark.parametrize("reply", [
    f"PR #3 in {DEMO} will be merged after your review.",
    f"PR #3 in {DEMO} is not merged yet.",
    f"PR #3 in {DEMO} can be merged now.",
    f"PR #3 in {DEMO} is ready to be merged.",
    f"When PR #3 in {DEMO} is merged, the release follows.",
    f"Tell me whether PR #3 in {DEMO} was merged.",
    f"Was PR #3 in {DEMO} merged?",
    f"The branch for PR #3 in {DEMO} was merged into staging.",
    f"I merged main into fix-mul for {PULL_3}.",
    f"Review of {PULL_3}: the base was merged into the head twice.",
])
def test_a_sentence_that_does_not_say_the_pull_request_was_merged_gets_no_receipt(reply):
    forge = forge_with_branch()
    assert receipt_lines(reply, forge) == []
    assert forge.calls == []


@pytest.mark.parametrize("reply", [
    f"Committed 9f8e7d6 locally in {DEMO}.",
    f"I committed the fix as 9f8e7d6 in my worktree of {DEMO} and will push after your review.",
    f"Committed {SHORT} on fix-mul in {DEMO}.",
])
def test_a_commit_is_no_claim_that_it_was_pushed(reply):
    forge = forge_with_branch()
    assert receipt_lines(reply, forge) == []
    assert forge.calls == []


def test_committed_and_pushed_is_a_push_claim():
    assert receipt_lines(f"Committed and pushed {SHORT} to fix-mul in {DEMO}.") == [
        f"✓ pushed {SHORT} to fix-mul in {DEMO}: verified at {HEAD_SHA[:12]}"]
    lines = receipt_lines(f"Committed the fix and pushed 9f8e7d6 to fix-mul in {DEMO}.")
    assert len(lines) == 1 and lines[0].startswith(f"❌ pushed 9f8e7d6 to fix-mul in {DEMO}:")


@pytest.mark.parametrize("reply", [
    f"Pushed 3 commits (1048576 bytes) to fix-mul in {DEMO}.",
    f"Pushed 1048576 bytes to fix-mul in {DEMO}.",
    f"Pushed 12 files, 2048000 bytes, to fix-mul in {DEMO}.",
    f"Pushed the notes for 20261110 to fix-mul in {DEMO}.",
    f"Pushed the fix for issue #1234567 to fix-mul in {DEMO}.",
])
def test_a_number_that_cannot_be_a_commit_is_no_push_claim(reply):
    forge = forge_with_branch()
    assert receipt_lines(reply, forge) == []
    assert forge.calls == []


def test_a_number_beside_the_commit_does_not_hide_the_commit():
    assert receipt_lines(f"Pushed 3 commits (1048576 bytes), head {SHORT}, to fix-mul in {DEMO}.") == [
        f"✓ pushed {SHORT} to fix-mul in {DEMO}: verified at {HEAD_SHA[:12]}"]


DIGITS = "1234567" + "0" * 33


@pytest.mark.parametrize("reply", [
    f"Pushed 1234567 to digits in {DEMO}.",
    f"Pushed commit 1234567 to digits in {DEMO}.",
    f"I force-pushed 1234567, and the checks run in {DEMO}.",
])
def test_a_commit_of_digits_only_is_still_a_push_claim(reply):
    forge = forge_with_branch()
    forge.data[DEMO]["branches"]["digits"] = {"name": "digits", "commit": {"id": DIGITS}}
    forge.data[DEMO]["commits"][DIGITS] = {"commit": {"message": "All digits"}}
    lines = receipt_lines(reply, forge)
    assert len(lines) == 1 and lines[0].startswith("✓ pushed 1234567") and lines[0].endswith(DIGITS[:12]), lines
    invented = receipt_lines(reply.replace("1234567", "7654321"), forge)
    assert len(invented) == 1 and invented[0].startswith("❌ pushed 7654321"), invented


def test_a_file_path_in_the_reply_does_not_hide_a_push():
    forge = forge_with_branch()
    assert receipt_lines(f"Pushed {SHORT} to fix-mul. The repo is {DEMO}. The fix is in src/calc.py.", forge) == [
        f"✓ pushed {SHORT} to fix-mul in {DEMO}: verified at {HEAD_SHA[:12]}"]
    lines = receipt_lines(f"Pushed 9f8e7d6 to fix-mul. The repo is {DEMO}. See src/calc.py and docs/api.md.", forge)
    assert len(lines) == 1 and lines[0].startswith(f"❌ pushed 9f8e7d6 to fix-mul in {DEMO}:")
    merged = receipt_lines(f"PR #3 was merged. It lives in {DEMO} and touches src/calc.py.", forge)
    assert len(merged) == 1 and merged[0].startswith(f"❌ merged PR #3 in {DEMO}:")


def test_a_push_with_no_repo_the_account_sees_gets_no_receipt():
    assert receipt_lines(f"Pushed {SHORT} to fix-mul. The fix is in src/calc.py.") == []
    assert receipt_lines(f"Pushed {SHORT} to fix-mul.") == []


def test_a_reply_that_names_repos_and_claims_nothing_sends_no_request():
    forge = forge_with_branch()
    assert receipt_lines(f"The repo is {DEMO}. The fix is in src/calc.py. I pushed nothing so far.", forge) == []
    assert forge.calls == []
