// Stands in for CloudStack's '@/api' helper, which only exists inside
// their build. Records calls so tests can assert a screen fetches
// what it needs, and answers with whatever was queued.
const calls = []
let answers = {}

export function reset (responses = {}) {
  calls.length = 0
  answers = responses
}

export function apiCalls () {
  return calls
}

export function getAPI (command, params) {
  calls.push({ command, params })
  return Promise.resolve(answers[command] ?? {})
}

export function postAPI (command, params) {
  calls.push({ command, params })
  return Promise.resolve(answers[command] ?? {})
}
