"""Which flow runs on which repo."""
from fb_fakes import DEMO
from fb_fakes_engine import FLOW, Kit, engine_env  # noqa: F401


def two_flows(tmp_path, order) -> Kit:
    k = Kit(tmp_path, flows=order)
    k.set_flow({**FLOW, "flow": "second"})
    return k


def test_a_rule_that_lists_two_flows_runs_the_first_and_says_so(tmp_path):
    k = two_flows(tmp_path, ("demo", "second"))
    reports = [k.tick() for _ in range(3)]
    assert len(k.runner.started) == 1 and k.runner.stopped == []
    assert reports[-1]["flows"] == [f"demo@{DEMO}"]
    assert f"{DEMO}: only the first flow of a rule runs. demo runs, second does not." in reports[-1]["notes"]
    assert k.state()["flow"] == "demo" and len(k.comments()) == 1


def test_the_first_flow_is_the_first_in_the_rule_not_in_the_files(tmp_path):
    k = two_flows(tmp_path, ("second", "demo"))
    assert k.tick()["flows"] == [f"second@{DEMO}"]


def test_a_first_flow_that_no_file_defines_runs_nothing_and_says_so(tmp_path):
    k = Kit(tmp_path, flows=("missing", "demo"))
    report = k.tick()
    assert report["flows"] == [] and k.runner.started == []
    assert f"{DEMO}: the rule lists flow missing, but no valid flow file defines it" in report["notes"]
