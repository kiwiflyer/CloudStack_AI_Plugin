import { defineConfig } from '@playwright/test'

// These drive a real CloudStack UI and sign in as real accounts, so
// they are skipped unless one is pointed at — the same convention the
// Terraform acceptance tests use. `make test-ui` runs the unit tests
// unconditionally and these only when CLOUDSTACK_UI_URL is set.
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  // A failing role test is usually a permissions question, and the
  // trace is what answers it.
  use: {
    baseURL: process.env.CLOUDSTACK_UI_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure'
  },
  reporter: [['list']]
})
