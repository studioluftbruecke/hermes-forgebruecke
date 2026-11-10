"""What a push does to the gates, and which statuses the new head gets."""
from fb_fakes import HEAD_SHA
from fb_fakes_engine import FLOW, NEW_SHA, PROMOTE, Kit, engine_env  # noqa: F401

NOTIFY_FIRST = {**FLOW, "gates": [{"notify": {"who": "acme", "message": "ready"}},
                                  {"review": {"by": "forgebruecke-dev"}}, {"human": {"who": "acme"}}]}


def all_passed(tmp_path) -> Kit:
    k = Kit(tmp_path)
    k.tick()
    k.verdict("pass", summary="fine")
    k.tick()
    assert k.gates()["human"]["state"] == "waiting"
    return k


def test_a_push_after_every_gate_passed_runs_every_gate_again(tmp_path):
    k = all_passed(tmp_path)
    k.push()
    k.tick()
    g = k.gates()
    assert g["review"]["state"] == "running" and g["review"]["head_sha"] == NEW_SHA and not g["review"]["carried_from"]
    assert len(k.runner.started) == 2
    assert k.statuses(NEW_SHA) == {"forgebruecke/demo/review": "pending"}
    assert k.state()["history"][-1] == {"from": HEAD_SHA[:12], "to": NEW_SHA[:12], "at": int(k.clock.t),
                                        "reset": ["review", "check", "human"], "carried": []}


def test_a_push_while_a_later_gate_waits_runs_every_gate_again(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("pass", summary="fine")
    k.devops("pending")
    k.tick()
    assert k.gates()["check"]["state"] == "running"
    k.push()
    k.tick()
    assert k.gates()["review"]["state"] == "running" and len(k.runner.started) == 2
    assert k.statuses(HEAD_SHA)["forgebruecke/demo/check"] == "error"


def test_a_passed_check_is_never_carried_to_a_new_head(tmp_path):
    k = Kit(tmp_path, flow={**FLOW, "gates": [FLOW["gates"][1], FLOW["gates"][0], FLOW["gates"][2]]})
    k.tick()
    k.verdict("fail")
    k.tick()
    assert k.gates()["check"]["state"] == "passed" and k.gates()["review"]["state"] == "failed"
    k.push()
    k.tick()
    g = k.gates()
    assert g["check"]["state"] == "running" and not g["check"]["carried_from"] and g["review"]["state"] == "pending"
    assert k.statuses(NEW_SHA) == {"forgebruecke/demo/check": "pending"}
    assert len(k.runner.started) == 1


def test_a_passed_promote_is_never_carried_to_a_new_head(tmp_path):
    k = Kit(tmp_path, flow={**FLOW, "gates": [PROMOTE["gates"][0], FLOW["gates"][0], FLOW["gates"][2]]})
    k.staging()
    k.tick()
    k.verdict("fail")
    k.tick()
    assert k.gates()["promote"]["state"] == "passed" and k.gates()["review"]["state"] == "failed"
    k.push()
    k.tick()
    assert [b["head_commit_id"] for _, _, b in k.sent("/merge", "POST")] == [HEAD_SHA, NEW_SHA]
    assert not k.gates()["promote"]["carried_from"]


def test_a_notify_gate_never_gets_a_status(tmp_path):
    k = Kit(tmp_path, flow=NOTIFY_FIRST)
    k.tick()
    k.verdict("fail")
    k.tick()
    k.push()
    k.tick()
    assert k.gates()["notify"]["state"] == "passed" and k.gates()["notify"]["carried_from"] == HEAD_SHA
    assert not any(c.endswith("/notify") for c in {**k.statuses(), **k.statuses(NEW_SHA)})
    assert len([c for c in k.comments() if "ready" in c]) == 1


def test_a_check_that_failed_and_waits_again_shows_pending(tmp_path):
    k = Kit(tmp_path, flow={**FLOW, "gates": FLOW["gates"][1:]})
    k.devops("failure")
    k.tick()
    assert k.statuses()["forgebruecke/demo/check"] == "failure"
    k.devops("pending")
    k.tick()
    assert k.gates()["check"]["state"] == "running"
    assert k.statuses()["forgebruecke/demo/check"] == "pending"
