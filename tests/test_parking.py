"""The parking store and the wake rule."""
import json
import os
import time

import pytest

from fb_fakes import sub

parking = sub("parking")
desk = sub("desk")
SHA_A, SHA_B = "a" * 40, "b" * 40
FORGE = "https://forge.example.test"
REPO = "Acme/Demo"
STATUS = "x\n\n<sub>Kept up to date by Forgebrücke (Hermes). The merge into the target branch is yours.</sub>"
TOOL = "Done.\n\n<sub>Posted by Hermes through Forgebrücke</sub>"


def log_lines(tmp_path):
    path = tmp_path / "desk-actions.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def seen(sha=SHA_A, comment_at=None, draft=False):
    return {"head_sha": sha, "comment_at": comment_at, "draft": draft}


# The store.

def test_park_repark_and_unpark(tmp_path):
    entry, note = parking.park(tmp_path, REPO, 9, note="after staging", seen=seen(), now=lambda: 100)
    assert note == {} and entry == {"repo": REPO, "number": 9, "note": "after staging", "parked_at": 100,
                                    "wake_commits": True, "seen": seen()}
    entries, bad = parking.load(tmp_path)
    assert not bad and list(entries) == ["acme/demo#9"]
    parking.park(tmp_path, REPO, 9, note="later", wake_commits=False, seen=seen(SHA_B, "2026-11-08T12:00:00Z", True),
                 now=lambda: 200)
    again = parking.load(tmp_path)[0]["acme/demo#9"]
    assert again["note"] == "later" and again["parked_at"] == 200 and not again["wake_commits"]
    assert again["seen"] == seen(SHA_B, "2026-11-08T12:00:00Z", True)
    assert parking.unpark(tmp_path, "acme/DEMO", 9) == (True, {})
    assert parking.unpark(tmp_path, REPO, 9) == (False, {})
    assert parking.load(tmp_path) == ({}, False)
    assert [x["action"] for x in log_lines(tmp_path)] == ["park", "park", "unpark", "unpark"]
    first = log_lines(tmp_path)[0]
    assert first == {"at": 100, "action": "park", "repo": REPO, "number": 9, "note_chars": 13, "wake_commits": True}


def test_a_note_is_one_line_of_at_most_280_characters():
    assert parking.clean_note("a\nb\tc\x00d\x1b[31m  e ") == "a b c d [31m e"
    assert len(parking.clean_note("x" * 400)) == parking.MAX_NOTE == 280
    assert parking.clean_note(None) == ""


def test_a_bad_seen_is_refused_before_anything_is_written(tmp_path):
    with pytest.raises(ValueError):
        parking.park(tmp_path, REPO, 9, seen={"head_sha": "not a sha"})
    with pytest.raises(ValueError):
        parking.park(tmp_path, REPO, 9, seen={"head_sha": SHA_A, "comment_at": "yesterday"})
    assert not (tmp_path / parking.FILE).exists() and log_lines(tmp_path) == []


@pytest.mark.parametrize("text", ['{"version": 1, "parked": {"a#1": {"repo"', "[]", '{"parked": []}', "\x00\x01"])
def test_a_corrupt_or_truncated_file_reads_as_empty_and_is_reported(tmp_path, text):
    (tmp_path / parking.FILE).write_text(text, encoding="utf-8")
    assert parking.load(tmp_path) == ({}, True)


def test_no_folder_and_no_file_read_as_empty_without_a_report(tmp_path):
    assert parking.load(None) == ({}, False)
    assert parking.load(tmp_path / "missing") == ({}, False)


def test_a_corrupt_file_is_never_pruned(tmp_path):
    (tmp_path / parking.FILE).write_text("{", encoding="utf-8")
    assert parking.prune(tmp_path, set(), scan_at=10**10) == []
    assert (tmp_path / parking.FILE).read_text(encoding="utf-8") == "{"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_two_processes_writing_at_once_keep_both_entries(tmp_path):
    pids = []
    for who in range(2):
        pid = os.fork()
        if pid == 0:  # child: 25 parks of its own numbers
            try:
                for i in range(25):
                    parking.park(tmp_path, REPO, who * 100 + i + 1, seen=seen())
            finally:
                os._exit(0)
        pids.append(pid)
    for pid in pids:
        assert os.waitpid(pid, 0)[1] == 0
    entries, bad = parking.load(tmp_path)
    assert not bad and len(entries) == 50
    assert len([x for x in log_lines(tmp_path) if x["action"] == "park"]) == 50


def test_a_prune_keeps_a_park_newer_than_its_scan_and_what_the_scan_saw_open(tmp_path):
    parking.park(tmp_path, REPO, 1, seen=seen(), now=lambda: 100)   # closed, parked before the scan
    parking.park(tmp_path, REPO, 2, seen=seen(), now=lambda: 100)   # still open
    parking.park(tmp_path, REPO, 3, seen=seen(), now=lambda: 205)   # parked while the scan ran
    gone = parking.prune(tmp_path, {"ACME/demo#2"}, scan_at=200, now=lambda: 210)
    assert gone == ["acme/demo#1"]
    assert sorted(parking.load(tmp_path)[0]) == ["acme/demo#2", "acme/demo#3"]
    assert log_lines(tmp_path)[-1] == {"at": 210, "action": "prune", "ids": ["acme/demo#1"]}
    assert parking.prune(tmp_path, {"acme/demo#2"}, scan_at=200) == []  # nothing to drop: no write, no line
    assert len(log_lines(tmp_path)) == 4


def test_a_park_between_the_prune_s_read_and_its_write_survives(tmp_path, monkeypatch):
    parking.park(tmp_path, REPO, 1, seen=seen(), now=lambda: 100)
    real = parking.load
    calls = []

    def load_then_park(data_dir):  # the second load is the prune's re-read under the lock
        calls.append(1)
        if len(calls) == 1:
            out = real(data_dir)
            monkeypatch.setattr(parking, "load", real)
            parking.park(tmp_path, REPO, 7, seen=seen(), now=lambda: 250)  # a click while the scan ran
            monkeypatch.setattr(parking, "load", load_then_park)
            return out
        return real(data_dir)

    monkeypatch.setattr(parking, "load", load_then_park)
    assert parking.prune(tmp_path, set(), scan_at=200) == ["acme/demo#1"]
    assert list(real(tmp_path)[0]) == ["acme/demo#7"]  # the prune re-read under the lock and kept the new park


def test_the_store_writes_atomically_and_keeps_its_shape(tmp_path):
    parking.park(tmp_path, REPO, 9, note="n", seen=seen(), now=lambda: 1)
    data = json.loads((tmp_path / parking.FILE).read_text(encoding="utf-8"))
    assert data["version"] == 1 and set(data["parked"]["acme/demo#9"]) == {
        "repo", "number", "note", "parked_at", "wake_commits", "seen"}
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]


# The wake rule.

def entry(**over):
    base = {"repo": REPO, "number": 9, "note": "", "parked_at": 1_000_000, "wake_commits": True, "seen": seen()}
    return {**base, **over}


def row(**over):
    base = {"repo": REPO, "number": 9, "url": f"{FORGE}/{REPO}/pulls/9", "head_sha": SHA_A, "draft": False,
            "wake_comment": None}
    return {**base, **over}


NOW = lambda: 1_000_000 + 3 * 86400  # noqa: E731


def test_ready_wakes_a_parked_draft_and_comes_first():
    e = entry(seen=seen(draft=True))
    r = row(draft=False, head_sha=SHA_B, wake_comment={"author": "bob", "at": "2030-01-01T00:00:00Z", "url": "c"})
    assert parking.wake(e, r, NOW, forge_url=FORGE) == {
        "reason": "ready", "text": "It is marked ready now.", "url": f"{FORGE}/{REPO}/pulls/9"}


def test_an_unknown_draft_state_never_wakes():
    assert parking.wake(entry(seen=seen(draft=True)), row(draft=None), NOW) is None
    assert parking.wake(entry(seen=seen(draft=None)), row(draft=False), NOW) is None


def test_new_commits_wake_with_the_compare_page():
    out = parking.wake(entry(), row(head_sha=SHA_B), NOW, forge_url=FORGE + "/")
    assert out == {"reason": "commit", "text": "New commits since you parked it 3 d ago.",
                   "url": f"{FORGE}/{REPO}/compare/{SHA_A}...{SHA_B}"}


def test_new_commits_do_not_wake_without_the_box_or_without_a_known_sha():
    assert parking.wake(entry(wake_commits=False), row(head_sha=SHA_B), NOW) is None
    assert parking.wake(entry(seen=seen(sha="")), row(head_sha=SHA_B), NOW) is None
    assert parking.wake(entry(), row(head_sha=""), NOW) is None


def test_a_newer_comment_by_someone_else_wakes_with_its_link():
    c = {"author": "bob", "at": "1970-01-12T14:00:00Z", "url": f"{FORGE}/{REPO}/pulls/9#issuecomment-5"}
    out = parking.wake(entry(), row(wake_comment=c), NOW)
    assert out == {"reason": "comment", "text": "@bob commented since you parked it 3 d ago.", "url": c["url"]}


def test_a_comment_older_than_what_he_saw_does_not_wake():
    c = {"author": "bob", "at": "2026-11-08T12:00:00Z", "url": "u"}
    assert parking.wake(entry(seen=seen(comment_at="2026-11-08T12:00:00Z")), row(wake_comment=c), NOW) is None
    newer = {**c, "at": "2026-11-08T12:00:01Z"}
    assert parking.wake(entry(seen=seen(comment_at="2026-11-08T12:00:00Z")), row(wake_comment=newer), NOW)["reason"] == "comment"
    before_park = {**c, "at": "1970-01-12T13:00:00Z"}  # parked_at 1_000_000 is 13:46:40
    assert parking.wake(entry(), row(wake_comment=before_park), NOW) is None


def test_first_match_wins_commit_before_comment():
    c = {"author": "bob", "at": "2030-01-01T00:00:00Z", "url": "u"}
    assert parking.wake(entry(), row(head_sha=SHA_B, wake_comment=c), NOW, forge_url=FORGE)["reason"] == "commit"
    assert parking.wake(entry(wake_commits=False), row(head_sha=SHA_B, wake_comment=c), NOW)["reason"] == "comment"


def test_nothing_changed_means_still_parked():
    assert parking.wake(entry(), row(), NOW) is None


def test_seen_of_a_row_is_its_fingerprint():
    r = row(draft=True, wake_comment={"author": "bob", "at": "2026-11-08T12:00:00Z", "url": "u"})
    assert parking.seen_of(r) == {"head_sha": SHA_A, "comment_at": "2026-11-08T12:00:00Z", "draft": True}
    assert parking.seen_of({"draft": "yes"}) == {"head_sha": "", "comment_at": None, "draft": None}


# Which comment wakes: _last_comment's second value.

class Answer:
    def __init__(self, data, total):
        self.data, self.headers = data, {"x-total-count": str(total)}


class Thread:
    def __init__(self, comments, fail=False):
        self.comments, self.fail = comments, fail

    def get(self, path, params=None):
        if self.fail:
            raise OSError("down")
        page, limit = params["page"], params["limit"]
        return Answer(self.comments[(page - 1) * limit:page * limit], len(self.comments))


def c(i, author, body):
    return {"id": i, "user": {"login": author}, "body": body, "created_at": f"2026-11-08T12:{i:02d}:00Z",
            "html_url": f"u#{i}"}


def wake_of(comments, you="acme", fail=False):
    return desk._last_comment(Thread(comments, fail), REPO, 9, you=you)


def test_his_own_comment_never_wakes_and_a_flow_status_comment_does_not_either():
    last, wake = wake_of([c(1, "bob", "hi"), c(2, "ACME", "my note"), c(3, "hermes", STATUS)])
    assert last["author"] == "ACME" and wake == {"author": "bob", "at": "2026-11-08T12:01:00Z", "url": "u#1"}


def test_a_hermes_tool_comment_wakes_but_the_shown_comment_skips_it():
    last, wake = wake_of([c(1, "bob", "hi"), c(2, "hermes", TOOL)])
    assert last["author"] == "bob" and wake["author"] == "hermes"


def test_a_failed_comment_read_wakes_nothing():
    assert wake_of([c(1, "bob", "hi")], fail=True) == (None, None)
    e = entry()
    assert parking.wake(e, row(wake_comment=None), NOW) is None


def test_only_his_comments_give_no_wake_comment():
    assert wake_of([c(1, "acme", "a"), c(2, "acme", "b")])[1] is None


def test_the_wake_comment_is_found_on_the_previous_page():
    comments = [c(1, "bob", "hi")] + [c(i, "acme", "me") for i in range(2, 13)]
    last, wake = wake_of(comments)
    assert last["author"] == "acme" and wake["author"] == "bob"


def test_parking_is_quick_enough_for_a_click(tmp_path):
    start = time.monotonic()
    for i in range(50):
        parking.park(tmp_path, REPO, i + 1, seen=seen())
    assert time.monotonic() - start < 5
