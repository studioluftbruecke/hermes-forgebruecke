"""The Repos tab (feedback round 2026-11-12, FB-3): open pull requests that expand with Merge, actions per repo,
list or grid.

Konstantin: "there could be a way to directly expand the PRs in the repos list and merge directly from there".
The backend half is ``desk.api_repo_pulls`` (``GET /repo-pulls``) on the tests' stand-in Forgejo; the page's
functions run in node, cut out of desktop/plugin.js. The render is proven by the harness under experiments/."""
import json
import re
import shutil
import subprocess

import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, ROOT, FakeForgejo, bot_account, make_token, open_flow_prs, owner_account, \
    sample_data, sub

desk = sub("desk")
state_mod = sub("flows.state")
BOT, OWNER = make_token("bot"), make_token("owner")
NODE = shutil.which("node")
SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
API = (ROOT / "dashboard" / "plugin_api.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def fresh_cache():
    desk.clear_cache()
    yield
    desk.clear_cache()


def forge():
    owner = {**owner_account(), "scopes": {"write:repository", "write:issue", "read:organization", "read:user"}}
    return FakeForgejo({BOT: bot_account(), OWNER: owner}, data=sample_data())


def getters(you="acme", owner_token=True):
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}
    if owner_token:
        secrets["FORGEBRUECKE_PERSONAL_WRITE_TOKEN"] = OWNER
    values = {"url": BASE, "dry_run": False, "you": you, "writes": [{"repo_glob": DEMO, "flows": ["demo"]}]}
    return (lambda k, d=None: values.get(k, d)), secrets.get


def add_pr(f, n, **changes):
    """Another open pull request on the demo repo, asking nobody, by hermes."""
    pr = {**f.data[DEMO]["pulls"][3], "number": n, "title": f"PR {n}", "user": {"login": "hermes"},
          "requested_reviewers": [], "html_url": f"{BASE}/{DEMO}/pulls/{n}", "updated_at": "2026-11-08T12:00:00Z",
          **changes}
    f.data[DEMO]["pulls"][n] = pr
    f.data[DEMO]["issues"][n] = {**f.data[DEMO]["issues"][3], "number": n}
    f.data[DEMO]["reviews"][n] = []
    return pr


def pulls_of(tmp_path, f, repo=DEMO, **kw):
    gs, sec = getters(**kw)
    return desk.api_repo_pulls(gs, sec, repo, data_dir=tmp_path, transport=f, now=lambda: 1794500000)


def by_number(body):
    return {p["number"]: p for p in body["pulls"]}


# --- the backend: one repo's open pull requests, read on expand ---------------------------------------

def test_the_expanded_repo_lists_every_open_pull_request_with_checks_and_merge(tmp_path):
    f = forge()
    add_pr(f, 8)
    status, body = pulls_of(tmp_path, f)
    assert status == 200 and body["ok"] is True, body
    pulls = by_number(body)
    assert sorted(pulls) == [3, 8]
    p = pulls[8]
    assert (p["id"], p["repo"], p["title"], p["author"], p["base"], p["head_sha"]) == \
        (f"{DEMO}#8", DEMO, "PR 8", "hermes", "main", HEAD_SHA)
    assert p["url"] == f"{BASE}/{DEMO}/pulls/8" and p["age_s"] > 0
    assert p["checks"]["state"] == "failure" and {s["context"] for s in p["checks"]["statuses"]} == {"devops/tests", "devops/review"}
    # #8 asks nobody for a review, so it is not in Waiting for me; Merge shows anyway: his account may merge.
    assert p["can_merge"] is True and pulls[3]["can_merge"] is True


def test_the_default_branch_line_is_its_latest_commit_and_its_checks(tmp_path):
    f = forge()
    f.data[DEMO]["status"]["1" * 40] = {"state": "success", "sha": "1" * 40, "total_count": 1, "statuses": [
        {"context": "ci/build", "status": "success", "description": "built"}]}
    body = pulls_of(tmp_path, f)[1]
    assert body["default_branch"] == "main"
    assert body["branch"]["name"] == "main" and body["branch"]["sha"] == "1" * 40
    assert body["branch"]["checks"]["state"] == "success"
    assert body["branch"]["url"] == f"{BASE}/{DEMO}/commit/" + "1" * 40


def test_merge_hides_where_the_merge_route_would_refuse(tmp_path):
    f = forge()
    add_pr(f, 9, draft=True)
    add_pr(f, 10, mergeable=False)
    st = {"repo": DEMO, "pr": 11, "flow": "demo", "head_sha": HEAD_SHA, "title": "PR 11", "base": "main",
          "url": f"{BASE}/{DEMO}/pulls/11", "updated_at": 111,
          "gates": [{"gate_key": "review", "type": "review", "state": "running", "detail": ""},
                    {"gate_key": "human", "type": "human", "state": "pending", "detail": ""}]}
    state_mod.FlowStore(tmp_path).save_pr(st)
    open_flow_prs(f, st)
    pulls = by_number(pulls_of(tmp_path, f)[1])
    assert (pulls[9]["can_merge"], pulls[10]["can_merge"]) == (False, False)
    assert pulls[11]["flow"] == "demo" and pulls[11]["can_merge"] is False  # the flow's gate does not wait for him
    desk.clear_cache()
    assert not any(p["can_merge"] for p in pulls_of(tmp_path, f, owner_token=False)[1]["pulls"])


def test_a_bad_or_unseen_repo_is_said_and_not_read(tmp_path):
    f = forge()
    status, body = pulls_of(tmp_path, f, repo="no slash")
    assert status == 400 and body["ok"] is False
    status, body = pulls_of(tmp_path, f, repo="acme/hidden")
    assert status == 404 and "cannot see acme/hidden" in body["message"]


def test_a_merge_drops_the_expanded_list_and_the_repo_count(tmp_path):
    f = forge()
    add_pr(f, 8)
    gs, sec = getters()
    card = next(c for c in desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1]["repos"] if c["repo"] == DEMO)
    assert card["open_prs"] == 2
    assert sorted(by_number(pulls_of(tmp_path, f)[1])) == [3, 8]
    status, out = desk.api_merge(gs, sec, {"repo": DEMO, "number": 8, "head_sha": HEAD_SHA}, data_dir=tmp_path, transport=f)
    assert status == 200 and out["merged"] is True, out
    assert sorted(by_number(pulls_of(tmp_path, f)[1])) == [3]  # the merge dropped the cached answer
    card = next(c for c in desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1]["repos"] if c["repo"] == DEMO)
    assert card["open_prs"] == 1


def test_the_route_is_mounted_and_the_page_reads_it_only_on_expand():
    assert '@router.get("/repo-pulls")' in API and "api_repo_pulls(_get_setting, _get_secret, repo" in API
    assert SOURCE.count("rest('/repo-pulls?repo=' + encodeURIComponent(repo))") == 1
    # The query lives in RepoPulls, which RepoCard draws only while the repo is open.
    pulls = SOURCE[SOURCE.index("function RepoPulls("):SOURCE.index("// One repo, as a line")]
    assert "useRepoPulls(repo, true)" in pulls
    card = SOURCE[SOURCE.index("function RepoCard("):SOURCE.index("function Repos(")]
    assert "const pulls = open ? el(RepoPulls," in card
    assert "useRepoPulls" not in SOURCE[SOURCE.index("function Repos("):SOURCE.index("function MergeDialog(")]


# --- the page: functions run in node -------------------------------------------------------------------

def run_js(names, consts, body):
    fns = [re.search(r"export function " + n + r"\(.*?\n\}", SOURCE, re.S).group(0).replace("export ", "") for n in names]
    defs = [re.search(r"export const " + c + r" = .*", SOURCE).group(0).replace("export ", "") for c in consts]
    script = "\n".join(defs + fns) + "\n" + body
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_waiting_for_you_jumps_to_waiting_for_me_with_the_repo_filter():
    rows = [{"id": "acme/web#1", "repo": "acme/web", "tab": "waiting", "section": "now"},
            {"id": "acme/web#2", "repo": "acme/web", "tab": "waiting", "section": "parked"},
            {"id": "acme/api#3", "repo": "acme/api", "tab": "waiting", "section": "now"},
            {"id": "acme/api#4", "repo": "acme/api", "tab": "waiting", "section": "now"},
            {"id": "acme/zed#5", "repo": "acme/zed", "tab": "running", "section": "now"}]
    body = (f"const rows = {json.dumps(rows)}\nconst calls = []\n"
            "showWaiting('acme/web', t => calls.push(['tab', t]), r => calls.push(['repo', r]))\n"
            "const buttons = repoButtons(rows)\n"
            "process.stdout.write(JSON.stringify([waitingByRepo(rows), calls, pickRepo(buttons, 'acme/web')]))")
    counts, calls, active = run_js(["repoButtons", "waitingByRepo", "showWaiting", "pickRepo"], ["ALL_REPOS"], body)
    assert counts == {"acme/api": 2, "acme/web": 1}  # parked and running rows are not waiting for you
    assert calls == [["repo", "acme/web"], ["tab", "waiting"]]
    assert active == "acme/web"  # the Waiting tab keeps the filter the jump set
    repos = SOURCE[SOURCE.index("function Repos("):SOURCE.index("function MergeDialog(")]
    assert "onWaiting: () => showWaiting(r.repo, setTab, setRepo)" in repos
    assert "waiting: waiting[r.repo.toLowerCase()] || 0" in repos
    card = SOURCE[SOURCE.index("function RepoCard("):SOURCE.index("function Repos(")]
    assert "onClick: onWaiting" in card and "waiting + ' waiting for you'" in card


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_merge_from_the_expanded_list_updates_the_count_and_waiting_for_me():
    data = {"label": "Forgebrücke · 2", "counts": {"waiting": 2, "parked": 0, "running": 0, "blocked": 0},
            "rows": [{"id": "acme/web#1", "repo": "acme/web", "tab": "waiting", "section": "now"},
                     {"id": "acme/api#3", "repo": "acme/api", "tab": "waiting", "section": "now"}],
            "repos": [{"repo": "acme/web", "open_prs": 3}, {"repo": "acme/api", "open_prs": 1}]}
    pulls = {"ok": True, "pulls": [{"id": "acme/Web#1"}, {"id": "acme/web#1"}, {"id": "acme/web#7"}]}
    body = (f"const d = {json.dumps(data)}, p = {json.dumps(pulls)}\n"
            "process.stdout.write(JSON.stringify([withMerged(d, {id: 'acme/web#1', repo: 'acme/web'}),"
            " withMerged(d, {id: 'acme/web#7', repo: 'ACME/web'}), withMerged(null, {id: 'x'}),"
            " withoutPull(p, 'acme/web#1'), withoutPull(undefined, 'x')]))")
    waiting_row, other, none, left, nothing = run_js(["formatNavLabel", "withoutRow", "withMerged", "withoutPull"], [], body)
    assert [r["id"] for r in waiting_row["rows"]] == ["acme/api#3"] and waiting_row["counts"]["waiting"] == 1
    assert waiting_row["label"] == "Forgebrücke · 1"
    assert waiting_row["repos"] == [{"repo": "acme/web", "open_prs": 2}, {"repo": "acme/api", "open_prs": 1}]
    # A pull request that was not in Waiting for me: the rows stay, the repo's count still drops.
    assert len(other["rows"]) == 2 and other["repos"][0]["open_prs"] == 2
    assert none is None and nothing is None
    assert [x["id"] for x in left["pulls"]] == ["acme/Web#1", "acme/web#7"]
    dialog = SOURCE[SOURCE.index("function MergeDialog("):SOURCE.index("// ── the desk page")]
    assert dialog.index("withoutPull(client.getQueryData(pullsKey), row.id)") < dialog.index("invalidateQueries")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_layout_is_list_or_grid_and_the_repo_question_is_drafted():
    out = run_js(["readLayout", "repoAskDraft"], ["LAYOUTS", "REPO_QUESTION"],
                 "process.stdout.write(JSON.stringify([readLayout('grid'), readLayout('list'), readLayout(null),"
                 " readLayout('tiles'), repoAskDraft('acme/web')]))")
    assert out == ["grid", "list", "list", "list", "@forgejo:acme/web What changed here this week?"]
    assert "gridTemplateColumns: 'repeat(auto-fill, minmax(20rem, 1fr))'" in SOURCE
    assert "'forgebruecke:repos-layout:' + (profile || 'default')" in SOURCE  # kept per profile


def test_grid_cards_in_one_row_have_one_height_and_the_count_line_at_the_bottom():
    """Konstantin on the grid (2026-11-12): "Yes, they should match." A badge made a card ~20 px taller than its
    neighbours. The grid stretches every card of a row, and the count line is pushed to the card's bottom, so the
    lines align. run-fb3.cjs measures it in both themes at 1280 and 1600 px."""
    grid = SOURCE[SOURCE.index("const GRID = {"):SOURCE.index("}", SOURCE.index("const GRID = {"))]
    assert "alignItems: 'stretch'" in grid and "'start'" not in grid
    card = SOURCE[SOURCE.index("function RepoCard("):SOURCE.index("function Repos(")]
    grid_card = card[card.index("if (layout === 'grid')"):card.index("  return el('div', { style: { ...S.col(4)")]
    assert "justifyContent: 'space-between', marginTop: 'auto' }, 'data-test': 'repo-count'" in grid_card
