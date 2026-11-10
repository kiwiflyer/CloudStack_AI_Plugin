# CloudStack AI Services — Technical Specification

Nov 7, 2026 · @Simon

## 1. Overview

Build "CloudStack AI Services": a native Apache CloudStack plugin, backed by a Python engine, that lets providers source open-weight LLMs from Hugging Face, deploy them on NVIDIA GPU capacity, tune them through profiles, and sell them to tenants as OpenAI-compatible endpoints.

**Goals**

- Native experience: a Java API plugin and a Vue section in the standard CloudStack UI are the only user surfaces; everything is also available through CloudMonkey and Terraform.
- Declarative, GitOps-friendly definition of GPU pools, models, profiles, tenants and SLOs in one YAML spec, converged by a reconcile loop.
- Model sourcing from Hugging Face with compatibility checks, approval, immutable mirroring and supply-chain scanning.
- Profile-based tuning of serving, generation and guardrail parameters at domain, account, deployment and key level.
- Multi-tenant metering, budgets and usage records that feed existing CloudStack billing.

**Scope by release**

| Release | Contents |
| --- | --- |
| v1 GA | Core platform (sections 3–6, 9–10), Tier 1 operability (section 7), tests (section 12) |
| v2 | Tier 2 differentiators (section 8): multi-LoRA, embeddings/rerank/vision, batch, guardrails, evaluation gate, private endpoints |

**Non-goals**

- Model training or fine-tuning pipelines (tenants bring LoRA adapters; they are not trained here).
- Multi-node tensor parallelism; a replica is capped at one 8-GPU host.
- Replacing CloudStack scheduling; CloudStack remains the IaaS authority.

**Guiding principles**

- CloudStack supplies GPU capacity; Kubernetes (CKS) serves models; the Python engine reconciles both; the Java plugin is the front door.
- Everything is idempotent and supports `--dry-run`; every action is logged with a plan diff.
- Only the mirror worker reaches the internet; inference nodes load verified artifacts from the internal registry.
- Passthrough GPUs cannot live-migrate, so the system drains and replaces nodes rather than migrating them.

## 2. Architecture

CloudStack supplies GPU capacity, CKS serves the models, and a Python engine reconciles both against the spec while the UI and gateway sit on top.

&#91;embedded content: AI Services architecture · 4 layers\]

The UI never touches CloudStack GPU resources directly: every change goes through the engine, which keeps the spec, the audit log and the actual state in step.

## 3. Declarative platform spec

One YAML file (`platform.yaml`) is the source of truth for the whole platform; it is validated by pydantic v2 models in `aisvc/spec/`, versioned in Git, and updated by the engine through Git commits when changes are made in the UI or API.

```yaml
apiVersion: aisvc/v1
zones:
  - name: zone-east-1
    cloudstack_zone: "East-1"
    cks_cluster: aisvc-east-1
gpuClasses:
  - name: h100-8x
    host_tag: h100-sxm
    service_offering: "GPU-H100-8x"
    gpus_per_node: 8
    gpu_mem_gb: 80
    mode: passthrough          # passthrough | vgpu | mig
  - name: l40s-1x
    host_tag: l40s
    service_offering: "GPU-L40S-1x"
    gpus_per_node: 1
    gpu_mem_gb: 48
    mode: passthrough
nodePools:
  - name: h100-pool
    zone: zone-east-1
    gpu_class: h100-8x
    min_nodes: 1
    max_nodes: 6
    template: "gpu-golden-2026.11"
huggingface:
  endpoint: https://huggingface.co     # or a private mirror / Enterprise Hub
  token_secret: aisvc/hf-token          # org token; accepted gated licenses apply
  allow_patterns: ["*.safetensors", "*.json", "tokenizer*", "*.model"]
  block_pickle: true
  trust_remote_code: false
  require_approval: true
  storage_quota_gb_per_domain: 2000
models:
  - name: llama-3.3-70b-instruct
    source: hf://meta-llama/Llama-3.3-70B-Instruct@<commit-sha>
    license: llama3.3
    task: chat                 # chat | embedding | rerank | vision-chat
    engine: vllm               # vllm | sglang | trtllm
    quantization: fp8
    tensor_parallel: 2
    max_model_len: 32768
    node_pool: h100-pool
    replicas: {min: 2, max: 4}
    slo: {ttft_p95_ms: 800, queue_depth_target: 8}
    rollout: {strategy: bluegreen, canary_percent: 10}
    serving_profile: h100-throughput
    concurrency: {working_context_tokens: 1536}   # what max_num_seqs is checked against
    visibility: {domains: ["/Customers"]}
    price_tier: standard
    lora: {enabled: true, max_adapters_per_account: 20}     # v2
    batch: {enabled: true, discount: 0.5}                    # v2
tuningProfiles:
  serving:
    - name: h100-throughput
      params: {gpu_memory_utilization: 0.92, max_num_seqs: 256,
               enable_prefix_caching: true, kv_cache_dtype: fp8,
               enable_lora: true, max_loras: 16, max_lora_rank: 64}
  generation:
    - name: precise
      params:
        temperature: {default: 0.2, min: 0.0, max: 0.5, policy: clamp}
        top_p: {default: 0.9, policy: default}
        max_tokens: {default: 1024, max: 4096, policy: clamp}
        system_prompt_prefix: {value: "Answer concisely.", policy: locked}
  guardrail:                                                  # v2
    - name: education-safe
      checks:
        safety: {action: block, policy: locked}
        pii: {action: redact}
        max_input_tokens: {value: 8000}
assignments:
  - {scope: domain, target: "/Customers/Acme", generation_profile: precise}
  - {scope: deployment, target: llama-3.3-70b-instruct, serving_profile: h100-throughput}
priceTiers:
  - {name: standard, input_per_mtok: 0.60, output_per_mtok: 1.80, currency: USD}
tenants:
  - name: acme
    cloudstack_domain: "/Customers/Acme"
    cloudstack_account: acme
    models: [llama-3.3-70b-instruct]
    quota: {tpm: 500000, rpm: 600, gpu_hours_month: 2000}
    budget: {monthly: 5000, currency: USD, on_exceed: block}
    tier: shared               # shared | dedicated
evaluation:                                                    # v2
  suites: [standard-core, customer-golden]
  max_regression_points: 2
```

Prices in the example are placeholders, not recommendations.

**Validation rules**

- `tensor_parallel` ≤ the referenced class's `gpus_per_node`, and is a power of two.
- `capacity.estimate()` must fit weights + KV cache for `max_model_len` within `tensor_parallel × gpu_mem_gb × 0.9`; otherwise the spec is rejected with the shortfall in GB.
- Serving profile parameters must be on the per-engine allow-list, and the engine's concurrency cap (`max_num_seqs` on vLLM) must fit the class's KV cache budget at the model's declared `concurrency.working_context_tokens`. A cap too small to be given one KV block per sequence is rejected outright. Where no working context is declared, the spec reports the average sequence length at which the cap is reachable instead of failing.

  *Amended Nov 2026, milestone 1.* This rule originally read `max_model_len` × `max_num_seqs`, which treats the cap as a reservation. A paged engine never makes that reservation: KV blocks are allocated per token as sequences grow, and the scheduler queues whatever does not fit. vLLM's own startup assertion is only that the cache hold one `max_model_len` sequence — which is the separate feasibility gate above. Read literally the original rule rejected this document's own example by roughly 16x, so it is expressed against a declared workload instead.
- Generation profile values must respect model limits (for example `max_tokens` ≤ context length).
- Every model's `license` must appear in `licenses.yaml`; `source` must pin a commit SHA.
- A `dedicated` tenant needs its own `nodePools` entry; `shared` tenants reference shared pools only.
- All references (zone, class, pool, model, profile, price tier) must resolve; unknown keys are errors.

## 4. Python control plane

The engine is Python 3.12, fully typed (mypy strict), async where it talks to external APIs; each module exposes a narrow interface so it can be unit-tested against fakes.

| Module | Responsibility | Key interfaces / calls | Release |
| --- | --- | --- | --- |
| `cloudstack` | Signed CloudStack API client with async job polling, retries, pagination | `call(cmd, **params)`, `wait_job(jobid)` | v1 |
| `inventory` | Live GPU capacity per zone and class | `listHosts`, `listVirtualMachines`, `listServiceOfferings` → `CapacitySnapshot` | v1 |
| `capacity` | Sizes models, profiles and pools | `estimate(model, profile) -> GpuRequirement`: weights + KV cache + 10% headroom | v1 |
| `provision` | Creates and removes GPU nodes | `deployVirtualMachine` (offering, template, anti-affinity, cloud-init), `scaleKubernetesCluster`, `destroyVirtualMachine` | v1 |
| `k8s` | Applies serving workloads | Renders a `Deployment`, `Service` and `PodDisruptionBudget` from Jinja templates with serving profile arguments as engine command-line flags; server-side apply ([ADR-0001](docs/adr/0001-serving-layer.md)) | v1 |
| `hf` | Hugging Face search, check, mirror | `huggingface_hub` `list_models`, `model_info`, `snapshot_download` in the mirror Job (section 5) | v1 |
| `registry` | Model artifacts | S3 (boto3) to Ceph RGW/MinIO; manifests, sha256 verification, license and metadata | v1 |
| `profiles` | Profile storage and resolution | `resolve(deployment, account, key) -> EffectiveParams` with source level per value (section 6) | v1 |
| `gateway` | Routes, keys, enforcement | LiteLLM config and custom pre-call hook: key scopes, profile merge and clamp, quotas, budgets | v1 |
| `autoscale` | Replica and node scaling | Prometheus: `vllm:num_requests_waiting`, TTFT p95, DCGM utilization; up in 1 min, down after 15 min | v1 |
| `lifecycle` | Rollouts and maintenance | Blue/green with canary weights; `drain(host)`; template and driver upgrades; auto-quarantine | v1 |
| `metering` | Usage and budgets | Tokens and GPU-hours per tenant hourly; budget thresholds; usage export to the plugin | v1 |
| `setup` | Setup wizard backend | Connection checks, GPU discovery, self-test (section 7) | v1 |
| `playground` | Ephemeral session keys | Mints 15-minute model-scoped keys | v1 |
| `adapters` | LoRA adapters | Upload, validate against base model and rank, register with the gateway | v2 |
| `batch` | Batch inference | JSONL intake, low-priority scheduling, preemption, results | v2 |
| `guardrails` | Safety, PII, topic checks | Guard model client, redaction, block events | v2 |
| `evaluation` | Pre-publish gate | Staging deployment, benchmark suites, regression comparison | v2 |
| `reconciler` | Converges desired to actual | `observe() -> diff() -> plan() -> execute()`; ordered: capacity, models, profiles, routes | v1 |
| `api` | Engine REST service | FastAPI app per section 10; called only by the Java plugin and the CLI | v1 |
| `cli` | Operator entry point | Typer: `aisvc plan`, `apply`, `status`, `drain <host>`, `mirror <repo>`, `effective <deployment>` | v1 |
| `db` | Persistence | SQLAlchemy models, Alembic migrations | v1 |

**Reconciler behaviour**

1. Load and validate `platform.yaml`; abort with a readable error on any failure.
2. Observe CloudStack, Kubernetes, gateway, registry and Prometheus state into one `ActualState`.
3. Compute a typed diff and print it as a human-readable plan (like `terraform plan`).
4. Execute actions in dependency order with per-action timeouts; a failed action stops its dependants only.
5. Record every run (plan, actions, outcomes) in Postgres for audit and the UI activity view.
6. Run every 60 s in daemon mode, or once via `aisvc apply`; one leader per zone via Postgres advisory lock.

State lives in Postgres (runs, actions, usage, budgets, profiles, hashed keys, mirror requests, evaluations); nothing is kept only in memory.

## 5. Hugging Face model sourcing

Admins, and reseller domain admins where enabled, browse the Hugging Face Hub from the CloudStack UI, check a model's fit, and mirror a pinned revision into the internal registry before offering it to tenants. Only the mirror worker reaches the internet.

**Flow**

1. **Search.** `HfApi.list_models` with search text, task filter, library and sort (downloads, trending, recently updated).
2. **Check.** `model_info(repo_id, revision, files_metadata=True)` produces a compatibility report: architecture from `config.json` against the engine's supported list, parameter count and dtype from safetensors metadata, download size, context length, license, gated status, task type, and whether remote code is required. `capacity` adds which GPU classes and TP sizes can serve it.
3. **Request.** The requester picks a revision; the engine resolves it to a commit SHA so the mirror is immutable.
4. **Approve.** License must be on the allow-list. Domain admin requests go to a root admin queue when `ai.services.hf.require.approval` is true for that domain.
5. **Mirror.** A Kubernetes Job in the isolated `aisvc-mirror` namespace (the only workload with egress, limited to Hugging Face hosts or a proxy) runs `snapshot_download` with allow-patterns, resumable and parallel, then uploads to `s3://models/<org>/<repo>/<sha>/` with a sha256 manifest.
6. **Scan.** Reject pickle weights (`*.bin`, `*.pt`) by default, run a model scanner and malware scan, and refuse repos needing remote code unless a root admin overrides.
7. **Evaluate (v2).** Run the pre-publish evaluation gate (section 8).
8. **Publish.** Set catalog visibility (all tenants, a domain, or named accounts), price tier and default serving profile. Default generation values are imported from the repo's `generation_config.json`.

**Rules**

- Gated models need the org token's account to have accepted the license on Hugging Face; the check step reports this before any download.
- Tokens live in the secrets manager; optional per-domain tokens let resellers use their own Hugging Face accounts.
- Identical commit SHAs are stored once and shared; storage counts against the requesting domain's quota.
- A daily job flags newer upstream revisions; mirroring one creates a new model version that deploys through the normal blue/green rollout.
- LoRA adapters (v2) can be mirrored through the same flow and are validated against their base model.

## 6. Tuning profiles

All model behaviour is set through named, reusable profiles of three kinds, assigned at defined scopes and resolved by one precedence rule so every value is predictable and auditable.

|  | Serving profile (v1) | Generation profile (v1) | Guardrail profile (v2) |
| --- | --- | --- | --- |
| Applies to | A deployment's engine process | Requests to a deployment | Requests and responses |
| Enforced by | Engine launch arguments rendered by `k8s` | Gateway pre-call hook | Gateway pre- and post-call hooks |
| Change takes effect | Blue/green rollout, no downtime | Immediately | Immediately |
| Who can edit | Root admins | Root and domain admins; users for their own account within bounds | Root and domain admins; users may add stricter rules only |
| Example parameters | `gpu_memory_utilization`, `max_model_len`, `max_num_seqs`, `max_num_batched_tokens`, prefix caching, chunked prefill, `kv_cache_dtype`, quantization, TP size, speculative decoding, LoRA limits, tool-call and reasoning parsers, chat template | `temperature`, `top_p`, `top_k`, `min_p`, `max_tokens`, presence, frequency and repetition penalties, `seed`, stop sequences, system prompt prefix, structured output allowed, logprobs allowed | Safety classification, PII redaction, topic deny-lists, maximum input size; action per check: block, redact or flag |

**Value policy.** Each generation and guardrail value carries a policy: `default` (client may override within `min`/`max`), `clamp` (client value forced into range), or `locked` (always applied). Unset values fall through to the next level.

**Precedence (most specific wins; a lock wins over every level below it):**

1. Platform defaults.
2. Model defaults, imported from `generation_config.json` at mirror time.
3. Domain profile.
4. Account profile.
5. Deployment profile.
6. API key profile.
7. The request itself, within the merged bounds.

Locked values are greyed out in the UI with the level that set them.

**Rules**

- Serving profiles are validated by `capacity` before saving; only allow-listed engine parameters are accepted.
- Generation profiles are validated against model limits.
- `getAiEffectiveParameters` returns the resolved value and source level of every parameter for any deployment, account and key.
- The gateway records effective parameters per request in metering logs, never prompt or output content.

## 7. Operability and tenant experience (Tier 1, v1 GA)

These seven features make the platform installable by operators and adoptable by tenants without support tickets; v1 does not ship without them.

**Admin setup wizard**

- First-run flow in the CloudStack UI, re-runnable per zone: enable the zone, register engine URLs and mTLS certificates, configure the S3 registry, register the Hugging Face endpoint and token, choose or create the CKS cluster, set the gateway URL.
- GPU discovery reads host tags via `listHosts`, proposes GPU classes, and maps them to existing offerings or creates them.
- Each step validates live (S3 write/read, Hugging Face `whoami`, engine health) before moving on.
- Ends with a self-test: deploy a tiny test model, send a request through the gateway, check metering, tear down. The report is stored.
- Writes ConfigKeys and commits the resulting `platform.yaml`.

**Host maintenance integration**

- The plugin listens for host maintenance events and asks the engine to drain the node before the host enters maintenance.
- The engine reschedules replicas onto other hosts, waits for health, then stops and removes the GPU node VM.
- Maintenance waits up to `ai.services.maintenance.drain.timeout` (default 15 min); if capacity elsewhere is short, the admin must confirm a forced drain.
- On exit from maintenance, capacity is re-added per the spec.
- Auto-quarantine: repeated GPU XID or ECC errors from DCGM trigger the same drain and an alert.

**Tenant playground and snippets**

- Streaming chat console on each endpoint page with the effective profiles applied, showing token counts and latency.
- Uses a 15-minute, model-scoped session key, so real keys never reach the browser; usage is metered normally.
- Copy-ready snippets for curl, Python and the OpenAI SDK (Python and JavaScript) with base URL and model filled in.

**Scoped API keys**

- Fields: name, allowed models, expiry, IP CIDR allow-list, optional rate limit up to the account quota, optional key-level profiles.
- Shown once; last-used time visible; rotation by overlapping keys.
- Enforced at the gateway; out-of-scope requests return 403 with a clear reason.

**Budgets and alerts**

- Monthly budgets per account or domain, in currency or in tokens and GPU-hours, priced from the catalog price tier.
- Default thresholds 50%, 80% and 100%; at 100% the admin-chosen action is alert only or block.
- Notifications as CloudStack events, email and HMAC-signed webhooks.

**Engine high availability**

- At least two stateless engine API replicas behind a load balancer.
- One reconciler leader per zone via Postgres advisory lock.
- Postgres in HA; daily backups of Postgres and the spec repository; restore runbook tested quarterly.
- The Java plugin accepts multiple engine URLs and fails over between them.

**Terraform support**

- Resources in the CloudStack Terraform provider (Go), built on the native API commands: `cloudstack_ai_deployment`, `cloudstack_ai_api_key`, `cloudstack_ai_generation_profile`, `cloudstack_ai_serving_profile` (admin), `cloudstack_ai_budget`; data source `cloudstack_ai_models`.
- Import support; acceptance tests against the simulator.

## 8. Differentiating features (Tier 2, v2)

These six features drive margin and win regulated and enterprise buyers; they build on v1 without changing its architecture.

**Multi-LoRA serving**

- Tenants upload LoRA adapters (PEFT format, safetensors only) through the UI or API, or mirror them from Hugging Face (section 5).
- Validation: base model must match a deployed base, rank ≤ the serving profile's `max_lora_rank`, size and count limits per account.
- Served on shared base deployments with the engine's multi-LoRA support; adapters load on demand with least-recently-used eviction.
- Exposed at the gateway as `<base>:<adapter>`, visible only to the owning account (or domain, if shared by a domain admin).
- Metered at the base price plus a configurable adapter premium.

**Embeddings, rerankers and vision models**

- Catalog `task` field: `chat`, `embedding`, `rerank` or `vision-chat`; the compatibility check and `capacity` handle each type.
- Gateway exposes `/v1/embeddings` and a rerank endpoint; vision models accept image inputs in chat requests.
- Small embedding and rerank models can be packed onto L40S, vGPU or MIG classes; priced per input token.

**Batch inference**

- OpenAI-compatible batch API: JSONL request files in, results out within a completion window (default 24 h).
- Runs on spare capacity at lower priority and is preempted by online traffic.
- Priced at a configurable discount to the online rate; results kept for a configurable retention period.

**Guardrail profiles**

- The third profile type in section 6, with the same scopes and lock semantics.
- Checks use an open-weight guard model served internally, PII detection with redaction, topic deny-lists and input size limits.
- Blocks emit events with category only, never content, unless the tenant opted into request logging.
- Presets include an education-safe profile for school customers. Target added latency: under 150 ms at p95, validated in the GPU lab.

**Pre-publish evaluation gate**

- New models, revisions and quantizations run in a staging deployment against a standard benchmark subset and any customer golden sets before publishing.
- Publishing is blocked if any suite regresses beyond the threshold (default 2 points) unless a root admin overrides with a reason.
- Performance results (TTFT, tokens/s per GPU class) are recorded on the catalog entry and feed `capacity`.

**Private endpoints**

- An endpoint can attach to a tenant's CloudStack VPC tier through a per-tenant gateway proxy with an address on that network.
- The public endpoint can be disabled for that deployment.
- Custom domain with TLS via ACME or an uploaded certificate.

## 9. CloudStack native integration

A Java API plugin owns auth, RBAC, async jobs, events, usage and limits, and a Vue section in the standard CloudStack UI is the only user surface; the Python engine sits behind the management servers and is never exposed to users.

**API plugin (`plugins/ai-services`)**

- Implements `PluggableService`; state-changing commands extend `BaseAsyncCmd` and run as CloudStack async jobs that poll the engine's `/v1/jobs/{id}`.
- Minimal ownership table (`ai_deployment`: uuid, account\_id, domain\_id, zone\_id, engine\_id, state) created by a schema upgrade script, so CloudStack enforces ownership, list filtering and cleanup natively. The engine stays the source of truth for runtime state.
- ConfigKeys: `ai.services.enabled` (zone), `ai.services.engine.urls`, `ai.services.engine.timeout`, mTLS settings, `ai.services.hf.require.approval` (domain), `ai.services.maintenance.drain.timeout`.
- Default role permissions for Root Admin, Domain Admin and User; admin-only commands are never granted to tenant roles.
- Action events: `AI.MODEL.MIRROR`, `AI.MODEL.PUBLISH`, `AI.MODEL.DEPLOY`, `AI.MODEL.UPDATE`, `AI.MODEL.DELETE`, `AI.ROLLOUT.PROMOTE`, `AI.ROLLOUT.ROLLBACK`, `AI.PROFILE.UPDATE`, `AI.KEY.CREATE`, `AI.KEY.REVOKE`, `AI.BUDGET.THRESHOLD`, `AI.NODE.DRAIN`, `AI.GUARDRAIL.BLOCK` (v2).
- Usage types for tokens, GPU-hours, adapter premium and batch tokens; the plugin pulls `/v1/usage` hourly and writes usage events for existing billing integrations.
- Limits on deployments, adapters and GPU-hours per account; native GPU resource limits via `max.account.gpus` / `max.domain.gpus` / `max.project.gpus`, which 4.23 supports ([ADR-0003](docs/adr/0003-cloudstack-release.md)).
- Hooks: host maintenance triggers a drain (section 7); account or domain deletion removes deployments, adapters and keys.
- ~~Before writing bespoke Java, evaluate whether the Extensions framework in the target release covers any custom actions.~~ *Evaluated for 4.23 in [ADR-0003](docs/adr/0003-cloudstack-release.md): it does not. Custom actions attach to `VirtualMachine`, `Network` and `Vpc` only, so new first-class resources need the Java plugin. Revisit for v2 private endpoints, where a `Vpc` custom action is the right shape.*

**Command map**

| Command | Engine endpoint | Async | Roles | Release |
| --- | --- | --- | --- | --- |
| `searchAiHfModels` | GET `/v1/hf/search` | No | Admin; domain admin if enabled | v1 |
| `checkAiHfModel` | GET `/v1/hf/check` | No | Admin; domain admin if enabled | v1 |
| `mirrorAiModel` | POST `/v1/models/mirror` | Yes | Admin; domain admin if enabled | v1 |
| `approveAiModel` | POST `/v1/models/{id}/approve` | No | Root admin | v1 |
| `publishAiModel` | POST `/v1/models/{id}/publish` | No | Admin; domain admin for own domain | v1 |
| `listAiModels` | GET `/v1/models` | No | All | v1 |
| `deployAiModel` | POST `/v1/deployments` | Yes | All | v1 |
| `listAiModelDeployments` | GET `/v1/deployments` | No | All | v1 |
| `updateAiModelDeployment` | PATCH `/v1/deployments/{id}` | Yes | All | v1 |
| `deleteAiModelDeployment` | DELETE `/v1/deployments/{id}` | Yes | All | v1 |
| `promoteAiRollout`, `rollbackAiRollout` | POST `/v1/deployments/{id}/rollout/...` | Yes | All | v1 |
| `listAiEndpoints` | GET `/v1/endpoints` | No | All | v1 |
| `createAiApiKey`, `listAiApiKeys`, `revokeAiApiKey` | `/v1/keys` | No | All | v1 |
| `listAiServingProfiles`, `createAiServingProfile`, `updateAiServingProfile`, `deleteAiServingProfile` | `/v1/profiles/serving` | Yes (edits roll out) | Root admin | v1 |
| `listAiGenerationProfiles`, `createAiGenerationProfile`, `updateAiGenerationProfile`, `deleteAiGenerationProfile` | `/v1/profiles/generation` | No | All, scoped | v1 |
| `assignAiTuningProfile` | POST `/v1/profiles/assignments` | Yes for serving | All, scoped | v1 |
| `getAiEffectiveParameters` | GET `/v1/profiles/effective` | No | All | v1 |
| `listAiUsage` | GET `/v1/usage` | No | All | v1 |
| `listAiBudgets`, `createAiBudget`, `updateAiBudget`, `deleteAiBudget` | `/v1/budgets` | No | All, scoped | v1 |
| `createAiPlaygroundSession` | POST `/v1/playground/sessions` | No | All | v1 |
| `listAiSetupChecks` | GET `/v1/setup/preflight` | No | Root admin | v1 |
| `discoverAiGpuClasses` | GET `/v1/setup/gpu-discovery` | No | Root admin | v1 |
| `runAiSetupSelfTest` | POST `/v1/setup/selftest` | Yes | Root admin | v1 |
| `listAiGpuCapacity` | GET `/v1/capacity` | No | Admin | v1 |
| `drainAiNode` | POST `/v1/nodes/{id}/drain` | Yes | Admin | v1 |
| `listAiReconcilerRuns` | GET `/v1/runs` | No | Admin | v1 |
| `listAiAdapters`, `uploadAiAdapter`, `deleteAiAdapter` | `/v1/adapters` | Yes for upload | All, scoped | v2 |
| `createAiBatch`, `listAiBatches`, `cancelAiBatch` | `/v1/batches` | Yes for create | All | v2 |
| `getAiBatchResults` | GET `/v1/batches/{id}/results` | No | All | v2 |
| `listAiGuardrailProfiles`, `createAiGuardrailProfile`, `updateAiGuardrailProfile`, `deleteAiGuardrailProfile` | `/v1/profiles/guardrail` | No | All, scoped | v2 |
| `runAiModelEvaluation`, `listAiEvaluations` | `/v1/models/{id}/evaluations` | Yes | Admin | v2 |
| `uploadAiCertificate`, `listAiCertificates`, `replaceAiCertificate`, `deleteAiCertificate` | `/v1/certificates` | Yes for upload, replace, delete | All, scoped | v2 |
| `listAiPrivateEndpoints`, `createAiPrivateEndpoint`, `deleteAiPrivateEndpoint` | `/v1/endpoints/private` | Yes for create, delete | All, where enabled | v2 |

**UI section**

- `ui/src/config/section/aiservices.js`, views under `ui/src/views/aiservices/`, strings in `ui/public/locales/en.json`.
- Standard list, detail and action patterns, so it inherits search, pagination, async job notifications and role-based action visibility.
- Everything in the UI is also available through CloudMonkey and Terraform.

**Screens**

| Audience | Screen | Contents | Release |
| --- | --- | --- | --- |
| Admin | Setup wizard | Zone enablement, connection checks, GPU discovery, self-test report | v1 |
| Admin | Hugging Face browser | Search, compatibility report, mirror request, approval queue, publish | v1 |
| Admin | Model registry | Mirrored models, versions, checksums, license, deployments using each | v1 |
| Admin | GPU capacity | Heatmap by zone and class: allocated, free, unhealthy, quarantined | v1 |
| Admin | Fleet health | Template and driver versions, drift alerts, drain action | v1 |
| Admin | Rollouts | Active blue/green rollouts, canary weight, promote/rollback | v1 |
| Admin | Tuning profiles | Serving, generation and guardrail profiles; assignments by scope | v1 (guardrail v2) |
| Admin | Activity | Reconciler runs, plans, failed actions | v1 |
| Admin | Evaluations | Runs, scores against current version, gate result, override | v2 |
| Tenant | Model catalog | Approved models, task, size, license, price tier, Deploy action | v1 |
| Tenant | Endpoints | Base URL, status, replicas, scoped API keys, snippets | v1 |
| Tenant | Playground | Streaming chat with effective profiles, token counts, latency | v1 |
| Tenant | Generation settings | Account and key profiles within bounds; locked values shown with source | v1 |
| Tenant | Effective parameters | Resolved values per endpoint and key, with source level | v1 |
| Tenant | Usage and budgets | Tokens, GPU-hours, spend, thresholds, alert channels | v1 |
| Tenant | Adapters | LoRA adapters, base model, status, usage | v2 |
| Tenant | Batch jobs | Batches, progress, results download | v2 |
| Tenant | Guardrails | Assigned guardrails, stricter account rules, block events by category | v2 |
| Tenant | Private endpoints | VPC attachment, custom domain, certificate, public access toggle | v2 |

## 10. Engine REST API contract

The engine exposes a versioned REST API (`/v1`) called only by the Java plugin and the operator CLI; the section 9 command map is the authoritative list of endpoints, and the FastAPI-generated OpenAPI schema is the contract.

**Engine-only endpoints (not mapped to user commands)**

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/jobs/{id}` | Async job status, polled by the plugin |
| GET | `/v1/health`, `/v1/ready` | Liveness and readiness for load balancers |
| GET | `/v1/usage/export` | Hourly usage pull by the plugin for CloudStack usage records |
| POST | `/v1/events/host-maintenance` | Plugin notifies the engine of host maintenance |

**Tenant-facing gateway APIs (OpenAI-compatible, served by the gateway, not the engine)**

- `/v1/chat/completions` and `/v1/completions` (v1).
- `/v1/models` filtered to what the key may use (v1).
- `/v1/embeddings`, rerank, `/v1/files` and `/v1/batches` (v2).

**Conventions**

- Async calls return `202` with a job id; job states mirror CloudStack async jobs (pending, success, failure with error text).
- Every request carries a caller context `{user_id, account, domain_path, role}` set by the plugin from the CloudStack session; the engine filters every response by it.
- Changes made through the API are written back to `platform.yaml` via Git commit.
- Errors use RFC 9457 problem JSON; breaking changes require a new version prefix.

## 11. Non-functional requirements

**Multi-tenancy**

- CloudStack domains and accounts map one-to-one to tenants; dedicated tiers get their own VPC and node pool.
- Shared pools isolate tenants at the gateway (keys, scopes, quotas, budgets, profiles) and in metering.
- Prompts and outputs are never logged by default; tenant opt-in logging has its own retention setting.

**Security**

- Engine and plugin communicate over mTLS; the engine is not reachable from tenant networks.
- API keys stored as salted hashes, shown once; playground keys expire in 15 minutes.
- Only the mirror worker has egress; inference pods have none, enforced by network policy.
- Artifacts verified by sha256 before load; pickle weights and remote code blocked by default; license allow-list enforced at mirror and deploy.
- Secrets (CloudStack keys, S3 and Hugging Face tokens) come from a secrets manager or Kubernetes Secrets, never the spec file.

**Observability**

- DCGM exporter, vLLM and gateway metrics into Prometheus; Grafana dashboards shipped in the repo.
- Structured JSON logs with run id and action id on every line.
- Alerts: GPU XID/ECC errors, TTFT p95 over SLO for 10 minutes, queue depth over target, driver drift, reconciler failures, budget thresholds, mirror failures.

**Reliability and performance**

- At least 2 replicas per production model, spread across hosts by anti-affinity.
- Weights pre-staged on node-local NVMe by a cache DaemonSet; target cold start under 3 minutes for a 70B FP8 model (validated in milestone 3).
- Engine HA and reconciler leader election as in section 7.
- Node loss recovered to SLO within 15 minutes; host maintenance drains with no failed requests.
- Guardrails (v2) add under 150 ms at p95.

## 12. Test framework

Testing is built in from milestone 1: every module ships with its tests, most coverage runs in CI without GPUs, a GPU lab gates releases, and every acceptance criterion in section 14 maps to at least one automated test.

**Test layers**

| Layer | Scope | Tools | Runs without GPU |
| --- | --- | --- | --- |
| Unit | Spec validation, capacity math, plan diff, profile resolution and clamping, HF compatibility parsing, metering and budgets | pytest, hypothesis, pytest-asyncio | Yes |
| Contract | Engine API against OpenAPI; CloudStack calls against recorded responses from the target release; plugin client against the same schema | schemathesis, pytest-recording (vcrpy), OpenAPI-generated Java client | Yes |
| Integration | Full reconcile loop, gateway enforcement, mirroring | Docker Compose: CloudStack simulator, kind, MinIO, Postgres, LiteLLM, mock model server, mock Hub | Yes |
| Java plugin | Commands, DAOs, async bridge, events, usage, hooks | JUnit 5, Mockito; Marvin against a simulator zone | Yes |
| UI | Section config, views, role-based action visibility | Vitest; Playwright against the simulator | Yes |
| Terraform | Provider resources | Terraform acceptance tests against the simulator | Yes |
| GPU lab | Real serving, rollouts, autoscaling, maintenance, SLOs | pytest marked `gpu` on a lab zone | No |
| Load | TTFT, tokens/s, queueing, autoscale, batch preemption | Locust or k6 through the gateway; vLLM benchmarks | No |
| Chaos | Recovery from node, pod, gateway, engine and storage failures | Scripted fault injection | Partly |
| Security | Isolation, egress, supply chain, static analysis | Role × command matrix, network policy probes, bandit, semgrep, pip-audit, trivy, ruff, mypy strict | Yes |

The mock model server returns deterministic tokens with configurable latency, so routing, profile enforcement, guardrail hooks and metering are testable without GPUs.

**CI stages**

| Stage | Trigger | Time budget | Blocks merge |
| --- | --- | --- | --- |
| Lint, types, unit | Every push | 5 min | Yes |
| Contract + integration | Every PR | 25 min | Yes |
| Java unit + Marvin | PRs touching `java-plugin/` | 30 min | Yes |
| UI component + Playwright | PRs touching `ui/` | 20 min | Yes |
| Terraform acceptance | PRs touching the provider | 20 min | Yes |
| Nightly | Schedule | 2 h | No, alerts the team |
| Release gate | Release branch | Half a day | Yes |

Nightly runs a real Hugging Face mirror of a tiny test model, a GPU lab smoke test and a load baseline. The release gate runs the full GPU lab suite, chaos, load against SLOs, and all security scans.

**Coverage targets**

- 90% line and branch on `spec`, `capacity`, `reconciler` diffing, `profiles` resolution and `gateway` enforcement hooks.
- 80% overall for the engine; 70% line for the Java plugin.
- Every native command has a Marvin test per default role, allowed and denied.
- Every UI screen has at least one Playwright flow per audience.

**Key test cases**

| Test | Layer | Proves | Release |
| --- | --- | --- | --- |
| `test_second_apply_produces_empty_plan` | Integration | Reconciler is idempotent | v1 |
| `test_dry_run_makes_no_api_writes` | Integration | `--dry-run` is safe | v1 |
| `test_capacity_rejects_model_that_cannot_fit` | Unit | Sizing blocks impossible specs with the GB shortfall | v1 |
| `test_hf_check_flags_gated_remote_code_and_size` | Unit | Compatibility report is complete | v1 |
| `test_mirror_rejects_pickle_weights` | Integration | Supply-chain rule enforced | v1 |
| `test_mirror_pins_commit_sha_and_checksums` | Integration | Mirrors are immutable and verified | v1 |
| `test_locked_domain_param_beats_account_key_and_request` | Unit + integration | Lock precedence holds | v1 |
| `test_clamp_policy_bounds_temperature` | Unit | Clamp policy enforced | v1 |
| `test_effective_params_report_source_level` | Contract | Audit view is accurate | v1 |
| `test_serving_profile_change_rolls_out_without_errors` | GPU lab | Zero failed requests during rollout | v1 |
| `test_tenant_cannot_see_other_tenant_resources` | Marvin + Playwright | Isolation across API, CloudMonkey and UI | v1 |
| `test_inference_pod_has_no_internet_egress` | Security | Only the mirror worker reaches the internet | v1 — **not written; no NetworkPolicy exists to test** |
| `test_account_deletion_removes_deployments_and_keys` | Marvin | Lifecycle hook works | v1 |
| `test_node_loss_recovers_within_slo_window` | Chaos (GPU lab) | Recovery within 15 minutes | v1 |
| `test_usage_reconciles_with_gateway_within_1pct` | Integration | Billing data is trustworthy | v1 |
| `test_setup_wizard_selftest_on_empty_zone` | Integration + Playwright | Install works without the CLI | v1 |
| `test_maintenance_drains_before_gpu_vm_stops` | Marvin + GPU lab | Maintenance causes no failed requests | v1 |
| `test_scoped_key_rejects_other_model_and_ip` | Integration | Key scopes enforced | v1 |
| `test_budget_block_at_100pct_and_alerts` | Integration | Budgets alert and block | v1 |
| `test_engine_leader_failover_no_duplicate_actions` | Chaos | Engine HA holds | v1 |
| `test_terraform_ai_resources_lifecycle` | Terraform acceptance | Create, import, destroy work | v1 |
| `test_lora_adapter_private_to_owner` | Integration + Marvin | Adapter isolation | v2 |
| `test_batch_yields_to_online_traffic` | Load (GPU lab) | Batch never breaches online SLOs | v2 |
| `test_locked_guardrail_blocks_and_logs_category_only` | Integration + security | Enforcement without content leakage | v2 |
| `test_eval_regression_blocks_publish` | Integration | Evaluation gate works | v2 |
| `test_private_endpoint_not_publicly_reachable` | Security | Private endpoints stay private | v2 |

**Conventions for Claude Code**

- Write tests first; a milestone is done only when its tests pass in CI.
- Make targets: `test-unit`, `test-contract`, `test-int`, `test-java`, `test-ui`, `test-tf`, `test-gpu` (lab only), `record`.
- External services are always faked in unit tests; recorded fixtures are refreshed only with `make record` against a lab zone.
- Every bug gets a regression test before the fix.

## 13. Repo layout and Claude Code build plan

Give Claude Code this spec as `SPEC.md` at the repo root and build one milestone per session, each ending with green tests and a short `PROGRESS.md` update; milestones 1–8 make v1 GA and milestone 9 makes v2.

```text
aisvc/
  SPEC.md                  # this document, exported to Markdown
  CLAUDE.md                # conventions, commands, test instructions
  PROGRESS.md
  pyproject.toml
  Makefile                 # test-unit, test-contract, test-int, test-java, test-ui, test-tf, test-gpu, record
  aisvc/
    spec/                  # pydantic models, loader, validation
    cloudstack/            # signed API client, async job polling
    inventory/  capacity/  provision/  k8s/
    hf/  registry/  profiles/  gateway/
    autoscale/  lifecycle/  metering/
    setup/  playground/
    adapters/  batch/  guardrails/  evaluation/   # v2
    reconciler/
    api/                   # FastAPI app (section 10)
    cli/                   # Typer CLI
    db/                    # SQLAlchemy models + Alembic
  gateway-hooks/           # LiteLLM custom handlers: scopes, profiles, budgets, guardrails
  java-plugin/             # plugins/ai-services, the product surface
    src/main/  src/test/
  ui/                      # aiservices section for the CloudStack ui/ tree
    src/  tests/
  terraform/               # resources for the CloudStack Terraform provider
  templates/
    cloud-init/            # GPU node userdata
    packer/                # golden GPU template
  deploy/                  # Helm: engine, LiteLLM, mirror namespace, GPU Operator values, dashboards
  tests/
    unit/  contract/  integration/  gpu/  load/  chaos/  security/
    mocks/model_server/  mocks/hf_hub/
    fixtures/cloudstack/  fixtures/hf/
  test/integration/plugins/ai-services/   # Marvin tests
  examples/platform.yaml
```

**Milestones (in order; each depends on the previous)**

1. **Foundation.** Scaffold, `CLAUDE.md`, CI, `spec` models and validation, CloudStack client with recorded responses, `inventory`, `capacity`, `aisvc plan`.
2. **Provisioning.** `provision` for VMs and CKS pools, cloud-init and Packer golden template, `aisvc apply` for node pools, Postgres run log.
3. **Serving and sourcing.** `registry`, `hf` search/check/mirror/scan (section 5), `k8s` rendering, `gateway` with hooks, `profiles` serving and generation (section 6), one model end to end in a lab zone, cold-start target validated.
4. **Engine API.** FastAPI per section 10, async jobs, caller-context filtering, mTLS, OpenAPI contract tests; CLI switched to the API.
5. **Native API plugin.** In a CloudStack source checkout of the target release: `plugins/ai-services` with the v1 commands from section 9, ConfigKeys, ownership table, async bridge, events, role permissions, usage types, deletion hooks; JUnit and Marvin tests.
6. **Native UI section.** v1 screens from section 9, tenant first then admin, i18n, Playwright per role.
7. **Operations.** `autoscale`, `lifecycle` (blue/green, drain, template upgrades), `metering` into CloudStack usage, alerts and dashboards.
8. **Tier 1 operability (v1 GA).** Setup wizard and self-test, maintenance integration and auto-quarantine, playground and snippets, scoped keys, budgets, engine HA, Terraform resources (section 7).
9. **Tier 2 differentiators (v2).** Multi-LoRA, embeddings/rerank/vision, batch, guardrail profiles, evaluation gate, private endpoints (section 8).

**Session prompting tips:** point Claude Code at the milestone's section numbers, ask for tests first, and require `aisvc plan --dry-run` output against `examples/platform.yaml` before any apply logic merges.

## 14. Acceptance criteria and open decisions

**v1 GA is done when:**

- [ ] A new zone goes from empty to a passing self-test using only the setup wizard.
- [ ] `aisvc apply` builds an H100 node pool and serves a 70B FP8 model behind the gateway with no manual steps; re-running it produces an empty plan.
- [ ] An admin can search Hugging Face, review the compatibility report, mirror a pinned revision and publish it to a domain entirely from the CloudStack UI.
- [ ] Pickle-only and remote-code repos are rejected by default; a gated model mirrors with the org token.
- [ ] A value locked in a domain generation profile cannot be overridden by an account profile, key profile or request, and the effective-parameters view shows its source.
- [ ] Changing a serving profile, or a blue/green model rollout with 10% canary, completes with no failed requests; promote and rollback work from the UI and CloudMonkey.
- [ ] Killing a GPU node restores the model's SLO within 15 minutes; putting a GPU host into maintenance drains it with no failed requests.
- [ ] Two tenants on a shared pool cannot see each other's endpoints, keys or usage via the API, CloudMonkey or UI, and every action appears in CloudStack events and usage records.
- [ ] A key scoped to one model and CIDR is rejected for any other model or address.
- [ ] A blocking budget stops responses at 100% after alerts at 50% and 80%.
- [ ] Losing an engine replica or the reconciler leader causes no API outage and no duplicate actions.
- [ ] Deployments, keys, profiles and budgets can be created, imported and destroyed with Terraform.
- [ ] Monthly usage reconciles to gateway token counts within 1%.

**v2 is done when:**

- [ ] A tenant's LoRA adapter serves on a shared base model and is invisible to other accounts.
- [ ] Embedding, rerank and vision models deploy and meter through the same flow as chat models.
- [ ] A batch completes within its window on idle capacity and yields to online traffic without breaching online SLOs.
- [ ] A locked guardrail profile blocks unsafe output, records the category only, and adds under 150 ms at p95.
- [ ] A candidate revision that regresses beyond the threshold cannot be published without a recorded override.
- [ ] A private endpoint is reachable from the tenant VPC and not from the public network when public access is off.

**Open decisions**

- [x] ~~Serving layer: KServe vs the vLLM production stack (or llm-d).~~ **Decided Nov 2026: plain Kubernetes Deployments rendered by `k8s`, with blue/green canary weighting at the gateway. See [ADR-0001](docs/adr/0001-serving-layer.md).** llm-d's features are all multi-node, which is a non-goal; KServe's canary needs Knative and Istio upstream; and routing, autoscaling and rollouts already belong to modules in section 4. Revisit if the one-host replica cap is lifted.
- [x] ~~Gateway: LiteLLM vs Envoy AI Gateway for multi-tenant routing, enforcement and metering at scale.~~ **Decided Nov 2026: LiteLLM Proxy, with hooks importing `aisvc.profiles` directly. See [ADR-0002](docs/adr/0002-gateway.md).** Envoy AI Gateway became Agent Router under the Agentic AI Foundation in Sept 2026; its throughput advantage is real but spent on a bottleneck we do not have, since the GPU fleet saturates an order of magnitude before the gateway does, while its ext_proc model would force a second implementation of profile precedence. Revisit if one zone needs >200 RPS sustained, or if the platform starts fronting third-party hosted models.
- [x] ~~Target CloudStack release, and whether its GPU/vGPU offerings, GPU resource limits and Extensions framework cover part of the plugin.~~ **Decided Nov 2026: 4.23.0.0. See [ADR-0003](docs/adr/0003-cloudstack-release.md).** GPU/vGPU offerings and `max.*.gpus` resource limits are native since 4.21, so the plugin uses them rather than rebuilding them. The Extensions framework **does not** cover this plugin: its custom actions target only `VirtualMachine`, `Network` and `Vpc`, and cannot define new first-class resources, so the Java API plugin is necessary. Note 4.23 is a **non-LTS** release — keep to the 4.22 LTS API surface and plan a 4.24 LTS retarget as a GA gate.
- [ ] Upstream a generic `ai-services` plugin (including setup wizard and maintenance integration), keeping metering, guardrails, evaluation and LoRA management as the commercial layer. *The Python dependency tree is already held to the ASF third-party licensing policy so this stays possible; see [docs/third-party-licenses.md](docs/third-party-licenses.md), enforced by `make test-security`.*
- [ ] Whether reseller domain admins can mirror from Hugging Face directly or only request it.
- [ ] Public Hugging Face Hub vs a private mirror or Enterprise Hub as the source.
- [ ] Whether dedicated-tier tenants may choose serving profiles for their own node pools.
- [ ] Initial model catalog, license allow-list and price tiers; pricing unit (per token, per GPU-hour or reserved capacity).
