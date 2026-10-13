function bridge() {
  if (!globalThis.__SESSION_HUB_HARNESS__) throw new Error('Session Hub test harness is not mounted')
  return globalThis.__SESSION_HUB_HARNESS__
}

export function useState(initial) {
  return bridge().useState(initial)
}
