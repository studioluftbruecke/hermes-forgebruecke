"""The Desktop half's contract, checked without a browser (the render itself is proven by the off-device
harness under experiments/, the real components by the check on the Mac)."""
import json
import re
import shutil
import subprocess

import pytest

from fb_fakes import ROOT, sub

JS = ROOT / "desktop" / "plugin.js"
SOURCE = JS.read_text(encoding="utf-8")
NODE = shutil.which("node")


def part(start, end):
    return SOURCE[SOURCE.index(start):SOURCE.index(end)]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_desktop_half_parses_as_an_es_module():
    out = subprocess.run([NODE, "--check", str(JS)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


def test_imports_only_the_sdk_and_react_and_never_calls_the_forge():
    imports = set(re.findall(r"^import [^;]*? from '([^']+)'", SOURCE, re.M | re.S))
    assert imports == {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    for banned in (" fetch(", "XMLHttpRequest", "eval(", "new Function", "/api/v1", "FORGEBRUECKE_BOT_ACCOUNT_TOKEN'",
                   "createElement('script')", "innerHTML"):
        assert banned not in SOURCE, banned
    assert not re.search(r"(?<![.\w])fetch\(", SOURCE)
    assert "rest('/desk')" in SOURCE and "refetchInterval" in SOURCE  # polled through ctx.rest + useQuery


def test_a_profile_without_forgebruecke_gets_a_sentence_not_a_bare_404():
    assert "function notHere(error)" in SOURCE and "/plugin not found/i" in SOURCE
    assert "'Forgebrücke is not on in this profile'" in SOURCE
    assert SOURCE.index("desk.isError && notHere(desk.error)") < SOURCE.index("'The desk could not load'")


def test_registers_the_row_route_palette_and_settings_page():
    assert "const PATH = '/forgebruecke'" in SOURCE
    assert "codicon: 'repo-forked'" in SOURCE and "SIDEBAR_NAV_AREA" in SOURCE and "startSidebarRow(ctx, {" in SOURCE
    assert "registerSettingsPage" in SOURCE and "PALETTE_AREA" in SOURCE
    assert "ctx.setInterval" in SOURCE  # the count poll dies with the plugin


def test_hover_and_focus_styles_live_in_one_sheet_that_leaves_with_the_plugin():
    add = part("function addSheet(ctx)", "export default")
    assert "document.createElement('style')" in add and "ctx.onDispose(() => node.remove())" in add
    assert set(re.findall(r"className: '(fb-[a-z]+)", SOURCE)) <= set(re.findall(r"'\.(fb-[a-z]+)", SOURCE))


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("n,label", [(0, "Forgebrücke"), (3, "Forgebrücke · 3"), (99, "Forgebrücke · 99"), (100, "Forgebrücke · 99+")])
def test_the_pane_label_rule_matches_the_backend(n, label):
    fn = re.search(r"export function formatNavLabel\(waiting\) \{.*?\n\}", SOURCE, re.S).group(0)
    script = fn.replace("export ", "") + f"\nprocess.stdout.write(JSON.stringify(formatNavLabel({n})))"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert json.loads(out.stdout) == label
    assert sub("desk").nav_label(n) == label


def test_the_merge_button_is_drawn_only_from_the_backend_flag():
    assert "row.next === 'merge' ? el(Button" in SOURCE and "row.can_merge && row.next !== 'merge'" in SOURCE
    assert "rest('/merge', { method: 'POST', body: { repo: row.repo, number: row.number, head_sha: row.head_sha } })" in SOURCE


def test_a_row_shows_the_backends_sentence_and_one_short_link_label():
    row = part("function Row(", "const EMPTY")
    assert "row.summary.text" in row and "row.review_summary" in row and "comment.age_s" in row
    assert "row.gates && row.gates.length ? el(GateChain" in row
    assert "head_sha.slice(0, 12)" in row and "' · flow '" not in row  # the commit sits in the tooltip
    labels = part("const MOVE = {", "function Row(")
    assert all(text in labels for text in ("'Open in Forgejo'", "'Merge in Forgejo'", "'Review in Forgejo'"))
    assert "to merge'" not in SOURCE and "to review'" not in SOURCE


def test_passed_checks_fold_and_other_checks_stay_listed():
    checks = part("function Checks(", "const MOVE = {")
    assert "s.state !== 'success'" in checks and "' checks passed'" in checks and "DisclosureCaret" in checks


def test_ask_hermes_drafts_a_reference_and_never_sends():
    ask = part("const QUESTIONS", "const SEGMENT")
    for sentence in ("Summarise this pull request.", "What should I check before I merge this?",
                     "Why is this blocked, and what fixes it?", "Fix what the review asked for."):
        assert f"'{sentence}'" in ask
    assert "'@forgejo:' + row.repo + '#' + row.number + ' ' + sentence" in ask
    chat = part("async function openChat(text)", "const QUESTIONS")
    assert "tabTitle: CHAT_TITLE" in chat and "storage.get(storageKey, null)" in chat and "host.newChat" in chat
    assert "const CHAT_TITLE = 'Forgebrücke'" in SOURCE  # D5 of the sidebar-rows plan (0.0.22)
    assert ".submit(" not in SOURCE and "session.send" not in SOURCE and "prompt.submit" not in SOURCE


def test_the_you_note_offers_the_login_in_one_click_and_the_repos_tab_follows_the_backend():
    assert "el(RowList, { blocked: counts.blocked > 0, canComment: Boolean(data.can_comment), canMerge: Boolean(data.can_merge), note: data.waiting_note" in SOURCE
    who = part("function WhoAreYou(", "function WaitingNote(")
    assert "save('you', login)" in who and "'Use @' + login" in who and "'Open settings'" in who
    repos = part("function Repos(", "function MergeDialog(")
    for field in ("r.collaboration_url", "r.open_prs > 0", "r.mentions", "r.flows", "r.right === 'read'"):
        assert field in repos, field
    assert "SearchField" in repos and "repos.length > FILTER_ABOVE" in repos and "const FILTER_ABOVE = 8" in SOURCE


def test_empty_states_are_one_sentence_and_point_only_at_work():
    assert "waiting: 'Nothing waits for you.'" in SOURCE
    rows = part("function RowList(", "function Repos(")
    assert "tab === 'waiting' && blocked" in rows and "'Show blocked'" in rows
    assert "'Show running'" not in SOURCE and "'Show repos'" not in SOURCE


def test_the_settings_page_reads_next_action_setup_switches_what_works_how_it_works():
    body = part("function SettingsBody()", "function SettingsPage()")
    order = [body.index(x) for x in ("el(Next,", "el(Setup,", "el(Switches,", "el(Features,", "el(HowItWorks,",
                                     "el(AgentReviews,")]
    assert order == sorted(order)
    assert "startOpen: s.state === 'setup'" in body and "flows.status === 'available' ? el(AgentReviews" in body
    assert "BADGE[s.state]" in body
    assert "setup: ['warn', 'setup needed'], preview: ['outline', 'preview only'], ready: ['success', 'ready']" in SOURCE
    titles = part("const ROW_TITLE", "function SetupRow(")
    for title in ("'Forgejo address'", "'Hermes’ account token'", "'Your Forgejo login'", "'Your write token (optional)'"):
        assert title in titles, title
    setup = part("function Setup(", "function Switches(")
    assert "s.you_suggestion" in setup and "'read'" not in setup and "read-only token" not in SOURCE.lower()


def test_the_settings_page_draws_the_backends_verdict_and_decides_nothing():
    setup = part("function Setup(", "function Switches(")
    assert "const rows = s.setup || []" in setup and "r.problem" in setup and "kind: r.button" in setup
    assert ".error" not in setup and "tokenState" not in SOURCE and "NEXT_BUTTON" not in SOURCE
    nxt = part("function Next(", "const ROW_TITLE")
    assert "kind: action.button" in nxt and "action.text" in nxt
    act = part("function Act(", "function Next(")
    assert sorted(re.findall(r"kind === '(\w+)'", act)) == sorted(
        {button for _badge, button, _row in sub("desk").ACTIONS.values() if button})
    assert set(re.findall(r"(\w+): \['\w+', '[^']+'\]", part("const BADGE", "const STEP_ICON"))) == {
        badge for badge, _button, _row in sub("desk").ACTIONS.values()}
    assert set(re.findall(r"(\w+): \['[\w-]+', T\.\w+\]", part("const STEP_ICON", "const WRITE_CONFIRM"))) == {
        "done", "todo", "problem", "unchecked", "optional"}


def test_switches_save_through_the_setting_route_and_writing_asks_once():
    save = part("function useSaveSetting()", "const MARK")
    assert "rest('/setting', { method: 'POST', body: { key, value } })" in save
    assert "client.setQueryData(settingsKey, withSetting(before, key, value))" in save  # shown at once
    assert "client.setQueryData(settingsKey, before)" in save and "'Not saved: '" in save  # and taken back
    switches = part("function Switches(", "function Features(")
    assert "value ? onWrite() : save('dry_run', true)" in switches and "ToggleRow" in switches
    assert {key for key in re.findall(r"\['(\w+)', '[^']+', '[^']+'\]", part("const SWITCHES", "const RULES"))} == {
        "review_every_pr", "learn_mode", "audit_enabled"}
    assert ("const WRITE_CONFIRM = 'Hermes will send comments, pull requests and checks. Each follows Hermes’ approval "
            "setting; flows and review requests run on their own.'") in SOURCE
    assert "onConfirm: () => save('dry_run', false, true)" in SOURCE
    assert set(re.findall(r"save\('(\w+)'", SOURCE)) | {"review_every_pr", "learn_mode", "audit_enabled"} == set(
        sub("desk").SETTABLE)


def test_the_pane_never_takes_a_token_itself():
    # Only Hermes' own form (All settings) writes a secret to .env; every token step links there.
    # Input arrives in 0.0.22 for the Sidebar section's Position field only: outside the shared module's block it
    # is named in the import and handed to the module, nowhere else.
    block = SOURCE[SOURCE.index("// sidebar-row v1 begin"):SOURCE.index("// sidebar-row v1 end")]
    outside = SOURCE.replace(block, "")
    assert len(re.findall(r"\bInput\b", outside)) == 2 and "SIDEBAR_SDK = { SIDEBAR_NAV_AREA, Switch, Input," in outside
    assert "type: 'number'" in block and "'password'" not in SOURCE and "<input" not in SOURCE
    assert "const openFields = () => host.navigate(SETTINGS_HREF)" in SOURCE
    assert "const SETTINGS_HREF = '/settings?tab=plugins&agent=forgebruecke'" in SOURCE


def test_text_is_short_and_carries_no_long_dash_or_plan_id():
    assert "\u2014" not in SOURCE and "\u2013" not in SOURCE
    assert not re.search(r"\b(D\d\d|K-F\d|M\d+b?|N\d\d)\b(?!\d* \d)", re.sub(r"'M32[^']*'", "", SOURCE))
    literals = [s for s in re.findall(r"'((?:[^'\\\n]|\\.)*)'", SOURCE) if " " in s and not s.startswith((".", "M32"))]
    assert literals and max(len(s) for s in literals) <= 140
    for _key, label, description in re.findall(r"\['(\w+)', '([^']+)', '([^']+)'\]", part("const SWITCHES", "const RULES")):
        assert len(description.split()) <= 10, label
    assert len("When off, Hermes only previews each write.".split()) <= 10 and "'When off, Hermes only previews each write.'" in SOURCE
    labels = re.findall(r"\['([\w-]+)', '([^']+)', \[", part("const RULES", "function Scopes("))
    assert len(labels) == 4
    for _icon, label in labels:
        assert len(label.split()) <= 6, label


def test_every_button_that_opens_hermes_form_has_one_label():
    assert set(re.findall(r"onClick: openFields[^)]*?\}, (?:el\(Codicon, \{[^}]*\}\), )?('[^']+'|[^)]+)\)", SOURCE)) == {
        "'Open settings'"}
    for label in ("Change", "All settings", "Someone else", "Enter address", "Add token", "Replace token"):
        assert f"'{label}'" not in SOURCE, label


def test_a_setup_row_and_its_form_field_start_with_the_same_words():
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    labels = dict(re.findall(r'^  (\w+):\n(?:    (?:type|env|default): .*\n)+    label: "([^"]*)"', manifest, re.M))
    titles = dict(re.findall(r"(\w+): '([^']+)'", part("const ROW_TITLE", "function SetupRow(")))
    for row, key in (("url", "url"), ("bot", "token"), ("you", "you"), ("owner", "owner_token")):
        lead = titles[row].replace("’", "'").replace(" (optional)", "")
        assert labels[key].startswith(lead), (titles[row], labels[key])
    assert labels["owner_token"].endswith("(optional)")
