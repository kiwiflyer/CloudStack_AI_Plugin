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
