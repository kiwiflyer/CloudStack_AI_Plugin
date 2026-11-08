<!-- Copy-ready snippets for an endpoint (SPEC.md section 7).

     The engine renders these; nothing is built here. They read the key
     from the environment rather than carrying one, because a snippet
     is copied into tickets and chats where a live key would outlive
     any session. -->
<template>
  <div class="ai-snippets">
    <section
      v-for="snippet in snippets"
      :key="snippet.name"
      data-test="snippet"
      class="ai-snippets__item"
    >
      <h4 class="ai-snippets__name">{{ snippet.name }}</h4>
      <pre class="ai-snippets__code"><code>{{ snippet.code }}</code></pre>
    </section>
  </div>
</template>

<script>
export default {
  name: 'AiSnippetsTab',
  props: {
    snippets: { type: Array, default: () => [] }
  }
}
</script>
