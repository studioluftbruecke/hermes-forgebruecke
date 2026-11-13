"""A mention turn starts on a Forgejo commenter's text, so its toolset may not reach the host: the channel refuses
to connect while `platform_toolsets.forgebruecke` holds a tool or bundle that runs commands, writes files or
starts other agents or jobs, and the doctor says why. Hermes' command-approval prompt is never posted into the
public thread."""
import asyncio
import sys

import pytest

from fb_fakes import sub
from fb_fakes_workers import gateway_stubs

profile = sub("profile")


def config(toolsets):
    return {"plugins": {"enabled": ["forgebruecke"]}, "platforms": {"forgebruecke": {"enabled": True}},
            "platform_toolsets": {"forgebruecke": toolsets}}


@pytest.mark.parametrize("toolsets", [
    ["hermes-cli"], ["forgebruecke", "terminal"], ["file"], ["code_execution"], ["coding"], ["debugging"],
    ["forgebruecke", "execute_code"], ["write_file"], ["patch"], ["process_manage"], ["delegation"], ["cronjob"],
    ["hermes-gateway"], ["browser"], ["memory"], ["forgebruecke", "session_search"],
])
def test_a_mention_toolset_that_reaches_the_host_keeps_the_channel_off(toolsets):
    blockers = profile.channel_blockers(config(toolsets), "coding")
    assert blockers and any("platform_toolsets.forgebruecke" in b for b in blockers)


@pytest.mark.parametrize("toolsets", [["forgebruecke"], ["forgebruecke", "web"], ["forgebruecke", "vision", "clarify"],
                                      ["hermes-webhook"]])
def test_a_read_only_mention_toolset_connects(toolsets):
    assert profile.channel_blockers(config(toolsets), "coding") == []


def test_the_command_approval_prompt_is_declined_not_posted(monkeypatch, tmp_path):
    for name, module in gateway_stubs().items():
        monkeypatch.setitem(sys.modules, name, module)
    adapter_mod = sub("channel.adapter")
    try:
        import types
        adapter = adapter_mod.make_adapter(types.SimpleNamespace(extra={}), home=tmp_path, data_dir=tmp_path / "d",
                                           get_secret=lambda name: None)
        posted = []
        adapter.send = lambda *a, **k: posted.append(a)  # nothing may reach the thread
        result = asyncio.run(adapter.send_exec_approval(chat_id="acme/demo#1", command="rm -rf " + "x" * 5000,
                                                        session_key="k", description="d", request_id="r"))
        assert not result.success and posted == []
        assert result.raw_response["code"] == "egress_declined"
        assert type(adapter)._send_exec_approval_prompt is not adapter_mod.BasePlatformAdapter.__dict__.get(
            "_send_exec_approval_prompt")  # Hermes routes the prompt to the adapter only when it overrides this
    finally:
        sys.modules.pop(adapter_mod.__name__, None)
