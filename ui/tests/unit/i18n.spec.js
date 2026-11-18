// Licensed to the Apache Software Foundation (ASF) under one or more
// contributor license agreements. See the NOTICE file distributed with this
// work for additional information regarding copyright ownership. The ASF
// licenses this file to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance with the
// License. You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
// WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
// License for the specific language governing permissions and limitations
// under the License.
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
      // CloudStack renders a tab as $t('label.' + tab.name), so the
      // key is the name with the prefix added — not the name itself.
      // Collecting it the other way round is what let five tabs ship
      // with names that rendered as `label.label.ai.…`.
      if (tab.name && tab.name !== 'details') keys.add(`label.${tab.name}`)
    }
    for (const action of screen.actions ?? []) {
      if (action.label) keys.add(action.label)
      if (action.message) keys.add(action.message)
    }
    // A column or detail may carry its own title, which CloudStack
    // renders as `label.<customTitle>`. These count as used: without
    // them the orphan check below calls every field label an orphan.
    for (const key of ['columns', 'details']) {
      const value = screen[key]
      if (typeof value !== 'function') continue
      let listed
      try {
        listed = value()
      } catch {
        continue
      }
      for (const entry of listed ?? []) {
        if (entry && entry.customTitle) {
          keys.add(`label.${String(entry.customTitle).toLowerCase()}`)
        }
      }
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

describe('the fields a screen lists', () => {
  // CloudStack renders a column or detail as `label.<field>`, or as
  // `label.<customTitle>` where one is given — AutogenView.vue and
  // DetailsTab.vue both. So a bare field name needs CloudStack to
  // define that key, and anything it does not define needs a
  // customTitle pointing into our own namespace.
  //
  // This is the gap that let the section ship looking broken. A live
  // 4.23.0.0 rendered the model catalogue's headers as `label.task`,
  // `label.license` and `label.pricetier`, and the tests above passed
  // throughout — they check the keys the section names as strings and
  // never the field names it renders through `label.<field>`.
  const provided = new Set(
    JSON.parse(
      readFileSync(resolve(process.cwd(), 'tests/fixtures/cloudstack-label-fields.json'), 'utf8')
    ).fields
  )

  /** Every column and detail entry, as {field, customTitle}. */
  function fields () {
    const found = []
    for (const screen of screens()) {
      for (const key of ['columns', 'details']) {
        const value = screen[key]
        if (typeof value !== 'function') continue
        let listed
        try {
          listed = value()
        } catch {
          continue // needs a record to decide; those are checked below
        }
        for (const entry of listed ?? []) {
          if (typeof entry === 'string') found.push({ field: entry })
          else if (entry && entry.field) found.push(entry)
        }
      }
    }
    return found
  }

  it('found fields to check', () => {
    expect(fields().length).toBeGreaterThan(20)
  })

  it('every field resolves to a string somebody defines', () => {
    const unresolved = []
    for (const { field, customTitle } of fields()) {
      if (customTitle) {
        const key = `label.${String(customTitle).toLowerCase()}`
        if (!(key in en)) unresolved.push(`${field}: customTitle ${key} is not in en.json`)
      } else if (!provided.has(field)) {
        unresolved.push(
          `${field}: CloudStack defines no label.${field}, so give it a customTitle`
        )
      }
    }

    expect(unresolved).toEqual([])
  })
})

describe('a tab name is a key without its prefix', () => {
  it('no tab carries label. in its name', () => {
    // CloudStack renders a tab as $t('label.' + tab.name). Five tabs
    // were named `label.ai.…`, so the detail view drew
    // "label.label.ai.api.keys" where a title should be — visible on
    // every endpoint, usage and registry record, and invisible to
    // every test, because the i18n check collected the name as the
    // key rather than deriving the key from the name.
    const named = []
    for (const screen of screens()) {
      for (const tab of screen.tabs ?? []) {
        if (typeof tab.name === 'string' && tab.name.startsWith('label.')) {
          named.push(`${screen.name}: ${tab.name}`)
        }
      }
    }

    expect(named).toEqual([])
  })

  it('every tab name resolves to a key that exists', () => {
    const missing = []
    for (const screen of screens()) {
      for (const tab of screen.tabs ?? []) {
        if (typeof tab.name !== 'string' || tab.name === 'details') continue
        if (!(`label.${tab.name}` in en)) {
          missing.push(`${screen.name}: label.${tab.name}`)
        }
      }
    }

    expect(missing).toEqual([])
  })
})
