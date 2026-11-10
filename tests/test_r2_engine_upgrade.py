"""State files the last release stored. It saved dry runs too, so a write such a state only claims must
still happen once dry run is off. The shapes below are what that release wrote, field for field."""
from fb_fakes import DEMO, HEAD_SHA
from fb_fakes_engine import FLOW, PROMOTE, Kit, engine_env  # noqa: F401

NOTIFY = {**FLOW, "gates": [FLOW["gates"][1], {"notify": {"who": "acme", "message": "ready"}}, {"human": {}}]}
CHECK_ONLY = {**FLOW, "gates": [FLOW["gates"][1], {"human": {}}]}


def gate(index: int, key: str, state: str, detail: str = "", **evidence) -> dict:
    gate_type = key.split("-")[0]
    started = None if state == "pending" or gate_type == "notify" else 1_800_000_000
    return {"id": f"{DEMO}#3@{HEAD_SHA[:12]}:{key}", "flow": "demo", "repo": DEMO, "pr": 3, "head_sha": HEAD_SHA,
            "gate_index": index, "gate_key": key, "type": gate_type, "state": state, "attempts": 0,
            "evidence": evidence, "started_at": started, "ended_at": 1_800_000_000 if state == "passed" else None,
            "claim_until": None, "detail": detail, "carried_from": ""}


def store_old_state(k: Kit, gates: list, *, comment_id=None, notified=()) -> None:
    k.store.save_pr({"repo": DEMO, "pr": 3, "flow": "demo", "flow_version": 1, "head_sha": HEAD_SHA,
                     "status_comment_id": comment_id, "notified": list(notified), "history": [], "rounds": {},
                     "gates": gates, "closed": False, "merged": False, "title": "Fix calc.mul", "base": "main",
                     "head_ref": "fix-mul", "status_comment_hash": "72d9aea8bc827c3b"})


def check_passed(index: int) -> dict:
    return gate(index, "check", "passed", "passed: devops/tests",
                contexts={"devops/tests": {"state": "success", "creator": "hermes"}})


def test_a_promote_a_dry_run_only_recorded_merges_once_dry_run_is_off(tmp_path):
    k = Kit(tmp_path, flow=PROMOTE)
    k.staging()
    k.keep_opened_pulls()
    store_old_state(k, [gate(0, "promote", "passed", "dry run: would merge fix-mul into staging"),
                        gate(1, "human", "waiting", "waiting for you to merge in Forgejo")])
    for _ in range(3):
        assert k.tick()["errors"] == []
    promote = k.gates()["promote"]
    assert len(k.sent("/merge", "POST")) == 1 and promote["state"] == "passed"
    assert promote["detail"].startswith("merged into staging at ")
    posted = [b for b in k.status_posts() if b["context"] == "forgebruecke/demo/promote"]
    assert [b["state"] for b in posted] == ["success"] and "dry run" not in posted[0]["description"]
    assert len(k.comments()) == 1 and "dry run" not in k.comments()[0]


def test_a_notify_a_dry_run_only_recorded_is_sent_once_dry_run_is_off(tmp_path):
    k = Kit(tmp_path, flow=NOTIFY)
    store_old_state(k, [check_passed(0),
                        gate(1, "notify", "passed", "notified acme with a comment", comment_id=None),
                        gate(2, "human", "waiting", "waiting for you to merge in Forgejo")])
    for _ in range(3):
        assert k.tick()["errors"] == []
    assert [body.split("\n")[0] for body in k.notifies()] == ["@acme ready"]
    assert k.gates()["notify"]["evidence"]["comment_id"] and k.gates()["check"]["ended_at"] == 1_800_000_000


def test_a_failure_a_dry_run_only_recorded_is_notified_once_dry_run_is_off(tmp_path):
    k = Kit(tmp_path, flow=CHECK_ONLY)
    k.devops("failure")
    failed = gate(0, "check", "failed", "failed: devops/tests (tests)",
                  contexts={"devops/tests": {"state": "failure", "creator": "hermes"}})
    store_old_state(k, [failed, gate(1, "human", "pending")], notified=[failed["id"]])
    for _ in range(3):
        k.tick()
    assert len(k.notifies()) == 1 and len(k.comments()) == 2


def test_a_live_state_of_the_last_release_keeps_its_gates_and_gets_its_comment_edited_once(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("fail")
    k.tick()
    st = k.state()
    for stored in st["gates"]:
        del stored["posted"]
    del st["gate_hashes"]
    k.store.save_pr(st)
    edits = len(k.sent("", "PATCH"))
    for _ in range(3):
        k.tick()
    assert len(k.runner.started) == 1 and k.gates()["review"]["state"] == "failed"
    assert len(k.notifies()) == 1 and len(k.sent("", "PATCH")) == edits + 1


def test_a_dry_run_tick_leaves_an_old_dry_run_state_as_it_is(tmp_path):
    k = Kit(tmp_path, flow=PROMOTE, dry_run=True)
    k.staging()
    store_old_state(k, [gate(0, "promote", "passed", "dry run: would merge fix-mul into staging"),
                        gate(1, "human", "waiting", "waiting for you to merge in Forgejo")])
    saved = k.store.pr_path(DEMO, 3).read_text()
    k.tick()
    assert k.forge.writes == [] and k.store.pr_path(DEMO, 3).read_text() == saved
