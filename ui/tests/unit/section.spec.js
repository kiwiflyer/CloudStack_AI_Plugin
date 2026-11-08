import { beforeEach, describe, expect, it } from 'vitest'

import section from '@/config/section/aiservices'
import { asRole } from '../doubles/store.js'

function screens (node = section, found = []) {
  for (const child of node.children ?? []) {
    found.push(child)
    screens(child, found)
  }
  return found
}

const byName = (name) => screens().find((s) => s.name === name)

function visibleTo (roletype) {
  asRole(roletype)
  return screens()
    .filter((screen) => (screen.show ? screen.show() : true))
    .map((screen) => screen.name)
}

describe('the section itself', () => {
  beforeEach(() => asRole('Admin'))

  it('is named for its route', () => {
    expect(section.name).toBe('aiservices')
  })

  it('has a title key rather than a literal', () => {
    expect(section.title).toMatch(/^label\./)
  })

  it('has an icon', () => {
    expect(section.icon).toBeTruthy()
  })
})

describe('SPEC.md section 9 screens', () => {
  beforeEach(() => asRole('Admin'))

  // Tenant screens come first: milestone 6 is "tenant first", and a
  // tenant should land on their own catalog, not an empty admin list.
  it.each([
    'aimodelcatalog',
    'aiendpoint',
    'aiplayground',
    'aigenerationprofile',
    'aieffectiveparameters',
    'aiusage'
  ])('has the tenant screen %s', (name) => {
    expect(byName(name)).toBeTruthy()
  })

  it.each([
    'aisetup',
    'aihuggingface',
    'airegistry',
    'aicapacity',
    'aifleet',
    'airollout',
    'aiservingprofile',
    'aiactivity'
  ])('has the admin screen %s', (name) => {
    expect(byName(name)).toBeTruthy()
  })

  it('lists the tenant screens before the admin ones', () => {
    const names = screens().map((s) => s.name)
    expect(names.indexOf('aimodelcatalog')).toBeLessThan(names.indexOf('aisetup'))
  })

  it.each([
    'aiadapter',
    'aibatch',
    'aicertificate',
    'aiprivateendpoint',
    'aiguardrail'
  ])('has the v2 screen %s', (name) => {
    expect(byName(name)).toBeTruthy()
  })
})

// CloudStack's own gate. `filterAsyncRouter` drops any route whose
// `permission` entries are not in the account's `listApis` answer, and
// `hasAccessToSection` then drops a section left with no children. So
// a v2 screen disappears on its own when ai.services.v2.enabled is
// off: the plugin stops registering listAiAdapters, listApis stops
// naming it, and the route never reaches the menu.
//
// This is why no v2 screen carries a `show` flag of its own. A second
// switch is a switch that disagrees with the first one.
function reachable (availableApis) {
  const apis = new Set(availableApis)
  return screens()
    .filter((screen) => (screen.permission ?? []).every((api) => apis.has(api)))
    .map((screen) => screen.name)
}

const V1_APIS = [
  'listAiModels', 'listAiEndpoints', 'createAiPlaygroundSession',
  'listAiGenerationProfiles', 'getAiEffectiveParameters', 'listAiUsage',
  'listAiSetupChecks', 'searchAiHfModels', 'listAiGpuCapacity',
  'listAiModelDeployments', 'listAiServingProfiles', 'listAiReconcilerRuns'
]
const V2_APIS = [
  'listAiAdapters', 'listAiBatches', 'listAiCertificates',
  'listAiPrivateEndpoints', 'listAiGuardrailProfiles'
]

describe('v2 screens follow the plugin, not a flag of their own', () => {
  beforeEach(() => asRole('Admin'))

  it('declares no v2 screen with its own show flag', () => {
    // Visibility is the permission; a `show` here could contradict the
    // setting the plugin registers by.
    // aiguardrail included: the engine scopes guardrails per caller
    // now, so the UI has nothing left to compensate for.
    for (const name of ['aiadapter', 'aibatch', 'aicertificate', 'aiprivateendpoint', 'aiguardrail']) {
      expect(byName(name).show, `${name} second-guesses the plugin`).toBeUndefined()
    }
  })

  it('hides every v2 screen on a v1 install', () => {
    const visible = reachable(V1_APIS)
    for (const v2 of ['aiadapter', 'aibatch', 'aicertificate', 'aiprivateendpoint', 'aiguardrail']) {
      expect(visible, `${v2} showed up without its command`).not.toContain(v2)
    }
  })

  it('leaves the v1 screens alone on a v1 install', () => {
    const visible = reachable(V1_APIS)
    expect(visible).toContain('aimodelcatalog')
    expect(visible).toContain('aiendpoint')
  })

  it('shows them once the plugin registers v2', () => {
    const visible = reachable([...V1_APIS, ...V2_APIS])
    for (const v2 of ['aiadapter', 'aibatch', 'aicertificate', 'aiprivateendpoint', 'aiguardrail']) {
      expect(visible).toContain(v2)
    }
  })

  it('offers guardrails to a tenant', () => {
    // SPEC.md section 9's UI table lists Guardrails as a tenant
    // screen. It was admin-only while the engine's routes were, and
    // the engine scopes them per caller now.
    asRole('User')
    expect(byName('aiguardrail').show).toBeUndefined()
  })

  it('keeps every v2 screen behind a v2 command', () => {
    // The gate only works if the permission is one the plugin holds
    // back. A v2 screen permitted by a v1 command would always show.
    const v2 = new Set(V2_APIS)
    for (const name of ['aiadapter', 'aibatch', 'aicertificate', 'aiprivateendpoint', 'aiguardrail']) {
      expect(byName(name).permission.some((api) => v2.has(api)), name).toBe(true)
    }
  })
})

describe('role-based visibility', () => {
  it('shows tenants their own screens', () => {
    expect(visibleTo('User')).toContain('aimodelcatalog')
    expect(visibleTo('User')).toContain('aiendpoint')
  })

  it('hides admin screens from tenants', () => {
    const visible = visibleTo('User')
    for (const admin of ['aisetup', 'aicapacity', 'aifleet', 'airegistry']) {
      expect(visible).not.toContain(admin)
    }
  })

  it('shows admins everything', () => {
    const visible = visibleTo('Admin')
    expect(visible).toContain('aisetup')
    expect(visible).toContain('aimodelcatalog')
  })

  it('shows a domain admin the registry but not zone setup', () => {
    const visible = visibleTo('DomainAdmin')
    expect(visible).toContain('airegistry')
    expect(visible).not.toContain('aisetup')
  })

  it('hides the whole section from nobody with a role', () => {
    for (const role of ['Admin', 'DomainAdmin', 'User']) {
      asRole(role)
      expect(section.show()).toBe(true)
    }
  })
})

describe('list and detail columns', () => {
  beforeEach(() => asRole('Admin'))

  it('every screen has columns', () => {
    for (const screen of screens()) {
      const columns = typeof screen.columns === 'function' ? screen.columns() : screen.columns
      expect(columns, `${screen.name} has no columns`).toBeTruthy()
      expect(columns.length).toBeGreaterThan(0)
    }
  })

  it('every screen has details', () => {
    for (const screen of screens()) {
      const details = typeof screen.details === 'function' ? screen.details() : screen.details
      expect(details, `${screen.name} has no details`).toBeTruthy()
    }
  })

  it('shows the owning account to admins', () => {
    asRole('Admin')
    const columns = byName('aiendpoint').columns()
    expect(columns).toContain('account')
  })

  it('does not waste a column on the account for a tenant', () => {
    asRole('User')
    const columns = byName('aiendpoint').columns()
    expect(columns).not.toContain('account')
  })
})

describe('the components the section names', () => {
  // A typo in a dynamic import is a blank screen at runtime with
  // nothing in the console but a chunk load error.
  const referenced = new Set()
  for (const screen of screens()) {
    const sources = [screen.component, ...(screen.tabs ?? []).map((t) => t.component)]
    for (const source of sources) {
      if (source) referenced.add(source)
    }
  }

  it('names at least the screens that cannot be a plain list', () => {
    expect(referenced.size).toBeGreaterThan(5)
  })

  it('all resolve', async () => {
    // defineAsyncComponent defers the import; loading each one here is
    // what proves the path is right.
    for (const component of referenced) {
      await expect(component.value.__asyncLoader()).resolves.toBeTruthy()
    }
  })
})
