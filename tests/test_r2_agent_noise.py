"""A read answer carries no field that says the same thing twice or the same thing every time."""
import json

from fb_fakes import DEMO, sub
from fb_fakes_tools_read import kit, plugin, read


def repos_tool(args, **settings):
    forge, gs, sec = kit(settings=settings)
    handler = plugin.make_repos_handler(gs, lambda: sub("approval").RunContext(), sub("tools.repos").make_spec(), sec,
                                        transport=forge)
    return json.loads(handler(args))


def test_a_read_does_not_say_which_token_read_it():
    one, _ = read("forge_get", {"kind": "pr", "repo": DEMO, "number": 3})
    many, _ = read("forge_list", {"kind": "prs", "repo": DEMO})
    assert one["ok"] and many["ok"] and "read_as" not in one and "read_as" not in many


def test_the_overview_names_the_access_once():
    row = repos_tool({"action": "overview", "repo": DEMO})["repos"][0]
    assert row["your_access"] == "write" and "forgejo_right" not in row and "read_as" not in row


def test_the_audit_does_not_say_which_token_read_it():
    audit = repos_tool({"action": "audit", "repo": DEMO}, audit_enabled=True)["audits"][0]
    assert audit["repo"] == DEMO and "read_as" not in audit
