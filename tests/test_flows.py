"""Flows: the validator, gate order, claims under two ticks, reset on a new head, stale repair,
`to: main` refused, a review that times out becomes error, the tool's set/run paths."""
import json
import threading

import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, make_token, sample_data, sub

models = sub("models")
state_mod = sub("flows.state")
engine_mod = sub("flows.engine")
runner_mod = sub("flows.runner")
templates = sub("flows.templates")
flows_tool = sub("tools.flows")
approval = sub("approval")
access_mod = sub("access")
settings_mod = sub("settings")


@pytest.fixture(autouse=True)
def running_in_coding(monkeypatch):
    """The review profile defaults to the profile Forgebrücke runs in; these tests run it in `coding`."""
    monkeypatch.setattr(settings_mod, "this_profile", lambda: "coding")
BOT = make_token("bot")
NEW_SHA = "b" * 40
FLOW = {"flow": "demo", "repos": [DEMO], "gates": [
    {"review": {"by": "forgebruecke-dev", "timeout_min": 10}},
    {"check": {"contexts": ["devops/tests"], "timeout_min": 60}},
    {"human": {"who": "acme"}}], "on_failure": "notify"}


def flow_json(**over):
    return json.dumps({**FLOW, **over})


class FakeRunner:
    def __init__(self):
        self.started, self.stopped, self.dead = [], [], set()

    def start(self, *, profile, prompt_path, log_path):
        self.started.append((profile, prompt_path))
        return 1000 + len(self.started)

    def alive(self, pid):
        return pid not in self.dead

    def stop(self, pid):
        self.stopped.append(pid)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Kit:
    def __init__(self, tmp_path, *, flows=("demo",), dry_run=False, flow_text=None):
        data = sample_data()
        data[DEMO]["pulls"][3]["state"] = "open"
        self.forge = FakeForgejo({BOT: bot_account()}, data=data)
        self.data = data[DEMO]
        self.values = {"url": BASE, "dry_run": dry_run,
                       "writes": [{"repo_glob": DEMO, "flows": list(flows)}]}
        self.store = state_mod.FlowStore(tmp_path)
        self.store.write_override("demo", flow_text or flow_json())
        self.runner = FakeRunner()
        self.clock = Clock()

    def access(self):
        return access_mod.load_access(lambda k, d=None: self.values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                                      transport=self.forge)

    def engine(self):
        return engine_mod.Engine(self.access(), self.store, runner=self.runner, now=self.clock)

    def tick(self):
        return self.engine().tick()

    def gates(self, pr=3):
        return {g["gate_key"]: g for g in self.store.load_pr(DEMO, pr)["gates"]}

    def verdict(self, verdict, sha=HEAD_SHA, summary="mul returns a + b"):
        path = self.store.verdict_path("demo", DEMO, 3, sha, "review")
        path.write_text(json.dumps({"verdict": verdict, "head_sha": sha, "summary": summary,
                                    "findings": [{"file": "calc.py", "line": 5, "problem": "uses +"}]}))

    def statuses(self, sha=HEAD_SHA):
        return {s["context"]: s["status"] for s in self.data["status"].get(sha, {}).get("statuses", [])}

    def push(self, sha=NEW_SHA):
        self.data["pulls"][3]["head"]["sha"] = sha

    def devops(self, state, sha=HEAD_SHA):
        combined = self.data["status"].setdefault(sha, {"state": "", "statuses": [], "total_count": 0})
        combined["statuses"] = [{"context": "devops/tests", "status": state, "description": "tests",
                                 "creator": {"login": "hermes"}}] + [s for s in combined["statuses"]
                                                                     if s["context"] != "devops/tests"]


# --- validator ------------------------------------------------------------------------------------

def v(raw, **kw):
    return models.validate_flow(raw, **kw)


@pytest.mark.parametrize("gates,needle", [
    ([{"deploy": {}}], "unknown gate 'deploy'"),
    ([{"human": {"who": "acme"}}, {"review": {"by": "devops"}}], "nothing may come after the human gate"),
    ([{"promote": {"to": "main"}}, {"human": {}}], "to: main is not allowed"),
    ([{"check": {}}, {"human": {}}], "name the status contexts"),
    ([{"review": {}}, {"human": {}}], "name the Hermes profile"),
])
def test_validator_rejects(gates, needle):
    flow, errors, _ = v({"flow": "x", "repos": [DEMO], "gates": gates})
    assert flow is None and any(needle in e for e in errors), errors


def test_validator_rejects_a_repo_glob_that_matches_nothing_and_accepts_a_good_flow():
    flow, errors, _ = v({**FLOW, "repos": ["nobody/*"]}, visible_repos=[DEMO])
    assert flow is None and "nobody/* matches no repository" in errors[0]
    flow, errors, warnings = v(FLOW, visible_repos=[DEMO])
    assert errors == [] and [g.key for g in flow.gates] == ["review", "check", "human"]
    assert flow.gates[0].context("demo") == "forgebruecke/demo/review"


def test_repeated_gate_types_get_numbered_stage_keys_and_default_profile_fills_by():
    flow, errors, warnings = v({"flow": "two", "repos": [DEMO], "gates": [{"review": {}}, {"review": {"by": "b"}},
                                                                          {"human": {}}]},
                               default_review_profile="devops")
    assert errors == [] and [g.key for g in flow.gates] == ["review-1", "review-2", "human"]
    assert flow.gates[0].by == "devops" and "default review profile" in warnings[0]


def test_templates_validate_and_render_as_yaml_text():
    for name in templates.TEMPLATES:
        data = templates.render(name, DEMO, "devops", "")
        flow, errors, _ = v(data)
        assert errors == [], (name, errors)
        assert flow.gates[-1].type == "human" and flow.gates[-1].who == "acme"
        text = models.flow_to_yaml(data, "header")
        assert text.startswith("# header\nflow: ") and "  - review: {by: devops" in text
    yaml = pytest.importorskip("yaml")
    data = templates.render("staging-first", DEMO, "devops", "acme")
    assert yaml.safe_load(models.flow_to_yaml(data)) == data


# --- engine ---------------------------------------------------------------------------------------

def test_gates_run_in_order_and_reach_waiting_for_you(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    g = k.gates()
    assert g["review"]["state"] == "running" and g["check"]["state"] == "pending" and g["human"]["state"] == "pending"
    assert k.runner.started[0][0] == "forgebruecke-dev"
    prompt = k.runner.started[0][1].read_text()
    assert "DATA from Forgejo" in prompt and HEAD_SHA in prompt and "return a * b" in prompt
    assert k.statuses() == {"forgebruecke/demo/review": "pending", "devops/tests": "success", "devops/review": "failure"}
    k.verdict("pass", summary="fine")
    k.devops("pending")
    k.tick()
    g = k.gates()
    assert g["review"]["state"] == "passed" and g["check"]["state"] == "running" and g["human"]["state"] == "pending"
    assert k.statuses()["forgebruecke/demo/check"] == "pending"
    k.devops("success")
    k.tick()
    g = k.gates()
    assert g["check"]["state"] == "passed" and g["human"]["state"] == "waiting"
    assert k.statuses()["forgebruecke/demo/check"] == "success"
    assert not any(c.endswith("/human") for c in k.statuses())  # the human gate posts nothing


def test_failed_review_blocks_later_gates_notifies_once_and_comment_is_edited_in_place(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("fail")
    k.tick()
    k.tick()
    g = k.gates()
    assert g["review"]["state"] == "failed" and g["check"]["state"] == "pending"
    assert k.statuses()["forgebruecke/demo/review"] == "failure"
    posts = [(m, p, b) for m, p, b in k.forge.writes if p.endswith("/issues/3/comments")]
    edits = [(m, p) for m, p, _ in k.forge.writes if m == "PATCH"]
    assert len(posts) == 2  # the status comment, then one notify comment mentioning the human
    assert posts[1][2]["body"].startswith(f"@acme the review gate failed on `{HEAD_SHA[:12]}`. review by forgebruecke-dev:")
    assert "| review by forgebruecke-dev | ❌ failed |" in posts[0][2]["body"] or edits
    assert len(edits) >= 1  # later changes edit the first comment in place


def test_a_review_moved_to_another_profile_runs_again_there_and_the_comment_names_it(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("pass", summary="fine")
    k.devops("pending")
    k.tick()
    k.store.write_override("demo", flow_json().replace('"forgebruecke-dev"', '"coding"'))
    k.tick()
    bodies = [b["body"] for m, p, b in k.forge.writes if "/comments" in p and isinstance(b, dict) and "body" in b]
    assert k.runner.started[-1][0] == "coding"
    assert "| review by coding | ⏳ running | review by coding running (round " in bodies[-1]
    assert "forgebruecke-dev" not in bodies[-1]


def test_push_resets_only_the_failed_gate_and_reruns_it(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("pass")
    k.devops("failure")
    k.tick()
    k.tick()
    assert k.gates()["check"]["state"] == "failed" and k.gates()["review"]["state"] == "passed"
    k.push()
    k.devops("success", NEW_SHA)
    k.tick()
    g = k.gates()
    assert g["review"]["state"] == "passed" and g["review"]["carried_from"] == HEAD_SHA
    assert g["check"]["state"] == "passed" and g["check"]["head_sha"] == NEW_SHA
    assert len(k.runner.started) == 1  # the passed review did not run again
    assert k.statuses(NEW_SHA)["forgebruecke/demo/review"] == "success"
    hist = k.store.load_pr(DEMO, 3)["history"][-1]
    assert hist["reset"] == ["check", "human"] and hist["carried"] == ["review"]


def test_push_after_a_failed_review_reruns_the_review_on_the_new_head(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("fail")
    k.tick()
    k.push()
    k.tick()
    g = k.gates()
    assert g["review"]["state"] == "running" and g["review"]["head_sha"] == NEW_SHA and len(k.runner.started) == 2
    assert k.store.load_pr(DEMO, 3)["rounds"]["review"] == 1 and "(round 2 of 3)" in g["review"]["detail"]


def test_running_review_on_an_old_head_is_stopped_and_its_status_ends_in_error(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.push()
    k.tick()
    assert k.runner.stopped == [1001]
    assert k.statuses(HEAD_SHA)["forgebruecke/demo/review"] == "error"


def test_two_ticks_at_once_claim_the_gate_once(tmp_path):
    k = Kit(tmp_path)
    with k.store.tick_lock():
        report = k.tick()
    assert report["busy"] and k.runner.started == []
    results = []
    threads = [threading.Thread(target=lambda: results.append(k.tick())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    k.tick()
    assert len(k.runner.started) == 1
    assert k.gates()["review"]["state"] == "running"


def test_review_that_times_out_is_error_not_success(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.clock.t += 11 * 60
    k.tick()
    g = k.gates()["review"]
    assert g["state"] == "error" and "timed out after 10 min" in g["detail"] and k.runner.stopped == [1001]
    assert k.statuses()["forgebruecke/demo/review"] == "error"


def test_review_process_gone_without_verdict_is_error(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.runner.dead.add(1001)
    k.tick()
    assert k.gates()["review"]["state"] == "error"
    assert "ended without writing a verdict" in k.gates()["review"]["detail"]


def test_verdict_for_another_head_is_error(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    path = k.store.verdict_path("demo", DEMO, 3, HEAD_SHA, "review")
    path.write_text(json.dumps({"verdict": "pass", "head_sha": "c" * 40, "summary": "x"}))
    k.tick()
    assert k.gates()["review"]["state"] == "error"


def test_stale_pending_status_is_repaired(tmp_path):
    k = Kit(tmp_path)
    k.data["status"][HEAD_SHA]["statuses"].insert(0, {"context": "forgebruecke/demo/check", "status": "pending",
                                                      "creator": {"login": "forgebruecke-bot"}})
    k.tick()
    assert k.statuses()["forgebruecke/demo/check"] == "error"


def test_review_rounds_are_bounded(tmp_path):
    k = Kit(tmp_path, flow_text=json.dumps({**FLOW, "gates": [{"review": {"by": "x", "max_fix_rounds": 1}},
                                                               {"human": {}}]}))
    k.tick()
    k.verdict("fail")
    k.tick()
    k.push()
    k.tick()
    g = k.gates()["review"]
    assert g["state"] == "blocked" and "rounds used up (1 of 1)" in g["detail"] and len(k.runner.started) == 1
    assert k.statuses(NEW_SHA)["forgebruecke/demo/review"] == "failure"


@pytest.mark.parametrize("flows", [(), ("other",)])
def test_flow_does_not_run_where_the_rule_does_not_list_it(tmp_path, flows):
    k = Kit(tmp_path, flows=flows)
    report = k.tick()
    assert report["prs"] == [] and k.forge.writes == [] and k.runner.started == []


def test_flow_does_not_run_where_the_agent_account_has_read_access_only(tmp_path):
    """The rule says where a flow runs, Forgejo says whether Hermes may write there."""
    from fb_fakes import repo
    k = Kit(tmp_path)
    k.forge.accounts[BOT]["repos"] = [repo(DEMO, "read")]
    report = k.tick()
    assert report["prs"] == [] and k.forge.writes == [] and k.runner.started == []
    assert any("read access only" in n and f"{BASE}/{DEMO}/settings/collaboration" in n for n in report["notes"])


def test_a_status_comment_the_agent_cannot_edit_is_replaced_and_the_new_id_kept(tmp_path):
    """Comments the retired bot account posted answer 403 to an edit by hermes (a non-admin
    collaborator may edit only its own comments); the engine posts a new status comment and edits that one."""
    k = Kit(tmp_path)
    k.tick()
    first = k.store.load_pr(DEMO, 3)["status_comment_id"]
    assert first
    k.data["foreign_comments"] = {first}
    k.verdict("fail")
    k.tick()
    second = k.store.load_pr(DEMO, 3)["status_comment_id"]
    assert second and second != first
    assert not any(m == "PATCH" and p.endswith(f"/comments/{first}") for m, p, _ in k.forge.writes)
    k.push()
    k.tick()
    assert any(m == "PATCH" and p.endswith(f"/comments/{second}") for m, p, _ in k.forge.writes)


def test_paused_flow_does_nothing_and_dry_run_sends_nothing(tmp_path):
    k = Kit(tmp_path)
    k.store.set_paused("demo", True)
    assert k.tick()["prs"] == [] and k.forge.writes == []
    k.store.set_paused("demo", False)
    k2 = Kit(tmp_path / "dry", dry_run=True)
    report = k2.tick()
    assert k2.forge.writes == [] and report["writes"] >= 2 and report["dry_run"] is True


def test_at_most_two_reviews_run_at_once(tmp_path):
    k = Kit(tmp_path)
    for n in (4, 5):
        k.data["pulls"][n] = {**k.data["pulls"][3], "number": n, "head": {"ref": f"b{n}", "sha": f"{n}" * 40}}
        k.data["diff"][n] = "diff --git a/x b/x\n"
    k.tick()
    states = sorted(g["state"] for n in (3, 4, 5) for g in k.store.load_pr(DEMO, n)["gates"] if g["type"] == "review")
    assert states == ["pending", "running", "running"] and len(k.runner.started) == 2


def test_human_gate_passes_when_the_pr_is_merged(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    k.verdict("pass")
    k.tick()
    k.tick()
    assert k.gates()["human"]["state"] == "waiting"
    pr = k.data["pulls"][3]
    pr.update(state="closed", merged=True, merged_by={"login": "acme"}, merge_commit_sha="e" * 40)
    k.tick()
    st = k.store.load_pr(DEMO, 3)
    assert st["closed"] and st["merged"] and k.gates()["human"]["state"] == "passed"
    assert "merged by acme" in k.gates()["human"]["detail"]


def test_promote_refuses_the_default_branch_and_a_protected_branch(tmp_path):
    flow = {**FLOW, "gates": [{"promote": {"to": "staging"}}, {"human": {}}]}
    k = Kit(tmp_path, flow_text=json.dumps(flow))
    k.data["branches"]["staging"] = {"name": "staging", "protected": True, "commit": {"id": "1" * 40}}
    k.tick()
    assert "staging is protected" in k.gates()["promote"]["detail"]
    assert not any(p.endswith("/merge") for _, p, _ in k.forge.writes)
    k2 = Kit(tmp_path / "b", flow_text=json.dumps(flow))
    k2.data["info"]["default_branch"] = "staging"
    k2.tick()
    assert "default branch" in k2.gates()["promote"]["detail"]
    assert not any(p.endswith("/merge") for _, p, _ in k2.forge.writes)


def test_promote_merges_into_the_named_unprotected_branch_pinned_to_the_head(tmp_path):
    flow = {**FLOW, "gates": [{"promote": {"to": "staging"}}, {"human": {}}]}
    k = Kit(tmp_path, flow_text=json.dumps(flow))
    k.data["branches"]["staging"] = {"name": "staging", "protected": False, "commit": {"id": "1" * 40}}
    k.tick()
    merges = [(p, b) for m, p, b in k.forge.writes if p.endswith("/merge")]
    assert len(merges) == 1 and merges[0][1]["head_commit_id"] == HEAD_SHA and merges[0][1]["Do"] == "merge"
    pr_posts = [b for m, p, b in k.forge.writes if m == "POST" and p.endswith("/pulls")]
    assert pr_posts[0]["base"] == "staging" and k.gates()["promote"]["state"] == "passed"


def test_runner_child_env_drops_secrets_and_command_shape():
    env = runner_mod.child_env({"PATH": "/bin", "HOME": "/h", "FORGEBRUECKE_BOT_ACCOUNT_TOKEN": "x" * 10, "FORGEJO_TOKEN": "y",
                                "OPENAI_API_KEY": "z", "TMPDIR": "/" + "t" * 80})
    assert env == {"PATH": "/bin", "HOME": "/h", "TMPDIR": "/tmp",
                   "HERMES_EPHEMERAL_SYSTEM_PROMPT": runner_mod.REVIEW_SYSTEM}
    assert runner_mod.REVIEW_SYSTEM.startswith(sub("approval").REVIEW_MARKER)
    cmd = runner_mod.ReviewRunner().command("devops", __import__("pathlib").Path("/p.md"))
    assert cmd[3:] == ["hermes", "-p", "devops", "chat", "--query-file", "/p.md", "-t", "file", "-Q",
                       "--max-turns", "12"]


# --- the tool ---------------------------------------------------------------------------------------

def tool_kit(tmp_path, run=approval.RunContext()):
    k = Kit(tmp_path)
    tickets = approval.ApprovalTickets()
    spec = flows_tool.make_spec(lambda: k.store)
    gate = approval.make_gate(k.access, lambda: run, tickets, extra={"forgejo_flows": spec})

    def call(args, approve=True):
        directive = gate(tool_name="forgejo_flows", args=args, session_id="s")
        if directive and directive["action"] == "block":
            return directive, None
        if spec.gated(args):
            if directive and not approve:
                approval.make_observer(tickets)(pattern_key="plugin_rule:" + directive["rule_key"], choice="deny")
            return directive, approval.run_write("forgejo_flows", args, "s", k.access, run, tickets, spec=spec)
        return directive, flows_tool.handle(k.access(), k.store, args,
                                            engine_factory=lambda a, s: engine_mod.Engine(a, s, runner=k.runner,
                                                                                          now=k.clock))
    return k, call


def test_tool_template_and_set_local_asks_first_and_saves_the_override(tmp_path):
    k, call = tool_kit(tmp_path)
    _, out = call({"action": "template"})
    assert [t["name"] for t in out["templates"]] == ["solo-with-agents", "one-review-then-me", "staging-first"]
    args = {"action": "set", "repo": DEMO, "template": "one-review-then-me", "flow": "beginner", "target": "local"}
    directive, out = call(args)
    assert directive["action"] == "approve" and directive["message"].startswith(
        "This changes Forgebrücke's flows in this profile: saves the flow beginner")
    assert out["ok"] and (tmp_path / "flows" / "overrides" / "beginner.yml").exists()
    _, denied = call({**args, "flow": "other"}, approve=False)
    assert denied["error"] == "not_approved" and not (tmp_path / "flows" / "overrides" / "other.yml").exists()


def test_tool_set_repo_opens_a_branch_and_a_pr_never_a_push_to_main(tmp_path):
    k, call = tool_kit(tmp_path)
    directive, out = call({"action": "set", "repo": DEMO, "template": "solo-with-agents", "target": "repo"})
    assert "a pull request forgebruecke/flow-solo-with-agents → main (never a push to main)" in directive["message"]
    contents = [b for m, p, b in k.forge.writes if "/contents/" in p]
    assert contents[0]["new_branch"] == "forgebruecke/flow-solo-with-agents" and contents[0]["branch"] == "main"
    assert [b["base"] for m, p, b in k.forge.writes if p.endswith("/pulls")] == ["main"]
    assert out["ok"] and out["sent"]


def test_tool_set_is_refused_unattended_and_invalid_flows_never_write(tmp_path):
    k, call = tool_kit(tmp_path, run=approval.RunContext(cron=True))
    directive, _ = call({"action": "set", "repo": DEMO, "template": "solo-with-agents", "target": "local"})
    assert directive["action"] == "block" and "cron job" in directive["message"]
    k2, call2 = tool_kit(tmp_path / "b")
    bad = json.dumps({**FLOW, "gates": [{"promote": {"to": "main"}}]})
    directive, _ = call2({"action": "set", "repo": DEMO, "text": bad, "target": "local"})
    assert directive["action"] == "block" and "to: main is not allowed" in directive["message"]


def test_tool_run_and_status_show_the_gate_chain(tmp_path):
    k, call = tool_kit(tmp_path)
    _, out = call({"action": "run", "repo": DEMO, "number": 3})
    assert out["ok"] and [g["state"] for g in out["gates"]] == ["running", "pending", "pending"]
    _, st = call({"action": "status", "repo": DEMO, "number": 3})
    assert st["flow"] == "demo" and st["gates"][0]["gate"] == "review"
    _, paused = call({"action": "pause", "flow": "demo"})
    assert paused["paused"] == ["demo"]
    directive, resumed = call({"action": "resume", "flow": "demo"})
    assert directive["action"] == "approve" and resumed["paused"] == []


def test_list_profiles_reads_the_host_and_a_missing_review_profile_is_a_warning(tmp_path, monkeypatch):
    profile = sub("profile")
    sources = sub("flows.sources")
    (tmp_path / "config.yaml").write_text("x: 1")
    for name in ("coding", "devops"):
        (tmp_path / "profiles" / name).mkdir(parents=True)
        (tmp_path / "profiles" / name / "config.yaml").write_text("x: 1")
    (tmp_path / "profiles" / "broken").mkdir()
    assert profile.list_profiles(tmp_path / "profiles" / "coding") == ["coding", "default", "devops"]
    monkeypatch.setattr(profile, "current_home", lambda: tmp_path / "profiles" / "coding")
    flow, _, _ = models.validate_flow({**FLOW, "gates": [{"review": {"by": "nobody"}}, {"human": {}}]})
    assert "names the profile nobody, which does not exist here" in sources.profile_warnings(flow)[0]
    ok, _, _ = models.validate_flow({**FLOW, "gates": [{"review": {"by": "devops"}}, {"human": {}}]})
    assert sources.profile_warnings(ok) == []
