"""Every tool handler answers a failure it does not know with the exception's type, never its text:
urllib quotes a bad header value, and that value is the token."""
import json
import logging

import pytest

from fb_fakes import DEMO, sub
from fb_fakes_tools_read import BOT, approval, kit, plugin, repo

flows_tool, repos_tool, state_mod = sub("tools.flows"), sub("tools.repos"), sub("flows.state")
SECRET = "SECRETTAIL"


def failing(exc):
    def transport(method, url, headers, body, timeout):
        raise exc
    return transport


def handlers(tmp_path, transport):
    _, gs, sec = kit(settings={"dry_run": False})
    store = state_mod.FlowStore(tmp_path)
    run = approval.RunContext
    return {
        "forgejo_get": (plugin.make_read_handler("forge_get", gs, sec, transport=transport),
                        {"kind": "repo", "repo": DEMO}),
        "forgejo_list": (plugin.make_read_handler("forge_list", gs, sec, transport=transport), {"kind": "repos"}),
        "forgejo_doctor": (plugin.make_doctor_handler(gs, sec, transport=transport), {}),
        "forgejo_flows": (plugin.make_flows_handler(gs, lambda: store, run, flows_tool.make_spec(lambda: store), sec,
                                                    transport=transport), {"action": "get", "repo": DEMO}),
        "forgejo_repos": (plugin.make_repos_handler(gs, run, repos_tool.make_spec(), sec, transport=transport),
                          {"action": "overview", "repo": DEMO}),
        "forgejo_comment": (plugin.make_write_handler("forgejo_comment", gs, run, sec, transport=transport),
                            {"repo": DEMO, "number": 3, "body": "hi"}),
    }


TOOLS = ("forgejo_get", "forgejo_list", "forgejo_doctor", "forgejo_flows", "forgejo_repos", "forgejo_comment")


@pytest.mark.parametrize("tool", TOOLS)
def test_an_unknown_failure_answers_with_its_type_and_never_its_text(tool, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    handler, args = handlers(tmp_path, failing(RuntimeError(f"boom {SECRET}")))[tool]
    text = handler(args)
    assert json.loads(text) == {"ok": False, "error": "internal", "message": "RuntimeError"}
    assert SECRET not in text and SECRET not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("tool", [t for t in TOOLS if t != "forgejo_comment"])
def test_a_token_with_a_line_break_never_reaches_the_answer_or_the_log(tool, tmp_path, caplog):
    """What urllib raises before it connects when the stored token holds a line break."""
    caplog.set_level(logging.DEBUG)
    bad_header = ValueError(f"Invalid header value b'token {BOT}\\n{SECRET}'")
    handler, args = handlers(tmp_path, failing(bad_header))[tool]
    text = handler(args)
    assert json.loads(text) == {"ok": False, "error": "internal", "message": "ValueError"}
    assert SECRET not in text and SECRET not in caplog.text


def test_a_value_error_the_plugin_raised_itself_keeps_its_sentence(tmp_path):
    odd = "acme/a..b"
    forge, gs, sec = kit()
    forge.accounts[BOT]["repos"].append(repo(odd, "read"))
    store = state_mod.FlowStore(tmp_path)
    handler = plugin.make_flows_handler(gs, lambda: store, approval.RunContext, flows_tool.make_spec(lambda: store),
                                        sec, transport=forge)
    out = json.loads(handler({"action": "status", "repo": odd, "number": 1}))
    assert out["ok"] is False and out["error"] == "bad_args" and "unsafe repo name" in out["message"]
