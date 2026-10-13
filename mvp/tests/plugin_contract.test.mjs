import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { Script } from 'node:vm'
import { test } from 'node:test'

const pluginPath = new URL('../plugins/session-hub/desktop/plugin.js', import.meta.url)

async function loadPluginSandbox() {
  let source = await readFile(pluginPath, 'utf8')
  source = source
    .replace(/^import .+$/gm, '')
    .replace(/export const /g, 'const ')
    .replace(/export function /g, 'function ')
    .replace(/export default /g, 'const defaultExport = ')
  source += '\n;({ defaultExport, applyHubConfig, createOpenController, getInitialPageSize, PAGE_SIZE_CHOICES, SESSION_HUB_ROUTE })'
  const calls = []
  const storage = new Map()
  const sandbox = {
    ROUTES_AREA: 'routes',
    SIDEBAR_NAV_AREA: 'sidebar.nav',
    PALETTE_AREA: 'palette',
    host: {
      navigate: path => calls.push(['navigate', path]),
      openSession: async (id, opts) => {
        calls.push(['openSession', id, opts])
        return { ok: true, id, opts }
      },
      notify: notice => calls.push(['notify', notice]),
      onEvent: (type, callback) => {
        calls.push(['onEvent', type, typeof callback])
        return () => calls.push(['disposeEvent'])
      }
    },
    useQuery: options => ({ data: { items: [] }, isFetching: false, refetch: () => calls.push(['refetch']), options }),
    useQueryClient: () => ({ invalidateQueries: args => calls.push(['invalidate', args]) }),
    queryClient: { invalidateQueries: args => calls.push(['invalidate', args]) },
    jsx: (type, props) => ({ type, props }),
    jsxs: (type, props) => ({ type, props }),
    calls,
    console: { error() {} }
  }
  const loaded = new Script(source).runInNewContext(sandbox)
  return { ...loaded, calls, storage }
}

test('plugin imports only official Desktop SDK, react, and jsx runtime', async () => {
  const source = await readFile(pluginPath, 'utf8')
  const imports = [...source.matchAll(/^\s*import\s+.+?\s+from\s+['"]([^'"]+)['"]/gm)].map(match => match[1])
  assert.deepEqual(imports.sort(), ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'].sort())
  assert.equal(/\bimport\s*\(/.test(source), false)
  assert.equal(/https?:\/\//.test(source), false)
})

test('registers official route, sidebar nav, and palette command for session hub', async () => {
  const { defaultExport, SESSION_HUB_ROUTE, calls } = await loadPluginSandbox()
  const registered = []
  const ctx = {
    storage: { get: () => 20, set: () => undefined },
    registerMany(items) {
      registered.push(...items)
      return () => undefined
    },
    rest: async () => ({ items: [] })
  }
  defaultExport.register(ctx)
  assert.equal(SESSION_HUB_ROUTE, '/session-hub')
  assert.deepEqual(registered.map(item => [item.area, item.data?.path ?? item.data?.id]), [
    ['routes', '/session-hub'],
    ['sidebar.nav', '/session-hub'],
    ['palette', 'sessionHub.open']
  ])
  assert.deepEqual(calls[0], ['onEvent', '*', 'function'])
  calls.length = 0
  registered[2].data.run()
  assert.deepEqual(calls[0], ['navigate', '/session-hub'])
})

test('page size defaults to 20 and persists only supported choices', async () => {
  const { getInitialPageSize, PAGE_SIZE_CHOICES } = await loadPluginSandbox()
  assert.equal(JSON.stringify(PAGE_SIZE_CHOICES), JSON.stringify([10, 20, 50, 100, 'all']))
  assert.equal(getInitialPageSize({ get: () => undefined }), 20)
  assert.equal(getInitialPageSize({ get: () => 50 }), 50)
  assert.equal(getInitialPageSize({ get: () => 2 }), 20)
})

test('plugin page uses 30s React Query refresh and exposes manual refresh', async () => {
  const source = await readFile(pluginPath, 'utf8')
  assert.match(source, /refetchInterval:\s*30000/)
  assert.match(source, /refetch\(\)/)
  assert.match(source, /ctx\.storage\.set\(PAGE_SIZE_KEY/)
})

test('click revalidates owner and opens current window in place', async () => {
  const { applyHubConfig, createOpenController, calls } = await loadPluginSandbox()
  assert.equal(applyHubConfig({ contract: 'session-hub-config-v1', localProfiles: ['default', 'review'] }), true)
  const backend = {
    async revalidate(selection) {
      assert.deepEqual(selection, { ownerProfile: 'review', storedSessionId: 'fixture-session' })
      return { ownerProfile: 'review', storedSessionId: 'fixture-session' }
    }
  }
  const controller = createOpenController({ backend, host: {
    openSession: async (id, opts) => calls.push(['openSession', id, opts])
  } })
  await controller.open({ ownerProfile: 'review', storedSessionId: 'fixture-session' })
  assert.equal(JSON.stringify(calls[0]), JSON.stringify(['openSession', 'fixture-session', {
    profile: 'review',
    intent: 'in-place',
    keepAllProfilesScope: false
  }]))
})

test('rapid click handling is last-wins and ownership mismatch does not open', async () => {
  const { createOpenController } = await loadPluginSandbox()
  const opened = []
  let resolveFirst
  const first = new Promise(resolve => { resolveFirst = resolve })
  const backend = {
    revalidate(selection) {
      if (selection.storedSessionId === 'first') return first
      if (selection.storedSessionId === 'bad') throw new Error('mismatch')
      return Promise.resolve(selection)
    }
  }
  const controller = createOpenController({
    backend,
    host: { openSession: async id => opened.push(id) }
  })
  const firstOpen = controller.open({ ownerProfile: 'default', storedSessionId: 'first' })
  const secondOpen = controller.open({ ownerProfile: 'default', storedSessionId: 'second' })
  resolveFirst({ ownerProfile: 'default', storedSessionId: 'first' })
  await Promise.allSettled([firstOpen, secondOpen])
  await controller.open({ ownerProfile: 'default', storedSessionId: 'bad' })
  assert.deepEqual(opened, ['second'])
})
