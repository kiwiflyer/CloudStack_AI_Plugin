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
	"context"
	"encoding/json"
	"fmt"

	"github.com/hashicorp/terraform-plugin-sdk/v2/diag"
	"github.com/hashicorp/terraform-plugin-sdk/v2/helper/schema"
)

// New returns the AI Services provider (SPEC.md section 7).
//
// These resources are meant to be folded into the CloudStack Terraform
// provider rather than shipped separately; they are kept here so they
// can be developed and tested alongside the commands they call.
func New() *schema.Provider {
	return &schema.Provider{
		Schema: map[string]*schema.Schema{
			"api_url": {
				Type:        schema.TypeString,
				Required:    true,
				DefaultFunc: schema.EnvDefaultFunc("CLOUDSTACK_API_URL", nil),
				Description: "The CloudStack management server API endpoint.",
			},
			"api_key": {
				Type:        schema.TypeString,
				Required:    true,
				Sensitive:   true,
				DefaultFunc: schema.EnvDefaultFunc("CLOUDSTACK_API_KEY", nil),
			},
			"secret_key": {
				Type:        schema.TypeString,
				Required:    true,
				Sensitive:   true,
				DefaultFunc: schema.EnvDefaultFunc("CLOUDSTACK_SECRET_KEY", nil),
			},
			"timeout": {
				Type:        schema.TypeInt,
				Optional:    true,
				Default:     1800,
				Description: "Seconds to wait for an async command's job.",
			},
		},
		ResourcesMap: map[string]*schema.Resource{
			"cloudstack_ai_deployment":         resourceAiDeployment(),
			"cloudstack_ai_api_key":            resourceAiApiKey(),
			"cloudstack_ai_generation_profile": resourceAiGenerationProfile(),
			"cloudstack_ai_serving_profile":    resourceAiServingProfile(),
			"cloudstack_ai_budget":             resourceAiBudget(),

			// Tier 2. Declared unconditionally: Terraform cannot ask
			// CloudStack what is registered before a plan, and a
			// resource that came and went would be a schema error
			// rather than a message anyone can act on. v2Error turns
			// "unknown command" into the setting to turn on.
			"cloudstack_ai_adapter":           resourceAiAdapter(),
			"cloudstack_ai_batch":             resourceAiBatch(),
			"cloudstack_ai_certificate":       resourceAiCertificate(),
			"cloudstack_ai_private_endpoint":  resourceAiPrivateEndpoint(),
			"cloudstack_ai_guardrail_profile": resourceAiGuardrailProfile(),
		},
		DataSourcesMap: map[string]*schema.Resource{
			"cloudstack_ai_models":      dataSourceAiModels(),
			"cloudstack_ai_evaluations": dataSourceAiEvaluations(),
		},
		ConfigureContextFunc: configure,
	}
}

func configure(_ context.Context, d *schema.ResourceData) (any, diag.Diagnostics) {
	return &Client{
		BaseURL:     d.Get("api_url").(string),
		APIKey:      d.Get("api_key").(string),
		SecretKey:   d.Get("secret_key").(string),
		PollTimeout: d.Get("timeout").(int),
	}, nil
}

func clientFrom(meta any) *Client {
	return meta.(*Client)
}

// firstItem pulls the single object out of a list response, or reports
// that it was not there.
//
// A resource that has been deleted outside Terraform comes back as an
// empty list, and the caller is expected to clear the ID rather than
// error: that is what lets `terraform apply` recreate it.
func firstItem(body []byte, key string) (map[string]any, bool) {
	fields := unwrap(body)
	raw, ok := fields[key]
	if !ok {
		return nil, false
	}
	var items []map[string]any
	if err := json.Unmarshal(raw, &items); err != nil || len(items) == 0 {
		return nil, false
	}
	return items[0], true
}

func allItems(body []byte, key string) []map[string]any {
	fields := unwrap(body)
	raw, ok := fields[key]
	if !ok {
		return nil
	}
	var items []map[string]any
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil
	}
	return items
}

// setFrom copies response fields onto the resource, mapping each
// Terraform attribute name to the response key the plugin uses.
func setFrom(d *schema.ResourceData, item map[string]any, mapping map[string]string) error {
	for attribute, responseKey := range mapping {
		value, ok := item[responseKey]
		if !ok {
			continue
		}
		if err := d.Set(attribute, value); err != nil {
			return fmt.Errorf("setting %s: %w", attribute, err)
		}
	}
	return nil
}

// idOf reads the identifier out of a created object.
func idOf(item map[string]any) string {
	if id, ok := item["id"].(string); ok {
		return id
	}
	return ""
}
