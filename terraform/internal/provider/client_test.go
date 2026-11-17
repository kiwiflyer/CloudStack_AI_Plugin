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
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

// CloudStack signs the lowercased, sorted, URL-encoded query with
// HMAC-SHA1. Getting any part of that wrong produces a 401 that says
// nothing useful, so it is pinned here.
func TestSignatureIsOverTheSortedLowercasedQuery(t *testing.T) {
	values := url.Values{}
	values.Set("command", "listAiModels")
	values.Set("apiKey", "KEY")
	values.Set("response", "json")

	signed := signature(values, "SECRET")
	again := signature(values, "SECRET")

	if signed != again {
		t.Fatalf("signing is not deterministic: %q then %q", signed, again)
	}
	if signed == "" {
		t.Fatal("no signature produced")
	}
}

func TestSignatureChangesWithTheSecret(t *testing.T) {
	values := url.Values{"command": {"listAiModels"}}

	if signature(values, "one") == signature(values, "two") {
		t.Fatal("different secrets produced the same signature")
	}
}

// CloudStack lowercases percent-escapes before signing, so %2C must be
// %2c in the canonical string. This caught us once already.
func TestPercentEscapesAreLowercasedBeforeSigning(t *testing.T) {
	withComma := url.Values{"tags": {"a,b"}}
	canonical := canonicalQuery(withComma)

	if strings.Contains(canonical, "%2C") {
		t.Fatalf("percent-escapes must be lowercased for signing: %q", canonical)
	}
	if !strings.Contains(canonical, "%2c") {
		t.Fatalf("expected a lowercased escape in %q", canonical)
	}
}

func TestTheSignatureIsNotPartOfWhatIsSigned(t *testing.T) {
	values := url.Values{"command": {"listAiModels"}, "signature": {"stale"}}
	canonical := canonicalQuery(values)

	if strings.Contains(canonical, "signature") {
		t.Fatalf("signature must be excluded from the canonical query: %q", canonical)
	}
}

func TestACallSendsTheCommandAndParses(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.FormValue("command"); got != "listAiModels" {
			t.Errorf("command = %q", got)
		}
		if r.FormValue("signature") == "" {
			t.Error("request was not signed")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"listaimodelsresponse":{"count":1,"aimodel":[{"name":"llama"}]}}`))
	}))
	defer server.Close()

	client := &Client{BaseURL: server.URL, APIKey: "k", SecretKey: "s", HTTP: server.Client()}
	body, err := client.Call("listAiModels", nil)
	if err != nil {
		t.Fatalf("call failed: %v", err)
	}

	var parsed struct {
		Response struct {
			Count int `json:"count"`
		} `json:"listaimodelsresponse"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if parsed.Response.Count != 1 {
		t.Fatalf("count = %d", parsed.Response.Count)
	}
}

// CloudStack reports API errors with 4xx/5xx codes of its own, and the
// body carries the only useful text. Swallowing it leaves the operator
// with "500" and nothing else.
func TestAnApiErrorCarriesItsText(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(431)
		_, _ = w.Write([]byte(`{"listaimodelsresponse":{"errorcode":431,"errortext":"unable to find model"}}`))
	}))
	defer server.Close()

	client := &Client{BaseURL: server.URL, APIKey: "k", SecretKey: "s", HTTP: server.Client()}
	_, err := client.Call("listAiModels", nil)

	if err == nil {
		t.Fatal("expected an error")
	}
	if !strings.Contains(err.Error(), "unable to find model") {
		t.Fatalf("error lost its text: %v", err)
	}
}

func TestAnAsyncCallPollsUntilTheJobFinishes(t *testing.T) {
	polls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.FormValue("command") {
		case "deployAiModel":
			_, _ = w.Write([]byte(`{"deployaimodelresponse":{"jobid":"job-1"}}`))
		case "queryAsyncJobResult":
			polls++
			if polls < 2 {
				_, _ = w.Write([]byte(`{"queryasyncjobresultresponse":{"jobstatus":0}}`))
				return
			}
			_, _ = w.Write([]byte(`{"queryasyncjobresultresponse":{"jobstatus":1,"jobresult":{"aideployment":{"id":"d-1"}}}}`))
		}
	}))
	defer server.Close()

	client := &Client{BaseURL: server.URL, APIKey: "k", SecretKey: "s", HTTP: server.Client(), PollInterval: 1}
	result, err := client.CallAsync("deployAiModel", nil)

	if err != nil {
		t.Fatalf("async call failed: %v", err)
	}
	if polls < 2 {
		t.Fatalf("expected polling, got %d poll(s)", polls)
	}
	if !strings.Contains(string(result), "d-1") {
		t.Fatalf("job result was lost: %s", result)
	}
}

func TestAFailedJobBecomesAnError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.FormValue("command") {
		case "deployAiModel":
			_, _ = w.Write([]byte(`{"deployaimodelresponse":{"jobid":"job-1"}}`))
		default:
			_, _ = w.Write([]byte(`{"queryasyncjobresultresponse":{"jobstatus":2,` +
				`"jobresult":{"errortext":"no GPU capacity"}}}`))
		}
	}))
	defer server.Close()

	client := &Client{BaseURL: server.URL, APIKey: "k", SecretKey: "s", HTTP: server.Client(), PollInterval: 1}
	_, err := client.CallAsync("deployAiModel", nil)

	if err == nil {
		t.Fatal("a failed job must be an error")
	}
	if !strings.Contains(err.Error(), "no GPU capacity") {
		t.Fatalf("the job's reason was lost: %v", err)
	}
}

// A TLS private key must not travel in the request line.
//
// CloudStack scrubs its API access log with a query-string regex that
// covers password, accesskey and secretkey — and not privatekey. The
// JSON scrubber in the same class does list privatekey, so CloudStack
// classifies the field as a secret; the masking that applies to a GET
// request line simply does not cover it. Anything that records a URL
// — the access log, a reverse proxy, an APM trace, TF_LOG=DEBUG —
// therefore keeps an unencrypted PEM key verbatim.
func TestASecretNeverTravelsInTheRequestLine(t *testing.T) {
	const pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"

	var rawQuery, posted string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rawQuery = r.URL.RawQuery
		_ = r.ParseForm()
		posted = r.PostFormValue("privatekey")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"uploadaicertificateresponse":{}}`))
	}))
	defer server.Close()

	client := &Client{BaseURL: server.URL, APIKey: "k", SecretKey: "s", HTTP: server.Client()}
	if _, err := client.Call("uploadAiCertificate", map[string]string{
		"privatekey": pem,
		"domain":     "ai.example.com",
	}); err != nil {
		t.Fatalf("call failed: %v", err)
	}

	if strings.Contains(rawQuery, "BEGIN") || strings.Contains(rawQuery, "PRIVATE") {
		t.Errorf("the private key reached the request line: %s", rawQuery)
	}
	if posted != pem {
		t.Errorf("the private key did not arrive in the body: %q", posted)
	}
}

func TestTheRequestIsStillSignedOverEveryParameter(t *testing.T) {
	// The signature is computed over the canonical query whatever the
	// method, so moving to a form body must not change what is signed.
	var form url.Values
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		form = r.PostForm
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"uploadaicertificateresponse":{}}`))
	}))
	defer server.Close()

	client := &Client{BaseURL: server.URL, APIKey: "k", SecretKey: "s", HTTP: server.Client()}
	if _, err := client.Call("uploadAiCertificate", map[string]string{"privatekey": "x"}); err != nil {
		t.Fatalf("call failed: %v", err)
	}

	signed := url.Values{}
	for key, value := range form {
		if key != "signature" {
			signed[key] = value
		}
	}
	if got := form.Get("signature"); got != signature(signed, "s") {
		t.Errorf("signature = %q, not computed over the sent parameters", got)
	}
}

// A created object's id lives one level below the envelope.
//
// CloudStack answers a create with
// {"createaiapikeyresponse": {"aiapikey": {"id": …}}}, so unwrap()
// lands on {"aiapikey": …} and "id" is not there. Five resources read
// it at that level, set an empty id, and Terraform reported
//
//	Provider produced inconsistent result after apply … Root object
//	was present, but now absent.
//
// immediately after creating the object — found by the first
// acceptance run against a live management server.
func TestCreatedIDReadsThroughTheObjectName(t *testing.T) {
	body := []byte(`{"createaiapikeyresponse":{"aiapikey":{"id":"2f21ef0e50f477f2","name":"k"}}}`)

	if got := createdID(body); got != "2f21ef0e50f477f2" {
		t.Fatalf("createdID = %q, want the nested id", got)
	}
}

func TestCreatedIDStillReadsAFlatResponse(t *testing.T) {
	body := []byte(`{"createaithingresponse":{"id":"flat-id"}}`)

	if got := createdID(body); got != "flat-id" {
		t.Fatalf("createdID = %q, want flat-id", got)
	}
}

func TestCreatedIDIsEmptyWhenThereIsNone(t *testing.T) {
	if got := createdID([]byte(`{"createaithingresponse":{"warning":"none"}}`)); got != "" {
		t.Fatalf("createdID = %q, want empty", got)
	}
}
