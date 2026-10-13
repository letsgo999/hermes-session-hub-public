import assert from 'node:assert/strict'
import { test } from 'node:test'

import plugin, {
  DEFAULT_HISTORY_SCOPE,
  createKanbanController,
  createOpenController,
  defaultSessionFilters,
  historyWindowSelection,
  registerEventRefresh
} from '../plugins/session-hub/desktop/plugin.js'
import { createHarness, textContent } from './fixtures/renderer.mjs'
import './fixtures/hub-config.mjs'


test('session history exposes deterministic 7/14/30/full boundaries', () => {
  const now = 2_000_000_000
  const seven = defaultSessionFilters(now, '7d')
  const fourteen = defaultSessionFilters(now, '14d')
  const recent = defaultSessionFilters(now, '30d')
  const full = defaultSessionFilters(now, 'full')
  assert.equal(DEFAULT_HISTORY_SCOPE, '30d')
  assert.equal(seven.dateFrom, now - 7 * 24 * 60 * 60)
  assert.equal(fourteen.dateFrom, now - 14 * 24 * 60 * 60)
  assert.equal(recent.dateFrom, now - 30 * 24 * 60 * 60)
  assert.equal(full.dateFrom, null)
})

test('project hub starts with full session history and omits the daily execution summary', () => {
  const registered = []
  const harness = createHarness({
    queryResult: {
      data: {
        projects: [
          { id: 'one', name: 'Summary one' },
          { id: 'two', name: 'Summary two' },
          { id: 'three', name: 'Summary three' }
        ],
        actions: [{ projectId: 'one', status: 'running', nextAction: 'Ship it' }],
        items: [],
        page: { returned: 0, totalKnown: 0 },
        nextCursor: null
      },
      isLoading: false,
      isError: false,
      isFetching: false,
      error: null,
      refetch() {}
    }
  })
  const ctx = {
    storage: { get: () => 20, set() {} },
    os: { async writeClipboard() { return true } },
    rest: async () => ({ items: [] }),
    registerMany(entries) { registered.push(...entries) }
  }
  plugin.register(ctx)
  const page = registered.find(entry => entry.area === 'routes')
  harness.mount(() => page.render())

  const hub = harness.find(node => node.props?.['data-project-hub'] === true, 'Project Hub root')
  const children = hub.props.children.filter(Boolean)
  assert.equal(children[0].type, 'header')
  assert.equal(children[1].type, 'details')
  assert.equal(textContent(children[1].props.children[0]), '전체 세션 이력')
  assert.equal(harness.text().includes('오늘의 실행목록'), false)
  assert.equal(harness.findAll(node => node.props?.['data-project-card']).length, 0)
  assert.equal(harness.text().includes('Summary one'), false)
})

test('history-window selection preserves a coherent custom marker and resets from exact now', () => {
  const now = 2_000_000_000
  assert.deepEqual(historyWindowSelection('7d', now), {
    historyScope: '7d',
    dateFrom: now - 7 * 24 * 60 * 60
  })
  assert.deepEqual(historyWindowSelection('full', now), {
    historyScope: 'full',
    dateFrom: null
  })
  assert.deepEqual(historyWindowSelection('custom', now, 1234), {
    historyScope: 'custom',
    dateFrom: 1234
  })
})

test('cross-profile open uses official in-place contract and remains last-click-wins', async () => {
  const opened = []
  let resolveFirst
  const first = new Promise(resolve => { resolveFirst = resolve })
  const controller = createOpenController({
    backend: {
      revalidate(selection) {
        return selection.storedSessionId === 'first' ? first : Promise.resolve(selection)
      }
    },
    host: { openSession: async (id, options) => opened.push([id, options]) }
  })
  const p1 = controller.open({ ownerProfile: 'default', storedSessionId: 'first' })
  const p2 = controller.open({ ownerProfile: 'review', storedSessionId: 'second' })
  resolveFirst({ ownerProfile: 'default', storedSessionId: 'first' })
  await Promise.all([p1, p2])
  assert.deepEqual(opened, [['second', {
    profile: 'review', intent: 'in-place', keepAllProfilesScope: false
  }]])
})

test('kanban row always opens exact card detail before any session action', async () => {
  const calls = []
  const controller = createKanbanController({
    openSession: async selection => calls.push(['session', selection]),
    showTask: task => calls.push(['task', task.id])
  })
  await controller.open({ id: 't_linked', sourceSession: { ownerProfile: 'builder', storedSessionId: 's1' } })
  await controller.open({ id: 't_unlinked', sourceSession: null })
  assert.deepEqual(calls, [
    ['task', 't_linked'],
    ['task', 't_unlinked']
  ])
})

test('recovery is user-clicked only and never created automatically', async () => {
  const created = []
  const controller = createOpenController({
    backend: { async revalidate() { throw new Error('unknown_session') } },
    host: { async openSession() {}, newChat: profile => created.push(profile) }
  })
  const failure = await controller.open({ ownerProfile: 'search', storedSessionId: 'missing' })
  assert.equal(failure.recoveryLabel, '복구용 새 세션')
  assert.equal(created.length, 0)
  failure.recover()
  assert.deepEqual(created, ['search'])
})

test('event refresh invalidates project queries and 30s polling remains configured', () => {
  const listeners = []
  const invalidations = []
  const dispose = registerEventRefresh({
    onEvent(type, callback) { listeners.push([type, callback]); return () => listeners.push(['disposed']) }
  }, {
    invalidateQueries(query) { invalidations.push(query) }
  })
  assert.equal(listeners[0][0], '*')
  listeners[0][1]({ type: 'kanban.updated' })
  assert.deepEqual(invalidations, [{ queryKey: ['session-hub'] }])
  dispose()
  assert.equal(listeners.at(-1)[0], 'disposed')
})

test('project task opens the origin session from exact-card provenance, never the assignee', async () => {
  const opened = []
  const controller = createKanbanController({
    openSession: async selection => { opened.push(selection); return { opened: true } },
    showTask: async () => {},
    resolveOrigin: async () => ({ available: true, ownerProfile: 'default', storedSessionId: '20260101_000000_abcdef' })
  })
  const result = await controller.openOrigin({ id: 't_card', ownerProfile: 'ops', sessionId: '20260101_000000_abcdef' })
  assert.equal(result.opened, true)
  assert.deepEqual(opened, [{ ownerProfile: 'default', storedSessionId: '20260101_000000_abcdef' }])
})

test('unavailable or failing origin lookup never opens a substitute session', async () => {
  const opened = []
  const base = { openSession: async selection => { opened.push(selection); return { opened: true } }, showTask: async () => {} }
  const missing = createKanbanController({ ...base, resolveOrigin: async () => ({ available: false, reason: 'stale' }) })
  const failing = createKanbanController({ ...base, resolveOrigin: async () => { throw new Error('timeout') } })
  assert.deepEqual(await missing.openOrigin({ id: 't_card' }), { opened: false, reason: 'stale' })
  assert.equal((await failing.openOrigin({ id: 't_card' })).opened, false)
  assert.deepEqual(opened, [])
})
