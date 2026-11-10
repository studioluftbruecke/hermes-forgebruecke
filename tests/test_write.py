"""Write tools behind rules and Hermes' approval: allowed, refused, previewed, never approve or merge."""
import ast
import json
import pathlib

import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, make_token, owner_account, repo, sample_data, sub

approval = sub("approval")
access_mod = sub("access")
write = sub("tools.write")
BOT, READ = make_token("bot"), make_token("read")
ROOT = pathlib.Path(__file__).resolve().parents[1]
RULE = {"repo_glob": DEMO, "flows": ["demo"]}
READ_ONLY = "acme/backup"
ATTENDED = approval.RunContext()
PUSHED = ("x", "fix-mul", "fix-mul-2", "fix-mul-3")


class Kit:
    def __init__(self, settings=None, *, run=ATTENDED, read_token=True):
        bot = bot_account()
        bot["repos"] = bot["repos"] + [repo(READ_ONLY, "read")]
        data = sample_data()
        data[DEMO]["branches"].update({name: {"name": name, "commit": {"id": "2" * 40}} for name in PUSHED})
        self.forge = FakeForgejo({BOT: bot, READ: owner_account()}, data=data)
        self.values = {"url": BASE, "dry_run": False, "writes": [RULE], **(settings or {})}
        # the retired read token's env name is set on purpose: the plugin never reads it
        self.secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, **({"FORGEBRUECKE_PERSONAL_READ_TOKEN": READ} if read_token else {})}
        self.tickets = approval.ApprovalTickets()
        self.run = run
        self.gate = approval.make_gate(self.load, lambda: self.run, self.tickets)

    def load(self):
        return access_mod.load_access(lambda k, d=None: self.values.get(k, d), self.secrets.get, transport=self.forge)

    def call(self, tool, args, session="s1", approve=True):
        """What Hermes does: hook, then (if approved) the handler."""
        directive = self.gate(tool_name=tool, args=args, session_id=session)
        if directive and directive["action"] == "block":
            return directive, None
        if directive and directive["action"] == "approve" and not approve:
            approval.make_observer(self.tickets)(pattern_key="plugin_rule:" + directive["rule_key"], choice="deny")
        result = approval.run_write(tool, args, session, self.load, self.run, self.tickets)
        return directive, result


COMMENT = {"repo": DEMO, "number": 3, "body": "Looks right now."}


def test_allowed_comment_asks_first_then_lands_with_the_bot_token():
    k = Kit()
    directive, result = k.call("forgejo_comment", COMMENT)
    assert directive["action"] == "approve"
    assert directive["rule_key"] == f"forgebruecke:forgejo_comment:{DEMO}"
    assert directive["message"].startswith(f"This writes to {DEMO} as Hermes' account:")
    assert ': a comment on PR #3 "Fix calc.mul".\n\nLooks right now.' in directive["message"]
    assert write.FOOTER.strip() not in directive["message"]
    assert result["ok"] and result["sent"] and result["status"] == 201
    method, path, body = k.forge.writes[-1]
    assert (method, path) == ("POST", f"/repos/{DEMO}/issues/3/comments")
    assert body["body"].startswith("Looks right now.") and body["body"].endswith(write.FOOTER)
    # The read token cannot write (no write:issue), so a 201 proves the write went out as the agent account.


def test_a_repo_the_agent_cannot_see_is_refused_by_the_hook_and_nothing_is_written():
    k = Kit()
    directive, result = k.call("forgejo_comment", {**COMMENT, "repo": "studioluftbruecke/brain"})
    assert directive["action"] == "block" and result is None
    assert "Hermes' account cannot see studioluftbruecke/brain, or" in directive["message"]
    assert f"{BASE}/studioluftbruecke/brain/settings/collaboration" in directive["message"]
    assert "Nothing was sent" in directive["message"]
    assert [c[0] for c in k.forge.calls] == ["GET"] and k.forge.writes == []  # one read: the right


@pytest.mark.parametrize("tool,args", [
    ("forgejo_comment", {**COMMENT, "repo": READ_ONLY}),
    ("forgejo_create_pr", {"repo": READ_ONLY, "head": "x", "title": "t"}),
    ("forgejo_status", {"repo": READ_ONLY, "sha": HEAD_SHA, "state": "success", "description": "x"}),
])
def test_writes_on_a_repo_where_the_agent_has_read_access_are_refused_naming_the_right(tool, args):
    """Forgejo's collaborator right is the only layer; no rule could allow this, and none is needed for DEMO."""
    k = Kit({"writes": [{"repo_glob": "acme/*", "flows": ["demo"]}]})
    directive, result = k.call(tool, args)
    assert directive["action"] == "block" and result is None
    assert "Hermes' account has read access there" in directive["message"]
    assert f"{BASE}/{READ_ONLY}/settings/collaboration" in directive["message"]
    assert k.forge.writes == []


def test_write_access_alone_allows_a_comment_without_any_rule():
    k = Kit({"writes": []})
    directive, result = k.call("forgejo_comment", COMMENT)
    assert directive["action"] == "approve" and result["ok"]


def test_the_handler_refuses_a_call_the_hook_never_saw():
    k = Kit()
    result = approval.run_write("forgejo_comment", COMMENT, "s1", k.load, ATTENDED, k.tickets)
    assert result["error"] == "not_approved" and k.forge.writes == []


def test_a_denied_approval_revokes_the_ticket():
    k = Kit()
    directive, result = k.call("forgejo_comment", COMMENT, approve=False)
    assert directive["action"] == "approve" and result["error"] == "not_approved" and k.forge.writes == []


def test_a_ticket_is_used_once_and_bound_to_the_arguments_and_session():
    k = Kit()
    k.gate(tool_name="forgejo_comment", args=COMMENT, session_id="s1")
    other = {**COMMENT, "body": "something else"}
    assert approval.run_write("forgejo_comment", other, "s1", k.load, ATTENDED, k.tickets)["error"] == "not_approved"
    assert approval.run_write("forgejo_comment", COMMENT, "s2", k.load, ATTENDED, k.tickets)["error"] == "not_approved"
    assert approval.run_write("forgejo_comment", COMMENT, "s1", k.load, ATTENDED, k.tickets)["ok"]
    assert approval.run_write("forgejo_comment", COMMENT, "s1", k.load, ATTENDED, k.tickets)["error"] == "not_approved"
    assert len(k.forge.writes) == 1


@pytest.mark.parametrize("run,reason", [
    (approval.RunContext(cron=True), "cron job"),
    (approval.RunContext(single_query=True), "single-query run"),
    (approval.RunContext(platform="webhook"), "unattended webhook"),
    (approval.RunContext(unknown=True), "cannot tell whether a person is present"),
])
def test_unattended_runs_are_refused_by_hook_and_handler(run, reason):
    k = Kit(run=run)
    directive, _ = k.call("forgejo_comment", COMMENT)
    assert directive["action"] == "block" and reason in directive["message"]
    assert approval.run_write("forgejo_comment", COMMENT, "s1", k.load, run, k.tickets)["error"] == "unattended"
    assert k.forge.calls == []


@pytest.mark.parametrize("tool,args,summary", [
    ("forgejo_comment", COMMENT, 'a comment on PR #3 "Fix calc.mul"'),
    ("forgejo_create_pr", {"repo": DEMO, "head": "fix-mul", "title": "Fix mul", "body": "a * b", "reviewers": []},
     'a pull request fix-mul → main, "Fix mul"'),
    ("forgejo_status", {"repo": DEMO, "sha": HEAD_SHA, "state": "success", "description": "checked by hand"},
     f"commit status forgebruecke/agent = success on {HEAD_SHA}"),
])
def test_a_dry_run_preview_is_not_ok_names_the_write_in_one_line_and_sends_nothing(tool, args, summary):
    k = Kit({"dry_run": True})
    directive, result = k.call(tool, args)
    assert directive is None and k.forge.writes == []
    assert (result["ok"], result["error"], result["sent"]) == (False, "dry_run", False)
    assert result["summary"] == summary
    assert result["message"] == ("Preview only. Nothing was sent. The user switches on Let Hermes write on the "
                                 "Forgebrücke page.")
    assert not {"would_send", "plan", "requests", "dry_run"} & set(result)
    sent_text = args.get("body") or args["description"]
    assert sent_text not in json.dumps(result, ensure_ascii=False)


def test_a_write_the_forge_refuses_answers_one_summary_line_without_the_body():
    k = Kit()
    args = {**COMMENT, "body": "x" * 5000}
    k.gate(tool_name="forgejo_comment", args=args, session_id="s1")
    bot = k.forge.accounts[BOT]
    bot["scopes"] = (bot["scopes"] - {"write:issue"}) | {"read:issue"}  # the forge refuses after the approval
    out = approval.run_write("forgejo_comment", args, "s1", k.load, ATTENDED, k.tickets)
    assert out["ok"] is False and out["error"] == "missing_scope" and out["status"] == 403
    assert out["summary"] == 'a comment on PR #3 "Fix calc.mul"' and out["note"] == sub("tools.read").DATA_NOTE
    assert "plan" not in out and len(json.dumps(out)) < 500


def test_a_comment_result_quotes_the_thread_title_so_it_carries_the_data_note():
    note = sub("tools.read").DATA_NOTE
    assert Kit().call("forgejo_comment", COMMENT)[1]["note"] == note
    assert Kit({"dry_run": True}).call("forgejo_comment", COMMENT)[1]["note"] == note
    opened = Kit().call("forgejo_create_pr", {"repo": DEMO, "head": "fix-mul", "title": "Fix mul", "reviewers": []})[1]
    assert opened["ok"] and "note" not in opened


def test_line_comment_is_a_comment_review_never_an_approval():
    k = Kit()
    _, result = k.call("forgejo_comment", {**COMMENT, "path": "calc.py", "line": 8})
    assert result["ok"]
    method, path, body = k.forge.writes[-1]
    assert path.endswith(f"/repos/{DEMO}/pulls/3/reviews") and body["event"] == "COMMENT"
    assert body["comments"] == [{"path": "calc.py", "new_position": 8, "body": body["comments"][0]["body"]}]
    directive, _ = k.call("forgejo_comment", {"repo": DEMO, "number": 1, "body": "x", "path": "a", "line": 1})
    assert directive["action"] == "block" and "needs a pull request" in directive["message"]


def test_create_pr_default_base_labels_reviewers():
    k = Kit()
    args = {"repo": DEMO, "head": "fix-mul-2", "title": "Fix mul", "body": "a * b", "labels": ["bug"],
            "reviewers": ["acme"]}
    directive, result = k.call("forgejo_create_pr", args)
    assert "a pull request fix-mul-2 → main" in directive["message"] and "asking acme to review" in directive["message"]
    assert result["ok"] and result["number"] and result["review_requested"] == ["acme"]
    (_, p1, b1), (_, p2, b2) = k.forge.writes
    assert p1.endswith(f"/repos/{DEMO}/pulls") and b1["base"] == "main" and b1["labels"] == [1]
    assert p2.endswith("/requested_reviewers") and b2 == {"reviewers": ["acme"]}


def test_create_pr_asks_you_to_review_by_default():
    """Every pull request Hermes opens requests the user's review, unless the call names reviewers."""
    k = Kit({"you": "acme"})
    args = {"repo": DEMO, "head": "fix-mul-3", "title": "Fix mul"}
    directive, result = k.call("forgejo_create_pr", args)
    assert "asking acme to review (you, by default)" in directive["message"]
    assert result["review_requested"] == ["acme"]
    assert k.forge.writes[-1][1].endswith("/requested_reviewers") and k.forge.writes[-1][2] == {"reviewers": ["acme"]}
    k = Kit({"you": "acme"})
    directive, result = k.call("forgejo_create_pr", {**args, "reviewers": ["bob"]})
    assert "asking bob to review (as asked)" in directive["message"] and result["review_requested"] == ["bob"]
    k = Kit({"you": "acme"})
    directive, result = k.call("forgejo_create_pr", {**args, "reviewers": []})
    assert "asking no one to review" in directive["message"] and "review_requested" not in result
    assert not any(w[1].endswith("/requested_reviewers") for w in k.forge.writes)
    k = Kit()  # no you: no default reviewer
    directive, result = k.call("forgejo_create_pr", args)
    assert "to review" not in directive["message"] and "review_requested" not in result


def test_create_pr_refuses_a_target_the_rule_does_not_name_and_unknown_labels():
    k = Kit()
    directive, _ = k.call("forgejo_create_pr", {"repo": DEMO, "head": "x", "base": "release", "title": "t"})
    assert directive["action"] == "block" and "only the default branch main is allowed" in directive["message"]
    k2 = Kit({"writes": [{**RULE, "targets": ["main", "staging"]}]})
    assert k2.call("forgejo_create_pr", {"repo": DEMO, "head": "x", "base": "staging", "title": "t"})[1]["ok"]
    directive, _ = k2.call("forgejo_create_pr", {"repo": DEMO, "head": "x", "base": "release", "title": "t"})
    assert "allows main, staging (targets)" in directive["message"]
    directive, _ = k.call("forgejo_create_pr", {"repo": DEMO, "head": "x", "title": "t", "labels": ["nope"]})
    assert "has no label nope" in directive["message"]


def test_create_pr_refuses_a_branch_that_is_not_on_the_repo_before_anyone_is_asked():
    k = Kit()
    directive, result = k.call("forgejo_create_pr", {"repo": DEMO, "head": "not-pushed", "title": "t"})
    assert directive["action"] == "block" and result is None and k.forge.writes == []
    assert f"Branch not-pushed is not on {DEMO}. Push it first." in directive["message"]


@pytest.mark.parametrize("head", ["a/../../issues/1", "/x", "x/", "a//b", "x.", "a b"])
def test_create_pr_refuses_a_head_that_is_no_branch_name_without_asking_the_forge_for_it(head):
    k = Kit()
    directive, _ = k.call("forgejo_create_pr", {"repo": DEMO, "head": head, "title": "t"})
    assert directive["action"] == "block" and "is not a branch name" in directive["message"]
    assert not any("/branches/" in c[1] for c in k.forge.calls)


def test_status_posts_with_default_context_and_validates():
    k = Kit()
    _, result = k.call("forgejo_status", {"repo": DEMO, "sha": HEAD_SHA, "state": "success",
                                          "description": "manual check passed"})
    assert result["ok"] and result["context"] == "forgebruecke/agent" and result["state"] == "success"
    directive, _ = k.call("forgejo_status", {"repo": DEMO, "sha": HEAD_SHA, "state": "approved", "description": "x"})
    assert directive["action"] == "block" and "state must be one of" in directive["message"]
    directive, _ = k.call("forgejo_status", {"repo": DEMO, "sha": "main", "state": "success", "description": "x"})
    assert "full commit sha" in directive["message"]


@pytest.mark.parametrize("sha,accepted", [
    (HEAD_SHA, True), ("a" * 64, True), (HEAD_SHA.upper(), True),
    (HEAD_SHA[:12], False), (HEAD_SHA[:39], False), (HEAD_SHA + "a", False), ("a" * 63, False),
])
def test_status_takes_only_a_full_sha(sha, accepted):
    k = Kit()
    directive, result = k.call("forgejo_status", {"repo": DEMO, "sha": sha, "state": "success", "description": "x"})
    if accepted:
        assert result["ok"] and k.forge.writes[-1][1].endswith(f"/statuses/{sha.lower()}")
        assert sha.lower() in result["summary"]
    else:
        assert directive["action"] == "block" and "full commit sha" in directive["message"] and k.forge.writes == []


def test_non_write_tools_pass_the_hook_untouched():
    k = Kit()
    assert k.gate(tool_name="forgejo_get", args={"kind": "repo", "repo": DEMO}, session_id="s1") is None
    assert k.gate(tool_name="terminal", args={"command": "ls"}, session_id="s1") is None


def test_every_write_plan_avoids_approve_and_merge():
    k = Kit()
    acc = k.load()
    plans = [write.plan(acc, "forgejo_comment", COMMENT),
             write.plan(acc, "forgejo_comment", {**COMMENT, "path": "calc.py", "line": 1}),
             write.plan(acc, "forgejo_create_pr", {"repo": DEMO, "head": "x", "title": "t", "reviewers": ["acme"]}),
             write.plan(acc, "forgejo_status", {"repo": DEMO, "sha": HEAD_SHA, "state": "failure", "description": "d"})]
    for p in plans:
        for r in p.requests:
            assert "/merge" not in r.path
            assert r.body.get("event", "COMMENT") == "COMMENT"


def _string_constants(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_only_the_flow_engine_may_hold_a_merge_call_and_nothing_holds_an_approval():
    # the engine (promote, flow lane) and the desk's merge button (owner lane, the user's click; its route
    # path lives in the dashboard adapter). No agent tool merges.
    allowed_merge = {ROOT / "flows" / "engine.py", ROOT / "desk.py", ROOT / "dashboard" / "plugin_api.py"}
    modules = [p for p in ROOT.rglob("*.py") if "tests" not in p.parts]
    assert len(modules) >= 8
    for module in modules:
        strings = _string_constants(module)
        assert "APPROVE" not in strings, module
        if module not in allowed_merge:
            assert not any("/merge" in s for s in strings), module


def test_run_context_fails_closed_when_the_one_shot_marker_is_gone(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_session_finite(name, *args, **kwargs):
        if name == "gateway.session_finite":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_session_finite)

    class Ctx:
        def current_cron_execution(self):
            return None

        def session_surface(self):
            return "cli"

    run = approval.run_context_from(Ctx())
    assert run.unknown and "cannot tell" in run.unattended_reason()


def test_editing_the_plugins_own_code_is_a_guarded_settings_change(tmp_path):
    """Seen in use: an agent chat patched tools/repos.py to widen the tool's schema."""
    approval = sub("approval")
    root = approval.PLUGIN_ROOT
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "forgebruecke").symlink_to(root)
    assert "own code" in approval.settings_touch("patch", {"path": str(root / "tools" / "repos.py")}, home)
    assert "own code" in approval.settings_touch("write_file", {"path": str(home / "plugins/forgebruecke/approval.py")}, home)
    assert "own code" in approval.settings_touch("terminal", {"command": f"sed -i s/a/b/ {root}/approval.py"}, home)
    assert approval.settings_touch("terminal", {"command": f"cat {root}/approval.py"}, home) is None  # a read
    assert approval.settings_touch("patch", {"path": str(tmp_path / "notes.md")}, home) is None
    assert "own code" in approval.settings_touch("patch", {"path": "plugins/forgebruecke/approval.py"}, home)  # base unknown


def test_a_checkout_that_is_not_the_loaded_plugin_is_ordinary_code(tmp_path):
    """A profile that develops Forgebrücke edits worktrees of it; only the copy it loaded is guarded."""
    approval = sub("approval")
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "forgebruecke").symlink_to(approval.PLUGIN_ROOT)
    worktree = tmp_path / "hermes-plugins-feature" / "plugins" / "forgebruecke"
    (worktree / "tools").mkdir(parents=True)
    assert approval.settings_touch("patch", {"path": str(worktree / "approval.py")}, home) is None
    assert approval.settings_touch("write_file", {"path": str(worktree / "tools" / "repos.py")}, home) is None
    assert approval.settings_touch("terminal", {"command": f"cd {worktree.parent.parent} && sed -i s/a/b/ "
                                                           "plugins/forgebruecke/approval.py"}, home) is None
    assert "own code" in approval.settings_touch("terminal", {"command": "sed -i s/a/b/ "
                                                              "$HERMES_HOME/plugins/forgebruecke/approval.py"}, home)
