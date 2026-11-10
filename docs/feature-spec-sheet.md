# CloudStack AI Services — feature spec sheet

Every number here is read from the code, not from the specification.
Where the two disagree the code wins and the row says so.

**Status:** all nine milestones implemented; nothing has run against a
real management server. [PROGRESS.md](../PROGRESS.md) keeps the
unvarnished list, and the gaps are marked **·not built** in the tables
below.

---

## Platform

| | |
| --- | --- |
| Target CloudStack | 4.23.0.0, held to APIs present in 4.22 LTS ([ADR-0003](adr/0003-cloudstack-release.md)) |
| Engine runtime | Python 3.12, fully typed under `mypy --strict` |
| Plugin build | JDK 17, targeting JDK 11 bytecode |
| Serving | Kubernetes `Deployment` on CKS, rendered from Jinja ([ADR-0001](adr/0001-serving-layer.md)) |
| Gateway | LiteLLM Proxy, MIT side only ([ADR-0002](adr/0002-gateway.md)) **·not a pinned dependency yet** |
| Engine state | Postgres (default) or MariaDB ([ADR-0004](adr/0004-state-store.md)) |
| Object store | S3, Ceph RGW or MinIO |
| Licence policy | ASF Category A/B/X enforced in CI; GPL, LGPL and AGPL prohibited |

## Catalog and models

| | |
| --- | --- |
| Tasks | `chat`, `vision-chat`, `embedding`, `rerank` |
| Serving engines | `vllm`, `sglang`, `trtllm` |
| Quantizations | 12 accepted values across 5 widths: `fp32`/`float32` (4 B/param), `bf16`/`fp16`/`float16`/`none` (2), `fp8`/`int8`/`w8a8` (1), `int4`/`awq`/`gptq` (0.5) |
| Source pinning | A commit SHA is required; artifacts verified by sha256 before load |
| Blocked by default | Pickle weights (`*.bin`, `*.pt`) and `trust_remote_code` |
| Licence enforcement | Checked at mirror time and again at deploy time |
| Internet egress | Mirror worker only — **·not built**: no `NetworkPolicy` is rendered, so nothing enforces it |

## Capacity and sizing

| | |
| --- | --- |
| GPU modes | `passthrough`, `vgpu`, `mig` |
| Replica cap | One replica per host. Validation refuses `tensor_parallel` above the class's `gpus_per_node`; there is no multi-node tensor parallelism, so a replica is bounded by one node's GPUs |
| Sizing inputs | Parameter count, layers, KV heads, head dimension, quantization, context length |
| Memory headroom | 10% reserved above weights and KV cache |
| GPU memory utilisation | 0.9 by default |
| Minimum KV block | 16 tokens |
| Node naming | `aisvc-<pool>-<ordinal>` — pool membership is read back from it |
| Pool bounds | `min_nodes` ≥ 0, `max_nodes` > 0 |
| Units | Memory in GiB throughout; parameter counts in decimal billions |
| Failure mode | A model that will not fit fails **validation**, with the shortfall in GiB, before anything is provisioned |

## Rollouts

| | |
| --- | --- |
| Strategies | `bluegreen`, `canary`, `recreate` |
| Change detection | Config hash stamped as `aisvc.io/spec-hash`, digested at a normalised replica count so scaling never triggers a rollout |
| Live colour | Derived from the gateway's weights, never stored |
| Promotion | An operator action; the superseded colour is retired on the next reconcile |
| Startup probe | 30 failures allowed, roughly 300 s |
| Convergence | A second `apply` on an unchanged system plans nothing; `--dry-run` makes zero writes |
| Leader election | One per zone, by advisory lock — `pg_try_advisory_lock` on Postgres, `GET_LOCK(name, 0)` on MariaDB |

## Tuning profiles

| | |
| --- | --- |
| Profile types | Serving, generation, guardrail |
| Precedence, lowest first | platform → model → domain → account → deployment → key → request |
| Policies | `default`, `clamp`, `locked` — a locked value beats every level below it |
| Assignment scopes | `domain`, `account`, `deployment`, `key` |
| Reporting | `getAiEffectiveParameters` returns the resolved value **and** the level that set it |
| Implementation | One module, imported by both the engine and the gateway, so the two cannot disagree |

## Tenant controls

| | |
| --- | --- |
| API key scopes | Allowed models, expiry, IP CIDR allow-list, optional rate limit, optional key-level profiles |
| Key storage | Salted hash; the secret is shown once |
| Playground keys | Model-scoped, 15-minute TTL, metered normally |
| Budget thresholds | 50%, 80%, 100% |
| At 100% | `alert` or `block`, admin's choice |
| Budget scope | Per account or domain, in currency or in tokens and GPU-hours |
| Prompt logging | Never by default; a guardrail block records its category only |

## Autoscaling and operations

| | |
| --- | --- |
| Signals | Queue depth (target 8), time to first token p95, GPU utilisation |
| Scale up after | 60 s breaching |
| Scale down after | 15 min idle |
| Sample window | 30 min |
| Requirement | **·opt-in**: without `AISVC_PROMETHEUS_URL` replicas hold at the floor |
| Drain | **Destroys** the node — a stopped node keeps its GPU allocated |
| Maintenance drain timeout | 900 s, then an admin confirms a forced drain |
| Maintenance hook | **·partial**: automatic only where an event bus is configured, and none is enabled by default |
| Self-test | **·not runnable**: planned, but its steps need GPU capacity |

## Tier 2 — behind `ai.services.v2.enabled`

| | |
| --- | --- |
| Adapters | PEFT safetensors, addressed as `<base>:<adapter>`, max 2 GiB, default rank cap 32 **·artefacts are not fetched** |
| Batch | JSONL in, results out; 24 h completion window, 30 day retention, 0.5 discount, preempted by online traffic **·no leader election, run exactly one worker** |
| Guardrails | Actions `block`, `redact`, `flag`; scoped per caller with the same lock semantics as generation profiles **·the guard model is not called** |
| Evaluation gate | Blocks a revision regressing past 2.0 points; "never measured" is a block, not a pass; root admin may override with a recorded reason **·nothing produces a run** |
| Private endpoints | Per-tenant proxy VM on the tenant's own tier, public address optionally removed **·no proxy has booted** |
| Certificates | Uploaded or ACME; renewal alerts at 30/14/7/1 days, critical at ≤7 **·replacing one rebuilds the proxy, which is downtime** |
| Bootstrap token | Single-use, 30-minute TTL, bound to one certificate, stored hashed |

## Surfaces

| | |
| --- | --- |
| API commands | 58 — 38 v1, 20 v2 |
| Roles | Admin, DomainAdmin, User |
| CloudStack config keys | 9 — one zone-scoped, one domain-scoped, seven global |
| UI screens | 19, inside one `aiservices` section |
| Terraform | 12 entries — 5 resources and 1 data source per release |
| Usage types | 5 — input tokens, output tokens, GPU-hours, adapter premium, batch tokens |
| CLI commands | `validate`, `plan`, `apply`, `status`, `serve`, `batch` |
| Database migrations | 7 revisions |
| Async convention | `202` plus a job id mirroring CloudStack async job states |
| Errors | RFC 9457 problem JSON |

## Security

| | |
| --- | --- |
| Engine ↔ plugin | mTLS, required; the engine binds loopback only without it |
| Caller identity | Asserted by the plugin in `X-Aisvc-*` headers, filtered on every response |
| Uncontexted endpoints | Exactly one — `POST /v1/certificates/bootstrap`, where a proxy has no identity yet |
| Secrets | From the environment or a secrets manager, never `platform.yaml` |
| Tenant isolation | CloudStack domains and accounts map one-to-one to tenants |
| Engine reachability | Never from a tenant network |

---

## What the numbers do not cover

Counts and limits are real and read from the code. What they cannot
tell you is whether any of it works on a management server, and none of
it has been tried on one: no CloudStack has loaded the plugin, no proxy
VM has booted, and every test needing a live server, a CKS cluster or a
GPU host is skipped.

Treat this sheet as a specification of what is built, not a claim about
what is proven.
