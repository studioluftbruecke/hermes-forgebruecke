// Forgebrücke: the Desktop half. A sidebar row "Forgejo · N" (N = pull requests waiting for you), a desk
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
  PALETTE_AREA,
  ROUTES_AREA,
  SearchField,
  SegmentedControl,
  SIDEBAR_NAV_AREA,
  Skeleton,
  Tip,
  ToggleRow,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { Component, useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'forgebruecke'
const PATH = '/forgebruecke'
const TAG = '[' + ID + ']'
const NAV_POLL_MS = 60000
const DESK_POLL_MS = 30000
const SETTINGS_HREF = '/settings?tab=plugins&agent=forgebruecke'
const CHAT_TITLE = 'Forgejo'
const FILTER_ABOVE = 8

let pluginCtx = null
let navLabel = null

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
  '.fb-repo:hover{background:' + T.fill + '}'
].join('\n')

// The label rule is the backend's (desk.nav_label); this copy only runs before the first answer.
export function formatNavLabel(waiting) {
  const n = Number(waiting) || 0
  if (n <= 0) return 'Forgejo'
  return 'Forgejo · ' + (n < 100 ? String(n) : '99+')
}

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

const MARK =
  'M32 50 C12 46 12 37 32 33 C52 29 52 20 32 16 M32 50 C52 46 52 37 32 33 C12 29 12 20 32 16 M32 16 L32 56 M27 56 L37 56'

function Mark({ size }) {
  return el(
    'svg',
    { 'aria-label': 'Forgebrücke', role: 'img', viewBox: '0 0 64 64', width: size || 20, height: size || 20, style: { color: T.primary, flex: 'none' } },
    el('path', { d: MARK, fill: 'none', stroke: 'currentColor', strokeWidth: 5 }),
    el('circle', { cx: 32, cy: 16, r: 6.5, fill: 'var(--ui-bg-editor, transparent)', stroke: 'currentColor', strokeWidth: 4 }),
    el('circle', { cx: 32, cy: 50, r: 6.5, fill: 'var(--ui-bg-editor, transparent)', stroke: 'currentColor', strokeWidth: 4 })
  )
}

function openExternal(href) {
  if (!href) return
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

function AskHermes({ row }) {
  const ask = sentence => openChat('@forgejo:' + row.repo + '#' + row.number + ' ' + sentence)
  return el(DropdownMenu, {},
    el(DropdownMenuTrigger, { asChild: true },
      el(Button, { size: 'xs', variant: 'secondary', 'data-test': 'ask' }, el(Codicon, { name: 'comment-discussion', size: '0.75rem' }), 'Ask Hermes', el(Codicon, { name: 'chevron-down', size: '0.625rem' }))),
    el(DropdownMenuContent, { align: 'end' },
      ...QUESTIONS.filter(([, when]) => when(row)).map(([sentence]) => el(DropdownMenuItem, { key: sentence, onSelect: () => ask(sentence) }, sentence))))
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

function Row({ row, onMerge }) {
  const tone = TONE[(row.summary && row.summary.tone) || 'wait'] || TONE.wait
  const meta = row.repo + ' #' + row.number + (row.author ? ' by ' + row.author : '') + (row.age_s != null ? ' · ' + ago(row.age_s) : '')
  const comment = row.last_comment
  return el('div', { style: S.card, 'data-test': 'row', 'data-tab': row.tab },
    el('div', { style: { display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 } },
      el('div', { style: { ...S.col(2), minWidth: 0 } },
        el('button', { className: 'fb-link text-sm font-medium', onClick: () => openExternal(row.url), style: { color: T.primary }, title: 'Open in Forgejo', type: 'button' }, row.title),
        el('div', { style: S.small, title: row.head_sha ? 'Commit ' + row.head_sha.slice(0, 12) : undefined }, meta)),
      el('div', { style: { ...S.line(6), flex: 'none' } },
        el(AskHermes, { row }),
        row.can_merge && row.next !== 'merge' ? el(Button, { onClick: () => onMerge(row), size: 'xs', variant: 'secondary', 'data-test': 'merge' }, el(Codicon, { name: 'git-merge', size: '0.75rem' }), MOVE.merge) : null,
        row.next === 'merge' ? el(Button, { onClick: () => onMerge(row), size: 'xs', 'data-test': 'merge' }, el(Codicon, { name: 'git-merge', size: '0.75rem' }), MOVE.merge)
          : el(Button, { disabled: !row.url, onClick: () => openExternal(row.url), size: 'xs', variant: row.next === 'open_in_forgejo' ? 'secondary' : 'default', 'data-test': 'open' }, el(Codicon, { name: 'link-external', size: '0.75rem' }), MOVE[row.next] || MOVE.open_in_forgejo))),
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
      : null)
}

const EMPTY = { waiting: 'Nothing waits for you.', running: 'Nothing is running.', blocked: 'Nothing is blocked.' }

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

function RowList({ rows, tab, setTab, onMerge, note, blocked }) {
  const mine = rows.filter(r => r.tab === tab)
  const top = tab === 'waiting' ? el(WaitingNote, { note }) : null
  if (!mine.length) {
    return el('div', { style: S.col(8), 'data-test': 'empty' },
      top,
      el(EmptyState, { title: EMPTY[tab] }),
      tab === 'waiting' && blocked ? el('div', { style: { display: 'flex', justifyContent: 'center' } }, el(Button, { onClick: () => setTab('blocked'), size: 'sm', variant: 'secondary' }, 'Show blocked')) : null)
  }
  return el('div', { style: S.col(10) }, top, ...mine.map(r => el(Row, { key: r.id, onMerge, row: r })))
}

// Every repo Hermes' account sees, one line each. A badge only for what differs from the usual
// (write access, no flow, mentions answered).
function Repos({ repos, error, forgeUrl, onRetry }) {
  const [query, setQuery] = useState('')
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
  return el('div', { style: S.col(8) },
    el('div', { style: { ...S.row(12), justifyContent: 'space-between' } },
      el('div', { style: S.small }, 'Hermes can do in a repo what its account may do in Forgejo.'),
      repos.length > FILTER_ABOVE ? el('div', { style: { width: '15rem', maxWidth: '100%' } }, el(SearchField, { onChange: setQuery, placeholder: 'Filter repos', value: query, variant: 'box' })) : null),
    shown.length
      ? el('div', { style: S.col(0) },
        ...shown.map(r => el('div', { key: r.repo, className: 'fb-repo', style: { ...S.line(8), padding: '5px 8px' }, 'data-test': 'repo', 'data-right': r.right },
          el('button', { className: 'fb-link', onClick: () => openExternal(r.url), style: { ...S.mono, ...S.clip, fontSize: '0.75rem', color: T.primary }, title: 'Open in Forgejo', type: 'button' }, r.repo),
          r.right === 'read' ? el(Badge, { variant: 'muted', 'data-test': 'right' }, 'read only') : null,
          ...(r.flows || []).map(f => el(Badge, { key: f, variant: 'success' }, 'flow ' + f)),
          !r.mentions && r.right !== 'read' ? el(Badge, { variant: 'muted' }, 'mentions off') : null,
          r.open_prs > 0 ? el('span', { style: S.small }, r.open_prs + (r.open_prs === 1 ? ' open pull request' : ' open pull requests')) : null,
          el('span', { style: { flex: 1 } }),
          el(Button, { onClick: () => openExternal(r.collaboration_url), size: 'xs', variant: 'ghost' }, 'Change access'))))
      : el('div', { style: { ...S.small, padding: '5px 8px' } }, 'No repo matches “' + query.trim() + '”.'))
}

function MergeDialog({ row, onClose }) {
  const client = useQueryClient()
  const profile = useProfile()
  if (!row) return null
  const run = async () => {
    const result = await rest('/merge', { method: 'POST', body: { repo: row.repo, number: row.number, head_sha: row.head_sha } })
    if (!result || !result.ok) throw new Error((result && result.message) || 'Not merged.')
    host.notify({ kind: 'success', message: result.message })
    await client.invalidateQueries({ queryKey: qk(profile, 'desk') })
  }
  return el(ConfirmDialog, {
    children: el('div', { style: S.col(4) },
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
        el('div', { className: 'text-base font-semibold', style: { color: T.primary } }, 'Forgejo desk'),
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
  const [merging, setMerging] = useState(null)
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
  if (data && data.label) setNavLabel(data.label)
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
      data.forge_error ? el('div', { style: { ...S.small, color: T.yellow }, 'data-test': 'forge-error' }, data.forge_error) : null,
      el(Notes, { notes: data.notes }),
      tab === 'repos' ? el(Repos, { error: data.repos_error, forgeUrl: data.forge_url, onRetry: refresh, repos }) : el(RowList, { blocked: counts.blocked > 0, note: data.waiting_note, onMerge: setMerging, rows: data.rows || [], setTab, tab }),
      merging ? el(MergeDialog, { onClose: () => setMerging(null), row: merging }) : null)
  }
  return el('div', { style: { ...S.col(16), padding: 20, maxWidth: 900 } },
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
const WRITE_CONFIRM = 'Hermes will send comments, pull requests and checks. You approve each one in the chat; flows and review requests run on their own.'
const SWITCHES = [
  ['review_every_pr', 'Ask me to review every pull request', 'Also for pull requests opened by others.'],
  ['learn_mode', 'Learn mode', 'Short Git lessons at your first milestones.'],
  ['audit_enabled', 'Security audit', 'Read-only check of branch protection and keys.']
]
const RULES = [
  ['person', 'Hermes has its own account', 'Everything it writes is signed by that account, never by you.'],
  ['eye', 'Forgejo decides its access', 'Per repo you give its account Read or Write in Forgejo. Nothing else grants access.'],
  ['pass', 'You approve every write', 'Each comment, pull request or check asks you first. Nothing approves or merges main.'],
  ['key', 'Tokens stay narrow', 'Tokens live on the Hermes host, where any agent shell can read them. Mint each with the fewest scopes.']
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
function Features({ features }) {
  const [open, setOpen] = useState(false)
  const by = status => features.filter(f => f.status === status)
  const [works, off, dark] = [by('available'), by('off'), by('dark')]
  const line = f => el('div', { key: f.id, style: { ...S.text, display: 'flex', alignItems: 'baseline', gap: 8 }, 'data-test': 'feature', 'data-status': f.status },
    el(Badge, { variant: 'muted' }, f.label), el('span', { style: { minWidth: 0 } }, f.reason))
  return el('section', { style: S.col(8), 'data-test': 'features' },
    el(Heading, {}, 'What works'),
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
      ? el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '6px 14px' }, 'data-test': 'rules' },
        ...RULES.map(([icon, label, long]) => el(Tip, { key: icon, label: long },
          el('span', { style: { ...S.line(6), fontSize: '0.75rem', color: T.primary }, title: long }, el(Codicon, { name: icon, size: '0.85rem', style: { color: T.accent } }), label))))
      : null)
}

function AgentReviews({ s }) {
  const [answer, setAnswer] = useState(null)
  const create = async () => {
    try {
      setAnswer(await rest('/reviewer-profile', { method: 'POST', body: {} }))
    } catch (error) {
      setAnswer({ ok: false, message: error.message })
    }
  }
  return el('section', { style: S.col(6), 'data-test': 'reviews' },
    el('div', { style: { ...S.row(8), ...S.text } },
      el('span', {}, 'Agent reviews run in profile ', el('span', { style: { ...S.mono, color: T.primary } }, s.default_review_profile || 'this one'), '.'),
      s.reviewer && !s.reviewer.exists ? el(Button, { onClick: create, size: 'xs', variant: 'secondary' }, 'Create a reviewer profile') : null),
    answer ? el('div', { style: { ...S.text, color: answer.ok ? T.green : T.yellow } }, answer.message, answer.command ? el('div', { style: { ...S.mono, color: T.primary } }, answer.command) : null) : null)
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
  if (q.isLoading) return el('div', { style: { padding: 20, maxWidth: 720 } }, el(Loading, { text: 'Reading your setup.' }))
  if (q.isError) return el(Problem, { action: 'Try again', onAction: () => q.refetch(), text: q.error.message, title: 'The settings could not load' })
  const s = q.data || {}
  if (s.ok === false) return el(Refused, { data: s, onRetry: retry, title: 'The settings could not load' })
  const [variant, word] = BADGE[s.state] || BADGE.setup
  const flows = (s.features || []).find(f => f.id === 'flows')
  const askToWrite = () => setConfirming(true)
  return el('div', { style: { ...S.col(22), padding: 20, maxWidth: 720 } },
    el('div', { style: { ...S.row(10), justifyContent: 'space-between' } },
      el('div', { style: S.row(10) }, el(Mark, { size: 22 }),
        el('div', { className: 'text-base font-semibold', style: { color: T.primary } }, 'Forgebrücke'),
        el(Badge, { variant, 'data-test': 'ready' }, word)),
      el(Button, { onClick: openFields, size: 'xs', variant: 'secondary' }, el(Codicon, { name: 'settings-gear', size: '0.75rem' }), 'Open settings')),
    el(Next, { s, onRetry: retry, onWrite: askToWrite }),
    el(Setup, { s, onRetry: retry }),
    el(Switches, { s, onWrite: askToWrite }),
    el(Features, { features: s.features || [] }),
    el(HowItWorks, { s, startOpen: s.state === 'setup' }),
    flows && flows.status === 'available' ? el(AgentReviews, { s }) : null,
    el(TwoPlaceNote, {}),
    confirming
      ? el(ConfirmDialog, { confirmLabel: 'Let Hermes write', description: WRITE_CONFIRM, onClose: () => setConfirming(false), onConfirm: () => save('dry_run', false, true), open: true, title: 'Let Hermes write?' })
      : null)
}

function SettingsPage() {
  return el(Guard, {}, el(SettingsBody, {}))
}

// ── registration ─────────────────────────────────────────────────────────────────────────────────

function setNavLabel(label) {
  if (!pluginCtx || !label || label === navLabel) return
  navLabel = label
  pluginCtx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, order: 56, data: { codicon: 'repo-forked', label, path: PATH } })
}

async function pollNav() {
  try {
    const desk = await rest('/desk')
    if (desk && desk.label) setNavLabel(desk.label)
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
    navLabel = null
    addSheet(ctx)
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(DeskPage, {}) })
    setNavLabel(formatNavLabel(0))
    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: { id: 'forgebruecke.open', label: 'Open Forgejo desk', keywords: ['forgejo', 'merge', 'pull request', 'review'], run: () => host.navigate(PATH) }
    })
    if (typeof ctx.registerSettingsPage === 'function') {
      ctx.registerSettingsPage({ id: 'setup', title: 'Forgebrücke', icon: 'repo-forked', order: 50, render: () => jsx(SettingsPage, {}) })
    }
    const every = typeof ctx.setInterval === 'function' ? ctx.setInterval : null
    pollNav()
    if (every) every(pollNav, NAV_POLL_MS)
    log('registered', { path: PATH })
  }
}
