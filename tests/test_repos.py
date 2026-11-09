"""M8: packs, protection presets, the audit and the overview of forgejo_repos."""
import base64
import json

import pytest

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, load_plugin, make_token, owner_account, repo, sample_data, sub

plugin = load_plugin()
repos = sub("tools.repos")
approval = sub("approval")
access_mod = sub("access")
BOT, READ = make_token("bot"), make_token("read")
OWN = "forgebruecke-bot/own"


def forge(admin_on=(), **extra_data):
    bot = bot_account()
    bot["repos"] = [repo(r["full_name"], "admin") if r["full_name"] in admin_on else r for r in bot["repos"]] + \
        [repo(OWN, "admin")]
    data = sample_data()
    data[OWN] = {"info": {"private": True, "default_branch": "main", "html_url": f"{BASE}/{OWN}",
                          "owner": {"login": "forgebruecke-bot"}}, "protections": [], "labels": []}
    for key, value in extra_data.items():
        data[DEMO][key] = value
    return FakeForgejo({BOT: bot, READ: owner_account()}, data=data)


def loader(f, *, audit=False, tokens=(BOT,), rules=None):
    values = {"url": BASE, "dry_run": False, "audit_enabled": audit,
              "writes": rules or [{"repo_glob": DEMO, "flows": ["demo"]},
                                  {"repo_glob": OWN}]}
    env = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}
    if READ in tokens:
        env["FORGEBRUECKE_PERSONAL_READ_TOKEN"] = READ
    return lambda: access_mod.load_access(lambda k, d=None: values.get(k, d), env.get, transport=f)


def approved_run(f, args, run=None):
    spec = repos.make_spec()
    tickets = approval.ApprovalTickets()
    load = loader(f)
    run = run or approval.RunContext()
    gate = approval.make_gate(load, lambda: run, tickets, extra={"forgejo_repos": spec})
    verdict = gate(tool_name="forgejo_repos", args=args, session_id="s")
    out = approval.run_write("forgejo_repos", args, "s", load, run, tickets, spec=spec)
    return verdict, out


# --- presets -------------------------------------------------------------------------------------------

def test_preset_fields_fill_in_the_owner_and_the_checks():
    solo = repos.preset_fields("solo-with-agents", "acme", ["forgebruecke/*"])
    assert solo["push_whitelist_usernames"] == ["acme"] and solo["merge_whitelist_usernames"] == ["acme"]
    assert solo["status_check_contexts"] == ["forgebruecke/*"] and solo["required_approvals"] == 0
    team = repos.preset_fields("team-of-two", "acme", ["devops/tests"])
    assert team["enable_push"] is False and team["required_approvals"] == 1
    with pytest.raises(access_mod.Refusal):
        repos.preset_fields("everyone", "acme", [])


def test_protection_diff_lists_only_what_changes():
    wanted = repos.preset_fields("team-of-two", "acme", ["devops/tests"])
    changed, lines = repos.protection_diff("main", None, wanted)
    assert changed == wanted and lines[0] == "`main` has no protection rule yet; this creates one."
    current = {**wanted, "required_approvals": 0, "status_check_contexts": ["devops/tests"]}
    changed, lines = repos.protection_diff("main", current, wanted)
    assert changed == {"required_approvals": 1}
    assert lines == ["`main`: 1 approval (now: 0 approvals)"]


def test_preset_on_a_repo_the_bot_administers_creates_the_rule_after_approval():
    f = forge()
    verdict, out = approved_run(f, {"action": "preset", "repo": OWN, "preset": "solo-with-agents"})
    assert verdict["action"] == "approve" and "Solo with agents" in verdict["message"]
    assert out["ok"], out
    (method, path, body), = [w for w in f.writes if "branch_protections" in w[1]]
    assert method == "POST" and body["rule_name"] == "main"
    assert body["push_whitelist_usernames"] == ["forgebruecke-bot"]
    assert body["enable_status_check"] is False  # no flow on this repo and no checks given


def test_preset_patches_only_the_changed_fields():
    f = forge()
    f.data[OWN]["protections"] = [{"rule_name": "main", **repos.preset_fields("team-of-two", "forgebruecke-bot",
                                                                               ["devops/tests"]),
                                   "required_approvals": 0}]
    _, out = approved_run(f, {"action": "preset", "repo": OWN, "preset": "team-of-two", "checks": ["devops/tests"]})
    assert out["ok"], out
    (method, path, body), = [w for w in f.writes if "branch_protections" in w[1]]
    assert method == "PATCH" and path.endswith("/branch_protections/main") and body == {"required_approvals": 1}


def test_preset_without_admin_is_refused_before_the_card():
    f = forge()
    verdict, out = approved_run(f, {"action": "preset", "repo": DEMO})
    assert verdict["action"] == "block" and "need admin rights" in verdict["message"]
    assert out["error"] == "needs_admin" and f.writes == []


# --- packs ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("preset,codeowners,pr_line", [
    ("solo-with-agents", "* @acme\n", "Agent changes: the checks are green"),
    ("team-of-two", "* @acme @bea\n", "One approval from the other person"),
])
def test_pack_rendering_per_preset(preset, codeowners, pr_line):
    items = {i.get("path") or i.get("name"): i for i in repos.pack_items(DEMO, preset, "acme", "bea")}
    assert items[".forgejo/CODEOWNERS"]["text"].endswith(codeowners)
    assert pr_line in items[".forgejo/pull_request_template.md"]["text"]
    assert items["README.md"]["text"].startswith("# forgebruecke-demo\n")
    assert {"bug", "enhancement", "agent", "needs-human"} <= set(items)


def test_pack_lands_as_a_branch_and_a_pull_request_never_on_main():
    f = forge()
    verdict, out = approved_run(f, {"action": "pack", "repo": DEMO})
    assert verdict["action"] == "approve" and out["ok"], out
    labels = [w[2]["name"] for w in f.writes if w[1].endswith("/labels")]
    assert labels == ["enhancement", "agent", "needs-human"]  # bug exists already
    (_, _, commit), = [w for w in f.writes if w[1].endswith(f"/{DEMO}/contents")]
    assert commit["branch"] == "main" and commit["new_branch"] == repos.PACK_BRANCH
    paths = [x["path"] for x in commit["files"]]
    assert ".forgejo/CODEOWNERS" in paths and "README.md" in paths
    assert all(x["operation"] == "create" for x in commit["files"])
    assert "@acme" in base64.b64decode(next(x["content"] for x in commit["files"]
                                            if x["path"] == ".forgejo/CODEOWNERS")).decode()
    (_, _, pr), = [w for w in f.writes if w[1].endswith("/pulls")]
    assert pr["head"] == repos.PACK_BRANCH and pr["base"] == "main"
    assert not any(w[0] == "PUT" for w in f.writes)


def test_pack_skips_files_and_labels_the_repo_has():
    f = forge(files_in_repo={"README": "x", ".forgejo/CODEOWNERS": "* @acme"},
              labels=[{"name": n} for n in ("bug", "enhancement", "agent", "needs-human")])
    _, out = approved_run(f, {"action": "pack", "repo": DEMO})
    (_, _, commit), = [w for w in f.writes if w[1].endswith("/contents")]
    assert {x["path"] for x in commit["files"]} == {".forgejo/issue_template/bug.md", ".forgejo/issue_template/task.md",
                                                   ".forgejo/pull_request_template.md"}
    assert out["labels"] == []


def test_pack_refused_in_an_unattended_run():
    f = forge()
    verdict, out = approved_run(f, {"action": "pack", "repo": DEMO}, run=approval.RunContext(single_query=True))
    assert verdict["action"] == "block" and out["error"] == "unattended" and f.writes == []


# --- audit and overview --------------------------------------------------------------------------------

def handler(f, *, audit=False, tokens=(BOT,), rules=None):
    values = {"url": BASE, "audit_enabled": audit,
              "writes": rules if rules is not None else [{"repo_glob": DEMO, "flows": ["demo"]}]}
    env = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, **({"FORGEBRUECKE_PERSONAL_READ_TOKEN": READ} if READ in tokens else {})}
    return plugin.make_repos_handler(lambda k, d=None: values.get(k, d), lambda: approval.RunContext(),
                                     repos.make_spec(), env.get, transport=f)


def test_audit_is_off_by_default():
    out = json.loads(handler(forge())({"action": "audit", "repo": DEMO}))
    assert out["error"] == "off" and out.get("setting") == "audit_enabled"


def test_audit_findings_where_the_agent_is_admin_most_severe_first_capped_at_five():
    f = forge(admin_on=(DEMO,), protections=[], info={**sample_data()[DEMO]["info"], "private": False},
              keys=[{"title": "ci", "read_only": False, "created_at": "2026-01-01T00:00:00Z"},
                    {"title": "old", "read_only": True, "created_at": "2023-01-01T00:00:00Z"}],
              hooks=[{"active": False, "config": {"url": "https://x.example/h"}, "updated_at": "2026-11-01T00:00:00Z"},
                     {"active": True, "config": {"url": "https://y.example/h"}, "updated_at": "2024-01-01T00:00:00Z"}])
    out = json.loads(handler(f, audit=True, tokens=(BOT, READ))({"action": "audit", "repo": DEMO}))
    audit = out["audits"][0]
    rules = [x["rule"] for x in audit["findings"]]
    assert rules[0] == "unprotected_default" and len(rules) == 5 and audit["more"] == 1
    assert {"deploy_key_write", "public"} <= set(rules)
    assert all(x["link"].startswith(BASE) and x["fix"] for x in audit["findings"])
    assert audit["read_as"] == "bot" and audit["not_checked"] == []
    assert not any(c[0] != "GET" for c in f.calls)


def test_audit_without_admin_skips_the_admin_checks_and_names_the_right():
    """Item 7: admin-only checks light per repo where Hermes' account is admin; elsewhere they are not even asked."""
    f = forge()
    out = json.loads(handler(f, audit=True)({"action": "audit", "repo": DEMO}))
    audit = out["audits"][0]
    assert len(audit["not_checked"]) == 1 and "need admin right of Hermes' Forgejo account" in audit["not_checked"][0]
    assert "it has write" in audit["not_checked"][0] and f"{BASE}/{DEMO}/settings/collaboration" in audit["not_checked"][0]
    assert not any(c[1].endswith(("/branch_protections", "/keys", "/hooks")) for c in f.calls)


def test_audit_flags_missing_checks_and_approvals_on_a_protected_branch():
    f = forge(admin_on=(DEMO,), protections=[{"rule_name": "main", "enable_status_check": False, "required_approvals": 0}])
    audit = json.loads(handler(f, audit=True)({"action": "audit", "repo": DEMO}))["audits"][0]
    assert [x["rule"] for x in audit["findings"]] == ["no_required_checks", "approvals_off"]


def test_overview_reports_access_and_protection():
    f = forge(admin_on=(DEMO,), protections=[{"rule_name": "main", "enable_push": False, "enable_status_check": True,
                                              "status_check_contexts": ["devops/tests"], "required_approvals": 1}])
    out = json.loads(handler(f, tokens=(BOT, READ))({"action": "overview"}))
    ov = out["repos"][0]
    assert ov["repo"] == DEMO and ov["your_access"] == "admin" and ov["read_as"] == "bot"
    assert ov["collaboration_url"] == f"{BASE}/{DEMO}/settings/collaboration"
    assert ov["protection"] == {"branch": "main", "protected": True, "direct_push": False,
                                "required_checks": ["devops/tests"], "approvals": 1, "mergers": "anyone with write"}


def test_overview_without_a_rule_covers_the_agents_repos_and_keeps_protection_dark_without_admin():
    f = forge()
    out = json.loads(handler(f, rules=[])({"action": "overview"}))
    by_repo = {r["repo"]: r for r in out["repos"]}
    assert set(by_repo) == {DEMO, OWN}
    assert by_repo[DEMO]["your_access"] == "write" and "needs admin right" in by_repo[DEMO]["protection"]["unknown"]
    assert by_repo[OWN]["your_access"] == "admin" and by_repo[OWN]["protection"]["protected"] is False
    assert not any(c[1] == f"/api/v1/repos/{DEMO}/branch_protections" for c in f.calls)
