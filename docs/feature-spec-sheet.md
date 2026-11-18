# CloudStack AI Services — feature spec sheet

Every number here is read from the code, not from the specification.
Where the two disagree the code wins and the row says so.

**Status:** all nine milestones implemented. Verified against a real
Apache CloudStack 4.23.0.0: the engine's CloudStack-facing half, the
plugin loaded into the management server, its commands and settings,
mTLS between the two, the async job bridge, caller-context filtering,
engine failover, the Marvin tests for every command and role, and the
UI section built and served at `/client`. **Not** verified: the write
half of the reconciler, which needs a Kubernetes cluster; anything
that depends on mirroring a model; the gateway; and a GPU that is not
simulated. [PROGRESS.md](../PROGRESS.md) keeps the unvarnished list,
and the gaps are marked **·not built** in the tables below.

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
| Kubernetes access | Fetched from CloudStack with `getKubernetesClusterConfig` for a `cks` zone; `KUBECONFIG` overrides it and is still needed for an `external` cluster and for `serve` outside the cluster |
| Cluster source | Per zone: a CKS cluster CloudStack manages, or an external one it does not ([ADR-0007](adr/0007-kubernetes-cluster-source.md)). With `create: true` the engine builds it — newest enabled Kubernetes version, smallest offering clearing its minimums, both named in the plan. Both sources join nodes with `kubeadm join` |
| Object store | S3, Ceph RGW or MinIO. The bucket and its credentials can come from CloudStack's object store instead of configuration ([ADR-0005](adr/0005-model-artifact-storage.md)) |
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
| GPU discovery | `listGpuDevices` (4.21+), per device, so vGPU and MIG are countable where host tags are not. `gpu_card` is checked against `listGpuCards` in preflight. **·management not built** — nothing calls `manageGpuDevice`, `unmanageGpuDevice` or `discoverGpuDevices` |
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
| CloudStack config keys | 10 — one zone-scoped, one domain-scoped, eight global |
| UI screens | 19, inside one `aiservices` section |
| Terraform | 12 entries — 5 resources and 1 data source per release |
| Usage types | 5 — input tokens, output tokens, GPU-hours, adapter premium, batch tokens |
| CLI commands | `validate`, `plan`, `apply`, `status`, `serve`, `batch` |
| Database migrations | 10 revisions |
| Async convention | `202` plus a job id mirroring CloudStack async job states |
| Errors | RFC 9457 problem JSON |

## Security

| | |
| --- | --- |
| Engine ↔ plugin | mTLS, required; the engine binds loopback only without it. `install.py --make-certs` generates the CA, both certificates and the PKCS12 stores |
| Caller identity | Asserted by the plugin in `X-Aisvc-*` headers, filtered on every response |
| Uncontexted endpoints | Exactly one — `POST /v1/certificates/bootstrap`, where a proxy has no identity yet |
| Secrets | From the environment or a secrets manager, never `platform.yaml` |
| Tenant isolation | CloudStack domains and accounts map one-to-one to tenants |
| Engine reachability | Never from a tenant network |

---

## What the numbers do not cover

Counts and limits are real and read from the code. What they cannot
tell you is how much of it has been exercised against something real.

**Verified against Apache CloudStack 4.23.0.0** (`make stack-up`), as of
2026-11-18:

- Request signing; `take_snapshot` reading GPU capacity from tagged hosts;
  the capacity planner and its warnings on that observed data;
  `listKubernetesClusters`, `listGpuCards`, `listGpuDevices`, the
  tenant-network lookup; all eight migrations on Postgres 16.
- **The Java plugin**, built and loaded into a running management server:
  all thirty-eight v1 commands register (`listApis` 865 → 903) and the
  twenty v2 ones stay away until `ai.services.v2.enabled` is on, which
  takes it to 923. Commands answer over mTLS; tenant filtering by domain
  works; all ten config keys appear in `listConfigurations`.
- **Role access**, by Marvin: 58 commands across three default roles,
  allowed and denied, 7 tests.
- **The UI section**, by Playwright inside a real 4.23.0.0 UI: 22 of 22
  role tests.
- **Terraform**: 6 of 8 acceptance tests against a live management server;
  2 skip for want of a real `.safetensors` and a real deployment.
- **Engine failover**, in all four branches, including that a replica which
  answered is never retried.
- **Events**: a write command records Completed on success and the same
  event at ERROR level on failure.

**Not verified against anything:** the write half of the reconciler (`apply`
needs a Kubernetes cluster and there is none, though `deployVirtualMachine`
itself now works in the harness), the gateway inside a real LiteLLM process,
and any GPU that is not simulated.

**Known not to work**, as of this date: one command points at an engine
route that does not exist — `approveAiModel`, and it is a missing decision
rather than a missing route. What approval *does* depends on SPEC section
14's open question about whether resellers mirror directly or only request.
See `tests/contract/test_engine_api_contract.py`, where it is listed as
`NOT_YET_SERVED` and the list may only shrink.

Treat the rest of this sheet as a specification of what is built, not a
claim about what is proven.
