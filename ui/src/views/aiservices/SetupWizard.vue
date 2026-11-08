<!-- The admin setup wizard (SPEC.md sections 7 and 9).

     Every failing check shows its remedy: an operator working through
     a wizard cannot read the source to find out what went wrong. Only
     a failure blocks — a warning is worth saying, and `skipped` is the
     normal state of a zone nothing has observed yet, which is every
     zone on its first run. -->
<template>
  <div class="ai-setup">
    <section
      v-for="status in order"
      :key="status"
      v-show="grouped[status].length"
      :class="['ai-setup__group', `is-${status}`]"
    >
      <h4 class="ai-setup__status">{{ status }}</h4>
      <ul>
        <li v-for="check in grouped[status]" :key="check.name">
          <strong>{{ check.name }}</strong>
          <span class="ai-setup__detail">{{ check.detail }}</span>
          <p v-if="check.remedy" class="ai-setup__remedy">{{ check.remedy }}</p>
        </li>
      </ul>
    </section>

    <button
      data-test="proceed"
      class="ai-setup__proceed"
      :disabled="!proceedable || null"
    >
      Continue
    </button>
  </div>
</template>

<script>
import { getAPI } from '@/api'

import { canProceed, groupChecks } from '@/utils/setup'

export default {
  name: 'AiSetupWizard',
  props: {
    checks: { type: Array, default: null }
  },
  data () {
    return { fetched: [], order: ['fail', 'warn', 'skipped', 'pass'] }
  },
  computed: {
    rows () {
      return this.checks ?? this.fetched
    },
    grouped () {
      return groupChecks(this.rows)
    },
    proceedable () {
      return canProceed(this.rows)
    }
  },
  mounted () {
    if (this.checks === null) this.load()
  },
  methods: {
    load () {
      getAPI('listAiSetupChecks', {}).then((json) => {
        this.fetched = json?.listaisetupchecksresponse?.checks ?? []
      })
    }
  }
}
</script>
