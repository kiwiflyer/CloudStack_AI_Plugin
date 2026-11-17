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
import { expect, test } from '@playwright/test'

import { credentials, signIn } from './sign-in.js'
import { sessionFor } from './sessions.js'

// SPEC.md section 9 and CLAUDE.md: every screen is checked per default
// role, allowed and denied. Role-based visibility is config in
// aiservices.js and unit-tested there; what these add is that
// CloudStack honours it end to end — the section config and the
// plugin's RBAC have to agree, and only a real sign-in proves that.

const ROLES = {
  admin: {
    ...credentials('admin'),
    sees: ['Setup', 'GPU capacity', 'Model catalog', 'Endpoints'],
    hidden: []
  },
  domainadmin: {
    ...credentials('domainadmin'),
    sees: ['Model registry', 'Model catalog', 'Rollouts'],
    hidden: ['Setup', 'GPU capacity', 'Serving profiles']
  },
  user: {
    ...credentials('user'),
    sees: ['Model catalog', 'Endpoints', 'Playground', 'Usage and budgets'],
    hidden: ['Setup', 'GPU capacity', 'Fleet health', 'Model registry']
  }
}

const configured = Boolean(process.env.CLOUDSTACK_UI_URL)

test.describe('the AI Services section, per role', () => {
  test.skip(
    !configured,
    'set CLOUDSTACK_UI_URL (and the per-role credentials) to run these'
  )

  for (const [role, expected] of Object.entries(ROLES)) {
    test.describe(role, () => {
      // The session global-setup.js saved, replayed. Falls back to
      // signing in when there is none, so a role whose setup failed
      // still runs rather than erroring on a missing file.
      test.use({ storageState: sessionFor(role) })

      test.beforeEach(async ({ page }) => {
        test.skip(!expected.user, `set CLOUDSTACK_${role.toUpperCase()}_USER`)
        if (!sessionFor(role)) {
          await signIn(page, expected)
        }
      })

      test('the section is reachable', async ({ page }) => {
        await page.goto('/client/#/aiservices')
        await expect(page.getByText('AI Services')).toBeVisible()
      })

      for (const screen of expected.sees) {
        test(`shows ${screen}`, async ({ page }) => {
          await page.goto('/client/#/aiservices')
          // `.first()` because /aiservices redirects to the section's
          // first child, so whichever screen that is matches twice —
          // once in the side menu and once in the breadcrumb, both
          // links to the same route. Without it the assertion fails
          // on a strict-mode violation for exactly one screen per
          // role, which is a worse way to learn this.
          await expect(page.getByRole('link', { name: screen }).first()).toBeVisible()
        })
      }

      for (const screen of expected.hidden) {
        test(`does not show ${screen}`, async ({ page }) => {
          await page.goto('/client/#/aiservices')
          await expect(page.getByRole('link', { name: screen })).toHaveCount(0)
        })
      }
    })
  }
})

test.describe('a tenant cannot reach an admin screen by its URL', () => {
  test.skip(!configured, 'set CLOUDSTACK_UI_URL to run these')

  test.use({ storageState: sessionFor('user') })

  test('the setup wizard refuses a tenant', async ({ page }) => {
    test.skip(!ROLES.user.user, 'set CLOUDSTACK_USER_USER')
    if (!sessionFor('user')) {
      await signIn(page, ROLES.user)
    }

    // Hiding a link is presentation; this is about what a tenant gets
    // when they type the URL anyway.
    //
    // It is 404, not "not authorised", and that was worth measuring
    // rather than assuming. CloudStack drops a route whose
    // `permission` is missing from the caller's `listApis`, so for a
    // tenant the admin screen does not exist at all — there is no
    // page to refuse them. The stronger half of the guarantee is at
    // the API: `listAiSetupChecks` returns 432 to this account, which
    // the engine's own per-role tests cover.
    await page.goto('/client/#/aisetup')
    await expect(page.getByText('404')).toBeVisible()
    await expect(page.getByRole('link', { name: 'Setup' })).toHaveCount(0)
  })
})
