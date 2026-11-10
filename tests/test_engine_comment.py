"""What the status comment on the pull request says."""
from fb_fakes_engine import FLOW, Kit, engine_env  # noqa: F401

CREATORS = {**FLOW, "gates": [{"check": {"contexts": ["devops/tests"], "creators": ["hermes"]}}, {"human": {"who": "acme"}}]}


def test_a_check_posted_by_an_unexpected_account_is_flagged_under_the_table(tmp_path):
    k = Kit(tmp_path, flow=CREATORS)
    k.devops("success", creator="mallory")
    k.tick()
    assert k.gates()["check"]["state"] == "passed"
    table, _, below = k.comments()[0].partition("| acme merges |")
    assert "mallory" not in table
    assert "**Posted by an unexpected account**\n- devops/tests was posted by mallory, not by hermes" in below


def test_a_check_posted_by_a_named_account_has_no_flag(tmp_path):
    k = Kit(tmp_path, flow=CREATORS)
    k.tick()
    assert "unexpected account" not in k.comments()[0]
