"""The parking area's Desktop half (PLAN-v0.0.21-park.md PK6, § 3 rule 5, § 7): no new SDK import, the routes it
calls, every string of § 7 within the copy budget, inputs in the park dialog that pass only Enter, and the
desk answer it shows before the refetch."""
import json
import re
import shutil
import subprocess

import pytest

from fb_fakes import ROOT

SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
SDK_0020 = {"atom", "Badge", "Button", "Codicon", "ConfirmDialog", "DisclosureCaret", "DropdownMenu",
            "DropdownMenuContent", "DropdownMenuItem", "DropdownMenuTrigger", "EmptyState", "ErrorState", "host",
            "PALETTE_AREA", "ROUTES_AREA", "SearchField", "SegmentedControl", "SIDEBAR_NAV_AREA", "Skeleton", "Tip",
            "ToggleRow", "useQuery", "useQueryClient", "useValue"}


def part(start, end):
    return SOURCE[SOURCE.index(start):SOURCE.index(end, SOURCE.index(start))]


def test_no_new_sdk_import_so_any_desktop_that_loads_0_0_20_loads_it():
    block = re.search(r"^import \{(.*?)\} from '@hermes/plugin-sdk'", SOURCE, re.S | re.M).group(1)
    assert {n.strip() for n in block.split(",") if n.strip()} == SDK_0020
    assert "import { Component, useEffect, useRef, useState } from 'react'" in SOURCE  # useRef from react
    assert not re.search(r"(?<![\w.])fetch\(", SOURCE)  # no direct network call; refetch() is the query's


def test_it_calls_the_three_routes_through_the_plugin_context():
    for route in ("/park", "/unpark", "/comment"):
        assert f"rest('{route}', {{ method: 'POST'" in SOURCE, route


EXACT = ["Park", "Park again", "It leaves your count and comes back when something changes.", "Note (only you see it)",
         "For example: after the staging review", "Also bring it back on new commits",
         "Also post the note on the PR, from @", "Parked #", " is back in Waiting for me.", "Undo", "Parked · ",
         "parked ", "Unpark", "Open", "Back", "Your note: ", "What changed", "Comment as @",
         "Write a comment. Markdown works.", "Post as @", "Cancel", "Posts with your write token, as you.",
         "Comment posted as @"]


def test_every_string_of_section_7_is_there_and_within_the_copy_budget():
    copy = part("export const PARK = {", "\n}\n")
    strings = re.findall(r"'([^']*)'", copy)
    for text in EXACT:
        assert any(text in s for s in strings), text
    for s in strings:
        assert len(s.split()) <= 12 and s.count("·") <= 1, s
    assert "'Nothing waits for you.'" in SOURCE  # the all-parked empty state is the existing EMPTY.waiting


def test_every_input_in_the_park_dialog_passes_only_enter_and_it_guards_a_second_confirm():
    dialog = part("function ParkDialog(", "function BackLine(")
    assert dialog.count("el('input'") == 2 and dialog.count("onKeyDown: onlyEnter") == 2
    assert "const onlyEnter = event => {\n  if (event.key !== 'Enter') event.stopPropagation()\n}" in SOURCE
    assert "dismissOnConfirm: true" in dialog and "if (flight.current) return" in dialog
    assert "window.setTimeout(() => noteRef.current && noteRef.current.focus(), 0)" in dialog
    assert "canPost && post && !posted.current" in dialog and "posted.current = true" in dialog
    assert dialog.index("rest('/comment'") < dialog.index("await park(row")  # the comment first, then the park


def test_the_collapse_is_kept_per_profile_and_open_by_default():
    assert "const PARKED_OPEN = profile => 'forgebruecke:parked-open:' + (profile || 'default')" in SOURCE
    assert "window.localStorage.getItem(PARKED_OPEN(profile)) !== '0'" in SOURCE


def test_the_comment_icon_shows_only_when_his_token_can_comment():
    row = part("function Row(", "// ── the parking area")
    assert "canComment && you && row.tab === 'waiting'" in row and "el(Tip, { label: PARK.commentTip(you) }" in row
    assert "name: 'comment'" in row and "name: 'archive'" in row and "row.back ? PARK.again : PARK.park" in row


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_desk_moves_a_row_before_the_refetch():
    fns = "\n".join(re.search(rf"export function {name}\(.*?\n\}}", SOURCE, re.S).group(0).replace("export ", "")
                    for name in ("formatNavLabel", "seenOf", "withParked"))
    rows = [{"id": "a#1", "tab": "waiting", "section": "now"}, {"id": "a#2", "tab": "waiting", "section": "now"},
            {"id": "a#3", "tab": "running", "section": "now"}]
    data = {"rows": rows, "counts": {"waiting": 2, "parked": 0, "running": 1, "blocked": 0}, "label": "Forgejo · 2"}
    script = fns + f"""
const d = {json.dumps(data)}
const p = withParked(d, 'a#1', {{ note: 'n' }})
const u = withParked(p, 'a#1', null)
const r = withParked(d, 'a#3', {{ note: 'x' }})
process.stdout.write(JSON.stringify([p.counts, p.label, p.rows[0].section, u.counts, u.label, u.rows[0].parked, r === d,
  seenOf({{ head_sha: 'ab', wake_comment: {{ at: 't' }}, draft: false }}), seenOf({{}})]))"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [
        {"waiting": 1, "parked": 1, "running": 1, "blocked": 0}, "Forgejo · 1", "parked",
        {"waiting": 2, "parked": 0, "running": 1, "blocked": 0}, "Forgejo · 2", None, True,
        {"head_sha": "ab", "comment_at": "t", "draft": False}, {"head_sha": "", "comment_at": None, "draft": None}]
