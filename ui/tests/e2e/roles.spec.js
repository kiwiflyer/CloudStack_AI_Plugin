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

// SPEC.md section 9 and CLAUDE.md: every screen is checked per default
// role, allowed and denied. Role-based visibility is config in
// aiservices.js and unit-tested there; what these add is that
// CloudStack honours it end to end — the section config and the
// plugin's RBAC have to agree, and only a real sign-in proves that.

const ROLES = {
  admin: {
    user: process.env.CLOUDSTACK_ADMIN_USER,
    password: process.env.CLOUDSTACK_ADMIN_PASSWORD,
    sees: ['Setup', 'GPU capacity', 'Model catalog', 'Endpoints'],
    hidden: []
  },
  domainadmin: {
    user: process.env.CLOUDSTACK_DOMAINADMIN_USER,
    password: process.env.CLOUDSTACK_DOMAINADMIN_PASSWORD,
    sees: ['Model registry', 'Model catalog', 'Rollouts'],
    hidden: ['Setup', 'GPU capacity', 'Serving profiles']
  },
  user: {
    user: process.env.CLOUDSTACK_USER_USER,
    password: process.env.CLOUDSTACK_USER_PASSWORD,
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
      test.beforeEach(async ({ page }) => {
        test.skip(!expected.user, `set CLOUDSTACK_${role.toUpperCase()}_USER`)
        await page.goto('/client/#/user/login')
        await page.getByLabel('Username').fill(expected.user)
        await page.getByLabel('Password').fill(expected.password)
        await page.getByRole('button', { name: /sign in/i }).click()
        await page.waitForURL(/dashboard/)
      })

      test('the section is reachable', async ({ page }) => {
        await page.goto('/client/#/aiservices')
        await expect(page.getByText('AI Services')).toBeVisible()
      })

      for (const screen of expected.sees) {
        test(`shows ${screen}`, async ({ page }) => {
          await page.goto('/client/#/aiservices')
          await expect(page.getByRole('link', { name: screen })).toBeVisible()
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

  test('the setup wizard refuses a tenant', async ({ page }) => {
    test.skip(!ROLES.user.user, 'set CLOUDSTACK_USER_USER')
    await page.goto('/client/#/user/login')
    await page.getByLabel('Username').fill(ROLES.user.user)
    await page.getByLabel('Password').fill(ROLES.user.password)
    await page.getByRole('button', { name: /sign in/i }).click()
    await page.waitForURL(/dashboard/)

    // Hiding a link is presentation; the plugin has to refuse the call
    // as well, and this is what proves it does.
    await page.goto('/client/#/aisetup')
    await expect(page.getByText(/not (authori[sz]ed|permitted)/i)).toBeVisible()
  })
})
