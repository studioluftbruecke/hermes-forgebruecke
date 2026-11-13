"""Waiting for me: the repository filter, "Ask something else", the green Merge.

The functions run in node, cut out of desktop/plugin.js; the render itself is checked outside this suite."""
import json
import re
import shutil
import subprocess

import pytest

from fb_fakes import ROOT

SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def run_js(names, consts, body):
    fns = [re.search(r"export function " + n + r"\(.*?\n\}", SOURCE, re.S).group(0).replace("export ", "") for n in names]
    defs = [re.search(r"export const " + c + r" = .*", SOURCE).group(0).replace("export ", "") for c in consts]
    script = "\n".join(defs + fns) + "\n" + body
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def row(n, repo, tab="waiting", section="now"):
    return {"id": f"{repo}#{n}", "number": n, "repo": repo, "tab": tab, "section": section}


ROWS = [row(1, "acme/web"), row(2, "acme/api"), row(3, "acme/web"), row(4, "acme/docs", section="parked"),
        row(5, "acme/zed", tab="running"), row(6, "acme/api")]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_one_button_per_repo_with_waiting_rows_sorted_and_counted():
    out = run_js(["repoButtons"], [], f"process.stdout.write(JSON.stringify(repoButtons({json.dumps(ROWS)})))")
    # docs has only a parked row and zed only a running one: no button. One owner, so the label drops it.
    assert out == [{"repo": "acme/api", "count": 2, "label": "api"}, {"repo": "acme/web", "count": 2, "label": "web"}]
    mixed = run_js(["repoButtons"], [], "process.stdout.write(JSON.stringify(repoButtons("
                   + json.dumps([row(1, "b/x"), row(2, "a/y")]) + ").map(b => b.label)))")
    assert mixed == ["a/y", "b/x"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_order_never_follows_the_selection_or_the_counts():
    more_web = ROWS + [row(7, "acme/web"), row(8, "acme/web")]
    out = run_js(["repoButtons"], [], f"process.stdout.write(JSON.stringify([repoButtons({json.dumps(ROWS)}), "
                 f"repoButtons({json.dumps(more_web)})].map(bs => bs.map(b => b.repo))))")
    assert out[0] == out[1] == ["acme/api", "acme/web"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_filter_falls_back_to_all_when_its_repo_leaves_the_list():
    gone = [r for r in ROWS if r["repo"] != "acme/api"] + [row(9, "acme/new")]
    body = (f"const b1 = repoButtons({json.dumps(ROWS)}), b2 = repoButtons({json.dumps(gone)}),"
            f" b3 = repoButtons({json.dumps([row(1, 'acme/web')])})\n"
            "process.stdout.write(JSON.stringify([pickRepo(b1, 'acme/api'), pickRepo(b2, 'acme/api'),"
            " pickRepo(b1, ALL_REPOS), pickRepo(b1, 'acme/nope'), pickRepo(b3, 'acme/web'), pickRepo([], 'acme/web')]))")
    out = run_js(["repoButtons", "pickRepo"], ["ALL_REPOS"], body)
    assert out == ["acme/api", "all", "all", "all", "all", "all"]


def test_the_choice_lives_in_the_desk_page_so_refresh_and_tabs_keep_it():
    desk = SOURCE[SOURCE.index("function DeskBody("):SOURCE.index("// A render error")]
    assert "const [repo, setRepo] = useState(ALL_REPOS)" in desk and "repo, rows: data.rows || [], setRepo" in desk
    rows = SOURCE[SOURCE.index("function RowList("):SOURCE.index("// Every repo Hermes")]
    assert "const active = pickRepo(buttons, repo)" in rows and "if (tab === 'waiting' && active !== repo) setRepo(active)" in rows
    assert "(active === ALL_REPOS || r.repo === active)" in rows


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_ask_something_else_drafts_only_the_reference_and_a_space():
    r = {"repo": "acme/web", "number": 12}
    out = run_js(["askDraft"], [], f"const r = {json.dumps(r)}\n"
                 "process.stdout.write(JSON.stringify([askDraft(r), askDraft(r, 'Summarise this pull request.')]))")
    assert out == ["@forgejo:acme/web#12 ", "@forgejo:acme/web#12 Summarise this pull request."]
    ask = SOURCE[SOURCE.index("function AskHermes("):SOURCE.index("// ── rows")]
    assert "export const ASK_OWN = 'Ask something else…'" in SOURCE
    assert ask.index("el(MenuLine") < ask.index("onSelect: () => openChat(askDraft(row))")
    assert ask.rstrip().endswith("ASK_OWN)))\n}")  # the last item


def test_every_merge_button_is_green_from_the_theme():
    actions = SOURCE[SOURCE.index("function RowActions("):SOURCE.index("function Row(")]
    assert actions.count("onMerge(row)") == 1 and "className: 'fb-merge'" in actions
    sheet = SOURCE[SOURCE.index("const SHEET = ["):SOURCE.index("].join('\\n')")]
    assert "color-mix(in srgb,' + T.green + ' 78%,black)" in sheet and ".fb-merge:hover" in sheet
    assert "[role=dialog]:has(.fb-merge-dialog) button[data-variant=default]" in sheet
    dialog = SOURCE[SOURCE.index("function MergeDialog("):SOURCE.index("// ── the desk page")]
    assert "className: 'fb-merge-dialog'" in dialog
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", sheet)
