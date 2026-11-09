// Forgebrücke: the Desktop half. A sidebar row "Forgejo · N" (N = pull requests waiting for you), a desk
// page (/forgebruecke) with four tabs (Waiting for me, Running, Blocked, Repos), a palette command and a
// settings page that opens with how Forgebrücke works, then the setup steps and what the setup can do.
//
// Everything the page shows comes from this plugin's own backend routes (dashboard/plugin_api.py) through
// ctx.rest, polled with useQuery; the page never calls Forgejo and holds no token. The merge button exists
// only when the backend says the write token of your account can merge; it confirms first, names the
// commit, and the route merges pinned to that commit. Every other row action is a link into Forgejo.
//
// Plain ESM, loaded uncompiled: jsx() calls, imports only @hermes/plugin-sdk, react and
// react/jsx-runtime, theme variables only, no eval.

import {
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  host,
  PALETTE_AREA,
  ROUTES_AREA,
  SegmentedControl,
  SIDEBAR_NAV_AREA,
  Skeleton,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { Component, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'forgebruecke'
const PATH = '/forgebruecke'
const TAG = '[' + ID + ']'
const NAV_POLL_MS = 60000
const DESK_POLL_MS = 30000
const SETTINGS_HREF = '/settings?tab=plugins&agent=forgebruecke'

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
  card: { border: '1px solid ' + T.strokeSoft, borderRadius: 8, padding: 12, display: 'flex', flexDirection: 'column', gap: 8 },
  small: { fontSize: '0.6875rem', color: T.secondary },
  mono: { fontFamily: 'var(--font-mono, ui-monospace, monospace)', fontSize: '0.6875rem' }
}

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

// Hermes answers 404 "Plugin not found" before any plugin code runs when the active profile has Forgebrücke
// off (seen on the Mac, 2026-11-09): say that, not a bare HTTP error.
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

// ── small pieces ─────────────────────────────────────────────────────────────────────────────────

const MARK =
  'M32 50 C12 46 12 37 32 33 C52 29 52 20 32 16 M32 50 C52 46 52 37 32 33 C12 29 12 20 32 16 M32 16 L32 56 M27 56 L37 56'

function Mark({ size }) {
  // The Forgebrücke mark (BRAND.md, mono variant: currentColor), reduced to its structure for 20 px.
  return el(
    'svg',
    { 'aria-label': 'Forgebrücke', role: 'img', viewBox: '0 0 64 64', width: size || 20, height: size || 20, style: { color: T.primary, flex: 'none' } },
    el('path', { d: MARK, fill: 'none', stroke: 'currentColor', strokeWidth: 5 }),
    el('circle', { cx: 32, cy: 16, r: 6.5, fill: 'var(--ui-bg-editor, transparent)', stroke: 'currentColor', strokeWidth: 4 }),
    el('circle', { cx: 32, cy: 50, r: 6.5, fill: 'var(--ui-bg-editor, transparent)', stroke: 'currentColor', strokeWidth: 4 })
  )
}

function openExternal(href) {
  const os = pluginCtx && pluginCtx.os
  Promise.resolve(os && typeof os.openExternal === 'function' ? os.openExternal(href) : false)
    .then(ok => {
      if (!ok) window.open(href, '_blank', 'noopener')
    })
    .catch(() => window.open(href, '_blank', 'noopener'))
}

function LinkButton({ href, children, icon }) {
  return el(Button, { disabled: !href, onClick: () => openExternal(href), size: 'xs', title: href, variant: 'outline' },
    el(Codicon, { name: icon || 'link-external', size: '0.75rem' }), children)
}

function ago(seconds) {
  if (seconds == null) return 'never'
  if (seconds < 60) return seconds + ' s ago'
  if (seconds < 3600) return Math.round(seconds / 60) + ' min ago'
  return Math.round(seconds / 3600) + ' h ago'
}

const SEGMENT = {
  done: { icon: 'pass-filled', color: T.green, word: 'done' },
  running: { icon: 'loading', color: T.accent, word: 'running', spin: true },
  pending: { icon: 'circle-large-outline', color: T.tertiary, word: 'not started' },
  waiting: { icon: 'person', color: T.yellow, word: 'waiting for you' },
  failed: { icon: 'error', color: T.red, word: 'failed' }
}

function GateChain({ gates }) {
  return el(
    'div',
    { style: S.row(4), 'data-test': 'gates' },
    ...gates.map((g, i) => {
      const seg = SEGMENT[g.segment] || SEGMENT.pending
      return el(
        'span',
        {
          key: g.key + i,
          'data-segment': g.segment,
          style: { ...S.row(4), border: '1px solid ' + T.strokeSoft, borderRadius: 4, padding: '2px 6px', fontSize: '0.6875rem', color: T.primary },
          title: (g.detail || seg.word)
        },
        el(Codicon, { name: seg.icon, size: '0.75rem', spinning: seg.spin, style: { color: seg.color } }),
        el('span', {}, g.key),
        el('span', { style: { color: T.secondary } }, seg.word)
      )
    })
  )
}

const CHECK_ICON = { success: ['pass', T.green], failure: ['error', T.red], error: ['error', T.red], pending: ['clock', T.yellow], warning: ['warning', T.yellow] }

function Checks({ checks }) {
  if (!checks) return el('div', { style: S.small }, 'Checks could not be read just now.')
  const list = checks.statuses || []
  if (!list.length) return el('div', { style: S.small }, 'No checks on this commit yet.')
  return el(
    'div',
    { style: S.col(2), 'data-test': 'checks' },
    ...list.map(s => {
      const [icon, color] = CHECK_ICON[s.state] || ['circle-large-outline', T.tertiary]
      return el(
        'div',
        { key: s.context, style: { ...S.row(6), fontSize: '0.6875rem' } },
        el(Codicon, { name: icon, size: '0.75rem', style: { color } }),
        el('span', { style: S.mono }, s.context),
        el('span', { style: { color: T.secondary } }, s.state + (s.creator ? ' by ' + s.creator : ''))
      )
    })
  )
}

// ── rows ─────────────────────────────────────────────────────────────────────────────────────────

function Row({ row, onMerge }) {
  const human = row.tab === 'waiting'
  return el(
    'div',
    { style: S.card, 'data-test': 'row', 'data-tab': row.tab },
    el(
      'div',
      { style: { ...S.row(8), justifyContent: 'space-between' } },
      el(
        'div',
        { style: S.col(2) },
        el('div', { className: 'text-sm font-medium', style: { color: T.primary } }, row.title),
        el('div', { style: S.small }, row.repo + ' #' + row.number + (row.author ? ' by ' + row.author : '') + (row.flow ? ' · flow ' + row.flow : ' · ' + (row.reason || 'no flow')) + ' · ',
          el('span', { style: S.mono, title: row.head_sha }, (row.head_sha || '').slice(0, 12)))
      ),
      el(
        'div',
        { style: S.row(6) },
        row.can_merge ? el(Button, { onClick: () => onMerge(row), size: 'xs', 'data-test': 'merge' }, el(Codicon, { name: 'git-merge', size: '0.75rem' }), 'Merge') : null,
        el(LinkButton, { href: row.url }, !human ? 'Open in Forgejo' : row.flow ? 'Open in Forgejo to merge' : 'Open in Forgejo to review')
      )
    ),
    row.gates && row.gates.length ? el(GateChain, { gates: row.gates }) : null,
    el(Checks, { checks: row.checks }),
    row.last_comment
      ? el('div', { style: { ...S.small, borderLeft: '2px solid ' + T.strokeSoft, paddingLeft: 8 } },
        el('span', { style: { color: T.primary } }, row.last_comment.author + ': '), row.last_comment.body)
      : null
  )
}

const EMPTY = {
  waiting: ['Nothing waits for you.', 'When a pull request requests your review, or passes every gate before you, it shows up here.', 'running', 'Show running'],
  running: ['Nothing is running.', 'A pull request in a repo with a flow appears here while its gates run.', 'repos', 'Show repos'],
  blocked: ['Nothing is blocked.', 'A pull request whose gate failed waits here until a new push.', 'waiting', 'Show waiting']
}

// Without the setting you, Waiting for me cannot know whose review requests to list: say so, with the
// login the backend suggests (the owner of the repos Hermes sees) and a way to the field.
function WaitingNote({ note }) {
  if (!note || !note.message) return null
  return el('div', { style: { ...S.card, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 12 }, 'data-test': 'waiting-note' },
    el('div', { style: { ...S.row(8), flexWrap: 'nowrap', alignItems: 'flex-start' } },
      el(Codicon, { name: 'info', size: '0.85rem', style: { color: T.accent, marginTop: 1 } }),
      el('div', { style: S.col(2) },
        el('div', { style: { fontSize: '0.75rem', color: T.primary } }, note.message),
        note.suggestion ? el('div', { style: S.small }, 'Probably ', el('span', { style: S.mono }, note.suggestion), ': the owner of the repos Hermes sees.') : null)),
    el(Button, { onClick: () => host.navigate(note.settings_href || SETTINGS_HREF), size: 'xs', variant: 'outline' }, 'Open the settings'))
}

function Notes({ notes }) {
  if (!notes || !notes.length) return null
  return el('div', { style: S.col(2), 'data-test': 'notes' }, ...notes.map(n => el('div', { key: n, style: { ...S.small, color: T.yellow } }, n)))
}

function RowList({ rows, tab, setTab, onMerge, note }) {
  const mine = rows.filter(r => r.tab === tab)
  const top = tab === 'waiting' ? el(WaitingNote, { note }) : null
  if (!mine.length) {
    const [title, description, next, label] = EMPTY[tab]
    return el('div', { style: S.col(8), 'data-test': 'empty' },
      top,
      el(EmptyState, { title, description }),
      el('div', { style: { display: 'flex', justifyContent: 'center' } }, el(Button, { onClick: () => setTab(next), size: 'sm', variant: 'outline' }, label)))
  }
  return el('div', { style: S.col(10) }, top, ...mine.map(r => el(Row, { key: r.id, onMerge, row: r })))
}

const RIGHT = { admin: ['default', 'Hermes: admin'], write: ['default', 'Hermes: write'], read: ['muted', 'Hermes: read only'] }

// Every repo Hermes' Forgejo account sees. Its right there is the only permission layer (D27): this tab
// shows it and links to the page where the owner changes it.
function Repos({ repos }) {
  if (!repos.length) {
    return el('div', { style: S.col(8), 'data-test': 'empty' },
      el(EmptyState, { title: 'Hermes sees no repos yet.', description: 'In Forgejo, add Hermes’ account to a repo under Settings → Collaborators, with Read or Write. It shows up here.' }),
      el('div', { style: { display: 'flex', justifyContent: 'center' } }, el(Button, { onClick: () => host.navigate(SETTINGS_HREF), size: 'sm', variant: 'outline' }, 'Open the settings')))
  }
  return el('div', { style: S.col(10) },
    el('div', { style: S.small }, 'What Hermes may do in each repo is its account’s right in Forgejo. Change it on the repo’s collaborator page.'),
    el(
      'div',
      { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(16rem, 1fr))', gap: 10 } },
      ...repos.map(r => {
        const [variant, word] = RIGHT[r.right] || ['muted', r.right]
        return el(
          'div',
          { key: r.repo, style: S.card, 'data-test': 'repo', 'data-right': r.right },
          el('div', { className: 'text-sm font-medium', style: { ...S.mono, fontSize: '0.75rem', color: T.primary } }, r.repo),
          el('div', { style: S.row(4) },
            el(Badge, { variant, 'data-test': 'right' }, word),
            el(Badge, { variant: r.flows && r.flows.length ? 'success' : 'muted' }, r.flows && r.flows.length ? 'flows: ' + r.flows.join(', ') : 'no flow'),
            el(Badge, { variant: r.mentions ? 'outline' : 'muted' }, r.mentions ? 'answers mentions' : 'no mentions')),
          el('div', { style: S.small }, r.open_rows + ' open pull request' + (r.open_rows === 1 ? '' : 's') + ' with a flow'),
          el('div', { style: S.row(6) },
            el(LinkButton, { href: r.url }, 'Open in Forgejo'),
            el(LinkButton, { href: r.collaboration_url, icon: 'person-add' }, 'Change access')))
      })
    )
  )
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
    children: el('div', { style: S.col(6) },
      el('div', { style: S.small }, row.repo + ' #' + row.number + ': ' + row.title),
      el('div', { style: S.small }, 'Commit ', el('span', { style: S.mono }, row.head_sha.slice(0, 12)), ' into ' + (row.base || 'the base branch') + '.'),
      el('div', { style: S.small }, 'This merges with your account (the write token in the settings), not as Hermes. If the branch moved since this page loaded, nothing is merged.')),
    confirmLabel: 'Merge with my account',
    busyLabel: 'Merging',
    doneLabel: 'Merged',
    description: row.flow ? 'Every gate before you has passed.' : 'Your review was requested. Forgejo still applies its branch protection and required checks.',
    onClose,
    onConfirm: run,
    open: true,
    title: 'Merge pull request #' + row.number
  })
}

// ── the page ─────────────────────────────────────────────────────────────────────────────────────

function Header({ data, onRefresh, refreshing }) {
  const host_ = data && data.forge_url ? data.forge_url.replace(/^https?:\/\//, '').replace(/\/$/, '') : ''
  const engine = (data && data.engine) || {}
  return el(
    'div',
    { style: { ...S.row(10), justifyContent: 'space-between' } },
    el('div', { style: S.row(10) },
      el(Mark, { size: 22 }),
      el('div', { style: S.col(2) },
        el('div', { className: 'text-base font-semibold', style: { color: T.primary } }, 'Forgejo desk'),
        el('div', { style: S.small }, [host_, 'flows checked ' + ago(engine.age_s)].filter(Boolean).join(' · ')))),
    el(Button, { disabled: refreshing, onClick: onRefresh, size: 'sm', variant: 'outline' }, el(Codicon, { name: 'refresh', size: '0.75rem', spinning: refreshing }), 'Refresh')
  )
}

function Loading() {
  return el('div', { style: S.col(10), 'data-test': 'loading' },
    el('div', { style: S.small }, 'Reading your pull requests from the forge.'),
    el(Skeleton, { style: { height: 92 } }), el(Skeleton, { style: { height: 92 } }), el(Skeleton, { style: { height: 92 } }))
}

function Problem({ title, text, action, onAction }) {
  return el('div', { 'data-test': 'error', style: { paddingTop: 24 } },
    el(ErrorState, { title, description: text },
      el('div', { style: { display: 'flex', justifyContent: 'center' } }, el(Button, { onClick: onAction, size: 'sm' }, action))))
}

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
  if (desk.isLoading) body = el(Loading, {})
  else if (desk.isError && notHere(desk.error)) body = el(Problem, { action: 'Open the plugins', onAction: () => host.navigate('/settings?tab=plugins'), text: 'The desk reads the profile you are using' + (profile ? ' (' + profile + ')' : '') + ', and Forgebrücke is not switched on there. Open a chat of a profile where it is on, or switch it on for this one in Settings → Plugins. Your pull requests are unchanged.', title: 'Forgebrücke is not on in this profile' })
  else if (desk.isError) body = el(Problem, { action: 'Try again', onAction: () => desk.refetch(), text: 'Hermes did not answer: ' + desk.error.message + '. Your pull requests are unchanged.', title: 'The desk could not load' })
  else if (data && data.ok === false) body = el(Problem, { action: 'Open the settings', onAction: () => host.navigate(data.settings_href || SETTINGS_HREF), text: data.message, title: 'Forgebrücke needs one more setting' })
  else {
    const counts = data.counts || {}
    const rows = data.rows || []
    body = el('div', { style: S.col(12) },
      el(SegmentedControl, {
        onChange: setTab,
        options: [
          { id: 'waiting', label: 'Waiting for me · ' + (counts.waiting || 0) },
          { id: 'running', label: 'Running · ' + (counts.running || 0) },
          { id: 'blocked', label: 'Blocked · ' + (counts.blocked || 0) },
          { id: 'repos', label: 'Repos · ' + (data.repos || []).length }
        ],
        sizing: 'content',
        value: tab
      }),
      data.forge_error ? el('div', { style: { ...S.small, color: T.yellow } }, 'The forge did not answer, so checks and comments are missing: ' + data.forge_error) : null,
      el(Notes, { notes: data.notes }),
      tab === 'repos' ? el(Repos, { repos: data.repos || [] }) : el(RowList, { note: data.waiting_note, onMerge: setMerging, rows, setTab, tab }),
      merging ? el(MergeDialog, { onClose: () => setMerging(null), row: merging }) : null)
  }
  return el('div', { style: { ...S.col(16), padding: 20, maxWidth: 980 } },
    el(Header, { data: data && data.ok !== false ? data : null, onRefresh: refresh, refreshing }), body)
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
        el(Problem, { action: 'Reload the desk', onAction: () => this.setState({ error: null }), text: 'Something in this view could not be drawn (' + String(this.state.error.message || this.state.error) + '). Nothing on the forge changed.', title: 'The desk hit an error' }))
    }
    return this.props.children
  }
}

function DeskPage() {
  return el(Guard, {}, el(DeskBody, {}))
}

// ── settings page ──────────────────────────────────────────────────────────────────────────────
// Read top to bottom: how Forgebrücke works and what it promises, then the setup steps, each next to the
// field it asks for. The key fields themselves are Hermes' own form (Agent settings, below this page in
// the rail): only that form writes a secret to the profile's .env, so every step links there.

const RULES = [
  ['person', 'Hermes has its own Forgejo account', 'Everything it writes is signed by that account, never by you.'],
  ['eye', 'Forgejo decides what it may do', 'Per repo you give its account Read or Write in Forgejo. That right is the only permission layer.'],
  ['pass', 'You approve every write', 'Each comment, pull request or status asks you first. Nothing approves or merges main.'],
  ['key', 'Keys stay narrow', 'Tokens live on the Hermes host, where any agent shell can read them. Mint each with the fewest scopes.']
]

const STEP_RING = { done: T.green, todo: T.accent, optional: T.tertiary, problem: T.red }
const FEATURE_BADGE = { available: ['success', 'available'], off: ['muted', 'off'], dark: ['outline', 'not with this setup'] }
const FEATURE_ORDER = { available: 0, off: 1, dark: 2 }

const openFields = () => host.navigate(SETTINGS_HREF)
const strong = text => el('span', { style: { color: T.primary, fontWeight: 600 } }, text)
const BODY = { fontSize: '0.75rem', color: T.secondary, lineHeight: 1.5 }

function Heading({ title, caption }) {
  return el('div', { style: S.col(2) },
    el('div', { className: 'text-sm font-semibold', style: { color: T.primary } }, title),
    caption ? el('div', { style: S.small }, caption) : null)
}

function Node({ icon, title, caption }) {
  return el('div', { style: { ...S.col(4), alignItems: 'center', textAlign: 'center', padding: '12px 8px', borderRadius: 8, border: '1px solid ' + T.stroke, minWidth: 0 } },
    el(Codicon, { name: icon, size: '1.4rem', style: { color: T.primary } }),
    el('div', { style: { fontSize: '0.8125rem', fontWeight: 600, color: T.primary } }, title),
    el('div', { style: S.small }, caption))
}

function Link({ above, below, toward }) {
  const arrow = el(Codicon, { name: toward === 'right' ? 'arrow-right' : 'arrow-left', size: '0.9rem', style: { color: T.secondary } })
  const line = el('div', { style: { flex: 1, height: 1, background: T.stroke } })
  return el('div', { style: { ...S.col(4), justifyContent: 'center', minWidth: 0, padding: '0 6px' } },
    el('div', { style: { ...S.small, color: T.primary, textAlign: 'center' } }, above),
    el('div', { style: { display: 'flex', alignItems: 'center', gap: 2 } }, toward === 'right' ? line : arrow, toward === 'right' ? arrow : line),
    el('div', { style: { ...S.small, textAlign: 'center' } }, below))
}

function HowItWorks({ bot, forgeHost }) {
  return el('section', { style: S.col(14), 'data-test': 'how' },
    el(Heading, { title: 'How it works', caption: 'Hermes works on your Forgejo as its own account, with the right you give it per repo. You keep the merge.' }),
    el('div', { style: { display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(7rem,1.1fr) minmax(0,1fr) minmax(7rem,1.1fr) minmax(0,1fr)', alignItems: 'stretch' } },
      el(Node, { icon: 'account', title: 'You', caption: 'your own Forgejo account' }),
      el(Link, { above: 'review and merge', below: 'only you', toward: 'right' }),
      el(Node, { icon: 'server', title: 'Forgejo', caption: forgeHost ? forgeHost.replace(/\./g, '.\u200b') : 'your forge' }),
      el(Link, { above: 'comments, PRs, checks', below: 'where its account may write', toward: 'left' }),
      el(Node, { icon: 'hubot', title: 'Hermes', caption: bot && bot.login ? 'signs in as @' + bot.login : 'signs in as its own account' })),
    el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(17rem, 1fr))', gap: '10px 20px' }, 'data-test': 'rules' },
      ...RULES.map(([icon, title, body]) => el('div', { key: icon, style: { display: 'flex', gap: 10, alignItems: 'flex-start' } },
        el(Codicon, { name: icon, size: '1rem', style: { color: T.accent, marginTop: 1 } }),
        el('div', { style: S.col(2) }, el('div', { style: { fontSize: '0.75rem', fontWeight: 600, color: T.primary } }, title), el('div', { style: BODY }, body))))))
}

function Scopes({ list }) {
  return el('span', { style: { display: 'inline-flex', flexWrap: 'wrap', gap: 4, verticalAlign: 'middle' } },
    ...list.map(sc => el('span', { key: sc, style: { ...S.mono, padding: '1px 5px', borderRadius: 4, border: '1px solid ' + T.strokeSoft, color: T.primary } }, sc)))
}

function tokenState(t) {
  if (!t.set) return 'unset'
  return t.error ? 'refused' : 'set'
}

function TokenStatus({ t }) {
  const state = tokenState(t)
  const [icon, color, words] = state === 'unset'
    ? (t.renamed_from
        ? ['warning', T.yellow, 'Not set. A token is still stored under the old name ' + t.renamed_from + ': enter it again here']
        : ['circle-large-outline', T.tertiary, 'Not set'])
    : state === 'refused'
      ? ['error', T.red, 'Set, but Forgejo refused it: ' + ((t.error && t.error.message) || 'unknown error')]
      : ['pass-filled', T.green, t.login ? 'Set, signs in as @' + t.login : 'Set']
  return el('div', { style: { ...S.row(6), fontSize: '0.75rem' }, 'data-test': 'token', 'data-state': state, 'data-env': t.env },
    el(Codicon, { name: icon, size: '0.85rem', style: { color } }),
    el('span', { style: { color: state === 'set' ? T.primary : T.secondary } }, words),
    el('span', { style: { ...S.mono, color: T.tertiary } }, t.env))
}

function FieldButton({ label, set, change }) {
  return el('div', { style: S.row(8) },
    el(Button, { onClick: openFields, size: 'xs', variant: set ? 'outline' : 'default', 'data-test': 'enter' }, el(Codicon, { name: 'edit', size: '0.75rem' }), set ? change || 'Replace the token' : 'Enter ' + label.toLowerCase()),
    el('span', { style: S.small }, 'Opens Agent settings, field “' + label + '”'))
}

function Step({ n, state, title, tag, last, children }) {
  const ring = STEP_RING[state]
  return el('div', { style: { display: 'grid', gridTemplateColumns: '26px minmax(0,1fr)', columnGap: 12 }, 'data-test': 'step', 'data-state': state },
    el('div', { style: { display: 'flex', flexDirection: 'column', alignItems: 'center' } },
      el('div', { style: { width: 24, height: 24, borderRadius: 12, border: '1.5px solid ' + ring, color: ring, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.75rem', fontWeight: 600, flex: 'none' } },
        state === 'done' ? el(Codicon, { name: 'check', size: '0.85rem' }) : state === 'problem' ? el(Codicon, { name: 'warning', size: '0.8rem' }) : String(n)),
      last ? null : el('div', { style: { width: 1.5, flex: 1, minHeight: 12, background: T.strokeSoft, marginTop: 4 } })),
    el('div', { style: { ...S.col(8), paddingBottom: last ? 0 : 22, paddingTop: 2 } },
      el('div', { style: S.row(8) }, el('div', { style: { fontSize: '0.8125rem', fontWeight: 600, color: T.primary } }, title), tag ? el(Badge, { variant: tag[0] }, tag[1]) : null),
      ...children))
}

function OptionCard({ t, title, headline }) {
  return el('div', { style: { ...S.card, gap: 10 }, 'data-test': 'option', 'data-role': t.role },
    el('div', { style: S.col(2) }, el('div', { style: { fontSize: '0.75rem', fontWeight: 600, color: T.primary } }, title), el('div', { style: BODY }, headline)),
    el('div', { style: BODY }, t.unlocks),
    el('div', { style: S.small }, 'Never used for: ' + t.never.charAt(0).toLowerCase() + t.never.slice(1)),
    t.risk ? el('div', { style: { ...S.row(6), ...S.small, color: T.yellow, flexWrap: 'nowrap', alignItems: 'flex-start' } }, el(Codicon, { name: 'warning', size: '0.8rem' }), t.risk) : null,
    el('div', { style: BODY }, 'Signed in as yourself, mint it with ', el(Scopes, { list: t.mint })),
    t.mint_note ? el('div', { style: S.small }, t.mint_note) : null,
    el('div', { style: { marginTop: 'auto', ...S.col(8) } }, el(TokenStatus, { t }), el(FieldButton, { label: t.label.replace(/ \(optional\)$/, ''), set: t.set })))
}

function Setup({ s }) {
  const [bot, owner] = ['bot', 'owner'].map(role => (s.tokens || []).find(t => t.role === role) || { role, env: '', set: false, mint: [], label: role })
  const urlState = s.forge_error ? 'problem' : s.forge_url ? 'done' : 'todo'
  const botState = { set: 'done', unset: 'todo', refused: 'problem' }[tokenState(bot)]
  const youState = s.you ? 'done' : 'todo'
  const optional = tokenState(owner) === 'refused' ? 'problem' : owner.set ? 'done' : 'optional'
  return el('section', { style: S.col(14), 'data-test': 'setup' },
    el(Heading, { title: 'Set it up', caption: 'Three steps. The fourth is optional, and most setups skip it.' }),
    el('div', {},
      el(Step, { n: 1, state: urlState, title: 'Your Forgejo address', tag: urlState === 'done' ? null : ['default', 'required'] },
        el('div', { style: BODY }, 'The address you open Forgejo at, for example https://forgejo.example.com.'),
        s.forge_url ? el('div', { style: { ...S.mono, color: T.primary } }, s.forge_url) : null,
        s.forge_error ? el('div', { style: { ...S.small, color: T.red } }, 'Forgejo did not answer: ' + s.forge_error) : null,
        el(FieldButton, { label: 'Forgejo address', set: Boolean(s.forge_url), change: 'Change the address' })),
      el(Step, { n: 2, state: botState, title: 'A Forgejo account for Hermes', tag: botState === 'done' ? null : ['default', 'required'] },
        el('ol', { style: { ...BODY, margin: 0, paddingLeft: 18, listStyle: 'decimal', display: 'flex', flexDirection: 'column', gap: 4 } },
          el('li', {}, 'In Forgejo, use one account for Hermes, for example ', strong('hermes'), '.'),
          el('li', {}, 'On each repo Hermes should see, add that account under ', strong('Settings → Collaborators'), ' with ', strong('Read'), ' or ', strong('Write'), '. That right is all Hermes may do there; other repos stay invisible to it.'),
          el('li', {}, 'Signed in as that account, open ', strong('Settings → Applications'), ' and generate a token for all repositories with ', el(Scopes, { list: bot.mint || [] }), '.'),
          el('li', {}, 'Paste the token into ', strong(bot.label || 'Agent account token'), '.')),
        el(TokenStatus, { t: bot }),
        el(FieldButton, { label: bot.label || 'Agent account token', set: bot.set })),
      el(Step, { n: 3, state: youState, title: 'Your Forgejo login', tag: youState === 'done' ? null : ['default', 'for the desk'] },
        el('div', { style: BODY }, 'Waiting for me lists the pull requests that request your review, and every pull request Hermes opens asks you to review it.'),
        s.you
          ? el('div', { style: { ...S.row(6), fontSize: '0.75rem' }, 'data-test': 'you' }, el(Codicon, { name: 'pass-filled', size: '0.85rem', style: { color: T.green } }), el('span', { style: { color: T.primary } }, 'You are @' + s.you))
          : el('div', { style: S.small, 'data-test': 'you' }, s.you_suggestion ? ['Not set. Probably ', el('span', { key: 'm', style: S.mono }, s.you_suggestion), ': the owner of the repos Hermes sees.'] : 'Not set.'),
        el(FieldButton, { label: 'Your Forgejo login', set: Boolean(s.you), change: 'Change your login' })),
      el(Step, { n: 4, state: optional, title: 'Merge from the desk (optional)', tag: ['muted', 'optional'], last: true },
        el('div', { style: BODY }, 'Leave this empty and merge in Forgejo. Hermes never writes with this token on its own.'),
        el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(17rem, 1fr))', gap: 10 } },
          el(OptionCard, { t: owner, title: 'Your write token', headline: 'Merge from the desk, as you.' })))),
    ...(s.token_warnings || []).map(w => el('div', { key: w, style: { ...S.row(6), ...S.small, color: T.yellow, flexWrap: 'nowrap', alignItems: 'flex-start' }, 'data-test': 'token-warning' },
      el(Codicon, { name: 'warning', size: '0.8rem' }), w)))
}

function Features({ features }) {
  const sorted = [...features].sort((a, b) => (FEATURE_ORDER[a.status] ?? 3) - (FEATURE_ORDER[b.status] ?? 3))
  const working = features.filter(f => f.status === 'available').length
  return el('section', { style: S.col(10) },
    el(Heading, { title: 'What this setup can do', caption: working + ' of ' + features.length + ' features work now. A greyed one names the token that would light it.' }),
    ...sorted.map(f => {
      const [variant, word] = FEATURE_BADGE[f.status] || ['muted', f.status]
      return el('div', { key: f.id, 'data-test': 'feature', 'data-status': f.status, style: { ...S.col(2), opacity: f.status === 'dark' ? 0.6 : 1 } },
        el('div', { style: S.row(6) }, el(Badge, { variant }, word), el('span', { style: { fontSize: '0.75rem', color: T.primary } }, f.label)),
        el('div', { style: S.small }, f.reason))
    }))
}

function SettingsBody() {
  const q = useSettings()
  const [reviewer, setReviewer] = useState(null)
  if (q.isLoading) return el(Loading, {})
  if (q.isError) return el(Problem, { action: 'Try again', onAction: () => q.refetch(), text: q.error.message, title: 'The settings could not load' })
  const s = q.data || {}
  if (s.ok === false) return el(Problem, { action: 'Open the fields', onAction: openFields, text: s.message, title: 'Forgebrücke needs one more setting' })
  const createReviewer = async () => {
    try {
      setReviewer(await rest('/reviewer-profile', { method: 'POST', body: {} }))
    } catch (error) {
      setReviewer({ ok: false, message: error.message })
    }
  }
  const bot = (s.tokens || []).find(t => t.role === 'bot')
  const ready = Boolean(s.forge_url && !s.forge_error && bot && tokenState(bot) === 'set')
  const forgeHost = s.forge_url ? s.forge_url.replace(/^https?:\/\//, '') : ''
  return el(
    'div',
    { style: { ...S.col(28), padding: 20, maxWidth: 860 } },
    el('div', { style: { ...S.row(10), justifyContent: 'space-between' } },
      el('div', { style: S.row(10) }, el(Mark, { size: 22 }),
        el('div', { style: S.col(2) }, el('div', { className: 'text-base font-semibold', style: { color: T.primary } }, 'Forgebrücke'), el('div', { style: S.small }, 'A bridge to your forge.')),
        el(Badge, { variant: ready ? 'success' : 'outline', 'data-test': 'ready' }, ready ? 'ready' : 'setup needed')),
      el(Button, { onClick: openFields, size: 'xs', variant: 'outline' }, el(Codicon, { name: 'settings-gear', size: '0.75rem' }), 'All settings')),
    el(HowItWorks, { bot, forgeHost }),
    el(Setup, { s }),
    el(Features, { features: s.features || [] }),
    el('section', { style: S.col(8) },
      el(Heading, { title: 'Agent reviews' }),
      el('div', { style: S.small }, 'The profile that runs agent reviews: ', el('span', { style: S.mono }, s.default_review_profile || 'not set'),
        '. Any profile you have works: ' + (s.profiles || []).join(', ') + '.'),
      s.reviewer && !s.reviewer.exists
        ? el('div', { style: S.row(8) }, el(Button, { onClick: createReviewer, size: 'xs', variant: 'outline' }, 'Create a reviewer profile'),
          el('span', { style: S.small }, 'None fits? This clones your profile as ' + s.reviewer.name + ', without Forgebrücke tokens.'))
        : null,
      reviewer ? el('div', { style: { ...S.small, color: reviewer.ok ? T.green : T.yellow } }, reviewer.message, reviewer.command ? el('div', { style: S.mono }, reviewer.command) : null) : null)
  )
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

export default {
  id: ID,
  name: 'Forgebrücke',
  description: 'Your Forgejo pull requests, their gates and checks, and what waits for you.',
  register(ctx) {
    pluginCtx = ctx
    navLabel = null
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
