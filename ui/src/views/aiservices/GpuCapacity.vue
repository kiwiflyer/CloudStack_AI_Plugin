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
<!-- GPU capacity heatmap (SPEC.md section 9).

     One row per zone, one cell per GPU class. The status class is what
     carries the colour; unhealthy outranks busy so failing hardware
     cannot hide behind a quiet class. -->
<template>
  <div class="ai-capacity">
    <div v-for="row in rows" :key="row.zone" class="ai-capacity__row">
      <h4 class="ai-capacity__zone">{{ row.zone }}</h4>
      <div class="ai-capacity__cells">
        <div
          v-for="cell in row.cells"
          :key="cell.gpuclass"
          data-test="cell"
          :class="['ai-capacity__cell', `is-${cell.status}`]"
        >
          <span class="ai-capacity__class">{{ cell.gpuclass }}</span>
          <span class="ai-capacity__numbers">
            {{ cell.allocated }} / {{ cell.total }} allocated
          </span>
          <span v-if="cell.unhealthy" class="ai-capacity__unhealthy">
            {{ cell.unhealthy }} unhealthy
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { getAPI } from '@/api'

import { heatmapRows } from '@/utils/capacity'

export default {
  name: 'AiGpuCapacity',
  props: {
    // CloudStack renders this as a route view and passes it nothing,
    // so null means "fetch for yourself". A parent that already holds
    // the data, and the tests, pass it in.
    capacity: { type: Array, default: null }
  },
  data () {
    return { fetched: [] }
  },
  computed: {
    rows () {
      return heatmapRows(this.capacity ?? this.fetched)
    }
  },
  mounted () {
    if (this.capacity === null) this.load()
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
