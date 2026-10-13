function bridge() {
  if (!globalThis.__SESSION_HUB_HARNESS__) throw new Error('Session Hub test harness is not mounted')
  return globalThis.__SESSION_HUB_HARNESS__
}

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'

export const host = {
  navigate(...args) { return bridge().host.navigate(...args) },
  openSession(...args) { return bridge().host.openSession(...args) },
  notify(...args) { return bridge().host.notify(...args) },
  newChat(...args) { return bridge().host.newChat?.(...args) },
  onEvent(...args) { return bridge().host.onEvent?.(...args) ?? (() => undefined) }
}

export const queryClient = {
  invalidateQueries(...args) { return bridge().queryClient?.invalidateQueries(...args) }
}

export function useQuery(options) {
  return bridge().useQuery(options)
}
