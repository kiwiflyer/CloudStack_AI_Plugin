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
<!-- Scoped API keys for an endpoint (SPEC.md section 7).

     A key's secret is shown once, by the dialog that creates it, and
     never again — so this list carries metadata only. `lastused` is
     here because it is how a tenant decides whether an old key is
     still in use before revoking it. -->
<template>
  <table class="ai-keys">
    <thead>
      <tr>
        <th>Name</th>
        <th>Models</th>
        <th>Last used</th>
        <th>Expires</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="key in rows" :key="key.id" data-test="key">
        <td>{{ key.name }}</td>
        <td>{{ (key.allowedmodels || []).join(', ') || 'any' }}</td>
        <td>{{ key.lastused || 'never' }}</td>
        <td>{{ key.expires || 'never' }}</td>
        <td>{{ key.revoked ? 'revoked' : 'active' }}</td>
      </tr>
    </tbody>
  </table>
</template>

<script>
import { getAPI } from '@/api'

export default {
  name: 'AiApiKeysTab',
  props: {
    // CloudStack hands a tab its parent `resource`, never a named
    // array, so a tab that only accepts props renders an empty table
    // however much the account has. null means "fetch for yourself",
    // the same arrangement the route screens use; the tests and any
    // parent holding the data still pass it in.
    keys: { type: Array, default: null }
  },
  data () {
    return { fetched: [] }
  },
  computed: {
    rows () {
      return this.keys ?? this.fetched
    }
  },
  mounted () {
    if (this.keys === null) this.load()
  },
  methods: {
    load () {
      getAPI('listAiApiKeys', {}).then((json) => {
        this.fetched = json?.listaiapikeysresponse?.aiapikey ?? []
      }).catch(() => {
        // Shown, never thrown. An unhandled rejection leaves a blank
        // screen with the reason only in the browser console, where
        // no operator is looking.
        this.fetched = []
      })
    }
  }
}
</script>
