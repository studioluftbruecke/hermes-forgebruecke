"""`@forgejo:` in prose: a value that is no reference attaches nothing, a failed reference one short line."""
import json

import pytest

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, sample_data, sub

reference = sub("reference")
BOT = make_token("bot")
MARKER = "was not expanded: "


def setup():
    forge = FakeForgejo({BOT: bot_account()}, data=sample_data())
    asked = []

    def get_setting(key, default=None):
        asked.append(key)
        return {"url": BASE}.get(key, default)

    return forge, asked, get_setting, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get


@pytest.mark.parametrize("raw", ["", "`", "``", "'", "  ", "this", "#12", "`owner`"])
def test_a_value_that_is_no_reference_expands_to_nothing(raw):
    forge, asked, get_setting, get_secret = setup()
    assert reference.expand(raw, get_setting, get_secret, transport=forge) is None
    assert asked == [] and forge.calls == []


@pytest.mark.parametrize("raw,why", [
    (f"{DEMO}#abc", "is not owner/repo#number"),
    (f"{DEMO}#42", "Not found"),
    ("https://elsewhere.test/a/b/issues/1", "elsewhere.test is not your Forgejo (forge.example.test)"),
    (f"{BASE}/{DEMO}/wiki/" + "long-page-name/" * 40, "is not an issue, pull request or repository address"),
])
def test_a_reference_that_fails_keeps_one_short_line(raw, why):
    forge, _asked, get_setting, get_secret = setup()
    text = reference.expand(raw, get_setting, get_secret, transport=forge)
    assert text.startswith("[Forgebrücke: @forgejo:") and text.endswith("]") and "\n" not in text
    reason = text.split(MARKER, 1)[1][:-1]
    assert len(reason) <= 140 and len(text) <= 300
    assert why in reason or reason.endswith("…")


def test_a_long_reason_keeps_its_whole_sentences_on_one_line():
    forge, _asked, get_setting, get_secret = setup()
    first = "The forge holds this issue in a state it will not show."
    long_message = first + "\nIt goes on for much longer than a reader wants. " + "More words follow here. " * 10

    def refusing(method, url, headers, body, timeout):
        if url.endswith(f"/repos/{DEMO}/issues/7"):
            return 409, {}, json.dumps({"message": long_message}).encode()
        return forge(method, url, headers, body, timeout)

    text = reference.expand(f"{DEMO}#7", get_setting, get_secret, transport=refusing)
    assert text == f"[Forgebrücke: @forgejo:{DEMO}#7 {MARKER}{first} It goes on for much longer than a reader wants. " \
                   "More words follow here.]"


def test_a_reference_still_expands():
    forge, _asked, get_setting, get_secret = setup()
    text = reference.expand(f"`{DEMO}#1`", get_setting, get_secret, transport=forge)
    assert text.startswith(f"[Forgebrücke: live state of {DEMO}#1") and "Probe thread" in text
