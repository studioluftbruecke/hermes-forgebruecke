"""What one "always" covers: Hermes keys a standing approval on the hook's rule key, so two different
actions of one tool on one repo must not share a key."""
from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, repo, sample_data, sub

plugin, approval = sub("__init__"), sub("approval")
flows_tool, repos_tool = sub("tools.flows"), sub("tools.repos")


def gate(tmp_path):
    token = make_token("bot")
    account = bot_account()
    account["repos"] = [repo(DEMO, "admin")]
    forge = FakeForgejo({token: account}, data=sample_data())
    values = {"url": BASE, "dry_run": False, "default_review_profile": "coding"}
    extra = {"forgejo_flows": flows_tool.make_spec(lambda: sub("flows.state").FlowStore(tmp_path)),
             "forgejo_repos": repos_tool.make_spec()}
    load = plugin._default_access_loader(lambda k, d=None: values.get(k, d),
                                         {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": token}.get, forge)
    hook = approval.make_gate(load, approval.RunContext, approval.ApprovalTickets(), extra=extra)
    return lambda tool, **args: hook(tool_name=tool, args=args, session_id="s")["rule_key"]


def test_two_actions_of_one_tool_on_one_repo_have_two_rule_keys(tmp_path):
    key = gate(tmp_path)
    resume = key("forgejo_flows", action="resume", flow="demo", repo=DEMO)
    replace = key("forgejo_flows", action="set", repo=DEMO, template="one-review-then-me", target="local")
    assert resume == f"forgebruecke:forgejo_flows:resume:{DEMO}"
    assert replace == f"forgebruecke:forgejo_flows:set:{DEMO}"
    pack = key("forgejo_repos", action="pack", repo=DEMO)
    preset = key("forgejo_repos", action="preset", repo=DEMO)
    assert pack == f"forgebruecke:forgejo_repos:pack:{DEMO}" and preset == f"forgebruecke:forgejo_repos:preset:{DEMO}"


def test_a_tool_with_one_action_keeps_its_key(tmp_path):
    key = gate(tmp_path)
    assert key("forgejo_comment", repo=DEMO, number=3, body="one") == f"forgebruecke:forgejo_comment:{DEMO}"
    assert key("forgejo_comment", repo=DEMO, number=3, body="two") == f"forgebruecke:forgejo_comment:{DEMO}"
