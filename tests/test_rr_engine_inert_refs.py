"""Issue, pull request and commit references in a review worker's text are posted inert: Forgejo neither links
them nor adds a cross-reference to the issue they name. Bidi controls are dropped. The engine's own commit ids
and a check's own description are posted as before."""
import re

from fb_fakes import DEMO, HEAD_SHA
from fb_fakes_engine import Kit
from fb_fakes_rr_engine import list_posted_comments

HOSTILE = ("closes #1, see acme/other#2 and !3, commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef, "
           "@admin [x](https://e.example) <img src=x> \u202eevil\u2066")


def posted(k) -> str:
    edits = [b["body"] for _, p, b in k.sent("", "PATCH") if "/comments/" in p]
    return "\n".join(k.comments() + edits)


def test_references_and_bidi_controls_in_a_workers_text_are_inert(tmp_path):
    k = Kit(tmp_path)
    list_posted_comments(k)
    k.tick()
    k.verdict("fail", summary=HOSTILE)
    k.tick()
    text = posted(k)
    assert "closes #" in text and "acme/other#" in text  # the words are still there to read
    for live in ("#1", "acme/other#2", "!3", "deadbeefdeadbeef", "\u202e", "\u2066"):
        assert live not in text, live
    assert not re.search(r"(?:^|[\s(\[])[0-9a-f]{7,}(?:[\s)\].,:]|$)", text.replace(HEAD_SHA[:12], ""))


def test_a_finding_is_made_inert_too(tmp_path):
    k = Kit(tmp_path)
    list_posted_comments(k)
    k.tick()
    path = k.store.verdict_path("demo", DEMO, 3, HEAD_SHA, "review")
    path.write_text('{"verdict": "fail", "head_sha": "%s", "summary": "s", "findings": '
                    '[{"file": "calc.py", "line": 3, "problem": "breaks #7 and cafebabe1234567"}]}' % HEAD_SHA)
    k.tick()
    text = posted(k)
    assert "breaks #" in text
    assert "#7" not in text and "cafebabe1234567" not in text


def test_the_engines_own_commit_ids_and_a_checks_description_read_as_before(tmp_path):
    k = Kit(tmp_path)
    list_posted_comments(k)
    combined = k.data["status"].setdefault(HEAD_SHA, {"state": "", "statuses": [], "total_count": 0})
    combined["statuses"] = [{"context": "devops/tests", "status": "failure", "description": "build #42 failed",
                             "creator": {"login": "hermes"}}]
    k.tick()
    k.verdict("pass", summary="mul is fine")
    k.tick()
    assert k.gates()["check"]["state"] == "failed"
    text = posted(k)
    assert f"`{HEAD_SHA[:12]}`" in text
    assert "build #42 failed" in text
    assert "mul is fine" in text
