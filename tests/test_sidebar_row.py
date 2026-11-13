"""The plugin's sidebar row comes from the shared module, under its brand, at its place.

The module is `templates/plugin-skeleton/desktop/sidebar-row.js`. A plugin with an esbuild build
carries it as `desktop/src/sidebar-row.js`; a single-file plugin pastes the code between the markers
`// sidebar-row v1 begin` and `// sidebar-row v1 end` into `desktop/plugin.js`. This test checks:

- the copy between the markers equals the template's (its SHA-256 is pinned below);
- the one `startSidebarRow(ctx, {...})` call names the brand from `dashboard/manifest.json` `label`
  and the plugin's default position from POSITIONS;
- no other sidebar row is registered outside the marked block (in `desktop/src` for a built
  plugin, never the bundle);
- the module's behaviour, in node, against a fake `ctx` whose `register` replaces an entry by id
  and whose disposers remove by id, as Desktop's registry does: stored choices at start, show and
  hide, the position clamp, a count while hidden, `setPrefs` twice then a count change leaves
  exactly one row, dispose; and what the settings block renders.

This file is identical in every plugin folder that has a sidebar row; change it only in
`templates/plugin-skeleton/tests/` (with the new hash after a template change), then copy it to
every plugin. `scripts/check.sh` fails a copy that differs from the template's.

Run from the plugin folder: python3 -m pytest tests -q --import-mode=importlib
"""

import hashlib
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BEGIN = "// sidebar-row v1 begin"
END = "// sidebar-row v1 end"
BLOCK_SHA256 = "f24b11df18931e3044f90aa25fe46f166eba1d76e7e066af9d311a91179fb6d1"

# Every plugin row sits below Kanban (50), one block from 51.
POSITIONS = {
    "eselsbruecke": 51,
    "shaderbruecke": 52,
    "unblockbruecke": 53,
    "coolifybruecke": 54,
    "forgebruecke": 55,
    "wpbruecke": 56,
}

TEMPLATE = (ROOT / "desktop" / "sidebar-row.js").is_file()
BUILT = (ROOT / "desktop" / "src").is_dir()
REGISTRATION = re.compile(r"area\s*:\s*(?:[\w$]+\.)?SIDEBAR_NAV_AREA\b|['\"]sidebar\.nav['\"]")


def copy_path() -> pathlib.Path:
    if TEMPLATE:
        return ROOT / "desktop" / "sidebar-row.js"
    if BUILT:
        return ROOT / "desktop" / "src" / "sidebar-row.js"
    return ROOT / "desktop" / "plugin.js"


def block_of(text: str) -> str:
    assert text.count(BEGIN) == 1 and text.count(END) == 1, (
        f"expected one '{BEGIN}' and one '{END}' line, found {text.count(BEGIN)} and {text.count(END)}")
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    assert start < end
    return text[start:end]


def plugin_name() -> str:
    found = re.search(r"(?m)^name:\s*['\"]?([\w.-]+)", (ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert found, "plugin.yaml has no name"
    return found.group(1)


def sources() -> list[tuple[pathlib.Path, str]]:
    """Desktop source outside the marked block: desktop/src/**/*.js for a built plugin (the module's
    own file left out), desktop/plugin.js with its block cut out for a single-file plugin."""
    if BUILT:
        files = sorted(p for p in (ROOT / "desktop" / "src").rglob("*.js") if p.name != "sidebar-row.js")
        return [(p, p.read_text(encoding="utf-8")) for p in files]
    path = ROOT / "desktop" / "plugin.js"
    text = path.read_text(encoding="utf-8")
    return [(path, text.replace(block_of(text), ""))]


def test_copy_equals_the_template():
    path = copy_path()
    assert path.is_file(), f"no sidebar-row copy at {path.relative_to(ROOT)}"
    block = block_of(path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    assert digest == BLOCK_SHA256, (
        f"{path.relative_to(ROOT)}: the code between the sidebar-row markers differs from "
        "templates/plugin-skeleton/desktop/sidebar-row.js; copy the template's block unchanged")


def test_the_module_has_no_imports():
    block = block_of(copy_path().read_text(encoding="utf-8"))
    assert not re.search(r"(?m)^\s*(import|export)\b", block), "the marked block must hold no import or export"


@pytest.mark.skipif(TEMPLATE, reason="the template has no plugin of its own")
def test_one_call_with_the_brand_and_the_position():
    calls = [(p, m) for p, text in sources() for m in re.finditer(r"\bstartSidebarRow\(", text)
             if not text[max(0, m.start() - 9):m.start()].endswith("function ")]
    assert len(calls) == 1, f"expected one startSidebarRow call, found {len(calls)}"
    path, match = calls[0]
    text = dict(sources())[path]
    window = text[match.end():match.end() + 600]
    brand = re.search(r"\bbrand\s*:\s*'([^']+)'", window)
    position = re.search(r"\bdefaultPosition\s*:\s*(\d+)", window)
    assert brand and position, "the call names brand and defaultPosition as literals"
    label = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))["label"]
    assert brand.group(1) == label, f"brand {brand.group(1)!r} is not the dashboard label {label!r}"
    name = plugin_name()
    assert int(position.group(1)) == POSITIONS.get(name), (
        f"{name} sits at {POSITIONS.get(name)}, the call says {position.group(1)}")


@pytest.mark.skipif(TEMPLATE, reason="the template has no plugin of its own")
def test_no_other_sidebar_row():
    found = [f"{p.relative_to(ROOT)}: {m.group(0)}" for p, text in sources() for m in REGISTRATION.finditer(text)]
    assert not found, "sidebar rows registered outside the shared module: " + "; ".join(found)


HARNESS = r"""
const atom = v => {
  const ls = new Set()
  return { get: () => v, set: n => { v = n; ls.forEach(l => l(v)) }, listen: l => (ls.add(l), () => ls.delete(l)) }
}
// Desktop's registry: register replaces an entry with the same id and appends it; a disposer
// removes whatever entry holds its id now.
function fakeCtx(stored) {
  const rows = []
  const store = new Map(stored === undefined ? [] : [['sidebar', JSON.stringify(stored)]])
  return {
    rows,
    stored: () => (store.has('sidebar') ? JSON.parse(store.get('sidebar')) : undefined),
    storage: { get: (k, f) => (store.has(k) ? JSON.parse(store.get(k)) : f), set: (k, v) => store.set(k, JSON.stringify(v)) },
    register(c) {
      const i = rows.findIndex(r => r.id === c.id)
      if (i >= 0) rows.splice(i, 1)
      rows.push(c)
      return () => { const j = rows.findIndex(r => r.id === c.id); if (j >= 0) rows.splice(j, 1) }
    },
  }
}
const sdk = { SIDEBAR_NAV_AREA: 'sidebar.nav', atom, Switch: 'Switch', Input: 'Input' }
const opts = { brand: 'Brücke', path: '/bruecke', codicon: 'plug', defaultPosition: 55, sdk }
const view = rows => rows.map(r => `${r.id}@${r.order}:${r.data.label}`).join(' ')
const out = {}
let ctx, row

ctx = fakeCtx(); row = startSidebarRow(ctx, opts)
out.default_shown = view(ctx.rows) === 'nav@55:Brücke' && ctx.rows[0].area === 'sidebar.nav' && ctx.rows[0].data.path === '/bruecke' && ctx.stored() === undefined

ctx = fakeCtx({ show: true, position: 12 }); startSidebarRow(ctx, opts)
out.stored_position = view(ctx.rows) === 'nav@12:Brücke'
ctx = fakeCtx({ show: false, position: 12 }); row = startSidebarRow(ctx, opts)
out.stored_hidden = ctx.rows.length === 0 && row.prefs().show === false
ctx = fakeCtx('junk'); startSidebarRow(ctx, opts)
out.stored_junk = view(ctx.rows) === 'nav@55:Brücke'

ctx = fakeCtx(); row = startSidebarRow(ctx, opts)
row.setPrefs({ show: false })
const hidden = ctx.rows.length === 0 && ctx.stored().show === false
row.setPrefs({ show: true })
out.hide_show = hidden && view(ctx.rows) === 'nav@55:Brücke' && ctx.stored().show === true

ctx = fakeCtx(); row = startSidebarRow(ctx, opts)
const clamps = [[0, 1], [-5, 1], [100, 99], [12.6, 13], ['40', 40], ['x', 40], [null, 40]].map(([v, want]) => row.setPrefs({ position: v }).position === want)
out.position_clamp = clamps.every(Boolean) && view(ctx.rows) === 'nav@40:Brücke' && ctx.stored().position === 40

ctx = fakeCtx(); row = startSidebarRow(ctx, opts)
row.setCount(3)
const counted = view(ctx.rows) === 'nav@55:Brücke · 3'
row.setPrefs({ show: false }); row.setCount(7)
const stillHidden = ctx.rows.length === 0
row.setPrefs({ show: true })
row.setCount(250)
const capped = view(ctx.rows) === 'nav@55:Brücke · 99+'
row.setCount(0)
out.count_while_hidden = counted && stillHidden && capped && view(ctx.rows) === 'nav@55:Brücke'

ctx = fakeCtx(); row = startSidebarRow(ctx, opts)
row.setPrefs({ position: 10 }); row.setPrefs({ position: 20 }); row.setCount(2)
out.one_row_after_moves = view(ctx.rows) === 'nav@20:Brücke · 2'

ctx = fakeCtx(); row = startSidebarRow(ctx, opts)
let registers = 0
const register = ctx.register
ctx.register = c => { registers += 1; return register(c) }
row.setPrefs({ position: 55 }); row.setPrefs({ show: true }); row.setCount(0)
out.no_churn = registers === 0

ctx = fakeCtx(); row = startSidebarRow(ctx, opts)
row.dispose(); row.setCount(4); row.setPrefs({ position: 30 })
out.dispose = ctx.rows.length === 0

// The settings block: a tree from a fake jsx, the switch and the field wired to setPrefs.
const tree = (type, props, key) => ({ type, props, key })
const ui = { ...sdk, jsx: tree, jsxs: tree, useValue: a => a.get() }
const texts = n => (n == null || typeof n !== 'object' ? (typeof n === 'string' ? [n] : []) : Array.isArray(n) ? n.flatMap(texts) : texts(n.props && n.props.children))
const find = (n, type) => (n == null || typeof n !== 'object' ? [] : Array.isArray(n) ? n.flatMap(c => find(c, type)) : [...(n.type === type ? [n] : []), ...find(n.props && n.props.children, type)])
ctx = fakeCtx(); row = startSidebarRow(ctx, opts)
let section = SidebarSection({ row, sdk: ui })
const shownTexts = texts(section)
find(section, 'Switch')[0].props.onCheckedChange(false)
section = SidebarSection({ row, sdk: ui })
const field = find(section, 'Input')[0]
const target = { value: '7' }
field.props.onKeyDown({ key: 'Enter', target })
out.section = shownTexts.join('|') === 'Sidebar|Show in the sidebar|Position|Lower numbers sit higher. Kanban is 50.'
  && texts(section).includes('Open it from the command palette: Open Brücke.')
  && ctx.rows.length === 0 && row.prefs().position === 7 && target.value === '7'
  && field.props.min === 1 && field.props.max === 99

process.stdout.write(JSON.stringify(out))
"""

CASES = ["default_shown", "stored_position", "stored_hidden", "stored_junk", "hide_show", "position_clamp",
         "count_while_hidden", "one_row_after_moves", "no_churn", "dispose", "section"]


@pytest.fixture(scope="module")
def node_results(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    script = tmp_path_factory.mktemp("sidebar-row") / "run.mjs"
    script.write_text(block_of(copy_path().read_text(encoding="utf-8")) + "\n" + HARNESS, encoding="utf-8")
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("case", CASES)
def test_behaviour(node_results, case):
    assert node_results.get(case) is True, f"{case}: {node_results}"
