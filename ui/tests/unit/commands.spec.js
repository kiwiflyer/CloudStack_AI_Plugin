import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import section from '@/config/section/aiservices'

// Every command the engine maps, read from the single source of truth.
// CLAUDE.md: commands are generated from aisvc/api/commands.py, never
// hand-listed — and that has to hold across the UI boundary too, or a
// renamed command leaves a button that 403s with no sign of why.
// Vitest runs with this package as its root, so the engine's map is one
// directory up. Read rather than imported: it is Python.
const commandsPy = readFileSync(
  resolve(process.cwd(), '../aisvc/api/commands.py'),
  'utf8'
)
const ENGINE_COMMANDS = new Set(
  [...commandsPy.matchAll(/_c\(\s*"([A-Za-z]+)"/g)].map((m) => m[1])
)

function screens (node = section, found = []) {
  for (const child of node.children ?? []) {
    found.push(child)
    screens(child, found)
  }
  return found
}

function referencedApis () {
  const names = new Set()
  for (const screen of screens()) {
    for (const permission of screen.permission ?? []) names.add(permission)
    for (const action of screen.actions ?? []) {
      if (action.api) names.add(action.api)
    }
  }
  return names
}

describe('the section only calls commands the engine maps', () => {
  it('found the engine command map', () => {
    expect(ENGINE_COMMANDS.size).toBeGreaterThan(20)
  })

  it('references no command the plugin does not expose', () => {
    const unknown = [...referencedApis()].filter((api) => !ENGINE_COMMANDS.has(api))
    expect(unknown).toEqual([])
  })

  it('every screen declares the permission that lists it', () => {
    for (const screen of screens()) {
      expect(screen.permission, `${screen.name} has no permission`).toBeTruthy()
      expect(screen.permission.length).toBeGreaterThan(0)
    }
  })

  it('every action names an api or is a plain link', () => {
    for (const screen of screens()) {
      for (const action of screen.actions ?? []) {
        expect(action.api || action.component, `${action.label} does nothing`).toBeTruthy()
      }
    }
  })
})
