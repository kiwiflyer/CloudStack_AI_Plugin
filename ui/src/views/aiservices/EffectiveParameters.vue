<!-- Resolved generation parameters (SPEC.md sections 6 and 9).

     "Resolved values per endpoint and key, with source level." The
     source is the whole point: a bare number tells a tenant nothing
     about why it is that or whether they may change it. -->
<template>
  <table class="ai-effective">
    <thead>
      <tr>
        <th>Parameter</th>
        <th>Value</th>
        <th>Set at</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="parameter in described" :key="parameter.name">
        <td>{{ parameter.name }}</td>
        <td>{{ parameter.value }}</td>
        <td>
          {{ parameter.source }}
          <span
            v-if="parameter.locked"
            data-test="locked"
            class="ai-effective__locked"
            title="Locked at this level; it cannot be overridden below."
          >locked</span>
        </td>
      </tr>
    </tbody>
  </table>
</template>

<script>
import { describeParameter } from '@/utils/profiles'

export default {
  name: 'AiEffectiveParameters',
  props: {
    parameters: { type: Array, default: () => [] }
  },
  computed: {
    described () {
      return this.parameters.map(describeParameter)
    }
  }
}
</script>
