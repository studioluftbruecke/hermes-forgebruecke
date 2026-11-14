// Forgebrücke: the Desktop half. A sidebar row "Forgebrücke · N" (N = pull requests waiting for you), a desk
// page (/forgebruecke) with four tabs (Waiting for me, Running, Blocked, Repos), a palette command and a
// settings page: one next action, four setup rows, four switches and what the setup can do.
//
// Everything the pages show comes from this plugin's own backend routes (dashboard/plugin_api.py) through
// ctx.rest, polled with useQuery; the pages never call Forgejo and hold no token. Each row's state
// sentence is written by the backend. The page saves five settings on a click (POST /setting); tokens
// stay in Hermes' own form. The merge button exists only when the backend says your write token can
// merge; it confirms first and the route merges pinned to the commit shown.
//
// Plain ESM, loaded uncompiled: jsx() calls, imports only @hermes/plugin-sdk, react and
// react/jsx-runtime, theme variables only, no eval.

import {
  atom,
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  DisclosureCaret,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  EmptyState,
  ErrorState,
  host,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  SearchField,
  SegmentedControl,
  SIDEBAR_NAV_AREA,
  Skeleton,
  Switch,
  Tip,
  ToggleRow,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { Component, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'forgebruecke'
// The same string as plugin.yaml (tests/test_settings.py holds them equal). The routes answer Hermes' version,
// so the pages can say when this half is older or newer.
export const VERSION = '0.0.26'
const PATH = '/forgebruecke'
const TAG = '[' + ID + ']'
const NAV_POLL_MS = 60000
const DESK_POLL_MS = 30000
const SETTINGS_HREF = '/settings?tab=plugins&agent=forgebruecke'
const CHAT_TITLE = 'Forgebrücke'
const FILTER_ABOVE = 8

let pluginCtx = null
// The sidebar row (the shared module below), kept here so the settings page can draw its Sidebar section.
let sidebarRow = null

function log(step, value) {
  try {
    console.info(TAG, step, value === undefined ? '' : value)
  } catch {
    /* no console */
  }
}

function el(type, props, ...children) {
  const { key, ...rest } = props || {}
  const kids = children.filter(c => c !== undefined)
  if (kids.length === 0) return jsx(type, rest, key)
  if (kids.length === 1) return jsx(type, { ...rest, children: kids[0] }, key)
  return jsxs(type, { ...rest, children: kids }, key)
}

const T = {
  primary: 'var(--ui-text-primary)',
  secondary: 'var(--ui-text-secondary)',
  tertiary: 'var(--ui-text-tertiary)',
  stroke: 'var(--ui-stroke-secondary)',
  strokeSoft: 'var(--ui-stroke-tertiary)',
  accent: 'var(--ui-accent)',
  red: 'var(--ui-red)',
  green: 'var(--ui-green)',
  yellow: 'var(--ui-yellow)',
  fill: 'var(--ui-bg-quaternary)'
}
const S = {
  col: gap => ({ display: 'flex', flexDirection: 'column', gap }),
  row: gap => ({ display: 'flex', alignItems: 'center', gap, flexWrap: 'wrap' }),
  line: gap => ({ display: 'flex', alignItems: 'center', gap, minWidth: 0 }),
  card: { border: '1px solid ' + T.stroke, borderRadius: 8, padding: 12, display: 'flex', flexDirection: 'column', gap: 8 },
  text: { fontSize: '0.75rem', color: T.secondary, lineHeight: 1.5 },
  small: { fontSize: '0.6875rem', color: T.secondary },
  mono: { fontFamily: 'var(--font-mono, ui-monospace, monospace)', fontSize: '0.6875rem' },
  clip: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }
}

// Inline styles cannot reach hover or keyboard focus, so those live in one sheet, added in register.
const SHEET = [
  '.fb-press{background:transparent;border:0;border-radius:6px;color:inherit;cursor:pointer;font:inherit;text-align:left}',
  '.fb-press:hover{background:' + T.fill + '}',
  '.fb-press:focus-visible,.fb-link:focus-visible{outline:2px solid ' + T.accent + ';outline-offset:1px}',
  '.fb-link{background:transparent;border:0;color:inherit;cursor:pointer;font:inherit;padding:0;text-align:left}',
  '.fb-link:hover{text-decoration:underline}',
  '.fb-repo{border-radius:6px}',
  '.fb-repo:hover{background:' + T.fill + '}',
  // Merge in the theme's green, darkened so white text stays readable in light and dark. The dialog's confirm
  // button takes no class, so the dialog is found by the marker its content carries.
  '.fb-merge,[role=dialog]:has(.fb-merge-dialog) button[data-variant=default]{background:color-mix(in srgb,' + T.green + ' 78%,black);color:white}',
  '.fb-merge:hover,[role=dialog]:has(.fb-merge-dialog) button[data-variant=default]:hover{background:color-mix(in srgb,' + T.green + ' 66%,black)}'
].join('\n')

// The desk answer's label after a park, before the refetch: the backend's rule (desk.nav_label). The sidebar row
// does not read it; it shows sidebarRowLabel('Forgebrücke', counts.waiting), the same text.
export function formatNavLabel(waiting) {
  const n = Number(waiting) || 0
  if (n <= 0) return 'Forgebrücke'
  return 'Forgebrücke · ' + (n < 100 ? String(n) : '99+')
}

// The sidebar row's count: the desk answer's counts.waiting, never its label, so a backend older than 0.0.22
// that still answers "Forgejo · 3" draws "Forgebrücke · 3". An answer without counts (a refusal) gives null
// and the row keeps its last label.
export function waitingOf(desk) {
  const counts = desk && desk.counts
  if (!counts || counts.waiting === null || counts.waiting === undefined || counts.waiting === '') return null
  const n = Number(counts.waiting)
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null
}

function showCount(desk) {
  const n = waitingOf(desk)
  if (sidebarRow && n !== null) sidebarRow.setCount(n)
}

// sidebar-row v1 begin
// The plugin's sidebar row: its brand as the label (plus an optional count), a show switch and a
// position, kept per Desktop in ctx.storage under 'sidebar' as { show, position }. One source:
// templates/plugin-skeleton/desktop/sidebar-row.js. Every copy (desktop/src/sidebar-row.js in a
// built plugin, the marked block in a single-file desktop/plugin.js) must equal it between these
// markers; tests/test_sidebar_row.py fails one that differs. Change the template, then copy it.
// Import-free: the plugin passes the SDK names in `sdk`:
//   { SIDEBAR_NAV_AREA, Switch, Input, jsx, jsxs, atom, useValue }
const SIDEBAR_ROW_KEY = 'sidebar'
const SIDEBAR_ROW_MIN = 1
const SIDEBAR_ROW_MAX = 99

/** An integer from 1 to 99, or `fallback` when `value` is not a number. */
function sidebarRowPosition(value, fallback) {
  if (value === null || value === undefined || value === '') return fallback
  const n = Math.round(Number(value))
  if (!Number.isFinite(n)) return fallback
  return Math.min(SIDEBAR_ROW_MAX, Math.max(SIDEBAR_ROW_MIN, n))
}

/** The row's label: the brand, plus " · N" while N is above 0 ("99+" from 100). */
function sidebarRowLabel(brand, count) {
  const n = Math.floor(Number(count) || 0)
  return n > 0 ? `${brand} · ${n < 100 ? n : '99+'}` : brand
}

/** Register the row from this Desktop's stored choice (nothing stored: shown, at defaultPosition).
 *  No backend read, so the row paints in its place with the first frame. Every change disposes the
 *  held contribution before it registers again: Desktop's registry replaces an entry by id, and an
 *  older disposer would then remove the new entry. */
function startSidebarRow(ctx, { brand, path, codicon, defaultPosition, sdk }) {
  const store = ctx.storage || { get: (key, fallback) => fallback, set: () => {} }
  const fallback = sidebarRowPosition(defaultPosition, 50)
  const stored = store.get(SIDEBAR_ROW_KEY, null)
  const saved = stored && typeof stored === 'object' ? stored : {}
  const $prefs = sdk.atom({ show: saved.show !== false, position: sidebarRowPosition(saved.position, fallback) })
  let count = 0
  let disposeRow = null
  let stopped = false

  const drop = () => {
    const dispose = disposeRow
    disposeRow = null
    if (dispose) dispose()
  }
  const apply = () => {
    drop()
    const { show, position } = $prefs.get()
    if (stopped || !show) return
    disposeRow = ctx.register({ id: 'nav', area: sdk.SIDEBAR_NAV_AREA, order: position, data: { codicon, label: sidebarRowLabel(brand, count), path } })
  }
  apply()

  return {
    brand,
    $prefs,
    prefs: () => $prefs.get(),
    /** A new count relabels a shown row; a hidden row stays hidden and shows the count when shown. */
    setCount(n) {
      const next = Math.max(0, Math.floor(Number(n) || 0))
      const relabel = sidebarRowLabel(brand, next) !== sidebarRowLabel(brand, count)
      count = next
      if (relabel && $prefs.get().show) apply()
    },
    /** Store { show, position } (either may be left out), then show, hide or move the row at once. */
    setPrefs(patch) {
      const now = $prefs.get()
      const p = patch && typeof patch === 'object' ? patch : {}
      const next = {
        show: 'show' in p ? p.show !== false : now.show,
        position: sidebarRowPosition(p.position, now.position),
      }
      if (next.show === now.show && next.position === now.position) return now
      store.set(SIDEBAR_ROW_KEY, next)
      $prefs.set(next)
      apply()
      return next
    },
    /** Remove the row for good (the plugin's own unload runs Desktop's disposers anyway). */
    dispose() {
      stopped = true
      drop()
    },
  }
}

/** The "Sidebar" block of the plugin's settings page: SDK Switch and Input, nothing else. */
function SidebarSection({ row, sdk }) {
  const { jsx, Switch, Input } = sdk
  const jsxs = sdk.jsxs || sdk.jsx
  const prefs = sdk.useValue(row.$prefs)
  const line = { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, minHeight: 32 }
  const note = { margin: 0, fontSize: '0.75rem', color: 'var(--ui-text-secondary)' }
  const commit = event => {
    const next = row.setPrefs({ position: event.target.value })
    event.target.value = String(next.position)
  }
  return jsxs('section', {
    'data-sidebar-section': row.brand,
    style: { display: 'grid', gap: 6, margin: '12px 0', padding: '10px 12px', border: '1px solid var(--ui-stroke-tertiary)', borderRadius: 8 },
    children: [
      jsx('h3', { style: { margin: 0, fontSize: '0.8125rem', fontWeight: 600 }, children: 'Sidebar' }, 'title'),
      jsxs('div', { style: line, children: [
        jsx('span', { children: 'Show in the sidebar' }),
        jsx(Switch, { checked: prefs.show, 'aria-label': 'Show in the sidebar', onCheckedChange: on => row.setPrefs({ show: on }) }),
      ] }, 'show'),
      jsxs('div', { style: line, children: [
        jsx('span', { children: 'Position' }),
        jsx(Input, {
          type: 'number', min: SIDEBAR_ROW_MIN, max: SIDEBAR_ROW_MAX, step: 1, inputMode: 'numeric',
          'aria-label': 'Position', defaultValue: String(prefs.position), style: { width: 72 },
          onBlur: commit,
          onKeyDown: event => { if (event.key === 'Enter') commit(event) },
        }, String(prefs.position)),
      ] }, 'position'),
      jsx('p', { style: note, children: 'Lower numbers sit higher. Kanban is 50.' }, 'hint'),
      prefs.show ? null : jsx('p', { style: note, children: `Open it from the command palette: Open ${row.brand}.` }, 'hidden'),
    ],
  })
}
// sidebar-row v1 end

// Every SDK name the shared module uses, passed in because the block imports nothing.
const SIDEBAR_SDK = { SIDEBAR_NAV_AREA, Switch, Input, jsx, jsxs, atom, useValue }

// ── backend ──────────────────────────────────────────────────────────────────────────────────────

function restError(error) {
  const message = error && error.message ? String(error.message) : String(error)
  const found = message.match(/\b([1-5]\d\d): ([\s\S]*)$/)
  const status = found ? Number(found[1]) : 0
  let body = null
  const raw = found ? found[2] : message
  const start = raw.indexOf('{')
  if (start >= 0) {
    try {
      body = JSON.parse(raw.slice(start))
    } catch {
      body = null
    }
  }
  const out = new Error((body && (body.message || (typeof body.detail === 'string' ? 'HTTP ' + status + ', ' + body.detail : null))) || raw || 'Request failed')
  out.status = status
  out.body = body
  return out
}

async function rest(path, opts) {
  if (!pluginCtx || typeof pluginCtx.rest !== 'function') throw new Error('This Hermes build cannot reach plugin routes.')
  try {
    return await pluginCtx.rest(path, { timeoutMs: 60000, ...(opts || {}) })
  } catch (error) {
    const out = restError(error)
    log('rest ' + ((opts && opts.method) || 'GET') + ' ' + path + ' failed', { status: out.status, message: out.message })
    throw out
  }
}

// Hermes answers 404 "Plugin not found" before any plugin code runs when the active profile has
// Forgebrücke off: say that, not a bare HTTP error.
function notHere(error) {
  return Boolean(error && error.status === 404 && /plugin not found/i.test(String((error.body && error.body.detail) || error.message || '')))
}

function useProfile() {
  return useValue(host.state.profile) || ''
}

const qk = (profile, ...rest) => [ID, profile, ...rest]
const retryOnce = failures => failures < 1

function useDesk() {
  const profile = useProfile()
  return useQuery({ queryKey: qk(profile, 'desk'), queryFn: () => rest('/desk'), refetchInterval: DESK_POLL_MS, staleTime: 10000, retry: retryOnce })
}

function useSettings() {
  const profile = useProfile()
  return useQuery({ queryKey: qk(profile, 'settings'), queryFn: () => rest('/settings'), refetchInterval: 120000, staleTime: 30000, retry: retryOnce })
}

function withSetting(settings, key, value) {
  if (!settings || settings.ok === false) return settings
  if (key === 'you') return { ...settings, you: value || null }
  return { ...settings, switches: { ...(settings.switches || {}), [key]: value } }
}

// Saves one of the five page settings. The settings query shows the new value at once and goes back to
// the old one, with a toast, when the route refuses. `quiet` leaves the error to the caller's dialog.
function useSaveSetting() {
  const client = useQueryClient()
  const profile = useProfile()
  return async (key, value, quiet) => {
    const settingsKey = qk(profile, 'settings')
    const before = client.getQueryData(settingsKey)
    client.setQueryData(settingsKey, withSetting(before, key, value))
    try {
      const out = await rest('/setting', { method: 'POST', body: { key, value } })
      if (!out || !out.ok) throw new Error((out && out.message) || 'Hermes did not confirm it.')
      await client.invalidateQueries({ queryKey: qk(profile) })
      return true
    } catch (error) {
      client.setQueryData(settingsKey, before)
      if (quiet) throw error
      host.notify({ kind: 'error', message: 'Not saved: ' + error.message })
      return false
    }
  }
}

// ── small pieces ─────────────────────────────────────────────────────────────────────────────────

// The mark: staff, two branches, the wings and two rings, in one colour so it follows
// the theme. The geometry is the brand's small-size mark.
const MARK_LINES = 'M32 16 L32 56 M32 50 C12 46 12 37 32 33 C52 29 52 20 32 16 M32 50 C52 46 52 37 32 33 C12 29 12 20 32 16'
const WING = 'M0 0 C8 0 16 -2 22 -8 M-1 5 C6 5 12 4 17 0 M-2 10 C4 10 8 9 12 6'

function Mark({ size }) {
  const wing = transform => el('path', { d: WING, fill: 'none', stroke: 'currentColor', strokeWidth: 5.3, strokeLinecap: 'round', strokeLinejoin: 'round', transform })
  const ring = cy => el('circle', { cx: 32, cy, r: 5.2, fill: 'var(--ui-bg-editor, transparent)', stroke: 'currentColor', strokeWidth: 3.6 })
  return el(
    'svg',
    { 'aria-label': 'Forgebrücke', role: 'img', viewBox: '0 0 64 64', width: size || 20, height: size || 20, style: { color: T.primary, flex: 'none' }, 'data-test': 'mark' },
    el('g', { transform: 'translate(0 2.95)' },
      el('path', { d: MARK_LINES, fill: 'none', stroke: 'currentColor', strokeWidth: 5, strokeLinejoin: 'round' }),
      el('path', { d: 'M27 56 L37 56', stroke: 'currentColor', strokeWidth: 3 }),
      wing('translate(39.5 13.5) scale(0.68 0.68) rotate(-26)'),
      wing('translate(24.5 13.5) scale(-0.68 0.68) rotate(-26)'),
      ring(16), ring(50))
  )
}

// Hermes answers with its version; a Desktop half of another version says so, with the command that updates it.
function VersionNote({ version }) {
  if (!version || version === VERSION) return null
  return el('div', { 'data-test': 'version-note', style: { ...S.line(6), fontSize: '0.75rem', color: T.yellow, flexWrap: 'wrap' } },
    el(Codicon, { name: 'warning', size: '0.75rem' }),
    el('span', null, 'Desktop shows ' + VERSION + ', Hermes runs ' + version + '. Update the plugin on this computer: ',
      el('code', null, 'hermes plugins update ' + ID)))
}

// Every address here comes from Forgejo or the user's own setting; only http(s) ever leaves the page.
export function isWebAddress(href) {
  return typeof href === 'string' && /^https?:\/\/[^\s]+$/i.test(href)
}

function openExternal(href) {
  if (!isWebAddress(href)) return
  const os = pluginCtx && pluginCtx.os
  Promise.resolve(os && typeof os.openExternal === 'function' ? os.openExternal(href) : false)
    .then(ok => {
      if (!ok) window.open(href, '_blank', 'noopener')
    })
    .catch(() => window.open(href, '_blank', 'noopener'))
}

const openFields = () => host.navigate(SETTINGS_HREF)

function ago(seconds) {
  if (seconds == null) return ''
  if (seconds < 60) return seconds + ' s ago'
  if (seconds < 3600) return Math.round(seconds / 60) + ' min ago'
  if (seconds < 86400) return Math.round(seconds / 3600) + ' h ago'
  return Math.round(seconds / 86400) + ' d ago'
}

// Seconds since the backend read Forgejo: its age when the answer arrived plus the time since.
export function readAge(ageAtArrival, arrivedAt, now) {
  if (!arrivedAt) return null
  return Math.max(0, Math.round((Number(ageAtArrival) || 0) + (now - arrivedAt) / 1000))
}

function useNow(everyMs) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), everyMs)
    return () => window.clearInterval(timer)
  }, [everyMs])
  return now
}

function Heading({ children }) {
  return el('div', { style: { fontSize: '0.6875rem', fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase', color: T.secondary } }, children)
}

// ── Ask Hermes: one reused chat tab, the question drafted and never sent ───────────────────────────

const sleep = ms => new Promise(resolve => window.setTimeout(resolve, ms))
const memory = new Map()

function readAtom(name) {
  try {
    const a = host.state && host.state[name]
    return a && typeof a.get === 'function' ? a.get() : undefined
  } catch {
    return undefined
  }
}

async function fillDraft(address, text) {
  const composer = host.composer
  if (!composer || typeof composer.setDraft !== 'function') return false
  for (let attempt = 1; attempt <= 8; attempt += 1) {
    try {
      if (await composer.setDraft(address, text)) return true
    } catch (error) {
      log('setDraft failed', error && error.message)
    }
    await sleep(400)
  }
  return false
}

async function createSession(profile) {
  const created = await host.request('session.create', { ...(profile ? { profile } : {}), title: CHAT_TITLE, follow_profile_config: true })
  const runtime = created && created.session_id
  const stored = created && created.stored_session_id
  if (!stored) throw new Error('session.create returned no stored_session_id')
  // session.create is lazy; naming it materializes the stored row so it can be opened.
  if (runtime) await host.request('session.title', { session_id: runtime, title: CHAT_TITLE })
  return stored
}

async function openChat(text) {
  const profile = readAtom('profile') || readAtom('focusedSessionProfile') || ''
  const storageKey = 'session:' + profile
  const storage = (pluginCtx && pluginCtx.storage) || { get: (k, d) => (memory.has(k) ? memory.get(k) : d), set: (k, v) => memory.set(k, v) }
  let stored = storage.get(storageKey, null)
  const open = id => host.openSession(id, { profile: profile || undefined, intent: 'tab', awaitHydration: true, tabTitle: CHAT_TITLE })
  try {
    if (typeof host.openSession !== 'function' || typeof host.request !== 'function') throw new Error('no session control in this Desktop build')
    if (stored) {
      try {
        await open(stored)
      } catch (error) {
        log('the stored chat did not open, creating a new one', error && error.message)
        stored = null
      }
    }
    if (!stored) {
      stored = await createSession(profile)
      storage.set(storageKey, stored)
      await open(stored)
    }
    if (await fillDraft(stored, text)) return
    throw new Error('the chat tab did not accept the draft')
  } catch (error) {
    log('session control failed, falling back to a new chat', error && error.message)
    if (typeof host.newChat === 'function') host.newChat(profile || null)
    await sleep(500)
    if ((await fillDraft('new', text)) || (await fillDraft(null, text))) return
    host.notify({ kind: 'warning', message: 'Paste this into a chat: ' + text })
  }
}

const QUESTIONS = [
  ['Summarise this pull request.', () => true],
  ['What should I check before I merge this?', row => row.tab === 'waiting'],
  ['Why is this blocked, and what fixes it?', row => row.tab === 'blocked' || (row.summary && row.summary.tone === 'bad')],
  ['Fix what the review asked for.', row => row.tab === 'blocked' || (row.summary && row.summary.tone === 'bad')]
]

export const ASK_OWN = 'Ask something else…'

// The chat draft: the PR reference, a space, then the question. Without one, the caret waits after the space.
export function askDraft(row, sentence) {
  return '@forgejo:' + row.repo + '#' + row.number + ' ' + (sentence || '')
}

// A menu separator drawn like Desktop's (dropdown-menu.tsx), without a new SDK import, so Desktop 0.23.0 still loads it.
const MenuLine = () => el('div', { role: 'separator', 'aria-orientation': 'horizontal', style: { height: 1, margin: '4px -4px', background: T.strokeSoft } })

function AskHermes({ row }) {
  return el(DropdownMenu, {},
    el(DropdownMenuTrigger, { asChild: true },
      el(Button, { size: 'xs', variant: 'secondary', style: { width: '100%' }, 'data-test': 'ask' }, el(Codicon, { name: 'comment-discussion', size: '0.75rem' }), 'Ask Hermes', el(Codicon, { name: 'chevron-down', size: '0.625rem' }))),
    el(DropdownMenuContent, { align: 'end' },
      ...QUESTIONS.filter(([, when]) => when(row)).map(([sentence]) => el(DropdownMenuItem, { key: sentence, onSelect: () => openChat(askDraft(row, sentence)) }, sentence)),
      el(MenuLine, { key: 'sep' }),
      el(DropdownMenuItem, { key: 'own', onSelect: () => openChat(askDraft(row)), 'data-test': 'ask-own' }, ASK_OWN)))
}

// ── rows ─────────────────────────────────────────────────────────────────────────────────────────

const SEGMENT = {
  done: { icon: 'pass-filled', color: T.green, word: 'done' },
  running: { icon: 'loading', color: T.accent, word: 'running', spin: true },
  pending: { icon: 'circle-large-outline', color: T.tertiary, word: 'next' },
  waiting: { icon: 'person', color: T.yellow, word: 'waiting' },
  failed: { icon: 'error', color: T.red, word: 'failed' }
}
const TONE = {
  ok: { icon: 'pass-filled', color: T.green },
  wait: { icon: 'person', color: T.yellow },
  run: { icon: 'loading', color: T.accent, spin: true },
  bad: { icon: 'error', color: T.red }
}
const CHECK_ICON = { success: ['pass', T.green], failure: ['error', T.red], error: ['error', T.red], pending: ['clock', T.yellow], warning: ['warning', T.yellow] }

// Icon and key for every gate; the word only on the gate the pull request stands at.
function GateChain({ gates, yours }) {
  const current = gates.findIndex(g => g.segment !== 'done')
  return el('div', { style: S.row(4), 'data-test': 'gates' },
    ...gates.map((g, i) => {
      const seg = SEGMENT[g.segment] || SEGMENT.pending
      const word = yours && g.segment === 'waiting' ? 'your turn' : seg.word
      return el(Tip, { key: g.key + i, label: g.detail || word },
        el('span', { 'data-segment': g.segment, style: { ...S.line(4), border: '1px solid ' + T.strokeSoft, borderRadius: 4, padding: '2px 6px', fontSize: '0.6875rem', color: T.primary } },
          el(Codicon, { name: seg.icon, size: '0.75rem', spinning: seg.spin, style: { color: seg.color } }),
          el('span', {}, g.type === 'human' ? 'merge' : g.key),
          i === current ? el('span', { style: { color: T.secondary } }, word) : null))
    }))
}

// `said` is the row's state sentence: a check whose description it already carries shows its state only.
function CheckLine({ check, said }) {
  const [icon, color] = CHECK_ICON[check.state] || ['circle-large-outline', T.tertiary]
  const repeats = Boolean(said && check.description && said.includes(check.description.replace(/\.$/, '')))
  return el('div', { style: { ...S.line(6), fontSize: '0.6875rem' } },
    el(Codicon, { name: icon, size: '0.75rem', style: { color } }),
    el('span', { style: { ...S.mono, color: T.primary } }, check.context),
    el('span', { style: { ...S.clip, color: T.secondary } }, (!repeats && check.description) || check.state))
}

// Passed checks fold into one line that opens on click; a failing or pending check is always listed.
function Checks({ checks, said }) {
  const [open, setOpen] = useState(false)
  const list = (checks && checks.statuses) || []
  if (!list.length) return null
  const passed = list.filter(s => s.state === 'success')
  const rest_ = list.filter(s => s.state !== 'success')
  return el('div', { style: S.col(3), 'data-test': 'checks' },
    ...rest_.map(s => el(CheckLine, { key: s.context, check: s, said })),
    passed.length
      ? el('button', { 'aria-expanded': open, className: 'fb-press', onClick: () => setOpen(!open), style: { ...S.line(6), fontSize: '0.6875rem', color: T.secondary, padding: '1px 4px 1px 0', alignSelf: 'flex-start' }, type: 'button', 'data-test': 'checks-passed' },
        el(DisclosureCaret, { open, size: '0.625rem' }),
        el(Codicon, { name: 'pass', size: '0.75rem', style: { color: T.green } }),
        passed.length + (passed.length === 1 ? ' check passed' : ' checks passed'))
      : null,
    ...(open ? passed.map(s => el(CheckLine, { key: s.context, check: s })) : []))
}

// The row's main button is the backend's choice (row.next): the user's own move, or a link.
const MOVE = {
  merge: 'Merge',
  merge_in_forgejo: 'Merge in Forgejo',
  review_in_forgejo: 'Review in Forgejo',
  open_in_forgejo: 'Open in Forgejo'
}

// Why a waiting row has no Merge button while the write token can merge.
export function noMergeReason(row, canMerge) {
  if (!canMerge || row.can_merge || row.tab !== 'waiting') return null
  if (row.draft) return 'A draft. Forgejo merges it once it is marked ready.'
  if (row.mergeable === false) return 'Forgejo cannot merge it yet.'
  return null
}

// The row's actions, stacked in one narrow column at its right: Merge first, then the link, Ask Hermes, Park, Comment.
function RowActions({ row, onMerge, why, onPark, you, canComment, onComment }) {
  const full = { width: '100%' }
  const merge = row.can_merge || row.next === 'merge'
    ? el(Button, { className: 'fb-merge', onClick: () => onMerge(row), size: 'xs', style: full, 'data-test': 'merge' }, el(Codicon, { name: 'git-merge', size: '0.75rem' }), MOVE.merge)
    : null
  const link = row.next === 'merge' ? null
    : el(Button, { disabled: !row.url, onClick: () => openExternal(row.url), size: 'xs', style: full, variant: row.next === 'open_in_forgejo' ? 'secondary' : 'default', 'data-test': 'open', 'data-why': why || undefined }, el(Codicon, { name: 'link-external', size: '0.75rem' }), MOVE[row.next] || MOVE.open_in_forgejo)
  return el('div', { style: { ...S.col(4), alignItems: 'stretch', flex: 'none', width: '9.5rem' }, 'data-test': 'actions' },
    merge,
    link && why ? el(Tip, { label: why }, link) : link,
    el(AskHermes, { row }),
    onPark && row.tab === 'waiting'
      ? el(Button, { onClick: () => onPark(row), size: 'xs', style: full, variant: 'ghost', 'data-test': 'park' }, el(Codicon, { name: 'archive', size: '0.75rem' }), row.back ? PARK.again : PARK.park)
      : null,
    canComment && you && row.tab === 'waiting'
      ? el(Tip, { label: PARK.commentTip(you) },
        el(Button, { onClick: onComment, size: 'xs', style: full, variant: 'ghost', 'data-test': 'comment-icon' }, el(Codicon, { name: 'comment', size: '0.75rem' }), PARK.comment))
      : null)
}

function Row({ row, onMerge, canMerge, onPark, you, canComment }) {
  const [commenting, setCommenting] = useState(false)
  const tone = TONE[(row.summary && row.summary.tone) || 'wait'] || TONE.wait
  const meta = row.repo + (row.author ? ' by ' + row.author : '') + (row.age_s != null ? ' · ' + ago(row.age_s) : '')
  const comment = row.last_comment
  return el('div', { style: S.card, 'data-test': 'row', 'data-tab': row.tab },
    el('div', { style: { display: 'flex', alignItems: 'flex-start', gap: 12 } },
      el('div', { style: { ...S.col(8), flex: 1, minWidth: 0 } },
        el('div', { style: S.col(2) },
          el('button', { className: 'fb-link text-sm font-medium', onClick: () => openExternal(row.url), style: { color: T.primary }, title: 'Open in Forgejo', type: 'button' }, '#' + row.number + ' ' + row.title),
          el('div', { style: S.small, title: row.head_sha ? 'Commit ' + row.head_sha.slice(0, 12) : undefined }, meta)),
        row.back ? el(BackLine, { row }) : null,
        row.summary
          ? el('div', { style: { ...S.line(6), fontSize: '0.75rem', color: T.primary, alignItems: 'flex-start' }, 'data-test': 'summary', 'data-tone': row.summary.tone },
            el(Codicon, { name: tone.icon, size: '0.85rem', spinning: tone.spin, style: { color: tone.color, marginTop: 1 } }),
            el('span', {}, row.summary.text))
          : null,
        row.gates && row.gates.length ? el(GateChain, { gates: row.gates, yours: Boolean(row.summary && row.summary.tone === 'ok') }) : null,
        row.review_summary ? el('div', { style: S.small, 'data-test': 'review-summary' }, row.review_summary) : null,
        el(Checks, { checks: row.checks, said: row.summary && row.summary.text }),
        comment
          ? el('div', { style: { ...S.small, borderLeft: '2px solid ' + T.strokeSoft, paddingLeft: 8 }, 'data-test': 'comment' },
            el('span', { style: { color: T.primary } }, (comment.author || 'Someone') + (comment.age_s != null ? ', ' + ago(comment.age_s) : '') + ': '), comment.body)
          : null),
      el(RowActions, { canComment, onComment: () => setCommenting(!commenting), onMerge, onPark, row, why: noMergeReason(row, canMerge), you })),
    commenting ? el(CommentBox, { row, you, onClose: () => setCommenting(false) }) : null)
}

// ── the parking area ─────────────────────────────────────────────────────────────────────────────

export const PARK = {
  park: 'Park',
  again: 'Park again',
  title: n => 'Park #' + n + '?',
  line: 'It leaves your count and comes back when something changes.',
  noteLabel: 'Note (only you see it)',
  notePlaceholder: 'For example: after the staging review',
  commits: 'Also bring it back on new commits',
  post: you => 'Also post the note on the PR, from @' + you,
  parked: n => 'Parked #' + n + '.',
  unparked: n => '#' + n + ' is back in Waiting for me.',
  undo: 'Undo',
  divider: p => 'Parked · ' + p,
  parkedLine: (note, age) => (note ? note + ' · ' : '') + 'parked ' + age,
  unpark: 'Unpark',
  open: 'Open',
  back: 'Back',
  yourNote: note => 'Your note: ' + note,
  whatChanged: 'What changed',
  commentTip: you => 'Comment as @' + you,
  comment: 'Comment',
  commentPlaceholder: 'Write a comment. Markdown works.',
  postAs: you => 'Post as @' + you,
  cancel: 'Cancel',
  commentHint: 'Posts with your write token, as you.',
  posted: you => 'Comment posted as @' + you + '.'
}

// Inputs inside ConfirmDialog: its content confirms on Enter and Space from anywhere (Desktop's
// confirm-dialog.tsx), so every key but Enter stays in the input. Enter parks; Space types.
const onlyEnter = event => {
  if (event.key !== 'Enter') event.stopPropagation()
}

export function seenOf(row) {
  return { head_sha: row.head_sha || '', comment_at: (row.wake_comment && row.wake_comment.at) || null, draft: typeof row.draft === 'boolean' ? row.draft : null }
}

// The desk answer after a park or an unpark, before the refetch: the row moves, the counts and the label follow.
export function withParked(data, id, parked) {
  if (!data || !data.rows) return data
  let moved = false
  const rows = data.rows.map(r => {
    if (r.id !== id || r.tab !== 'waiting') return r
    moved = true
    return parked ? { ...r, section: 'parked', back: null, parked } : { ...r, section: 'now', back: null, parked: null }
  })
  if (!moved) return data
  const waiting = rows.filter(r => r.tab === 'waiting' && r.section !== 'parked').length
  const counts = { ...(data.counts || {}), waiting, parked: rows.filter(r => r.section === 'parked').length }
  return { ...data, rows, counts, label: formatNavLabel(waiting) }
}

// The desk answer right after a merge, before the refetch: the merged row leaves, the counts and the label follow.
export function withoutRow(data, id) {
  if (!data || !data.rows || !data.rows.some(r => r.id === id)) return data
  const rows = data.rows.filter(r => r.id !== id)
  const counts = { ...(data.counts || {}) }
  for (const tab of ['waiting', 'running', 'blocked']) counts[tab] = rows.filter(r => r.tab === tab && r.section !== 'parked').length
  counts.parked = rows.filter(r => r.section === 'parked').length
  return { ...data, rows, counts, label: formatNavLabel(counts.waiting) }
}

function useParking() {
  const client = useQueryClient()
  const profile = useProfile()
  const key = qk(profile, 'desk')
  const show = (id, parked) => {
    const next = withParked(client.getQueryData(key), id, parked)
    if (next) {
      client.setQueryData(key, next)
      showCount(next)
    }
  }
  const refetch = () => client.invalidateQueries({ queryKey: key })
  const park = async (row, { note, wakeCommits, seen }) => {
    const out = await rest('/park', { method: 'POST', body: { repo: row.repo, number: row.number, note, wake_commits: wakeCommits, seen } })
    if (!out || !out.ok) throw new Error((out && out.message) || 'Not parked.')
    show(row.id, { ...out.parked, age_s: 0 })
    void refetch()
    host.notify({ kind: 'success', message: PARK.parked(row.number), action: { label: PARK.undo, onClick: () => void unpark(row, { quiet: true }) } })
    return out
  }
  const unpark = async (row, { quiet } = {}) => {
    try {
      const out = await rest('/unpark', { method: 'POST', body: { repo: row.repo, number: row.number } })
      if (!out || !out.ok) throw new Error((out && out.message) || 'Not unparked.')
      show(row.id, null)
      void refetch()
      const was = row.parked
      if (!quiet && was) {
        host.notify({ kind: 'success', message: PARK.unparked(row.number), action: { label: PARK.undo, onClick: () => void park(row, { note: was.note || '', wakeCommits: was.wake_commits !== false, seen: was.seen || seenOf(row) }).catch(error => host.notify({ kind: 'error', message: error.message })) } })
      }
    } catch (error) {
      host.notify({ kind: 'error', message: 'Not unparked: ' + error.message })
    }
  }
  return { park, unpark }
}

function ParkDialog({ row, you, canComment, onClose }) {
  const [note, setNote] = useState('')
  const [commits, setCommits] = useState(true)
  const [post, setPost] = useState(false)
  const noteRef = useRef(null)
  const flight = useRef(false)
  const posted = useRef(false)
  const { park } = useParking()
  useEffect(() => {
    // The dialog focuses its confirm button on open. This effect runs after the dialog's (a parent's effects
    // follow its children's), and the tick keeps the note focused if Radix ever focuses later.
    const timer = window.setTimeout(() => noteRef.current && noteRef.current.focus(), 0)
    return () => window.clearTimeout(timer)
  }, [])
  if (!row) return null
  const text = note.trim()
  const canPost = Boolean(canComment && you && text)
  const run = async () => {
    if (flight.current) return // ConfirmDialog with dismissOnConfirm never goes busy: a second Enter lands here
    flight.current = true
    try {
      if (canPost && post && !posted.current) {
        const out = await rest('/comment', { method: 'POST', body: { repo: row.repo, number: row.number, body: text } })
        if (!out || !out.ok) throw new Error((out && out.message) || 'The note was not posted.')
        posted.current = true // a failed park and a second confirm never post it twice
        setPost(false)
      }
      await park(row, { note: text, wakeCommits: commits, seen: seenOf(row) })
    } finally {
      flight.current = false
    }
  }
  const box = (checked, onChange, label, test) =>
    el('label', { style: { ...S.line(6), fontSize: '0.75rem', color: T.primary, cursor: 'pointer' } },
      el('input', { checked, onChange: e => onChange(e.target.checked), onKeyDown: onlyEnter, type: 'checkbox', 'data-test': test }), label)
  return el(ConfirmDialog, {
    children: el('div', { style: S.col(8) },
      el('label', { style: S.col(4) },
        el('span', { style: S.small }, PARK.noteLabel),
        el('input', {
          maxLength: 280, onChange: e => setNote(e.target.value), onKeyDown: onlyEnter, placeholder: PARK.notePlaceholder, ref: noteRef, type: 'text', value: note, 'data-test': 'park-note',
          style: { background: 'transparent', border: '1px solid ' + T.stroke, borderRadius: 6, color: T.primary, font: 'inherit', fontSize: '0.8125rem', padding: '5px 8px' }
        })),
      box(commits, setCommits, PARK.commits, 'park-commits'),
      canPost ? box(post, setPost, PARK.post(you), 'park-post') : null),
    confirmLabel: PARK.park,
    description: PARK.line,
    dismissOnConfirm: true,
    onClose,
    onConfirm: run,
    open: true,
    title: PARK.title(row.number)
  })
}

function BackLine({ row }) {
  const note = row.parked && row.parked.note
  return el('div', { style: { ...S.line(6), fontSize: '0.75rem', color: T.primary, alignItems: 'flex-start', flexWrap: 'wrap' }, 'data-test': 'back', 'data-reason': row.back.reason },
    el(Badge, { variant: 'default' }, PARK.back),
    el('span', null, row.back.text),
    note ? el('span', { style: { color: T.secondary } }, PARK.yourNote(note)) : null,
    row.back.url ? el('button', { className: 'fb-link', onClick: () => openExternal(row.back.url), style: { color: T.accent }, type: 'button', 'data-test': 'what-changed' }, PARK.whatChanged) : null)
}

function CommentBox({ row, you, onClose }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const client = useQueryClient()
  const profile = useProfile()
  const send = async () => {
    if (busy || !text.trim()) return
    setBusy(true)
    try {
      const out = await rest('/comment', { method: 'POST', body: { repo: row.repo, number: row.number, body: text } })
      if (!out || !out.ok) throw new Error((out && out.message) || 'Not posted.')
      host.notify({ kind: 'success', message: PARK.posted(you), action: out.url ? { label: PARK.open, onClick: () => openExternal(out.url) } : undefined })
      onClose()
      void client.invalidateQueries({ queryKey: qk(profile, 'desk') })
    } catch (error) {
      host.notify({ kind: 'error', message: 'Not posted: ' + error.message })
    } finally {
      setBusy(false)
    }
  }
  return el('div', { style: S.col(6), 'data-test': 'comment-box' },
    el('textarea', {
      autoFocus: true, onChange: e => setText(e.target.value), placeholder: PARK.commentPlaceholder, rows: 3, value: text, 'data-test': 'comment-text',
      style: { background: 'transparent', border: '1px solid ' + T.stroke, borderRadius: 6, color: T.primary, font: 'inherit', fontSize: '0.8125rem', padding: '6px 8px', resize: 'vertical' }
    }),
    el('div', { style: { ...S.row(8), justifyContent: 'space-between' } },
      el('span', { style: S.small }, PARK.commentHint),
      el('div', { style: S.line(6) },
        el(Button, { onClick: onClose, size: 'xs', variant: 'ghost' }, PARK.cancel),
        el(Button, { disabled: busy || !text.trim(), onClick: send, size: 'xs', 'data-test': 'comment-post' }, PARK.postAs(you)))))
}

function ParkedRow({ row, onUnpark }) {
  const p = row.parked || {}
  return el('div', { style: { ...S.line(10), justifyContent: 'space-between', padding: '6px 12px', border: '1px solid ' + T.strokeSoft, borderRadius: 8 }, 'data-test': 'parked-row' },
    el('div', { style: { ...S.col(1), minWidth: 0 } },
      el('button', { className: 'fb-link', onClick: () => openExternal(row.url), style: { ...S.clip, fontSize: '0.8125rem', color: T.primary }, title: 'Open in Forgejo', type: 'button' }, '#' + row.number + ' ' + row.title),
      el('div', { style: { ...S.small, ...S.clip }, 'data-test': 'parked-line' }, row.repo + ' · ' + PARK.parkedLine(p.note, ago(p.age_s || 0)))),
    el('div', { style: { ...S.line(6), flex: 'none' } },
      el(Button, { onClick: () => onUnpark(row), size: 'xs', variant: 'secondary', 'data-test': 'unpark' }, PARK.unpark),
      el(Button, { disabled: !row.url, onClick: () => openExternal(row.url), size: 'xs', variant: 'ghost' }, el(Codicon, { name: 'link-external', size: '0.75rem' }), PARK.open)))
}

const PARKED_OPEN = profile => 'forgebruecke:parked-open:' + (profile || 'default')

function readOpen(profile) {
  try {
    return window.localStorage.getItem(PARKED_OPEN(profile)) !== '0'
  } catch {
    return true
  }
}

function ParkedArea({ rows, onUnpark }) {
  const profile = useProfile()
  const [open, setOpen] = useState(() => readOpen(profile))
  const toggle = () => {
    const next = !open
    setOpen(next)
    try {
      window.localStorage.setItem(PARKED_OPEN(profile), next ? '1' : '0')
    } catch {
      /* the choice holds until the page reloads */
    }
  }
  return el('div', { style: S.col(8), 'data-test': 'parked-area' },
    el('button', { 'aria-expanded': open, className: 'fb-press', onClick: toggle, style: { ...S.line(8), fontSize: '0.75rem', color: T.secondary, padding: '2px 0' }, type: 'button', 'data-test': 'divider' },
      el(DisclosureCaret, { open, size: '0.625rem' }),
      el('span', null, PARK.divider(rows.length)),
      el('span', { style: { flex: 1, borderTop: '1px solid ' + T.stroke } })),
    open ? el('div', { style: S.col(6) }, ...rows.map(r => el(ParkedRow, { key: r.id, onUnpark, row: r }))) : null)
}

const EMPTY = { waiting: 'Nothing waits for you.', running: 'Nothing is running.', blocked: 'Nothing is blocked.' }

export const ALL_REPOS = 'all'

// One filter button per repository with rows in Waiting for me (parked rows not counted), sorted by name so a
// click never moves a button. The owner is left out of the label when every repository shares one.
export function repoButtons(rows) {
  const counts = new Map()
  for (const r of rows || []) {
    if (r.tab === 'waiting' && r.section !== 'parked' && r.repo) counts.set(r.repo, (counts.get(r.repo) || 0) + 1)
  }
  const repos = [...counts.keys()].sort((a, b) => a.localeCompare(b))
  const owners = new Set(repos.map(repo => repo.split('/')[0]))
  return repos.map(repo => ({ repo, count: counts.get(repo), label: owners.size === 1 && repo.includes('/') ? repo.slice(repo.indexOf('/') + 1) : repo }))
}

// The filter in force: the chosen repository while it still has a button, otherwise All. With fewer than two
// repositories no filter row shows, so All.
export function pickRepo(buttons, chosen) {
  return chosen !== ALL_REPOS && buttons.length > 1 && buttons.some(b => b.repo === chosen) ? chosen : ALL_REPOS
}

function RepoFilter({ buttons, active, total, onPick }) {
  const pick = (id, label, count, title) => el(Button, {
    key: id, 'aria-pressed': active === id, onClick: () => onPick(id), size: 'xs', title, variant: active === id ? 'default' : 'chip', 'data-test': 'repo-filter', 'data-repo': id
  }, label + ' · ' + count)
  return el('div', { role: 'group', 'aria-label': 'Filter by repository', style: S.row(6), 'data-test': 'repo-filters' },
    pick(ALL_REPOS, 'All', total),
    ...buttons.map(b => pick(b.repo, b.label, b.count, b.repo)))
}

// Without the setting you, Waiting for me cannot know whose review requests to list. One click sets it
// when the backend knows the login; "Open settings" opens Hermes' own form for another login.
function WhoAreYou({ login, onDone }) {
  const save = useSaveSetting()
  const [busy, setBusy] = useState(false)
  const use = async () => {
    setBusy(true)
    const ok = await save('you', login)
    setBusy(false)
    if (ok && onDone) onDone()
  }
  return el('div', { style: S.row(6), 'data-test': 'who' },
    login ? el(Button, { disabled: busy, onClick: use, size: 'xs', 'data-test': 'use-login' }, 'Use @' + login) : null,
    el(Button, { onClick: openFields, size: 'xs', variant: 'secondary' }, 'Open settings'))
}

function WaitingNote({ note }) {
  if (!note || !note.message) return null
  return el('div', { style: { ...S.card, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 }, 'data-test': 'waiting-note' },
    el('div', { style: S.line(8) },
      el(Codicon, { name: 'person', size: '0.85rem', style: { color: T.accent } }),
      el('span', { style: { fontSize: '0.75rem', color: T.primary } }, note.message)),
    el(WhoAreYou, { login: note.suggestion }))
}

function Notes({ notes }) {
  if (!notes || !notes.length) return null
  return el('div', { style: S.col(2), 'data-test': 'notes' }, ...notes.map(n => el('div', { key: n, style: { ...S.small, color: T.yellow } }, n)))
}

function RowList({ rows, tab, setTab, onMerge, note, blocked, canMerge, onPark, you, canComment, repo, setRepo }) {
  const { unpark } = useParking()
  const buttons = tab === 'waiting' ? repoButtons(rows) : []
  const active = pickRepo(buttons, repo)
  useEffect(() => {
    if (tab === 'waiting' && active !== repo) setRepo(active)
  }, [tab, active, repo, setRepo])
  const all = rows.filter(r => r.tab === tab && (active === ALL_REPOS || r.repo === active))
  const mine = all.filter(r => r.section !== 'parked')
  const parked = tab === 'waiting' ? all.filter(r => r.section === 'parked') : []
  const filters = buttons.length > 1 ? el(RepoFilter, { active, buttons, onPick: setRepo, total: buttons.reduce((n, b) => n + b.count, 0) }) : null
  const top = tab === 'waiting' ? el(WaitingNote, { note }) : null
  const area = parked.length ? el(ParkedArea, { onUnpark: unpark, rows: parked }) : null
  if (!mine.length) {
    return el('div', { style: S.col(8), 'data-test': 'empty' },
      top,
      el(EmptyState, { title: EMPTY[tab] }),
      tab === 'waiting' && blocked ? el('div', { style: { display: 'flex', justifyContent: 'center' } }, el(Button, { onClick: () => setTab('blocked'), size: 'sm', variant: 'secondary' }, 'Show blocked')) : null,
      area)
  }
  return el('div', { style: S.col(10) }, top, filters, ...mine.map(r => el(Row, { key: r.id, canComment, canMerge, onMerge, onPark, row: r, you })), area)
}

// ── the Repos tab: list or grid, open pull requests that expand, actions per repo ──

export const LAYOUTS = ['list', 'grid']
const LAYOUT_KEY = profile => 'forgebruecke:repos-layout:' + (profile || 'default')
// 20rem cards: 3 per row in a 1280 px window, 4 from 1600 px (run-fb3.cjs measures both). The cards of one row
// stretch to one height, and the count line sits at the bottom of each (marginTop auto in RepoCard), so a badge
// on one card does not lift its neighbours' count lines.
const GRID = { display: 'grid', gap: 10, gridTemplateColumns: 'repeat(auto-fill, minmax(20rem, 1fr))', alignItems: 'stretch' }

export function readLayout(stored) {
  return LAYOUTS.includes(stored) ? stored : 'list'
}

function useLayout() {
  const profile = useProfile()
  const [layout, setLayout] = useState(() => {
    try {
      return readLayout(window.localStorage.getItem(LAYOUT_KEY(profile)))
    } catch {
      return 'list'
    }
  })
  const choose = next => {
    setLayout(readLayout(next))
    try {
      window.localStorage.setItem(LAYOUT_KEY(profile), readLayout(next))
    } catch {
      /* the choice holds until the page reloads */
    }
  }
  return [layout, choose]
}

export const REPO_QUESTION = 'What changed here this week?'

export function repoAskDraft(repo) {
  return '@forgejo:' + repo + ' ' + REPO_QUESTION
}

// Rows in Waiting for me per repository, parked rows not counted: the same numbers as the filter buttons.
export function waitingByRepo(rows) {
  const out = {}
  for (const b of repoButtons(rows)) out[b.repo.toLowerCase()] = b.count
  return out
}

// "<n> waiting for you": Waiting for me, filtered to this repository.
export function showWaiting(repo, setTab, setRepo) {
  setRepo(repo)
  setTab('waiting')
}

// The desk answer right after a merge from anywhere on the page: the row leaves Waiting for me (withoutRow) and the
// repo's open pull request count drops by one, before the refetch.
export function withMerged(data, row) {
  if (!data) return data
  const next = withoutRow(data, row.id) || data
  if (!next.repos || !row.repo) return next
  const repo = row.repo.toLowerCase()
  return { ...next, repos: next.repos.map(c => (c.repo.toLowerCase() === repo ? { ...c, open_prs: Math.max(0, (Number(c.open_prs) || 0) - 1) } : c)) }
}

// The expanded repo's answer after the same merge: the pull request leaves the list.
export function withoutPull(data, id) {
  if (!data || !data.pulls) return data
  return { ...data, pulls: data.pulls.filter(p => p.id !== id) }
}

const prWord = n => n + (n === 1 ? ' open pull request' : ' open pull requests')

function useRepoPulls(repo, enabled) {
  const profile = useProfile()
  return useQuery({ queryKey: qk(profile, 'repo-pulls', repo.toLowerCase()), queryFn: () => rest('/repo-pulls?repo=' + encodeURIComponent(repo)), enabled, staleTime: 15000, retry: retryOnce })
}

// The default branch's latest commit and its checks, one line.
function BranchLine({ branch, name }) {
  if (!branch) return el('div', { style: S.small, 'data-test': 'branch' }, (name || 'The default branch') + ': its latest commit could not be read.')
  const state = (branch.checks && branch.checks.state) || 'none'
  const failing = ((branch.checks && branch.checks.statuses) || []).filter(s => s.state === 'failure' || s.state === 'error').length
  const [icon, color] = state === 'none' ? ['circle-large-outline', T.tertiary] : CHECK_ICON[state] || ['circle-large-outline', T.tertiary]
  const said = state === 'none' ? 'no checks' : state === 'success' ? 'checks pass' : failing ? failing + (failing === 1 ? ' check fails' : ' checks fail') : 'checks ' + state
  return el('div', { style: { ...S.line(6), fontSize: '0.75rem', color: T.primary }, 'data-test': 'branch', 'data-state': state },
    el(Codicon, { name: icon, size: '0.85rem', style: { color } }),
    el('span', { style: S.mono }, branch.name),
    el('button', { className: 'fb-link', onClick: () => openExternal(branch.url), style: { ...S.mono, color: T.secondary }, title: 'Open the commit in Forgejo', type: 'button' }, branch.sha.slice(0, 7)),
    el('span', { style: { color: T.secondary } }, said))
}

// One open pull request inside an expanded repo: title, author, age, checks; Merge (the desk's rule), the link and
// Ask Hermes in the same narrow column as the Waiting rows.
function RepoPull({ pull, onMerge, tab }) {
  const row = { ...pull, tab }
  const full = { width: '100%' }
  return el('div', { style: { display: 'flex', alignItems: 'flex-start', gap: 12, padding: '8px 0', borderTop: '1px solid ' + T.strokeSoft }, 'data-test': 'repo-pull', 'data-number': pull.number },
    el('div', { style: { ...S.col(4), flex: 1, minWidth: 0 } },
      el('button', { className: 'fb-link text-sm', onClick: () => openExternal(pull.url), style: { color: T.primary }, title: 'Open in Forgejo', type: 'button' }, '#' + pull.number + ' ' + pull.title),
      el('div', { style: S.small }, [pull.author ? 'by ' + pull.author : '', pull.age_s != null ? ago(pull.age_s) : '', pull.draft ? 'draft' : '', pull.flow ? 'flow ' + pull.flow : ''].filter(Boolean).join(' · ')),
      el(Checks, { checks: pull.checks })),
    el('div', { style: { ...S.col(4), alignItems: 'stretch', flex: 'none', width: '9.5rem' }, 'data-test': 'actions' },
      pull.can_merge ? el(Button, { className: 'fb-merge', onClick: () => onMerge(row), size: 'xs', style: full, 'data-test': 'merge' }, el(Codicon, { name: 'git-merge', size: '0.75rem' }), MOVE.merge) : null,
      el(Button, { disabled: !pull.url, onClick: () => openExternal(pull.url), size: 'xs', style: full, variant: 'secondary', 'data-test': 'open' }, el(Codicon, { name: 'link-external', size: '0.75rem' }), MOVE.open_in_forgejo),
      el(AskHermes, { row })))
}

function RepoPulls({ repo, onMerge, tabs }) {
  const q = useRepoPulls(repo, true)
  if (q.isLoading) return el('div', { style: S.col(6), 'data-test': 'repo-pulls-loading' }, el(Skeleton, { style: { height: 18 } }), el(Skeleton, { style: { height: 48 } }))
  if (q.isError || (q.data && q.data.ok === false)) {
    const message = q.isError ? q.error.message : q.data.message
    return el('div', { style: { ...S.row(8), ...S.small }, 'data-test': 'repo-pulls-error' }, 'Not read: ' + message, el(Button, { onClick: () => q.refetch(), size: 'xs', variant: 'ghost' }, 'Try again'))
  }
  const data = q.data
  return el('div', { style: S.col(4), 'data-test': 'repo-pulls' },
    el(BranchLine, { branch: data.branch, name: data.default_branch }),
    data.pulls.length
      ? el('div', { style: S.col(0) }, ...data.pulls.map(p => el(RepoPull, { key: p.id, onMerge, pull: p, tab: tabs[p.id] || 'open' })))
      : el('div', { style: { ...S.small, paddingTop: 6 }, 'data-test': 'repo-pulls-none' }, 'No open pull requests.'),
    data.has_more ? el('div', { style: S.small }, 'The newest ' + data.pulls.length + ' are listed. Open the repo for the rest.') : null)
}

// One repo, as a line (list) or a card (grid). The open pull request count expands the repo in place; an expanded
// card spans the whole grid row so its pull requests get the width of a Waiting row.
function RepoCard({ card, layout, open, onToggle, waiting, onWaiting, onMerge, tabs }) {
  const n = Number(card.open_prs) || 0
  const badges = [
    card.right === 'read' ? el(Badge, { key: 'read', variant: 'muted', 'data-test': 'right' }, 'read only') : null,
    ...(card.flows || []).map(f => el(Badge, { key: 'flow-' + f, variant: 'success' }, 'flow ' + f)),
    !card.mentions && card.right !== 'read' ? el(Badge, { key: 'mentions', variant: 'muted' }, 'mentions off') : null
  ].filter(Boolean)
  const name = el('button', { className: 'fb-link', onClick: () => openExternal(card.url), style: { ...S.mono, ...S.clip, fontSize: '0.75rem', color: T.primary }, title: 'Open in Forgejo', type: 'button', 'data-test': 'repo-name' }, card.repo)
  const expander = el('button', { 'aria-expanded': open, className: 'fb-press', onClick: onToggle, style: { ...S.line(4), ...S.small, padding: '2px 6px 2px 2px', flex: 'none' }, type: 'button', 'data-test': 'repo-expand' },
    el(DisclosureCaret, { open, size: '0.625rem' }), n > 0 ? prWord(n) : 'No open pull requests')
  const jump = waiting > 0
    ? el(Button, { onClick: onWaiting, size: 'xs', variant: 'chip', 'data-test': 'repo-waiting' }, el(Codicon, { name: 'person', size: '0.75rem' }), waiting + ' waiting for you')
    : null
  const icon = (name_, label, onClick, test) => el(Tip, { label }, el(Button, { 'aria-label': label, onClick, size: 'xs', variant: 'ghost', 'data-test': test }, el(Codicon, { name: name_, size: '0.75rem' })))
  const actions = el('div', { style: { ...S.line(2), flex: 'none' } },
    icon('comment-discussion', 'Ask Hermes about this repo', () => openChat(repoAskDraft(card.repo)), 'repo-ask'),
    icon('link-external', 'Open in Forgejo', () => openExternal(card.url), 'repo-open'),
    icon('organization', 'Change access', () => openExternal(card.collaboration_url), 'repo-access'))
  const pulls = open ? el(RepoPulls, { onMerge, repo: card.repo, tabs }) : null
  if (layout === 'grid') {
    return el('div', { style: { ...S.card, gap: 6, ...(open ? { gridColumn: '1 / -1' } : {}) }, 'data-test': 'repo', 'data-repo': card.repo, 'data-right': card.right, 'data-open': open ? 'true' : 'false' },
      el('div', { style: { ...S.line(6), justifyContent: 'space-between' } }, name, actions),
      badges.length ? el('div', { style: S.row(4) }, ...badges) : null,
      el('div', { style: { ...S.row(6), justifyContent: 'space-between', marginTop: 'auto' }, 'data-test': 'repo-count' }, expander, jump),
      pulls)
  }
  return el('div', { style: { ...S.col(4), padding: '4px 8px', borderRadius: 6, ...(open ? { background: T.fill } : {}) }, className: open ? undefined : 'fb-repo', 'data-test': 'repo', 'data-repo': card.repo, 'data-right': card.right, 'data-open': open ? 'true' : 'false' },
    el('div', { style: S.line(8) },
      name,
      ...badges,
      el('span', { style: { flex: 1 } }),
      jump,
      expander,
      actions),
    pulls ? el('div', { style: { paddingLeft: 14 } }, pulls) : null)
}

// Every repo Hermes' account sees. A badge only for what differs from the usual (write access, no flow, mentions
// answered).
function Repos({ repos, error, forgeUrl, onRetry, rows, setTab, setRepo, onMerge }) {
  const [query, setQuery] = useState('')
  const [layout, setLayout] = useLayout()
  const [open, setOpen] = useState({})
  if (error) {
    return el('div', { style: S.col(8), 'data-test': 'empty' },
      el(EmptyState, { title: error }),
      el('div', { style: { display: 'flex', justifyContent: 'center' } }, el(Button, { onClick: onRetry, size: 'sm', variant: 'secondary' }, 'Try again')))
  }
  if (!repos.length) {
    return el('div', { style: S.col(8), 'data-test': 'empty' },
      el(EmptyState, { title: 'Hermes sees no repos yet.', description: 'In Forgejo, add Hermes’ account to a repo under Settings, Collaborators.' }),
      el('div', { style: { display: 'flex', justifyContent: 'center' } }, el(Button, { disabled: !forgeUrl, onClick: () => openExternal(forgeUrl), size: 'sm', variant: 'secondary' }, 'Open Forgejo')))
  }
  const needle = query.trim().toLowerCase()
  const shown = needle ? repos.filter(r => r.repo.toLowerCase().includes(needle)) : repos
  const waiting = waitingByRepo(rows)
  const tabs = {}
  for (const r of rows || []) tabs[r.id] = r.section === 'parked' ? 'parked' : r.tab
  const cards = shown.map(r => el(RepoCard, {
    key: r.repo, card: r, layout, onMerge, tabs,
    open: Boolean(open[r.repo]),
    onToggle: () => setOpen({ ...open, [r.repo]: !open[r.repo] }),
    waiting: waiting[r.repo.toLowerCase()] || 0,
    onWaiting: () => showWaiting(r.repo, setTab, setRepo)
  }))
  return el('div', { style: S.col(10) },
    el('div', { style: { ...S.row(12), justifyContent: 'space-between' } },
      el('div', { style: S.small }, 'Hermes can do in a repo what its account may do in Forgejo.'),
      el('div', { style: S.row(10) },
        repos.length > FILTER_ABOVE ? el('div', { style: { width: '15rem', maxWidth: '100%' } }, el(SearchField, { onChange: setQuery, placeholder: 'Filter repos', value: query, variant: 'box' })) : null,
        el('div', { 'data-test': 'repos-layout', 'data-layout': layout },
          el(SegmentedControl, { onChange: setLayout, options: [{ id: 'list', label: 'List' }, { id: 'grid', label: 'Grid' }], sizing: 'content', value: layout })))),
    shown.length
      ? el('div', { style: layout === 'grid' ? GRID : S.col(0), 'data-test': 'repos', 'data-layout': layout }, ...cards)
      : el('div', { style: { ...S.small, padding: '5px 8px' } }, 'No repo matches “' + query.trim() + '”.'))
}

function MergeDialog({ row, onClose }) {
  const client = useQueryClient()
  const profile = useProfile()
  if (!row) return null
  const run = async () => {
    const result = await rest('/merge', { method: 'POST', body: { repo: row.repo, number: row.number, head_sha: row.head_sha } })
    if (!result || !result.ok) throw new Error((result && result.message) || 'Not merged.')
    const key = qk(profile, 'desk')
    const next = withMerged(client.getQueryData(key), row)
    if (next) {
      client.setQueryData(key, next)
      showCount(next)
    }
    const pullsKey = qk(profile, 'repo-pulls', row.repo.toLowerCase())
    const pulls = withoutPull(client.getQueryData(pullsKey), row.id)
    if (pulls) client.setQueryData(pullsKey, pulls)
    host.notify({ kind: 'success', message: result.message })
    void client.invalidateQueries({ queryKey: key })
    void client.invalidateQueries({ queryKey: pullsKey })
  }
  return el(ConfirmDialog, {
    children: el('div', { className: 'fb-merge-dialog', style: S.col(4) },
      el('div', { style: { fontSize: '0.75rem', color: T.primary } }, row.title),
      el('div', { style: S.small }, row.repo + ' #' + row.number + ', commit ', el('span', { style: S.mono }, row.head_sha.slice(0, 12)), ' into ' + (row.base || 'the base branch'))),
    confirmLabel: 'Merge as me',
    busyLabel: 'Merging',
    doneLabel: 'Merged',
    description: 'Merges with your account, not as Hermes. If the branch moved, nothing is merged.',
    onClose,
    onConfirm: run,
    open: true,
    title: 'Merge pull request #' + row.number + '?'
  })
}

// ── the desk page ────────────────────────────────────────────────────────────────────────────────

function Header({ data, updatedAt, onRefresh, refreshing }) {
  const now = useNow(10000)
  const forge = data && data.forge_url ? data.forge_url.replace(/^https?:\/\//, '').replace(/\/$/, '') : ''
  const seconds = data ? readAge(data.age_s, updatedAt, now) : null
  const age = seconds == null ? '' : seconds < 10 ? 'updated just now' : 'updated ' + ago(seconds)
  return el('div', { style: { ...S.row(10), justifyContent: 'space-between' } },
    el('div', { style: S.row(10) },
      el(Mark, { size: 22 }),
      el('div', { style: S.col(2) },
        el('div', { className: 'text-base font-semibold', style: { color: T.primary } }, 'Forgebrücke'),
        el('div', { style: S.small }, [forge, age].filter(Boolean).join(' · ')))),
    el(Button, { disabled: refreshing, onClick: onRefresh, size: 'sm', variant: 'secondary' }, el(Codicon, { name: 'refresh', size: '0.75rem', spinning: refreshing }), 'Refresh'))
}

function Loading({ text }) {
  return el('div', { style: S.col(10), 'data-test': 'loading' },
    el('div', { style: S.small }, text),
    el(Skeleton, { style: { height: 72 } }), el(Skeleton, { style: { height: 72 } }), el(Skeleton, { style: { height: 72 } }))
}

function Problem({ title, text, action, onAction }) {
  return el('div', { 'data-test': 'error', style: { paddingTop: 24 } },
    el(ErrorState, { title, description: text },
      el('div', { style: { display: 'flex', justifyContent: 'center' } }, el(Button, { onClick: onAction, size: 'sm' }, action))))
}

// A route that answered ok: false. The backend says whether a setting or another try helps.
function Refused({ data, title, onRetry }) {
  if (data.action === 'settings') {
    return el(Problem, { action: 'Open settings', onAction: () => host.navigate(data.settings_href || SETTINGS_HREF), text: data.message, title: data.title || title })
  }
  return el(Problem, { action: 'Try again', onAction: onRetry, text: data.message, title: data.title || title })
}

const tabLabel = (name, count) => (count > 0 ? name + ' · ' + count : name)

function DeskBody() {
  const desk = useDesk()
  const client = useQueryClient()
  const profile = useProfile()
  const [tab, setTab] = useState('waiting')
  const [repo, setRepo] = useState(ALL_REPOS)
  const [merging, setMerging] = useState(null)
  const [parking, setParking] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const data = desk.data
  const refresh = async () => {
    setRefreshing(true)
    try {
      client.setQueryData(qk(profile, 'desk'), await rest('/desk?refresh=true'))
    } catch (error) {
      host.notify({ kind: 'error', message: 'Refresh failed: ' + error.message })
    } finally {
      setRefreshing(false)
    }
  }
  useEffect(() => {
    if (data) showCount(data)
  }, [data])
  let body
  if (desk.isLoading) body = el(Loading, { text: 'Reading your pull requests.' })
  else if (desk.isError && notHere(desk.error)) body = el(Problem, { action: 'Open plugins', onAction: () => host.navigate('/settings?tab=plugins'), text: 'Switch it on under Settings, Plugins. Or open a chat of a profile that has it.', title: 'Forgebrücke is not on in this profile' })
  else if (desk.isError) body = el(Problem, { action: 'Try again', onAction: () => desk.refetch(), text: 'Hermes did not answer: ' + desk.error.message, title: 'The desk could not load' })
  else if (data && data.ok === false) body = el(Refused, { data, onRetry: refresh, title: 'The desk could not load' })
  else {
    const counts = data.counts || {}
    const repos = data.repos || []
    body = el('div', { style: S.col(12) },
      el(SegmentedControl, {
        onChange: setTab,
        options: [
          { id: 'waiting', label: tabLabel('Waiting for me', counts.waiting) },
          { id: 'running', label: tabLabel('Running', counts.running) },
          { id: 'blocked', label: tabLabel('Blocked', counts.blocked) },
          { id: 'repos', label: tabLabel('Repos', repos.length) }
        ],
        sizing: 'content',
        value: tab
      }),
      el(VersionNote, { version: data.version }),
      data.forge_error ? el('div', { style: { ...S.small, color: T.yellow }, 'data-test': 'forge-error' }, data.forge_error) : null,
      el(Notes, { notes: data.notes }),
      tab === 'repos' ? el(Repos, { error: data.repos_error, forgeUrl: data.forge_url, onMerge: setMerging, onRetry: refresh, repos, rows: data.rows || [], setRepo, setTab }) : el(RowList, { blocked: counts.blocked > 0, canComment: Boolean(data.can_comment), canMerge: Boolean(data.can_merge), note: data.waiting_note, onMerge: setMerging, onPark: setParking, repo, rows: data.rows || [], setRepo, setTab, tab, you: data.you }),
      merging ? el(MergeDialog, { onClose: () => setMerging(null), row: merging }) : null,
      parking ? el(ParkDialog, { canComment: Boolean(data.can_comment), onClose: () => setParking(null), row: parking, you: data.you }) : null)
  }
  return el('div', { style: { ...S.col(16), padding: 20 } },
    el(Header, { data: data && data.ok !== false ? data : null, onRefresh: refresh, refreshing, updatedAt: desk.dataUpdatedAt }), body)
}

// A render error anywhere below shows a sentence and a button, never a white page.
class Guard extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }
  static getDerivedStateFromError(error) {
    return { error }
  }
  componentDidCatch(error) {
    log('render failed', String(error && error.message))
  }
  render() {
    if (this.state.error) {
      return el('div', { style: { padding: 20 } },
        el(Problem, { action: 'Reload', onAction: () => this.setState({ error: null }), text: 'This view could not be drawn (' + String(this.state.error.message || this.state.error) + '). Nothing changed in Forgejo.', title: 'The page hit an error' }))
    }
    return this.props.children
  }
}

function DeskPage() {
  return el(Guard, {}, el(DeskBody, {}))
}

// ── settings page ──────────────────────────────────────────────────────────────────────────────
// One next action, then the setup rows (a finished one is a single line), the switches the page saves
// itself, and what works. The two token fields are Hermes' own form (Open settings): only that form
// writes a secret to the profile's .env.

// Desktop loads this file from the computer it runs on, never from a remote Hermes, so an update on the
// server leaves this half behind. host.state.connectionId is 'local' for this computer's own Hermes
// (Desktop's LOCAL_CONNECTION_ID, never given to a remote source); any other value, null or a Desktop
// without the field shows the line, because a missed update costs more than one line.
const NO_CONNECTION = atom(null)
const TWO_PLACE_TIP = 'No Hermes on this computer: Capabilities → Plugins → Install from Git with this plugin\'s repository, Force reinstall.'

function TwoPlaceNote() {
  const id = useValue((host.state && host.state.connectionId) || NO_CONNECTION)
  if (id === 'local') return null
  return el('div', { 'data-test': 'two-place', title: TWO_PLACE_TIP, style: { ...S.row(6), fontSize: '0.75rem', color: T.tertiary } },
    el(Codicon, { name: 'info', size: '0.75rem' }),
    el('span', null, 'Hermes on another computer? Update this plugin on both. Here: ',
      el('code', null, 'hermes plugins update ' + ID), ', then Rescan.'))
}

const BADGE = { setup: ['warn', 'setup needed'], preview: ['outline', 'preview only'], ready: ['success', 'ready'], unchecked: ['warn', 'not checked'] }
const STEP_ICON = { done: ['pass-filled', T.green], todo: ['circle-large-outline', T.accent], problem: ['error', T.red], unchecked: ['warning', T.yellow], optional: ['circle-large-outline', T.tertiary] }
const WRITE_CONFIRM = 'Hermes will send comments, pull requests and checks. Each follows Hermes’ approval setting; flows and review requests run on their own.'
const SWITCHES = [
  ['review_every_pr', 'Ask me to review every pull request', 'Also for pull requests opened by others.'],
  ['learn_mode', 'Learn mode', 'Short Git lessons at your first milestones.'],
  ['audit_enabled', 'Security audit', 'Read-only check of branch protection and keys.']
]
// His words for rules 2 and 3 (0.0.19 review). Rule 2's warning is the write token's risk sentence too
// (capabilities.OWNER_RISK); tests/test_v0021_copy.py holds the two equal.
const RULES = [
  ['person', 'Hermes has its own account', ['Everything it writes is signed by that account, never by you.']],
  ['eye', 'Forgejo decides what Hermes may do', ['Per repo you give Hermes’ account Read or Write in Forgejo.', 'That right is the only permission layer.',
    'Warning though: If you set the personal write token below,', 'it is also exposed to Hermes, ultimately giving Hermes full access to your account.']],
  ['pass', 'You approve the important part', ['Hermes works on its own feature branches.', 'Nothing gets to main unless you approve.',
    'For additional security, set up a branch protection for main and whitelist only yourself for push and merge.']],
  ['key', 'Tokens stay narrow', ['Tokens live on the Hermes host, where any agent shell can read them.', 'Mint each with the fewest scopes.']]
]

function Scopes({ list }) {
  return el('span', { style: { display: 'inline-flex', flexWrap: 'wrap', gap: 4, verticalAlign: 'middle' } },
    ...list.map(sc => el('span', { key: sc, style: { ...S.mono, padding: '1px 5px', borderRadius: 4, border: '1px solid ' + T.strokeSoft, color: T.primary } }, sc)))
}

function Steps({ lines }) {
  return el('ol', { style: { ...S.text, margin: 0, paddingLeft: 18, listStyle: 'decimal', ...S.col(3) } }, ...lines.map((line, i) => el('li', { key: i }, ...[].concat(line))))
}

// The backend names the kind of button a next step or a setup row gets; this draws it.
function Act({ kind, s, login, onWrite, onRetry }) {
  if (kind === 'who') return el(WhoAreYou, { login })
  if (kind === 'write') return el(Button, { onClick: onWrite, size: 'xs', 'data-test': 'let-write' }, 'Let Hermes write')
  if (kind === 'retry') return el(Button, { onClick: onRetry, size: 'xs', 'data-test': 'retry' }, 'Try again')
  if (kind === 'forgejo') return el(Button, { onClick: () => openExternal(s.forge_url), size: 'xs', 'data-test': 'open-forgejo' }, 'Open Forgejo')
  if (kind === 'settings') return el(Button, { onClick: openFields, size: 'xs', 'data-test': 'enter' }, 'Open settings')
  return null
}

// The one thing to do next, with its button. set_you saves on the click; dry_run asks once.
function Next({ s, onWrite, onRetry }) {
  const action = s.next_action
  if (!action || !action.id || action.id === 'none') return null
  return el('div', { style: { ...S.card, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12, borderColor: T.stroke }, 'data-test': 'next', 'data-action': action.id },
    el('div', { style: { ...S.line(8), alignItems: 'flex-start' } },
      el(Codicon, { name: 'arrow-right', size: '0.85rem', style: { color: T.accent, marginTop: 2 } }),
      el('span', { style: { fontSize: '0.8125rem', color: T.primary } }, action.text)),
    el('div', { style: { flex: 'none' } }, el(Act, { kind: action.button, login: action.login, onRetry, onWrite, s })))
}

const ROW_TITLE = { url: 'Forgejo address', bot: 'Hermes’ account token', you: 'Your Forgejo login', owner: 'Your write token (optional)' }

function SetupRow({ row, open, onToggle, children }) {
  const [icon, color] = STEP_ICON[row.state] || STEP_ICON.todo
  return el('div', { style: { borderBottom: '1px solid ' + T.strokeSoft }, 'data-test': 'step', 'data-step': row.id, 'data-state': row.state, 'data-open': open ? 'true' : 'false' },
    el('button', { 'aria-expanded': open, className: 'fb-press', onClick: onToggle, style: { ...S.line(8), width: '100%', padding: '8px 6px' }, type: 'button' },
      el(Codicon, { name: icon, size: '0.9rem', style: { color } }),
      el('span', { style: { fontSize: '0.8125rem', fontWeight: 600, color: T.primary, flex: 'none', minWidth: '10.5rem' } }, ROW_TITLE[row.id]),
      row.value ? el('span', { style: { ...S.clip, fontSize: '0.75rem', color: row.state === 'problem' ? T.red : row.state === 'unchecked' ? T.yellow : T.secondary } }, row.value) : null,
      el('span', { style: { marginLeft: 'auto', display: 'flex', color: T.tertiary } }, el(DisclosureCaret, { open }))),
    open ? el('div', { style: { ...S.col(8), padding: '2px 6px 12px 30px' } }, ...children) : null)
}

// The rows, their states and their sentences are the backend's (settings.setup). Only the first open row
// shows its instructions; a click opens any other.
function Setup({ s, onRetry }) {
  const [picked, setPicked] = useState(undefined)
  const rows = s.setup || []
  const [bot, owner] = ['bot', 'owner'].map(role => (s.tokens || []).find(t => t.role === role) || { role, mint: [] })
  const firstOpen = (rows.find(r => r.state === 'todo' || r.state === 'problem') || {}).id || null
  const open = picked === undefined ? firstOpen : picked
  const renamed = t => (t.renamed_from && !t.set ? el('div', { style: { ...S.text, color: T.yellow } }, 'A token is stored under an old name. Enter it again in Hermes’ settings.') : null)
  const help = {
    url: () => [el('div', { style: S.text }, 'The address you open Forgejo at, for example https://forgejo.example.com.')],
    bot: () => [
      el(Steps, { lines: [
        'Create a Forgejo user for Hermes, for example hermes.',
        'Add it to each repo under Settings, Collaborators, with Read or Write.',
        ['Signed in as that user, create a token for all repositories with ', el(Scopes, { key: 's', list: bot.mint || [] })]
      ] }),
      renamed(bot)
    ],
    you: () => [el('div', { style: S.text }, 'Your own Forgejo user name. The desk lists what waits for your review.')],
    owner: () => [
      el('div', { style: S.text }, owner.unlocks || 'Adds a Merge button to the desk.'),
      el('div', { style: S.text }, 'Signed in as yourself, create a token with ', el(Scopes, { list: owner.mint || [] })),
      owner.risk ? el('div', { style: { ...S.line(6), ...S.text, color: T.yellow, alignItems: 'flex-start' } }, el(Codicon, { name: 'warning', size: '0.8rem', style: { marginTop: 2 } }), owner.risk) : null,
      renamed(owner)
    ]
  }
  return el('section', { style: S.col(6), 'data-test': 'setup' },
    el(Heading, {}, 'Setup'),
    el('div', { style: { borderTop: '1px solid ' + T.strokeSoft } },
      ...rows.map(r => el(SetupRow, { key: r.id, row: r, open: open === r.id, onToggle: () => setPicked(open === r.id ? null : r.id) },
        ...(help[r.id] ? help[r.id]() : []),
        r.problem ? el('div', { style: { ...S.text, color: T.red }, 'data-test': 'problem' }, r.problem) : null,
        r.button ? el('div', {}, el(Act, { kind: r.button, login: s.you_suggestion, onRetry, s })) : null))),
    ...(s.token_warnings || []).map(w => el('div', { key: w, style: { ...S.line(6), ...S.text, color: T.yellow, alignItems: 'flex-start' }, 'data-test': 'token-warning' },
      el(Codicon, { name: 'warning', size: '0.8rem', style: { marginTop: 2 } }), w)))
}

function Switches({ s, onWrite }) {
  const save = useSaveSetting()
  const on = s.switches || {}
  return el('section', { style: S.col(2), 'data-test': 'switches' },
    el(Heading, {}, 'Switches'),
    el('div', { 'data-test': 'switch', 'data-key': 'dry_run' },
      el(ToggleRow, { checked: on.dry_run === false, description: 'When off, Hermes only previews each write.', label: 'Let Hermes write', onChange: value => (value ? onWrite() : save('dry_run', true)) })),
    ...SWITCHES.map(([key, label, description]) => el('div', { key, 'data-test': 'switch', 'data-key': key },
      el(ToggleRow, { checked: Boolean(on[key]), description, label, onChange: value => save(key, value) }))))
}

// Working features are names only. An off one says how to switch it on; the rest sit behind one line.
function Background({ b }) {
  if (!b || !b.text) return null
  return el('div', { style: { ...S.line(6), ...S.text, color: T.yellow, alignItems: 'flex-start', flexWrap: 'wrap' }, 'data-test': 'background' },
    el(Codicon, { name: 'warning', size: '0.8rem', style: { marginTop: 2 } }), b.text + ' ' + (b.fix || ''))
}

function Features({ features, background }) {
  const [open, setOpen] = useState(false)
  const by = status => features.filter(f => f.status === status)
  const [works, off, dark] = [by('available'), by('off'), by('dark')]
  const line = f => el('div', { key: f.id, style: { ...S.text, display: 'flex', alignItems: 'baseline', gap: 8 }, 'data-test': 'feature', 'data-status': f.status },
    el(Badge, { variant: 'muted' }, f.label), el('span', { style: { minWidth: 0 } }, f.reason))
  return el('section', { style: S.col(8), 'data-test': 'features' },
    el(Heading, {}, 'What works'),
    el(Background, { b: background }),
    works.length
      ? el('div', { style: S.row(4), 'data-test': 'works' }, ...works.map(f => el(Badge, { key: f.id, variant: 'success', 'data-test': 'feature', 'data-status': f.status }, f.label)))
      : el('div', { style: S.text }, 'Nothing yet.'),
    ...off.map(line),
    dark.length
      ? el('button', { 'aria-expanded': open, className: 'fb-press', onClick: () => setOpen(!open), style: { ...S.line(6), ...S.text, padding: '2px 6px 2px 0', alignSelf: 'flex-start' }, type: 'button', 'data-test': 'dark-toggle' },
        el(DisclosureCaret, { open, size: '0.625rem' }), 'Not available (' + dark.length + ')')
      : null,
    ...(open ? dark.map(line) : []))
}

function Node({ icon, title, caption }) {
  return el('div', { style: { ...S.col(3), alignItems: 'center', textAlign: 'center', padding: '10px 8px', borderRadius: 8, border: '1px solid ' + T.stroke, minWidth: 0 } },
    el(Codicon, { name: icon, size: '1.2rem', style: { color: T.primary } }),
    el('div', { style: { fontSize: '0.8125rem', fontWeight: 600, color: T.primary } }, title),
    el('div', { style: { ...S.small, ...S.clip, maxWidth: '100%' }, title: caption }, caption))
}

function Arrow({ label, toward }) {
  const head = el(Codicon, { name: toward === 'right' ? 'arrow-right' : 'arrow-left', size: '0.9rem', style: { color: T.secondary } })
  const shaft = el('div', { style: { flex: 1, height: 1, background: T.stroke } })
  return el('div', { style: { ...S.col(4), justifyContent: 'center', minWidth: 0, padding: '0 6px' } },
    el('div', { style: { ...S.small, color: T.primary, textAlign: 'center', whiteSpace: 'nowrap' } }, label),
    el('div', { style: { display: 'flex', alignItems: 'center', gap: 2 } }, toward === 'right' ? shaft : head, toward === 'right' ? head : shaft))
}

// With Security audit on, the backend lists repos whose default branch has no protection rule (rule 3).
function Unprotected({ list }) {
  if (!list.length) return null
  return el('div', { style: S.col(2), 'data-test': 'unprotected' },
    ...list.map(u => el('div', { key: u.repo, style: { ...S.line(6), ...S.text, color: T.yellow, alignItems: 'flex-start' } },
      el(Codicon, { name: 'warning', size: '0.8rem', style: { marginTop: 2 } }),
      el('span', { style: { minWidth: 0 } }, u.branch + ' is not protected in ', el('span', { style: { whiteSpace: 'nowrap' } }, u.repo + '.'), ' ',
        el('button', { className: 'fb-link', onClick: () => openExternal(u.url), style: { color: T.accent, whiteSpace: 'nowrap' }, type: 'button' }, 'Branch settings')))))
}

function HowItWorks({ s, startOpen }) {
  const [open, setOpen] = useState(startOpen)
  const bot = (s.tokens || []).find(t => t.role === 'bot')
  const forge = s.forge_url ? s.forge_url.replace(/^https?:\/\//, '') : 'your Forgejo'
  return el('section', { style: S.col(10), 'data-test': 'how', 'data-open': open ? 'true' : 'false' },
    el('button', { 'aria-expanded': open, className: 'fb-press', onClick: () => setOpen(!open), style: { ...S.line(6), padding: '2px 6px 2px 0', alignSelf: 'flex-start' }, type: 'button' },
      el(DisclosureCaret, { open, size: '0.625rem' }), el(Heading, {}, 'How it works')),
    open
      ? el('div', { style: { display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 5.5rem minmax(0,1.5fr) 5.5rem minmax(0,1fr)', alignItems: 'stretch' } },
        el(Node, { icon: 'account', title: 'You', caption: 'review, merge' }),
        el(Arrow, { label: 'you merge', toward: 'right' }),
        el(Node, { icon: 'server', title: 'Forgejo', caption: forge }),
        el(Arrow, { label: 'Hermes writes', toward: 'left' }),
        el(Node, { icon: 'hubot', title: 'Hermes', caption: bot && bot.login ? '@' + bot.login : 'own account' }))
      : null,
    open
      ? el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(16rem, 1fr))', gap: '12px 18px' }, 'data-test': 'rules' },
        ...RULES.map(([icon, label, long]) => el('div', { key: icon, style: S.col(3), 'data-test': 'rule', 'data-rule': icon },
          el('span', { style: { ...S.line(6), fontSize: '0.75rem', fontWeight: 600, color: T.primary } }, el(Codicon, { name: icon, size: '0.85rem', style: { color: T.accent } }), label),
          el('span', { style: S.text }, long.join(' ')),
          icon === 'pass' ? el(Unprotected, { list: s.unprotected || [] }) : null)))
      : null)
}

// The copy takes the profile's .env with every key, so the button only opens a confirm that says so (the
// backend's sentence); the route refuses without `confirm: true`.
function AgentReviews({ s }) {
  const [answer, setAnswer] = useState(null)
  const [asking, setAsking] = useState(false)
  const create = async () => {
    try {
      setAnswer(await rest('/reviewer-profile', { method: 'POST', body: { confirm: true } }))
    } catch (error) {
      setAnswer({ ok: false, message: error.message })
    }
  }
  return el('section', { style: S.col(6), 'data-test': 'reviews' },
    el('div', { style: { ...S.row(8), ...S.text } },
      el('span', {}, 'Agent reviews run in profile ', el('span', { style: { ...S.mono, color: T.primary } }, s.default_review_profile || 'this one'), '.'),
      s.reviewer && !s.reviewer.exists ? el(Button, { onClick: () => setAsking(true), size: 'xs', variant: 'secondary' }, 'Create a reviewer profile') : null),
    asking ? el(ConfirmDialog, {
      confirmLabel: 'Copy the profile',
      busyLabel: 'Copying',
      description: s.reviewer.copies,
      dismissOnConfirm: true,
      onClose: () => setAsking(false),
      onConfirm: create,
      open: true,
      title: 'Create ' + s.reviewer.name + '?'
    }) : null,
    answer ? el('div', { style: { ...S.text, color: answer.ok ? T.green : T.yellow } }, answer.message, answer.command ? el('div', { style: { ...S.mono, color: T.primary, whiteSpace: 'pre-wrap' }, 'data-test': 'reviewer-command' }, answer.command) : null) : null)
}

// A token saved after this profile's plugin host started is unseen by the page's routes (the backend reads
// the .env's time against its own start and the last reload). One click reads Forgebrücke's tokens again.
function Reload({ reload, onDone }) {
  const [busy, setBusy] = useState(false)
  if (!reload || !reload.needed) return null
  const run = async () => {
    setBusy(true)
    try {
      const out = await rest('/reload-tokens', { method: 'POST', body: {} })
      host.notify({ kind: 'success', message: (out && out.message) || 'Tokens loaded.' })
      await onDone()
    } catch (error) {
      host.notify({ kind: 'error', message: 'Not reloaded: ' + error.message })
    } finally {
      setBusy(false)
    }
  }
  return el('div', { style: { ...S.card, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 }, 'data-test': 'reload' },
    el('div', { style: S.line(8) }, el(Codicon, { name: 'key', size: '0.85rem', style: { color: T.yellow } }), el('span', { style: { fontSize: '0.8125rem', color: T.primary } }, reload.text)),
    el(Button, { disabled: busy, onClick: run, size: 'xs', 'data-test': 'reload-tokens' }, 'Load it now'))
}

function SettingsBody() {
  const q = useSettings()
  const save = useSaveSetting()
  const client = useQueryClient()
  const profile = useProfile()
  const [confirming, setConfirming] = useState(false)
  const retry = async () => {
    try {
      client.setQueryData(qk(profile, 'settings'), await rest('/settings?refresh=true'))
    } catch (error) {
      host.notify({ kind: 'error', message: 'Refresh failed: ' + error.message })
    }
  }
  if (q.isLoading) return el('div', { style: { padding: 20 } }, el(Loading, { text: 'Reading your setup.' }))
  if (q.isError) return el(Problem, { action: 'Try again', onAction: () => q.refetch(), text: q.error.message, title: 'The settings could not load' })
  const s = q.data || {}
  if (s.ok === false) return el(Refused, { data: s, onRetry: retry, title: 'The settings could not load' })
  const [variant, word] = BADGE[s.state] || BADGE.setup
  const flows = (s.features || []).find(f => f.id === 'flows')
  const askToWrite = () => setConfirming(true)
  return el('div', { style: { ...S.col(22), padding: 20 } },
    el('div', { style: { ...S.row(10), justifyContent: 'space-between' } },
      el('div', { style: S.row(10) }, el(Mark, { size: 22 }),
        el('div', { className: 'text-base font-semibold', style: { color: T.primary } }, 'Forgebrücke'),
        el(Badge, { variant, 'data-test': 'ready' }, word)),
      el(Button, { onClick: openFields, size: 'xs', variant: 'secondary' }, el(Codicon, { name: 'settings-gear', size: '0.75rem' }), 'Open settings')),
    el(VersionNote, { version: s.version }),
    el(Reload, { reload: s.reload, onDone: retry }),
    el(Next, { s, onRetry: retry, onWrite: askToWrite }),
    el(Setup, { s, onRetry: retry }),
    el(Switches, { s, onWrite: askToWrite }),
    el(Features, { background: s.background, features: s.features || [] }),
    el(HowItWorks, { s, startOpen: s.state === 'setup' || Boolean(s.unprotected && s.unprotected.length) }),
    flows && flows.status === 'available' ? el(AgentReviews, { s }) : null,
    el(TwoPlaceNote, {}),
    confirming
      ? el(ConfirmDialog, { confirmLabel: 'Let Hermes write', description: WRITE_CONFIRM, onClose: () => setConfirming(false), onConfirm: () => save('dry_run', false, true), open: true, title: 'Let Hermes write?' })
      : null)
}

// The Sidebar section sits under the body and outside its guard: it needs no backend, so it shows while the
// setup loads or fails too.
function SettingsPage() {
  return el('div', null,
    el(Guard, {}, el(SettingsBody, {})),
    sidebarRow ? el(Guard, {}, el('div', { style: { padding: '0 20px 20px' }, 'data-test': 'sidebar' }, jsx(SidebarSection, { row: sidebarRow, sdk: SIDEBAR_SDK }))) : null)
}

// ── registration ─────────────────────────────────────────────────────────────────────────────────

// A poll is due when Desktop is visible and NAV_POLL_MS or the backend's own poll interval (`poll_s` on /desk),
// whichever is longer, has passed since the last one. A hidden window reads nothing; showing it catches up.
export function navPollDue(lastMs, nowMs, hidden, pollS) {
  if (hidden) return false
  return nowMs - lastMs >= Math.max(NAV_POLL_MS, (Number(pollS) || 0) * 1000)
}

let navLast = 0
let navPollS = null

async function pollNav() {
  const hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden'
  if (!navPollDue(navLast, Date.now(), hidden, navPollS)) return
  navLast = Date.now()
  try {
    const desk = await rest('/desk')
    navPollS = desk && desk.poll_s ? desk.poll_s : navPollS
    showCount(desk)
  } catch {
    /* the row keeps its last label; the page shows the error */
  }
}

function addSheet(ctx) {
  if (typeof document === 'undefined') return
  const node = document.createElement('style')
  node.setAttribute('data-plugin', ID)
  node.textContent = SHEET
  document.head.appendChild(node)
  if (typeof ctx.onDispose === 'function') ctx.onDispose(() => node.remove())
}

export default {
  id: ID,
  name: 'Forgebrücke',
  description: 'Your Forgejo pull requests and what waits for you.',
  register(ctx) {
    pluginCtx = ctx
    addSheet(ctx)
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(DeskPage, {}) })
    sidebarRow = startSidebarRow(ctx, { brand: 'Forgebrücke', path: PATH, codicon: 'repo-forked', defaultPosition: 55, sdk: SIDEBAR_SDK })
    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: { id: 'forgebruecke.open', label: 'Open Forgebrücke', keywords: ['forgejo', 'desk', 'merge', 'pull request', 'review'], run: () => host.navigate(PATH) }
    })
    if (typeof ctx.registerSettingsPage === 'function') {
      ctx.registerSettingsPage({ id: 'setup', title: 'Forgebrücke', icon: 'repo-forked', order: 50, render: () => jsx(SettingsPage, {}) })
    }
    pollNav()
    if (typeof ctx.setInterval === 'function') ctx.setInterval(pollNav, NAV_POLL_MS / 4)
    if (typeof ctx.addEventListener === 'function' && typeof document !== 'undefined') {
      ctx.addEventListener(document, 'visibilitychange', pollNav)
    }
    log('registered', { path: PATH })
  }
}
