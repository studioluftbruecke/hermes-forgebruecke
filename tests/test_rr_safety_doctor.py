"""The user's personal write token is used only on the user's own click on the Forgebrücke page. The agent's
`forgejo_doctor` tool and `hermes forgebruecke doctor` (which the agent's shell can run) never send it; they
say it is set and checked on the page."""
import json
import types

from fb_fakes import BASE, FakeForgejo, bot_account, load_plugin, make_token, owner_account

plugin = load_plugin()
BOT, OWNER = make_token("bot"), make_token("owner")
ENV = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": OWNER}


class Recorder:
    """A transport over the fake forge that records which token each request carried."""

    def __init__(self, forge):
        self.forge, self.tokens = forge, []

    def __call__(self, method, url, headers, body, timeout):
        self.tokens.append((headers or {}).get("Authorization", ""))
        return self.forge(method, url, headers, body, timeout)

    def sent(self, token):
        return sum(1 for auth in self.tokens if token in auth)


def forge():
    return Recorder(FakeForgejo({BOT: bot_account(), OWNER: owner_account()}))


def getter(key, default=None):
    return {"url": BASE, "dry_run": False}.get(key, default)


def test_the_agents_doctor_never_sends_the_personal_token():
    wire = forge()
    handler = plugin.make_doctor_handler(getter, ENV.get, transport=wire)
    out = json.loads(handler({"detail": True}))
    assert wire.sent(BOT) > 0 and wire.sent(OWNER) == 0
    owner = next(t for t in out["tokens"] if t["role"] == "owner")
    assert owner["set"] is True and "page" in owner["note"]


def test_the_cli_doctor_never_sends_the_personal_token(capsys):
    wire = forge()
    handler = plugin.make_cli_handler(getter, ENV.get, transport=wire)
    handler(types.SimpleNamespace(forgebruecke_command="doctor", json=True))
    report = json.loads(capsys.readouterr().out)
    assert wire.sent(OWNER) == 0 and report["tokens"]


def test_the_doctor_without_hermes_token_does_not_probe_with_the_personal_one():
    wire = forge()
    only_owner = {"FORGEBRUECKE_PERSONAL_WRITE_TOKEN": OWNER}
    plugin.make_doctor_handler(getter, only_owner.get, transport=wire)({})
    assert wire.sent(OWNER) == 0


def test_the_page_still_checks_the_personal_token_on_the_users_click():
    wire = forge()
    report = plugin.doctor_report(getter, ENV.get, transport=wire)
    assert wire.sent(OWNER) > 0
    assert next(t for t in report["tokens"] if t["role"] == "owner").get("identity") == "acme"
