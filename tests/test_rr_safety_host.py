"""Under `plugins.isolation: host` Hermes refuses a platform adapter. The plugin then loads without its mention
channel: every tool and hook is registered, and the log says the channel needs the in-process mode."""
import logging

from fb_fakes import DictState, sub
from fb_fakes_rr_safety import RecordingCtx

plugin = sub("__init__")


class PluginHostUnsupported(RuntimeError):
    """What Hermes' plugin host raises for a ctx method it cannot carry across."""


def test_register_loads_without_the_channel_in_the_plugin_host(tmp_path, caplog):
    ctx = RecordingCtx(DictState(tmp_path), platform_error=PluginHostUnsupported(
        "ctx.register_platform() cannot be called across the plugin host"))
    with caplog.at_level(logging.INFO):
        plugin.register(ctx)
    assert len(ctx.tools) == 8 and ctx.platforms == {}
    assert set(ctx.hooks) == {"pre_tool_call", "post_approval_response", "transform_llm_output"}
    assert any("plugins.isolation" in r.getMessage() for r in caplog.records)
