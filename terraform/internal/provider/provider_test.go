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
package provider

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hashicorp/terraform-plugin-sdk/v2/helper/schema"
)

func TestTheProviderSchemaIsValid(t *testing.T) {
	if err := New().InternalValidate(); err != nil {
		t.Fatalf("provider schema is invalid: %v", err)
	}
}

// SPEC.md section 7 names exactly these. A resource missing here is a
// resource a tenant cannot express in Terraform.
func TestEverySpecifiedResourceExists(t *testing.T) {
	provider := New()
	for _, name := range []string{
		"cloudstack_ai_deployment",
		"cloudstack_ai_api_key",
		"cloudstack_ai_generation_profile",
		"cloudstack_ai_serving_profile",
		"cloudstack_ai_budget",
	} {
		if _, ok := provider.ResourcesMap[name]; !ok {
			t.Errorf("missing resource %s", name)
		}
	}
}

func TestTheModelsDataSourceExists(t *testing.T) {
	if _, ok := New().DataSourcesMap["cloudstack_ai_models"]; !ok {
		t.Fatal("missing data source cloudstack_ai_models")
	}
}

// SPEC.md section 7: "Import support".
func TestEveryResourceCanBeImported(t *testing.T) {
	for name, resource := range New().ResourcesMap {
		if resource.Importer == nil {
			t.Errorf("%s cannot be imported", name)
		}
	}
}

func TestTheApiKeySecretIsMarkedSensitive(t *testing.T) {
	field := New().ResourcesMap["cloudstack_ai_api_key"].Schema["api_key"]

	if !field.Sensitive {
		t.Fatal("the key secret must be marked sensitive or it prints in plan output")
	}
	if !field.Computed {
		t.Fatal("the key secret is issued by CloudStack, not supplied")
	}
}

// Every scope on a key forces a new one: the secret cannot be reissued
// in place, so changing a scope has to mean a new key.
func TestChangingAKeyScopeForcesANewKey(t *testing.T) {
	fields := New().ResourcesMap["cloudstack_ai_api_key"].Schema
	for _, name := range []string{"name", "allowed_models", "cidrs", "expires_at", "rate_limit_rpm"} {
		if !fields[name].ForceNew {
			t.Errorf("%s must force a new key", name)
		}
	}
}

func TestAnApiKeyHasNoUpdate(t *testing.T) {
	if New().ResourcesMap["cloudstack_ai_api_key"].UpdateContext != nil {
		t.Fatal("a key's scopes cannot be updated in place")
	}
}

func testClient(handler http.HandlerFunc) (*Client, func()) {
	server := httptest.NewServer(handler)
	return &Client{
		BaseURL: server.URL, APIKey: "k", SecretKey: "s",
		HTTP: server.Client(), PollInterval: 1,
	}, server.Close
}

func dataFor(t *testing.T, resource *schema.Resource, id string, values map[string]any) *schema.ResourceData {
	t.Helper()
	d := resource.TestResourceData()
	d.SetId(id)
	for key, value := range values {
		if err := d.Set(key, value); err != nil {
			t.Fatalf("setting %s: %v", key, err)
		}
	}
	return d
}

// The whole point of a write-once secret: reading the resource back must
// not overwrite what state holds, because the API will never return it
// again. Getting this wrong silently empties every key in state on the
// next refresh.
func TestReadingAKeyLeavesTheStoredSecretAlone(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaiapikeysresponse":{"count":1,"aiapikey":` +
			`[{"id":"k-1","name":"ci","lastused":"2026-11-08T10:00:00Z"}]}}`))
	})
	defer done()

	resource := resourceAiApiKey()
	d := dataFor(t, resource, "k-1", map[string]any{"api_key": "aisvc-k-1-secret"})

	if diags := readAiApiKey(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if got := d.Get("api_key").(string); got != "aisvc-k-1-secret" {
		t.Fatalf("the stored secret was overwritten: %q", got)
	}
	if got := d.Get("last_used").(string); got == "" {
		t.Fatal("last_used was not read back")
	}
}

// A resource deleted outside Terraform must clear its id rather than
// error, or every subsequent plan fails instead of recreating it.
func TestAMissingKeyClearsTheId(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaiapikeysresponse":{"count":0}}`))
	})
	defer done()

	d := dataFor(t, resourceAiApiKey(), "k-1", nil)
	if diags := readAiApiKey(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if d.Id() != "" {
		t.Fatalf("id should have been cleared, got %q", d.Id())
	}
}

func TestAMissingDeploymentClearsTheId(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaimodeldeploymentsresponse":{"count":0}}`))
	})
	defer done()

	d := dataFor(t, resourceAiDeployment(), "d-1", nil)
	if diags := readAiDeployment(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if d.Id() != "" {
		t.Fatalf("id should have been cleared, got %q", d.Id())
	}
}

func TestADeploymentReadsBackItsFields(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaimodeldeploymentsresponse":{"count":1,"aideployment":` +
			`[{"id":"d-1","name":"llama-prod","model":"llama","zoneid":"z-1",` +
			`"replicas":3,"state":"Running","endpoint":"https://ai.example.com/v1"}]}}`))
	})
	defer done()

	d := dataFor(t, resourceAiDeployment(), "d-1", nil)
	if diags := readAiDeployment(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if d.Get("replicas").(int) != 3 {
		t.Errorf("replicas = %d", d.Get("replicas").(int))
	}
	if d.Get("state").(string) != "Running" {
		t.Errorf("state = %q", d.Get("state").(string))
	}
}

func TestABudgetIsFoundByItsAccount(t *testing.T) {
	// A budget's identity is the account it belongs to, so an import
	// address of "acme" has to resolve.
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaibudgetsresponse":{"count":1,"aibudget":` +
			`[{"account":"acme","monthly":100,"currency":"USD","onexceed":"block",` +
			`"spent":42,"percentused":42}]}}`))
	})
	defer done()

	d := dataFor(t, resourceAiBudget(), "acme", nil)
	if diags := readAiBudget(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if d.Get("monthly").(float64) != 100 {
		t.Errorf("monthly = %v", d.Get("monthly"))
	}
	if d.Get("on_exceed").(string) != "block" {
		t.Errorf("on_exceed = %q", d.Get("on_exceed"))
	}
}

func TestAnApiErrorFromAReadIsReported(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(531)
		_, _ = w.Write([]byte(`{"listaibudgetsresponse":{"errorcode":531,` +
			`"errortext":"account not permitted"}}`))
	})
	defer done()

	d := dataFor(t, resourceAiBudget(), "acme", nil)
	diags := readAiBudget(nil, d, client)

	if !diags.HasError() {
		t.Fatal("an API error must not be read as an absent resource")
	}
	if d.Id() == "" {
		t.Fatal("an API error must not clear the id; that would delete it from state")
	}
}
