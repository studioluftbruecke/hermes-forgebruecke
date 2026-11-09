"""forgejo_doctor against a fake Forgejo that enforces scopes like 15.0.11."""
import json

from fb_fakes import BASE, DictState, FakeForgejo, bot_account, load_plugin, make_token, owner_account, repo, sub

plugin = load_plugin()
doctor = sub("tools.doctor")
URL = {"url": BASE}


def getter(values: dict):
    return lambda key, default=None: values.get(key, default)


def run(accounts, secrets, settings=None, **forge_kw):
    forge = FakeForgejo(accounts, **forge_kw)
    report = plugin.doctor_report(getter({**URL, **(settings or {})}), secrets.get, transport=forge)
    return report, forge


def feature(report, fid):
    return next(f for f in report["features"] if f["id"] == fid)


def test_bot_only_setup_names_identity_scopes_and_dark_features():
    bot = make_token("bot")
    report, forge = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot})
    assert report["ok"] is True
    assert report["instance"]["version"].startswith("15.0.11") and report["instance"]["supported"]
    t = report["tokens"][0]
    assert t["identity"] == "forgebruecke-bot" and t["repos"]["visible"] == 1 and t["repos"]["write"] == 1
    assert t["scopes"] == {"read:user": "yes", "read:repository": "yes", "read:issue": "yes",
                           "read:notification": "yes", "read:organization": "no", "write:issue": "yes",
                           "write:repository": "yes", "write:notification": "no", "write:user": "no"}
    assert report["tokens"][1] == {"role": "owner", "env": "FORGEBRUECKE_PERSONAL_WRITE_TOKEN", "set": False,
                                   "note": "optional: a write token of your own account, only for the desk's Merge button"}
    assert len(report["tokens"]) == 2
    assert feature(report, "protection_view")["status"] == "dark"
    assert feature(report, "mentions")["status"] == "available"  # on by default (D31), no rule needed
    assert feature(report, "writes")["status"] == "off"  # dry_run
    assert feature(report, "mark_read")["status"] == "dark"
    assert feature(report, "flows")["status"] == "off"  # capable, but no rule lists a flow
    assert feature(report, "audit")["status"] == "dark"  # the agent is admin nowhere: the setting alone would not light it
    assert report["next_step"].startswith("Ready.")


def test_doctor_writes_nothing():
    bot = make_token("bot")
    _, forge = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot})
    writes = [c for c in forge.calls if c[0] != "GET"]
    # Only the scope probes write, and each one is a request the forge rejects.
    assert {(m, p.rsplit("/", 1)[-1]) for m, p, _, _ in writes} == {
        ("POST", "issues"), ("POST", "pulls"), ("PATCH", "999999999"), ("POST", "repos")}
    assert all(b in (b"{}", None) for *_, b in writes)


def test_admin_right_of_the_agent_account_lights_protection_and_audit_and_the_read_token_is_ignored():
    bot, read = make_token("bot"), make_token("read")
    agent = bot_account()
    agent["repos"] = agent["repos"] + [repo("forgebruecke-bot/probe", "admin")]
    report, _ = run({bot: agent, read: owner_account()},
                    {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot, "FORGEBRUECKE_PERSONAL_READ_TOKEN": read},
                    settings={"audit_enabled": True})
    assert [t["role"] for t in report["tokens"]] == ["bot", "owner"]
    b = report["tokens"][0]
    assert b["repos"]["admin"] == 1 and b["protection"]["readable"] is True
    assert feature(report, "protection_view")["status"] == "available"
    assert "forgebruecke-bot/probe" in feature(report, "protection_view")["reason"]
    assert "its own Forgejo account" in feature(report, "read")["reason"]
    assert feature(report, "audit")["status"] == "available"


def test_rules_and_dry_run_switch_features_on():
    bot = make_token("bot")
    rules = [{"repo_glob": "acme/forgebruecke-demo", "mentions": True}]
    report, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}, settings={"writes": rules})
    assert feature(report, "mentions")["status"] == "available"
    assert feature(report, "writes")["status"] == "off" and "dry_run" in feature(report, "writes")["reason"]
    report2, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot},
                     settings={"writes": rules, "dry_run": False})
    assert feature(report2, "writes")["status"] == "available"


def test_missing_url_stops_before_any_request():
    forge = FakeForgejo({})
    report = plugin.doctor_report(getter({}), {}.get, transport=forge)
    assert report["ok"] is False and "url" in report["next_step"] and forge.calls == []


def test_missing_token_says_which_secret_to_store():
    report, _ = run({}, {})
    assert report["ok"] is False
    assert report["tokens"][0] == {"role": "bot", "env": "FORGEBRUECKE_BOT_ACCOUNT_TOKEN", "set": False}
    assert "FORGEBRUECKE_BOT_ACCOUNT_TOKEN" in report["next_step"]


def test_rejected_token_is_explained():
    report, _ = run({}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": make_token("stale")})
    t = report["tokens"][0]
    assert report["ok"] is False and t["error"]["kind"] == "token_rejected"
    assert report["next_step"].startswith("Fix the agent account token: Token rejected")


def test_unsupported_version_is_refused_with_a_sentence():
    bot = make_token("bot")
    report, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}, version="6.0.1+gitea-1.21.0")
    assert report["ok"] is False and "Upgrade the forge" in report["next_step"]


def test_a_forge_without_notifications_hides_the_channel():
    bot = make_token("bot")
    from fb_fakes import SWAGGER
    swagger = {"paths": {k: v for k, v in SWAGGER["paths"].items() if k != "/notifications"}}
    report, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}, swagger=swagger)
    assert feature(report, "mentions")["status"] == "dark"
    assert "GET /notifications" in feature(report, "mentions")["reason"]


def test_register_wires_the_tool_the_cli_and_the_state(tmp_path):
    class Ctx:
        def __init__(self):
            self.tools, self.cli, self.hooks, self.platforms = {}, {}, {}, {}
            self.state = DictState(tmp_path)

        def get_config(self, key, default=None):
            return {"url": BASE}.get(key, default)

        def register_tool(self, **kw):
            self.tools[kw["name"]] = kw

        def register_cli_command(self, **kw):
            self.cli[kw["name"]] = kw

        def register_hook(self, name, callback):
            self.hooks.setdefault(name, []).append(callback)

        def register_platform(self, **kw):
            self.platforms[kw["name"]] = kw

    ctx = Ctx()
    plugin.register(ctx)
    assert set(ctx.tools) == {"forgejo_doctor", "forgejo_get", "forgejo_list", "forgejo_comment",
                              "forgejo_create_pr", "forgejo_status", "forgejo_flows", "forgejo_repos"}
    assert set(ctx.cli) == {"forgebruecke"} and set(ctx.hooks) == {"pre_tool_call", "post_approval_response",
                                                                         "transform_llm_output"}
    assert len(ctx.hooks["pre_tool_call"]) == 2  # the write gate and the guardrail guard
    channel = ctx.platforms["forgebruecke"]
    assert channel["display_tier"] == "minimal" and channel["required_env"] == ["FORGEBRUECKE_BOT_ACCOUNT_TOKEN"]
    assert "only write" in channel["platform_hint"]
    assert ctx.state.data["version"] == 1
    out = json.loads(ctx.tools["forgejo_doctor"]["handler"]({}))
    assert "next_step" in out or "error" in out


def test_cli_text_lists_every_feature():
    bot = make_token("bot")
    report, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot})
    text = doctor.format_text(report)
    assert "forgebruecke-bot" in text and text.count("    [") == len(report["features"])
    assert bot not in text
