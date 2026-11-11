"""0.0.22 (experiments/sidebar-rows/PLAN.md, D1 and D5): the sidebar row reads "Forgebrücke · N".

The Desktop half registers its row through the shared module (tests/test_sidebar_row.py checks the copy) and
gives it the count from the desk answer's `counts.waiting`, never from the answer's `label`. So a backend older
than 0.0.22, which still answers "Forgejo · 3", draws "Forgebrücke · 3". These tests load the real
`desktop/plugin.js` in node with stand-ins for its three imports, call `register(ctx)` on a fake ctx whose
registry replaces an entry by id as Desktop's does, and read the row it holds after the first `/desk` poll.
"""
import json
import re
import shutil
import subprocess

import pytest

from fb_fakes import ROOT, sub

SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

REACT = """
export class Component { constructor(props) { this.props = props } setState() {} }
export const useEffect = () => {}
export const useRef = v => ({ current: v })
export const useState = v => [v, () => {}]
"""
JSX = """
export const jsx = (type, props, key) => ({ type, props: props || {}, key })
export const jsxs = jsx
"""
# Every name plugin.js imports from the SDK: a working atom, the rest inert.
SDK_SPECIAL = {
    "atom": "v => { const ls = new Set(); return { get: () => v, set: n => { v = n; ls.forEach(l => l(v)) }, listen: l => (ls.add(l), () => ls.delete(l)) } }",
    "useValue": "a => a.get()",
    "host": "{ state: {}, navigate: p => { globalThis.__navigated = p }, notify: () => {}, request: async () => ({}) }",
    "SIDEBAR_NAV_AREA": "'sidebar.nav'", "ROUTES_AREA": "'routes'", "PALETTE_AREA": "'palette'",
}


def sdk_stub() -> str:
    names = re.search(r"^import \{(.*?)\} from '@hermes/plugin-sdk'", SOURCE, re.S | re.M).group(1)
    lines = []
    for name in (n.strip() for n in names.split(",") if n.strip()):
        value = SDK_SPECIAL.get(name, f"function {name}() {{ return null }}")
        lines.append(f"export const {name} = {value}" if name in SDK_SPECIAL else f"export {value}")
    return "\n".join(lines) + "\n"


def run(tmp_path, scenario: str):
    """Write the plugin and its stand-ins under tmp_path, import it, run `scenario` (an async JS body that sees
    `plugin`, `mod`, `boot(answers, stored)` and `tick()`), and return what it prints as JSON."""
    mods = tmp_path / "node_modules"
    for pkg, files in {"@hermes/plugin-sdk": {"index.js": sdk_stub()},
                       "react": {"index.js": REACT, "jsx-runtime.js": JSX}}.items():
        folder = mods / pkg
        folder.mkdir(parents=True)
        (folder / "package.json").write_text(json.dumps({"name": pkg, "type": "module", "main": "index.js",
                                                         "exports": {".": "./index.js", "./jsx-runtime": "./jsx-runtime.js"}
                                                         if pkg == "react" else {".": "./index.js"}}))
        for name, text in files.items():
            (folder / name).write_text(text)
    (tmp_path / "plugin.mjs").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "main.mjs").write_text("""
console.info = () => {}
const mod = await import('./plugin.mjs')
const plugin = mod.default
const tick = () => new Promise(r => setTimeout(r, 0))
// Desktop's registry: register replaces an entry with the same id; a disposer removes whatever holds its id now.
function boot(answers, stored) {
  const rows = [], pages = [], palette = [], every = [], store = new Map(stored === undefined ? [] : [['sidebar', stored]])
  const queue = [...answers]
  const ctx = {
    register(c) {
      const list = c.area === 'sidebar.nav' ? rows : c.area === 'palette' ? palette : []
      const i = list.findIndex(r => r.id === c.id)
      if (i >= 0) list.splice(i, 1)
      list.push(c)
      return () => { const j = list.findIndex(r => r.id === c.id); if (j >= 0) list.splice(j, 1) }
    },
    registerSettingsPage: p => { pages.push(p); return () => {} },
    setInterval: fn => { every.push(fn); return () => {} },
    storage: { get: (k, f) => (store.has(k) ? store.get(k) : f), set: (k, v) => store.set(k, v) },
    rest: async path => {
      if (path !== '/desk') throw new Error('unexpected ' + path)
      const next = queue.shift()
      if (next instanceof Error) throw next
      return next
    },
  }
  plugin.register(ctx)
  const view = () => rows.map(r => `${r.id}@${r.order}:${r.data.label}`).join(' ')
  // The first poll runs in register; each later one is the interval Desktop would fire.
  let polls = 0
  return { ctx, rows, pages, palette, store, view, poll: async () => { if (polls++) every[0](); await tick(); await tick() } }
}
const out = await (async () => {
%s
})()
process.stdout.write(JSON.stringify(out))
""" % scenario, encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "main.mjs")], capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


OLD_BACKEND = {"ok": True, "label": "Forgejo · 3", "counts": {"waiting": 3, "parked": 0, "running": 1, "blocked": 0}}


@needs_node
def test_an_older_backend_that_answers_forgejo_3_draws_forgebruecke_3(tmp_path):
    out = run(tmp_path, f"""
  const b = boot([{json.dumps(OLD_BACKEND)}])
  const first = b.view()
  await b.poll()
  const row = b.rows[0]
  return [first, b.view(), row.data.codicon, row.data.path]""")
    assert out == ["nav@55:Forgebrücke", "nav@55:Forgebrücke · 3", "repo-forked", "/forgebruecke"]


@needs_node
def test_the_label_follows_counts_waiting_on_every_poll_not_the_label_field(tmp_path):
    answers = [{"ok": True, "label": "Forgejo · 2", "counts": {"waiting": 150}},
               {"ok": True, "label": "Forgejo · 5", "counts": {"waiting": 0}},
               {"ok": True, "label": "Forgebrücke · 7", "counts": {"waiting": 7}},
               {"ok": False, "error": "forge_down", "message": "Forgejo did not answer."}]
    out = run(tmp_path, f"""
  const b = boot({json.dumps(answers)})
  const seen = []
  for (let i = 0; i < 4; i++) {{ await b.poll(); seen.push(b.view()) }}
  return seen""")
    assert out == ["nav@55:Forgebrücke · 99+", "nav@55:Forgebrücke", "nav@55:Forgebrücke · 7", "nav@55:Forgebrücke · 7"]


@needs_node
def test_a_refused_or_failed_desk_read_keeps_the_last_label(tmp_path):
    out = run(tmp_path, """
  const refused = boot([{ ok: false, error: 'not_configured', message: 'Set the address.' }])
  await refused.poll()
  const failed = boot([new Error('502: Bad Gateway')])
  await failed.poll()
  return [refused.view(), failed.view()]""")
    assert out == ["nav@55:Forgebrücke", "nav@55:Forgebrücke"]


@needs_node
def test_a_hidden_row_stays_hidden_when_the_count_arrives_and_the_palette_still_opens_the_page(tmp_path):
    out = run(tmp_path, f"""
  const b = boot([{json.dumps(OLD_BACKEND)}], {{ show: false, position: 12 }})
  await b.poll()
  const open = b.palette.find(p => p.id === 'open')
  open.data.run()
  return [b.view(), open.data.label, globalThis.__navigated, b.pages.map(p => [p.id, p.title])]""")
    assert out == ["", "Open Forgebrücke", "/forgebruecke", [["setup", "Forgebrücke"]]]


@needs_node
def test_a_stored_position_is_used_and_a_count_after_two_moves_leaves_one_row(tmp_path):
    out = run(tmp_path, f"""
  const b = boot([{json.dumps(OLD_BACKEND)}], {{ show: true, position: 12 }})
  const start = b.view()
  await b.poll()
  // The settings page holds the same row: move it twice, then a new count arrives.
  const page = b.pages[0].render()
  const section = (function find(node) {{
    if (!node || typeof node !== 'object') return null
    if (node.type && node.type.name === 'SidebarSection') return node
    const kids = node.props && node.props.children
    for (const kid of Array.isArray(kids) ? kids : [kids]) {{ const hit = find(kid); if (hit) return hit }}
    return null
  }})(page.type(page.props))
  section.props.row.setPrefs({{ position: 3 }})
  section.props.row.setPrefs({{ position: 140 }})
  section.props.row.setCount(4)
  return [start, b.view(), b.store.get('sidebar')]""")
    assert out == ["nav@12:Forgebrücke", "nav@99:Forgebrücke · 4", {"show": True, "position": 99}]


@needs_node
def test_the_settings_page_draws_the_sidebar_section_outside_the_backend_guard(tmp_path):
    out = run(tmp_path, """
  const b = boot([])
  const page = b.pages[0].render()
  const tree = page.type(page.props)
  const [body, sidebar] = tree.props.children
  const inner = sidebar.props.children.props.children
  return [tree.type, body.props.children.type.name, sidebar.type.name, inner.type.name, inner.props.row === undefined,
          Object.keys(inner.props.sdk).sort()]""")
    assert out == ["div", "SettingsBody", "Guard", "SidebarSection", False,
                   ["Input", "SIDEBAR_NAV_AREA", "Switch", "atom", "jsx", "jsxs", "useValue"]]


@needs_node
@pytest.mark.parametrize("desk,count", [
    ({"counts": {"waiting": 3}, "label": "Forgejo · 3"}, 3), ({"counts": {"waiting": 0}}, 0),
    ({"counts": {"waiting": "4"}}, 4), ({"counts": {}}, None), ({"ok": False}, None), (None, None),
    ({"counts": {"waiting": -1}}, None), ({"counts": {"waiting": "x"}}, None), ({"counts": {"waiting": None}}, None)])
def test_waiting_of_reads_counts_waiting_only(tmp_path, desk, count):
    assert run(tmp_path, f"return mod.waitingOf({json.dumps(desk)}) ?? null") == count


@needs_node
def test_the_desk_label_rule_and_the_rows_label_rule_agree(tmp_path):
    block = SOURCE[SOURCE.index("// sidebar-row v1 begin"):SOURCE.index("// sidebar-row v1 end")]
    fn = re.search(r"export function formatNavLabel\(waiting\) \{.*?\n\}", SOURCE, re.S).group(0).replace("export ", "")
    script = block + fn + """
const ns = [0, 1, 2, 3, 9, 10, 42, 98, 99, 100, 101, 150, 999]
process.stdout.write(JSON.stringify(ns.map(n => [n, formatNavLabel(n), sidebarRowLabel('Forgebrücke', n)])))"""
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    rows = json.loads(done.stdout)
    assert all(a == b for _n, a, b in rows), rows
    nav_label = sub("desk").nav_label
    assert all(nav_label(n) == a for n, a, _b in rows)


def test_the_old_label_plumbing_is_gone_and_the_chat_tab_says_forgebruecke():
    for gone in ("setNavLabel", "navLabel", "order: 56", "desk.label", "data.label", "next.label"):
        assert gone not in SOURCE, gone
    assert "const CHAT_TITLE = 'Forgebrücke'" in SOURCE  # 'Forgejo' stays where it names Forgejo itself
    assert SOURCE.count("Forgejo ·") == 1 and "still answers \"Forgejo · 3\"" in SOURCE  # only the comment on old backends
    assert "startSidebarRow(ctx, { brand: 'Forgebrücke', path: PATH, codicon: 'repo-forked', defaultPosition: 55" in SOURCE
