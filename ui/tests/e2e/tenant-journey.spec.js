import { expect, test } from '@playwright/test'

// Milestone 6 is "tenant first", so the journey that has to work is a
// tenant's: find a model, deploy it, get a key, try it.

const configured = Boolean(
  process.env.CLOUDSTACK_UI_URL && process.env.CLOUDSTACK_USER_USER
)

test.describe('a tenant going from catalog to a working endpoint', () => {
  test.skip(!configured, 'set CLOUDSTACK_UI_URL and CLOUDSTACK_USER_USER')

  test.beforeEach(async ({ page }) => {
    await page.goto('/client/#/user/login')
    await page.getByLabel('Username').fill(process.env.CLOUDSTACK_USER_USER)
    await page.getByLabel('Password').fill(process.env.CLOUDSTACK_USER_PASSWORD)
    await page.getByRole('button', { name: /sign in/i }).click()
    await page.waitForURL(/dashboard/)
  })

  test('the catalog lists approved models with their price tier', async ({ page }) => {
    await page.goto('/client/#/aimodelcatalog')

    await expect(page.getByRole('columnheader', { name: /price/i })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: /license/i })).toBeVisible()
  })

  test('an endpoint shows its base URL', async ({ page }) => {
    await page.goto('/client/#/aiendpoint')

    await expect(page.getByRole('columnheader', { name: /base.*url/i })).toBeVisible()
  })

  test('creating a key shows the secret once, with a warning', async ({ page }) => {
    await page.goto('/client/#/aiendpoint')
    await page.getByRole('row').nth(1).click()
    await page.getByRole('button', { name: /create api key/i }).click()
    await page.getByLabel('Name').fill('playwright')
    await page.getByRole('button', { name: /^ok$/i }).click()

    // Shown once is the whole contract; a tenant who misses it has to
    // make another key.
    await expect(page.getByText(/shown once/i)).toBeVisible()
  })

  test('the key list never shows a secret', async ({ page }) => {
    await page.goto('/client/#/aiendpoint')
    await page.getByRole('row').nth(1).click()
    await page.getByRole('tab', { name: /api keys/i }).click()

    await expect(page.getByText(/aisvc-[a-f0-9]{16}-/)).toHaveCount(0)
  })

  test('effective parameters name the level each value came from', async ({ page }) => {
    await page.goto('/client/#/aieffectiveparameters')

    await expect(page.getByRole('columnheader', { name: /set at/i })).toBeVisible()
  })
})
