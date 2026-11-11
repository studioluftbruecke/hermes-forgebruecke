"""Settings: defaults, validation with warnings, per-repo rules, secrets that never show."""
import json
import logging

import pytest

from fb_fakes import DictState, FakeForgejo, bot_account, load_plugin, make_token, sub

settings = sub("settings")
state = sub("state")


def getter(values: dict):
    return lambda key, default=None: values.get(key, default)


def test_defaults_are_dry_run_with_mentions_on_and_no_flows():
    s = settings.load_settings(getter({}))
    assert s.warnings == ()
    assert s.dry_run is True and s.audit_enabled is False and s.learn_mode is False
    assert s.you == "" and s.poll_interval == 30
    rule = s.rule_for("acme/anything")
    assert (rule.mentions, rule.flows, rule.targets) == (True, (), ())
    assert not hasattr(rule, "write") and not hasattr(s, "read_scope") and not hasattr(s, "badge_meaning")


def test_retired_settings_and_rule_keys_are_ignored_with_a_warning_never_an_error():
    s = settings.load_settings(getter({"read_scope": "rules", "badge_meaning": "open_prs", "writes": [
        {"repo_glob": "acme/x", "read": True, "comment": True, "write": False, "flows": ["demo"]}]}))
    text = " ".join(s.warnings)
    assert "read_scope no longer applies" in text and "badge_meaning no longer applies" in text
    assert "writes[0]: read, comment, write no longer apply" in text and "Collaborators" in text
    assert s.rule_for("acme/x").flows == ("demo",) and s.rule_for("acme/x").mentions is True
    assert "read_scope" not in s.public() and "you" in s.public()


def test_you_is_a_forgejo_login():
    assert settings.load_settings(getter({"you": "@acme"})).you == "acme"
    s = settings.load_settings(getter({"you": "not a login!"}))
    assert s.you == "" and "you:" in s.warnings[0]


def test_poll_interval_is_clamped_or_defaulted():
    assert settings.load_settings(getter({"poll_interval": 5})).poll_interval == 30
    assert settings.load_settings(getter({"poll_interval": 99999})).poll_interval == 3600
    s = settings.load_settings(getter({"poll_interval": "often"}))
    assert s.poll_interval == 30 and "poll_interval" in s.warnings[0]
    assert settings.load_settings(getter({"poll_interval": True})).poll_interval == 30


def test_rules_parse_and_match_case_insensitively():
    s = settings.load_settings(getter({"writes": [
        {"repo_glob": "acme/forgebruecke-*", "mentions": False, "flows": ["solo"]},
        {"repo_glob": "not a glob"},
        {"repo_glob": "acme/x", "mentions": "maybe", "colour": "red"},
        "acme/y",
    ]}))
    assert len(s.writes) == 2
    r = s.rule_for("ACME/Forgebruecke-Demo")
    assert r.mentions is False and r.flows == ("solo",)
    text = " ".join(s.warnings)
    assert "writes[1]" in text and "writes[2].mentions" in text and "colour" in text and "writes[3]" in text


def rules(writes):
    return settings.load_settings(getter({"writes": writes}))


@pytest.mark.parametrize("value,expected", [("false", False), ("off", False), ("0", False), ("yes", True), (" True ", True)])
def test_mentions_reads_the_words_a_config_set_stores(value, expected):
    s = rules([{"repo_glob": "acme/x", "mentions": value}])
    assert s.rule_for("acme/x").mentions is expected and s.warnings == ()


def test_an_unreadable_mentions_value_switches_mentions_off_for_that_rule_and_says_so():
    s = rules([{"repo_glob": "acme/x", "mentions": "maybe"}, {"repo_glob": "acme/y", "mentions": 0}])
    assert s.rule_for("acme/x").mentions is False and s.rule_for("acme/y").mentions is False
    assert s.rule_for("acme/other").mentions is True
    assert all("mentions are off for this rule" in w for w in s.warnings) and len(s.warnings) == 2


def test_one_rule_written_as_a_mapping_and_a_bare_star_glob_still_switch_mentions_off():
    s = rules({"repo_glob": "acme/*", "mentions": False})
    assert s.rule_for("acme/secret").mentions is False and s.warnings == ()
    s = rules([{"repo_glob": "*", "mentions": False}])
    assert s.rule_for("acme/secret").mentions is False and s.warnings == ()
    assert s.public()["writes"][0]["repo_glob"] == "*/*"


def test_a_rule_that_cannot_be_read_says_where_mentions_are_off_now():
    s = rules([{"repo_glob": "acme", "mentions": False}, "acme/y"])
    assert s.writes == () and len(s.warnings) == 2
    assert all("mentions are off wherever no earlier rule applies" in w and len(w) <= 140 for w in s.warnings)
    assert "mentions are off in every repo" in rules("acme/x").warnings[0]
    assert s.rule_for("acme/y").mentions is False and rules("acme/x").rule_for("acme/y").mentions is False


def test_url_and_paths_are_validated():
    s = settings.load_settings(getter({"url": "forge.example.test", "flows_path": "../etc/x",
                                       "default_review_profile": "Dev Ops!"}), lambda: "")
    assert s.url == "" and s.flows_path == ".forgebruecke/flows.yml" and s.default_review_profile == ""
    assert len(s.warnings) == 3 and "using this profile" in s.warnings[2]


def test_an_address_with_a_user_name_or_password_is_refused_and_never_shown():
    secret = make_token("pw")
    for url in (f"https://bot:{secret}@forge.example.test", f"http://{secret}@forge.example.test:3000/",
                f"bot:{secret}@forge.example.test"):
        s = settings.load_settings(getter({"url": url}))
        assert s.url == "" and len(s.warnings) == 1 and "user name or password" in s.warnings[0]
        assert secret not in s.warnings[0] + json.dumps(s.public())
    s = settings.load_settings(getter({"url": "https://forge.example.test/@team/"}))
    assert s.url == "https://forge.example.test/@team" and s.warnings == ()


def test_load_settings_never_raises_on_values_yaml_can_produce():
    s = settings.load_settings(getter({"poll_interval": float("inf")}))
    assert s.poll_interval == 30 and "poll_interval" in s.warnings[0]
    s = settings.load_settings(getter({"writes": [{"repo_glob": "a/b", "flows": ["demo"], True: 1, 7: 2}]}))
    assert s.explicit_rule("a/b").flows == ("demo",) and "unknown keys 7, True ignored" in s.warnings[0]


def test_the_review_profile_defaults_to_the_profile_forgebruecke_runs_in():
    """No studio profile name as a default: empty means this profile, an explicit name wins."""
    assert settings.load_settings(getter({}), lambda: "coding").default_review_profile == "coding"
    assert settings.load_settings(getter({"default_review_profile": ""}), lambda: "work").default_review_profile == "work"
    assert settings.load_settings(getter({"default_review_profile": "devops"}), lambda: "coding").default_review_profile == "devops"
    assert settings.load_settings(getter({"default_review_profile": "Dev Ops!"}), lambda: "coding").default_review_profile == "coding"
    assert settings.DEFAULTS["default_review_profile"] == ""


def test_a_broken_config_read_gives_defaults():
    def boom(key, default=None):
        raise RuntimeError("config backend down")

    assert settings.load_settings(boom, lambda: "") == settings.Settings()


def test_credentials_strip_and_never_print():
    tok = make_token("bot")
    c = settings.load_credentials(lambda n: {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": f"  {tok}\n",
                                             "FORGEBRUECKE_PERSONAL_READ_TOKEN": tok}.get(n))
    assert c.token == tok and c.owner_token is None and not hasattr(c, "read_token")
    assert tok not in repr(c) and tok not in str(c) and "set" in repr(c)
    assert c.presence() == {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": True, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": False}


def test_a_token_no_header_can_carry_is_dropped_and_named_without_its_value():
    tok = make_token("bot")
    broken, foreign = f"{tok}\nSECRETTAIL", f"{tok}\u00e9"
    c = settings.Credentials(broken, foreign)
    assert c.token is None and c.owner_token is None
    assert c.presence() == {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": False, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": False}
    assert c.problems() == ["Hermes' token contains a space or line break. Enter it again.",
                            "Your write token contains a character no token has. Enter it again."]
    assert tok not in " ".join(c.problems()) + repr(c)
    assert settings.Credentials(f" {tok}\n", None).problems() == []
    assert settings.Credentials(None, "").problems() == []


def test_secret_is_never_logged_or_returned(caplog, tmp_path):
    tok = make_token("bot")
    plugin = load_plugin()
    forge = FakeForgejo({tok: bot_account()})
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": tok}
    caplog.set_level(logging.DEBUG)
    report = plugin.doctor_report(getter({"url": "https://forge.example.test", "poll_interval": "x"}),
                                  secrets.get, transport=forge, state=DictState(tmp_path))
    dumped = json.dumps(report)
    assert report["ok"] is True
    assert tok not in dumped and tok not in caplog.text
    assert "token" not in json.dumps(settings.load_settings(getter({})).public()).lower().replace("forgebruecke", "")


def test_state_store_keys_and_seen_cap(tmp_path):
    kv = DictState(tmp_path)
    store = state.StateStore(kv)
    assert store.ensure() == 0
    assert kv.data["version"] == state.SCHEMA_VERSION
    assert set(kv.data) >= {"threads", "seen", "gates", "claims", "repos", "capabilities"}
    assert store.ensure() == state.SCHEMA_VERSION
    store.remember_seen(f"c{i}" for i in range(450))
    kept = store.remember_seen(f"c{i}" for i in range(400, 600))
    assert len(kept) == 500 and kept[0] == "c100" and kept[-1] == "c599"
    assert store.runs_dir().is_dir() and store.runs_dir().parent == tmp_path


def test_the_key_fields_in_the_manifest_match_the_token_registry():
    import re
    from fb_fakes import ROOT
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    secrets = re.findall(r'^  (\w+):\n    type: secret\n    env: (\w+)\n    label: "([^"]+)"', manifest, re.M)
    assert len(secrets) == 2
    registry = sub("capabilities").TOKENS
    assert secrets == [(t["key"], t["env"], t["label"]) for t in registry]
    assert (settings.TOKEN_ENV, settings.OWNER_TOKEN_ENV) == tuple(t["env"] for t in registry)
    assert all(env.startswith("FORGEBRUECKE_") for _k, env, _l in secrets)


def test_the_manifest_drops_the_retired_fields_adds_you_and_says_the_write_tokens_risk():
    import re
    from fb_fakes import ROOT
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    keys = re.findall(r"^  (\w+):\n", manifest, re.M)
    assert "you" in keys and not {"read_token", "read_scope", "badge_meaning"} & set(keys)
    assert "PERSONAL_READ_TOKEN" not in manifest
    owner = manifest.split("  owner_token:")[1].split("\n  poll_interval:")[0]
    assert "; it is also exposed to Hermes, ultimately giving Hermes full access to your account." in owner
    owner_row = next(t for t in sub("capabilities").TOKENS if t["role"] == "owner")
    assert "full access to your account" in owner_row["risk"]
    version = sub("version").VERSION
    assert version == "0.0.21"
    assert re.search(r'^version: "' + re.escape(version) + '"$', manifest, re.M)
    assert '"version": "' + version + '"' in (ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8")
