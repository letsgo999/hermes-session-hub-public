function resolveNode(node) {
  if (node == null || typeof node === 'boolean') return null
  if (Array.isArray(node)) return node.map(resolveNode).filter(value => value != null)
  if (typeof node !== 'object' || !('type' in node)) return node
  if (typeof node.type === 'function') return resolveNode(node.type(node.props || {}))
  const props = { ...(node.props || {}) }
  if ('children' in props) props.children = resolveNode(props.children)
  return { type: node.type, key: node.key ?? null, props }
}

export function textContent(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (Array.isArray(node)) return node.map(textContent).join(' ')
  if (typeof node !== 'object') return String(node)
  return textContent(node.props?.children)
}

export function walk(node, visit) {
  if (node == null) return
  if (Array.isArray(node)) {
    for (const child of node) walk(child, visit)
    return
  }
  if (typeof node !== 'object') return
  visit(node)
  walk(node.props?.children, visit)
}

export function toHtml(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (Array.isArray(node)) return node.map(toHtml).join('')
  if (typeof node !== 'object') return escapeHtml(String(node))
  const props = node.props || {}
  const attributes = Object.entries(props)
    .filter(([key, value]) => key !== 'children' && key !== 'className' && !key.startsWith('on') && value != null && value !== false)
    .map(([key, value]) => {
      const name = key === 'htmlFor' ? 'for' : key
      if (value === true) return name
      return `${name}="${escapeHtml(String(value))}"`
    })
  if (props.className) attributes.push(`class="${escapeHtml(String(props.className))}"`)
  const voidElements = new Set(['input', 'meta', 'link', 'br', 'hr'])
  const opening = `<${node.type}${attributes.length ? ` ${attributes.join(' ')}` : ''}>`
  return voidElements.has(node.type) ? opening : `${opening}${toHtml(props.children)}</${node.type}>`
}

function escapeHtml(value) {
  return value.replace(/[&<>\"]/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[character])
}

export function createHarness({ queryResult, host } = {}) {
  const states = []
  let hookIndex = 0
  let rootFactory = null
  let currentQuery = queryResult || { data: { items: [], page: { returned: 0, totalKnown: 0 }, nextCursor: null }, isLoading: false, isError: false, isFetching: false, refetch() {} }
  const harness = {
    tree: null,
    queryOptions: null,
    host: host || { navigate() {}, async openSession() {}, notify() {} },
    useState(initial) {
      const index = hookIndex++
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
      const setState = value => {
        states[index] = typeof value === 'function' ? value(states[index]) : value
        harness.rerender()
      }
      return [states[index], setState]
    },
    useQuery(options) {
      harness.queryOptions = options
      return currentQuery
    },
    mount(factory) {
      rootFactory = factory
      return harness.rerender()
    },
    rerender() {
      if (!rootFactory) return null
      hookIndex = 0
      harness.tree = resolveNode(rootFactory())
      return harness.tree
    },
    setQuery(next) {
      currentQuery = next
      return harness.rerender()
    },
    findAll(predicate) {
      const found = []
      walk(harness.tree, node => { if (predicate(node)) found.push(node) })
      return found
    },
    find(predicate, label = 'node') {
      const found = harness.findAll(predicate)
      if (found.length !== 1) throw new Error(`Expected one ${label}, found ${found.length}`)
      return found[0]
    },
    text(node = harness.tree) { return textContent(node).replace(/\s+/g, ' ').trim() },
    states
  }
  globalThis.__SESSION_HUB_HARNESS__ = harness
  return harness
}
