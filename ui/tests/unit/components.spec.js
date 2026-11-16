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
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import EffectiveParameters from '@/views/aiservices/EffectiveParameters.vue'
import GpuCapacity from '@/views/aiservices/GpuCapacity.vue'
import Playground from '@/views/aiservices/Playground.vue'
import SetupWizard from '@/views/aiservices/SetupWizard.vue'
import SnippetsTab from '@/views/aiservices/SnippetsTab.vue'

describe('the playground', () => {
  const session = {
    id: 'k-1',
    apikey: 'aisvc-k1-thesecret',
    model: 'llama-3.3-70b-instruct',
    expires: '2026-11-08T21:00:00Z',
    snippets: [
      { name: 'curl', language: 'bash', code: 'curl -H "Authorization: Bearer $AISVC_API_KEY"' }
    ]
  }

  it('never renders the session key', () => {
    // The key is short-lived, but it is still a credential and the
    // screen is shared, screenshotted and demoed.
    const wrapper = mount(Playground, { props: { session } })

    expect(wrapper.text()).not.toContain('thesecret')
  })

  it('says when the session expires', () => {
    // Fifteen minutes in, the console stops working; a tenant with no
    // warning reads that as the product being broken.
    const wrapper = mount(Playground, { props: { session } })

    expect(wrapper.text().toLowerCase()).toContain('expires')
  })

  it('names the model it is talking to', () => {
    const wrapper = mount(Playground, { props: { session } })

    expect(wrapper.text()).toContain('llama-3.3-70b-instruct')
  })

  it('shows nothing until a session is opened', () => {
    const wrapper = mount(Playground, { props: { session: null } })

    expect(wrapper.find('[data-test="transcript"]').exists()).toBe(false)
  })
})

describe('the snippets tab', () => {
  const snippets = [
    { name: 'curl', language: 'bash', code: 'curl $AISVC_API_KEY' },
    { name: 'openai-python', language: 'python', code: 'OpenAI(base_url=...)' }
  ]

  it('shows every snippet the engine returned', () => {
    const wrapper = mount(SnippetsTab, { props: { snippets } })

    expect(wrapper.findAll('[data-test="snippet"]')).toHaveLength(2)
  })

  it('reads the key from the environment rather than embedding one', () => {
    const wrapper = mount(SnippetsTab, { props: { snippets } })

    expect(wrapper.text()).toContain('AISVC_API_KEY')
  })

  it('copes with no snippets', () => {
    const wrapper = mount(SnippetsTab, { props: { snippets: [] } })

    expect(wrapper.findAll('[data-test="snippet"]')).toHaveLength(0)
  })
})

describe('effective parameters', () => {
  const parameters = [
    { name: 'temperature', value: 0.2, source: 'domain', policy: 'locked' },
    { name: 'top_p', value: 0.9, source: 'platform', policy: 'default' }
  ]

  it('shows the source level beside every value', () => {
    // SPEC.md section 6: both the resolved value and its source level.
    const wrapper = mount(EffectiveParameters, { props: { parameters } })

    expect(wrapper.text()).toContain('Domain')
    expect(wrapper.text()).toContain('Platform default')
  })

  it('marks a locked value as locked', () => {
    const wrapper = mount(EffectiveParameters, { props: { parameters } })

    expect(wrapper.find('[data-test="locked"]').exists()).toBe(true)
  })

  it('does not mark an editable value as locked', () => {
    const wrapper = mount(EffectiveParameters, {
      props: { parameters: [parameters[1]] }
    })

    expect(wrapper.find('[data-test="locked"]').exists()).toBe(false)
  })
})

describe('the GPU capacity heatmap', () => {
  const capacity = [
    { zone: 'east', gpuclass: 'h100-8x', total: 16, allocated: 8, free: 8, unhealthy: 0 },
    { zone: 'east', gpuclass: 'l40s-1x', total: 4, allocated: 0, free: 3, unhealthy: 1 }
  ]

  it('renders a cell per class', () => {
    const wrapper = mount(GpuCapacity, { props: { capacity } })

    expect(wrapper.findAll('[data-test="cell"]')).toHaveLength(2)
  })

  it('marks an unhealthy class so it stands out', () => {
    const wrapper = mount(GpuCapacity, { props: { capacity } })

    expect(wrapper.find('.is-unhealthy').exists()).toBe(true)
  })

  it('shows nothing rather than an error when nothing is observed', () => {
    const wrapper = mount(GpuCapacity, { props: { capacity: [] } })

    expect(wrapper.findAll('[data-test="cell"]')).toHaveLength(0)
  })
})

describe('the setup wizard', () => {
  const checks = [
    { name: 'platform spec', status: 'pass', detail: 'ok', remedy: '' },
    { name: 'model registry', status: 'fail', detail: 'no credentials', remedy: 'Check AISVC_REGISTRY_BUCKET.' }
  ]

  it('shows the remedy for a failing check', () => {
    // An operator in a wizard cannot read the source to find the fix.
    const wrapper = mount(SetupWizard, { props: { checks } })

    expect(wrapper.text()).toContain('Check AISVC_REGISTRY_BUCKET.')
  })

  it('blocks while a check is failing', () => {
    const wrapper = mount(SetupWizard, { props: { checks } })

    expect(wrapper.find('[data-test="proceed"]').attributes('disabled')).toBeDefined()
  })

  it('lets the operator proceed once nothing fails', () => {
    const wrapper = mount(SetupWizard, {
      props: { checks: [checks[0], { ...checks[1], status: 'skipped' }] }
    })

    expect(wrapper.find('[data-test="proceed"]').attributes('disabled')).toBeUndefined()
  })
})
