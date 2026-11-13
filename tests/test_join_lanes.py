"""Behaviour that only exists where two halves meet: the engine with the real review runner, the engine
with a rate-limiting forge, the agent's desk list with the desk's own sentences."""
import json
import subprocess

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, sample_data, sub
from fb_fakes_engine import Kit, engine_env  # noqa: F401
from fb_fakes_rr_engine import review_home

runner_mod = sub("flows.runner")
approval = sub("approval")
access_mod = sub("access")
BOT = make_token("bot")
ATTENDED = approval.RunContext()


class RestartedRunner(runner_mod.ReviewRunner):
    """The real ``alive`` and ``stop``. ``start`` leaves what a worker of an earlier gateway process left: a
    pid record whose start time is not the one that pid has today."""

    def __init__(self, pid: int, home) -> None:
        super().__init__(home=home)
        self.pid = pid

    def start(self, *, profile, prompt_path, log_path):
        log_path.with_suffix(".pid").write_text(json.dumps({"pid": self.pid, "started": 1}), encoding="utf-8")
        return self.pid


def test_a_pid_another_process_reused_is_never_signalled_by_the_engine(tmp_path):
    bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        k = Kit(tmp_path)
        k.runner = RestartedRunner(bystander.pid, review_home(tmp_path / "root"))
        k.tick()
        assert k.gates()["review"]["state"] == "running"
        k.clock.t += 5 * 60
        k.tick()
        k.clock.t += 60 * 60
        k.tick()
        assert bystander.poll() is None
        assert k.gates()["review"]["state"] != "running"
    finally:
        bystander.kill()
        bystander.wait(timeout=5)


def test_a_rate_limited_forge_stops_the_tick_at_the_first_pull_request(tmp_path):
    k = Kit(tmp_path, flow={"flow": "demo", "repos": [DEMO], "gates": [
        {"check": {"contexts": ["devops/tests"]}}, {"human": {}}]})
    for n in (4, 5, 6):
        k.add_pr(n)
    k.transport.fail(lambda method, path: method == "GET" and path.endswith("/status"), status=429,
                     message="slow down")
    report = k.tick()
    status_reads = [hit for hit in k.transport.hits if hit[1].endswith("/status")]
    assert len(status_reads) <= 4  # one pull request's read and its retries, not one per pull request
    assert report["prs"] == [] and len(report["errors"]) == 1
    assert "rate limiting" in report["errors"][0] and "the next one goes on" in report["errors"][0]


class WriteKit:
    """The hook and the handler around one fake forge, as Hermes runs them for a write."""

    def __init__(self, forge_class=FakeForgejo):
        data = sample_data()
        data[DEMO]["branches"]["fix-mul"] = {"name": "fix-mul", "commit": {"id": "2" * 40}}
        self.forge = forge_class({BOT: bot_account()}, data=data)
        self.values = {"url": BASE, "dry_run": False}
        self.tickets = approval.ApprovalTickets()
        self.gate = approval.make_gate(self.load, lambda: ATTENDED, self.tickets)

    def load(self):
        return access_mod.load_access(lambda k, d=None: self.values.get(k, d),
                                      {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get, transport=self.forge)

    def write(self, tool, args):
        return approval.run_write(tool, args, "s1", self.load, ATTENDED, self.tickets)


def test_a_default_branch_that_moved_after_the_approval_stops_the_pull_request():
    k = WriteKit()
    args = {"repo": DEMO, "head": "fix-mul", "title": "Fix mul"}
    assert k.gate(tool_name="forgejo_create_pr", args=args, session_id="s1")["action"] == "approve"
    k.forge.data[DEMO]["info"]["default_branch"] = "production"
    out = k.write("forgejo_create_pr", args)
    assert out["error"] == "not_approved" and "changed between" in out["message"]
    assert out["sent"] is False and not k.forge.writes


class AnswersWithAList(FakeForgejo):
    """A forge, or a proxy before it, that answers the comment POST with 200 and a list: nothing was created."""

    def __call__(self, method, url, headers, body, timeout):
        if method == "POST" and url.endswith("/issues/3/comments"):
            return self._json(200, [{"id": 10, "body": "an older comment"}])
        return super().__call__(method, url, headers, body, timeout)


def test_a_write_answered_without_the_new_object_is_not_reported_as_done():
    k = WriteKit(AnswersWithAList)
    args = {"repo": DEMO, "number": 3, "body": "Looks right now."}
    assert k.gate(tool_name="forgejo_comment", args=args, session_id="s1")["action"] == "approve"
    out = k.write("forgejo_comment", args)
    assert out["ok"] is False and out["sent"] is True and out["error"] == "bad_answer"
    assert out["message"] == f"Forgejo answered 200 without the new comment. Check {DEMO} before you repeat it."


def test_an_address_nothing_answers_at_is_named_in_the_doctors_next_step():
    def nobody_home(method, url, headers, body, timeout):
        raise sub("client").ForgejoError(None, "unreachable", "The forge is unreachable ([Errno -2] Name or service "
                                         "not known); nothing was changed.")

    plugin = sub("__init__")
    report = plugin.doctor_report(lambda k, d=None: {"url": "https://forgejo.example.invalid"}.get(k, d),
                                  {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get, transport=nobody_home)
    assert report["next_action"] == {"id": "set_url", "text": "Hermes cannot reach https://forgejo.example.invalid. "
                                                              "Check your Forgejo address."}


def test_a_tick_that_died_after_the_start_takes_the_real_runners_worker_over(tmp_path):
    from fb_fakes_engine import Stopped

    workers = []

    def popen(command, **kw):
        workers.append(subprocess.Popen(["sleep", "60"], stdin=kw.get("stdin"), stdout=kw.get("stdout"),
                                        stderr=kw.get("stderr"), start_new_session=True))
        return workers[-1]

    k = Kit(tmp_path)
    k.runner = runner_mod.ReviewRunner(popen=popen, home=review_home(tmp_path / "root"))
    start = k.runner.start

    def start_then_die(**kw):
        start(**kw)
        raise Stopped()

    try:
        k.runner.start = start_then_die
        try:
            k.tick()
            raise AssertionError("the tick was meant to stop after the start")
        except Stopped:
            pass
        assert k.gates()["review"]["state"] == "pending" and len(workers) == 1  # no saved state knows the worker
        k.runner.start = start
        k.tick()
        review = k.gates()["review"]
        assert len(workers) == 1 and review["state"] == "running" and review["evidence"]["pid"] == workers[0].pid
    finally:
        for worker in workers:
            worker.kill()
            worker.wait(timeout=5)


def test_the_doctor_and_the_page_name_the_same_next_step_for_a_token_without_write_scopes():
    plugin, desk = sub("__init__"), sub("desk")
    forge = FakeForgejo({BOT: {**bot_account(), "scopes": {"read:repository", "read:user", "read:notification"}}})
    values = {"url": BASE, "you": "acme", "dry_run": False}
    report = plugin.doctor_report(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                                  transport=forge)
    assert report["ok"] is False and report["next_action"]["id"] == "fix_scopes"
    assert "write:issue" in report["next_action"]["text"] and report["next_step"] != "Ready."

    class Settings:
        you, dry_run = "acme", False

    assert desk.next_action(report, Settings())["text"] == report["next_action"]["text"]
