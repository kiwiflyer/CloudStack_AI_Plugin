// Licensed to the Apache Software Foundation (ASF) under one or more
// contributor license agreements. See the NOTICE file distributed with this
// work for additional information regarding copyright ownership. The ASF
// licenses this file to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance with the
// License. You may obtain a copy of the License at
//
//	http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
// WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
// License for the specific language governing permissions and limitations
// under the License.
package provider

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"os"
	"regexp"
	"testing"
	"time"

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

// selfSigned returns a PEM certificate and key for one domain.
//
// The engine checks a certificate's subject alternative names against
// the custom domain it is uploaded for, and refuses a mismatch — so
// this test cannot use a placeholder, and asking an operator to put
// real key material in the environment to run it is worse. Generating
// a throwaway pair here keeps the test self-contained and commits no
// secret.
func selfSigned(t *testing.T, domain string) (string, string) {
	t.Helper()

	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generating a key: %v", err)
	}
	template := x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: domain},
		DNSNames:     []string{domain},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageKeyEncipherment | x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("signing a certificate: %v", err)
	}

	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{
		Type:  "PRIVATE KEY",
		Bytes: mustMarshalKey(t, key),
	})
	return string(certPEM), string(keyPEM)
}

func mustMarshalKey(t *testing.T, key *rsa.PrivateKey) []byte {
	t.Helper()
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatalf("marshalling a key: %v", err)
	}
	return der
}

// needEnv skips a test that cannot run without something only a live
// zone can provide — a deployment to attach to, a model to read.
// Without this the test ran anyway, sent an empty value, and failed
// with the engine complaining about a missing parameter, which reads
// like a defect in the provider rather than an unconfigured run.
func needEnv(t *testing.T, names ...string) {
	t.Helper()
	for _, name := range names {
		if os.Getenv(name) == "" {
			t.Skipf("set %s to run this against a live zone", name)
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
  domain    = "/"
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
  domain    = "/"
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
	// The engine looks for weights where the uri points — "no adapter
	// weights found; expected a .safetensors file" — so this needs a
	// real artefact, not a placeholder.
	needEnv(t, "CLOUDSTACK_AI_TEST_ADAPTER_URI")

	resource.Test(t, resource.TestCase{
		PreCheck:          func() { testAccPreCheck(t) },
		ProviderFactories: testAccProviders(),
		Steps: []resource.TestStep{
			{
				Config: fmt.Sprintf(`
resource "cloudstack_ai_adapter" "test" {
  name       = "terraform-acceptance"
  base_model = "llama-3.3-70b-instruct"
  rank       = 16
  uri        = %q
  size_bytes = 1048576
}`, os.Getenv("CLOUDSTACK_AI_TEST_ADAPTER_URI")),
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

	certPEM, keyPEM := selfSigned(t, "terraform.acceptance.invalid")

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
}`, certPEM, keyPEM),
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
	needEnv(t, "CLOUDSTACK_AI_TEST_DEPLOYMENT", "CLOUDSTACK_AI_TEST_NETWORK")

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
	needEnv(t, "CLOUDSTACK_AI_TEST_MODEL")

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
