// The setup wizard's checks (SPEC.md sections 7 and 14).
//
// Section 14's first criterion is that a new zone reaches a passing
// self-test using only the wizard, so the screen has to be usable on a
// zone where most things are not configured yet.

const STATUSES = ['pass', 'warn', 'fail', 'skipped']

export function groupChecks (checks) {
  // Every group exists even when empty, so the template can render them
  // unconditionally instead of guarding each one.
  const grouped = Object.fromEntries(STATUSES.map((status) => [status, []]))
  for (const check of checks) {
    if (grouped[check.status]) grouped[check.status].push(check)
  }
  return grouped
}

/**
 * Whether the operator may move on.
 *
 * Only a failure blocks. A warning is worth saying and not worth
 * stopping for, and `skipped` is the normal state of a zone that has
 * not been observed yet — which is every zone on its first run.
 */
export function canProceed (checks) {
  return !checks.some((check) => check.status === 'fail')
}
