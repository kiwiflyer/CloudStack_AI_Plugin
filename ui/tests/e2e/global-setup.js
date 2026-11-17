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
import { mkdirSync } from 'node:fs'
import { dirname } from 'node:path'

import { chromium } from '@playwright/test'

import { credentials, signIn } from './sign-in.js'
import { sessionFile } from './sessions.js'

const ROLES = ['admin', 'domainadmin', 'user']

/**
 * Sign in once per role and save the session for the specs to replay.
 *
 * A failure here is deliberately not fatal. A role with no
 * credentials, or one whose sign-in does not complete, simply leaves
 * no session file; `sessionFor` then returns undefined and that
 * role's tests sign in for themselves or skip, exactly as before.
 * Refusing to start the suite because one of three roles is missing
 * would be worse than running the two that are there.
 */
export default async function globalSetup () {
  const baseURL = process.env.CLOUDSTACK_UI_URL
  if (!baseURL) {
    return
  }

  const browser = await chromium.launch()
  try {
    for (const role of ROLES) {
      const who = credentials(role)
      if (!who.user) {
        continue
      }
      // Twice, because the first sign-in after a management server
      // restart waits on a cold JVM and has been seen to exceed two
      // minutes — watched happening to `admin` while the other two
      // roles, signing in seconds later, were fine.
      for (let attempt = 1; attempt <= 2; attempt++) {
        const context = await browser.newContext({ baseURL })
        const page = await context.newPage()
        try {
          await signIn(page, who)
          const file = sessionFile(role)
          mkdirSync(dirname(file), { recursive: true })
          await context.storageState({ path: file })
          console.log(`signed in once as ${role}`)
          await context.close()
          break
        } catch (couldNotSignIn) {
          await context.close()
          if (attempt === 2) {
            console.log(`could not sign in as ${role}: ${couldNotSignIn.message}`)
          }
        }
      }
    }
  } finally {
    await browser.close()
  }
}
