"""The page's routes use only public Hermes: the data folder name is restated (and held equal to Hermes' own
here), and a setting is saved with Hermes' own `hermes config set` into the profile's config.yaml."""
import importlib.util
import os
import sys
import types

import pytest

from fb_fakes import ROOT

SOURCE = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")


def load_api():
    spec = importlib.util.spec_from_file_location("fb_plugin_api_rr_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def test_no_private_hermes_import_is_left_in_the_routes():
    assert "hermes_cli.plugins_manifest" not in SOURCE and "hermes_cli.plugins_state" not in SOURCE


def test_the_data_folder_name_is_hermes_own():
    manifest = pytest.importorskip("hermes_cli.plugins_manifest")
    api = load_api()
    for key in ("forgebruecke", "Some Plugin.x", "--", "über"):
        assert api.data_namespace(key) == manifest._portable_data_namespace(key)


def test_a_setting_is_saved_with_hermes_config_set(tmp_path):
    pytest.importorskip("hermes_cli.main")
    import subprocess
    api = load_api()
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: [forgebruecke]\n", encoding="utf-8")

    def run(argv, **kw):
        kw["env"] = {**kw["env"], "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
        return subprocess.run(argv, **kw)
    api.save_setting("dry_run", False, home=tmp_path, run=run)
    api.save_setting("you", "kay", home=tmp_path, run=run)
    import hermes_yaml
    saved = hermes_yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["plugins"]["entries"]["forgebruecke"]["settings"] == {"dry_run": False, "you": "kay"}


def test_a_name_hermes_would_read_as_a_value_is_refused_before_anything_runs(tmp_path):
    api = load_api()
    ran = []
    for name in ("123", "on", "null", "1e5"):
        with pytest.raises(PermissionError):
            api.save_setting("you", name, home=tmp_path, run=lambda *a, **k: ran.append(a))
    assert ran == []


def test_arbitrary_cli_output_becomes_a_stable_generic_refusal(tmp_path):
    api = load_api()
    done = types.SimpleNamespace(returncode=1, stdout="preparing dependencies\n",
                                 stderr="an arbitrary startup or dependency error\n")
    with pytest.raises(PermissionError) as refused:
        api.save_setting("dry_run", True, home=tmp_path, run=lambda *a, **k: done)
    assert str(refused.value) == "Hermes did not save the setting; the profile's config holds another value"
    assert "preparing" not in str(refused.value) and "arbitrary" not in str(refused.value)


@pytest.mark.parametrize("output,returncode,message", [
    ("Cannot set configuration values: this Hermes installation is managed by nix.", 0,
     "Plugin settings cannot be changed in a managed install"),
    ("Plugin setting 'plugins.entries.forgebruecke.settings.dry_run' is administrator-managed", 1,
     "This plugin setting is managed by your Hermes administrator"),
])
def test_known_cli_refusals_have_plugin_owned_messages(tmp_path, output, returncode, message):
    api = load_api()
    done = types.SimpleNamespace(returncode=returncode, stdout="", stderr=output)
    with pytest.raises(PermissionError) as refused:
        api.save_setting("dry_run", True, home=tmp_path, run=lambda *a, **k: done)
    assert str(refused.value) == message


def test_an_exit_zero_without_the_requested_config_change_is_not_success(tmp_path):
    api = load_api()
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if "set" in argv:
            return types.SimpleNamespace(returncode=0, stdout="preparing dependencies\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="true\n", stderr="")

    with pytest.raises(PermissionError) as refused:
        api.save_setting("dry_run", False, home=tmp_path, run=run)
    assert str(refused.value) == "Hermes did not save the setting; the profile's config holds another value"
    assert len(calls) == 2 and calls[1][-3:] == ["get", "plugins.entries.forgebruecke.settings.dry_run", "--json"]


def test_a_real_managed_refusal_is_not_success_and_changes_nothing(tmp_path):
    pytest.importorskip("hermes_cli.main")
    import subprocess
    api = load_api()
    for name in ("cron", "sessions", "logs", "memories"):
        (tmp_path / name).mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("plugins:\n  enabled: [forgebruecke]\n", encoding="utf-8")
    (tmp_path / ".managed").write_text("nix\n", encoding="utf-8")
    before = config.read_text(encoding="utf-8")

    def run(argv, **kw):
        kw["env"] = {**kw["env"], "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
        return subprocess.run(argv, **kw)

    with pytest.raises(PermissionError) as refused:
        api.save_setting("dry_run", False, home=tmp_path, run=run)
    assert str(refused.value) == "Plugin settings cannot be changed in a managed install"
    assert config.read_text(encoding="utf-8") == before


def test_the_profile_config_is_read_with_hermes_own_reader(tmp_path):
    pytest.importorskip("hermes_cli.config")
    from fb_fakes import sub
    profile = sub("profile")
    assert "hermes_yaml" not in (ROOT / "profile.py").read_text(encoding="utf-8")
    (tmp_path / "config.yaml").write_text("approvals:\n  mode: off\nplugins:\n  enabled: [forgebruecke]\n",
                                          encoding="utf-8")
    config = profile.read_config(tmp_path)
    assert config["approvals"]["mode"] is False and profile.enabled_in(config)
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    assert not profile.enabled_in(profile.read_config(tmp_path))  # read fresh, no stale cache
