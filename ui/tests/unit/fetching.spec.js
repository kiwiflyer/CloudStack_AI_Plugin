import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'

import EvaluationsTab from '@/views/aiservices/EvaluationsTab.vue'
import FleetHealth from '@/views/aiservices/FleetHealth.vue'
import GpuCapacity from '@/views/aiservices/GpuCapacity.vue'
import HuggingFaceBrowser from '@/views/aiservices/HuggingFaceBrowser.vue'
import SetupWizard from '@/views/aiservices/SetupWizard.vue'
import { apiCalls, reset } from '../doubles/api.js'

// CloudStack renders a section's `component` as the route view and
// passes it nothing. A screen that only accepts props therefore renders
// empty in the real UI however well it tests — so each one has to fetch
// for itself.

describe('route screens fetch their own data', () => {
  beforeEach(() => reset())

  it('the setup wizard asks for its checks', async () => {
    mount(SetupWizard)
    await flushPromises()

    expect(apiCalls().map((c) => c.command)).toContain('listAiSetupChecks')
  })

  it('the heatmap asks for capacity', async () => {
    mount(GpuCapacity)
    await flushPromises()

    expect(apiCalls().map((c) => c.command)).toContain('listAiGpuCapacity')
  })

  it('fleet health asks for capacity too', async () => {
    mount(FleetHealth)
    await flushPromises()

    expect(apiCalls().map((c) => c.command)).toContain('listAiGpuCapacity')
  })

  it('the Hugging Face browser does not search until asked', async () => {
    // Searching on mount would hit the Hub for every operator who
    // opened the screen, and the results would be meaningless.
    mount(HuggingFaceBrowser)
    await flushPromises()

    expect(apiCalls()).toHaveLength(0)
  })

  it('a screen given data does not fetch', async () => {
    // Which is how the rendering tests stay honest: they render what
    // they were given, not whatever a double answered.
    mount(SetupWizard, { props: { checks: [] } })
    await flushPromises()

    expect(apiCalls()).toHaveLength(0)
  })

  it('renders what came back', async () => {
    reset({
      // Keyed by command; the value is the envelope CloudStack returns.
      listAiSetupChecks: {
        listaisetupchecksresponse: {
          checks: [
            { name: 'model registry', status: 'fail', detail: 'x', remedy: 'Fix it.' }
          ]
        }
      }
    })

    const wrapper = mount(SetupWizard)
    await flushPromises()

    expect(wrapper.text()).toContain('Fix it.')
  })

  it('an API failure leaves the screen empty rather than broken', async () => {
    reset({})
    const wrapper = mount(GpuCapacity)
    await flushPromises()

    expect(wrapper.findAll('[data-test="cell"]')).toHaveLength(0)
  })
})

describe('the evaluations tab', () => {
  // Evaluations cannot be a screen of their own: listAiEvaluations is
  // scoped to a model (GET /v1/models/{model_name}/evaluations), so
  // there is no model-less list to draw. It is a tab on the registry,
  // where the model is the row you are already looking at.
  beforeEach(() => reset())

  it('asks for the evaluations of the model it was given', async () => {
    mount(EvaluationsTab, { props: { resource: { name: 'llama-3.3-70b-instruct' } } })
    await flushPromises()

    expect(apiCalls()).toEqual([
      { command: 'listAiEvaluations', params: { model: 'llama-3.3-70b-instruct' } }
    ])
  })

  it('asks for nothing until it has a model', async () => {
    // CloudStack mounts a tab before the detail view has loaded its
    // row; firing then would ask for the evaluations of undefined.
    mount(EvaluationsTab, { props: { resource: {} } })
    await flushPromises()

    expect(apiCalls()).toHaveLength(0)
  })

  it('refetches when the row changes', async () => {
    const wrapper = mount(EvaluationsTab, { props: { resource: {} } })
    await wrapper.setProps({ resource: { name: 'qwen-2.5-72b' } })
    await flushPromises()

    expect(apiCalls().map((c) => c.params.model)).toEqual(['qwen-2.5-72b'])
  })

  it('shows the gate result, not just the score', async () => {
    // The number alone does not say whether publishing was blocked,
    // which is the only thing the gate is for (SPEC.md section 8).
    reset({
      listAiEvaluations: {
        listaievaluationsresponse: {
          evaluations: [
            { id: 'e1', model: 'm', revision: 'abc123', passed: false, overridden: false }
          ]
        }
      }
    })

    const wrapper = mount(EvaluationsTab, { props: { resource: { name: 'm' } } })
    await flushPromises()

    expect(wrapper.text()).toMatch(/blocked/i)
  })

  it('says when a root admin published past the gate', async () => {
    reset({
      listAiEvaluations: {
        listaievaluationsresponse: {
          evaluations: [
            { id: 'e1', model: 'm', revision: 'abc', passed: false, overridden: true }
          ]
        }
      }
    })

    const wrapper = mount(EvaluationsTab, { props: { resource: { name: 'm' } } })
    await flushPromises()

    expect(wrapper.text()).toMatch(/overridden/i)
  })
})
