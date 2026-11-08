import { describe, expect, it } from 'vitest'

import { cellFor, heatmapRows } from '@/utils/capacity'
import { describeParameter, sourceLabel } from '@/utils/profiles'
import { canProceed, groupChecks } from '@/utils/setup'

describe('the GPU capacity heatmap', () => {
  const capacity = (over = {}) => ({
    zone: 'zone-east-1',
    gpuclass: 'h100-8x',
    total: 16,
    allocated: 8,
    free: 8,
    unhealthy: 0,
    ...over
  })

  it('reports utilisation as a fraction', () => {
    expect(cellFor(capacity()).utilisation).toBeCloseTo(0.5)
  })

  it('a class with no GPUs is not an error, just empty', () => {
    // A declared class whose hosts are not tagged yet; dividing by zero
    // here would render NaN% in the operator's first screen.
    expect(cellFor(capacity({ total: 0, allocated: 0, free: 0 })).utilisation).toBe(0)
  })

  it('full capacity reads as full', () => {
    expect(cellFor(capacity({ allocated: 16, free: 0 })).utilisation).toBe(1)
  })

  it('any unhealthy GPU outranks utilisation in the status', () => {
    // An operator scanning the heatmap needs broken hardware to stand
    // out even when the class is otherwise quiet.
    const cell = cellFor(capacity({ allocated: 0, free: 15, unhealthy: 1 }))
    expect(cell.status).toBe('unhealthy')
  })

  it('a full class is flagged before it is exhausted', () => {
    expect(cellFor(capacity({ allocated: 15, free: 1 })).status).toBe('tight')
  })

  it('an exhausted class is flagged as such', () => {
    expect(cellFor(capacity({ allocated: 16, free: 0 })).status).toBe('exhausted')
  })

  it('a quiet class is healthy', () => {
    expect(cellFor(capacity({ allocated: 2, free: 14 })).status).toBe('healthy')
  })

  it('groups classes into one row per zone', () => {
    const rows = heatmapRows([
      capacity(),
      capacity({ gpuclass: 'l40s-1x' }),
      capacity({ zone: 'zone-west-1' })
    ])

    expect(rows.map((r) => r.zone)).toEqual(['zone-east-1', 'zone-west-1'])
    expect(rows[0].cells).toHaveLength(2)
  })

  it('has nothing to show for no capacity', () => {
    expect(heatmapRows([])).toEqual([])
  })
})

describe('effective parameters', () => {
  it('names the level a value came from', () => {
    // SPEC.md section 6: getAiEffectiveParameters reports both the
    // resolved value and its source level. A bare number is useless —
    // the question is always "why is it that".
    expect(sourceLabel('account')).toBe('Account')
    expect(sourceLabel('apikey')).toBe('API key')
  })

  it('falls back to the raw level rather than hiding it', () => {
    expect(sourceLabel('something-new')).toBe('something-new')
  })

  it('describes a resolved parameter', () => {
    const described = describeParameter({
      name: 'temperature', value: 0.2, source: 'domain', policy: 'locked'
    })

    expect(described.value).toBe(0.2)
    expect(described.source).toBe('Domain')
    expect(described.locked).toBe(true)
  })

  it('marks a clamped value as adjustable within bounds', () => {
    const described = describeParameter({
      name: 'temperature', value: 0.7, source: 'account', policy: 'clamp'
    })

    expect(described.locked).toBe(false)
    expect(described.editable).toBe(true)
  })

  it('a locked value cannot be edited', () => {
    const described = describeParameter({
      name: 'temperature', value: 0.2, source: 'domain', policy: 'locked'
    })

    expect(described.editable).toBe(false)
  })

  it('a default is editable', () => {
    expect(describeParameter({
      name: 'top_p', value: 0.9, source: 'platform', policy: 'default'
    }).editable).toBe(true)
  })
})

describe('the setup wizard', () => {
  const check = (status, over = {}) => ({
    name: 'model registry', status, detail: 'd', remedy: '', ...over
  })

  it('groups checks by status', () => {
    const grouped = groupChecks([check('pass'), check('fail'), check('pass')])

    expect(grouped.pass).toHaveLength(2)
    expect(grouped.fail).toHaveLength(1)
  })

  it('always has every group, so the UI need not guard', () => {
    const grouped = groupChecks([])

    expect(Object.keys(grouped).sort()).toEqual(['fail', 'pass', 'skipped', 'warn'])
  })

  it('may proceed when nothing failed', () => {
    expect(canProceed([check('pass'), check('warn')])).toBe(true)
  })

  it('may not proceed with a failure', () => {
    expect(canProceed([check('pass'), check('fail')])).toBe(false)
  })

  it('may proceed with skipped checks', () => {
    // A new zone has not been observed yet; that is the normal first
    // run, not a reason to block the wizard.
    expect(canProceed([check('skipped'), check('pass')])).toBe(true)
  })

  it('may proceed when there is nothing to check', () => {
    expect(canProceed([])).toBe(true)
  })
})
