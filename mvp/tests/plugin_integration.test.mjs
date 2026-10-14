import assert from 'node:assert/strict'
import { test } from 'node:test'

import plugin, { applyHubConfig, createOpenController, HUB_CONFIG_STATE, loadHubConfig, loadHubConfigWithRetry, PROFILE_GROUPS, PROFILE_NAMES } from '../plugins/session-hub/desktop/plugin.js'
import { createHarness, textContent } from './fixtures/renderer.mjs'
import { TEST_HUB_CONFIG } from './fixtures/hub-config.mjs'

const profiles = ['default', 'ops', 'review', 'builder', 'search', 'X/default', 'Y/default', 'Z/default']

function item(index = 1, overrides = {}) {
  return {
    ownerProfile: 'review',
    storedSessionId: `fixture-${index}`,
    title: `Fixture title ${index}`,
    source: 'telegram',
    telegramType: 'topic',
    lastActivity: 1787770000 + index,
    messageCount: 4,
    previews: ['recent one', 'recent two', 'recent three'],
    ...overrides
  }
}

test('telegram cards render question, first reply, pending state, and original-session click target', async () => {
  const opened = []
  const turn = item(1, {
    storedSessionId: 'telegram-origin-session',
    title: '질문 제목',
    questionText: '질문 제목 전체 원문',
    replyPreview: '첫 응답 첫 줄',
    responseState: 'answered',
    messageId: 77,
    userMessageId: 77,
    platformMessageId: 'tg-77',
    previews: undefined
  })
  const { harness } = mountPlugin(queryResult({
    data: { items: [turn], page: { returned: 1, totalKnown: 1 }, nextCursor: null }
  }), {
    host: { async openSession(id, options) { opened.push([id, options]) }, notify() {} }
  })

  byRole(harness, 'tab', 'Telegram').props.onClick()
  assert.match(harness.text(), /질문 제목/)
  assert.match(harness.text(), /첫 응답 첫 줄/)
  assert.match(harness.text(), /telegram · topic/)
  assert.match(harness.text(), /KST/)
  assert.match(harness.text(), /응답 완료/)
  assert.ok(byLabel(harness, 'Open Telegram question 질문 제목'))
  const question = byLabel(harness, 'Telegram question 질문 제목')
  assert.equal(question.props.title, '질문 제목 전체 원문')
  assert.match(question.props.className, /line-clamp-2/)
  assert.match(question.props.className, /text-base/)
  assert.match(question.props.className, /font-semibold/)
  const answered = harness.find(node => node.props?.['data-reply-state'] === 'answered', 'answered reply')
  assert.match(answered.props.className, /line-clamp-2/)
  await byLabel(harness, 'Open Telegram question 질문 제목').props.onClick()
  assert.equal(opened[0][0], 'telegram-origin-session')

  harness.setQuery(queryResult({ data: { items: [{ ...turn, responseState: 'pending', replyPreview: null }], page: { returned: 1, totalKnown: 1 }, nextCursor: null } }))
  assert.match(harness.text(), /응답 대기 중/)
  assert.ok(harness.find(node => node.props?.['data-reply-state'] === 'pending', 'pending reply'))
})

function queryResult(overrides = {}) {
  return {
    data: { items: [item()], page: { returned: 1, totalKnown: 2 }, nextCursor: 'cursor-next' },
    isLoading: false,
    isError: false,
    isFetching: false,
    error: null,
    refetch() {},
    ...overrides
  }
}

function mountPlugin(result = queryResult(), options = {}) {
  const registered = []
  const restCalls = []
  const storageWrites = []
  const ctx = {
    storage: {
      get(key) { return key === 'pageSize' ? 20 : undefined },
      set(key, value) { storageWrites.push([key, value]) }
    },
    rest: async (path, requestOptions) => {
      if (path === '/config') return TEST_HUB_CONFIG
      restCalls.push([path, requestOptions])
      if (options.rest) return options.rest(path, requestOptions)
      if (path === '/revalidate') return requestOptions.body
      if (path.startsWith('/kanban/')) return result.data?.items?.[0]
      return result.data
    },
    os: options.os || { async writeClipboard() { return true } },
    registerMany(entries) { registered.push(...entries) }
  }
  const refreshes = []
  const harness = createHarness({
    queryResult: { ...result, refetch: () => refreshes.push('refresh') },
    host: options.host
  })
  plugin.register(ctx)
  const page = registered.find(entry => entry.area === 'routes')
  harness.mount(() => page.render())
  return { harness, ctx, registered, restCalls, storageWrites, refreshes }
}

function byRole(harness, role, name) {
  return harness.find(node => node.props?.role === role && (name == null || textContent(node).includes(name)), `${role}:${name || ''}`)
}

function byLabel(harness, label) {
  return harness.find(node => node.props?.['aria-label'] === label, `aria-label:${label}`)
}

test('actual ESM plugin renders stateful filters and sends namespace-relative query bodies', async () => {
  const { harness, restCalls, storageWrites, refreshes } = mountPlugin()

  assert.equal(byRole(harness, 'tab', 'Desktop').props['aria-selected'], true)
  assert.equal(byRole(harness, 'tab', 'Telegram').props['aria-selected'], false)
  assert.equal(byRole(harness, 'tab', 'Kanban').props['aria-selected'], false)
  assert.equal(harness.queryOptions.queryKey[1].tab, 'desktop')
  assert.equal(harness.queryOptions.refetchInterval, 30000)

  byRole(harness, 'tab', 'Telegram').props.onClick()
  assert.equal(harness.queryOptions.queryKey[1].tab, 'telegram')
  assert.equal(byRole(harness, 'tab', 'Telegram').props['aria-selected'], true)

  const history = byLabel(harness, 'History window')
  assert.deepEqual(history.props.children.map(option => option.props.children), [
    'Last 7 days', 'Last 14 days', 'Last 30 days', 'Full retained history'
  ])
  byLabel(harness, 'Next page').props.onClick()
  assert.equal(harness.queryOptions.queryKey[1].cursor, 'cursor-next')
  history.props.onChange({ target: { value: '7d' } })
  assert.equal(harness.queryOptions.queryKey[1].historyScope, '7d')
  assert.equal(harness.queryOptions.queryKey[1].cursor, null)

  byLabel(harness, 'Search title or content').props.onChange({ target: { value: 'needle' } })
  byLabel(harness, 'Profile builder').props.onChange({ target: { checked: false } })
  byLabel(harness, 'From date').props.onChange({ target: { value: '2026-08-01' } })
  assert.equal(harness.queryOptions.queryKey[1].historyScope, 'custom')
  byLabel(harness, 'To date').props.onChange({ target: { value: '2026-08-27' } })
  byLabel(harness, 'Telegram type topic').props.onChange({ target: { checked: true } })
  byLabel(harness, 'Sessions per page').props.onChange({ target: { value: '50' } })

  const filters = harness.queryOptions.queryKey[1]
  assert.equal(filters.query, 'needle')
  assert.deepEqual(filters.profiles, profiles.filter(profile => profile !== 'builder'))
  assert.deepEqual(filters.telegramTypes, ['topic'])
  assert.equal(filters.pageSize, 50)
  assert.ok(Number.isInteger(filters.dateFrom) && Number.isInteger(filters.dateTo))
  assert.deepEqual(storageWrites.at(-1), ['pageSize', 50])

  await harness.queryOptions.queryFn()
  assert.equal(restCalls.at(-1)[0], '/sessions')
  assert.deepEqual(restCalls.at(-1)[1].body, filters)

  byLabel(harness, 'Next page').props.onClick()
  assert.equal(harness.queryOptions.queryKey[1].cursor, 'cursor-next')
  byLabel(harness, 'Previous page').props.onClick()
  assert.equal(harness.queryOptions.queryKey[1].cursor, null)

  byLabel(harness, 'Refresh sessions').props.onClick()
  assert.equal(refreshes.length, 1)
  assert.equal(byRole(harness, 'tablist').props['aria-label'], 'Session source')
  assert.equal(byLabel(harness, 'Search title or content').props.type, 'search')
  assert.ok(harness.findAll(node => node.type === 'time').length > 0)
  assert.match(harness.text(), /review/)
  assert.match(harness.text(), /telegram/)
  assert.match(harness.text(), /recent one/)
})

test('codex·claude tab lists recent AI sessions with title, status and conclusion', async () => {
  const session = {
    id: 'codex:t1', source: 'codex', title: '빌드 스크립트 오류 진단', status: '완료',
    firstMessage: '빌드가 계속 실패해', folder: '2026-01-05/sample-task', model: 'gpt-5',
    archived: false, updatedAt: '2026-10-09T02:32:52Z',
    conclusion: ['원인을 찾아 고쳤습니다.', '수정: 경로 처리', '테스트: 12개 통과']
  }
  const { harness, restCalls } = mountPlugin(queryResult({
    data: {
      items: [session], page: { returned: 1, totalKnown: 1 }, nextCursor: null,
      collectedAt: '2026-10-14T00:00:00Z', sources: { codex: { ok: true, count: 1 }, 'claude-desktop': { ok: false, count: 0, error: 'claude_desktop_missing' } }
    }
  }))
  byRole(harness, 'tab', '코덱스·클로드').props.onClick()
  assert.equal(harness.queryOptions.queryKey[1].tab, 'external-ai')
  assert.match(harness.text(), /빌드 스크립트 오류 진단/)
  assert.match(harness.text(), /코덱스 · 폴더 2026-01-05\/sample-task · gpt-5/)
  assert.match(harness.text(), /요청: 빌드가 계속 실패해/)
  assert.match(harness.text(), /원인을 찾아 고쳤습니다\./)
  assert.match(harness.text(), /마지막 수집 .* · 30분마다 자동 갱신 · 코덱스 1건 · 클로드 데스크톱 없음\(이 PC에 기록 없음\)/)
  assert.equal(harness.findAll(node => node.props?.['data-ai-session-conclusion']).length, 1)
  assert.ok(byLabel(harness, 'Search AI sessions'))
  assert.ok(byLabel(harness, 'Refresh AI sessions'))
  assert.equal(harness.findAll(node => node.props?.['aria-label'] === 'History window').length, 0)
  await harness.queryOptions.queryFn()
  assert.equal(restCalls.at(-1)[0], '/ai-sessions')
  assert.deepEqual(Object.keys(restCalls.at(-1)[1].body).sort(), ['cursor', 'includeArchived', 'pageSize', 'query', 'sources'])
})

test('codex·claude refresh collects first, then refetches; source filter narrows the request', async () => {
  const { harness, restCalls } = mountPlugin(queryResult({
    data: { items: [], page: { returned: 0, totalKnown: 0 }, nextCursor: null, collectedAt: null, sources: {} }
  }))
  byRole(harness, 'tab', '코덱스·클로드').props.onClick()
  assert.match(harness.text(), /아직 수집 전/)
  assert.match(harness.text(), /조건에 맞는 코덱스·클로드 작업이 없습니다/)
  byLabel(harness, 'AI session source').props.onChange({ target: { value: 'claude' } })
  assert.deepEqual(harness.queryOptions.queryKey[1].aiSources, ['claude'])
  await byLabel(harness, 'Refresh AI sessions').props.onClick()
  assert.ok(restCalls.some(call => call[0] === '/ai-sessions/refresh'))
})

test('codex·claude tab shows cloud summary reports with remote labels and filter', async () => {
  const report = {
    id: 'claude-cloud:session_01ABC', source: 'claude-cloud', title: '원격 요약 예시', status: '완료',
    firstMessage: '원격 작업 요약을 보여줘', folder: 'SampleProject', model: 'claude-opus-5-5',
    archived: false, updatedAt: '2026-10-13T22:00:00Z', conclusion: ['요약 파일 출처를 추가했습니다.']
  }
  const { harness } = mountPlugin(queryResult({
    data: {
      items: [report], page: { returned: 1, totalKnown: 1 }, nextCursor: null,
      collectedAt: '2026-10-14T00:00:00Z', sources: { reports: { ok: true, count: 1 } }
    }
  }))
  byRole(harness, 'tab', '코덱스·클로드').props.onClick()
  assert.match(harness.text(), /클로드\(원격\) · 프로젝트 SampleProject · claude-opus-5-5/)
  assert.match(harness.text(), /원격 요약 1건/)
  byLabel(harness, 'AI session source').props.onChange({ target: { value: 'remote' } })
  assert.deepEqual(harness.queryOptions.queryKey[1].aiSources, ['remote'])
})

test('skills tab searches on submit, refreshes cache, toggles archives, and copies absolute paths', async () => {
  const copied = []
  const skill = {
    id: 'fixture-skill', title: 'fixture-skill',
    description: 'Use when exact phrase automation is needed.',
    triggers: 'Use when exact phrase automation is needed.', tags: ['automation'],
    statuses: ['active', 'backup'], deployedProfiles: ['default', 'builder'],
    canonical: {
      status: 'verified', absolutePath: 'D:/SharedSkills/fixture-skill/SKILL.md',
      paths: ['D:/SharedSkills/fixture-skill/SKILL.md'], evidence: 'sha256_match_active'
    },
    runtimeCopies: [
      { profile: 'default', status: 'active', absolutePath: 'C:/Users/me/AppData/Local/hermes/skills/fixture-skill/SKILL.md' },
      { profile: 'builder', status: 'active', absolutePath: 'C:/Users/me/AppData/Local/hermes/profiles/builder/skills/fixture-skill/SKILL.md' }
    ],
    otherCopies: [], matchReason: 'title · description', snippet: 'exact phrase automation', score: 160
  }
  const { harness, restCalls, refreshes } = mountPlugin(queryResult({
    data: {
      items: [skill], page: { returned: 1, totalKnown: 1 }, refreshedAt: 1790403000,
      sources: [], includeArchive: false
    }
  }), {
    os: { async writeClipboard(path) { copied.push(path); return true } }
  })

  byRole(harness, 'tab', 'Skills').props.onClick()
  assert.equal(harness.queryOptions.queryKey[1].tab, 'skills')
  assert.equal(harness.queryOptions.refetchInterval, false)
  assert.equal(byRole(harness, 'tab', 'Skills').props['aria-selected'], true)
  assert.match(harness.text(), /fixture-skill/)
  assert.match(harness.text(), /정본 verified/)
  assert.match(harness.text(), /default, builder/)
  assert.match(harness.text(), /마지막 갱신/)

  const input = byLabel(harness, 'Search skills')
  input.props.onChange({ target: { value: 'automation "exact phrase"' } })
  byLabel(harness, 'Search skills').props.onKeyDown({ key: 'Enter', preventDefault() {} })
  assert.equal(harness.queryOptions.queryKey[1].query, 'automation "exact phrase"')
  await harness.queryOptions.queryFn()
  assert.equal(restCalls.at(-1)[0], '/skills/search')
  assert.deepEqual(restCalls.at(-1)[1].body, {
    query: 'automation "exact phrase"', includeArchive: false
  })

  byLabel(harness, '보관/백업 포함').props.onChange({ target: { checked: true } })
  assert.equal(harness.queryOptions.queryKey[1].includeArchive, true)
  await byLabel(harness, 'Refresh skills').props.onClick()
  assert.equal(restCalls.at(-1)[0], '/skills/refresh')
  assert.equal(refreshes.length, 1)

  await byLabel(harness, 'Copy path default fixture-skill').props.onClick()
  assert.deepEqual(copied, ['C:/Users/me/AppData/Local/hermes/skills/fixture-skill/SKILL.md'])
})

test('kanban row fetches exact detail, opens right drawer, and uses exact session actions', async () => {
  const opened = []
  const navigated = []
  const card = {
    kind: 'kanban', id: 't_exact', title: 'Exact card', board: 'team', status: 'blocked',
    assignee: 'builder', ownerProfile: 'builder', priority: 9, createdAt: 1787770000,
    updatedAt: 1787770100, dueAt: 1788000000, projectId: 'proj-1', parentIds: ['t_parent'],
    childIds: ['t_child'], body: 'Sanitized detail body', sourceInstance: null,
    currentRun: { id: 22, profile: 'builder', status: 'running', startedAt: 1787770050 },
    latestRun: { id: 22, profile: 'builder', status: 'running', startedAt: 1787770050 },
    originSession: { kind: 'origin', state: 'available', available: true, ownerProfile: 'default', storedSessionId: '20260101_090000_aaaaaa', reason: null },
    latestWorkerSession: { kind: 'worker', state: 'available', available: true, ownerProfile: 'ops', storedSessionId: '20260101_100000_bbbbbb', reason: null, runId: 22 },
    blocked: true, blockKind: 'needs_input'
  }
  const { harness, restCalls } = mountPlugin(queryResult({
    data: { items: [card], page: { returned: 1, totalKnown: 1 }, nextCursor: null }
  }), {
    host: {
      navigate(path) { navigated.push(path) },
      async openSession(id, args) { opened.push([id, args]) },
      notify() {},
      newChat() {}
    }
  })

  byRole(harness, 'tab', 'Kanban').props.onClick()
  assert.equal(harness.queryOptions.queryKey[1].tab, 'kanban')
  assert.deepEqual(harness.queryOptions.queryKey[1].profiles, profiles)
  assert.match(harness.text(), /Cards per page/)
  assert.match(harness.text(), /1 card shown · 1 total/)
  assert.match(harness.text(), /t_exact/)
  assert.match(harness.text(), /team/)
  assert.match(harness.text(), /needs_input/)
  assert.ok(byLabel(harness, 'Kanban status blocked'))
  await byLabel(harness, 'Open Kanban card t_exact').props.onClick()
  assert.equal(restCalls.at(-1)[0], '/kanban/team/t_exact')
  const drawer = byLabel(harness, 'Kanban detail drawer')
  assert.match(drawer.props.className, /bg-\(--theme-elevated-seed\)/)
  assert.match(drawer.props.className, /\bisolate\b/)
  assert.match(drawer.props.className, /\bopacity-100\b/)
  assert.doesNotMatch(drawer.props.className, /bg-\(--ui-bg-primary\)/)
  assert.doesNotMatch(drawer.props.className, /bg-\(--ui-bg-elevated\)/)
  assert.equal(drawer.props.style.backgroundColor, 'var(--theme-elevated-seed, Canvas)')
  assert.equal(drawer.props.style.isolation, 'isolate')
  assert.equal(drawer.props.style.opacity, 1)
  assert.match(harness.text(), /project=proj-1/)
  assert.match(harness.text(), /Sanitized detail body/)
  assert.match(harness.text(), /t_parent/)
  assert.match(harness.text(), /t_child/)
  assert.ok(byLabel(harness, 'Open parent task t_parent'))
  assert.ok(byLabel(harness, 'Open child task t_child'))
  assert.equal(byLabel(harness, 'Open Kanban card t_exact').props['aria-pressed'], true)

  await byLabel(harness, '상위 세션으로 이동').props.onClick()
  await byLabel(harness, '작업 실행 세션으로 이동').props.onClick()
  assert.deepEqual(opened, [
    ['20260101_090000_aaaaaa', { profile: 'default', intent: 'in-place', keepAllProfilesScope: false }],
    ['20260101_100000_bbbbbb', { profile: 'ops', intent: 'in-place', keepAllProfilesScope: false }]
  ])
  byLabel(harness, 'Close Kanban detail').props.onClick()
  assert.equal(harness.findAll(node => node.props?.['aria-label'] === 'Kanban detail drawer').length, 0)

  await byLabel(harness, 'Open Kanban card t_exact').props.onClick()
  byLabel(harness, 'Kanban 보드 열기').props.onClick()
  // No built-in Kanban pane is assumed: stay on Session Hub, Kanban tab, card id searched, drawer closed.
  assert.deepEqual(navigated, ['/session-hub'])
  assert.equal(harness.findAll(node => node.props?.['aria-label'] === 'Kanban detail drawer').length, 0)
  assert.equal(byRole(harness, 'tab', 'Kanban').props['aria-selected'], true)
  assert.equal(byLabel(harness, 'Search title or content').props.value, 't_exact')
})

test('exact Kanban detail survives a response slower than 5s and keeps the loading drawer open', async () => {
  const simulatedLatencyMs = 5100
  const exact = {
    kind: 'kanban', id: 't_slow', title: 'Slow exact card', board: 'team', status: 'running',
    assignee: 'ops', ownerProfile: 'ops', priority: 5, createdAt: 1, updatedAt: 2,
    parentIds: [], childIds: [], body: 'Arrived after five seconds',
    originSession: { kind: 'origin', state: 'available', available: true, ownerProfile: 'default', storedSessionId: '20260101_090000_aaaaaa', reason: null },
    latestWorkerSession: { kind: 'worker', state: 'available', available: true, ownerProfile: 'ops', storedSessionId: '20260101_100000_bbbbbb', reason: null, runId: 23 }
  }
  const { harness } = mountPlugin(queryResult({
    data: { items: [exact], page: { returned: 1, totalKnown: 1 }, nextCursor: null }
  }), {
    host: { navigate() {}, async openSession() {}, notify() {}, newChat() {} },
    rest: async (path, requestOptions) => {
      if (path.startsWith('/kanban/')) {
        if (requestOptions.timeoutMs <= simulatedLatencyMs) throw new Error('simulated timeout')
        await new Promise(resolve => setTimeout(resolve, 10))
        return exact
      }
      return { items: [exact], page: { returned: 1, totalKnown: 1 }, nextCursor: null }
    }
  })

  byRole(harness, 'tab', 'Kanban').props.onClick()
  const opening = byLabel(harness, 'Open Kanban card t_slow').props.onClick()
  assert.ok(byLabel(harness, 'Kanban detail drawer'))
  assert.match(harness.text(), /Slow exact card/)
  await opening

  assert.doesNotMatch(harness.text(), /Exact Kanban card detail is unavailable/)
  assert.match(harness.text(), /Arrived after five seconds/)
  assert.ok(byLabel(harness, '상위 세션으로 이동'))
  assert.ok(byLabel(harness, '작업 실행 세션으로 이동'))
})

test('drawer stays open on stale session failure, shows inline recovery, and closes by Escape/backdrop', async () => {
  const created = []
  const card = {
    kind: 'kanban', id: 't_stale', title: 'Stale link', board: 'default', status: 'done',
    assignee: 'ops', priority: 1, createdAt: 1, updatedAt: 2, parentIds: [], childIds: [],
    body: '', originSession: {
      kind: 'origin', state: 'available', available: true,
      ownerProfile: 'default', storedSessionId: 'gone', reason: null
    },
    latestWorkerSession: { kind: 'worker', state: 'absent', available: false, ownerProfile: null, storedSessionId: null, reason: 'No worker execution session is recorded.' }
  }
  const { harness } = mountPlugin(queryResult({ data: { items: [card], page: { returned: 1, totalKnown: 1 }, nextCursor: null } }), {
    host: { navigate() {}, async openSession() {}, notify() {}, newChat(profile) { created.push(profile) } },
    rest: async path => {
      if (path === '/revalidate') throw new Error('unknown_session')
      if (path.startsWith('/kanban/')) return card
      return { items: [card], page: { returned: 1, totalKnown: 1 }, nextCursor: null }
    }
  })
  byRole(harness, 'tab', 'Kanban').props.onClick()
  await byLabel(harness, 'Open Kanban card t_stale').props.onClick()
  await byLabel(harness, '상위 세션으로 이동').props.onClick()
  assert.ok(byLabel(harness, 'Kanban detail drawer'))
  assert.match(harness.text(), /Session could not be verified \(unknown_session\)/)
  byLabel(harness, '복구용 새 세션').props.onClick()
  assert.deepEqual(created, ['default'])

  byLabel(harness, 'Kanban detail drawer').props.onKeyDown({ key: 'Escape', stopPropagation() {} })
  assert.equal(harness.findAll(node => node.props?.['aria-label'] === 'Kanban detail drawer').length, 0)
  await byLabel(harness, 'Open Kanban card t_stale').props.onClick()
  byLabel(harness, 'Close Kanban detail backdrop').props.onClick()
  assert.equal(harness.findAll(node => node.props?.['aria-label'] === 'Kanban detail drawer').length, 0)
})

test('actual component distinguishes accessible loading, backend error, and empty states', () => {
  const { harness } = mountPlugin(queryResult({ data: undefined, isLoading: true }))
  assert.match(textContent(byRole(harness, 'status')), /Loading sessions/)

  harness.setQuery(queryResult({ data: undefined, isError: true, error: new Error('backend disabled') }))
  assert.match(textContent(byRole(harness, 'alert')), /backend is unavailable/i)
  assert.ok(byLabel(harness, 'Retry loading sessions'))

  harness.setQuery(queryResult({ data: { items: [], page: { returned: 0, totalKnown: 0 }, nextCursor: null } }))
  assert.match(textContent(byRole(harness, 'status')), /No sessions match/)

  byRole(harness, 'tab', 'Kanban').props.onClick()
  assert.match(textContent(byRole(harness, 'status')), /No Kanban cards match/)
})

test('all preference traverses 601 stable rows with no duplicates and at most 100 rendered cards', () => {
  const rows = Array.from(
    { length: 601 },
    (_, index) => item(index + 1, { source: 'desktop', telegramType: null })
  )
  const page = offset => ({
    items: rows.slice(offset, offset + 100),
    page: { returned: Math.min(100, rows.length - offset), totalKnown: rows.length, windowLimit: 100 },
    nextCursor: offset + 100 < rows.length ? `cursor-${offset + 100}` : null
  })
  const { harness } = mountPlugin(queryResult({ data: page(0) }))
  byLabel(harness, 'Sessions per page').props.onChange({ target: { value: 'all' } })

  const collected = []
  for (let offset = 0; offset < rows.length; offset += 100) {
    const cards = harness.findAll(node => node.props?.['data-session-card'] === true)
    assert.ok(cards.length <= 100, `rendered ${cards.length} cards in one all-mode window`)
    collected.push(...cards.map(card => textContent(card.props.children[0])))
    if (offset + 100 >= rows.length) break

    const nextCursor = `cursor-${offset + 100}`
    const next = byLabel(harness, 'Next page')
    assert.equal(next.props.disabled, false)
    next.props.onClick()
    assert.equal(harness.queryOptions.queryKey[1].cursor, nextCursor)
    harness.setQuery(queryResult({ data: page(offset + 100) }))
  }

  assert.deepEqual(collected, rows.map(row => row.title))
  assert.equal(new Set(collected).size, 601)
  assert.equal(byLabel(harness, 'Next page').props.disabled, true)
  assert.ok(byLabel(harness, 'Previous page'))
})

test('all preference keeps an overfull backend response bounded without append controls', () => {
  const rows = Array.from(
    { length: 150 },
    (_, index) => item(index + 1, { source: 'desktop', telegramType: null })
  )
  const { harness } = mountPlugin(queryResult({
    data: {
      items: rows,
      page: { returned: 150, totalKnown: 151, windowLimit: 100 },
      nextCursor: 'cursor-next'
    }
  }))
  byLabel(harness, 'Sessions per page').props.onChange({ target: { value: 'all' } })

  assert.equal(harness.findAll(node => node.props?.['data-session-card'] === true).length, 100)
  assert.equal(harness.findAll(node => node.props?.['aria-label'] === 'Show more sessions').length, 0)
  assert.equal(byLabel(harness, 'Next page').props.disabled, false)
})

test('open controller rejects malformed selections and rapid clicks remain last-wins', async () => {
  const opened = []
  const selections = []
  let releaseFirst
  const first = new Promise(resolve => { releaseFirst = resolve })
  const backend = {
    revalidate(selection) {
      selections.push(selection)
      if (selection.storedSessionId === 'first') return first
      if (selection.storedSessionId === 'mismatch') throw new Error('mismatch')
      return Promise.resolve(selection)
    }
  }
  const controller = createOpenController({
    backend,
    host: { async openSession(id) { opened.push(id) }, notify() {} }
  })

  for (const invalid of [null, {}, { ownerProfile: 'unknown', storedSessionId: 'x' }, { ownerProfile: 'review', storedSessionId: '../x' }]) {
    const result = await controller.open(invalid)
    assert.equal(result.opened, false)
  }
  assert.equal(selections.length, 0)

  const firstOpen = controller.open({ ownerProfile: 'review', storedSessionId: 'first' })
  const secondOpen = controller.open({ ownerProfile: 'review', storedSessionId: 'second' })
  releaseFirst({ ownerProfile: 'review', storedSessionId: 'first' })
  await Promise.allSettled([firstOpen, secondOpen])
  await controller.open({ ownerProfile: 'review', storedSessionId: 'mismatch' })
  assert.deepEqual(opened, ['second'])
})

test('open controller fails closed when revalidation returns a different valid selection', async () => {
  const opened = []
  const notifications = []
  const controller = createOpenController({
    backend: {
      async revalidate() {
        return { ownerProfile: 'builder', storedSessionId: 'different-valid-session' }
      }
    },
    host: {
      async openSession(id, options) { opened.push({ id, options }) },
      notify(notification) { notifications.push(notification) }
    }
  })

  const result = await controller.open({ ownerProfile: 'review', storedSessionId: 'requested-valid-session' })

  assert.deepEqual(result, { opened: false, phase: 'revalidate', reason: 'Session could not be verified (ownership validation failed).' })
  assert.equal(opened.length, 0)
  assert.deepEqual(notifications, [{ kind: 'warning', message: 'Session could not be verified (ownership validation failed).' }])
})

test('open controller reports a Desktop open failure separately from a backend rejection', async () => {
  const notifications = []
  const controller = createOpenController({
    backend: { async revalidate(sel) { return sel } },
    host: {
      async openSession() { throw new Error('Timed out loading ops\'s session history.') },
      notify(n) { notifications.push(n) },
      newChat() {}
    }
  })
  const result = await controller.open({ ownerProfile: 'ops', storedSessionId: '20260101_100000_bbbbbb' })
  assert.equal(result.opened, false)
  assert.equal(result.phase, 'open')
  assert.match(result.reason, /^Desktop could not open ops session \(Timed out loading/)
  assert.equal(notifications.length, 1)
})

test('open controller surfaces the backend error code when revalidate returns ok:false', async () => {
  const controller = createOpenController({
    backend: { async revalidate() { return { ok: false, error: 'source_not_allowed' } } },
    host: { async openSession() { throw new Error('must not be called') }, notify() {} }
  })
  const result = await controller.open({ ownerProfile: 'ops', storedSessionId: '20260101_100000_bbbbbb' })
  assert.deepEqual(result, { opened: false, phase: 'revalidate', reason: 'Session could not be verified (backend rejected: source_not_allowed).' })
})

test('branch (지사) session rows are display-only with a freshness badge and grouped profile filters', async () => {
  const opened = []
  const notifications = []
  const instances = [
    { id: 'HQ', kind: 'local', state: 'local', label: '본사' },
    { id: 'X', kind: 'remote', state: 'fresh', label: 'Branch X', ageSeconds: 720, ttlSeconds: 1800, reason: null },
    { id: 'Y', kind: 'remote', state: 'unknown', label: 'Branch Y', ageSeconds: null, ttlSeconds: 86400, reason: 'no_snapshot' },
    { id: 'Z', kind: 'remote', state: 'stale', label: 'Branch Z', ageSeconds: 5 * 86400, ttlSeconds: 3 * 86400, reason: 'ttl_exceeded' }
  ]
  const { harness } = mountPlugin(queryResult({
    data: {
      items: [
        item(1, { ownerProfile: 'X/default', instance: 'X', remote: true, title: 'Branch X work', source: 'desktop', telegramType: null }),
        item(2, { ownerProfile: 'review', instance: 'HQ', remote: false, title: 'HQ review work' })
      ],
      page: { returned: 2, totalKnown: 2 }, nextCursor: null, instances,
      skippedProfiles: [{ profile: 'Y/default', instance: 'Y', error: 'snapshot_unavailable' }]
    }
  }), {
    host: {
      async openSession(id, args) { opened.push([id, args]) },
      notify(notice) { notifications.push(notice) }
    }
  })

  // Eight checkboxes in two labelled groups; the aria-labels keep the raw profile keys.
  const groups = harness.findAll(node => node.props?.['data-profile-group'])
  assert.deepEqual(groups.map(group => group.props['data-profile-group']), ['HQ', 'branch'])
  for (const profile of profiles) assert.ok(byLabel(harness, `Profile ${profile}`), profile)
  assert.match(harness.text(), /본사:/)
  assert.match(harness.text(), /지사:/)
  assert.match(harness.text(), /Branch X \(X\)/)
  assert.match(harness.text(), /Lead/)
  byLabel(harness, 'Profile X/default').props.onChange({ target: { checked: false } })
  assert.deepEqual(harness.queryOptions.queryKey[1].profiles, profiles.filter(profile => profile !== 'X/default'))

  // Header strip shows every branch state; the row badge shows the row's own instance.
  const strip = byLabel(harness, 'Branch snapshot freshness')
  assert.match(textContent(strip), /X · 12분 전 스냅샷/)
  assert.match(textContent(strip), /Y · 스냅샷 없음/)
  assert.match(textContent(strip), /Z · 스냅샷 오래됨 \(5일 전\)/)
  const remoteCard = harness.find(node => node.props?.['data-session-card'] && node.props['data-remote'] === 'true', 'remote card')
  assert.match(textContent(remoteCard), /이 PC에서는 열 수 없음 — 지사 X 세션/)
  assert.match(textContent(remoteCard), /Branch X \(X\) · X\/default/)
  assert.equal(harness.findAll(node => node.props?.['aria-label'] === 'Open Branch X work').length, 0)
  assert.ok(byLabel(harness, 'Remote session Branch X work'))
  // The HQ row still opens in place.
  await byLabel(harness, 'Open HQ review work').props.onClick()
  assert.deepEqual(opened, [['fixture-2', { profile: 'review', intent: 'in-place', keepAllProfilesScope: false }]])
  assert.equal(notifications.length, 0)
})

test('branch kanban cards carry the instance badge and map the branch default profile', async () => {
  const instances = [
    { id: 'HQ', kind: 'local', state: 'local' },
    { id: 'X', kind: 'remote', state: 'damaged', label: 'Branch X', ageSeconds: null, reason: 'state_sha_mismatch' }
  ]
  const card = {
    kind: 'kanban', id: 'k_t1', title: 'Branch card', board: 'X', instance: 'X', remote: true, status: 'running',
    assignee: 'default', ownerProfile: 'X/default', priority: 5, createdAt: 1787770000, updatedAt: 1787770100,
    dueAt: null, projectId: null, parentIds: [], childIds: [], sourceInstance: 'X', currentRun: null, latestRun: null,
    blocked: false, blockKind: null
  }
  const { harness } = mountPlugin(queryResult({
    data: { items: [card], page: { returned: 1, totalKnown: 1 }, nextCursor: null, instances, skippedRemoteBoards: [] }
  }), { host: { navigate() {}, async openSession() {}, notify() {} } })
  byRole(harness, 'tab', 'Kanban').props.onClick()
  const remoteCard = harness.find(node => node.props?.['data-kanban-card'] === 'k_t1', 'remote kanban card')
  assert.equal(remoteCard.props['data-remote'], 'true')
  assert.match(textContent(remoteCard), /X · 스냅샷 손상/)
  assert.match(textContent(remoteCard), /assignee=Branch X \(X\) · X\/default/)
  assert.match(textContent(remoteCard), /X · k_t1/)
})

test('open controller refuses branch profiles before touching the backend', async () => {
  const notifications = []
  let revalidated = 0
  const controller = createOpenController({
    backend: { async revalidate(sel) { revalidated += 1; return sel } },
    host: { async openSession() { throw new Error('must not be called') }, notify(n) { notifications.push(n) } }
  })
  const result = await controller.open({ ownerProfile: 'X/default', storedSessionId: 'k-recent' })
  assert.deepEqual(result, { opened: false })
  assert.equal(revalidated, 0)
  assert.deepEqual(notifications, [{ kind: 'warning', message: 'Session selection is invalid.' }])
})

test('profile checkboxes come from GET /config and malformed payloads are ignored', async () => {
  const invalidations = []
  const client = { invalidateQueries: options => invalidations.push(options) }
  try {
    const changed = await loadHubConfig({
      rest: async path => path === '/config'
        ? { ...TEST_HUB_CONFIG, localProfiles: ['default', 'writer'], remoteProfiles: [], profileLabels: { writer: 'Writer' } }
        : null
    }, client)
    assert.equal(changed, true)
    assert.deepEqual(PROFILE_NAMES, ['default', 'writer'])
    assert.deepEqual(PROFILE_GROUPS.map(group => group.id), ['HQ'])
    assert.deepEqual(invalidations, [{ queryKey: ['session-hub'] }])
    assert.equal(applyHubConfig({ items: [] }), false)
    assert.equal(applyHubConfig({ ...TEST_HUB_CONFIG, localProfiles: ['../evil'] }), false)
    assert.deepEqual(PROFILE_NAMES, ['default', 'writer'])
    applyHubConfig({ ...TEST_HUB_CONFIG, instancesEnabled: false })
    assert.deepEqual(PROFILE_NAMES, TEST_HUB_CONFIG.localProfiles)
    assert.equal(PROFILE_GROUPS.length, 1)
    assert.equal(await loadHubConfig({ rest: async () => { throw new Error('offline') } }, client), false)
  } finally {
    applyHubConfig(TEST_HUB_CONFIG)
  }
  assert.deepEqual(PROFILE_NAMES, [...TEST_HUB_CONFIG.localProfiles, ...TEST_HUB_CONFIG.remoteProfiles])
})

test('config load retries while the backend is still starting', async () => {
  const saved = HUB_CONFIG_STATE.loaded
  HUB_CONFIG_STATE.loaded = false
  let calls = 0
  try {
    const changed = await loadHubConfigWithRetry({
      rest: async () => {
        calls += 1
        if (calls < 3) throw new Error('backend not ready')
        return { ...TEST_HUB_CONFIG, localProfiles: ['default', 'late'], remoteProfiles: [] }
      }
    }, { invalidateQueries() {} }, [1, 1, 1], { keepAlive: true })
    assert.equal(calls, 3)
    assert.equal(changed, true)
    assert.equal(HUB_CONFIG_STATE.loaded, true)
    assert.deepEqual(PROFILE_NAMES, ['default', 'late'])
  } finally {
    HUB_CONFIG_STATE.loaded = saved
    applyHubConfig(TEST_HUB_CONFIG)
  }
})

test('local profiles the backend could not read are listed, not hidden', async () => {
  const { harness } = mountPlugin(queryResult({
    data: { items: [], page: { returned: 0, totalKnown: 0 }, nextCursor: null,
      skippedProfiles: [{ profile: 'review', instance: 'HQ', error: 'stale_schema' }, { profile: 'X/default', instance: 'X', error: 'snapshot_unavailable' }] }
  }))
  assert.match(harness.text(), /읽지 못한 프로필: Review \(stale_schema\)/)
  assert.doesNotMatch(harness.text(), /읽지 못한 프로필:.*X\/default/)
})
