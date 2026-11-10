"""M4b and 0.0.14: the lane matrix, Forgejo's collaborator right as the only permission layer (D27), the
capability registry per setup, the mention-read leak, the write token of the user's account kept away from
agent writes, and a token of the wrong account flagged."""
import json

import pytest

from fb_fakes import (BASE, DEMO, FakeForgejo, bot_account, load_plugin, make_token, owner_account, repo,
                      sample_data, sub)

plugin = load_plugin()
access_mod = sub("access")
caps_mod = sub("capabilities")
approval = sub("approval")
doctor = sub("tools.doctor")
settings_mod = sub("settings")
BOT, READ, OWNER = make_token("bot"), make_token("read"), make_token("owner")
OTHER = "acme/website"
READ_ONLY = "acme/backup"


def owner_write_account():
    acct = owner_account()
    return {**acct, "scopes": {"write:repository", "write:issue", "read:organization", "read:user"}}


def forge(bot_admin_on=()):
    bot = bot_account()
    bot["repos"] = bot["repos"] + [repo(OTHER, "write"), repo(READ_ONLY, "read")] + \
        [repo(r, "admin") for r in bot_admin_on]
    return FakeForgejo({BOT: bot, READ: owner_account(), OWNER: owner_write_account()}, data=sample_data())


def token_used(f) -> str:
    return {BOT: "bot", READ: "read", OWNER: "owner"}[f.last_token]


def recorder(f):
    orig = f.__call__

    def call(method, url, headers, body, timeout):
        f.last_token = headers.get("Authorization", "")[len("token "):]
        return orig(method, url, headers, body, timeout)
    return call


# The retired read token's env name, set on purpose: 0.0.14 never reads it (D29).
SECRETS = {"bot": ("FORGEBRUECKE_BOT_ACCOUNT_TOKEN", BOT), "read": ("FORGEBRUECKE_PERSONAL_READ_TOKEN", READ),
           "owner": ("FORGEBRUECKE_PERSONAL_WRITE_TOKEN", OWNER)}


def access(lane="agent", scope="", tokens=("bot", "read", "owner"), f=None, values=None):
    env = dict(SECRETS[t] for t in tokens)
    f = f or forge()
    values = values or {"url": BASE, "writes": [{"repo_glob": DEMO, "flows": ["demo"]}]}
    a = access_mod.load_access(lambda k, d=None: values.get(k, d), env.get, transport=recorder(f), lane=lane,
                               scope_repo=scope)
    return a, f


@pytest.mark.parametrize("lane,tokens,expected", [
    ("agent", ("bot", "read", "owner"), "bot"),
    ("agent", ("bot", "owner"), "bot"),
    ("agent", ("bot",), "bot"),
    ("mention", ("bot", "read", "owner"), "bot"),
    ("flow", ("bot", "read", "owner"), "bot"),
    ("owner", ("bot", "read", "owner"), "owner"),
])
def test_lane_matrix_for_reads(lane, tokens, expected):
    """D29: the agent lane reads with the agent token only; neither the retired read token nor the user's write
    token widens what the desk and his chats see."""
    a, f = access(lane, DEMO if lane in ("mention", "flow") else "", tokens)
    a.reader().get(f"/repos/{DEMO}")
    assert token_used(f) == expected and a.read_role == expected


def test_the_retired_read_token_alone_configures_nothing():
    with pytest.raises(access_mod.Refusal) as e:
        access(tokens=("read",))
    assert e.value.code == "not_configured"


@pytest.mark.parametrize("lane", ["agent", "flow"])
def test_agent_and_flow_writes_use_the_agent_token_only(lane):
    a, f = access(lane, DEMO if lane == "flow" else "")
    a.writer().post(f"/repos/{DEMO}/issues/3/comments", {"body": "x"})
    assert token_used(f) == "bot"


def test_mention_lane_has_no_tool_writer_and_owner_token_only_in_the_owner_lane():
    a, _ = access("mention", DEMO)
    with pytest.raises(access_mod.Refusal) as e:
        a.writer()
    assert e.value.code == "unattended"
    for lane in ("agent", "mention", "flow"):
        with pytest.raises(access_mod.Refusal) as e:
            access(lane, DEMO if lane != "agent" else "")[0].owner_writer()
        assert e.value.code == "wrong_lane"
    o, f = access("owner")
    o.owner_writer().post(f"/repos/{DEMO}/pulls/3/merge", {"Do": "merge"})
    assert token_used(f) == "owner"
    with pytest.raises(access_mod.Refusal) as e:
        o.writer()
    assert e.value.code == "wrong_lane"


def test_owner_lane_without_the_write_token_is_dark_with_the_field_named():
    o, _ = access("owner", tokens=("bot",))
    with pytest.raises(access_mod.Refusal) as e:
        o.owner_writer()
    assert e.value.code == "dark" and "FORGEBRUECKE_PERSONAL_WRITE_TOKEN" in e.value.message


def test_agent_tool_write_never_uses_the_owner_token():
    """The full tool path (gate + handler) with every token set: the comment goes out as the agent account."""
    f = forge()
    rec = recorder(f)
    values = {"url": BASE, "dry_run": False}
    env = dict(SECRETS.values())
    load = lambda: access_mod.load_access(lambda k, d=None: values.get(k, d), env.get, transport=rec)  # noqa: E731
    tickets = approval.ApprovalTickets()
    gate = approval.make_gate(load, lambda: approval.RunContext(), tickets)
    args = {"repo": DEMO, "number": 3, "body": "hi"}
    assert gate(tool_name="forgejo_comment", args=args, session_id="s")["action"] == "approve"
    out = approval.run_write("forgejo_comment", args, "s", load, approval.RunContext(), tickets)
    assert out["ok"] and token_used(f) == "bot"


# --- Forgejo is the permission layer (D27) --------------------------------------------------------------

def test_write_on_a_read_only_repo_is_refused_with_the_right_and_the_collaboration_page():
    a, f = access()
    with pytest.raises(access_mod.Refusal) as e:
        a.check_write(READ_ONLY, "write")
    link = f"{BASE}/{READ_ONLY}/settings/collaboration"
    assert e.value.code == "read_only" and "has read access there" in e.value.message and link in e.value.message
    assert e.value.to_dict()["link"] == link
    assert a.check_read(READ_ONLY) == "read"  # reading it is fine
    assert not [c for c in f.calls if c[0] != "GET"]


def test_a_repo_the_agent_cannot_see_is_refused_for_reads_and_writes_alike():
    a, _ = access()
    for check in (a.check_read, a.check_write):
        with pytest.raises(access_mod.Refusal) as e:
            check("acme/secret")
        assert e.value.code == "no_access" and "cannot see acme/secret" in e.value.message
        assert f"{BASE}/acme/secret/settings/collaboration" in e.value.message


def test_write_access_needs_no_rule_and_returns_the_default_rule():
    a, _ = access(values={"url": BASE})
    rule = a.check_write(OTHER, "comment")
    assert rule.repo_glob == OTHER and rule.targets == () and rule.mentions is True


def test_rights_are_asked_once_per_repo_and_the_repo_list_fills_the_cache():
    a, f = access()
    for _ in range(3):
        a.check_write(DEMO)
        a.check_read(DEMO)
    assert sum(1 for c in f.calls if c[1] == f"/api/v1/repos/{DEMO}") == 1
    rights = {r["repo"]: r["right"] for r in a.repos()}
    assert rights[READ_ONLY] == "read" and rights[OTHER] == "write"
    before = len(f.calls)
    assert a.check_read(OTHER) == "write" and a.check_read(READ_ONLY) == "read"
    assert len(f.calls) == before  # answered from the listing


def test_another_access_for_the_same_token_shares_the_cache_and_clear_rights_forgets():
    a, f = access()
    a.check_read(DEMO)
    a.for_lane("flow", DEMO).check_read(DEMO)
    assert sum(1 for c in f.calls if c[1] == f"/api/v1/repos/{DEMO}") == 1
    access_mod.clear_rights()
    a.check_read(DEMO)
    assert sum(1 for c in f.calls if c[1] == f"/api/v1/repos/{DEMO}") == 2


def test_a_repo_shared_after_a_refusal_is_seen_on_the_next_call():
    a, f = access()
    assert a.right("acme/new") is None
    f.accounts[BOT]["repos"].append(repo("acme/new", "write"))
    assert a.right("acme/new") == "write"


def test_a_caller_cannot_change_the_cached_repo_list():
    a, _ = access()
    listed = a.repos()
    first = listed[0]["repo"]
    listed[0]["right"] = "none"
    listed.clear()
    again = a.repos()
    assert again[0]["repo"] == first and again[0]["right"] == "write"


def test_you_is_suggested_from_the_owners_of_the_agents_repos():
    counts = access_mod.owner_counts([{"owner": "acme"}, {"owner": "acme"}, {"owner": "forgebruecke-bot"},
                                      {"owner": "forgebruecke-bot"}, {"owner": "forgebruecke-bot"}, {"owner": "x"}])
    assert access_mod.suggest_you(counts, "forgebruecke-bot") == "acme"
    assert access_mod.suggest_you({"forgebruecke-bot": 3}, "forgebruecke-bot") == ""


def test_you_is_not_suggested_from_a_login_the_caller_rules_out():
    counts = {"acme-org": 5, "dana": 2, "forgebruecke-bot": 9}
    assert access_mod.suggest_you(counts, "forgebruecke-bot") == "acme-org"
    assert access_mod.suggest_you(counts, "forgebruecke-bot", skip=("ACME-Org",)) == "dana"
    assert access_mod.suggest_you(counts, "forgebruecke-bot", skip=["acme-org", "dana"]) == ""


@pytest.mark.parametrize("value", ["../..", "acme/..", "./repo", "acme/.", "../user.git"])
def test_a_dot_segment_is_not_a_repository_name(value):
    with pytest.raises(access_mod.Refusal) as e:
        access_mod.parse_repo(value)
    assert e.value.code == "bad_repo"
    assert access_mod.thread_repo(f"{value}#1") == ""


def test_dots_inside_a_repository_name_are_fine():
    assert access_mod.parse_repo("acme/.github") == "acme/.github"
    assert access_mod.parse_repo("a.b/c..d.git") == "a.b/c..d"
    assert access_mod.thread_repo("acme/.github#4") == "acme/.github"


def read_handler(tool, lane, chat_id=""):
    f = forge()
    values = {"url": BASE}
    env = dict(SECRETS.values())
    run = approval.RunContext(platform="forgebruecke" if lane == "mention" else "", chat_id=chat_id)
    h = plugin.make_read_handler(tool, lambda k, d=None: values.get(k, d), env.get, transport=recorder(f),
                                 lane_fn=run.lane)
    return h, f


def test_a_mention_turn_asking_for_another_repo_is_refused_before_any_request():
    h, f = read_handler("forge_get", "mention", f"{DEMO}#1")
    out = json.loads(h({"kind": "repo", "repo": OTHER}))
    assert out["error"] == "lane_scope" and f"may read only {DEMO}, not {OTHER}" in out["message"]
    assert f.calls == []
    out = json.loads(h({"kind": "issue", "repo": DEMO, "number": 1}))
    assert out["ok"] and token_used(f) == "bot"


def test_a_mention_turn_cannot_search_across_repos():
    h, f = read_handler("forge_list", "mention", f"{DEMO}#1")
    for args in ({"kind": "repos"}, {"kind": "prs"}, {"kind": "issues", "q": "secret"}):
        out = json.loads(h(args))
        assert out["error"] == "lane_scope", args
    assert f.calls == []


def test_a_mention_reference_to_another_repo_does_not_expand():
    reference = sub("reference")
    f = forge()
    values = {"url": BASE}
    env = dict(SECRETS.values())
    run = approval.RunContext(platform="forgebruecke", chat_id=f"{DEMO}#1")
    text = reference.expand(f"{OTHER}#1", lambda k, d=None: values.get(k, d), env.get, transport=recorder(f),
                            lane_fn=run.lane)
    assert "was not expanded" in text and "may read only" in text and f.calls == []
    assert reference.completions("x", lambda k, d=None: values.get(k, d), env.get, transport=f, lane_fn=run.lane) == []


def test_a_mention_lane_without_a_thread_repo_may_touch_no_repo():
    a, f = access()
    mention = a.for_lane("mention")
    for check in (mention.check_scope, mention.check_read, mention.check_write):
        with pytest.raises(access_mod.Refusal) as e:
            check(DEMO)
        assert e.value.code == "lane_scope" and "reads nothing" in e.value.message
    assert f.calls == []
    a.for_lane("mention", DEMO).check_scope(DEMO)
    a.for_lane("flow").check_scope(DEMO)  # the engine builds its lane once and names the repo per call


def test_user_chat_reads_use_the_agent_token():
    h, f = read_handler("forge_get", "agent")
    out = json.loads(h({"kind": "repo", "repo": DEMO}))
    assert out["ok"] and token_used(f) == "bot"


# --- the registry ------------------------------------------------------------------------------

def run_doctor(env, f=None, values=None):
    f = f or forge()
    values = values or {"url": BASE, "writes": [{"repo_glob": DEMO, "flows": ["demo"]}], "dry_run": False}
    settings = settings_mod.load_settings(lambda k, d=None: values.get(k, d))
    creds = settings_mod.load_credentials(env.get)
    return doctor.run_doctor(settings, creds, client_factory=lambda u, t: sub("client").ForgejoClient(u, t, transport=f))


def status(report, fid):
    return next(x for x in report["features"] if x["id"] == fid)


def test_registry_per_setup_agent_only_and_owner():
    agent_only = run_doctor({"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_READ_TOKEN": READ})
    assert [t["role"] for t in agent_only["tokens"]] == ["bot", "owner"]  # the read token is not even probed
    assert status(agent_only, "read")["status"] == "available"
    assert "desk_scope" not in {f["id"] for f in agent_only["features"]}
    assert status(agent_only, "owner_merge")["status"] == "dark"
    assert "FORGEBRUECKE_PERSONAL_WRITE_TOKEN" in status(agent_only, "owner_merge")["reason"]
    assert status(agent_only, "owner_merge")["light_with"] == ["owner"]
    assert status(agent_only, "protection_view")["status"] == "dark"
    assert "Hermes' account as an admin of the repo" in status(agent_only, "protection_view")["reason"]
    with_owner = run_doctor({"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": OWNER})
    assert status(with_owner, "owner_merge")["status"] == "available"
    assert status(with_owner, "owner_merge")["uses"] == ["owner"]
    assert status(with_owner, "writes")["uses"] == ["bot"]  # agent writes stay on the agent token
    # the user's token is admin everywhere, yet admin features light only where Hermes' account is admin (item 7)
    assert status(with_owner, "protection_view")["status"] == "dark"
    assert all("read token" not in f["reason"] for f in with_owner["features"])


def test_admin_features_light_where_the_agent_account_is_admin_and_say_where():
    report = run_doctor({"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}, f=forge(bot_admin_on=("forgebruecke-bot/probe",)))
    for fid in ("protection_view", "presets"):
        assert status(report, fid)["status"] == "available"
        assert "forgebruecke-bot/probe" in status(report, fid)["reason"] and "not available on the others" in status(report, fid)["reason"]


def test_setups_report_lists_scopes_and_what_each_lights():
    setups = {s["id"]: s for s in caps_mod.setups_report()}
    assert set(setups) == {"agent_only", "agent_owner"}
    assert setups["agent_only"]["mint"] == {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": list(caps_mod.BOT_SCOPES)}
    assert "Merge button" not in setups["agent_only"]["lights"]
    assert "Merge button" in setups["agent_owner"]["lights"]
    assert "could merge into main as you" in setups["agent_owner"]["summary"]


def test_a_token_of_the_wrong_account_is_flagged_and_ignored():
    report = run_doctor({"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": BOT})
    assert any("belongs to forgebruecke-bot, Hermes' own account" in w for w in report["token_warnings"])
    assert status(report, "owner_merge")["status"] == "dark"


def test_the_doctor_suggests_you_and_asks_for_it():
    report = run_doctor({"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT})
    assert report["you"] == {"set": "", "suggestion": "acme"}
    assert report["ok"] and 'Click "Use @acme" on the Forgebrücke page.' in report["next_step"]
    report = run_doctor({"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}, values={"url": BASE, "you": "acme"})
    assert "Use @" not in report["next_step"]


def test_dark_sentence_names_the_field():
    assert "FORGEBRUECKE_PERSONAL_WRITE_TOKEN" in caps_mod.dark_sentence("owner_merge")
    with pytest.raises(KeyError):
        caps_mod.dark_sentence("nope")
