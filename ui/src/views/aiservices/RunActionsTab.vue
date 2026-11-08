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
