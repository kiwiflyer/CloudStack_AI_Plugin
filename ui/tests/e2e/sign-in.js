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

/**
 * Sign in to CloudStack's own login page.
 *
 * Every e2e spec had its own copy of this, and all three copies were
 * wrong in the same three ways, because none of them had ever run
 * against a management server. Measured against 4.23.0.0:
 *
 *   - the fields carry `placeholder`, not a label, so `getByLabel`
 *     never matches and the test times out on `fill`
 *   - the button says "Login", not "Sign in"
 *   - there is a third field, Domain, and an account outside ROOT
 *     cannot sign in without it
 *
 * One copy, so the next surprise is fixed once.
 */
export async function signIn (page, { user, password, domain }) {
  await page.goto('/client/#/user/login')

  await page.getByPlaceholder('Username').fill(user)
  await page.getByPlaceholder('Password').fill(password)
  if (domain) {
    await page.getByPlaceholder('Domain').fill(domain)
  }

  await page.getByRole('button', { name: /^(log ?in|sign ?in)$/i }).click()
  // Generous: against a webpack dev server the first sign-in of a
  // run waits for chunks to compile, and 60s is not always enough.
  await page.waitForURL(/dashboard/, { timeout: 120_000 })
}

/** The credentials for one role, from the environment. */
export function credentials (role) {
  const key = role.toUpperCase()
  return {
    user: process.env[`CLOUDSTACK_${key}_USER`],
    password: process.env[`CLOUDSTACK_${key}_PASSWORD`],
    domain: process.env[`CLOUDSTACK_${key}_DOMAIN`]
  }
}
