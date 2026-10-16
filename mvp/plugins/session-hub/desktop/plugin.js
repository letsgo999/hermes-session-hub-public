import { host, queryClient, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, useQuery } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

export const SESSION_HUB_ROUTE = '/session-hub'
export const PAGE_SIZE_CHOICES = [10, 20, 50, 100, 'all']
// Local profiles live on this PC and can be opened in place. Optional branch profiles ("<ID>/default")
// are read from synced snapshot copies and are display-only. The real lists come from GET /config
// (auto-detected Hermes profiles + session-hub.config.json); applyHubConfig() fills these in place.
export const LOCAL_PROFILE_NAMES = ['default']
export const REMOTE_PROFILE_NAMES = []
export const PROFILE_NAMES = ['default']
export const HUB_UI = { instancesEnabled: true, localLabel: '본사', remoteGroupLabel: '지사' }
export const PROFILE_GROUPS = [
  { id: 'HQ', label: HUB_UI.localLabel, profiles: LOCAL_PROFILE_NAMES }
]
export const PROFILE_LABELS = {}
const CONFIG_PROFILE_RE = /^[A-Za-z0-9_.-]{1,64}(\/default)?$/

function replaceArray(target, values) {
  target.splice(0, target.length, ...values)
}

function cleanLabel(value, fallback) {
  if (typeof value !== 'string') return fallback
  const text = value.split(/\s+/).filter(Boolean).join(' ').slice(0, 40)
  return text || fallback
}

// Apply the backend's GET /config payload. Returns true when the profile lists changed.
export const HUB_CONFIG_STATE = { loaded: false }
const CONFIG_RETRY_DELAYS_MS = [2000, 5000, 15000, 30000, 60000]

export function applyHubConfig(config) {
  if (!config || config.contract !== 'session-hub-config-v1' || !Array.isArray(config.localProfiles)) return false
  const valid = name => typeof name === 'string' && CONFIG_PROFILE_RE.test(name) && !/(^|\/)\.\.?(\/|$)/.test(name)
  const local = [...new Set(config.localProfiles.filter(name => valid(name) && !name.includes('/')))]
  if (!local.length) return false
  HUB_CONFIG_STATE.loaded = true
  const enabled = config.instancesEnabled !== false
  const remote = enabled && Array.isArray(config.remoteProfiles)
    ? [...new Set(config.remoteProfiles.filter(name => valid(name) && name.endsWith('/default')))]
    : []
  const before = JSON.stringify([PROFILE_NAMES, PROFILE_GROUPS.map(group => group.label), PROFILE_LABELS])
  replaceArray(LOCAL_PROFILE_NAMES, local)
  replaceArray(REMOTE_PROFILE_NAMES, remote)
  replaceArray(PROFILE_NAMES, [...local, ...remote])
  HUB_UI.instancesEnabled = enabled
  HUB_UI.localLabel = cleanLabel(config.localLabel, '본사')
  HUB_UI.remoteGroupLabel = cleanLabel(config.remoteGroupLabel, '지사')
  for (const key of Object.keys(PROFILE_LABELS)) delete PROFILE_LABELS[key]
  const labels = config.profileLabels && typeof config.profileLabels === 'object' ? config.profileLabels : {}
  for (const [key, value] of Object.entries(labels)) {
    if (valid(key) && typeof value === 'string' && value.trim()) PROFILE_LABELS[key] = cleanLabel(value, key)
  }
  const groups = [{ id: 'HQ', label: enabled ? HUB_UI.localLabel : 'Profiles', profiles: LOCAL_PROFILE_NAMES }]
  if (remote.length) groups.push({ id: 'branch', label: HUB_UI.remoteGroupLabel, profiles: REMOTE_PROFILE_NAMES })
  replaceArray(PROFILE_GROUPS, groups)
  return before !== JSON.stringify([PROFILE_NAMES, PROFILE_GROUPS.map(group => group.label), PROFILE_LABELS])
}

export function loadHubConfig(ctx, client = queryClient) {
  if (!ctx || typeof ctx.rest !== 'function') return Promise.resolve(false)
  return Promise.resolve()
    .then(() => ctx.rest('/config', { timeoutMs: 5000 }))
    .then(config => {
      const changed = applyHubConfig(config)
      if (changed) client.invalidateQueries({ queryKey: ['session-hub'] })
      return changed
    })
    .catch(() => false)
}

// The Python half may still be starting when the Desktop registers plugins: retry with backoff
// until one valid GET /config answer has been applied.
export function loadHubConfigWithRetry(ctx, client = queryClient, delays = CONFIG_RETRY_DELAYS_MS, { keepAlive = false } = {}) {
  const attempt = index => loadHubConfig(ctx, client).then(changed => {
    if (HUB_CONFIG_STATE.loaded || index >= delays.length || typeof setTimeout !== 'function') return changed
    return new Promise(resolve => {
      const timer = setTimeout(() => resolve(attempt(index + 1)), delays[index])
      if (!keepAlive && timer && typeof timer.unref === 'function') timer.unref()
    })
  })
  return attempt(0).catch(() => false)
}

export const TELEGRAM_TYPES = ['dm', 'group', 'topic']
export const KANBAN_STATUSES = ['triage', 'todo', 'ready', 'running', 'review', 'review-pending', 'blocked', 'done', 'archived', 'cancelled']
export const DEFAULT_HISTORY_SCOPE = '30d'
export const PROJECT_POLL_MS = 30000
export const KANBAN_DETAIL_TIMEOUT_MS = 20000

const PAGE_SIZE_KEY = 'pageSize'
const SESSION_ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/
const ALL_WINDOW_ROWS = 100
const CURSOR_HISTORY_LIMIT = 100

export function getInitialPageSize(storage) {
  const value = storage.get(PAGE_SIZE_KEY)
  return PAGE_SIZE_CHOICES.includes(value) ? value : 20
}

export function defaultSessionFilters(nowEpochSeconds, historyScope = DEFAULT_HISTORY_SCOPE) {
  return historyWindowSelection(historyScope, nowEpochSeconds)
}

export function historyWindowSelection(historyScope, nowEpochSeconds, customDateFrom = null) {
  if (historyScope === 'custom') return { historyScope, dateFrom: customDateFrom }
  const days = { '7d': 7, '14d': 14, '30d': 30 }
  if (historyScope === 'full') return { historyScope, dateFrom: null }
  if (!Object.hasOwn(days, historyScope)) throw new Error('Unsupported history window')
  return {
    historyScope,
    dateFrom: Math.floor(nowEpochSeconds) - days[historyScope] * 24 * 60 * 60
  }
}

export function registerEventRefresh(eventSource, client = queryClient) {
  if (!eventSource || typeof eventSource.onEvent !== 'function') return () => undefined
  return eventSource.onEvent('*', () => client.invalidateQueries({ queryKey: ['session-hub'] }))
}

export function isRemoteProfile(profile) {
  return REMOTE_PROFILE_NAMES.includes(profile)
}

export function profileInstance(profile) {
  return isRemoteProfile(profile) ? profile.split('/')[0] : 'HQ'
}

export function profileLabel(profile) {
  const label = PROFILE_LABELS[profile]
  return label ? `${label} · ${profile}` : String(profile)
}

// Branch snapshot freshness badge. `status` is one entry of the backend's `instances` array.
export function formatFreshness(status, nowEpochSeconds = Math.floor(Date.now() / 1000)) {
  if (!status || status.kind === 'local') return null
  const id = status.id
  const age = Number.isFinite(Number(status.ageSeconds)) && status.ageSeconds !== null
    ? Number(status.ageSeconds)
    : (Number.isFinite(Number(status.checkedAt)) && status.checkedAt ? Math.max(0, nowEpochSeconds - Number(status.checkedAt)) : null)
  const ago = age === null ? null
    : age < 60 ? '방금'
    : age < 3600 ? `${Math.floor(age / 60)}분 전`
    : age < 86400 ? `${Math.floor(age / 3600)}시간 전`
    : `${Math.floor(age / 86400)}일 전`
  switch (status.state) {
    case 'fresh': return { text: `${id} · ${ago} 스냅샷`, tone: 'fresh' }
    case 'stale': return { text: `${id} · 스냅샷 오래됨${ago ? ` (${ago})` : ''}`, tone: 'stale' }
    case 'damaged': return { text: `${id} · 스냅샷 손상`, tone: 'error' }
    case 'unsupported': return { text: `${id} · 스냅샷 버전 미지원`, tone: 'error' }
    default: return { text: `${id} · 스냅샷 없음`, tone: 'unknown' }
  }
}

export function instancesById(data) {
  const map = new Map()
  for (const status of Array.isArray(data?.instances) ? data.instances : []) {
    if (status && status.id) map.set(status.id, status)
  }
  return map
}

function isValidSelection(selection) {
  // Desktop SDK openSession only reaches this PC's profiles: branch rows are never a valid open target.
  return Boolean(
    selection &&
    LOCAL_PROFILE_NAMES.includes(selection.ownerProfile) &&
    SESSION_ID_RE.test(selection.storedSessionId || '')
  )
}

export function createOpenController({ backend, host: hostApi }) {
  let sequence = 0
  return {
    async open(selection) {
      const current = ++sequence
      if (!isValidSelection(selection)) {
        if (hostApi.notify) hostApi.notify({ kind: 'warning', message: 'Session selection is invalid.' })
        return { opened: false }
      }
      // Two failure classes are reported separately so a backend rejection (ownership,
      // source, schema) is never mistaken for a Desktop open failure, and vice versa.
      let phase = 'revalidate'
      try {
        const validated = await backend.revalidate(selection)
        if (current !== sequence) return { ignored: true }
        if (validated && validated.ok === false) {
          throw new Error(`backend rejected: ${validated.error || 'unknown'}`)
        }
        if (
          !isValidSelection(validated) ||
          validated.ownerProfile !== selection.ownerProfile ||
          validated.storedSessionId !== selection.storedSessionId
        ) {
          throw new Error('ownership validation failed')
        }
        phase = 'open'
        await hostApi.openSession(selection.storedSessionId, {
          profile: selection.ownerProfile,
          intent: 'in-place',
          keepAllProfilesScope: false
        })
        return { opened: true }
      } catch (error) {
        const detail = error && error.message ? String(error.message) : 'unknown error'
        const reason = phase === 'revalidate'
          ? `Session could not be verified (${detail}).`
          : `Desktop could not open ${selection.ownerProfile} session (${detail}).`
        if (current === sequence && hostApi.notify) {
          hostApi.notify({ kind: 'warning', message: reason })
        }
        if (typeof hostApi.newChat === 'function') {
          return {
            opened: false,
            phase,
            reason,
            recoveryLabel: '복구용 새 세션',
            recover: () => hostApi.newChat(selection.ownerProfile)
          }
        }
        return { opened: false, phase, reason }
      }
    }
  }
}

export function createKanbanController({ openSession, showTask, resolveOrigin }) {
  return {
    async open(task) {
      if (task?.id && typeof showTask === 'function') {
        await showTask(task)
        return { detailOpened: true, taskId: task.id }
      }
      return { detailOpened: false }
    },
    // Opens the task's origin session using the exact-card provenance from the Kanban backend
    // (never the task assignee, which is not the session owner).
    async openOrigin(task) {
      if (!task?.id || typeof resolveOrigin !== 'function' || typeof openSession !== 'function') {
        return { opened: false, reason: 'Origin session is unavailable.' }
      }
      let origin = null
      try {
        origin = await resolveOrigin(task)
      } catch (_error) {
        return { opened: false, reason: 'Exact Kanban card detail is unavailable.' }
      }
      if (!origin?.available || !origin.ownerProfile || !origin.storedSessionId) {
        return { opened: false, reason: origin?.reason || 'Origin session is unavailable.' }
      }
      return openSession({ ownerProfile: origin.ownerProfile, storedSessionId: origin.storedSessionId })
    }
  }
}

async function resolveKanbanOrigin(backend, task) {
  const listing = await backend.list({ tab: 'kanban', query: task.id, historyScope: 'full', pageSize: 20 })
  const match = (listing?.items || []).find(item => item.id === task.id)
  if (!match?.board) return null
  const card = await backend.kanbanCard(match.board, task.id)
  return card?.id === task.id ? card.originSession : null
}

function createBackend(ctx) {
  return {
    async list(filters) {
      const endpoint = filters.tab === 'skills'
        ? '/skills/search'
        : filters.tab === 'kanban'
        ? '/kanban'
        : filters.tab === 'external-ai' ? '/ai-sessions' : '/sessions'
      const body = filters.tab === 'skills'
        ? { query: filters.query, includeArchive: filters.includeArchive }
        : filters.tab === 'external-ai'
        ? { query: filters.query, sources: filters.aiSources, includeArchived: filters.includeArchive, pageSize: filters.pageSize, cursor: filters.cursor }
        : filters
      return ctx.rest(endpoint, { method: 'POST', body, timeoutMs: filters.tab === 'skills' ? 15000 : 5000 })
    },
    async refreshAiSessions() {
      return ctx.rest('/ai-sessions/refresh', { method: 'POST', body: {}, timeoutMs: 60000 })
    },
    async refreshSkills(filters) {
      return ctx.rest('/skills/refresh', {
        method: 'POST',
        body: { query: filters.query, includeArchive: filters.includeArchive },
        timeoutMs: 20000
      })
    },
    async kanbanCard(board, taskId) {
      return ctx.rest(`/kanban/${encodeURIComponent(board)}/${encodeURIComponent(taskId)}`, { timeoutMs: KANBAN_DETAIL_TIMEOUT_MS })
    },
    async revalidate(selection) {
      return ctx.rest('/revalidate', { method: 'POST', body: selection, timeoutMs: 3000 })
    },
    async projects() {
      return ctx.rest('/projects', { timeoutMs: 5000 })
    },
    async refreshProjects() {
      return ctx.rest('/projects/refresh', { method: 'POST', body: {}, timeoutMs: 10000 })
    },
    async projectDetail(projectId) {
      return ctx.rest(`/projects/${projectId}`, { timeoutMs: 5000 })
    },
    async updateProject(projectId, changes) {
      return ctx.rest(`/projects/${projectId}/overrides`, { method: 'PATCH', body: changes, timeoutMs: 5000 })
    }
  }
}

// 2026-10-13: surface the real backend error code instead of always blaming the Python plugin gate.
const BACKEND_ERROR_HINTS = {
  stale_schema: 'Hermes state.db schema_version is outside the range this plugin accepts. Update sessionhub_backend.py (MIN/MAX_SCHEMA_VERSION).',
  unexpected_schema: 'Hermes state.db is missing columns this plugin needs (sessions/messages). Check the Hermes update notes.',
  readonly_not_enforced: 'Read-only guard could not be enforced on state.db.',
  kanban_schema_unsupported: 'Kanban DB schema is not supported by this plugin version.',
  local_desktop_required: '이 기능(스킬 검색, 코덱스·클로드 새로고침)은 이 PC의 Hermes Desktop에서만 열립니다. 원격 대시보드(브라우저)에서는 쓸 수 없습니다. Desktop인데도 보이면 Hermes를 재시작한 뒤 다시 시도하세요.',
  plugin_host_isolation: 'config.yaml의 plugins.isolation이 host로 되어 있어 이 PC 요청인지 확인할 수 없습니다. 스킬 검색·코덱스·클로드 새로고침을 쓰려면 plugins.isolation을 in_process(기본값)로 두고 Hermes를 재시작하세요.'
}

function describeBackendError(error) {
  const raw = String((error && (error.message || error)) || '').slice(0, 300)
  const match = raw.match(/"error"\s*:\s*"([A-Za-z0-9_.-]+)"/) || raw.match(/backend rejected:\s*([A-Za-z0-9_.-]+)/)
  const code = match ? match[1] : ''
  if (code) {
    return {
      headline: `returned an error: ${code}.`,
      detail: BACKEND_ERROR_HINTS[code] || raw
    }
  }
  return {
    headline: 'is unavailable. Enable the separately gated Python plugin (or restart the Hermes backend) and retry.',
    detail: raw || 'No response from the dashboard backend.'
  }
}

function useSessionHubData(ctx, filters) {
  const refreshOptions = filters.tab === 'skills'
    ? { refetchInterval: false, staleTime: 300000 }
    : { refetchInterval: 30000, staleTime: 5000 }
  return useQuery({
    queryKey: ['session-hub', filters],
    queryFn: () => createBackend(ctx).list(filters),
    ...refreshOptions,
    retry: 1
  })
}

function padDatePart(value) {
  return String(value).padStart(2, '0')
}

function kstDateText(date) {
  const shifted = new Date(date.getTime() + 9 * 60 * 60 * 1000)
  return `${shifted.getUTCFullYear()}-${padDatePart(shifted.getUTCMonth() + 1)}-${padDatePart(shifted.getUTCDate())} ${padDatePart(shifted.getUTCHours())}:${padDatePart(shifted.getUTCMinutes())}:${padDatePart(shifted.getUTCSeconds())} KST`
}

export function formatActivity(value) {
  const numeric = Number(value)
  const milliseconds = Number.isFinite(numeric) && numeric > 0
    ? numeric * 1000
    : (typeof value === 'string' ? Date.parse(value) : Number.NaN)
  if (!Number.isFinite(milliseconds)) return { text: 'Unknown time', dateTime: '' }
  const date = new Date(milliseconds)
  if (Number.isNaN(date.getTime())) return { text: 'Unknown time', dateTime: '' }
  return {
    text: kstDateText(date),
    dateTime: date.toISOString()
  }
}

export function dateBoundary(value, endOfDay = false) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return null
  const suffix = endOfDay ? 'T23:59:59+09:00' : 'T00:00:00+09:00'
  const milliseconds = Date.parse(`${value}${suffix}`)
  return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1000) : null
}

function dateInputValue(epochSeconds) {
  if (!Number.isFinite(epochSeconds)) return ''
  const shifted = new Date(epochSeconds * 1000 + 9 * 60 * 60 * 1000)
  return `${shifted.getUTCFullYear()}-${padDatePart(shifted.getUTCMonth() + 1)}-${padDatePart(shifted.getUTCDate())}`
}

function toggleValue(values, value, checked) {
  if (checked) return values.includes(value) ? values : [...values, value]
  return values.filter(candidate => candidate !== value)
}

export function groupSelectionState(selected, groupProfiles) {
  const selectedCount = groupProfiles.filter(profile => selected.includes(profile)).length
  return {
    checked: groupProfiles.length > 0 && selectedCount === groupProfiles.length,
    indeterminate: selectedCount > 0 && selectedCount < groupProfiles.length
  }
}

export function toggleGroupProfiles(selected, groupProfiles, checked) {
  const group = new Set(groupProfiles)
  if (checked) {
    const next = [...selected]
    for (const profile of groupProfiles) {
      if (!next.includes(profile)) next.push(profile)
    }
    return next
  }
  const next = selected.filter(profile => !group.has(profile))
  return next.length ? next : selected
}

function TabButton({ value, active, onSelect, children }) {
  return jsx('button', {
    type: 'button',
    role: 'tab',
    'aria-selected': active,
    tabIndex: active ? 0 : -1,
    className: [
      'rounded-md border px-3 py-1.5 text-sm',
      active
        ? 'border-(--ui-accent) bg-(--ui-accent-soft) text-(--ui-text-primary)'
        : 'border-(--ui-stroke-secondary) text-(--ui-text-secondary)'
    ].join(' '),
    onClick: () => onSelect(value),
    onKeyDown: event => {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault()
        const tabs = ['desktop', 'telegram', 'kanban', 'external-ai', 'skills']
        const offset = event.key === 'ArrowRight' ? 1 : -1
        onSelect(tabs[(tabs.indexOf(value) + offset + tabs.length) % tabs.length])
      }
    },
    children
  })
}

function CheckOption({ label, ariaLabel, checked, indeterminate = false, onChange }) {
  return jsxs('label', {
    className: 'inline-flex items-center gap-1.5 text-xs text-(--ui-text-secondary)',
    children: [
      jsx('input', {
        type: 'checkbox',
        checked,
        'aria-label': ariaLabel,
        'aria-checked': indeterminate ? 'mixed' : checked,
        ref: node => {
          if (node) node.indeterminate = Boolean(indeterminate)
        },
        onChange
      }),
      jsx('span', { children: label })
    ]
  })
}

function FreshnessBadge({ status }) {
  const badge = formatFreshness(status)
  if (!badge) return null
  const tone = badge.tone === 'fresh' ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-primary) font-medium'
  return jsx('span', {
    'data-freshness': badge.tone,
    className: `rounded border border-(--ui-stroke-secondary) px-1 ${tone}`,
    title: status?.reason ? `reason=${status.reason}` : undefined,
    children: badge.text
  })
}

function TelegramTurnCard({ item, onOpen, instances }) {
  const activity = formatActivity(item.userTimestamp ?? item.lastActivity)
  const sourceLabel = `${item.source} · ${item.telegramType || 'group'}`
  const remote = Boolean(item.remote) || isRemoteProfile(item.ownerProfile)
  const instance = item.instance || profileInstance(item.ownerProfile)
  const responsePending = item.responseState === 'pending' || !item.replyPreview
  const question = jsx('h2', {
    className: 'line-clamp-2 text-base font-semibold leading-snug text-(--ui-text-primary)',
    'aria-label': `Telegram question ${item.title}`,
    title: item.questionText || item.title,
    children: item.title
  })
  return jsxs('article', {
    'data-session-card': true,
    'data-telegram-turn-card': item.userMessageId,
    'data-remote': remote ? 'true' : 'false',
    className: 'rounded-md border border-(--ui-stroke-secondary) p-3',
    children: [
      remote
        ? jsxs('div', {
            className: 'w-full text-left',
            'aria-label': `Remote Telegram question ${item.title}`,
            children: [
              question,
              jsx('p', {
                className: 'mt-0.5 text-xs text-(--ui-text-tertiary)',
                'data-remote-notice': true,
                children: `이 PC에서는 열 수 없음 — ${HUB_UI.remoteGroupLabel} ${instance} 세션`
              })
            ]
          })
        : jsx('button', {
            type: 'button',
            className: 'w-full text-left hover:text-(--ui-accent)',
            'aria-label': `Open Telegram question ${item.title}`,
            onClick: () => onOpen(item),
            children: question
          }),
      jsx('p', {
        className: 'mt-2 line-clamp-2 text-sm text-(--ui-text-secondary)',
        'data-reply-state': responsePending ? 'pending' : 'answered',
        children: responsePending ? '응답 대기 중' : item.replyPreview
      }),
      jsxs('div', {
        className: 'mt-2 flex flex-wrap gap-2 text-xs text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: profileLabel(item.ownerProfile) }),
          remote ? jsx(FreshnessBadge, { status: instances?.get?.(instance) || { id: instance, kind: 'remote', state: 'unknown' } }) : null,
          jsx('span', { children: sourceLabel }),
          jsx('time', { dateTime: activity.dateTime, children: activity.text }),
          jsx('span', { children: responsePending ? '응답 대기 중' : '응답 완료' })
        ]
      })
    ]
  })
}

function SessionCard({ item, onOpen, instances }) {
  if (item.userMessageId != null) return jsx(TelegramTurnCard, { item, onOpen, instances })
  const activity = formatActivity(item.lastActivity)
  const sourceLabel = item.telegramType ? `${item.source} · ${item.telegramType}` : item.source
  const remote = Boolean(item.remote) || isRemoteProfile(item.ownerProfile)
  const instance = item.instance || profileInstance(item.ownerProfile)
  const title = jsx('h2', { className: 'truncate text-sm font-medium text-(--ui-text-primary)', children: item.title })
  return jsxs('article', {
    'data-session-card': true,
    'data-remote': remote ? 'true' : 'false',
    className: 'rounded-md border border-(--ui-stroke-secondary) p-3',
    children: [
      remote
        ? jsxs('div', {
            className: 'w-full text-left',
            'aria-label': `Remote session ${item.title}`,
            children: [
              title,
              jsx('p', {
                className: 'mt-0.5 text-xs text-(--ui-text-tertiary)',
                'data-remote-notice': true,
                children: `이 PC에서는 열 수 없음 — ${HUB_UI.remoteGroupLabel} ${instance} 세션`
              })
            ]
          })
        : jsx('button', {
            type: 'button',
            className: 'w-full text-left hover:text-(--ui-accent)',
            'aria-label': `Open ${item.title}`,
            onClick: () => onOpen(item),
            children: title
          }),
      jsxs('div', {
        className: 'mt-1 flex flex-wrap gap-2 text-xs text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: profileLabel(item.ownerProfile) }),
          remote ? jsx(FreshnessBadge, { status: instances?.get?.(instance) || { id: instance, kind: 'remote', state: 'unknown' } }) : null,
          jsx('span', { children: sourceLabel }),
          jsx('time', { dateTime: activity.dateTime, children: activity.text }),
          jsx('span', { children: `${Number(item.messageCount || 0)} messages` })
        ]
      }),
      jsx('ul', {
        className: 'mt-2 space-y-1 text-xs text-(--ui-text-secondary)',
        'aria-label': 'Recent messages',
        children: (item.previews || []).slice(0, 3).map((preview, index) =>
          jsx('li', { className: 'line-clamp-1', children: preview }, `${item.ownerProfile}:${index}`)
        )
      })
    ]
  })
}

function KanbanCard({ item, onOpen, selected = false, instances }) {
  const updated = formatActivity(item.updatedAt)
  const remote = Boolean(item.remote)
  const instance = item.instance || (remote ? item.board : 'HQ')
  const due = item.dueAt ? formatActivity(item.dueAt) : null
  const relationships = [
    item.projectId ? `project=${item.projectId}` : null,
    item.parentIds?.length ? `parents=${item.parentIds.join(',')}` : null,
    item.childIds?.length ? `children=${item.childIds.join(',')}` : null
  ].filter(Boolean)
  return jsxs('article', {
    'data-kanban-card': item.id,
    'data-selected': selected ? 'true' : 'false',
    'data-remote': remote ? 'true' : 'false',
    className: `rounded-md border p-3 ${selected ? 'border-(--ui-accent) bg-(--ui-accent-soft)' : 'border-(--ui-stroke-secondary)'}`,
    children: [
      jsx('button', {
        type: 'button',
        className: 'w-full text-left hover:text-(--ui-accent)',
        'aria-label': `Open Kanban card ${item.id}`,
        'aria-pressed': selected,
        onClick: () => onOpen(item),
        children: jsxs('span', { className: 'grid gap-0.5', children: [
          jsx('strong', { className: 'truncate text-sm', children: item.title || item.id }),
          jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: `${item.board} · ${item.id}` })
        ] })
      }),
      jsxs('div', { className: 'mt-2 flex flex-wrap gap-2 text-xs text-(--ui-text-secondary)', children: [
        remote ? jsx(FreshnessBadge, { status: instances?.get?.(instance) || { id: instance, kind: 'remote', state: 'unknown' } }) : null,
        jsx('span', { children: `status=${item.status || 'unknown'}` }),
        jsx('span', { children: `assignee=${item.ownerProfile ? profileLabel(item.ownerProfile) : (item.assignee || 'unassigned')}` }),
        jsx('span', { children: `priority=${Number(item.priority || 0)}` }),
        jsx('time', { dateTime: updated.dateTime, children: `updated=${updated.text}` }),
        due ? jsx('time', { dateTime: due.dateTime, children: `due=${due.text}` }) : null
      ] }),
      relationships.length ? jsx('div', { className: 'mt-1 text-xs text-(--ui-text-tertiary)', children: relationships.join(' · ') }) : null,
      item.currentRun ? jsx('div', { className: 'mt-1 text-xs text-(--ui-text-secondary)', children: `run=${item.currentRun.id}:${item.currentRun.status}` }) : null,
      item.blocked ? jsx('div', { className: 'mt-1 text-xs text-(--ui-text-secondary)', children: `blocked=${item.blockKind || 'true'}` }) : null
    ]
  })
}

const AI_SOURCE_LABELS = {
  codex: '코덱스', 'claude-desktop': '클로드 데스크톱', 'claude-code': 'Claude Code',
  'codex-cloud': '코덱스(원격)', 'claude-cloud': '클로드(원격)', reports: '원격 요약'
}

function AISessionCard({ item }) {
  const updated = formatActivity(item.updatedAt)
  const source = AI_SOURCE_LABELS[item.source] || item.source || '알 수 없음'
  const conclusion = Array.isArray(item.conclusion) ? item.conclusion.filter(Boolean).slice(0, 3) : []
  const place = String(item.source || '').endsWith('-cloud') ? '프로젝트' : '폴더'
  const meta = [source, item.folder ? `${place} ${item.folder}` : null, item.model || null, item.archived ? '보관됨' : null].filter(Boolean).join(' · ')
  return jsxs('article', {
    'data-ai-session': item.id,
    className: 'rounded-md border border-(--ui-stroke-secondary) p-3',
    children: [
      jsxs('div', { className: 'flex flex-wrap items-start justify-between gap-2', children: [
        jsx('strong', { className: 'text-sm', 'data-ai-session-title': true, children: item.title || item.firstMessage || '제목 없음' }),
        jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: item.status || '알 수 없음' })
      ] }),
      jsx('div', { className: 'mt-1 text-xs text-(--ui-text-secondary)', children: meta }),
      jsx('time', { className: 'mt-1 block text-xs text-(--ui-text-tertiary)', dateTime: updated.dateTime, children: `최종 활동 ${updated.text}` }),
      item.firstMessage && item.firstMessage !== item.title
        ? jsx('div', { className: 'mt-1 line-clamp-1 text-xs text-(--ui-text-secondary)', children: `요청: ${item.firstMessage}` })
        : null,
      jsxs('div', {
        'data-ai-session-conclusion': true,
        className: 'mt-2 border-t border-(--ui-stroke-secondary) pt-2 text-xs text-(--ui-text-secondary)',
        children: [
          jsx('div', { className: 'mb-1 font-semibold', children: item.status === '진행 중' ? '최근 결론 (진행 중)' : '결론' }),
          ...(conclusion.length
            ? conclusion.map((line, index) => jsx('div', { className: 'mt-0.5', children: line }, `conclusion-${index}`))
            : [jsx('div', { className: 'mt-0.5 text-(--ui-text-tertiary)', children: '기록된 결론이 아직 없습니다.' }, 'conclusion-empty')])
        ]
      })
    ]
  })
}

function AISessionPanel({ queryText, setQueryText, aiSource, setAiSource, includeArchive, setIncludeArchive, resetPaging, collectedAt, sources }) {
  const collected = collectedAt ? formatActivity(collectedAt).text : '아직 수집 전'
  const statusText = Object.entries(sources || {}).map(([name, info]) =>
    `${AI_SOURCE_LABELS[name] || name} ${info && info.ok ? `${info.count}건` : /_missing$/.test(info?.error || '') ? '없음(이 PC에 기록 없음)' : `확인 불가${info && info.error ? `(${info.error})` : ''}`}`
  ).join(' · ')
  return jsxs('div', {
    className: 'grid gap-2 border-b border-(--ui-stroke-secondary) px-4 py-2 text-xs text-(--ui-text-secondary)',
    children: [
      jsxs('div', { className: 'flex flex-wrap items-end gap-2', children: [
        jsxs('label', { className: 'grid min-w-56 flex-1 gap-1', children: [
          jsx('span', { children: '제목·요청·결론 검색' }),
          jsx('input', {
            type: 'search', 'aria-label': 'Search AI sessions', placeholder: '예: 헤르메스, 슬라이드',
            value: queryText, onChange: event => { setQueryText(event.target.value); resetPaging() },
            className: 'rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-sm'
          })
        ] }),
        jsxs('label', { className: 'grid gap-1', children: [
          jsx('span', { children: '출처' }),
          jsx('select', {
            'aria-label': 'AI session source', value: aiSource,
            onChange: event => { setAiSource(event.target.value); resetPaging() },
            className: 'rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-sm',
            children: [
              jsx('option', { value: 'all', children: '전체' }, 'all'),
              jsx('option', { value: 'codex', children: '코덱스' }, 'codex'),
              jsx('option', { value: 'claude', children: '클로드' }, 'claude'),
              jsx('option', { value: 'remote', children: '원격(클라우드)' }, 'remote')
            ]
          })
        ] }),
        jsxs('label', { className: 'flex items-center gap-1 pb-1', children: [
          jsx('input', { type: 'checkbox', 'aria-label': 'Include archived AI sessions', checked: includeArchive,
            onChange: event => { setIncludeArchive(event.target.checked); resetPaging() } }),
          jsx('span', { children: '보관된 세션 포함' })
        ] })
      ] }),
      jsx('div', { 'data-ai-collected': true, children: `마지막 수집 ${collected} · 30분마다 자동 갱신${statusText ? ` · ${statusText}` : ''}` })
    ]
  })
}

async function copySkillPath(ctx, path) {
  if (!path || typeof ctx.os?.writeClipboard !== 'function') return false
  const copied = await ctx.os.writeClipboard(path)
  if (copied && host.notify) host.notify({ kind: 'info', message: 'Skill path copied.' })
  return Boolean(copied)
}

function SkillPathRow({ ctx, item, copy, label }) {
  return jsxs('li', {
    className: 'grid gap-1 rounded border border-(--ui-stroke-secondary) p-2 text-xs',
    children: [
      jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-2', children: [
        jsx('span', { className: 'font-medium', children: `${label} · ${copy.status}` }),
        jsx('button', {
          type: 'button',
          'aria-label': `Copy path ${label} ${item.title}`,
          className: 'rounded border border-(--ui-stroke-secondary) px-2 py-1',
          onClick: () => copySkillPath(ctx, copy.absolutePath),
          children: 'Copy path'
        })
      ] }),
      jsx('code', { className: 'break-all text-(--ui-text-tertiary)', children: copy.absolutePath })
    ]
  })
}

function SkillCard({ item, ctx }) {
  const canonical = item.canonical || { status: 'unverified', evidence: 'unknown', absolutePath: null }
  const canonicalCopy = canonical.absolutePath
    ? { status: canonical.status, absolutePath: canonical.absolutePath }
    : null
  return jsxs('article', {
    'data-skill-card': item.id,
    className: 'grid gap-3 rounded-md border border-(--ui-stroke-secondary) p-3',
    children: [
      jsxs('div', { className: 'flex flex-wrap items-start justify-between gap-2', children: [
        jsxs('div', { children: [
          jsx('h2', { className: 'text-sm font-semibold', children: item.title }),
          jsx('p', { className: 'mt-1 text-xs text-(--ui-text-secondary)', children: item.description || '설명 없음' })
        ] }),
        jsx('div', {
          className: 'flex flex-wrap gap-1',
          children: (item.statuses || []).map(status => jsx('span', {
            className: 'rounded border border-(--ui-stroke-secondary) px-1.5 py-0.5 text-xs text-(--ui-text-secondary)',
            children: status
          }, status))
        })
      ] }),
      jsxs('div', { className: 'grid gap-1 text-xs', children: [
        jsx('div', { children: `배포 프로필: ${(item.deployedProfiles || []).join(', ') || '없음'}` }),
        jsx('div', { children: `정본 ${canonical.status} · evidence=${canonical.evidence || 'none'}` }),
        item.triggers ? jsx('div', { className: 'text-(--ui-text-secondary)', children: `trigger=${item.triggers}` }) : null,
        item.tags?.length ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: `tags=${item.tags.join(', ')}` }) : null,
        jsx('div', { className: 'text-(--ui-text-secondary)', children: `match=${item.matchReason || '전체 목록'}` }),
        jsx('p', { className: 'text-(--ui-text-tertiary)', children: item.snippet || '' })
      ] }),
      canonicalCopy ? jsx('ul', {
        'aria-label': `Canonical path ${item.title}`,
        children: jsx(SkillPathRow, { ctx, item, copy: canonicalCopy, label: 'canonical' })
      }) : null,
      jsx('ul', {
        className: 'grid gap-2',
        'aria-label': `Runtime paths ${item.title}`,
        children: (item.runtimeCopies || []).map(copy => jsx(SkillPathRow, {
          ctx, item, copy, label: copy.profile || 'runtime'
        }, `${copy.profile}:${copy.absolutePath}`))
      }),
      item.otherCopies?.length ? jsx('details', { children: [
        jsx('summary', { className: 'cursor-pointer text-xs', children: `기타 사본 (${item.otherCopies.length})` }),
        jsx('ul', { className: 'mt-2 grid gap-2', children: item.otherCopies.map((copy, index) => jsx(SkillPathRow, {
          ctx, item, copy, label: copy.profile || copy.status || `copy-${index + 1}`
        }, `${copy.status}:${copy.absolutePath}`)) })
      ] }) : null
    ]
  })
}

function SkillSearchPanel({ draft, setDraft, submit, includeArchive, setIncludeArchive, refresh, refreshing, refreshedAt }) {
  const refreshed = refreshedAt ? formatActivity(refreshedAt).text : '아직 갱신 안 됨'
  return jsxs('div', {
    className: 'grid gap-3 border-b border-(--ui-stroke-secondary) px-4 py-3',
    children: [
      jsxs('div', { className: 'flex flex-wrap items-end gap-2', children: [
        jsxs('label', { className: 'grid min-w-64 flex-1 gap-1 text-xs text-(--ui-text-secondary)', children: [
          jsx('span', { children: 'Search skills' }),
          jsx('input', {
            type: 'search', value: draft, 'aria-label': 'Search skills',
            placeholder: '제목, 설명, trigger, tags, 본문 검색',
            className: 'rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1.5 text-sm',
            onChange: event => setDraft(event.target.value),
            onKeyDown: event => {
              if (event.key === 'Enter') { event.preventDefault(); submit() }
            }
          })
        ] }),
        jsx('button', {
          type: 'button', 'aria-label': 'Submit skill search', onClick: submit,
          className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm',
          children: 'Search'
        }),
        jsx('button', {
          type: 'button', 'aria-label': 'Refresh skills', 'aria-busy': refreshing,
          onClick: refresh,
          className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm',
          children: refreshing ? 'Refreshing…' : 'Refresh'
        })
      ] }),
      jsxs('div', { className: 'flex flex-wrap items-center justify-between gap-3 text-xs text-(--ui-text-secondary)', children: [
        jsx(CheckOption, {
          label: '보관/백업 포함', ariaLabel: '보관/백업 포함', checked: includeArchive,
          onChange: event => setIncludeArchive(event.target.checked)
        }),
        jsx('time', { children: `마지막 갱신: ${refreshed}` })
      ] })
    ]
  })
}

function SessionLinkPanel({ title, actionLabel, link, onOpen }) {
  const state = link?.state || 'absent'
  return jsxs('section', {
    className: 'grid gap-1 rounded-md border border-(--ui-stroke-secondary) p-2',
    children: [
      jsx('h3', { className: 'font-medium', children: title }),
      jsx('div', { children: `state=${state}` }),
      link?.ownerProfile ? jsx('div', { children: `profile=${link.ownerProfile}` }) : null,
      link?.storedSessionId ? jsx('div', { className: 'break-all', children: `session=${link.storedSessionId}` }) : null,
      link?.runId ? jsx('div', { children: `run=${link.runId}` }) : null,
      link?.reason ? jsx('p', { className: 'text-(--ui-text-secondary)', children: link.reason }) : null,
      link?.available ? jsx('button', {
        type: 'button', 'aria-label': actionLabel,
        className: 'mt-1 rounded-md border border-(--ui-stroke-secondary) px-2 py-1 text-left',
        onClick: () => onOpen(link), children: actionLabel
      }) : null
    ]
  })
}

function runText(label, run) {
  if (!run) return `${label}=none`
  return `${label}=${run.id}:${run.status} · profile=${run.profile || 'unknown'} · started=${formatActivity(run.startedAt).text}`
}

function TaskLinkList({ label, ids, onOpen }) {
  const relation = label === 'parents' ? 'parent' : 'child'
  return jsxs('div', { className: 'flex flex-wrap items-center gap-1', children: [
    jsx('span', { children: `${label}=` }),
    ids?.length ? ids.map(id => jsx('button', {
      type: 'button', 'aria-label': `Open ${relation} task ${id}`,
      className: 'rounded border border-(--ui-stroke-secondary) px-1',
      onClick: () => onOpen(id), children: id
    }, id)) : jsx('span', { children: 'none' })
  ] })
}

function KanbanDetailDrawer({ detail, failure, onClose, onOpenSession, onOpenTask, onOpenBoard }) {
  if (!detail) return null
  const created = formatActivity(detail.createdAt)
  const updated = formatActivity(detail.updatedAt)
  const due = detail.dueAt ? formatActivity(detail.dueAt) : null
  return jsxs('aside', {
    role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Kanban detail drawer', tabIndex: -1,
    onKeyDown: event => {
      if (event.key === 'Escape') { event.stopPropagation?.(); onClose() }
    },
    className: 'fixed inset-y-0 right-0 z-30 isolate w-full max-w-xl overflow-auto border-l border-(--ui-stroke-secondary) bg-(--theme-elevated-seed) p-4 opacity-100 shadow-xl lg:static lg:z-auto lg:max-w-none lg:shadow-none',
    style: { backgroundColor: 'var(--theme-elevated-seed, Canvas)', isolation: 'isolate', opacity: 1 },
    children: [
      jsxs('header', { className: 'flex items-start justify-between gap-3', children: [
        jsxs('div', { children: [
          jsx('h2', { className: 'font-semibold', children: detail.title || detail.id }),
          jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: `${detail.board} · ${detail.id}` })
        ] }),
        jsx('button', { type: 'button', 'aria-label': 'Close Kanban detail', onClick: onClose,
          className: 'rounded-md border border-(--ui-stroke-secondary) px-2 py-1', children: '×' })
      ] }),
      jsxs('div', { className: 'mt-3 grid gap-1 text-xs', children: [
        jsx('div', { children: `ID=${detail.id}` }),
        jsx('div', { children: `board=${detail.board}` }),
        jsx('div', { children: `status=${detail.status || 'unknown'} · assignee=${detail.assignee || 'unassigned'} · priority=${Number(detail.priority || 0)}` }),
        jsx('div', { children: `created=${created.text}` }),
        jsx('div', { children: `updated=${updated.text}` }),
        jsx('div', { children: `due=${due ? due.text : 'none'}` }),
        jsx('div', { children: `project=${detail.projectId || 'none'}` }),
        jsx('div', { children: `sourceInstance=${detail.sourceInstance || 'local/unknown'}` }),
        jsx(TaskLinkList, { label: 'parents', ids: detail.parentIds, onOpen: onOpenTask }),
        jsx(TaskLinkList, { label: 'children', ids: detail.childIds, onOpen: onOpenTask }),
        jsx('div', { children: runText('currentRun', detail.currentRun) }),
        jsx('div', { children: runText('latestRun', detail.latestRun) })
      ] }),
      jsxs('section', { className: 'mt-3 grid gap-1', children: [
        jsx('h3', { className: 'text-sm font-medium', children: 'Description' }),
        jsx('p', { className: 'whitespace-pre-wrap break-words text-xs text-(--ui-text-secondary)', children: detail.body || 'No description.' })
      ] }),
      jsxs('div', { className: 'mt-3 grid gap-2 text-xs', children: [
        jsx(SessionLinkPanel, { title: 'Origin / parent Hermes session', actionLabel: '상위 세션으로 이동', link: detail.originSession, onOpen: onOpenSession }),
        jsx(SessionLinkPanel, { title: 'Latest worker execution session', actionLabel: '작업 실행 세션으로 이동', link: detail.latestWorkerSession, onOpen: onOpenSession })
      ] }),
      failure ? jsxs('div', { role: 'alert', className: 'mt-3 rounded-md border border-(--ui-stroke-secondary) p-2 text-sm', children: [
        jsx('p', { children: failure.reason || 'Session is no longer available.' }),
        failure.recoveryLabel && typeof failure.recover === 'function' ? jsx('button', {
          type: 'button', 'aria-label': '복구용 새 세션', onClick: failure.recover,
          className: 'mt-2 rounded-md border border-(--ui-stroke-secondary) px-2 py-1', children: '복구용 새 세션'
        }) : null
      ] }) : null,
      jsx('button', { type: 'button', 'aria-label': 'Kanban 보드 열기', onClick: onOpenBoard,
        className: 'mt-3 rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm', children: 'Kanban 보드 열기' })
    ]
  })
}

function FilterPanel({
  tab,
  queryText,
  setQueryText,
  profiles,
  setProfiles,
  dateFrom,
  setDateFrom,
  setHistoryScope,
  setPresetDateFrom,
  dateTo,
  setDateTo,
  telegramTypes,
  setTelegramTypes,
  kanbanStatuses,
  setKanbanStatuses,
  resetPaging
}) {
  return jsxs('div', {
    className: 'grid gap-3 border-b border-(--ui-stroke-secondary) px-4 py-3',
    children: [
      jsxs('label', {
        className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
        children: [
          jsx('span', { children: 'Search title or content' }),
          jsx('input', {
            type: 'search',
            value: queryText,
            'aria-label': 'Search title or content',
            placeholder: 'Search title or content',
            className: 'rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1.5 text-sm',
            onChange: event => {
              setQueryText(event.target.value)
              resetPaging()
            }
          })
        ]
      }),
      jsxs('fieldset', {
        className: 'flex flex-wrap gap-x-5 gap-y-2',
        children: [
          jsx('legend', { className: 'mb-1 text-xs text-(--ui-text-tertiary)', children: tab === 'kanban' ? 'Assignees' : 'Profiles' }),
          ...PROFILE_GROUPS.map(group => {
            const groupState = groupSelectionState(profiles, group.profiles)
            return jsxs('div', {
              'data-profile-group': group.id,
              className: 'flex flex-wrap items-center gap-3',
              children: [
                jsx(CheckOption, {
                  label: `${group.label}:`,
                  ariaLabel: `${group.label} 전체`,
                  checked: groupState.checked,
                  indeterminate: groupState.indeterminate,
                  onChange: event => {
                    const next = toggleGroupProfiles(profiles, group.profiles, event.target.checked)
                    if (next.length && next !== profiles) {
                      setProfiles(next)
                      resetPaging()
                    }
                  }
                }),
                ...group.profiles.map(profile =>
                  jsx(CheckOption, {
                    label: PROFILE_LABELS[profile] || profile,
                    ariaLabel: `Profile ${profile}`,
                    checked: profiles.includes(profile),
                    onChange: event => {
                      const next = toggleValue(profiles, profile, event.target.checked)
                      if (next.length) {
                        setProfiles(next)
                        resetPaging()
                      }
                    }
                  }, profile)
                )
              ]
            }, group.id)
          })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap gap-3',
        children: [
          jsxs('label', {
            className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
            children: [
              jsx('span', { children: 'From date' }),
              jsx('input', {
                type: 'date', value: dateFrom, 'aria-label': 'From date',
                className: 'rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1',
                onChange: event => {
                  setDateFrom(event.target.value)
                  setHistoryScope('custom')
                  setPresetDateFrom(null)
                  resetPaging()
                }
              })
            ]
          }),
          jsxs('label', {
            className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
            children: [
              jsx('span', { children: 'To date' }),
              jsx('input', {
                type: 'date', value: dateTo, 'aria-label': 'To date',
                className: 'rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1',
                onChange: event => { setDateTo(event.target.value); resetPaging() }
              })
            ]
          })
        ]
      }),
      tab === 'telegram'
        ? jsxs('fieldset', {
            className: 'flex flex-wrap gap-3',
            children: [
              jsx('legend', { className: 'mb-1 text-xs text-(--ui-text-tertiary)', children: 'Telegram type' }),
              ...TELEGRAM_TYPES.map(type =>
                jsx(CheckOption, {
                  label: type,
                  ariaLabel: `Telegram type ${type}`,
                  checked: telegramTypes.includes(type),
                  onChange: event => {
                    setTelegramTypes(toggleValue(telegramTypes, type, event.target.checked))
                    resetPaging()
                  }
                }, type)
              )
            ]
          })
        : null,
      tab === 'kanban'
        ? jsxs('fieldset', {
            className: 'flex flex-wrap gap-3',
            children: [
              jsx('legend', { className: 'mb-1 text-xs text-(--ui-text-tertiary)', children: 'Kanban status' }),
              ...KANBAN_STATUSES.map(status =>
                jsx(CheckOption, {
                  label: status,
                  ariaLabel: `Kanban status ${status}`,
                  checked: kanbanStatuses.includes(status),
                  onChange: event => {
                    setKanbanStatuses(toggleValue(kanbanStatuses, status, event.target.checked))
                    resetPaging()
                  }
                }, status)
              )
            ]
          })
        : null
    ]
  })
}

export function ProjectHubPage({ ctx }) {
  const backend = createBackend(ctx)
  const [selectedProjectId, setSelectedProjectId] = useState(null)
  const [selectedTask, setSelectedTask] = useState(null)
  const [nextAction, setNextAction] = useState('')
  const [openFailure, setOpenFailure] = useState(null)
  const [opener] = useState(() => createOpenController({ backend, host }))
  const portfolioQuery = useQuery({
    queryKey: ['session-hub', 'projects'],
    queryFn: () => backend.projects(),
    refetchInterval: PROJECT_POLL_MS,
    staleTime: 5000,
    retry: 1
  })
  const detailQuery = useQuery({
    queryKey: ['session-hub', 'project', selectedProjectId],
    queryFn: () => backend.projectDetail(selectedProjectId),
    enabled: Boolean(selectedProjectId),
    refetchInterval: PROJECT_POLL_MS,
    retry: 1
  })
  const projects = Array.isArray(portfolioQuery.data?.projects) ? portfolioQuery.data.projects : []
  const actions = Array.isArray(portfolioQuery.data?.actions) ? portfolioQuery.data.actions : []
  const detail = selectedProjectId && detailQuery.data?.id === selectedProjectId ? detailQuery.data : null
  const links = new Map((detail?.links || []).map(link => [link.sourceId, link]))
  const [actionError, setActionError] = useState(null)
  const kanban = createKanbanController({
    openSession: async selection => {
      const result = await opener.open(selection)
      setOpenFailure(result?.recoveryLabel ? result : null)
      return result
    },
    showTask: task => setSelectedTask(task),
    resolveOrigin: task => resolveKanbanOrigin(backend, task)
  })
  const saveNextAction = async () => {
    if (!selectedProjectId || !nextAction.trim()) return
    setActionError(null)
    try {
      const result = await backend.updateProject(selectedProjectId, { nextAction: nextAction.trim() })
      if (result && result.ok === false) throw new Error(result.error || 'update_failed')
      setNextAction('')
      await queryClient.invalidateQueries({ queryKey: ['session-hub'] })
    } catch (_error) {
      setActionError('다음 행동을 저장하지 못했습니다. 입력값을 확인한 뒤 다시 시도하세요.')
    }
  }
  const refreshRegistry = async () => {
    setActionError(null)
    try {
      const result = await backend.refreshProjects()
      if (result && result.ok === false) throw new Error(result.error || 'refresh_failed')
      await queryClient.invalidateQueries({ queryKey: ['session-hub'] })
    } catch (_error) {
      setActionError('프로젝트 레지스트리를 새로고침하지 못했습니다.')
    }
  }
  const openOrigin = async task => {
    setActionError(null)
    const result = await kanban.openOrigin(task)
    if (!result?.opened && !result?.recoveryLabel && result?.reason) setActionError(result.reason)
  }

  return jsxs('section', {
    className: 'grid gap-4 border-b border-(--ui-stroke-secondary) p-4',
    'data-project-hub': true,
    children: [
      jsxs('header', {
        className: 'flex items-center justify-between gap-3',
        children: [
          jsxs('div', { children: [
            jsx('h1', { className: 'text-base font-semibold', children: 'Project Hub' }),
            jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: '프로젝트 → 다음 행동 → 정확한 원본을 3클릭 이내에 엽니다.' })
          ] }),
          jsx('button', {
            type: 'button', 'aria-label': 'Refresh project registry',
            className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm',
            onClick: refreshRegistry,
            children: 'Refresh'
          })
        ]
      }),
      jsx('details', { open: true, children: [
        jsx('summary', { className: 'cursor-pointer text-sm font-medium', children: '전체 세션 이력' }),
        jsx(SessionHubPage, { ctx })
      ] })
    ]
  })
}

export function SessionHubPage({ ctx }) {
  const backend = createBackend(ctx)
  const initialHistory = historyWindowSelection(DEFAULT_HISTORY_SCOPE, Math.floor(Date.now() / 1000))
  const [tab, setTab] = useState('desktop')
  const [queryText, setQueryText] = useState('')
  const [skillDraft, setSkillDraft] = useState('')
  const [skillQuery, setSkillQuery] = useState('')
  const [includeArchive, setIncludeArchive] = useState(false)
  const [skillRefreshing, setSkillRefreshing] = useState(false)
  useState(() => (HUB_CONFIG_STATE.loaded ? null : loadHubConfig(ctx)))
  const [profileSelection, setProfiles] = useState(null)
  // null = every profile the backend reported (the list arrives asynchronously from GET /config).
  const knownSelection = Array.isArray(profileSelection) ? profileSelection.filter(profile => PROFILE_NAMES.includes(profile)) : []
  const profiles = knownSelection.length ? knownSelection : [...PROFILE_NAMES]
  const [historyScope, setHistoryScope] = useState(DEFAULT_HISTORY_SCOPE)
  const [presetDateFrom, setPresetDateFrom] = useState(initialHistory.dateFrom)
  const [dateFrom, setDateFrom] = useState(() => dateInputValue(initialHistory.dateFrom))
  const [dateTo, setDateTo] = useState('')
  const [telegramTypes, setTelegramTypes] = useState([])
  const [kanbanStatuses, setKanbanStatuses] = useState([])
  const [aiSource, setAiSource] = useState('all')
  const [aiRefreshing, setAiRefreshing] = useState(false)
  const [aiRefreshError, setAiRefreshError] = useState('')
  const [pageSize, setPageSize] = useState(() => getInitialPageSize(ctx.storage))
  const [cursor, setCursor] = useState(null)
  const [cursorHistory, setCursorHistory] = useState([])
  const [openFailure, setOpenFailure] = useState(null)
  const [selectedKanban, setSelectedKanban] = useState(null)
  const [kanbanFailure, setKanbanFailure] = useState(null)
  const [opener] = useState(() => createOpenController({ backend, host }))
  const kanban = createKanbanController({
    showTask: async task => {
      setKanbanFailure(null)
      setSelectedKanban({ ...task, loading: true })
      try {
        setSelectedKanban(await backend.kanbanCard(task.board, task.id))
      } catch (_error) {
        setKanbanFailure({ reason: 'Exact Kanban card detail is unavailable.' })
      }
    }
  })
  const openKanbanSession = async selection => {
    setKanbanFailure(null)
    const result = await opener.open(selection)
    if (!result?.opened) setKanbanFailure(result?.reason ? result : { reason: 'Session is no longer available.' })
    return result
  }
  const closeKanban = () => { setSelectedKanban(null); setKanbanFailure(null) }
  // "Kanban 보드 열기": this Desktop may have no built-in Kanban pane (/kanban falls through to a
  // blank session), so stay inside Session Hub — Kanban tab, card id searched, drawer closed.
  const openBoardForCard = card => {
    const id = card && card.id ? String(card.id) : ''
    closeKanban()
    setTab('kanban')
    setQueryText(id)
    resetPaging()
    host.navigate(SESSION_HUB_ROUTE)
  }

  const resetPaging = () => {
    setCursor(null)
    setCursorHistory([])
  }
  const filters = {
    tab,
    query: tab === 'skills' ? skillQuery.trim() : queryText.trim(),
    includeArchive: tab === 'skills' || tab === 'external-ai' ? includeArchive : false,
    aiSources: tab === 'external-ai' && aiSource !== 'all' ? [aiSource] : [],
    profiles,
    historyScope,
    dateFrom: historyScope === 'full' ? null : historyScope === 'custom' ? dateBoundary(dateFrom) : presetDateFrom,
    dateTo: dateBoundary(dateTo, true),
    telegramTypes: tab === 'telegram' ? telegramTypes : [],
    statuses: tab === 'kanban' ? kanbanStatuses : [],
    pageSize,
    cursor
  }
  const query = useSessionHubData(ctx, filters)
  const data = query.data || { items: [], page: { returned: 0, totalKnown: 0 }, nextCursor: null }
  const items = Array.isArray(data.items) ? data.items : []
  const instances = instancesById(data)
  const branchStatuses = Array.isArray(data.instances) ? data.instances.filter(status => status && status.kind === 'remote') : []
  const skippedLocal = Array.isArray(data.skippedProfiles)
    ? data.skippedProfiles.filter(row => row && typeof row.profile === 'string' && !isRemoteProfile(row.profile))
    : []
  const visibleItems = tab === 'skills' ? items : pageSize === 'all' ? items.slice(0, ALL_WINDOW_ROWS) : items

  const chooseTab = next => {
    setTab(next)
    setSelectedKanban(null)
    resetPaging()
  }
  const chooseHistoryWindow = event => {
    const selection = historyWindowSelection(event.target.value, Math.floor(Date.now() / 1000))
    setHistoryScope(selection.historyScope)
    setPresetDateFrom(selection.dateFrom)
    if (selection.dateFrom !== null) setDateFrom(dateInputValue(selection.dateFrom))
    resetPaging()
  }
  const changePageSize = event => {
    const raw = event.target.value
    const next = raw === 'all' ? 'all' : Number(raw)
    if (!PAGE_SIZE_CHOICES.includes(next)) return
    ctx.storage.set(PAGE_SIZE_KEY, next)
    setPageSize(next)
    resetPaging()
  }
  const nextPage = () => {
    if (!data.nextCursor) return
    setCursorHistory(previous => [...previous.slice(-(CURSOR_HISTORY_LIMIT - 1)), cursor])
    setCursor(data.nextCursor)
  }
  const previousPage = () => {
    if (!cursorHistory.length) return
    const previous = cursorHistory[cursorHistory.length - 1] ?? null
    setCursorHistory(values => values.slice(0, -1))
    setCursor(previous)
  }
  const submitSkillSearch = () => setSkillQuery(skillDraft.trim())
  const refreshSkills = async () => {
    setSkillRefreshing(true)
    try {
      await backend.refreshSkills(filters)
      await query.refetch()
    } finally {
      setSkillRefreshing(false)
    }
  }

  const refreshAiSessions = async () => {
    setAiRefreshing(true)
    setAiRefreshError('')
    try {
      await backend.refreshAiSessions()
    } catch (error) {
      // Collection failures still fall back to the last cached list; explain gate refusals.
      const described = describeBackendError(error)
      setAiRefreshError(/local_desktop_required|plugin_host_isolation/.test(described.headline) ? `새로고침 실패: ${described.detail}` : '')
    } finally {
      await query.refetch()
      setAiRefreshing(false)
    }
  }

  let content
  if (query.isLoading || query.isPending) {
    content = jsx('div', {
      role: 'status', 'aria-live': 'polite',
      className: 'rounded-md border border-(--ui-stroke-secondary) p-6 text-sm text-(--ui-text-secondary)',
      children: tab === 'skills' ? 'Loading skills…' : tab === 'kanban' ? 'Loading Kanban cards…' : tab === 'external-ai' ? '코덱스·클로드 작업 목록을 불러오는 중…' : 'Loading sessions…'
    })
  } else if (query.isError) {
    content = jsxs('div', {
      role: 'alert',
      className: 'rounded-md border border-(--ui-stroke-secondary) p-6 text-sm text-(--ui-text-secondary)',
      children: [
        jsx('p', { children: `${tab === 'skills' ? 'Local skills index' : tab === 'kanban' ? 'Kanban card source' : tab === 'external-ai' ? '코덱스·클로드 작업 목록' : 'Session Hub backend'} ${describeBackendError(query.error).headline}` }),
        jsx('p', { className: 'mt-1 text-xs opacity-80', children: describeBackendError(query.error).detail }),
        jsx('button', {
          type: 'button', 'aria-label': tab === 'skills' ? 'Retry loading skills' : tab === 'kanban' ? 'Retry loading Kanban cards' : 'Retry loading sessions', onClick: () => query.refetch(),
          className: 'mt-3 rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5',
          children: 'Retry'
        })
      ]
    })
  } else if (!visibleItems.length) {
    content = jsx('div', {
      role: 'status', 'aria-live': 'polite',
      className: 'rounded-md border border-(--ui-stroke-secondary) p-6 text-sm text-(--ui-text-secondary)',
      children: tab === 'skills' ? 'No skills match the current search.' : tab === 'kanban' ? 'No Kanban cards match the current filters.' : tab === 'external-ai' ? '조건에 맞는 코덱스·클로드 작업이 없습니다. 새로고침으로 바로 수집할 수 있습니다.' : 'No sessions match the current filters.'
    })
  } else {
    content = jsx('div', {
      className: 'grid gap-2',
      children: visibleItems.map(entry => tab === 'skills'
        ? jsx(SkillCard, { item: entry, ctx }, entry.id)
        : tab === 'external-ai'
        ? jsx(AISessionCard, { item: entry }, entry.id)
        : tab === 'kanban'
        ? jsx(KanbanCard, {
            item: entry,
            instances,
            selected: selectedKanban?.board === entry.board && selectedKanban?.id === entry.id,
            onOpen: task => kanban.open(task)
          }, `${entry.board}:${entry.id}`)
        : jsx(SessionCard, {
            item: entry,
            instances,
            onOpen: async selection => {
              const result = await opener.open(selection)
              setOpenFailure(result?.recoveryLabel ? result : null)
            }
          }, `${entry.ownerProfile}:${entry.storedSessionId}`)
      )
    })
  }

  return jsxs('section', {
    className: 'flex h-full flex-col text-(--ui-text-primary)',
    children: [
      jsxs('header', {
        className: 'flex items-center justify-between border-b border-(--ui-stroke-secondary) px-4 py-3',
        children: [
          jsxs('div', {
            children: [
              jsx('h1', { className: 'text-base font-semibold', children: 'Session Hub' }),
              jsx('div', {
                className: 'text-xs text-(--ui-text-tertiary)',
                children: `${data.page?.returned || 0} ${tab === 'skills' ? 'skill' : tab === 'kanban' ? 'card' : tab === 'external-ai' ? 'AI session' : 'session'} shown · ${data.page?.totalKnown || 0} total`
              }),
              branchStatuses.length ? jsxs('div', {
                'data-instance-strip': true,
                className: 'mt-1 flex flex-wrap items-center gap-2 text-xs text-(--ui-text-tertiary)',
                'aria-label': 'Branch snapshot freshness',
                children: [
                  jsx('span', { children: `${HUB_UI.remoteGroupLabel}:` }),
                  ...branchStatuses.map(status => jsx(FreshnessBadge, { status }, status.id))
                ]
              }) : null,
              skippedLocal.length ? jsx('div', {
                'data-skipped-profiles': true,
                role: 'status',
                className: 'mt-1 text-xs text-(--ui-text-tertiary)',
                children: `읽지 못한 프로필: ${skippedLocal.map(row => `${PROFILE_LABELS[row.profile] || row.profile} (${row.error})`).join(', ')}`
              }) : null
            ]
          }),
          tab === 'skills' ? null : jsxs('div', {
            className: 'flex items-end gap-2',
            children: [
              tab === 'external-ai' ? null : jsxs('label', {
                className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
                children: [
                  jsx('span', { children: 'History window' }),
                  jsx('select', {
                    'aria-label': 'History window', value: historyScope,
                    className: 'rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-sm',
                    onChange: chooseHistoryWindow,
                    children: [
                      historyScope === 'custom' ? jsx('option', { value: 'custom', disabled: true, children: 'Custom date range' }) : null,
                      jsx('option', { value: '7d', children: 'Last 7 days' }),
                      jsx('option', { value: '14d', children: 'Last 14 days' }),
                      jsx('option', { value: '30d', children: 'Last 30 days' }),
                      jsx('option', { value: 'full', children: 'Full retained history' })
                    ].filter(Boolean)
                  })
                ]
              }),
              jsxs('label', {
                className: 'grid gap-1 text-xs text-(--ui-text-secondary)',
                children: [
                  jsx('span', { children: tab === 'kanban' ? 'Cards per page' : tab === 'external-ai' ? 'AI sessions per page' : 'Sessions per page' }),
                  jsx('select', {
                    'aria-label': tab === 'kanban' ? 'Cards per page' : tab === 'external-ai' ? 'AI sessions per page' : 'Sessions per page', value: String(pageSize), onChange: changePageSize,
                    className: 'rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-sm',
                    children: PAGE_SIZE_CHOICES.map(choice =>
                      jsx('option', { value: String(choice), children: choice === 'all' ? 'All (100 per window)' : String(choice) }, String(choice))
                    )
                  })
                ]
              }),
              jsx('button', {
                type: 'button', 'aria-label': tab === 'kanban' ? 'Refresh Kanban cards' : tab === 'external-ai' ? 'Refresh AI sessions' : 'Refresh sessions', 'aria-busy': Boolean(query.isFetching),
                className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm',
                onClick: () => (tab === 'external-ai' ? refreshAiSessions() : query.refetch()),
                children: query.isFetching || aiRefreshing ? 'Refreshing…' : 'Refresh'
              }),
              tab === 'external-ai' && aiRefreshError ? jsx('p', {
                role: 'status', className: 'basis-full text-xs text-(--ui-text-secondary)', children: aiRefreshError
              }) : null,
              jsx('span', {
                'aria-live': 'polite', className: 'sr-only',
                children: query.isFetching ? `Refreshing ${tab === 'kanban' ? 'Kanban cards' : tab === 'external-ai' ? 'AI sessions' : 'sessions'}` : `${tab === 'kanban' ? 'Kanban card' : tab === 'external-ai' ? 'AI session' : 'Session'} list ready`
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        role: 'tablist', 'aria-label': 'Session source',
        className: 'flex gap-2 border-b border-(--ui-stroke-secondary) px-4 py-2',
        children: [
          jsx(TabButton, { value: 'desktop', active: tab === 'desktop', onSelect: chooseTab, children: 'Desktop' }),
          jsx(TabButton, { value: 'telegram', active: tab === 'telegram', onSelect: chooseTab, children: 'Telegram' }),
          jsx(TabButton, { value: 'kanban', active: tab === 'kanban', onSelect: chooseTab, children: 'Kanban' }),
          jsx(TabButton, { value: 'external-ai', active: tab === 'external-ai', onSelect: chooseTab, children: '코덱스·클로드' }),
          jsx(TabButton, { value: 'skills', active: tab === 'skills', onSelect: chooseTab, children: 'Skills' })
        ]
      }),
      tab === 'skills' ? jsx(SkillSearchPanel, {
        draft: skillDraft, setDraft: setSkillDraft, submit: submitSkillSearch,
        includeArchive, setIncludeArchive, refresh: refreshSkills,
        refreshing: skillRefreshing || Boolean(query.isFetching), refreshedAt: data.refreshedAt
      }) : tab === 'external-ai' ? jsx(AISessionPanel, {
        queryText, setQueryText, aiSource, setAiSource, includeArchive, setIncludeArchive, resetPaging,
        collectedAt: data.collectedAt, sources: data.sources
      }) : jsx(FilterPanel, {
        tab, queryText, setQueryText, profiles, setProfiles, dateFrom, setDateFrom,
        setHistoryScope, setPresetDateFrom, dateTo, setDateTo, telegramTypes, setTelegramTypes,
        kanbanStatuses, setKanbanStatuses, resetPaging
      }),
      jsxs('main', {
        className: 'relative min-h-0 flex-1 overflow-auto p-4',
        'aria-busy': Boolean(query.isLoading || query.isFetching),
        children: [
          openFailure ? jsxs('div', {
            role: 'alert',
            className: 'mb-3 rounded-md border border-(--ui-stroke-secondary) p-3 text-sm',
            children: [
              jsx('p', { children: openFailure.reason }),
              jsx('button', {
                type: 'button', 'aria-label': '복구용 새 세션', onClick: openFailure.recover,
                className: 'mt-2 rounded-md border border-(--ui-stroke-secondary) px-2 py-1',
                children: '복구용 새 세션'
              })
            ]
          }) : null,
          jsxs('div', {
            className: selectedKanban ? 'grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(22rem,38%)]' : 'grid',
            children: [
              jsxs('section', { className: 'min-w-0', children: [
                content,
                tab === 'skills' ? null : jsxs('nav', {
                  'aria-label': tab === 'kanban' ? 'Kanban card pages' : tab === 'external-ai' ? 'AI session pages' : 'Session pages', className: 'mt-3 flex justify-between gap-2',
                  children: [
                    jsx('button', {
                      type: 'button', 'aria-label': 'First page', disabled: cursor === null,
                      className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm disabled:opacity-50',
                      onClick: resetPaging, children: 'First'
                    }),
                    jsx('button', {
                      type: 'button', 'aria-label': 'Previous page', disabled: cursorHistory.length === 0,
                      className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm disabled:opacity-50',
                      onClick: previousPage, children: 'Previous'
                    }),
                    jsx('button', {
                      type: 'button', 'aria-label': 'Next page', disabled: !data.nextCursor,
                      className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm disabled:opacity-50',
                      onClick: nextPage, children: 'Next'
                    })
                  ]
                })
              ] }),
              selectedKanban ? jsx('button', {
                type: 'button', 'aria-label': 'Close Kanban detail backdrop', onClick: closeKanban,
                className: 'fixed inset-0 z-20 bg-black/30 lg:hidden', children: ''
              }) : null,
              jsx(KanbanDetailDrawer, {
                detail: selectedKanban, failure: kanbanFailure, onClose: closeKanban,
                onOpenSession: openKanbanSession,
                onOpenTask: id => kanban.open({ board: selectedKanban.board, id }),
                onOpenBoard: () => openBoardForCard(selectedKanban)
              })
            ]
          })
        ]
      })
    ]
  })
}

export default {
  id: 'session-hub',
  name: 'Session Hub',
  defaultEnabled: false,
  register(ctx) {
    const disposeEvents = registerEventRefresh(host)
    if (typeof ctx.onDispose === 'function') ctx.onDispose(disposeEvents)
    loadHubConfigWithRetry(ctx)
    ctx.registerMany([
      {
        id: 'page', area: ROUTES_AREA, data: { path: SESSION_HUB_ROUTE },
        render: () => jsx(ProjectHubPage, { ctx })
      },
      {
        id: 'nav', area: SIDEBAR_NAV_AREA, order: 70,
        data: { path: SESSION_HUB_ROUTE, label: 'Session Hub', codicon: 'list-tree' }
      },
      {
        id: 'open', area: PALETTE_AREA,
        data: {
          id: 'sessionHub.open', label: 'Open Session Hub', keywords: ['session', 'hub'],
          run: () => host.navigate(SESSION_HUB_ROUTE)
        }
      }
    ])
  }
}
