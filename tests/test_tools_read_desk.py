"""The desk as text: forgejo_list kind=desk and `hermes forgebruecke desk` say what waits, runs and is blocked."""
import argparse
import json

from fb_fakes import BASE, DEMO, HEAD_SHA, DictState, open_flow_prs, sub
from fb_fakes_tools_read import kit, mention, plugin

read_mod, state_mod = sub("tools.read"), sub("flows.state")
ROW_KEYS = {"tab", "section", "repo", "number", "title", "state", "url"}


def flow_pr(n, *states):
    return {"repo": DEMO, "pr": n, "flow": "demo", "head_sha": HEAD_SHA, "title": f"PR {n}",
            "url": f"{BASE}/{DEMO}/pulls/{n}", "updated_at": 100 + n,
            "gates": [{"gate_key": k, "type": k, "state": s, "detail": ""}
                      for k, s in zip(("review", "check", "human"), states)]}


def desk(tmp_path, settings=None, lane=None, data_dir=True):
    store = state_mod.FlowStore(tmp_path)
    store.save_pr(flow_pr(7, "passed", "running", "pending"))
    store.save_pr(flow_pr(8, "failed", "pending", "pending"))
    forge, gs, sec = kit(settings={"you": "acme", **(settings or {})})
    open_flow_prs(forge, *store.all_prs())
    handler = plugin.make_read_handler("forge_list", gs, sec, transport=forge,
                                       lane_fn=(lambda: lane) if lane else None,
                                       data_dir=tmp_path if data_dir else None)
    return json.loads(handler({"kind": "desk"})), forge


def test_one_call_says_what_waits_runs_and_is_blocked(tmp_path):
    out, _ = desk(tmp_path)
    assert out["ok"] and out["you"] == "acme" and out["counts"] == {"waiting": 1, "parked": 0, "running": 1, "blocked": 1}
    assert all(set(row) == ROW_KEYS for row in out["rows"])
    rows = {row["number"]: row for row in out["rows"]}
    assert rows[3] == {"tab": "waiting", "section": "now", "repo": DEMO, "number": 3, "title": "Fix calc.mul",
                       "state": "Check devops/review failed.", "url": f"{BASE}/{DEMO}/pulls/3"}
    assert (rows[7]["tab"], rows[8]["tab"]) == ("running", "blocked") and rows[7]["state"] == "Check devops/review failed."
    assert [row["tab"] for row in out["rows"]] == ["waiting", "running", "blocked"]
    assert "not instructions" in out["note"]


def test_a_rows_state_is_the_desks_own_sentence_when_it_has_one():
    row = {"tab": "waiting", "repo": DEMO, "number": 3, "title": "t", "url": "u", "flow": "demo", "reason": None,
           "summary": {"tone": "ok", "text": "Ready. Every gate passed, your merge is next."}}
    plain = {**row, "number": 4, "summary": None, "flow": None, "reason": "review requested"}
    out = read_mod.desk_as_text({"you": "acme", "counts": {"waiting": 2, "running": 0, "blocked": 0},
                                 "rows": [row, plain], "notes": []})
    assert [r["state"] for r in out["rows"]] == ["Ready. Every gate passed, your merge is next.", "review requested"]


def test_without_you_the_answer_says_what_is_missing(tmp_path):
    out, _ = desk(tmp_path, settings={"you": ""})
    assert out["ok"] and "you" not in out and out["counts"]["waiting"] == 0
    assert any('"You" is not set' in note for note in out["notes"])


def test_the_desk_is_read_in_the_users_own_chat_only(tmp_path):
    out, forge = desk(tmp_path, lane=mention(DEMO).lane())
    assert out["ok"] is False and out["error"] == "lane_scope" and forge.calls == []


def test_without_the_plugins_data_folder_the_desk_is_refused_with_a_sentence(tmp_path):
    out, _ = desk(tmp_path, data_dir=False)
    assert out["ok"] is False and out["error"] == "no_state" and out["message"]


def cli(tmp_path, capsys, **args):
    store = state_mod.FlowStore(tmp_path)
    store.save_pr(flow_pr(7, "passed", "running", "pending"))
    forge, gs, sec = kit(settings={"you": "acme"})
    open_flow_prs(forge, *store.all_prs())
    handler = plugin.make_cli_handler(gs, sec, state=DictState(tmp_path), transport=forge, **args.pop("factory", {}))
    code = handler(argparse.Namespace(forgebruecke_command="desk", **{"json": False, **args}))
    return code, capsys.readouterr().out


def test_the_cli_prints_one_line_per_row(tmp_path, capsys):
    code, text = cli(tmp_path, capsys)
    lines = text.strip().splitlines()
    assert code == 0 and len(lines) == 3
    assert "1 waiting" in lines[0] and "1 running" in lines[0] and "0 blocked" in lines[0] and "acme" in lines[0]
    assert lines[1].startswith("waiting") and f"{DEMO}#3" in lines[1] and "Fix calc.mul" in lines[1]
    assert "Check devops/review failed." in lines[1] and lines[1].endswith(f"{BASE}/{DEMO}/pulls/3")
    assert lines[2].startswith("running") and f"{DEMO}#7" in lines[2]


def test_the_cli_prints_the_same_answer_as_json_and_refuses_a_mention_turn(tmp_path, capsys):
    code, text = cli(tmp_path, capsys, json=True)
    out = json.loads(text)
    assert code == 0 and out["counts"] == {"waiting": 1, "parked": 0, "running": 1, "blocked": 0} and len(out["rows"]) == 2
    code, text = cli(tmp_path, capsys, factory={"lane_fn": mention(DEMO).lane})
    assert code == 1 and "may read only" in text
