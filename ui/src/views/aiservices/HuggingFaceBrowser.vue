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
<!-- Searching Hugging Face and checking a model (SPEC.md sections 5, 9).

     The compatibility report is the part that matters: pickle weights
     and `trust_remote_code` are blocked by default, and a model's
     licence is checked at mirror time as well as deploy time. A result
     that cannot be mirrored says why here, before anyone tries. -->
<template>
  <div class="ai-hf">
    <ul class="ai-hf__results">
      <li v-for="model in models" :key="model.repo" data-test="result">
        <strong>{{ model.repo }}</strong>
        <span class="ai-hf__meta">
          {{ model.task }} · {{ model.license }} · {{ model.downloads }} downloads
        </span>
        <ul v-if="model.problems && model.problems.length" class="ai-hf__problems">
          <li v-for="problem in model.problems" :key="problem">{{ problem }}</li>
        </ul>
        <span v-else class="ai-hf__ok">Compatible</span>
      </li>
    </ul>
  </div>
</template>

<script>
export default {
  name: 'AiHuggingFaceBrowser',
  props: { models: { type: Array, default: () => [] } }
}
</script>
