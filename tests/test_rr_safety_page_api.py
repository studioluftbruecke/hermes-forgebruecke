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


def test_a_failed_save_raises(tmp_path):
    api = load_api()
    with pytest.raises(RuntimeError):
        api.save_setting("dry_run", True, home=tmp_path, run=lambda *a, **k: types.SimpleNamespace(returncode=1))


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
