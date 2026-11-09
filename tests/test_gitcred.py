import io
import types

from fb_fakes import load_plugin, sub

plugin = load_plugin()
gitcred = sub("gitcred")

URL = "https://forge.example"
BOT, OWNER = "bot-token-0001", "owner-token-0002"
SECRETS = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": OWNER}


def run_cli(action, stdin="", url=URL, secrets=SECRETS):
    out = io.StringIO()
    code = plugin._cli_git_credential(types.SimpleNamespace(action=action),
                                      lambda key, default=None: {"url": url}.get(key, default),
                                      secrets.get, stdin=io.StringIO(stdin), stdout=out)
    return code, out.getvalue()


def test_git_gets_the_agent_token_for_the_configured_forge():
    code, out = run_cli("get", "protocol=https\nhost=forge.example\n\n")
    assert code == 0 and out == f"username=forgebruecke\npassword={BOT}\n"
    assert OWNER not in out


def test_the_username_git_sent_is_kept():
    _, out = run_cli("get", "protocol=https\nhost=forge.example\nusername=hermes\n\n")
    assert out.startswith("username=hermes\n")


def test_other_hosts_schemes_and_ports_get_no_answer():
    for request in ("protocol=https\nhost=github.com\n\n",
                    "protocol=http\nhost=forge.example\n\n",
                    "protocol=https\nhost=forge.example:3000\n\n",
                    "protocol=https\nhost=forge.example.evil.test\n\n",
                    ""):
        assert run_cli("get", request) == (0, "")


def test_a_non_default_port_in_the_address_must_match():
    assert run_cli("get", "protocol=https\nhost=forge.example:3000\n\n", url="https://forge.example:3000")[1]
    assert run_cli("get", "protocol=https\nhost=forge.example\n\n", url="https://forge.example:443/")[1]


def test_no_token_or_no_address_means_no_answer():
    assert run_cli("get", "protocol=https\nhost=forge.example\n\n", secrets={}) == (0, "")
    assert run_cli("get", "protocol=https\nhost=forge.example\n\n", url="") == (0, "")


def test_store_and_erase_are_accepted_and_change_nothing():
    for action in ("store", "erase"):
        assert run_cli(action, f"protocol=https\nhost=forge.example\npassword={BOT}\n\n") == (0, "")


def test_without_an_action_it_prints_the_setup_for_this_forge():
    code, out = run_cli(None)
    assert code == 0
    assert "git config --global --replace-all credential.https://forge.example.helper ''" in out
    assert "--add credential.https://forge.example.helper 'cache --timeout=3600'" in out
    assert "forgebruecke git-credential get; fi; }; f'" in out and " -p " in out
    assert BOT not in out and OWNER not in out


def test_setup_text_asks_for_the_address_first():
    assert "Forgejo address" in gitcred.setup_text("", "coding", "/usr/bin/hermes")


def test_setup_text_starts_hermes_only_for_get():
    text = gitcred.setup_text(URL, "coding", "/opt/hermes/bin/hermes")
    assert ("""'!f() { if [ "$1" = get ]; then exec /opt/hermes/bin/hermes -p coding forgebruecke """
            """git-credential get; fi; }; f'""") in text
