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
                                   "note": "Optional. Only for the desk's Merge and Comment buttons."}
    assert len(report["tokens"]) == 2
    assert feature(report, "protection_view")["status"] == "dark"
    assert feature(report, "mentions")["status"] == "available"  # on by default (D31), no rule needed
    assert feature(report, "writes")["status"] == "off"  # dry_run
    assert feature(report, "mark_read")["status"] == "dark"
    assert feature(report, "flows")["status"] == "off"  # capable, but no rule lists a flow
    assert feature(report, "audit")["status"] == "dark"  # the agent is admin nowhere: the setting alone would not light it
    assert report["next_action"]["id"] == "set_you"


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
    assert "its own account" in feature(report, "read")["reason"]
    assert feature(report, "audit")["status"] == "available"


def test_rules_and_dry_run_switch_features_on():
    bot = make_token("bot")
    rules = [{"repo_glob": "acme/forgebruecke-demo", "mentions": True}]
    report, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}, settings={"writes": rules})
    assert feature(report, "mentions")["status"] == "available"
    assert feature(report, "writes")["status"] == "off" and "Preview only" in feature(report, "writes")["reason"]
    report2, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot},
                     settings={"writes": rules, "dry_run": False})
    assert feature(report2, "writes")["status"] == "available"


def test_missing_url_stops_before_any_request():
    forge = FakeForgejo({})
    report = plugin.doctor_report(getter({}), {}.get, transport=forge)
    assert report["ok"] is False and forge.calls == []
    assert report["next_action"] == {"id": "set_url", "text": "Enter your Forgejo address."}
    assert report["next_step"] == report["next_action"]["text"]


def test_missing_token_says_which_secret_to_store():
    report, _ = run({}, {})
    assert report["ok"] is False
    assert report["tokens"][0] == {"role": "bot", "env": "FORGEBRUECKE_BOT_ACCOUNT_TOKEN", "set": False}
    assert report["next_action"] == {"id": "set_token", "text": "Add the token of Hermes' account."}


def test_rejected_token_is_explained():
    report, _ = run({}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": make_token("stale")})
    t = report["tokens"][0]
    assert report["ok"] is False and t["error"]["kind"] == "token_rejected"
    assert report["next_action"]["id"] == "fix_token"
    assert report["next_step"].startswith("Forgejo refused Hermes' token (401).")


def test_unsupported_version_is_refused_with_a_sentence():
    bot = make_token("bot")
    report, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}, version="6.0.1+gitea-1.21.0")
    assert report["ok"] is False and "Upgrade Forgejo" in report["next_step"]
    assert report["next_action"]["id"] == "set_url"


def test_a_forge_without_notifications_hides_the_channel():
    bot = make_token("bot")
    from fb_fakes import SWAGGER
    swagger = {"paths": {k: v for k, v in SWAGGER["paths"].items() if k != "/notifications"}}
    report, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}, swagger=swagger)
    assert feature(report, "mentions")["status"] == "dark"
    assert "GET /notifications" in feature(report, "mentions")["missing"]


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


def test_a_green_setup_that_only_previews_says_so_as_its_next_action():
    # A green token and address used to read "Ready." while every write stayed a preview.
    bot = make_token("bot")
    report, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}, settings={"you": "acme"})
    assert report["ok"] is True
    assert report["next_action"] == {"id": "dry_run", "text": "Preview only: Hermes shows each write and sends none."}
    assert feature(report, "writes")["status"] == "off"
    report2, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot},
                     settings={"you": "acme", "dry_run": False})
    assert report2["next_action"] == {"id": "none", "text": "Ready."} and report2["next_step"] == "Ready."


def test_next_asks_who_you_are_before_it_mentions_the_preview():
    bot = make_token("bot")
    report, _ = run({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot})
    assert report["ok"] is True
    assert report["next_action"] == {"id": "set_you", "text": "Which Forgejo user are you?"}
    assert report["next_step"].startswith("Which Forgejo user are you?") and "acme" in report["next_step"]
    assert report["you"]["suggestion"] == "acme" and report["you"].get("source", "repo_owner") == "repo_owner"


def test_your_write_tokens_own_login_is_the_certain_suggestion():
    bot, own = make_token("bot"), make_token("owner")
    agent = bot_account()
    agent["repos"] = agent["repos"] + [repo("someorg/a", "read"), repo("someorg/b", "read")]
    mine = {**owner_account(), "scopes": {"write:repository", "read:user"}}
    report, _ = run({bot: agent, own: mine}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot,
                                               "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": own})
    assert report["you"] == {"set": "", "suggestion": "acme", "source": "write_token"}
    same, _ = run({bot: agent}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": bot})
    assert same["you"]["suggestion"] == "someorg" and "source" not in same["you"]


class Unsteady(FakeForgejo):
    """Answers 500 for the account of every token: the forge is up, the check of the token is not."""

    def __call__(self, method, url, headers, body, timeout):
        if url.endswith("/api/v1/user"):
            return self._json(500, {"message": "boom"})
        return super().__call__(method, url, headers, body, timeout)


def direct(forge, secrets, settings=None, creds=None, channel=None):
    settings_mod, client_mod = sub("settings"), sub("client")
    loaded = settings_mod.load_settings(getter({**URL, **(settings or {})}))
    creds = creds or settings_mod.load_credentials(secrets.get)
    return doctor.run_doctor(loaded, creds, channel=channel, client_factory=lambda u, t: client_mod.ForgejoClient(
        u, t, transport=forge, sleep=lambda s: None, clock=lambda: 0.0))


def test_refused_is_said_only_when_the_forge_refused():
    bot = make_token("bot")
    report = direct(Unsteady({bot: bot_account()}), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot})
    assert report["ok"] is False and report["next_action"]["id"] == "retry"
    assert "refused" not in report["next_step"] and "500" in report["next_step"]


def test_problems_the_credentials_report_join_the_token_warnings():
    bot = make_token("bot")
    real = sub("settings").load_credentials({"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}.get)

    class Creds:
        token, owner_token = real.token, None

        def problems(self):
            return ["The write token holds a line break and is ignored."]

    report = direct(FakeForgejo({bot: bot_account()}), {}, creds=Creds())
    assert "The write token holds a line break and is ignored." in report["token_warnings"]
    assert "The write token holds a line break and is ignored." in doctor.compact(report)["warnings"]


def handler(accounts, secrets, settings=None):
    return plugin.make_doctor_handler(getter({**URL, **(settings or {})}), secrets.get,
                                      transport=FakeForgejo(accounts))


def test_the_tool_answers_short_by_default_and_in_full_on_request():
    bot, own = make_token("bot"), make_token("owner")
    for accounts, secrets in (({bot: bot_account()}, {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}),
                              ({bot: bot_account(), own: owner_account()},
                               {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": own})):
        text = handler(accounts, secrets)({})
        assert len(text) < 1500, len(text)
        out = json.loads(text)
        assert out["ok"] is True and out["forge"]["version"].startswith("15.0.11") and out["forge"]["supported"]
        assert [(t["role"], t["set"]) for t in out["tokens"]] == [("hermes", True), ("user", own in accounts)]
        assert out["tokens"][0]["login"] == "forgebruecke-bot"
        assert out["next_action"]["id"] == "set_you" and out["you"]["suggestion"] == "acme"
        full = json.loads(handler(accounts, secrets)({"detail": True}))
        named = [*out["works"], *out["off"], *(n for names in out["not_available"].values() for n in names)]
        assert sorted(named) == sorted(f["label"] for f in full["features"])
        assert full["setups"] and full["tokens"][0]["scopes"]["read:user"] == "yes"
        assert "setups" not in out and bot not in text


def test_the_short_answer_of_an_unfinished_setup_names_the_next_action():
    out = json.loads(handler({}, {})({}))
    assert out["ok"] is False and out["next_action"]["id"] == "set_token"
    assert [(t["role"], t["set"]) for t in out["tokens"]] == [("hermes", False), ("user", False)]
    out = json.loads(plugin.make_doctor_handler(getter({}), {}.get, transport=FakeForgejo({}))({}))
    assert out["ok"] is False and out["next_action"]["id"] == "set_url"


def test_the_description_does_not_ask_for_a_call_before_every_first_action():
    text = plugin.FORGEJO_DOCTOR["description"]
    assert "before the first" not in text and "detail" in plugin.FORGEJO_DOCTOR["parameters"]["properties"]


def test_the_doctor_says_when_hermes_asks_nobody_before_a_write():
    """With approvals off in the profile, Hermes approves every write itself: the user should read that here."""
    bot = make_token("bot")

    def report(channel):
        return direct(FakeForgejo({bot: bot_account()}), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": bot}, channel=channel)
    off = report({"blockers": [], "toolsets": [], "approvals_off": True})
    assert any("approvals are off" in w and "unasked" in w for w in doctor.compact(off)["warnings"])
    for channel in (None, {"blockers": [], "toolsets": [], "approvals_off": False}):
        assert not any("approvals" in w for w in doctor.compact(report(channel)).get("warnings", []))


def test_approvals_off_is_read_as_hermes_reads_it():
    """YAML reads a bare ``off`` as false, so the config holds a boolean there."""
    assert plugin._approvals_off({"approvals": {"mode": "off"}}) is True
    assert plugin._approvals_off({"approvals": {"mode": False}}) is True
    assert plugin._approvals_off({"approvals": {"mode": " OFF "}}) is True
    for config in ({}, {"approvals": None}, {"approvals": {"mode": "manual"}}, {"approvals": {"mode": True}},
                   {"approvals": {"mode": "smart"}}, {"approvals": {}}):
        assert plugin._approvals_off(config) is False, config
