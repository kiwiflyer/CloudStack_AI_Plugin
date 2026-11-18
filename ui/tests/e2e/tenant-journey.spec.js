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
import { action, header, rowLink } from './table.js'

// Milestone 6 is "tenant first", so the journey that has to work is a
// tenant's: find a model, deploy it, get a key, try it.

const configured = Boolean(
  process.env.CLOUDSTACK_UI_URL && process.env.CLOUDSTACK_USER_USER
)

test.describe('a tenant going from catalog to a working endpoint', () => {
  test.use({ storageState: sessionFor('user') })

  test.skip(!configured, 'set CLOUDSTACK_UI_URL and CLOUDSTACK_USER_USER')

  test.beforeEach(async ({ page }) => {
    if (!sessionFor('user')) {
      await signIn(page, credentials('user'))
    }
  })

  test('the catalog lists approved models with their price tier', async ({ page }) => {
    await page.goto('/client/#/aimodelcatalog')

    await expect(header(page, /price/i)).toBeVisible()
    await expect(header(page, /license/i)).toBeVisible()
  })

  test('an endpoint shows its base URL', async ({ page }) => {
    await page.goto('/client/#/aiendpoint')

    await expect(header(page, /base.*url/i)).toBeVisible()
  })

  // Two CloudStack facts this used to get wrong, both already written
  // down and both silent: clicking a row does nothing, because the link
  // is on the first column; and an action button is icon-only, with its
  // words in a hover tooltip, so its accessible name is the icon.
  // Not built, and the test is right to ask for it. `createAiApiKey`
  // returns the secret — measured live, `apikey` is a 66-character
  // `aisvc-…` string — and CloudStack's dialog closes without showing
  // it. The hooks that would (`action.response`, `successMethod`) are
  // both inside $pollJob, so they fire for an async command only, and
  // this one is sync. Keys are stored as salted hashes, so a secret
  // nobody saw is gone: the UI cannot currently produce a usable key.
  //
  // The fix is to mint it from the API keys tab the way the playground
  // mints a session, rather than through an autogen action.
  test.fixme('creating a key shows the secret once, with a warning', async ({ page }) => {
    await page.goto('/client/#/aiendpoint')
    await rowLink(page, /./).first().click()
    await action(page, 'key').first().click()
    await page.getByLabel('Name').fill('playwright')
    await page.getByRole('button', { name: /^ok$/i }).click()

    // Shown once is the whole contract; a tenant who misses it has to
    // make another key.
    await expect(page.getByText(/shown once/i)).toBeVisible()
  })

  test('the key list never shows a secret', async ({ page }) => {
    await page.goto('/client/#/aiendpoint')
    await rowLink(page, /./).first().click()
    await page.getByRole('tab', { name: /api keys/i }).click()

    await expect(page.getByText(/aisvc-[a-f0-9]{16}-/)).toHaveCount(0)
  })

  test('effective parameters name the level each value came from', async ({ page }) => {
    await page.goto('/client/#/aieffectiveparameters')

    await expect(header(page, /set at/i)).toBeVisible()
  })
})
