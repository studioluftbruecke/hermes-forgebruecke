"""A review worker starts only in a profile that enables Forgebrücke: the plugin's own hook is what refuses
every tool call in a worker's session, and a profile that does not load the plugin has no such hook. Any other
review profile ends the gate in ``error`` with the command that fixes it, and the flow says so when it loads."""
import pytest

from fb_fakes import DEMO, HEAD_SHA, sub
from fb_fakes_engine import FLOW, Kit
from fb_fakes_rr_engine import fake_hermes, list_posted_comments, profiles, wait_for

runner_mod = sub("flows.runner")
sources = sub("flows.sources")
profile_mod = sub("profile")
models = sub("models")


@pytest.fixture(autouse=True)
def no_worker_outlives_its_test():
    before = set(runner_mod._PROCS)
    yield
    for pid in set(runner_mod._PROCS) - before:
        proc = runner_mod._PROCS.pop(pid)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def kit_with(tmp_path, by: str, home):
    k = Kit(tmp_path / "state", flow={**FLOW, "gates": [{"review": {"by": by, "timeout_min": 10}}, *FLOW["gates"][1:]]})
    script = fake_hermes(tmp_path / "bin")
    k.runner = runner_mod.ReviewRunner(hermes_bin=str(script), home=home)
    list_posted_comments(k)
    return k, script


def run_review(k) -> None:
    k.tick()
    verdict = k.store.verdict_path("demo", DEMO, 3, HEAD_SHA, "review")
    if k.gates()["review"]["state"] == "running":
        assert wait_for(verdict.exists)
        assert wait_for(lambda: not runner_mod.ReviewRunner.alive(k.gates()["review"]["evidence"]["pid"],
                                                                   log_path=verdict.with_suffix(".log")))
        k.tick()


@pytest.mark.parametrize("by", ["coding", "reviewer", "default"])
def test_a_profile_that_enables_forgebruecke_reviews_as_before(tmp_path, by):
    home = profiles(tmp_path / "root", coding=["forgebruecke"], reviewer=["coolifybruecke", "forgebruecke"],
                    default=["forgebruecke"])
    k, script = kit_with(tmp_path, by, home)
    run_review(k)
    assert k.gates()["review"]["state"] == "passed"
    assert (script.parent / "argv.json").exists()


@pytest.mark.parametrize("by, config, said", [
    ("devops", {"devops": ["coolifybruecke"]}, "does not enable Forgebrücke"),
    ("nobody", {}, "does not exist here"),
])
def test_a_profile_without_forgebruecke_starts_no_worker_and_the_gate_says_why(tmp_path, by, config, said):
    home = profiles(tmp_path / "root", coding=["forgebruecke"], **{k: v for k, v in config.items()})
    k, script = kit_with(tmp_path, by, home)
    k.tick()
    review = k.gates()["review"]
    assert review["state"] == "error"
    assert said in review["detail"]
    assert not (script.parent / "argv.json").exists()
    assert k.store.running_reviews() == 0
    assert len(k.notifies()) == 1 and said in k.notifies()[0]


def test_a_name_that_is_no_profile_name_is_refused_before_any_path_is_read(tmp_path):
    home = profiles(tmp_path / "root", coding=["forgebruecke"])
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: [forgebruecke]\n", encoding="utf-8")
    for name in ("../..", "Coding", ""):
        assert "is not a profile name" in runner_mod.ReviewRunner(home=home).refusal(name)


def test_the_error_names_the_command_that_enables_the_plugin_there(tmp_path):
    home = profiles(tmp_path / "root", coding=["forgebruecke"], devops=["coolifybruecke"])
    k, _ = kit_with(tmp_path, "devops", home)
    k.tick()
    assert "hermes -p devops plugins enable forgebruecke" in k.gates()["review"]["detail"]


def test_a_plugin_listed_as_disabled_counts_as_not_enabled(tmp_path):
    home = profiles(tmp_path / "root", coding=["forgebruecke"], devops=["forgebruecke"])
    (home.parent / "devops" / "config.yaml").write_text(
        "plugins:\n  enabled: [forgebruecke]\n  disabled: [forgebruecke]\n", encoding="utf-8")
    k, _ = kit_with(tmp_path, "devops", home)
    k.tick()
    assert k.gates()["review"]["state"] == "error"


def test_enabling_the_plugin_and_pushing_runs_the_review(tmp_path):
    home = profiles(tmp_path / "root", coding=["forgebruecke"], devops=["coolifybruecke"])
    k, _ = kit_with(tmp_path, "devops", home)
    k.tick()
    assert k.gates()["review"]["state"] == "error"
    profiles(tmp_path / "root", devops=["coolifybruecke", "forgebruecke"])
    k.push()
    k.tick()
    assert k.gates()["review"]["state"] in ("running", "passed")
    assert k.gates()["review"]["head_sha"] == "b" * 40


def test_the_flow_warns_when_its_review_profile_does_not_enable_forgebruecke(tmp_path, monkeypatch):
    home = profiles(tmp_path / "root", coding=["forgebruecke"], devops=["coolifybruecke"], reviewer=["forgebruecke"])
    monkeypatch.setattr(profile_mod, "current_home", lambda: home)
    bad, _, _ = models.validate_flow({**FLOW, "gates": [{"review": {"by": "devops"}}, {"human": {}}]})
    good, _, _ = models.validate_flow({**FLOW, "gates": [{"review": {"by": "reviewer"}}, {"human": {}}]})
    assert any("does not enable Forgebrücke" in w for w in sources.profile_warnings(bad))
    assert sources.profile_warnings(good) == []
