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
        v-for="budget in budgets"
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
export default {
  name: 'AiBudgetsTab',
  props: { budgets: { type: Array, default: () => [] } }
}
</script>
