"""M7: the Desktop half's contract, checked without a browser (the render itself is proven by
experiments/forgebruecke/m7/harness, the Mac by K-F5)."""
import json
import re
import shutil
import subprocess

import pytest

from fb_fakes import ROOT

JS = ROOT / "desktop" / "plugin.js"
SOURCE = JS.read_text(encoding="utf-8")
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_desktop_half_parses_as_an_es_module():
    out = subprocess.run([NODE, "--check", str(JS)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


def test_imports_only_the_sdk_and_react_and_never_calls_the_forge():
    imports = set(re.findall(r"^import [^;]*? from '([^']+)'", SOURCE, re.M | re.S))
    assert imports == {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    for banned in (" fetch(", "XMLHttpRequest", "eval(", "new Function", "/api/v1", "FORGEBRUECKE_BOT_ACCOUNT_TOKEN'"):
        assert banned not in SOURCE, banned
    assert not re.search(r"(?<![.\w])fetch\(", SOURCE)
    assert "rest('/desk')" in SOURCE and "refetchInterval" in SOURCE  # polled through ctx.rest + useQuery


def test_a_profile_without_forgebruecke_gets_a_sentence_not_a_bare_404():
    """Seen on the Mac (2026-11-09): the desk in a profile with the plugin off showed "HTTP 404, Plugin not found"."""
    assert "function notHere(error)" in SOURCE and "/plugin not found/i" in SOURCE
    assert "'Forgebrücke is not on in this profile'" in SOURCE
    assert SOURCE.index("desk.isError && notHere(desk.error)") < SOURCE.index("'The desk could not load'")


def test_registers_the_row_route_palette_and_settings_page():
    assert "const PATH = '/forgebruecke'" in SOURCE
    assert "codicon: 'repo-forked'" in SOURCE and "SIDEBAR_NAV_AREA" in SOURCE
    assert "registerSettingsPage" in SOURCE and "PALETTE_AREA" in SOURCE
    assert "ctx.setInterval" in SOURCE  # the count poll dies with the plugin


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("n,label", [(0, "Forgejo"), (3, "Forgejo · 3"), (99, "Forgejo · 99"), (100, "Forgejo · 99+")])
def test_the_pane_label_rule_matches_the_backend(n, label):
    fn = re.search(r"export function formatNavLabel\(waiting\) \{.*?\n\}", SOURCE, re.S).group(0)
    script = fn.replace("export ", "") + f"\nprocess.stdout.write(JSON.stringify(formatNavLabel({n})))"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert json.loads(out.stdout) == label
    from fb_fakes import sub
    assert sub("desk").nav_label(n) == label


def test_the_merge_button_is_drawn_only_from_the_backend_flag():
    assert "row.can_merge ? el(Button" in SOURCE
    assert "rest('/merge', { method: 'POST', body: { repo: row.repo, number: row.number, head_sha: row.head_sha } })" in SOURCE


def test_review_requested_rows_show_their_reason_and_no_gate_chain():
    """0.0.14 (D28): a Waiting row without a flow says why it waits instead of 'flow null'."""
    row = SOURCE[SOURCE.index("function Row("):SOURCE.index("const EMPTY")]
    assert "row.flow ? ' · flow ' + row.flow : ' · ' + (row.reason || 'no flow')" in row
    assert "row.gates && row.gates.length ? el(GateChain" in row


def test_the_waiting_note_and_the_repos_tab_follow_the_backend_shapes():
    assert "el(RowList, { note: data.waiting_note" in SOURCE
    note = SOURCE[SOURCE.index("function WaitingNote("):SOURCE.index("function Notes(")]
    assert "'Open the settings'" in note and "note.suggestion" in note
    repos = SOURCE[SOURCE.index("function Repos("):SOURCE.index("function MergeDialog(")]
    assert "r.collaboration_url" in repos and "RIGHT[r.right]" in repos and "r.mentions" in repos and "r.flows" in repos
    assert "r.write" not in repos  # the retired rule flag


def test_the_settings_page_has_no_read_token_and_asks_for_you():
    setup = SOURCE[SOURCE.index("function Setup("):SOURCE.index("function Features(")]
    assert "'read'" not in setup and "read-only token" not in SOURCE.lower()
    assert "s.you_suggestion" in setup and "'Your Forgejo login'" in setup


def test_the_settings_page_opens_with_how_it_works_then_the_setup_steps():
    body = SOURCE[SOURCE.index("function SettingsBody()"):SOURCE.index("function SettingsPage()")]
    assert body.index("el(HowItWorks") < body.index("el(Setup") < body.index("el(Features") < body.index("'Agent reviews'")


def test_the_pane_never_takes_a_token_itself():
    # Only Hermes' own form (Agent settings) writes a secret to .env; every setup step links there.
    sdk_imports = re.search(r"import \{([^}]*)\} from '@hermes/plugin-sdk'", SOURCE).group(1)
    assert "Input" not in sdk_imports and "'password'" not in SOURCE and "<input" not in SOURCE
    assert "const openFields = () => host.navigate(SETTINGS_HREF)" in SOURCE
    assert "const SETTINGS_HREF = '/settings?tab=plugins&agent=forgebruecke'" in SOURCE
