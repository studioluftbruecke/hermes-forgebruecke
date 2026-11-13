"""The Merge button follows Forgejo's merge right for the user's account, and a merged pull
request leaves Waiting for me at once.

A user who may merge should always see a Merge button. A pull request that never requested the user's review
(its timeline holds no review request) was listed as "review not requested yet", and only "review requested"
rows offered Merge. A merged pull request must leave "Waiting for me" at once. Three sources kept a merged row: a flow's stored state says open until the next tick, a desk build
that began before the merge cached its answer after it, and the merge dropped the write-token facts, so the next
read reran the doctor's probes."""
import json
import shutil
import subprocess
import threading

import pytest

from fb_fakes import (BASE, DEMO, HEAD_SHA, ROOT, FakeForgejo, bot_account, make_token, open_flow_prs, owner_account,
                      sample_data, sub)

desk = sub("desk")
state_mod = sub("flows.state")
BOT, OWNER = make_token("bot"), make_token("owner")
NODE = shutil.which("node")
SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def fresh_cache():
    desk.clear_cache()
    yield
    desk.clear_cache()


def forge():
    owner = {**owner_account(), "scopes": {"write:repository", "write:issue", "read:organization", "read:user"}}
    return FakeForgejo({BOT: bot_account(), OWNER: owner}, data=sample_data())


def getters(you="acme"):
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": OWNER}
    values = {"url": BASE, "dry_run": False, "you": you, "writes": [{"repo_glob": DEMO, "flows": ["demo"]}]}
    return (lambda k, d=None: values.get(k, d)), secrets.get


def not_asked(f, n=8):
    """An open pull request by hermes that asks nobody to review it, as #143 did."""
    pr = {**f.data[DEMO]["pulls"][3], "number": n, "title": f"PR {n}", "user": {"login": "hermes"},
          "requested_reviewers": [], "html_url": f"{BASE}/{DEMO}/pulls/{n}"}
    f.data[DEMO]["pulls"][n] = pr
    f.data[DEMO]["issues"][n] = {**f.data[DEMO]["issues"][3], "number": n}
    f.data[DEMO]["reviews"][n] = []
    return pr


def flow_state(n=3):
    gate = lambda k, s: {"gate_key": k, "type": k, "state": s,
                         "detail": "waiting for acme to merge in Forgejo" if s == "waiting" else ""}
    return {"repo": DEMO, "pr": n, "flow": "demo", "head_sha": HEAD_SHA, "title": f"PR {n}", "base": "main",
            "url": f"{BASE}/{DEMO}/pulls/{n}", "updated_at": 100 + n,
            "gates": [gate("review", "passed"), gate("check", "passed"), gate("human", "waiting")]}


def rows(body):
    return {r["number"]: r for r in body["rows"]}


def merge(tmp_path, f, n):
    gs, sec = getters()
    return desk.api_merge(gs, sec, {"repo": DEMO, "number": n, "head_sha": HEAD_SHA}, data_dir=tmp_path, transport=f)


# --- Merge whenever Forgejo lets the user's account merge ------------------------------------------------

def test_a_pr_that_never_asked_for_his_review_offers_merge_and_merges(tmp_path):
    f = forge()
    not_asked(f)
    gs, sec = getters()
    row = rows(desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1])[8]
    assert row["reason"] == "review not requested yet"
    assert (row["can_merge"], row["next"]) == (True, "merge")
    status, body = merge(tmp_path, f, 8)
    assert status == 200 and body["merged"] is True, body


def test_the_merge_right_is_forgejos_answer_for_his_account_on_the_base_branch(tmp_path):
    """Hermes' account may not merge into main (measured: user_can_merge False for
    hermes), the user's may: the row offers Merge. Then the rule takes the user's merge right away: no Merge, and the
    review row's main button is Review in Forgejo again."""
    f = forge()
    branch = f.data[DEMO]["branches"]["main"]
    branch["user_can_merge_by"] = {"acme": True, "forgebruecke-bot": False}
    gs, sec = getters()
    assert rows(desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1])[3]["can_merge"] is True
    branch["user_can_merge_by"] = {"acme": False, "forgebruecke-bot": True}
    desk.clear_cache()
    row = rows(desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1])[3]
    assert row["reason"] == "review requested" and (row["can_merge"], row["next"]) == (False, "review_in_forgejo")
    reads = [c for c in f.calls if c[1].endswith(f"/repos/{DEMO}/branches/main")]
    assert len(reads) == 2  # one per desk build; the answer is kept MERGE_RIGHT_TTL_S


def test_no_merge_without_a_base_branch_answer_or_on_a_draft(tmp_path):
    f = forge()
    del f.data[DEMO]["branches"]["main"]  # Forgejo did not say
    gs, sec = getters()
    assert rows(desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1])[3]["can_merge"] is False
    f = forge()
    f.data[DEMO]["pulls"][3]["draft"] = True
    desk.clear_cache()
    assert rows(desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1])[3]["can_merge"] is False


def test_a_flow_row_reads_the_merge_right_on_the_base_forgejo_returned_in_the_same_build(tmp_path):
    """After a retarget the stored flow state still names the old base until the next tick. The row
    takes the base from the pull request Forgejo listed in this desk build, so the right is read for the
    branch the merge would go into, and the dialog names it."""
    f = forge()
    state_mod.FlowStore(tmp_path).save_pr(flow_state())  # stored base: main
    f.data[DEMO]["pulls"][3]["base"] = {"ref": "release"}
    branches = f.data[DEMO]["branches"]
    branches["release"] = {**branches["main"], "name": "release", "user_can_merge_by": {"acme": True}}
    branches["main"]["user_can_merge_by"] = {"acme": False}
    gs, sec = getters()
    row = rows(desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1])[3]
    assert row["flow"] == "demo" and row["base"] == "release"
    assert (row["can_merge"], row["next"]) == (True, "merge")
    assert not [c for c in f.calls if c[1].endswith(f"/repos/{DEMO}/branches/main")]


# --- the merged row leaves at once ----------------------------------------------------------------

def test_a_merged_flow_row_leaves_the_desk_before_the_flows_tick(tmp_path):
    f = forge()
    state_mod.FlowStore(tmp_path).save_pr(flow_state())
    open_flow_prs(f, flow_state())
    gs, sec = getters()
    assert rows(desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1])[3]["next"] == "merge"
    assert merge(tmp_path, f, 3)[0] == 200
    assert 3 not in rows(desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1])
    assert state_mod.FlowStore(tmp_path).load_pr(DEMO, 3).get("closed") is not True  # the tick closes it out


def test_a_desk_read_that_began_before_the_merge_is_not_served_after_it(tmp_path):
    f = forge()
    gs, sec = getters()
    desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)
    desk.clear_desk_cache()
    listed, merged = threading.Event(), threading.Event()
    real = f.__call__

    def slow(method, url, headers, body, timeout):
        out = real(method, url, headers, body, timeout)
        if threading.current_thread().name == "poll" and url.split("?")[0].endswith(f"/repos/{DEMO}/pulls"):
            listed.set()
            merged.wait(5)
        return out

    poll = threading.Thread(target=lambda: desk.api_desk(gs, sec, data_dir=tmp_path, transport=slow), name="poll")
    poll.start()
    assert listed.wait(5)
    assert merge(tmp_path, f, 3)[0] == 200
    merged.set()
    poll.join(10)
    assert 3 not in rows(desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)[1])


def test_the_read_after_a_merge_reruns_no_write_token_probe(tmp_path):
    f = forge()
    gs, sec = getters()
    desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)
    assert merge(tmp_path, f, 3)[0] == 200
    start = len(f.calls)
    desk.api_desk(gs, sec, data_dir=tmp_path, transport=f)
    assert [c[:2] for c in f.calls[start:] if c[0] != "GET"] == []


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_desktop_drops_the_merged_row_on_the_click():
    import re
    fn = {name: re.search(r"export function " + name + r"\(.*?\n\}", SOURCE, re.S).group(0).replace("export ", "")
          for name in ("formatNavLabel", "withoutRow")}
    data = {"label": "Forgebrücke · 2", "counts": {"waiting": 2, "parked": 1, "running": 1, "blocked": 0},
            "rows": [{"id": "a#1", "tab": "waiting", "section": "now"}, {"id": "a#2", "tab": "waiting", "section": "now"},
                     {"id": "a#3", "tab": "waiting", "section": "parked"}, {"id": "a#4", "tab": "running", "section": "now"}]}
    script = "\n".join(fn.values()) + f"\nconst d = {json.dumps(data)}\n" \
        "process.stdout.write(JSON.stringify([withoutRow(d, 'a#2'), withoutRow(d, 'zz') === d, withoutRow(null, 'a#2')]))"
    out = json.loads(subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert [r["id"] for r in out[0]["rows"]] == ["a#1", "a#3", "a#4"]
    assert out[0]["counts"] == {"waiting": 1, "parked": 1, "running": 1, "blocked": 0}
    assert out[0]["label"] == "Forgebrücke · 1" and out[1] is True and out[2] is None
    dialog = SOURCE[SOURCE.index("function MergeDialog("):SOURCE.index("// ── the desk page")]
    # the dialog calls withMerged, which is withoutRow plus the repo's open pull request count.
    assert dialog.index("withMerged(client.getQueryData(key), row)") < dialog.index("invalidateQueries")
    assert "const next = withoutRow(data, row.id) || data" in SOURCE
