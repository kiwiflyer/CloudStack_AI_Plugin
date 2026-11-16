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
<!-- One reconcile run's actions (SPEC.md section 4).

     A failed action stops only its dependants, so a run can be part
     applied; the outcome per action is what tells an operator how far
     it got. -->
<template>
  <table class="ai-run-actions">
    <thead>
      <tr>
        <th>Action</th>
        <th>Resource</th>
        <th>Outcome</th>
        <th>Detail</th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="action in actions"
        :key="action.id"
        data-test="action"
        :class="`is-${action.outcome}`"
      >
        <td>{{ action.kind }}</td>
        <td>{{ action.resourcetype }} / {{ action.name }}</td>
        <td>{{ action.outcome }}</td>
        <td>{{ action.error || action.detail }}</td>
      </tr>
    </tbody>
  </table>
</template>

<script>
export default {
  name: 'AiRunActionsTab',
  props: { actions: { type: Array, default: () => [] } }
}
</script>
