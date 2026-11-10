"""Edges of the write path: who counts as unattended, which lane a turn of unknown origin reads in, and how
a forge error during planning is answered."""
from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, sample_data, sub

approval = sub("approval")
access_mod = sub("access")
BOT = make_token("bot")
COMMENT = {"repo": DEMO, "number": 3, "body": "hi"}


class Ctx:
    def __init__(self, surface):
        self.surface = surface

    def current_cron_execution(self):
        return None

    def session_surface(self):
        if isinstance(self.surface, Exception):
            raise self.surface
        return self.surface


def test_a_review_worker_is_unattended_even_without_its_single_query_flag():
    worker = approval.RunContext(platform="cli", source=approval.REVIEW_SOURCE)
    assert "review worker" in worker.unattended_reason()
    assert approval.precheck(worker)["error"] == "unattended"
    assert approval.RunContext(platform="cli", source="cli").unattended_reason() is None


def test_a_turn_whose_surface_cannot_be_read_reads_nothing_and_writes_nothing(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    run = approval.run_context_from(Ctx(RuntimeError("moved")))
    assert run.lane() == ("mention", "")
    assert "cannot tell whether a person is present" in run.unattended_reason()
    assert approval.run_context_from(Ctx("cli")).lane() == ("agent", "")


def test_a_surface_hermes_names_in_the_session_env_is_still_an_origin(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    assert approval.run_context_from(Ctx(RuntimeError("moved"))).lane() == ("agent", "")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "forgebruecke")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", f"{DEMO}#3")
    assert approval.run_context_from(Ctx(RuntimeError("moved"))).lane() == ("mention", DEMO)


def test_a_forge_error_while_planning_a_write_answers_with_the_forges_kind():
    account = bot_account()
    account["scopes"] = {"write:issue"}
    forge = FakeForgejo({BOT: account}, data=sample_data())
    values = {"url": BASE, "dry_run": False}

    def load():
        return access_mod.load_access(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                                      transport=forge)

    tickets = approval.ApprovalTickets(clock=lambda: 0.0)
    key = tickets.key("forgejo_comment", "s", COMMENT)
    tickets.issue(key, ("approved earlier",), f"forgebruecke:forgejo_comment:{DEMO}")
    out = approval.run_write("forgejo_comment", COMMENT, "s", load, approval.RunContext(), tickets)
    assert (out["ok"], out["error"], out["sent"]) == (False, "missing_scope", False)
    assert "read:repository" in out["message"] and not forge.writes
    assert tickets.take(key) is None
