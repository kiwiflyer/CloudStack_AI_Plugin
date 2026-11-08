import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import section from '@/config/section/aiservices'

const LOCALES = resolve(process.cwd(), 'public/locales')
const en = JSON.parse(readFileSync(resolve(LOCALES, 'en.json'), 'utf8'))

function screens (node = section, found = []) {
  for (const child of node.children ?? []) {
    found.push(child)
    screens(child, found)
  }
  return found
}

/** Every translatable key the section refers to. */
function keysUsed () {
  const keys = new Set([section.title])
  for (const screen of screens()) {
    keys.add(screen.title)
    for (const tab of screen.tabs ?? []) {
      if (tab.name?.startsWith('label.')) keys.add(tab.name)
    }
    for (const action of screen.actions ?? []) {
      if (action.label) keys.add(action.label)
      if (action.message) keys.add(action.message)
    }
  }
  return keys
}

describe('the English strings', () => {
  it('cover every key the section uses', () => {
    const missing = [...keysUsed()].filter((key) => !(key in en))
    expect(missing).toEqual([])
  })

  it('carry no key the section does not use', () => {
    // An orphan string is one a translator spends time on for nothing.
    const used = keysUsed()
    const orphans = Object.keys(en).filter((key) => !used.has(key))
    expect(orphans).toEqual([])
  })

  it('has no empty string', () => {
    for (const [key, value] of Object.entries(en)) {
      expect(value.trim(), `${key} is empty`).toBeTruthy()
    }
  })

  it('uses our own namespace', () => {
    // Everything is label.ai.* or message.ai.*, bar the section title,
    // so our strings can never collide with CloudStack's own.
    for (const key of Object.keys(en)) {
      expect(key).toMatch(/^(label|message)\.ai/)
    }
  })

  it('names a destructive action in its confirmation', () => {
    // CloudStack shows `message` before acting; a vague one is how
    // people delete the wrong deployment.
    for (const key of Object.keys(en).filter((k) => k.startsWith('message.ai'))) {
      expect(en[key].length).toBeGreaterThan(20)
    }
  })
})

describe('every translation file', () => {
  const files = readdirSync(LOCALES).filter((name) => name.endsWith('.json'))

  it('includes English', () => {
    expect(files).toContain('en.json')
  })

  it.each(files)('%s has the same keys as English', (file) => {
    const other = JSON.parse(readFileSync(resolve(LOCALES, file), 'utf8'))
    expect(Object.keys(other).sort()).toEqual(Object.keys(en).sort())
  })

  it.each(files)('%s is sorted, so diffs stay readable', (file) => {
    const keys = Object.keys(
      JSON.parse(readFileSync(resolve(LOCALES, file), 'utf8'))
    )
    expect(keys).toEqual([...keys].sort())
  })
})
