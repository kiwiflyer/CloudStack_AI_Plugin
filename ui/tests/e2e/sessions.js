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
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'

import { credentials } from './sign-in.js'

/** Where one role's saved session lives. */
export function sessionFile (role) {
  return resolve(process.cwd(), 'tests/e2e/.sessions', `${role}.json`)
}

/**
 * The saved session for a role, or undefined to sign in normally.
 *
 * Every test used to sign in for itself: 22 tests, 22 round trips
 * through a management server that, in the compose harness, runs
 * emulated. Four of them timed out waiting 120 seconds for the
 * dashboard, and the failures moved around between runs. Signing in
 * once per role in `global-setup.js` and replaying the session costs
 * three sign-ins instead of twenty-two.
 *
 * Undefined when the role has no credentials, so the specs' own
 * `test.skip` still decides what runs rather than a missing file
 * failing the suite.
 */
export function sessionFor (role) {
  if (!credentials(role).user) {
    return undefined
  }
  const file = sessionFile(role)
  return existsSync(file) ? file : undefined
}
