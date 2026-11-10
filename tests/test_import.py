"""M9: import, push mirror and create_from_template through forgejo_repos."""
import json

from fb_fakes import BASE, DEMO, make_token, sub
from test_repos import BOT, OWN, approved_run, forge

repos = sub("tools.repos")
GH = "https://github.com/octocat/git-consortium"


def writes(f, suffix):
    return [w for w in f.writes if w[1].endswith(suffix)]


def test_import_from_github_brings_issues_and_prs_into_the_bots_account():
    f = forge()
    verdict, out = approved_run(f, {"action": "import", "source": GH})
    assert verdict["action"] == "approve" and "git-consortium" in verdict["message"]
    (_, _, body), = writes(f, "/repos/migrate")
    assert body["service"] == "github" and body["repo_owner"] == "forgebruecke-bot"
    assert body["repo_name"] == "git-consortium" and body["private"] is True and body["mirror"] is False
    assert all(body[i] for i in ("issues", "pull_requests", "labels", "milestones", "releases"))
    assert "wiki" not in body and "auth_token" not in body
    assert out["ok"] and out["came_over"] == {"issues": 3, "pull_requests": 1, "labels": 1, "milestones": 0,
                                              "releases": 1}
    assert out["changeover"][1].startswith("git remote set-url origin")


def test_import_from_a_plain_git_host_brings_only_the_code():
    f = forge()
    _, out = approved_run(f, {"action": "import", "source": "https://code.example.org/x/tool.git"})
    (_, _, body), = writes(f, "/repos/migrate")
    assert body["service"] == "git" and body["repo_name"] == "tool"
    assert not any(k in body for k in ("issues", "pull_requests", "labels"))
    assert out["came_over"] == {}


def test_import_refuses_credentials_in_the_url_and_forgejo_decides_a_foreign_owner():
    f = forge()
    verdict, out = approved_run(f, {"action": "import", "source": "https://user:pw@github.com/a/b"})
    assert verdict["action"] == "block" and out["error"] == "bad_args"
    # D27: no plugin rule names owners any more; the card names the owner, and Forgejo refuses what the
    # agent account may not do
    verdict, out = approved_run(f, {"action": "import", "source": GH, "owner": "acme"})
    assert verdict["action"] == "approve" and "as acme/git-consortium" in verdict["message"]
    assert out["ok"] is False and out["status"] == 403 and f.writes == []


def test_a_failed_migration_surfaces_the_forgejo_error():
    f = forge()
    _, out = approved_run(f, {"action": "import", "source": "https://github.com/octocat/fail-me"})
    assert out["ok"] is False and "Migration failed: Authentication failed" in out["message"]


def test_mirror_stores_the_token_only_in_the_request_never_on_the_card():
    f = forge()
    verdict, out = approved_run(f, {"action": "mirror", "repo": OWN, "target": DEMO})
    assert verdict["action"] == "approve" and BOT not in verdict["message"]
    (_, _, body), = writes(f, "/push_mirrors")
    assert body["remote_password"] == BOT and body["remote_username"] == "forgebruecke-bot"
    assert body["remote_address"].endswith(f"{DEMO}.git") and body["sync_on_commit"] is True
    assert len(writes(f, "/push_mirrors-sync")) == 1
    assert out["ok"] and "remote_password" not in json.dumps(out)


def test_mirror_needs_admin_on_the_source():
    f = forge()
    _, out = approved_run(f, {"action": "mirror", "repo": DEMO, "target": OWN})
    assert out["error"] == "needs_admin" and f.writes == []


def test_create_from_template_asks_for_protection_and_reads_it_back():
    f = forge()
    f.accounts[BOT]["scopes"] = f.accounts[BOT]["scopes"] | {"write:user"}
    f.data[OWN]["info"].update(template=True, id=34)
    f.data[OWN]["protections"] = [{"rule_name": "main"}]
    verdict, out = approved_run(f, {"action": "create_from_template", "repo": OWN, "name": "fresh"})
    (_, path, body), = writes(f, "/generate")
    assert path.endswith(f"/repos/{OWN}/generate") and body["protected_branch"] is True
    assert body["owner"] == "forgebruecke-bot" and body["name"] == "fresh" and body["git_content"] is True
    assert out["ok"] and out["protection"] == ["main"]
    assert out["web_link"] == f"{BASE}/repo/create?template_id=34"


def test_create_from_a_repo_that_is_no_template_is_refused():
    f = forge()
    _, out = approved_run(f, {"action": "create_from_template", "repo": OWN, "name": "fresh"})
    assert out["error"] == "not_template" and f.writes == []


def test_the_schema_offers_every_action_the_tool_handles():
    """The M9 build first shipped the new actions without them in the schema enum (Hermes refused the call)."""
    enum = set(repos.SCHEMA["parameters"]["properties"]["action"]["enum"])
    assert enum == set(repos.GATED) | {"overview", "audit"}


def test_an_import_that_outlives_the_timeout_is_polled_not_called_nothing_changed(monkeypatch):
    f = forge()
    real = f.__call__

    def slow(method, url, headers, body, timeout):
        if url.endswith("/repos/migrate"):
            assert timeout == repos.MIGRATE_TIMEOUT_S
            real(method, url, headers, body, timeout)  # the forge finishes the import...
            raise sub("client").ForgejoError(None, "timeout", "The forge did not answer in time (timed out). The "
                                             "write may still have landed: check before you repeat it.",
                                             may_have_landed=True)  # ...late
        return real(method, url, headers, body, timeout)
    monkeypatch.setattr(repos, "_sleep", lambda s: None)
    _, out = approved_run(slow, {"action": "import", "source": GH})
    assert out["ok"] and out["came_over"]["issues"] == 3 and "took longer than one request" in out["message"]
    assert "nothing was changed" not in json.dumps(out)


def test_a_write_timeout_never_claims_nothing_changed():
    client = sub("client")
    import urllib.request

    def boom(*a, **k):
        raise TimeoutError("The read operation timed out")
    orig = urllib.request.urlopen
    urllib.request.urlopen = boom
    try:
        for method, kind in (("POST", "timeout"), ("GET", "unreachable")):
            try:
                client.urllib_transport(method, "https://forge.example.test/api/v1/x", {}, None, 1)
            except client.ForgejoError as e:
                assert e.kind == kind
                assert "nothing was changed" not in e.message
                assert ("may have landed" in e.message) == (method == "POST")
    finally:
        urllib.request.urlopen = orig


def test_create_from_template_without_write_user_is_dark_with_the_web_link():
    """Measured 2026-11-09: generate into the bot's own account answers 403 without write:user."""
    f = forge()
    f.data[OWN]["info"].update(template=True, id=34)
    verdict, out = approved_run(f, {"action": "create_from_template", "repo": OWN, "name": "fresh"})
    assert verdict["action"] == "block" and out["error"] == "dark"
    assert "write:user" in out["message"] and f"{BASE}/repo/create?template_id=34" in out["message"]
    assert not writes(f, "/generate")
