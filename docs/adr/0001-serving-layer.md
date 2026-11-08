# ADR-0001: Serving layer

**Status:** Accepted
**Date:** 2026-11-07
**Deciders:** Simon Weller
**Resolves:** SPEC.md section 14, "Serving layer: KServe vs the vLLM production stack (or llm-d)"
**Blocks:** Milestone 3 (`k8s` rendering, one model end to end)

## Context

SPEC.md section 4 says the `k8s` module "renders KServe `InferenceService` or vLLM Deployment
from Jinja templates with serving profile arguments; server-side apply". The choice was left
open. Milestone 3 cannot start without it.

The landscape moved since the spec was written, and the three options are no longer peers:

- **llm-d** is an orchestration layer *on top of* vLLM, not an alternative to it. It reached
  1.0 in October 2026 and is a CNCF **Sandbox** project (accepted March 2026).
- **KServe's `LLMInferenceService`** is itself built on llm-d. So "KServe vs llm-d" is largely
  a false choice: picking `LLMInferenceService` means adopting llm-d underneath. KServe is
  CNCF **Incubating**; `LLMInferenceService` is still `serving.kserve.io/v1alpha1`.
- **KServe's older `InferenceService`** (`v1beta1`, stable) gained vLLM as a first-class
  runtime in v0.20, August 2026.
- **The vLLM production stack** is the oldest of the three and vLLM-only by design.

### Forces specific to this platform

These constraints, all already in SPEC.md, do most of the work:

1. **"Multi-node tensor parallelism" is an explicit non-goal; a replica is capped at one 8-GPU
   host** (section 1). This removes the entire reason llm-d exists: disaggregated
   prefill/decode, expert parallelism across nodes, and RDMA KV-cache transfer are all
   multi-node features.
2. **We already own routing.** The gateway (section 4) enforces key scopes, profile merging
   and clamping, quotas and budgets, and records effective parameters per request for
   metering. llm-d's EPP scheduler would be a *second* routing layer, splitting enforcement
   and metering across two places.
3. **We already own autoscaling.** `autoscale` (section 4) scales on
   `vllm:num_requests_waiting`, TTFT p95 and DCGM utilisation from Prometheus.
4. **We already own rollouts.** `lifecycle` (section 4) does blue/green with canary weights,
   drain, and template upgrades.
5. **Three engines are in scope.** `engine: vllm | sglang | trtllm` (section 3).
6. **Weights are pre-staged on node-local NVMe** by a cache DaemonSet, targeting cold start
   under three minutes for a 70B FP8 model (section 11). Milestone 2's cloud-init already
   mounts that cache at `/var/lib/aisvc/weights`.
7. **CKS is vanilla upstream Kubernetes.** Nothing from OpenShift is available, and every
   dependency is ours to install and keep running.

## Decision

**Serve models as plain Kubernetes `Deployment`s running the engine's own OpenAI-compatible
server, rendered from Jinja templates by the `k8s` module. Do not adopt KServe, llm-d or the
vLLM production stack for v1.**

Blue/green canary weighting happens at **our gateway**, not in the serving layer.

Keep rendering behind the Jinja templates the spec already calls for, so a later move to
`LLMInferenceService` changes templates and one module rather than the architecture.

## Options considered

### Option A: Plain Deployments rendered by `k8s` (chosen)

| Dimension | Assessment |
| --- | --- |
| Complexity | Low — a Deployment, Service, PDB and anti-affinity rules we fully control |
| New cluster dependencies | None |
| Scalability | Enough: a replica is one host by design, and replica count is ours to scale |
| Team familiarity | High — ordinary Kubernetes objects |
| Multi-engine | Straightforward; the engine differs by image and args |

**Pros**
- No new control plane to install, version, secure or debug on CKS.
- The NVMe weight cache is a `hostPath` mount, which is exactly what section 11 describes.
- Serving profile parameters map directly onto container args, which is the whole mechanism
  section 6 specifies; nothing has to be translated through a CRD's schema.
- One routing layer, so enforcement and metering stay in one place.
- Works identically for vLLM, SGLang and TensorRT-LLM.

**Cons**
- Blue/green, readiness gating and rollback are ours to write. (`lifecycle` owns these anyway.)
- No scale-to-zero out of the box. We do not want it: these are passthrough GPU hosts, and the
  spec's floor is two replicas per production model.
- We forgo a community-maintained CRD and the operational conventions that come with it.

### Option B: KServe `InferenceService` (v1beta1) with the vLLM runtime

| Dimension | Assessment |
| --- | --- |
| Complexity | Medium in RawDeployment, high in Serverless |
| New cluster dependencies | KServe; plus Knative and Istio/Kourier for Serverless |
| Scalability | Good |
| Team familiarity | Medium |
| Multi-engine | Partial — runtime support varies by engine |

**Pros**
- CNCF Incubating with a stable `v1beta1` CRD, and vLLM first-class since v0.20.
- `storageUri` handles pulling model artifacts, including from S3 and OCI.
- Readiness gating, traffic splitting and a well-understood operational shape.

**Cons — and this is what decided it**
- **Canary does not work in RawDeployment upstream.** `canaryTrafficPercent` is silently
  ignored and a full rollout happens instead ([kserve#5335]). Weighted splitting via Gateway
  API HTTPRoute is still an open feature request. Red Hat's OpenShift AI build has a
  downstream `canary` field using OpenShift Routes; we are not on OpenShift.
- Getting canary therefore means **Serverless mode: Knative plus Istio or Kourier on CKS** —
  a large dependency surface for one feature we are already building in `lifecycle`.
- `storageUri` pulls per pod, which works against the node-local NVMe cache that the
  three-minute cold-start target depends on.
- Duplicates `autoscale` and `lifecycle` rather than complementing them.

### Option C: KServe `LLMInferenceService` (llm-d underneath)

| Dimension | Assessment |
| --- | --- |
| Complexity | High |
| New cluster dependencies | KServe, Gateway API, Inference Extension, EPP scheduler, LeaderWorkerSet, WVA |
| Scalability | Excellent, at multi-node scale we have ruled out |
| Team familiarity | Low |
| Multi-engine | vLLM-centric |

**Pros**
- The strongest option on paper: KV-cache-aware routing, prefill/decode disaggregation,
  multi-node and inference-aware autoscaling.
- Where the ecosystem is heading; Red Hat, IBM and Google are behind it, and at least one
  large operator runs it in production.

**Cons**
- Every headline feature is a **multi-node** feature, and multi-node is our non-goal.
- `v1alpha1` API on a CNCF **Sandbox** dependency; reviewers consistently warn of API and
  Helm chart churn between minor versions.
- The EPP scheduler is a second routing layer competing with our gateway for the decisions
  that carry our enforcement and metering.
- The largest new-dependency surface of the three, on a vanilla CKS cluster.

### Option D: vLLM production stack

| Dimension | Assessment |
| --- | --- |
| Complexity | Medium |
| New cluster dependencies | Its Helm stack |
| Scalability | Good |
| Team familiarity | Low |
| Multi-engine | **None — vLLM only** |

**Pros:** Purpose-built for vLLM, with KV-cache sharing, prefix-aware routing and good
observability.
**Cons:** vLLM-only conflicts directly with `engine: vllm | sglang | trtllm`. It also carries
its own router and autoscaling, duplicating our gateway and `autoscale`, and it has the least
momentum of the three as attention moved to llm-d.

## Trade-off analysis

The honest summary is that **this platform is already a serving control plane.** SPEC.md
assigns routing, enforcement, metering, autoscaling and rollouts to modules we own. Every
candidate brings its own version of three or four of those. The question is not "build versus
buy" but "which layer owns each job" — and the spec answered that before this ADR.

Given that, the remaining case for KServe was the platform mechanics we would otherwise write:
canary and readiness gating. That case collapsed on inspection: upstream canary needs Knative
and Istio, which is a disproportionate dependency for one feature, on a cluster where we
install and operate everything ourselves.

The case for llm-d is real but **premature rather than wrong**. If the replica-per-host cap is
ever lifted, or prefill/decode disaggregation becomes necessary to hit margin targets, llm-d
becomes the obvious answer. Nothing here forecloses it.

The risk accepted is that we write and maintain blue/green rollout logic ourselves. That risk
is bounded: it is `lifecycle`, it is already in scope for milestone 7, and section 14 has a
precise acceptance criterion for it.

## Consequences

**Easier**
- Milestone 3 needs no new cluster components, so it can start now.
- Serving profile parameters go straight onto the container command line; the allow-list in
  `aisvc/spec/engines.py` is already the right shape.
- The NVMe weight cache works as designed, via `hostPath`.
- Adding SGLang or TensorRT-LLM is a template and an allow-list, not a runtime integration.
- One routing layer, so metering reconciles against one source (section 14 wants usage within
  1% of gateway token counts).

**Harder**
- `lifecycle` must implement blue/green with canary weights, readiness gating, promote and
  rollback. Milestone 7, with the gateway holding the weights.
- No community-maintained CRD, so pod templating, PDBs and anti-affinity are ours.
- We should track llm-d; drifting far from its conventions would make a later move costlier.

**To revisit**
- If multi-node tensor parallelism stops being a non-goal, reopen this ADR — that inverts the
  decision.
- If prefill/decode disaggregation is needed for margin on long-context workloads.
- When `LLMInferenceService` reaches `v1beta1` and llm-d leaves Sandbox, the cost side of this
  comparison changes.

## Action items

1. [ ] Milestone 3: render `Deployment` + `Service` + `PodDisruptionBudget` from Jinja in
       `aisvc/k8s/`, with server-side apply.
2. [ ] Pod spec: host anti-affinity across replicas, `nvidia.com/gpu` limits from
       `tensor_parallel`, `hostPath` weight cache at `/var/lib/aisvc/weights`, node selector on
       the `aisvc.io/node-pool` label milestone 2's cloud-init already applies.
3. [ ] Map serving profile parameters onto engine args per `aisvc/spec/engines.py`; a profile
       change rolls out blue/green (section 6).
4. [ ] Readiness and liveness on the engine's `/health`, with `startupProbe` tolerating the
       three-minute cold start.
5. [ ] Gateway must support weighted routing between blue and green backends — a requirement
       for ADR-0002 (gateway choice).
6. [ ] Record the engine image and version per deployment so fleet health can spot drift.
7. [ ] Re-evaluate llm-d at v2 planning, or sooner if the multi-node non-goal changes.

## Sources

- [Understanding LLMInferenceService](https://kserve.github.io/website/docs/model-serving/generative-inference/llmisvc/llmisvc-overview) — KServe docs: `LLMInferenceService` is built on llm-d; `v1alpha1`
- [KServe v0.20.0](https://cloudnativepulse.com/kserve-v0-20-0-the-release-that-turns-model-serving-into-an-llm-inference-platform/) — vLLM as a native runtime, August 2026
- [Announcing KServe v0.18](https://kserve.github.io/website/blog/kserve-0.18-release) — multi-node without Ray, llm-d v0.6
- [kserve#5335][kserve#5335] — `canaryTrafficPercent` silently ignored in RawDeployment
- [Canary Rollout Strategy](https://kserve.github.io/website/docs/model-serving/predictive-inference/rollout-strategies/canary) — canary is Serverless-mode only upstream
- [Deploying models, OpenShift AI 3.5](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/deploying_models/deploying_models) — downstream RawDeployment `canary` via OpenShift Routes
- [llm-d, CNCF](https://www.cncf.io/projects/llm-d/) — Sandbox, accepted March 2026
- [llm-d 1.0](https://newsroom.ibm.com/2026-10-20-llm-d-1-0-advances-open,-cost-efficient-ai-inference-for-production-scale) — 1.0, 20 October 2026
- [Demystifying llm-d and vLLM](https://www.redhat.com/en/blog/demystifying-llm-d-and-vllm-race-production) — llm-d orchestrates vLLM; plain vLLM suits single-node
- [Combining KServe and llm-d](https://developers.redhat.com/articles/2026/04/21/kserve-llm-d-optimized-gen-ai-inference) — KV-cache-aware routing benchmarks
- [vLLM production stack](https://researcher.ibm.com/publications/scalable-and-efficient-llm-serving-with-the-vllm-production-stack) — scope and design

[kserve#5335]: https://github.com/kserve/kserve/issues/5335
