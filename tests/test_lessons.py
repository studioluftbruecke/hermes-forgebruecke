"""M10: lesson cards in learn mode, riding on the receipts hook."""
import threading

import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, make_token, sample_data, sub

receipts = sub("receipts")
lessons = sub("lessons")
access_mod = sub("access")
BOT = make_token("bot")


def hook(tmp_path, *, learn=True, lane="agent", user="local:owner", forge=None):
    f = forge or FakeForgejo({BOT: bot_account()}, data=sample_data())
    values = {"url": BASE, "learn_mode": learn, "writes": [{"repo_glob": DEMO}]}
    load = lambda: access_mod.load_access(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,  # noqa
                                          transport=f, lane=lane, scope_repo=DEMO if lane == "mention" else "")
    return receipts.make_hook(load, lambda: True, tmp_path, user_key=lambda: user)


def test_a_failed_check_answer_gets_the_lesson_once(tmp_path):
    h = hook(tmp_path)
    text = "The check devops/tests failed on the last commit."
    out = h(response_text=text)
    assert out.startswith(text) and "**Learn: a failed check.**" in out
    assert h(response_text=text) is None  # never twice for the same user
    other = hook(tmp_path, user="local:someone")
    assert "Learn: a failed check" in other(response_text=text)


def test_a_verified_pr_claim_gets_the_receipt_then_the_first_pr_lesson(tmp_path):
    out = hook(tmp_path)(response_text=f"I opened PR #3 in {DEMO}.")
    assert out.index("Receipts, re-read from Forgejo:") < out.index("**Learn: your first pull request.**")
    assert "✓ opened PR #3" in out


def test_one_card_per_answer_and_none_when_learn_mode_is_off_or_in_a_mention(tmp_path):
    text = f"I opened PR #3 in {DEMO}. The tests failed."
    out = hook(tmp_path)(response_text=text)
    assert out.count("**Learn:") == 1
    assert hook(tmp_path / "off", learn=False)(response_text="The build is red: the check failed.") is None
    assert hook(tmp_path / "m", lane="mention")(response_text="The check failed.") is None


def test_an_unverified_claim_triggers_no_lesson(tmp_path):
    out = hook(tmp_path)(response_text=f"I merged PR #3 into main in {DEMO}.")  # the fake says: still open
    assert "❌ merged PR #3" in out and "Learn:" not in out


@pytest.mark.parametrize("text,fires", [("The tests are failing on main.", True), ("CI failed.", True),
                                        ("The checks passed.", False), ("I fixed the failing import.", False),
                                        ("The review gate on PR #7 failed (three attempts).", True)])
def test_failed_check_trigger(text, fires):
    assert ("first_failed_check" in lessons.triggers(text, [])) is fires


def together(*calls):
    """Run the calls on threads that start at the same moment; return what each returned or raised."""
    barrier, results = threading.Barrier(len(calls)), [None] * len(calls)

    def run(i, call):
        barrier.wait()
        try:
            results[i] = call()
        except Exception as e:
            results[i] = e
    threads = [threading.Thread(target=run, args=(i, call)) for i, call in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_two_answers_that_finish_together_both_keep_their_mark(tmp_path):
    for i in range(100):
        folder = tmp_path / str(i)
        results = together(*(lambda user=user: lessons.LessonStore(folder).mark(user, "first_pr")
                             for user in ("telegram:1", "telegram:2")))
        assert not [r for r in results if isinstance(r, Exception)]
        assert lessons.LessonStore(folder).shown() == {"telegram:1": ["first_pr"], "telegram:2": ["first_pr"]}


def test_two_answers_for_one_user_show_the_card_once(tmp_path):
    for i in range(50):
        store = lessons.LessonStore(tmp_path / str(i))
        cards = together(*(lambda: lessons.pick("the tests failed", [], store, "local:owner") for _ in range(2)))
        assert len([c for c in cards if c]) == 1 and None in cards
