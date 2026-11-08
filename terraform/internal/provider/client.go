// Package provider implements the AI Services resources for the
// CloudStack Terraform provider (SPEC.md section 7).
//
// The resources are built on the native API commands, not on the engine:
// the engine is unreachable from anywhere a Terraform run would execute,
// and the plugin is what enforces auth, RBAC and ownership. So this
// speaks plain CloudStack API, signed the usual way.
package provider

import (
	"crypto/hmac"
	"crypto/sha1"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

// Client makes signed CloudStack API calls.
//
// The CloudStack Go SDK is not used for these commands: it is generated
// from upstream's command set and knows nothing of ours. Signing is a
// dozen lines, so a thin client beats carrying a fork of the SDK.
type Client struct {
	BaseURL   string
	APIKey    string
	SecretKey string
	HTTP      *http.Client

	// PollInterval is in milliseconds; tests set it low.
	PollInterval int
	// PollTimeout is in seconds. Zero means the default.
	PollTimeout int
}

const (
	defaultPollInterval = 2000
	defaultPollTimeout  = 1800

	jobInProgress = 0
	jobSucceeded  = 1
	jobFailed     = 2
)

// canonicalQuery builds the string CloudStack signs.
//
// Sorted by key, lowercased whole, and — the part that is easy to miss —
// with percent-escapes lowercased too, because the lowercasing happens
// after encoding. A signature over %2C instead of %2c is rejected with a
// bare 401.
func canonicalQuery(values url.Values) string {
	keys := make([]string, 0, len(values))
	for key := range values {
		if key == "signature" {
			continue
		}
		keys = append(keys, key)
	}
	sort.Strings(keys)

	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		parts = append(parts, url.QueryEscape(key)+"="+url.QueryEscape(values.Get(key)))
	}
	return strings.ToLower(strings.Join(parts, "&"))
}

func signature(values url.Values, secret string) string {
	mac := hmac.New(sha1.New, []byte(secret))
	mac.Write([]byte(canonicalQuery(values)))
	return base64.StdEncoding.EncodeToString(mac.Sum(nil))
}

func (c *Client) httpClient() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return &http.Client{Timeout: 60 * time.Second}
}

// Call makes one synchronous API call and returns the raw body.
func (c *Client) Call(command string, params map[string]string) ([]byte, error) {
	values := url.Values{}
	for key, value := range params {
		if value != "" {
			values.Set(key, value)
		}
	}
	values.Set("command", command)
	values.Set("response", "json")
	values.Set("apiKey", c.APIKey)
	values.Set("signature", signature(values, c.SecretKey))

	endpoint := strings.TrimRight(c.BaseURL, "/") + "?" + values.Encode()
	response, err := c.httpClient().Get(endpoint)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", command, err)
	}
	defer response.Body.Close()

	body, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, fmt.Errorf("%s: reading response: %w", command, err)
	}

	// CloudStack signals API errors with status codes of its own (530,
	// 431 and friends). The body is where the reason is, so it is read
	// before the status is judged.
	if text := errorTextIn(body); text != "" {
		return nil, fmt.Errorf("%s: %s", command, text)
	}
	if response.StatusCode >= 400 {
		return nil, fmt.Errorf("%s: HTTP %d", command, response.StatusCode)
	}
	return body, nil
}

// CallAsync makes an async call and waits for its job.
func (c *Client) CallAsync(command string, params map[string]string) ([]byte, error) {
	body, err := c.Call(command, params)
	if err != nil {
		return nil, err
	}

	jobID := stringField(body, "jobid")
	if jobID == "" {
		// Not every call the plugin marks async returns a job: a
		// no-op update may answer directly. That is not an error.
		return body, nil
	}
	return c.waitForJob(command, jobID)
}

func (c *Client) waitForJob(command, jobID string) ([]byte, error) {
	interval := c.PollInterval
	if interval <= 0 {
		interval = defaultPollInterval
	}
	timeout := c.PollTimeout
	if timeout <= 0 {
		timeout = defaultPollTimeout
	}

	deadline := time.Now().Add(time.Duration(timeout) * time.Second)
	for {
		body, err := c.Call("queryAsyncJobResult", map[string]string{"jobid": jobID})
		if err != nil {
			return nil, err
		}

		switch jobStatusIn(body) {
		case jobSucceeded:
			return jobResultIn(body), nil
		case jobFailed:
			reason := errorTextIn(jobResultIn(body))
			if reason == "" {
				reason = "the job failed without saying why"
			}
			return nil, fmt.Errorf("%s: %s", command, reason)
		}

		if time.Now().After(deadline) {
			return nil, fmt.Errorf("%s: job %s did not finish within %ds", command, jobID, timeout)
		}
		time.Sleep(time.Duration(interval) * time.Millisecond)
	}
}

// unwrap returns the single response envelope CloudStack wraps replies
// in, whatever it happens to be called.
func unwrap(body []byte) map[string]json.RawMessage {
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil
	}
	for _, inner := range envelope {
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(inner, &fields); err == nil {
			return fields
		}
	}
	return nil
}

func errorTextIn(body []byte) string {
	fields := unwrap(body)
	if fields == nil {
		// A job result is the bare object, not an envelope.
		if err := json.Unmarshal(body, &fields); err != nil {
			return ""
		}
	}
	if raw, ok := fields["errortext"]; ok {
		var text string
		if json.Unmarshal(raw, &text) == nil {
			return text
		}
	}
	return ""
}

func jobStatusIn(body []byte) int {
	fields := unwrap(body)
	raw, ok := fields["jobstatus"]
	if !ok {
		return jobInProgress
	}
	var status int
	if json.Unmarshal(raw, &status) != nil {
		return jobInProgress
	}
	return status
}

func jobResultIn(body []byte) []byte {
	fields := unwrap(body)
	if raw, ok := fields["jobresult"]; ok {
		return raw
	}
	return body
}

// stringField pulls one string out of a response envelope.
func stringField(body []byte, name string) string {
	fields := unwrap(body)
	raw, ok := fields[name]
	if !ok {
		return ""
	}
	var value string
	if json.Unmarshal(raw, &value) == nil {
		return value
	}
	return ""
}

// rawString decodes one JSON string field, or "" when it is absent.
func rawString(raw json.RawMessage) string {
	if raw == nil {
		return ""
	}
	var value string
	if json.Unmarshal(raw, &value) == nil {
		return value
	}
	return ""
}

// unwrapObject pulls a single named object out of a response, for the
// async replies that return the object bare rather than in a list.
func unwrapObject(body []byte, key string) map[string]any {
	fields := unwrap(body)
	raw, ok := fields[key]
	if !ok {
		if err := json.Unmarshal(body, &fields); err != nil {
			return nil
		}
		if raw, ok = fields[key]; !ok {
			return nil
		}
	}
	var item map[string]any
	if json.Unmarshal(raw, &item) != nil {
		return nil
	}
	return item
}
