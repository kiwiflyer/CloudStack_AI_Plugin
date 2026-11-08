package provider

import (
	"fmt"
	"os"
	"regexp"
	"testing"

	"github.com/hashicorp/terraform-plugin-sdk/v2/helper/resource"
	"github.com/hashicorp/terraform-plugin-sdk/v2/helper/schema"
)

// Acceptance tests run against a live CloudStack — the simulator under
// `make test-int` — and are skipped otherwise, as Terraform's own
// convention has it. They create and destroy real objects.
//
// `make test-tf` runs the unit tests above unconditionally and these
// only when TF_ACC and the CloudStack credentials are set.

func testAccProviders() map[string]func() (*schema.Provider, error) {
	return map[string]func() (*schema.Provider, error){
		"cloudstack": func() (*schema.Provider, error) { return New(), nil },
	}
}

func testAccPreCheck(t *testing.T) {
	t.Helper()
	for _, name := range []string{"CLOUDSTACK_API_URL", "CLOUDSTACK_API_KEY", "CLOUDSTACK_SECRET_KEY"} {
		if os.Getenv(name) == "" {
			t.Fatalf("%s must be set for acceptance tests", name)
		}
	}
}

func TestAccAiApiKey(t *testing.T) {
	if os.Getenv("TF_ACC") == "" {
		t.Skip("set TF_ACC=1 and point CLOUDSTACK_API_URL at the simulator")
	}

	resource.Test(t, resource.TestCase{
		PreCheck:          func() { testAccPreCheck(t) },
		ProviderFactories: testAccProviders(),
		Steps: []resource.TestStep{
			{
				Config: `
resource "cloudstack_ai_api_key" "test" {
  name           = "terraform-acceptance"
  allowed_models = ["llama-3.3-70b-instruct"]
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudstack_ai_api_key.test", "id"),
					resource.TestCheckResourceAttrSet("cloudstack_ai_api_key.test", "api_key"),
				),
			},
			{
				ResourceName:      "cloudstack_ai_api_key.test",
				ImportState:       true,
				ImportStateVerify: true,
				// The secret is shown once and cannot be read back, so
				// an imported key has none to compare.
				ImportStateVerifyIgnore: []string{"api_key"},
			},
		},
	})
}

func TestAccAiBudget(t *testing.T) {
	if os.Getenv("TF_ACC") == "" {
		t.Skip("set TF_ACC=1 and point CLOUDSTACK_API_URL at the simulator")
	}

	account := os.Getenv("CLOUDSTACK_ACCOUNT")
	if account == "" {
		account = "admin"
	}

	resource.Test(t, resource.TestCase{
		PreCheck:          func() { testAccPreCheck(t) },
		ProviderFactories: testAccProviders(),
		Steps: []resource.TestStep{
			{
				Config: fmt.Sprintf(`
resource "cloudstack_ai_budget" "test" {
  account   = %q
  monthly   = 100
  currency  = "USD"
  on_exceed = "alert"
}`, account),
				Check: resource.TestCheckResourceAttr(
					"cloudstack_ai_budget.test", "monthly", "100"),
			},
			{
				Config: fmt.Sprintf(`
resource "cloudstack_ai_budget" "test" {
  account   = %q
  monthly   = 250
  currency  = "USD"
  on_exceed = "block"
}`, account),
				Check: resource.TestCheckResourceAttr(
					"cloudstack_ai_budget.test", "monthly", "250"),
			},
			{
				ResourceName:      "cloudstack_ai_budget.test",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccAiModelsDataSource(t *testing.T) {
	if os.Getenv("TF_ACC") == "" {
		t.Skip("set TF_ACC=1 and point CLOUDSTACK_API_URL at the simulator")
	}

	resource.Test(t, resource.TestCase{
		PreCheck:          func() { testAccPreCheck(t) },
		ProviderFactories: testAccProviders(),
		Steps: []resource.TestStep{
			{
				Config: `data "cloudstack_ai_models" "all" {}`,
				Check: resource.TestCheckResourceAttrSet(
					"data.cloudstack_ai_models.all", "models.#"),
			},
		},
	})
}

// -- Tier 2 ------------------------------------------------------------
//
// These need ai.services.v2.enabled on in the zone under test as well
// as TF_ACC, because without it CloudStack does not register the
// commands and every step fails with an unknown command. Gated on
// CLOUDSTACK_AI_V2 so a v1 simulator run skips them rather than failing
// for a reason that is not a bug.

func skipUnlessV2(t *testing.T) {
	t.Helper()
	if os.Getenv("TF_ACC") == "" {
		t.Skip("set TF_ACC=1 and point CLOUDSTACK_API_URL at the simulator")
	}
	if os.Getenv("CLOUDSTACK_AI_V2") != "1" {
		t.Skip("set CLOUDSTACK_AI_V2=1 once ai.services.v2.enabled is on in the zone")
	}
}

func TestAccAiAdapter(t *testing.T) {
	skipUnlessV2(t)

	resource.Test(t, resource.TestCase{
		PreCheck:          func() { testAccPreCheck(t) },
		ProviderFactories: testAccProviders(),
		Steps: []resource.TestStep{
			{
				Config: `
resource "cloudstack_ai_adapter" "test" {
  name       = "terraform-acceptance"
  base_model = "llama-3.3-70b-instruct"
  rank       = 16
  uri        = "s3://adapters/terraform-acceptance/"
}`,
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudstack_ai_adapter.test", "id"),
					// The name a request addresses, which is the
					// reason anyone declares one of these.
					resource.TestCheckResourceAttr("cloudstack_ai_adapter.test",
						"served_model_name", "llama-3.3-70b-instruct:terraform-acceptance"),
				),
			},
			{
				ResourceName:      "cloudstack_ai_adapter.test",
				ImportState:       true,
				ImportStateVerify: true,
				// Where it was fetched from is not kept by the engine.
				ImportStateVerifyIgnore: []string{"uri"},
			},
		},
	})
}

func TestAccAiCertificate(t *testing.T) {
	skipUnlessV2(t)

	resource.Test(t, resource.TestCase{
		PreCheck:          func() { testAccPreCheck(t) },
		ProviderFactories: testAccProviders(),
		Steps: []resource.TestStep{
			{
				Config: fmt.Sprintf(`
resource "cloudstack_ai_certificate" "test" {
  custom_domain = "terraform.acceptance.invalid"
  certificate   = <<-EOT
%s
  EOT
  private_key   = <<-EOT
%s
  EOT
}`, os.Getenv("CLOUDSTACK_AI_TEST_CERT"), os.Getenv("CLOUDSTACK_AI_TEST_KEY")),
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudstack_ai_certificate.test", "fingerprint"),
					resource.TestCheckResourceAttrSet("cloudstack_ai_certificate.test", "expires"),
				),
			},
			{
				ResourceName:      "cloudstack_ai_certificate.test",
				ImportState:       true,
				ImportStateVerify: true,
				// Metadata only comes back: the engine never returns
				// the body and never the key.
				ImportStateVerifyIgnore: []string{"certificate", "private_key"},
			},
		},
	})
}

func TestAccAiPrivateEndpoint(t *testing.T) {
	skipUnlessV2(t)

	resource.Test(t, resource.TestCase{
		PreCheck:          func() { testAccPreCheck(t) },
		ProviderFactories: testAccProviders(),
		Steps: []resource.TestStep{
			{
				Config: fmt.Sprintf(`
resource "cloudstack_ai_private_endpoint" "test" {
  deployment     = %q
  network_id     = %q
  public_enabled = false
}`, os.Getenv("CLOUDSTACK_AI_TEST_DEPLOYMENT"), os.Getenv("CLOUDSTACK_AI_TEST_NETWORK")),
				Check: resource.ComposeTestCheckFunc(
					resource.TestCheckResourceAttrSet("cloudstack_ai_private_endpoint.test", "id"),
					// The phrase, not a flag: "private on <net>".
					resource.TestMatchResourceAttr("cloudstack_ai_private_endpoint.test",
						"exposure", regexp.MustCompile(`private`)),
				),
			},
			{
				ResourceName:      "cloudstack_ai_private_endpoint.test",
				ImportState:       true,
				ImportStateVerify: true,
			},
		},
	})
}

func TestAccAiGuardrailProfile(t *testing.T) {
	skipUnlessV2(t)

	resource.Test(t, resource.TestCase{
		PreCheck:          func() { testAccPreCheck(t) },
		ProviderFactories: testAccProviders(),
		Steps: []resource.TestStep{
			{
				Config: `
resource "cloudstack_ai_guardrail_profile" "test" {
  name   = "terraform-acceptance"
  scope  = "account"
  target = "acme"
  preset = "education-safe"
}`,
				Check: resource.TestCheckResourceAttrSet("cloudstack_ai_guardrail_profile.test", "id"),
			},
			{
				// The one field update can change.
				Config: `
resource "cloudstack_ai_guardrail_profile" "test" {
  name   = "terraform-acceptance"
  scope  = "account"
  target = "other"
  preset = "education-safe"
}`,
				Check: resource.TestCheckResourceAttr("cloudstack_ai_guardrail_profile.test",
					"target", "other"),
			},
		},
	})
}

func TestAccAiEvaluationsDataSource(t *testing.T) {
	skipUnlessV2(t)

	resource.Test(t, resource.TestCase{
		PreCheck:          func() { testAccPreCheck(t) },
		ProviderFactories: testAccProviders(),
		Steps: []resource.TestStep{
			{
				Config: fmt.Sprintf(`
data "cloudstack_ai_evaluations" "test" {
  model = %q
}`, os.Getenv("CLOUDSTACK_AI_TEST_MODEL")),
				Check: resource.TestCheckResourceAttrSet("data.cloudstack_ai_evaluations.test", "id"),
			},
		},
	})
}
