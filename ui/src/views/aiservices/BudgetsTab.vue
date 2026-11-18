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
<!-- Budgets and where a tenant stands (SPEC.md section 7).

     `source` says whether a budget came from platform.yaml or was set
     through the API, because the two disagree often enough that an
     operator needs to know which one is in force. -->
<template>
  <table class="ai-budgets">
    <thead>
      <tr>
        <th>Account</th>
        <th>Monthly</th>
        <th>Spent</th>
        <th>Used</th>
        <th>At the limit</th>
        <th>Set in</th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="budget in rows"
        :key="budget.account"
        data-test="budget"
        :class="{ 'is-blocked': budget.blocked }"
      >
        <td>{{ budget.account }}</td>
        <td>{{ budget.monthly }} {{ budget.currency }}</td>
        <td>{{ budget.spent }} {{ budget.currency }}</td>
        <td>{{ budget.percent_used }}%</td>
        <td>{{ budget.on_exceed === 'block' ? 'stop serving' : 'alert only' }}</td>
        <td>{{ budget.source === 'api' ? 'CloudStack' : 'platform.yaml' }}</td>
      </tr>
    </tbody>
  </table>
</template>

<script>
import { getAPI } from '@/api'

export default {
  name: 'AiBudgetsTab',
  props: {
    // CloudStack hands a tab its parent `resource`, never a named
    // array, so a tab that only accepts props renders an empty table
    // however much the account has. null means "fetch for yourself",
    // the same arrangement the route screens use; the tests and any
    // parent holding the data still pass it in.
    budgets: { type: Array, default: null }
  },
  data () {
    return { fetched: [] }
  },
  computed: {
    rows () {
      return this.budgets ?? this.fetched
    }
  },
  mounted () {
    if (this.budgets === null) this.load()
  },
  methods: {
    load () {
      getAPI('listAiBudgets', {}).then((json) => {
        this.fetched = json?.listaibudgetsresponse?.aibudget ?? []
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
