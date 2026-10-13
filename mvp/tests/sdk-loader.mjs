export async function resolve(specifier, context, nextResolve) {
  const fixtures = new URL('./fixtures/', import.meta.url)
  const mapped = {
    '@hermes/plugin-sdk': new URL('sdk.mjs', fixtures).href,
    react: new URL('react.mjs', fixtures).href,
    'react/jsx-runtime': new URL('jsx-runtime.mjs', fixtures).href
  }
  if (Object.hasOwn(mapped, specifier)) {
    return { url: mapped[specifier], shortCircuit: true }
  }
  return nextResolve(specifier, context)
}
