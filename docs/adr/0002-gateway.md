# ADR-0002: Gateway

**Status:** Accepted
**Date:** 2026-11-07
**Deciders:** Simon Weller
**Resolves:** SPEC.md section 14, "Gateway: LiteLLM vs Envoy AI Gateway for multi-tenant routing, enforcement and metering at scale"
**Constrained by:** [ADR-0001](0001-serving-layer.md) — canary weighting lives at the gateway
**Blocks:** Milestone 3 (`gateway` with hooks, one model end to end)

## Context

The gateway is the tenant-facing surface. SPEC.md gives it more jobs than any other component:

- **Routing** to deployments, and — since [ADR-0001](0001-serving-layer.md) — weighted routing
  between blue and green backends during a canary rollout.
- **Key scopes** (section 7): allowed models, expiry, IP CIDR allow-list, per-key rate limit,
  optional key-level profiles. Out-of-scope requests get 403 with a clear reason.
- **Generation profile enforcement** (section 6): resolve seven precedence levels, apply
  `default` / `clamp` / `locked` policies, and rewrite the request accordingly.
- **Quotas and budgets** (section 7): alert at 50% and 80%, alert or block at 100%.
- **Metering** (section 11): effective parameters and token counts per request, never prompt
  or output content. Section 14 requires monthly usage to reconcile with gateway token counts
  within 1%.
- **Guardrails** (v2, section 8): pre- and post-call checks against a guard model, PII
  redaction, under 150 ms added at p95 — which means rewriting response bodies, including
  streamed ones.
- **v2 surface**: `/v1/embeddings`, rerank, `/v1/files`, `/v1/batches`, and LoRA adapters
  exposed as `<base>:<adapter>`.

Since the spec was written, the Envoy option changed name and owner: **Envoy AI Gateway
reached v1.0 in June 2026, then in September 2026 left the CNCF/Envoy sub-project structure
and became "Agent Router" under the Agentic AI Foundation**, a Linux Foundation entity. Same
code, maintainers, licence and CRD names; it is at 1.2 as of October 2026.

## Decision

**Use LiteLLM Proxy**, with every enforcement rule implemented as LiteLLM hooks in
`gateway-hooks/` that **import `aisvc.profiles` directly** rather than reimplementing it.

Run it as multiple replicas behind the zone's load balancer, with Redis for shared rate-limit
state and the Postgres instance we already operate for keys and spend. Pin the LiteLLM version
and upgrade deliberately.

## Options considered

### Option A: LiteLLM Proxy (chosen)

| Dimension | Assessment |
| --- | --- |
| Complexity | Low — one Python service, hooks in our own language |
| New cluster dependencies | LiteLLM, Redis (Postgres already present) |
| Throughput per instance | A few hundred RPS, reportedly degrading around 500 |
| Team familiarity | High — the engine is Python 3.12, mypy strict |
| Fit to our enforcement model | Very high |

**Pros**
- **One implementation of profile precedence.** `async_pre_call_hook` receives the request and
  returns a modified one, so the hook can call the same `aisvc.profiles.resolve()` that
  `getAiEffectiveParameters` calls. This matters more than anything else here: section 6's
  seven-level precedence with `locked` beating every level below it is the most
  correctness-critical logic in the platform, section 12 asks for 90% branch coverage on it,
  and section 14 requires the effective-parameters view to match what was actually applied.
  One implementation cannot disagree with itself.
- Hooks cover the whole v2 surface too: `async_post_call_streaming_iterator_hook` can rewrite
  streamed responses, which is what guardrail redaction needs.
- Virtual keys, budgets, RPM/TPM limits and spend tracking exist already, backed by Postgres
  and Redis.
- The OpenAI-compatible surface section 10 lists — chat, completions, models, and the v2
  embeddings, files and batches endpoints — is there out of the box.
- Weighted routing between deployments in a model group satisfies ADR-0001's canary
  requirement.
- Testable against the mock model server with no GPU (section 12).
- SPEC.md already assumes it, in section 4 and in the `gateway-hooks/` directory.

**Cons**
- **Split licence.** The repository puts `enterprise/` under a commercial BerriAI licence and
  everything outside it under MIT. We must stay on the MIT side: no imports from
  `enterprise/`, no licence-key-gated features. Everything we need — virtual keys, budgets,
  RPM/TPM limits, spend tracking, custom hooks — is in the MIT core, and the enterprise tier
  is SSO, RBAC and support, which CloudStack already provides. See
  [docs/third-party-licenses.md](../third-party-licenses.md).
- Python throughput. One report has proxied throughput falling from ~16 to ~9 req/s on a
  4-vCPU VM; another describes a 500M TPM target sustaining nearer 150M, with one model's
  throughput squeezed when another's grew — shared worker, Redis and Postgres contention.
- Regressions between versions and changed hook signatures are reported anecdotally.
- Open-source tier lacks per-tenant SSO and fine-grained RBAC. **Does not apply to us:**
  CloudStack owns auth, RBAC and roles (section 9); the gateway only sees a key.

### Option B: Agent Router, formerly Envoy AI Gateway

| Dimension | Assessment |
| --- | --- |
| Complexity | High — a second service in a second language, or a self-defeating one |
| New cluster dependencies | Envoy Gateway, AI Gateway controller, Rate Limit Service |
| Throughput per instance | Far higher; Envoy is C++ |
| Team familiarity | Low |
| Fit to our enforcement model | Poor |

**Pros**
- An Envoy data plane: the per-instance throughput and tail latency are in a different class.
- v1.0 GA June 2026 with a stability guarantee on the `v1beta1` control-plane API; Bloomberg
  reports running it in production; v1.1 added token counting across providers.
- Weighted routing comes free from Gateway API `HTTPRoute` `backendRefs`.
- `EnvoyExtensionPolicy` with `extProc` is a real, documented extension point that can inspect
  and modify request and response bodies.

**Cons**
- **Our enforcement logic becomes a separate gRPC ext_proc service, and both ways of building
  it are bad.** In Python, every request round-trips into a Python process anyway, which gives
  back the throughput we adopted Envoy to get. In Go, profile precedence is implemented
  *twice* — once in `aisvc/profiles` for `getAiEffectiveParameters`, once in the ext_proc
  service for enforcement — and those two must agree forever, across every `clamp` and
  `locked` edge case. That is precisely the drift section 14's acceptance criterion is
  designed to catch.
- The largest dependency surface of the two, on a vanilla CKS cluster we install and operate
  ourselves. ADR-0001 rejected Knative and Istio on exactly this ground; choosing Envoy
  Gateway here would be inconsistent.
- Governance churn: out of CNCF, into a young foundation, renamed, within the last two months.
  Not disqualifying — code, maintainers and licence are unchanged — but it is a young project
  under new stewardship, and the spec's own name for it is already stale.
- Streamed response body mutation through ext_proc is substantially more work than a Python
  iterator hook, and v2's guardrails need it.

## Trade-off analysis

Section 14 framed this as "…for multi-tenant routing, enforcement and metering **at scale**",
and scale is the only dimension where Envoy clearly wins. So the question is whether we are
actually at that scale — and the arithmetic says no, by an order of magnitude.

**The GPU fleet saturates long before the gateway does.** A 70B FP8 replica on two H100s
serves tens of concurrent streams, each running for seconds. Two replicas complete something
like 4–6 requests per second; ten replicas, perhaps 30. The spec's own example tenant quota is
`rpm: 600`, which is 10 requests per second. LiteLLM's reported few-hundred-RPS ceiling sits
roughly an order of magnitude above anything our own GPU capacity can absorb, and we would add
GPU nodes — the expensive part — long before the gateway became the constraint.

That reframes the comparison. Envoy's advantage is real but is spent on a bottleneck we do not
have, while its cost — a second language or a self-defeating Python sidecar, plus a controller
and a rate-limit service — is paid immediately and permanently, on the most correctness-critical
logic in the platform.

The honest risks on the LiteLLM side are operational rather than architectural: version
regressions, and shared-resource contention between models on one instance. Both are
manageable — pin the version, test the hooks against it, run replicas, keep rate-limit state in
Redis — and neither compounds the way a duplicated precedence engine would.

A note on where this *would* flip: the reported LiteLLM pain comes from deployments fronting
**hosted** provider APIs, where one proxy multiplexes enormous token volumes it does not
itself compute. If this platform ever fronts third-party hosted models, the GPU-bound argument
disappears and this ADR should be reopened.

## Consequences

**Easier**
- Milestone 3 writes hooks in the language the rest of the engine is written in, importing
  `aisvc.profiles` rather than reimplementing it.
- Keys, budgets and spend tracking are largely configuration, against Postgres and Redis.
- v2 guardrails, LoRA aliasing and batch all extend the same hook surface.
- Enforcement is testable with the mock model server, no GPU needed.

**Harder**
- The gateway tier must be horizontally scaled and capacity-planned; a single instance is not
  the answer. Rate-limit state has to be in Redis or per-instance limits will be wrong.
- LiteLLM upgrades need care: pin the version, and keep contract tests on the hook signatures
  so a changed signature fails in CI rather than in production.
- We inherit LiteLLM's behaviour on content logging. Prompt and output logging must be
  **off by default** (section 11) and verified by a test, not assumed from configuration.

**To revisit**
- If one zone's gateway tier needs more than ~200 RPS sustained, or gateway CPU becomes the
  thing we scale rather than GPU nodes.
- If the platform starts fronting third-party hosted models.
- If guardrail post-processing cannot hold the 150 ms p95 budget in Python (section 8).

## Action items

*Reviewed 2026-11-18. Items 1, 2, 3 and 6 are done; 4, 5 and 7 are not, and
item 4 is the one that matters — **LiteLLM is still not a dependency.** It
appears in `pyproject.toml` only as a mypy override, so nothing pins a version,
nothing contract-tests the hook signatures, and the hooks in `gateway-hooks/`
have never run inside a LiteLLM process. The enforcement logic they call is
tested; the integration they exist for is not.*

1. [x] Milestone 3: `gateway-hooks/` with `async_pre_call_hook` applying resolved generation
       parameters, key scopes (model allow-list, CIDR, expiry) and quota checks; 403 with a
       reason naming which scope failed.
2. [x] The hook imports `aisvc.profiles.resolve()`. No second implementation of precedence —
       add a test asserting the hook and `getAiEffectiveParameters` agree for the same inputs.
3. [x] Generate LiteLLM config from `platform.yaml` in the `gateway` module, with weighted
       entries per model group so `lifecycle` can shift canary weight.
4. [ ] Pin the LiteLLM version in `pyproject.toml`; contract-test the hook signatures so an
       upgrade that changes them fails in CI. Re-check the MIT/`enterprise/` licence split on
       every upgrade, and add LiteLLM to `docs/third-party-licenses.md` when it lands.
5. [ ] Redis for shared rate-limit and budget state across replicas; at least two replicas
       behind the zone load balancer.
6. [x] Assert in a test that no prompt or completion content reaches logs or metering by
       default (section 11).
7. [ ] Load test through the gateway against the mock model server, and record the measured
       per-instance ceiling in `PROGRESS.md` so the revisit trigger above has a real number
       behind it rather than a reported one.

## Sources

- [Modify / Reject Incoming Requests](https://docs.litellm.ai/docs/proxy/call_hooks) — `async_pre_call_hook` modifies request data; streaming iterator hook for responses
- [LiteLLM overhead in production, BerriAI/litellm#21046](https://github.com/BerriAI/litellm/issues/21046) — ~16 to ~9 req/s through the proxy on 4 vCPU
- [Scaling LiteLLM Proxy to 500M TPM, BerriAI/litellm#38081](https://github.com/BerriAI/litellm/issues/38081) — ~150M TPM sustained; cross-model contention
- [LiteLLM review 2026](https://www.truefoundry.com/blog/a-detailed-litellm-review-features-pricing-pros-and-cons-2026) — governance gaps, version regressions (vendor-authored)
- [Envoy AI Gateway v1.0 GA](https://tetrate.io/blog/envoy-ai-gateway-v1-0-release) — June 2026, API stability, Bloomberg in production
- [Envoy AI Gateway is becoming Agent Router](https://theagentrouter.ai/blog/envoy-ai-gateway-is-now-agent-router/) — rename and move to the Agentic AI Foundation
- [Agent Router, AAIF](https://aaif.io/projects/agent-router) — project page under the Linux Foundation
- [Envoy AI Gateway / Agent Router review](https://www.truefoundry.com/blog/envoy-ai-gateway-review) — September 2026 rename; required components (vendor-authored)
- [Envoy Gateway: External Processing](https://gateway.envoyproxy.io/docs/tasks/extensibility/ext-proc) — `EnvoyExtensionPolicy` with `extProc`, gRPC service, body modes
- [EnvoyExtensionPolicy design](https://gateway.envoyproxy.io/community/design/envoy-extension-policy/) — attachment to Gateway or HTTPRoute
