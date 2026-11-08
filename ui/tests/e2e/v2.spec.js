import { expect, test } from '@playwright/test'

// The v2 screens (SPEC.md section 8).
//
// These only exist when the plugin registers the v2 commands, which it
// does when ai.services.v2.enabled is on. That is not a UI flag, so
// these specs are the only place the two halves meet: the setting is
// set in CloudStack, and what it does is decide whether these screens
// are in the menu at all.
//
// Set CLOUDSTACK_AI_V2=1 once the setting is on in the zone under
// test. Without it the "absent" group runs instead, which is the
// GA-install expectation and the more important of the two.

const configured = Boolean(
  process.env.CLOUDSTACK_UI_URL && process.env.CLOUDSTACK_USER_USER
)
const v2Enabled = process.env.CLOUDSTACK_AI_V2 === '1'

const TENANT_SCREENS = [
  'Adapters', 'Batch jobs', 'Certificates', 'Private endpoints', 'Guardrail profiles'
]

async function signIn (page, user, password) {
  await page.goto('/client/#/user/login')
  await page.getByLabel('Username').fill(user)
  await page.getByLabel('Password').fill(password)
  await page.getByRole('button', { name: /sign in/i }).click()
  await page.waitForURL(/dashboard/)
}

test.describe('with ai.services.v2.enabled off', () => {
  test.skip(!configured, 'set CLOUDSTACK_UI_URL and CLOUDSTACK_USER_USER')
  test.skip(v2Enabled, 'v2 is on in this zone; the absent-screen group does not apply')

  test.beforeEach(async ({ page }) => {
    await signIn(page, process.env.CLOUDSTACK_USER_USER, process.env.CLOUDSTACK_USER_PASSWORD)
  })

  for (const screen of TENANT_SCREENS) {
    test(`${screen} is not in the menu`, async ({ page }) => {
      await page.goto('/client/#/aiservices')
      await expect(page.getByRole('link', { name: screen })).toHaveCount(0)
    })
  }

  test('the v1 screens are still there', async ({ page }) => {
    // The gate must not take the GA section down with it.
    await page.goto('/client/#/aiservices')
    await expect(page.getByRole('link', { name: 'Model catalog' })).toBeVisible()
  })

  test('a v2 route reached by URL does not render a broken screen', async ({ page }) => {
    await page.goto('/client/#/aiadapter')
    await expect(page.getByRole('columnheader', { name: /base model/i })).toHaveCount(0)
  })
})

test.describe('with ai.services.v2.enabled on', () => {
  test.skip(!configured, 'set CLOUDSTACK_UI_URL and CLOUDSTACK_USER_USER')
  test.skip(!v2Enabled, 'set CLOUDSTACK_AI_V2=1 once the setting is on')

  test.beforeEach(async ({ page }) => {
    await signIn(page, process.env.CLOUDSTACK_USER_USER, process.env.CLOUDSTACK_USER_PASSWORD)
  })

  for (const screen of TENANT_SCREENS) {
    test(`${screen} is in the menu`, async ({ page }) => {
      await page.goto('/client/#/aiservices')
      await expect(page.getByRole('link', { name: screen })).toBeVisible()
    })
  }

  test('adapters show the name a request actually addresses', async ({ page }) => {
    // `<base>:<adapter>`. The adapter's own name addresses nothing, so
    // a tenant reading only that column cannot call it.
    await page.goto('/client/#/aiadapter')

    await expect(page.getByRole('columnheader', { name: /served model/i })).toBeVisible()
  })

  test('a private endpoint says in words how it is reachable', async ({ page }) => {
    await page.goto('/client/#/aiprivateendpoint')

    await expect(page.getByRole('columnheader', { name: /exposure/i })).toBeVisible()
  })

  test('certificates never show key material', async ({ page }) => {
    // The engine returns metadata only; this is the screen-side check
    // that nothing put a body on the page.
    await page.goto('/client/#/aicertificate')

    await expect(page.getByText(/BEGIN (RSA |EC )?PRIVATE KEY/)).toHaveCount(0)
    await expect(page.getByText(/BEGIN CERTIFICATE/)).toHaveCount(0)
  })

  test('replacing a certificate warns about the interruption', async ({ page }) => {
    await page.goto('/client/#/aicertificate')
    await page.getByRole('row').nth(1).click()
    await page.getByRole('button', { name: /replace certificate/i }).click()

    await expect(page.getByText(/interrupts traffic/i)).toBeVisible()
  })

  test('a tenant sees the platform guardrail but cannot delete it', async ({ page }) => {
    // Scoped, not hidden: they can read what screens their traffic,
    // and the refusal on the one they do not own comes from the
    // engine with a message saying what they may do instead.
    await page.goto('/client/#/aiguardrail')

    await expect(page.getByRole('columnheader', { name: /scope/i })).toBeVisible()
  })

  test('tightening their own account is offered', async ({ page }) => {
    await page.goto('/client/#/aiguardrail')

    await expect(page.getByRole('button', { name: /create guardrail profile/i })).toBeVisible()
  })
})

test.describe('evaluations, for a root admin', () => {
  test.skip(!configured, 'set CLOUDSTACK_UI_URL')
  test.skip(!v2Enabled, 'set CLOUDSTACK_AI_V2=1 once the setting is on')

  test.beforeEach(async ({ page }) => {
    test.skip(!process.env.CLOUDSTACK_ADMIN_USER, 'set CLOUDSTACK_ADMIN_USER')
    await signIn(page, process.env.CLOUDSTACK_ADMIN_USER, process.env.CLOUDSTACK_ADMIN_PASSWORD)
  })

  test('a model in the registry has an evaluations tab', async ({ page }) => {
    await page.goto('/client/#/airegistry')
    await page.getByRole('row').nth(1).click()

    await expect(page.getByRole('tab', { name: /evaluations/i })).toBeVisible()
  })

  test('the tab says what the gate decided, not just a score', async ({ page }) => {
    await page.goto('/client/#/airegistry')
    await page.getByRole('row').nth(1).click()
    await page.getByRole('tab', { name: /evaluations/i }).click()

    await expect(page.getByRole('columnheader', { name: /gate/i })).toBeVisible()
  })
})
