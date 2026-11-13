"""The desk as the agent reads it: a page at a time, one repo on request, and what is missing said in full."""
import argparse
import json

from fb_fakes import BASE, DEMO, HEAD_SHA, DictState, open_flow_prs, sub
from fb_fakes_tools_read import OTHER, kit, plugin

state_mod = sub("flows.state")


def flow_pr(n, repo=DEMO):
    return {"repo": repo, "pr": n, "flow": "demo", "head_sha": HEAD_SHA, "title": f"PR {n}",
            "url": f"{BASE}/{repo}/pulls/{n}", "updated_at": 100 + n,
            "gates": [{"gate_key": "review", "type": "review", "state": "failed", "detail": ""}]}


def desk(tmp_path, args=None, *, prs=(), settings=None, engine=None):
    """``forgejo_list kind=desk`` over the flow state in ``tmp_path``. Returns ``(answer, forge)``."""
    store = state_mod.FlowStore(tmp_path)
    for pr in prs:
        store.save_pr(pr)
    if engine:
        store.write_engine(engine)
    forge, gs, sec = kit(settings={"you": "acme", **(settings or {})})
    open_flow_prs(forge, *store.all_prs())
    handler = plugin.make_read_handler("forge_list", gs, sec, transport=forge, data_dir=tmp_path)
    return json.loads(handler({"kind": "desk", **(args or {})})), forge


def numbers(out):
    return [row["number"] for row in out["rows"]]


def test_the_desk_comes_a_page_at_a_time_and_every_row_can_be_read(tmp_path):
    prs = [flow_pr(n) for n in range(10, 70)]
    first, _ = desk(tmp_path, prs=prs)
    assert len(first["rows"]) == 20 and first["total"] == 61 and first["has_more"] is True and first["next_page"] == 2
    assert first["counts"] == {"waiting": 1, "parked": 0, "running": 0, "blocked": 60}
    seen = numbers(first)
    page = first
    while page.get("next_page"):
        page, _ = desk(tmp_path, {"page": page["next_page"]})
        seen += numbers(page)
    assert page["has_more"] is False and sorted(seen) == sorted([3, *range(10, 70)])
    small, _ = desk(tmp_path, {"page": 2, "limit": 5})
    assert numbers(small) == seen[5:10] and small["next_page"] == 3


def test_the_desk_of_one_repo_lists_and_counts_that_repo_only(tmp_path):
    out, _ = desk(tmp_path, {"repo": OTHER}, prs=[flow_pr(7), flow_pr(8, OTHER), flow_pr(9, OTHER)])
    assert {row["repo"] for row in out["rows"]} == {OTHER} and sorted(numbers(out)) == [8, 9]
    assert out["total"] == 2 and out["counts"] == {"waiting": 0, "parked": 0, "running": 0, "blocked": 2}
    upper, _ = desk(tmp_path, {"repo": OTHER.upper()})
    assert sorted(numbers(upper)) == [8, 9]
    bad, _ = desk(tmp_path, {"repo": "not a repo"})
    assert bad["ok"] is False and bad["error"] == "bad_repo"


def test_without_you_the_answer_says_what_is_not_listed_who_sets_it_and_the_suggestion(tmp_path):
    out, _ = desk(tmp_path, settings={"you": ""})
    assert out["notes"] == ['Review requests are not listed, because "You" is not set. The user sets it on the '
                            "Forgebrücke page (suggested: @acme)."]
    assert "you" not in out


def test_the_last_flow_runs_errors_are_among_the_notes(tmp_path):
    out, _ = desk(tmp_path, prs=[flow_pr(7)],
                  engine={"at": 1, "source": "gateway", "errors": [f"{DEMO}#7: the review worker died"]})
    assert f"Last flow run: {DEMO}#7: the review worker died" in out["notes"]


def test_the_terminal_says_how_many_rows_it_left_out(tmp_path, capsys):
    store = state_mod.FlowStore(tmp_path)
    for n in range(10, 70):
        store.save_pr(flow_pr(n))
    forge, gs, sec = kit(settings={"you": "acme"})
    open_flow_prs(forge, *store.all_prs())
    handler = plugin.make_cli_handler(gs, sec, state=DictState(tmp_path), transport=forge)
    assert handler(argparse.Namespace(forgebruecke_command="desk", json=False)) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 52 and lines[-1] == "note: 11 more are not listed."


def test_the_skill_says_that_only_the_user_sets_you():
    rules = " ".join(plugin.SKILL_PATH.read_text(encoding="utf-8").split("## Rules")[1].split())
    assert "Only the user sets `you`" in rules and "Forgebrücke page" in rules
