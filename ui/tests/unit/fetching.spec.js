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
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'

import EvaluationsTab from '@/views/aiservices/EvaluationsTab.vue'
import FleetHealth from '@/views/aiservices/FleetHealth.vue'
import GpuCapacity from '@/views/aiservices/GpuCapacity.vue'
import HuggingFaceBrowser from '@/views/aiservices/HuggingFaceBrowser.vue'
import ApiKeysTab from '@/views/aiservices/ApiKeysTab.vue'
import BudgetsTab from '@/views/aiservices/BudgetsTab.vue'
import EffectiveParameters from '@/views/aiservices/EffectiveParameters.vue'
import Playground from '@/views/aiservices/Playground.vue'
import RunActionsTab from '@/views/aiservices/RunActionsTab.vue'
import SetupWizard from '@/views/aiservices/SetupWizard.vue'
import { apiCalls, refusal, reset } from '../doubles/api.js'

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

  it('the playground asks which models it may use', async () => {
    // It was the one route screen that fetched nothing: it took a
    // `session` prop CloudStack never passes, so it rendered "Open a
    // session to try this endpoint." for ever and no request for
    // createAiPlaygroundSession was ever made.
    mount(Playground)
    await flushPromises()

    expect(apiCalls().map((c) => c.command)).toContain('listAiModels')
  })

  it('the playground does not mint a session on load', async () => {
    // A session key is a credential. Minting one just because somebody
    // opened the screen issues a key nobody asked for, and the fifteen
    // minute expiry starts running against a tenant who may be reading
    // the page rather than using it.
    mount(Playground)
    await flushPromises()

    expect(apiCalls().map((c) => c.command)).not.toContain('createAiPlaygroundSession')
  })

  it('the playground opens a session for the model it was asked for', async () => {
    reset({
      listAiModels: { listaimodelsresponse: { aimodel: [{ name: 'llama-3.3-70b-instruct' }] } }
    })
    const wrapper = mount(Playground)
    await flushPromises()

    await wrapper.find('[data-test="open-session"]').trigger('click')
    await flushPromises()

    const opened = apiCalls().find((c) => c.command === 'createAiPlaygroundSession')
    expect(opened).toBeTruthy()
    expect(opened.params.model).toBe('llama-3.3-70b-instruct')
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

  it('the Hugging Face browser searches when asked', async () => {
    // The pair of the test above, and the one that was missing: a
    // screen that never searches also passes "does not search until
    // asked", so that assertion on its own documented a browser that
    // could not browse.
    const wrapper = mount(HuggingFaceBrowser)
    await wrapper.find('[data-test="query"]').setValue('llama')
    await wrapper.find('[data-test="search"]').trigger('click')
    await flushPromises()

    const search = apiCalls().find((c) => c.command === 'searchAiHfModels')
    expect(search).toBeTruthy()
    expect(search.params.query).toBe('llama')
  })

  it('effective parameters asks which models it may resolve for', async () => {
    mount(EffectiveParameters)
    await flushPromises()

    expect(apiCalls().map((c) => c.command)).toContain('listAiModels')
  })

  it('effective parameters resolves for the model it was asked about', async () => {
    reset({
      listAiModels: { listaimodelsresponse: { aimodel: [{ name: 'llama-3.3-70b-instruct' }] } }
    })
    const wrapper = mount(EffectiveParameters)
    await flushPromises()

    await wrapper.find('[data-test="resolve"]').trigger('click')
    await flushPromises()

    const asked = apiCalls().find((c) => c.command === 'getAiEffectiveParameters')
    expect(asked).toBeTruthy()
    expect(asked.params.model).toBe('llama-3.3-70b-instruct')
  })

  it('the api keys tab asks for the keys', async () => {
    // A tab is handed `resource`, never a `keys` array, so one that
    // only accepts props shows an empty table whatever the account has.
    mount(ApiKeysTab)
    await flushPromises()

    expect(apiCalls().map((c) => c.command)).toContain('listAiApiKeys')
  })

  it('the budgets tab asks for the budgets', async () => {
    mount(BudgetsTab)
    await flushPromises()

    expect(apiCalls().map((c) => c.command)).toContain('listAiBudgets')
  })

  it('the activity tab asks for the reconciler runs', async () => {
    mount(RunActionsTab)
    await flushPromises()

    expect(apiCalls().map((c) => c.command)).toContain('listAiReconcilerRuns')
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
      // Keyed by command; the value is the envelope CloudStack returns —
      // and the list sits under the name CloudStack serialises the
      // response as, 'aisetupcheck'. This fixture used to say 'checks',
      // which is the key the *Java* reads from the engine, so it agreed
      // with the screen's bug instead of with the server.
      listAiSetupChecks: {
        listaisetupchecksresponse: {
          aisetupcheck: [
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

  it('a screen that could not load says so', async () => {
    // Not the same as having nothing to show. listAiGpuCapacity
    // answers 534 until capacity has been observed, and the screen
    // drew a blank page — an operator could not tell a fresh install
    // from a broken one.
    reset({ listAiGpuCapacity: new Error('capacity has not been observed yet') })
    const wrapper = mount(GpuCapacity)
    await flushPromises()

    expect(wrapper.find('[data-test="load-failed"]').exists()).toBe(true)
  })

  it('a screen that could not load repeats the engine\'s own sentence', async () => {
    // Not "Request failed with status code 534", which tells an
    // operator nothing they can act on. CloudStack wraps a refusal in
    // a key named after the command, so the sentence sits at
    // data.listaigpucapacityresponse.errortext — one level deeper than
    // the screen used to look, which is why it always fell through to
    // axios's own message.
    reset({
      listAiGpuCapacity: refusal(
        'listAiGpuCapacity', 'capacity has not been observed yet', 534)
    })
    const wrapper = mount(GpuCapacity)
    await flushPromises()

    const said = wrapper.find('[data-test="load-failed"]').text()
    expect(said).toContain('capacity has not been observed yet')
    expect(said).not.toContain('status code')
  })

  it('a screen with nothing to show says that instead', async () => {
    reset({ listAiGpuCapacity: { listaigpucapacityresponse: { aigpucapacity: [] } } })
    const wrapper = mount(GpuCapacity)
    await flushPromises()

    expect(wrapper.find('[data-test="load-failed"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="empty"]').exists()).toBe(true)
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
          aievaluation: [
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
          aievaluation: [
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

describe('a screen survives the API failing', () => {
  // Every one of these called the API with no .catch(), so a 4xx or
  // 5xx became an unhandled promise rejection and the screen rendered
  // blank with the reason only in the browser console. listAiGpuCapacity
  // answers 534 until capacity is observed, which made that the normal
  // case on a fresh install rather than an edge one.
  const screens = [
    ['the heatmap', GpuCapacity, 'listAiGpuCapacity'],
    ['fleet health', FleetHealth, 'listAiGpuCapacity'],
    ['the setup wizard', SetupWizard, 'listAiSetupChecks'],
    ['the playground', Playground, 'listAiModels'],
    ['effective parameters', EffectiveParameters, 'listAiModels'],
    ['the api keys tab', ApiKeysTab, 'listAiApiKeys'],
    ['the budgets tab', BudgetsTab, 'listAiBudgets'],
    ['the activity tab', RunActionsTab, 'listAiReconcilerRuns']
  ]

  screens.forEach(([what, component, command]) => {
    it(`${what} handles a refusal`, async () => {
      const rejections = []
      const record = (e) => rejections.push(e)
      process.on('unhandledRejection', record)

      reset({ [command]: new Error('refused') })
      const wrapper = mount(component)
      await flushPromises()
      await new Promise((resolve) => setTimeout(resolve, 0))

      process.off('unhandledRejection', record)
      expect(rejections).toHaveLength(0)
      expect(wrapper.exists()).toBe(true)
    })
  })
})

describe('every screen says why it could not load', () => {
  // A screen that empties its list on a refusal renders exactly like a
  // screen with nothing to show. Only the heatmap told them apart; the
  // other eight set their list to [] in the catch and said nothing, so
  // "could not reach the engine" and "you have no keys yet" were the
  // same blank panel. The reason has to be the engine's own sentence,
  // which is the thing that names a remedy.
  const screens = [
    ['the heatmap', GpuCapacity, 'listAiGpuCapacity'],
    ['fleet health', FleetHealth, 'listAiGpuCapacity'],
    ['the setup wizard', SetupWizard, 'listAiSetupChecks'],
    ['the playground', Playground, 'listAiModels'],
    ['effective parameters', EffectiveParameters, 'listAiModels'],
    ['the api keys tab', ApiKeysTab, 'listAiApiKeys'],
    ['the budgets tab', BudgetsTab, 'listAiBudgets'],
    ['the activity tab', RunActionsTab, 'listAiReconcilerRuns']
  ]

  screens.forEach(([what, component, command]) => {
    it(`${what} repeats the engine's sentence`, async () => {
      reset({ [command]: refusal(command, 'the engine is not configured', 530) })
      const wrapper = mount(component)
      await flushPromises()

      const failed = wrapper.find('[data-test="load-failed"]')
      expect(failed.exists()).toBe(true)
      expect(failed.text()).toContain('the engine is not configured')
      expect(failed.text()).not.toContain('status code')
    })

    it(`${what} does not claim failure when it simply has nothing`, async () => {
      reset({ [command]: {} })
      const wrapper = mount(component)
      await flushPromises()

      expect(wrapper.find('[data-test="load-failed"]').exists()).toBe(false)
    })
  })

  it('the setup wizard does not let you continue past checks it could not read', async () => {
    // canProceed([]) is true, which is right for a zone whose checks
    // all passed and wrong for a wizard that never got any: the button
    // was enabled, so a refused listAiSetupChecks read as a clean
    // preflight. Checks that could not be read are not checks that
    // passed.
    reset({ listAiSetupChecks: refusal('listAiSetupChecks', 'the engine is not configured', 530) })
    const wrapper = mount(SetupWizard)
    await flushPromises()

    expect(wrapper.find('[data-test="proceed"]').attributes('disabled')).toBeDefined()
  })

  it('the hugging face browser repeats the hub refusal', async () => {
    // Its fetch happens on search rather than on mount, so it needs
    // asking before there is anything to fail.
    reset({ searchAiHfModels: refusal('searchAiHfModels', 'the hub could not be reached', 530) })
    const wrapper = mount(HuggingFaceBrowser)
    await wrapper.find('[data-test="query"]').setValue('llama')
    await wrapper.find('[data-test="search"]').trigger('click')
    await flushPromises()

    const failed = wrapper.find('[data-test="load-failed"]')
    expect(failed.exists()).toBe(true)
    expect(failed.text()).toContain('the hub could not be reached')
  })
})
