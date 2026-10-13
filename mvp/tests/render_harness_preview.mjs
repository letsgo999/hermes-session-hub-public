import { mkdir, writeFile } from 'node:fs/promises'

import plugin from '../plugins/session-hub/desktop/plugin.js'
import { createHarness, toHtml, walk } from './fixtures/renderer.mjs'
import './fixtures/hub-config.mjs'

const registered = []
const ctx = {
  storage: { get: () => 20, set() {} },
  os: { async writeClipboard() { return true } },
  rest: async () => ({ items: [] }),
  registerMany(entries) { registered.push(...entries) }
}
const sessionData = {
  items: [
    {
      ownerProfile: 'review', storedSessionId: 'fixture-review-1', title: 'Review planning session',
      source: 'desktop', telegramType: null, lastActivity: 1787770000, messageCount: 7,
      previews: ['Latest sanitized message', 'Second sanitized message', 'Third sanitized message']
    },
    {
      ownerProfile: 'builder', storedSessionId: 'fixture-builder-1', title: 'Automation implementation',
      source: 'desktop', telegramType: null, lastActivity: 1787769000, messageCount: 5,
      previews: ['Implementation checkpoint', 'Test result summary', 'Next safe local step']
    }
  ],
  page: { returned: 2, totalKnown: 2 },
  nextCursor: null
}
const sessionHarness = createHarness({
  queryResult: { data: sessionData, isLoading: false, isError: false, isFetching: false, error: null, refetch() {} }
})
plugin.register(ctx)
const page = registered.find(entry => entry.area === 'routes')
if (!page) throw new Error('Session Hub route was not registered')
sessionHarness.mount(() => page.render())

function mountedHarness(data) {
  const harness = createHarness({
    queryResult: { data, isLoading: false, isError: false, isFetching: false, error: null, refetch() {} }
  })
  harness.mount(() => page.render())
  return harness
}

function htmlDocument(title, tree) {
  return `<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title>
<style>
:root{color-scheme:light dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif}body{margin:0;background:#111827;color:#f3f4f6}section{max-width:1200px;margin:24px auto;border:1px solid #374151;border-radius:12px;overflow:hidden}header,section>div,main{padding:14px 18px;border-bottom:1px solid #374151}button,input,select{font:inherit;color:inherit;background:#1f2937;border:1px solid #4b5563;border-radius:6px;padding:7px 10px}button{cursor:pointer}label{margin-right:12px}fieldset{border:0;margin:6px 0;padding:0}article{border:1px solid #374151;border-radius:8px;padding:12px;margin:10px 0}h1{margin:0 0 4px}h2{margin:0}ul{margin:8px 0 0;padding-left:0;list-style:none}code{display:block;overflow-wrap:anywhere;color:#cbd5e1}nav{display:flex;justify-content:space-between;margin-top:12px}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
</style></head><body>${toHtml(tree)}</body></html>`
}

const sessionDocument = htmlDocument('Hermes Session Hub loader harness', sessionHarness.tree)

const skill = {
  id: 'sample-skill', title: 'sample-skill',
  description: 'Use when a new automation application needs a verified implementation blueprint.',
  triggers: 'Use when automation requirements must become a reproducible build plan.',
  tags: ['automation', 'blueprint', 'PDCA'], statuses: ['active', 'backup'],
  deployedProfiles: ['default', 'ops', 'review', 'builder', 'search'],
  canonical: { status: 'verified', absolutePath: 'D:/SharedSkills/sample-skill/SKILL.md', evidence: 'sha256_match_active' },
  runtimeCopies: [
    { profile: 'default', status: 'active', absolutePath: 'C:/Users/me/AppData/Local/hermes/skills/sample-skill/SKILL.md' },
    { profile: 'builder', status: 'active', absolutePath: 'C:/Users/me/AppData/Local/hermes/profiles/builder/skills/sample-skill/SKILL.md' }
  ],
  otherCopies: [], matchReason: 'title · description', snippet: 'automation application needs a verified implementation blueprint', score: 160
}
const skillsData = { items: [skill], page: { returned: 1, totalKnown: 1 }, refreshedAt: 1790403000, sources: [] }
const skillsHarness = mountedHarness(skillsData)
let skillsTab = null
walk(skillsHarness.tree, node => {
  if (node.props?.role === 'tab' && node.props?.children === 'Skills') skillsTab = node
})
if (!skillsTab) throw new Error('Skills tab was not rendered')
skillsTab.props.onClick()
const skillsDocument = htmlDocument('Hermes Session Hub Skills preview', skillsHarness.tree)

const outputDir = new URL('../evidence/', import.meta.url)
const sessionOutput = new URL('../evidence/ui-harness.html', import.meta.url)
const skillsOutput = new URL('../evidence/skills-ui-harness.html', import.meta.url)
await mkdir(outputDir, { recursive: true })
await Promise.all([
  writeFile(sessionOutput, sessionDocument, 'utf8'),
  writeFile(skillsOutput, skillsDocument, 'utf8')
])
console.log(JSON.stringify({
  ok: true,
  output: sessionOutput.pathname,
  skillsOutput: skillsOutput.pathname,
  actualComponentRendered: true,
  sessionCards: sessionData.items.length,
  tab: 'Skills',
  skillCards: 1
}))
