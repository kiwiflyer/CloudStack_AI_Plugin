package provider

import (
	"net/http"
	"strings"
	"testing"
)

// The Tier 2 resources (SPEC.md section 8).
//
// These call commands the plugin registers only when
// ai.services.v2.enabled is on. The provider declares them
// unconditionally: Terraform has no way to ask CloudStack what is
// registered before a plan, and a resource that vanished from the
// provider would be a schema error rather than a clear message. So
// using one on a v1 install fails at apply, and the job of this side is
// to make that failure say why.

var V2_RESOURCES = []string{
	"cloudstack_ai_adapter",
	"cloudstack_ai_batch",
	"cloudstack_ai_certificate",
	"cloudstack_ai_private_endpoint",
	"cloudstack_ai_guardrail_profile",
}

func TestEveryV2ResourceExists(t *testing.T) {
	provider := New()
	for _, name := range V2_RESOURCES {
		if _, ok := provider.ResourcesMap[name]; !ok {
			t.Errorf("missing resource %s", name)
		}
	}
}

func TestTheEvaluationsDataSourceExists(t *testing.T) {
	if _, ok := New().DataSourcesMap["cloudstack_ai_evaluations"]; !ok {
		t.Fatal("missing data source cloudstack_ai_evaluations")
	}
}

// -- the v1 install hint -----------------------------------------------

func TestAnUnregisteredV2CommandSaysWhichSettingTurnsItOn(t *testing.T) {
	// What CloudStack answers when a command is not registered. On its
	// own it reads like a provider bug; it is a setting being off.
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(431)
		_, _ = w.Write([]byte(`{"errorresponse":{"errorcode":431,` +
			`"errortext":"Unknown API command: listAiAdapters"}}`))
	})
	defer done()

	d := dataFor(t, resourceAiAdapter(), "a-1", nil)
	diags := readAiAdapter(nil, d, client)

	if !diags.HasError() {
		t.Fatal("an unknown command must be an error")
	}
	if !strings.Contains(diags[0].Summary, "ai.services.v2.enabled") {
		t.Fatalf("the error should name the setting, got %q", diags[0].Summary)
	}
}

func TestAnOrdinaryErrorIsNotDressedUpAsAV2Problem(t *testing.T) {
	// Blaming the setting for a permission error would send an
	// operator to the wrong place entirely.
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(531)
		_, _ = w.Write([]byte(`{"listaiadaptersresponse":{"errorcode":531,` +
			`"errortext":"account not permitted"}}`))
	})
	defer done()

	d := dataFor(t, resourceAiAdapter(), "a-1", nil)
	diags := readAiAdapter(nil, d, client)

	if !diags.HasError() {
		t.Fatal("expected an error")
	}
	if strings.Contains(diags[0].Summary, "ai.services.v2.enabled") {
		t.Fatalf("a permission error is not a v2 problem: %q", diags[0].Summary)
	}
}

// -- adapters ----------------------------------------------------------

func TestAnAdapterReadsBackTheNameARequestAddresses(t *testing.T) {
	// `<base>:<adapter>`. The adapter's own name addresses nothing, so
	// this is the attribute a module actually passes on.
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaiadaptersresponse":{"count":1,"aiadapter":` +
			`[{"id":"a-1","name":"legal-tone","basemodel":"llama-3.3-70b-instruct",` +
			`"servedmodelname":"llama-3.3-70b-instruct:legal-tone","rank":16,` +
			`"sizebytes":1024,"account":"acme"}]}}`))
	})
	defer done()

	d := dataFor(t, resourceAiAdapter(), "a-1", nil)
	if diags := readAiAdapter(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if got := d.Get("served_model_name").(string); got != "llama-3.3-70b-instruct:legal-tone" {
		t.Errorf("served_model_name = %q", got)
	}
	if d.Get("rank").(int) != 16 {
		t.Errorf("rank = %d", d.Get("rank").(int))
	}
}

func TestAMissingAdapterClearsTheId(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaiadaptersresponse":{"count":0}}`))
	})
	defer done()

	d := dataFor(t, resourceAiAdapter(), "a-1", nil)
	if diags := readAiAdapter(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}
	if d.Id() != "" {
		t.Fatalf("id should have been cleared, got %q", d.Id())
	}
}

func TestAnAdapterCannotBeEditedInPlace(t *testing.T) {
	// There is no updateAiAdapter: the artefact is immutable, so
	// changing any of it means uploading another one.
	resource := New().ResourcesMap["cloudstack_ai_adapter"]
	if resource.UpdateContext != nil {
		t.Fatal("an adapter has no update command")
	}
	for _, name := range []string{"name", "base_model", "rank", "uri"} {
		if !resource.Schema[name].ForceNew {
			t.Errorf("%s must force a new adapter", name)
		}
	}
}

// -- batches -----------------------------------------------------------

func TestABatchReadsItsProgress(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaibatchesresponse":{"count":1,"aibatch":` +
			`[{"id":"b-1","model":"llama","status":"running","requestcount":12,` +
			`"account":"acme","outputuri":"s3://out/b-1.jsonl"}]}}`))
	})
	defer done()

	d := dataFor(t, resourceAiBatch(), "b-1", nil)
	if diags := readAiBatch(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if d.Get("status").(string) != "running" {
		t.Errorf("status = %q", d.Get("status"))
	}
	if d.Get("request_count").(int) != 12 {
		t.Errorf("request_count = %d", d.Get("request_count").(int))
	}
	if d.Get("output_uri").(string) == "" {
		t.Error("output_uri is where the results are; it has to be readable")
	}
}

func TestDestroyingABatchCancelsIt(t *testing.T) {
	// A batch is work, not desired state: there is nothing to delete,
	// so destroy means cancel. Requests already run stay billed and
	// their results stay available.
	var called string
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		called = r.URL.Query().Get("command")
		_, _ = w.Write([]byte(`{"cancelaibatchresponse":{"id":"b-1"}}`))
	})
	defer done()

	d := dataFor(t, resourceAiBatch(), "b-1", nil)
	if diags := deleteAiBatch(nil, d, client); diags.HasError() {
		t.Fatalf("delete failed: %v", diags)
	}

	if called != "cancelAiBatch" {
		t.Fatalf("destroy called %q", called)
	}
	if d.Id() != "" {
		t.Fatal("id should have been cleared")
	}
}

func TestABatchCannotBeEditedInPlace(t *testing.T) {
	resource := New().ResourcesMap["cloudstack_ai_batch"]
	if resource.UpdateContext != nil {
		t.Fatal("a submitted batch cannot be changed")
	}
	for _, name := range []string{"model", "input_uri"} {
		if !resource.Schema[name].ForceNew {
			t.Errorf("%s must force a new batch", name)
		}
	}
}

// -- certificates ------------------------------------------------------

func TestTheCertificateKeyIsSensitive(t *testing.T) {
	fields := New().ResourcesMap["cloudstack_ai_certificate"].Schema

	if !fields["private_key"].Sensitive {
		t.Fatal("a private key must be marked sensitive or it prints in plan output")
	}
	if !fields["certificate"].Sensitive {
		// Not secret, but it travels with the key and showing one
		// while hiding the other invites pasting them the wrong way.
		t.Error("the certificate body should be sensitive too")
	}
}

func TestReadingACertificateLeavesTheStoredMaterialAlone(t *testing.T) {
	// The engine returns metadata only — never the body, never the
	// key. Writing what came back over state would empty both.
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaicertificatesresponse":{"count":1,"aicertificate":` +
			`[{"id":"c-1","customdomain":"ai.acme.example","fingerprint":"ab12",` +
			`"expires":"2027-01-01T00:00:00Z","serial":3,"account":"acme"}]}}`))
	})
	defer done()

	d := dataFor(t, resourceAiCertificate(), "c-1", map[string]any{
		"certificate": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----",
		"private_key": "-----BEGIN PRIVATE KEY-----\ny\n-----END PRIVATE KEY-----",
	})

	if diags := readAiCertificate(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if !strings.Contains(d.Get("private_key").(string), "BEGIN PRIVATE KEY") {
		t.Fatal("the stored key was overwritten by a response that never carries it")
	}
	if d.Get("custom_domain").(string) != "ai.acme.example" {
		t.Errorf("custom_domain = %q", d.Get("custom_domain"))
	}
	if d.Get("serial").(int) != 3 {
		t.Errorf("serial = %d", d.Get("serial").(int))
	}
}

func TestReplacingACertificateKeepsTheSameResource(t *testing.T) {
	// replaceAiCertificate exists precisely so a renewal is not a new
	// certificate: every private endpoint pointing at this id keeps
	// pointing at it.
	fields := New().ResourcesMap["cloudstack_ai_certificate"].Schema

	if New().ResourcesMap["cloudstack_ai_certificate"].UpdateContext == nil {
		t.Fatal("a renewal must update in place, not force a new certificate")
	}
	if fields["certificate"].ForceNew || fields["private_key"].ForceNew {
		t.Error("new material is a replacement, not a new resource")
	}
	if !fields["custom_domain"].ForceNew {
		t.Error("a different domain is a different certificate")
	}
}

func TestUpdatingACertificateCallsReplace(t *testing.T) {
	// Every command, not the last one: update replaces and then reads
	// back, so the read would otherwise be all this saw.
	var called []string
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		called = append(called, r.URL.Query().Get("command"))
		_, _ = w.Write([]byte(`{"listaicertificatesresponse":{"count":0}}`))
	})
	defer done()

	d := dataFor(t, resourceAiCertificate(), "c-1", map[string]any{
		"certificate": "x", "private_key": "y",
	})
	_ = updateAiCertificate(nil, d, client)

	if len(called) == 0 || called[0] != "replaceAiCertificate" {
		t.Fatalf("update called %v", called)
	}
}

// -- private endpoints -------------------------------------------------

func TestAPrivateEndpointReadsBackHowItIsReachable(t *testing.T) {
	// `exposure` is the engine's own phrase, and the thing a module
	// author checks to confirm an endpoint really is private.
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaiprivateendpointsresponse":{"count":1,` +
			`"aiprivateendpoint":[{"id":"e-1","deployment":"llama-prod",` +
			`"exposure":"private on net-1 as ai.acme.example","network":"net-1",` +
			`"publicenabled":false,"customdomain":"ai.acme.example","tls":"uploaded",` +
			`"certificate":"c-1","account":"acme"}]}}`))
	})
	defer done()

	d := dataFor(t, resourceAiPrivateEndpoint(), "e-1", nil)
	if diags := readAiPrivateEndpoint(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if !strings.Contains(d.Get("exposure").(string), "private on net-1") {
		t.Errorf("exposure = %q", d.Get("exposure"))
	}
	if d.Get("public_enabled").(bool) {
		t.Error("public_enabled came back true for a private-only endpoint")
	}
	if d.Get("certificate_id").(string) != "c-1" {
		t.Errorf("certificate_id = %q", d.Get("certificate_id"))
	}
}

func TestAMissingPrivateEndpointClearsTheId(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaiprivateendpointsresponse":{"count":0}}`))
	})
	defer done()

	d := dataFor(t, resourceAiPrivateEndpoint(), "e-1", nil)
	if diags := readAiPrivateEndpoint(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}
	if d.Id() != "" {
		t.Fatalf("id should have been cleared, got %q", d.Id())
	}
}

func TestChangingAPrivateEndpointRebuildsIt(t *testing.T) {
	// There is no update command: the proxy VM is built from these at
	// boot, so changing one means a new proxy either way.
	resource := New().ResourcesMap["cloudstack_ai_private_endpoint"]
	if resource.UpdateContext != nil {
		t.Fatal("there is no updateAiPrivateEndpoint")
	}
	for _, name := range []string{
		"deployment", "network_id", "public_enabled", "custom_domain",
		"tls", "certificate_id",
	} {
		if !resource.Schema[name].ForceNew {
			t.Errorf("%s must force a new endpoint", name)
		}
	}
}

// -- guardrail profiles ------------------------------------------------

func TestAGuardrailProfileReadsBackItsScope(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaiguardrailprofilesresponse":{"count":1,` +
			`"aiguardrailprofile":[{"id":"g-1","name":"education-safe",` +
			`"scope":"domain","target":"acme"}]}}`))
	})
	defer done()

	d := dataFor(t, resourceAiGuardrailProfile(), "g-1", nil)
	if diags := readAiGuardrailProfile(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	if d.Get("scope").(string) != "domain" {
		t.Errorf("scope = %q", d.Get("scope"))
	}
	if d.Get("name").(string) != "education-safe" {
		t.Errorf("name = %q", d.Get("name"))
	}
}

func TestAGuardrailProfileCanBeRetargetedButNotRescoped(t *testing.T) {
	// updateAiGuardrailProfile takes the target and nothing else.
	fields := New().ResourcesMap["cloudstack_ai_guardrail_profile"].Schema

	if fields["target"].ForceNew {
		t.Error("the target is the one thing update can change")
	}
	for _, name := range []string{"name", "scope", "preset"} {
		if !fields[name].ForceNew {
			t.Errorf("%s must force a new profile", name)
		}
	}
}

// -- evaluations -------------------------------------------------------

func TestTheEvaluationsDataSourceNeedsAModel(t *testing.T) {
	// listAiEvaluations is GET /v1/models/{model_name}/evaluations;
	// there is no model-less form to read.
	field := New().DataSourcesMap["cloudstack_ai_evaluations"].Schema["model"]

	if field == nil || !field.Required {
		t.Fatal("the model is not optional")
	}
}

func TestEvaluationsReportTheGateNotJustTheScore(t *testing.T) {
	client, done := testClient(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"listaievaluationsresponse":{"count":1,` +
			`"aievaluation":[{"id":"ev-1","model":"llama","revision":"abc123",` +
			`"passed":false,"overridden":true,"summary":"refusal rate too high"}]}}`))
	})
	defer done()

	source := dataSourceAiEvaluations()
	d := dataFor(t, source, "", map[string]any{"model": "llama"})
	if diags := source.ReadContext(nil, d, client); diags.HasError() {
		t.Fatalf("read failed: %v", diags)
	}

	rows := d.Get("evaluations").([]any)
	if len(rows) != 1 {
		t.Fatalf("got %d evaluations", len(rows))
	}
	row := rows[0].(map[string]any)
	if row["passed"].(bool) {
		t.Error("passed came back true for a blocked evaluation")
	}
	if !row["overridden"].(bool) {
		// Blocked and blocked-but-published are different facts.
		t.Error("an override has to be visible on its own")
	}
}
