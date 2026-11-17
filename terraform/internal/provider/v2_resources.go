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
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/hashicorp/terraform-plugin-sdk/v2/diag"
	"github.com/hashicorp/terraform-plugin-sdk/v2/helper/schema"
)

// Tier 2 resources (SPEC.md section 8).
//
// The plugin registers these commands only when ai.services.v2.enabled
// is on. The provider declares the resources either way: Terraform
// cannot ask CloudStack what is registered before a plan, and a
// resource that came and went would be a schema error rather than a
// message anyone can act on. So a v1 install fails at apply, and
// `v2Error` makes that failure say which setting to turn on.

// v2Error turns CloudStack's "unknown command" into something
// actionable.
//
// Only that one case. A permission or validation error dressed up as a
// v2 problem would send an operator to the wrong place entirely, which
// is worse than the bare message.
func v2Error(err error) diag.Diagnostics {
	if err == nil {
		return nil
	}
	text := err.Error()
	unknown := strings.Contains(strings.ToLower(text), "unknown api command") ||
		strings.Contains(strings.ToLower(text), "unable to find api")
	if !unknown {
		return diag.FromErr(err)
	}
	return diag.Errorf(
		"%s — this is a Tier 2 command, which CloudStack only registers when the "+
			"global setting ai.services.v2.enabled is true. Set it and restart the "+
			"management server.", text)
}

// readInto is the shape every v2 read shares: find the row by id in a
// list response, copy the mapped fields onto state, and clear the id if
// it is not there any more so the next apply recreates it.
//
// Nothing here ever writes a field the response does not carry, which
// is what keeps write-once material (a certificate's key) in state.
func readInto(
	d *schema.ResourceData, body []byte, key string,
	strings_ map[string]string, ints map[string]string, bools map[string]string,
) diag.Diagnostics {
	for _, item := range allItems(body, key) {
		if idOf(item) != d.Id() {
			continue
		}
		if err := setFrom(d, item, strings_); err != nil {
			return diag.FromErr(err)
		}
		for attribute, responseKey := range ints {
			if value, ok := item[responseKey].(float64); ok {
				if err := d.Set(attribute, int(value)); err != nil {
					return diag.FromErr(err)
				}
			}
		}
		for attribute, responseKey := range bools {
			if value, ok := item[responseKey].(bool); ok {
				if err := d.Set(attribute, value); err != nil {
					return diag.FromErr(err)
				}
			}
		}
		return nil
	}
	d.SetId("")
	return nil
}

// -- cloudstack_ai_adapter ---------------------------------------------

func resourceAiAdapter() *schema.Resource {
	return &schema.Resource{
		Description: "A LoRA adapter served alongside its base model. " +
			"Addressed in requests as `<base model>:<adapter name>`.",
		CreateContext: createAiAdapter,
		ReadContext:   readAiAdapter,
		DeleteContext: deleteAiAdapter,
		// No update: the artefact is immutable and its rank is what
		// vLLM was started with, so a change means another upload.
		Importer: importByID,
		Schema: map[string]*schema.Schema{
			"name":       {Type: schema.TypeString, Required: true, ForceNew: true},
			"base_model": {Type: schema.TypeString, Required: true, ForceNew: true},
			"rank": {Type: schema.TypeInt, Required: true, ForceNew: true,
				Description: "Must not exceed the serving profile's max_lora_rank, " +
					"or a valid adapter fails to load at request time."},
			"uri": {Type: schema.TypeString, Required: true, ForceNew: true,
				Description: "Where the adapter's files are fetched from."},
			"served_model_name": {Type: schema.TypeString, Computed: true,
				Description: "What a request actually names. The adapter's own " +
					"name addresses nothing."},
			"size_bytes": {Type: schema.TypeInt, Required: true, ForceNew: true,
				Description: "Checked against the engine's adapter size limit; " +
					"declared here until artefacts are fetched and measured."},
			"account": {Type: schema.TypeString, Computed: true},
			"shared":  {Type: schema.TypeBool, Computed: true},
		},
	}
}

func createAiAdapter(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).CallAsync("uploadAiAdapter", map[string]string{
		"name":      d.Get("name").(string),
		"basemodel": d.Get("base_model").(string),
		"rank":      strconv.Itoa(d.Get("rank").(int)),
		"sizebytes": strconv.Itoa(d.Get("size_bytes").(int)),
		"uri":       d.Get("uri").(string),
	})
	if err != nil {
		return v2Error(err)
	}
	item := unwrapObject(body, "aiadapter")
	if item == nil {
		if first, ok := firstItem(body, "aiadapter"); ok {
			item = first
		} else {
			return diag.Errorf("uploadAiAdapter returned no adapter")
		}
	}
	d.SetId(idOf(item))
	return readAiAdapter(nil, d, meta)
}

func readAiAdapter(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("listAiAdapters", nil)
	if err != nil {
		return v2Error(err)
	}
	return readInto(d, body, "aiadapter",
		map[string]string{
			"name": "name", "base_model": "basemodel",
			"served_model_name": "servedmodelname", "account": "account",
		},
		map[string]string{"rank": "rank", "size_bytes": "sizebytes"},
		map[string]string{"shared": "shared"})
}

func deleteAiAdapter(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	if _, err := clientFrom(meta).Call("deleteAiAdapter",
		map[string]string{"id": d.Id()}); err != nil {
		return v2Error(err)
	}
	d.SetId("")
	return nil
}

// -- cloudstack_ai_batch -----------------------------------------------

func resourceAiBatch() *schema.Resource {
	return &schema.Resource{
		Description: "A submitted batch job. This is work rather than desired " +
			"state: destroying it cancels what has not run, and requests already " +
			"completed stay billed with their results available.",
		CreateContext: createAiBatch,
		ReadContext:   readAiBatch,
		DeleteContext: deleteAiBatch,
		// No update: a submitted batch cannot be changed.
		Importer: importByID,
		Schema: map[string]*schema.Schema{
			"model": {Type: schema.TypeString, Required: true, ForceNew: true},
			"input_uri": {Type: schema.TypeString, Required: true, ForceNew: true,
				Description: "The JSONL of requests to run."},
			"status":        {Type: schema.TypeString, Computed: true},
			"request_count": {Type: schema.TypeInt, Computed: true},
			"output_uri": {Type: schema.TypeString, Computed: true,
				Description: "Where the results land once the batch finishes."},
			"account":  {Type: schema.TypeString, Computed: true},
			"created":  {Type: schema.TypeString, Computed: true},
			"finished": {Type: schema.TypeString, Computed: true},
		},
	}
}

func createAiBatch(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).CallAsync("createAiBatch", map[string]string{
		"model":    d.Get("model").(string),
		"inputuri": d.Get("input_uri").(string),
	})
	if err != nil {
		return v2Error(err)
	}
	item := unwrapObject(body, "aibatch")
	if item == nil {
		if first, ok := firstItem(body, "aibatch"); ok {
			item = first
		} else {
			return diag.Errorf("createAiBatch returned no batch")
		}
	}
	d.SetId(idOf(item))
	return readAiBatch(nil, d, meta)
}

func readAiBatch(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("listAiBatches", nil)
	if err != nil {
		return v2Error(err)
	}
	return readInto(d, body, "aibatch",
		map[string]string{
			"model": "model", "status": "status", "account": "account",
			"created": "created", "finished": "finished", "output_uri": "outputuri",
		},
		map[string]string{"request_count": "requestcount"},
		nil)
}

func deleteAiBatch(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	// Cancel, not delete: there is no deleteAiBatch, and the record of
	// what was billed has to survive the Terraform resource.
	if _, err := clientFrom(meta).CallAsync("cancelAiBatch",
		map[string]string{"id": d.Id()}); err != nil {
		return v2Error(err)
	}
	d.SetId("")
	return nil
}

// -- cloudstack_ai_certificate -----------------------------------------

func resourceAiCertificate() *schema.Resource {
	return &schema.Resource{
		Description: "A TLS certificate for a private endpoint's custom domain. " +
			"The engine stores it and returns metadata only; the body and key " +
			"live in Terraform state and are never read back.",
		CreateContext: createAiCertificate,
		ReadContext:   readAiCertificate,
		UpdateContext: updateAiCertificate,
		DeleteContext: deleteAiCertificate,
		Importer:      importByID,
		Schema: map[string]*schema.Schema{
			"custom_domain": {Type: schema.TypeString, Required: true, ForceNew: true,
				Description: "A different domain is a different certificate."},
			"certificate": {Type: schema.TypeString, Required: true, Sensitive: true,
				Description: "PEM. Sensitive because it travels with the key, and " +
					"showing one while hiding the other invites pasting them the " +
					"wrong way round."},
			"private_key": {Type: schema.TypeString, Required: true, Sensitive: true,
				Description: "PEM, unencrypted: the proxy cannot answer a passphrase " +
					"prompt. Changing it renews in place rather than making a new " +
					"certificate, so endpoints pointing at this id keep working — " +
					"though the proxy is rebuilt, which interrupts traffic."},
			"fingerprint": {Type: schema.TypeString, Computed: true},
			"expires":     {Type: schema.TypeString, Computed: true},
			"serial": {Type: schema.TypeInt, Computed: true,
				Description: "Bumped on every replacement; a proxy built from an " +
					"older serial is serving superseded material."},
			"account": {Type: schema.TypeString, Computed: true},
		},
	}
}

func createAiCertificate(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("uploadAiCertificate", map[string]string{
		"customdomain": d.Get("custom_domain").(string),
		"certificate":  d.Get("certificate").(string),
		"privatekey":   d.Get("private_key").(string),
	})
	if err != nil {
		return v2Error(err)
	}
	d.SetId(createdID(body))
	return readAiCertificate(nil, d, meta)
}

func readAiCertificate(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("listAiCertificates", nil)
	if err != nil {
		return v2Error(err)
	}
	// Deliberately touches neither `certificate` nor `private_key`:
	// the response never carries them, and writing what is not there
	// would empty both on the next refresh.
	return readInto(d, body, "aicertificate",
		map[string]string{
			"custom_domain": "customdomain", "fingerprint": "fingerprint",
			"expires": "expires", "account": "account",
		},
		map[string]string{"serial": "serial"},
		nil)
}

func updateAiCertificate(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	// Replace rather than recreate: every private endpoint naming this
	// id keeps naming it, which is the whole point of the command.
	if _, err := clientFrom(meta).Call("replaceAiCertificate", map[string]string{
		"id":          d.Id(),
		"certificate": d.Get("certificate").(string),
		"privatekey":  d.Get("private_key").(string),
	}); err != nil {
		return v2Error(err)
	}
	return readAiCertificate(nil, d, meta)
}

func deleteAiCertificate(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	if _, err := clientFrom(meta).Call("deleteAiCertificate",
		map[string]string{"id": d.Id()}); err != nil {
		return v2Error(err)
	}
	d.SetId("")
	return nil
}

// -- cloudstack_ai_private_endpoint ------------------------------------

func resourceAiPrivateEndpoint() *schema.Resource {
	return &schema.Resource{
		Description: "A deployment fronted privately on a network tier by a proxy " +
			"VM. Not a Kubernetes LoadBalancer: the CloudStack cloud-controller " +
			"takes a Service's network from the nodes' first NIC, so it cannot be " +
			"placed on a chosen tier.",
		CreateContext: createAiPrivateEndpoint,
		ReadContext:   readAiPrivateEndpoint,
		DeleteContext: deleteAiPrivateEndpoint,
		// No update command: the proxy VM is built from these values
		// at boot, so any change means a new proxy regardless.
		Importer: importByID,
		Schema: map[string]*schema.Schema{
			"deployment": {Type: schema.TypeString, Required: true, ForceNew: true},
			"network_id": {Type: schema.TypeString, Optional: true, ForceNew: true,
				Description: "The tier the proxy sits on. Required unless the " +
					"endpoint is also public, or nothing could reach it."},
			"public_enabled": {Type: schema.TypeBool, Optional: true, Default: true,
				ForceNew: true},
			"custom_domain": {Type: schema.TypeString, Optional: true, ForceNew: true},
			"tls": {Type: schema.TypeString, Optional: true, Default: "none",
				ForceNew: true,
				Description: "none, acme or uploaded. A custom domain needs one of " +
					"the latter two; ACME needs the endpoint to be public."},
			"certificate_id": {Type: schema.TypeString, Optional: true, ForceNew: true,
				Description: "Required when tls is uploaded."},
			"exposure": {Type: schema.TypeString, Computed: true,
				Description: "In words: what is reachable, from where, as what."},
			"account": {Type: schema.TypeString, Computed: true},
		},
	}
}

func createAiPrivateEndpoint(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).CallAsync("createAiPrivateEndpoint", map[string]string{
		"deployment":    d.Get("deployment").(string),
		"networkid":     d.Get("network_id").(string),
		"publicenabled": strconv.FormatBool(d.Get("public_enabled").(bool)),
		"customdomain":  d.Get("custom_domain").(string),
		"tls":           d.Get("tls").(string),
		"certificateid": d.Get("certificate_id").(string),
	})
	if err != nil {
		return v2Error(err)
	}
	item := unwrapObject(body, "aiprivateendpoint")
	if item == nil {
		if first, ok := firstItem(body, "aiprivateendpoint"); ok {
			item = first
		} else {
			return diag.Errorf("createAiPrivateEndpoint returned no endpoint")
		}
	}
	d.SetId(idOf(item))
	return readAiPrivateEndpoint(nil, d, meta)
}

func readAiPrivateEndpoint(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("listAiPrivateEndpoints", nil)
	if err != nil {
		return v2Error(err)
	}
	return readInto(d, body, "aiprivateendpoint",
		map[string]string{
			"deployment": "deployment", "exposure": "exposure",
			"network_id": "network", "custom_domain": "customdomain",
			"tls": "tls", "certificate_id": "certificate", "account": "account",
		},
		nil,
		map[string]string{"public_enabled": "publicenabled"})
}

func deleteAiPrivateEndpoint(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	if _, err := clientFrom(meta).CallAsync("deleteAiPrivateEndpoint",
		map[string]string{"id": d.Id()}); err != nil {
		return v2Error(err)
	}
	d.SetId("")
	return nil
}

// -- cloudstack_ai_guardrail_profile -----------------------------------

func resourceAiGuardrailProfile() *schema.Resource {
	return &schema.Resource{
		Description: "A guardrail profile: the third profile type, with the same " +
			"scopes and lock semantics as the other two. Scoped to the caller — a " +
			"tenant may tighten their own account and cannot touch a profile set " +
			"above them, so `scope` and `target` decide whether an apply is " +
			"permitted, not the role alone.",
		CreateContext: createAiGuardrailProfile,
		ReadContext:   readAiGuardrailProfile,
		UpdateContext: updateAiGuardrailProfile,
		DeleteContext: deleteAiGuardrailProfile,
		Importer:      importByID,
		Schema: map[string]*schema.Schema{
			"name": {Type: schema.TypeString, Required: true, ForceNew: true},
			"scope": {Type: schema.TypeString, Required: true, ForceNew: true,
				Description: "Which level it applies at: platform, domain, " +
					"account or deployment."},
			"target": {Type: schema.TypeString, Optional: true,
				Description: "What it applies to at that scope. The one field " +
					"updateAiGuardrailProfile can change."},
			"preset": {Type: schema.TypeString, Optional: true, ForceNew: true,
				Description: "A named set of checks, such as the education-safe " +
					"profile for school customers."},
		},
	}
}

func guardrailParams(d *schema.ResourceData) map[string]string {
	return map[string]string{
		"name":   d.Get("name").(string),
		"scope":  d.Get("scope").(string),
		"target": d.Get("target").(string),
		"preset": d.Get("preset").(string),
	}
}

func createAiGuardrailProfile(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("createAiGuardrailProfile", guardrailParams(d))
	if err != nil {
		return v2Error(err)
	}
	d.SetId(createdID(body))
	return readAiGuardrailProfile(nil, d, meta)
}

func readAiGuardrailProfile(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("listAiGuardrailProfiles", nil)
	if err != nil {
		return v2Error(err)
	}
	return readInto(d, body, "aiguardrailprofile",
		map[string]string{"name": "name", "scope": "scope", "target": "target"},
		nil, nil)
}

func updateAiGuardrailProfile(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	if _, err := clientFrom(meta).Call("updateAiGuardrailProfile", map[string]string{
		"id":     d.Id(),
		"target": d.Get("target").(string),
	}); err != nil {
		return v2Error(err)
	}
	return readAiGuardrailProfile(nil, d, meta)
}

func deleteAiGuardrailProfile(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	if _, err := clientFrom(meta).Call("deleteAiGuardrailProfile",
		map[string]string{"id": d.Id()}); err != nil {
		return v2Error(err)
	}
	d.SetId("")
	return nil
}

// -- cloudstack_ai_evaluations -----------------------------------------

func dataSourceAiEvaluations() *schema.Resource {
	return &schema.Resource{
		Description: "A model's pre-publish evaluation runs. A data source and " +
			"not a resource: an evaluation is a judgement already made, and the " +
			"gate result is the thing worth reading.",
		ReadContext: func(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
			model := d.Get("model").(string)
			body, err := clientFrom(meta).Call("listAiEvaluations",
				map[string]string{"model": model})
			if err != nil {
				return v2Error(err)
			}
			rows := make([]map[string]any, 0)
			for _, item := range allItems(body, "aievaluation") {
				passed, _ := item["passed"].(bool)
				overridden, _ := item["overridden"].(bool)
				rows = append(rows, map[string]any{
					"id":       fmt.Sprint(item["id"]),
					"revision": fmt.Sprint(item["revision"]),
					"summary":  fmt.Sprint(item["summary"]),
					"passed":   passed,
					// Its own field: blocked and blocked-but-published
					// are different facts about the catalog.
					"overridden": overridden,
				})
			}
			if err := d.Set("evaluations", rows); err != nil {
				return diag.FromErr(err)
			}
			d.SetId("ai-evaluations-" + model)
			return nil
		},
		Schema: map[string]*schema.Schema{
			"model": {Type: schema.TypeString, Required: true,
				Description: "Evaluations are per model; there is no model-less list."},
			"evaluations": {
				Type:     schema.TypeList,
				Computed: true,
				Elem: &schema.Resource{Schema: map[string]*schema.Schema{
					"id":         {Type: schema.TypeString, Computed: true},
					"revision":   {Type: schema.TypeString, Computed: true},
					"summary":    {Type: schema.TypeString, Computed: true},
					"passed":     {Type: schema.TypeBool, Computed: true},
					"overridden": {Type: schema.TypeBool, Computed: true},
				}},
			},
		},
	}
}
