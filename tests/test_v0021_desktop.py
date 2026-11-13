"""The Desktop half. Full width, `#n` titles, the name and the mark, the
rule copy always visible, the version note, why a row has no Merge, and the settings page lines for a stopped
background, an unprotected main and a token not picked up yet."""
import json
import re
import shutil
import subprocess

import pytest

from fb_fakes import ROOT

SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def part(start, end):
    return SOURCE[SOURCE.index(start):SOURCE.index(end)]


def test_the_desktop_half_carries_the_version_of_the_manifest():
    version = re.search(r'^version: "([^"]+)"$', (ROOT / "plugin.yaml").read_text(encoding="utf-8"), re.M).group(1)
    assert f"export const VERSION = '{version}'" in SOURCE
    note = part("function VersionNote(", "function openExternal(")
    assert "version === VERSION" in note and "'Desktop shows ' + VERSION + ', Hermes runs ' + version" in note
    assert "'hermes plugins update ' + ID" in note
    assert SOURCE.count("el(VersionNote, { version: data.version })") == 1
    assert SOURCE.count("el(VersionNote, { version: s.version })") == 1


def test_both_pages_use_the_full_width():
    assert "maxWidth: 900" not in SOURCE and "maxWidth: 720" not in SOURCE


def test_titles_start_with_the_number_and_the_meta_line_does_not_repeat_it():
    row = part("function Row(", "const EMPTY")
    assert "'#' + row.number + ' ' + row.title" in row
    assert "row.repo + (row.author ? ' by ' + row.author : '')" in row and "' #' + row.number" not in row


def test_the_page_is_called_forgebruecke_and_the_palette_says_so():
    assert "'Forgejo desk'" not in SOURCE and "'Open Forgejo desk'" not in SOURCE
    assert "label: 'Open Forgebrücke'" in SOURCE and "'desk'" in part("label: 'Open Forgebrücke'", "run: () =>")
    header = part("function Header(", "function Loading(")
    assert "'Forgebrücke'" in header and "el(Mark, { size: 22 })" in header


def test_the_mark_is_h1_1_with_its_wings_in_one_colour():
    mark = part("function Mark(", "function VersionNote(")
    assert "translate(39.5 13.5) scale(0.68 0.68) rotate(-26)" in mark
    assert "translate(24.5 13.5) scale(-0.68 0.68) rotate(-26)" in mark
    assert "currentColor" in mark and "#4100F5" not in SOURCE  # one colour
    asset = (ROOT.parents[1] / "experiments" / "forgebruecke" / "brand" / "assets" / "forgebruecke-mark-mono.svg")
    if asset.exists():
        for d in re.findall(r'\sd="([^"]+)"', asset.read_text(encoding="utf-8")):
            assert all(piece in SOURCE for piece in d.split(" M")), d


def test_his_rule_copy_is_on_the_page_word_for_word_and_always_visible():
    rules = part("const RULES", "function Scopes(")
    joined = {label: " ".join(re.findall(r"'([^']+)'", body))
              for label, body in re.findall(r"\['[\w-]+', '([^']+)', \[(.*?)\]\]", rules, re.S)}
    assert joined["Forgejo decides what Hermes may do"] == (
        "Per repo you give Hermes’ account Read or Write in Forgejo. That right is the only permission layer. Warning "
        "though: If you set the personal write token below, it is also exposed to Hermes, ultimately giving Hermes "
        "full access to your account.")
    assert joined["You approve the important part"] == (
        "Hermes works on its own feature branches. Nothing gets to main unless you approve. For additional security, "
        "set up a branch protection for main and whitelist only yourself for push and merge.")
    assert "You approve every write" not in SOURCE and "Forgejo decides its access" not in SOURCE
    how = part("function HowItWorks(", "function AgentReviews(")
    assert "el(Tip" not in how and "long.join(' ')" in how  # the text sits under the headline, no hover tip


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_row_without_merge_says_why():
    fn = re.search(r"export function noMergeReason\(.*?\n\}", SOURCE, re.S).group(0).replace("export ", "")
    cases = [({"tab": "waiting", "draft": True}, True), ({"tab": "waiting", "mergeable": False}, True),
             ({"tab": "waiting"}, True), ({"tab": "waiting", "draft": True}, False),
             ({"tab": "waiting", "can_merge": True}, True), ({"tab": "blocked", "draft": True}, True)]
    script = fn + "\nprocess.stdout.write(JSON.stringify(" + json.dumps(cases) + ".map(([r, c]) => noMergeReason(r, c))))"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["A draft. Forgejo merges it once it is marked ready.", "Forgejo cannot merge it yet.",
                                      None, None, None, None]
    assert "link && why ? el(Tip, { label: why }, link) : link" in part("function RowActions(", "const EMPTY")


def test_the_settings_page_shows_a_stopped_background_an_unprotected_main_and_a_token_to_reload():
    assert "el(Background, { b: background })" in part("function Features(", "function Node(")
    assert "el(Features, { background: s.background, features: s.features || [] })" in SOURCE
    unprotected = part("function Unprotected(", "function HowItWorks(")
    assert "u.branch + ' is not protected in '" in unprotected and "u.repo + '.'" in unprotected
    assert "'Branch settings'" in unprotected
    assert "icon === 'pass' ? el(Unprotected, { list: s.unprotected || [] })" in SOURCE
    assert "startOpen: s.state === 'setup' || Boolean(s.unprotected && s.unprotected.length)" in SOURCE
    reload = part("function Reload(", "function SettingsBody(")
    assert "rest('/reload-tokens', { method: 'POST', body: {} })" in reload and "'Load it now'" in reload
    assert "if (!reload || !reload.needed) return null" in reload
