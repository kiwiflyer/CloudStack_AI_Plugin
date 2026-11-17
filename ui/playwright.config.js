// Licensed to the Apache Software Foundation (ASF) under one or more
// contributor license agreements. See the NOTICE file distributed with this
// work for additional information regarding copyright ownership. The ASF
// licenses this file to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance with the
// License. You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
// WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
// License for the specific language governing permissions and limitations
// under the License.
import { defineConfig } from '@playwright/test'

// These drive a real CloudStack UI and sign in as real accounts, so
// they are skipped unless one is pointed at — the same convention the
// Terraform acceptance tests use. `make test-ui` runs the unit tests
// unconditionally and these only when CLOUDSTACK_UI_URL is set.
export default defineConfig({
  testDir: './tests/e2e',
  // Signs in once per role and saves the session, so the specs replay
  // it instead of each one logging in again.
  globalSetup: './tests/e2e/global-setup.js',
  // 60s was not enough for sign-in. These run against a real
  // management server, and behind a webpack dev server the first
  // navigation of a run waits for chunks to compile — one test per
  // run would fail in `beforeEach` with a bare "test timeout". The
  // per-test budget has to exceed the sign-in's own wait, or raising
  // that wait does nothing.
  timeout: 180_000,
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
