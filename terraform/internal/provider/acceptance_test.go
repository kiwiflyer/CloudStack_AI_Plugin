package provider

import (
	"fmt"
	"os"
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
		PreCheck:                 func() { testAccPreCheck(t) },
		ProviderFactories:        testAccProviders(),
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
