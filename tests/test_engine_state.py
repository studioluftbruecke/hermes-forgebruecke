"""The stored state of a pull request when the flow changes under it, or when its file is damaged."""
from fb_fakes import DEMO, HEAD_SHA
from fb_fakes_engine import FLOW, Kit, engine_env  # noqa: F401

WITHOUT_CHECK = {**FLOW, "gates": [FLOW["gates"][0], FLOW["gates"][2]]}


def test_a_gate_list_change_during_a_review_stops_the_old_worker(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.set_flow(WITHOUT_CHECK)
    k.tick()
    assert k.runner.stopped == [1001] and len(k.runner.started) == 2
    assert list(k.gates()) == ["review", "human"] and k.gates()["review"]["state"] == "running"
    assert k.store.running_reviews() == 1 and len(k.comments()) == 1


def test_a_gate_list_change_keeps_the_review_rounds(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("fail")
    k.tick()
    k.set_flow(WITHOUT_CHECK)
    k.tick()
    assert k.gates()["review"]["detail"] == "review by forgebruecke-dev running (round 2 of 3)"


def test_a_rule_that_moves_to_another_flow_ends_the_old_flows_review(tmp_path):
    k = Kit(tmp_path)
    k.set_flow({**FLOW, "flow": "second"})
    k.tick()
    k.values["writes"] = [{"repo_glob": DEMO, "flows": ["second"]}]
    k.tick()
    assert k.runner.stopped == [1001] and k.state()["flow"] == "second"
    assert k.statuses() == {**k.statuses(), "forgebruecke/demo/review": "error", "forgebruecke/second/review": "pending"}


def test_a_state_file_that_does_not_parse_is_reported_and_left_alone(tmp_path):
    k = Kit(tmp_path)
    k.add_pr(4)
    k.tick()
    path = k.store.pr_path(DEMO, 3)
    torn = path.read_text()[:200]
    path.write_text(torn)
    report = k.tick()
    assert len(k.runner.started) == 2 and len(k.comments(3)) == 1 and path.read_text() == torn
    assert report["errors"] == [f"{DEMO}#3: its state file does not parse, so it was skipped. "
                                "Delete flows/prs/acme__forgebruecke-demo/3.json to start its gates over."]
    assert [p["pr"] for p in report["prs"]] == [4]
    assert k.store.load_pr(DEMO, 3) is None and HEAD_SHA[:12] in k.comments(3)[0]


def test_a_reopened_pull_request_is_open_again(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.data["pulls"][3]["state"] = "closed"
    k.tick()
    assert k.state()["closed"] is True
    k.data["pulls"][3]["state"] = "open"
    k.tick()
    assert k.state()["closed"] is False and k.state()["merged"] is False


def test_state_stored_before_the_gate_hashes_existed_is_kept(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    st = k.state()
    del st["gate_hashes"]
    k.store.save_pr(st)
    k.tick()
    assert len(k.runner.started) == 1 and k.runner.stopped == [] and len(k.state()["gate_hashes"]) == 3


def test_a_lease_field_an_older_version_stored_is_dropped_on_the_next_save(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    st = k.state()
    st["gates"][0]["claim_until"] = 1
    k.store.save_pr(st)
    k.verdict("pass")
    k.tick()
    assert k.gates()["review"]["state"] == "passed"
    assert not any("claim_until" in g for g in k.state()["gates"])


def test_statuses_an_older_version_already_sent_are_not_sent_again(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("fail")
    k.tick()
    st = k.state()
    for gate in st["gates"]:
        del gate["posted"]
    k.store.save_pr(st)
    sent = len(k.status_posts())
    k.tick()
    assert len(k.status_posts()) == sent and k.gates()["review"]["posted"]
