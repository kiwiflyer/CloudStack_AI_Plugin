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
