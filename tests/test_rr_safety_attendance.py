"""Who is there to approve: a turn counts as unattended on cron, on the webhook, msgraph_webhook, api_server and
mention surfaces, with HERMES_SINGLE_QUERY_SESSION, on a gateway-admitted single-query turn, and inside Hermes'
plugin host when the surface is a command-line one. When this Hermes cannot say whether the turn is a
single-query one, writes are refused with a sentence that says so."""
import sys
import types

import pytest

from fb_fakes import sub
from fb_fakes_rr_safety import RecordingCtx

approval = sub("approval")


@pytest.fixture
def finite(monkeypatch):
    """Set what ``gateway.session_finite.finite_turn_required`` answers: True, False, an exception, or None for a
    Hermes without the module."""
    def set_answer(answer):
        if answer is None:
            monkeypatch.setitem(sys.modules, "gateway.session_finite", None)  # the import fails
            return
        module = types.ModuleType("gateway.session_finite")

        def finite_turn_required():
            if isinstance(answer, Exception):
                raise answer
            return answer
        module.finite_turn_required = finite_turn_required
        monkeypatch.setitem(sys.modules, "gateway.session_finite", module)
    for name in ("HERMES_CRON_SESSION", "HERMES_SINGLE_QUERY_SESSION", "HERMES_SESSION_PLATFORM",
                 "HERMES_PLUGIN_HOST_PROCESS"):
        monkeypatch.delenv(name, raising=False)
    return set_answer


def reason(surface="desktop"):
    return approval.run_context_from(RecordingCtx(surface=surface)).unattended_reason()


def test_an_attended_desktop_turn_may_ask(finite):
    finite(False)
    assert reason("desktop") is None and reason("cli") is None and reason("telegram") is None


def test_a_gateway_admitted_single_query_turn_is_unattended(finite):
    finite(True)
    assert "single-query" in reason("desktop")


@pytest.mark.parametrize("answer", [None, RuntimeError("moved")])
def test_a_hermes_that_does_not_say_refuses_writes_and_says_why(finite, answer):
    finite(answer)
    text = reason("desktop")
    assert "this Hermes version does not say" in text and "update Forgebrücke" in text
    refusal = approval.precheck(approval.run_context_from(RecordingCtx(surface="desktop")))
    assert refusal["error"] == "unattended" and "Nothing was sent" in refusal["message"]


@pytest.mark.parametrize("surface", ["webhook", "msgraph_webhook", "api_server", "forgebruecke"])
def test_unattended_surfaces(finite, surface):
    finite(False)
    assert reason(surface)


def test_cron_and_single_query_env(finite, monkeypatch):
    finite(False)
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert "cron" in reason()
    monkeypatch.delenv("HERMES_CRON_SESSION")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    assert "single-query" in reason()


@pytest.mark.parametrize("surface", ["", "cli", "oneshot"])
def test_in_the_plugin_host_a_command_line_turn_is_unattended(finite, monkeypatch, surface):
    finite(False)
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    text = reason(surface)
    assert text and "plugin host" in text and "plugins.isolation: in_process" in text


@pytest.mark.parametrize("surface", ["desktop", "tui", "telegram"])
def test_in_the_plugin_host_a_desktop_or_chat_turn_may_still_ask(finite, monkeypatch, surface):
    finite(False)
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    assert reason(surface) is None
