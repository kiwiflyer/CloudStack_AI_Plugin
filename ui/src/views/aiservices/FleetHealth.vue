<!-- Fleet health (SPEC.md section 9).

     Template and driver versions, and drift between them. Drift
     matters because hosts on different drivers serve the same model
     inconsistently and fail in ways that look like model bugs. The fix
     is drain and replace — passthrough GPUs cannot migrate
     (ADR-0003) — which is the action this screen carries. -->
<template>
  <table class="ai-fleet">
    <thead>
      <tr>
        <th>Host</th>
        <th>Template</th>
        <th>Driver</th>
        <th>State</th>
        <th>Nodes</th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="host in rows"
        :key="host.host"
        data-test="host"
        :class="{ 'is-drifted': host.drift }"
      >
        <td>{{ host.host }}</td>
        <td>{{ host.template }}</td>
        <td>{{ host.driver }}</td>
        <td>{{ host.state }}</td>
        <td>{{ host.nodes }}</td>
      </tr>
    </tbody>
  </table>
</template>

<script>
import { getAPI } from '@/api'

export default {
  name: 'AiFleetHealth',
  props: {
    hosts: { type: Array, default: null }
  },
  data () {
    return { fetched: [] }
  },
  computed: {
    rows () {
      return this.hosts ?? this.fetched
    }
  },
  mounted () {
    if (this.hosts === null) this.load()
  },
  methods: {
    load () {
      getAPI('listAiGpuCapacity', {}).then((json) => {
        this.fetched = json?.listaigpucapacityresponse?.aigpucapacity ?? []
      })
    }
  }
}
</script>
