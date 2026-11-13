"""The gateway enables the mention channel only when the profile opted in, not on a token alone."""
import pytest

from fb_fakes import load_plugin, make_token, sub

plugin = load_plugin()
profile = sub("profile")

OPTED_IN = """\
plugins:
  enabled: [forgebruecke]
platforms:
  forgebruecke: {enabled: true}
platform_toolsets:
  forgebruecke: [forgebruecke]
"""
TOKEN_ONLY = "plugins:\n  enabled: [forgebruecke]\n"


@pytest.fixture
def register(tmp_path, monkeypatch):
    def go(config_yaml, token=make_token("bot")):
        (tmp_path / "config.yaml").write_text(config_yaml, encoding="utf-8")
        monkeypatch.setattr(profile, "current_home", lambda: tmp_path)
        monkeypatch.setattr(plugin, "_default_secret", lambda name: token)

        class Ctx:
            platform = None

            def register_platform(self, **kw):
                self.platform = kw

        ctx = Ctx()
        plugin._register_channel(ctx, None)
        return ctx.platform
    return go


def test_a_token_without_the_profiles_opt_in_does_not_enable_the_channel(register):
    entry = register(TOKEN_ONLY)
    assert entry["check_fn"]() is True  # the passive probe still sees the token
    assert entry["is_connected"](None) is False


def test_the_full_opt_in_enables_the_channel(register):
    assert register(OPTED_IN)["is_connected"](None) is True


def test_no_token_never_enables_the_channel(register):
    assert register(OPTED_IN, token="")["is_connected"](None) is False


def test_an_unreadable_config_does_not_enable_the_channel(register, tmp_path):
    entry = register(OPTED_IN)
    (tmp_path / "config.yaml").write_text("plugins: [unclosed", encoding="utf-8")
    assert entry["is_connected"](None) is False
