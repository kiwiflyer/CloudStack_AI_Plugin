package provider

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/hashicorp/terraform-plugin-sdk/v2/diag"
	"github.com/hashicorp/terraform-plugin-sdk/v2/helper/schema"
)

// importByID is the import behaviour every resource here shares: the
// address is the object's id, and Read fills in the rest.
var importByID = &schema.ResourceImporter{StateContext: schema.ImportStatePassthroughContext}

func csv(d *schema.ResourceData, key string) string {
	raw, ok := d.GetOk(key)
	if !ok {
		return ""
	}
	parts := make([]string, 0)
	for _, item := range raw.([]any) {
		parts = append(parts, fmt.Sprint(item))
	}
	return strings.Join(parts, ",")
}

// -- cloudstack_ai_deployment ------------------------------------------

func resourceAiDeployment() *schema.Resource {
	return &schema.Resource{
		Description:   "A served model deployment.",
		CreateContext: createAiDeployment,
		ReadContext:   readAiDeployment,
		UpdateContext: updateAiDeployment,
		DeleteContext: deleteAiDeployment,
		Importer:      importByID,
		Schema: map[string]*schema.Schema{
			"model": {Type: schema.TypeString, Required: true, ForceNew: true,
				Description: "Catalog model to serve."},
			"zone_id": {Type: schema.TypeString, Required: true, ForceNew: true},
			"name":    {Type: schema.TypeString, Optional: true, Computed: true},
			"replicas": {Type: schema.TypeInt, Optional: true, Default: 1,
				Description: "Desired replicas; autoscaling may move this."},
			"serving_profile": {Type: schema.TypeString, Optional: true},
			"state":           {Type: schema.TypeString, Computed: true},
			"endpoint":        {Type: schema.TypeString, Computed: true},
		},
	}
}

func deploymentParams(d *schema.ResourceData) map[string]string {
	return map[string]string{
		"model":          d.Get("model").(string),
		"zoneid":         d.Get("zone_id").(string),
		"name":           d.Get("name").(string),
		"replicas":       strconv.Itoa(d.Get("replicas").(int)),
		"servingprofile": d.Get("serving_profile").(string),
	}
}

func createAiDeployment(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).CallAsync("deployAiModel", deploymentParams(d))
	if err != nil {
		return diag.FromErr(err)
	}
	item, ok := firstItem(body, "aideployment")
	if !ok {
		// An async job returns the object bare, not in a list.
		if single := unwrapObject(body, "aideployment"); single != nil {
			item = single
		} else {
			return diag.Errorf("deployAiModel returned no deployment")
		}
	}
	d.SetId(idOf(item))
	return readAiDeployment(nil, d, meta)
}

func readAiDeployment(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("listAiModelDeployments", map[string]string{"id": d.Id()})
	if err != nil {
		return diag.FromErr(err)
	}
	item, ok := firstItem(body, "aideployment")
	if !ok {
		// Gone outside Terraform. Clearing the id lets the next apply
		// recreate it instead of failing every plan from here on.
		d.SetId("")
		return nil
	}
	if err := setFrom(d, item, map[string]string{
		"model": "model", "name": "name", "zone_id": "zoneid",
		"state": "state", "endpoint": "endpoint",
	}); err != nil {
		return diag.FromErr(err)
	}
	if replicas, ok := item["replicas"].(float64); ok {
		if err := d.Set("replicas", int(replicas)); err != nil {
			return diag.FromErr(err)
		}
	}
	return nil
}

func updateAiDeployment(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	params := deploymentParams(d)
	params["id"] = d.Id()
	if _, err := clientFrom(meta).CallAsync("updateAiModelDeployment", params); err != nil {
		return diag.FromErr(err)
	}
	return readAiDeployment(nil, d, meta)
}

func deleteAiDeployment(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	if _, err := clientFrom(meta).CallAsync("deleteAiModelDeployment",
		map[string]string{"id": d.Id()}); err != nil {
		return diag.FromErr(err)
	}
	d.SetId("")
	return nil
}

// -- cloudstack_ai_api_key ---------------------------------------------

func resourceAiApiKey() *schema.Resource {
	return &schema.Resource{
		Description: "A scoped tenant API key. The secret is returned once, " +
			"at creation, and is stored in Terraform state thereafter.",
		CreateContext: createAiApiKey,
		ReadContext:   readAiApiKey,
		DeleteContext: deleteAiApiKey,
		// No Update: every field forces a new key, because a key's
		// scopes cannot be changed without reissuing the secret.
		Importer: importByID,
		Schema: map[string]*schema.Schema{
			"name": {Type: schema.TypeString, Required: true, ForceNew: true},
			"allowed_models": {Type: schema.TypeList, Optional: true, ForceNew: true,
				Elem: &schema.Schema{Type: schema.TypeString}},
			"cidrs": {Type: schema.TypeList, Optional: true, ForceNew: true,
				Elem: &schema.Schema{Type: schema.TypeString}},
			"expires_at":     {Type: schema.TypeString, Optional: true, ForceNew: true},
			"rate_limit_rpm": {Type: schema.TypeInt, Optional: true, ForceNew: true},
			"api_key": {Type: schema.TypeString, Computed: true, Sensitive: true,
				Description: "Shown once by CloudStack; kept in state because it " +
					"cannot be read back. An imported key has this empty."},
			"last_used": {Type: schema.TypeString, Computed: true},
		},
	}
}

func createAiApiKey(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	params := map[string]string{
		"name":          d.Get("name").(string),
		"allowedmodels": csv(d, "allowed_models"),
		"cidrs":         csv(d, "cidrs"),
		"expiresat":     d.Get("expires_at").(string),
	}
	if limit := d.Get("rate_limit_rpm").(int); limit > 0 {
		params["ratelimitrpm"] = strconv.Itoa(limit)
	}

	body, err := clientFrom(meta).Call("createAiApiKey", params)
	if err != nil {
		return diag.FromErr(err)
	}
	fields := unwrap(body)
	d.SetId(rawString(fields["id"]))
	// The only moment this value exists. It is not re-read later.
	if err := d.Set("api_key", rawString(fields["apikey"])); err != nil {
		return diag.FromErr(err)
	}
	return readAiApiKey(nil, d, meta)
}

func readAiApiKey(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("listAiApiKeys", nil)
	if err != nil {
		return diag.FromErr(err)
	}
	for _, item := range allItems(body, "aiapikey") {
		if idOf(item) != d.Id() {
			continue
		}
		// Deliberately does not touch api_key: the secret is not in
		// this response and never will be, and writing the empty
		// string would destroy what state already holds.
		return diag.FromErr(setFrom(d, item, map[string]string{
			"name": "name", "last_used": "lastused",
		}))
	}
	d.SetId("")
	return nil
}

func deleteAiApiKey(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	if _, err := clientFrom(meta).Call("revokeAiApiKey",
		map[string]string{"id": d.Id()}); err != nil {
		return diag.FromErr(err)
	}
	d.SetId("")
	return nil
}

// -- cloudstack_ai_budget ----------------------------------------------

func resourceAiBudget() *schema.Resource {
	return &schema.Resource{
		Description:   "A monthly spending budget for an account.",
		CreateContext: createAiBudget,
		ReadContext:   readAiBudget,
		UpdateContext: updateAiBudget,
		DeleteContext: deleteAiBudget,
		Importer:      importByID,
		Schema: map[string]*schema.Schema{
			"account":  {Type: schema.TypeString, Required: true, ForceNew: true},
			"monthly":  {Type: schema.TypeFloat, Required: true},
			"currency": {Type: schema.TypeString, Optional: true, Default: "USD"},
			"on_exceed": {Type: schema.TypeString, Optional: true, Default: "alert",
				Description: "alert or block."},
			"spent":        {Type: schema.TypeFloat, Computed: true},
			"percent_used": {Type: schema.TypeFloat, Computed: true},
		},
	}
}

func budgetParams(d *schema.ResourceData) map[string]string {
	return map[string]string{
		"account":  d.Get("account").(string),
		"monthly":  strconv.FormatFloat(d.Get("monthly").(float64), 'f', -1, 64),
		"currency": d.Get("currency").(string),
		"onexceed": d.Get("on_exceed").(string),
	}
}

func createAiBudget(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("createAiBudget", budgetParams(d))
	if err != nil {
		return diag.FromErr(err)
	}
	fields := unwrap(body)
	id := rawString(fields["id"])
	if id == "" {
		// A budget is identified by its account.
		id = d.Get("account").(string)
	}
	d.SetId(id)
	return readAiBudget(nil, d, meta)
}

func readAiBudget(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	body, err := clientFrom(meta).Call("listAiBudgets", nil)
	if err != nil {
		return diag.FromErr(err)
	}
	for _, item := range allItems(body, "aibudget") {
		if idOf(item) != d.Id() && fmt.Sprint(item["account"]) != d.Id() {
			continue
		}
		if err := setFrom(d, item, map[string]string{
			"account": "account", "currency": "currency", "on_exceed": "onexceed",
		}); err != nil {
			return diag.FromErr(err)
		}
		for attribute, key := range map[string]string{
			"monthly": "monthly", "spent": "spent", "percent_used": "percentused",
		} {
			if value, ok := item[key].(float64); ok {
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

func updateAiBudget(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	params := budgetParams(d)
	params["id"] = d.Id()
	if _, err := clientFrom(meta).Call("updateAiBudget", params); err != nil {
		return diag.FromErr(err)
	}
	return readAiBudget(nil, d, meta)
}

func deleteAiBudget(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
	if _, err := clientFrom(meta).Call("deleteAiBudget",
		map[string]string{"id": d.Id()}); err != nil {
		return diag.FromErr(err)
	}
	d.SetId("")
	return nil
}

// -- profiles ----------------------------------------------------------

// profileResource builds the generation and serving profile resources,
// which differ only in their commands, response key and parameter set.
func profileResource(kind string, extra map[string]*schema.Schema) *schema.Resource {
	fields := map[string]*schema.Schema{
		"name":        {Type: schema.TypeString, Required: true, ForceNew: true},
		"description": {Type: schema.TypeString, Optional: true},
	}
	for name, field := range extra {
		fields[name] = field
	}

	title := strings.ToUpper(kind[:1]) + kind[1:]
	responseKey := "ai" + kind + "profile"

	params := func(d *schema.ResourceData) map[string]string {
		out := map[string]string{
			"name":        d.Get("name").(string),
			"description": d.Get("description").(string),
		}
		for name := range extra {
			out[strings.ReplaceAll(name, "_", "")] = fmt.Sprint(d.Get(name))
		}
		return out
	}

	return &schema.Resource{
		Description: "An AI " + kind + " profile.",
		Importer:    importByID,
		Schema:      fields,
		CreateContext: func(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
			body, err := clientFrom(meta).Call("createAi"+title+"Profile", params(d))
			if err != nil {
				return diag.FromErr(err)
			}
			d.SetId(rawString(unwrap(body)["id"]))
			return nil
		},
		ReadContext: func(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
			body, err := clientFrom(meta).Call("listAi"+title+"Profiles", nil)
			if err != nil {
				return diag.FromErr(err)
			}
			for _, item := range allItems(body, responseKey) {
				if idOf(item) != d.Id() {
					continue
				}
				return diag.FromErr(setFrom(d, item, map[string]string{
					"name": "name", "description": "description",
				}))
			}
			d.SetId("")
			return nil
		},
		UpdateContext: func(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
			p := params(d)
			p["id"] = d.Id()
			_, err := clientFrom(meta).Call("updateAi"+title+"Profile", p)
			return diag.FromErr(err)
		},
		DeleteContext: func(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
			if _, err := clientFrom(meta).Call("deleteAi"+title+"Profile",
				map[string]string{"id": d.Id()}); err != nil {
				return diag.FromErr(err)
			}
			d.SetId("")
			return nil
		},
	}
}

func resourceAiGenerationProfile() *schema.Resource {
	return profileResource("generation", map[string]*schema.Schema{
		"temperature": {Type: schema.TypeFloat, Optional: true},
		"top_p":       {Type: schema.TypeFloat, Optional: true},
		"max_tokens":  {Type: schema.TypeInt, Optional: true},
		"policy": {Type: schema.TypeString, Optional: true, Default: "default",
			Description: "default, clamp or locked (SPEC.md section 6)."},
	})
}

func resourceAiServingProfile() *schema.Resource {
	// Admin-only (SPEC.md section 7); the plugin enforces that, not this.
	return profileResource("serving", map[string]*schema.Schema{
		"max_num_seqs":            {Type: schema.TypeInt, Optional: true},
		"gpu_memory_utilization":  {Type: schema.TypeFloat, Optional: true},
		"max_model_len":           {Type: schema.TypeInt, Optional: true},
		"tensor_parallel":         {Type: schema.TypeInt, Optional: true},
	})
}

// -- data source -------------------------------------------------------

func dataSourceAiModels() *schema.Resource {
	return &schema.Resource{
		Description: "The AI models this account may use.",
		ReadContext: func(_ context.Context, d *schema.ResourceData, meta any) diag.Diagnostics {
			body, err := clientFrom(meta).Call("listAiModels", nil)
			if err != nil {
				return diag.FromErr(err)
			}
			models := make([]map[string]any, 0)
			for _, item := range allItems(body, "aimodel") {
				models = append(models, map[string]any{
					"id":         fmt.Sprint(item["id"]),
					"name":       fmt.Sprint(item["name"]),
					"task":       fmt.Sprint(item["task"]),
					"price_tier": fmt.Sprint(item["pricetier"]),
				})
			}
			if err := d.Set("models", models); err != nil {
				return diag.FromErr(err)
			}
			d.SetId("ai-models")
			return nil
		},
		Schema: map[string]*schema.Schema{
			"models": {
				Type:     schema.TypeList,
				Computed: true,
				Elem: &schema.Resource{Schema: map[string]*schema.Schema{
					"id":         {Type: schema.TypeString, Computed: true},
					"name":       {Type: schema.TypeString, Computed: true},
					"task":       {Type: schema.TypeString, Computed: true},
					"price_tier": {Type: schema.TypeString, Computed: true},
				}},
			},
		},
	}
}
