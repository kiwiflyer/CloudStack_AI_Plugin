<!--
Licensed to the Apache Software Foundation (ASF) under one or more
contributor license agreements. See the NOTICE file distributed with this
work for additional information regarding copyright ownership. The ASF
licenses this file to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
License for the specific language governing permissions and limitations
under the License.
-->
<!-- The tenant playground (SPEC.md sections 7 and 9).

     "Streaming chat with effective profiles, token counts, latency",
     over a 15-minute model-scoped session key so real keys never reach
     the browser.

     The session key is deliberately never rendered. It is short-lived,
     but this screen gets demoed and screenshotted, and a credential on
     screen is a credential in someone's slide deck. -->
<template>
  <div class="ai-playground">
    <header v-if="session" class="ai-playground__header">
      <span class="ai-playground__model">{{ session.model }}</span>
      <span class="ai-playground__expiry">
        Session expires {{ expiresAt }}
      </span>
    </header>

    <p v-else class="ai-playground__empty">
      Open a session to try this endpoint.
    </p>

    <div v-if="session" data-test="transcript" class="ai-playground__transcript">
      <div
        v-for="(turn, index) in transcript"
        :key="index"
        :class="['ai-playground__turn', `is-${turn.role}`]"
      >
        <p class="ai-playground__content">{{ turn.content }}</p>
        <p v-if="turn.usage" class="ai-playground__usage">
          {{ turn.usage.input }} in / {{ turn.usage.output }} out
          · {{ turn.usage.latencyMs }} ms to first token
        </p>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  name: 'AiPlayground',
  props: {
    // What createAiPlaygroundSession returned.
    session: { type: Object, default: null },
    transcript: { type: Array, default: () => [] }
  },
  computed: {
    expiresAt () {
      if (!this.session?.expires) return 'shortly'
      return new Date(this.session.expires).toLocaleTimeString()
    }
  }
}
</script>
