function element(type, props, key) {
  return { type, key: key ?? null, props: props || {} }
}

export function jsx(type, props, key) {
  return element(type, props, key)
}

export function jsxs(type, props, key) {
  return element(type, props, key)
}
