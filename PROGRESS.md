# PROGRESS

## Milestone 1 — Foundation (done)

Scaffold, CI, `spec` models and validation, CloudStack client with recorded responses,
`inventory`, `capacity`, `aisvc plan`.

**State:** 131 tests pass, `ruff` clean, `mypy --strict` clean, 95% line coverage.

```
make check      # lint + types + unit + contract
aisvc validate --spec examples/platform.yaml
aisvc plan --spec examples/platform.yaml --no-observe
```

### What landed

| Module | What it does |
| --- | --- |
| `aisvc/spec` | pydantic v2 models for `platform.yaml`, license allow-list, per-engine serving-parameter allow-lists, and every cross-cutting rule in SPEC.md section 3 |
| `aisvc/capacity` | `estimate()` (weights + KV cache + 10% headroom) and `kv_budget()` for concurrency |
| `aisvc/cloudstack` | Signed async API client: HMAC-SHA1 request signing, pagination, async job polling, bounded retries |
| `aisvc/inventory` | `take_snapshot()` folding `listHosts` / `listVirtualMachines` into a `CapacitySnapshot` per zone and class |
| `aisvc/reconciler` | `Action` / `Plan` / `plan_capacity()` — the typed diff, rendered like `terraform plan` |
| `aisvc/cli` | `aisvc validate` and `aisvc plan`, both strictly read-only |

Validation covers: unknown keys, unresolved references, duplicate names, `tensor_parallel`
power-of-two and ≤ `gpus_per_node`, pinned commit SHAs, the license allow-list, capacity
shortfall in GiB, serving parameters against the engine allow-list, generation bounds and
context limits, and the dedicated/shared tenant pool rules. Every problem is collected and
reported at once, each with the spec path that caused it.

### Decisions taken, and why

**Memory is GiB throughout.** `gpu_mem_gb: 80` is read as 80 GiB, which is how GPU VRAM is
actually quoted. Parameter counts stay decimal billions.

**Models carry an optional `architecture` block** (`params_b`, `num_layers`, `num_kv_heads`,
`head_dim`). SPEC.md requires `capacity.estimate()` at validation time but `platform.yaml` as
written carries nothing to size from — layer and head counts come from the repo's `config.json`.
Milestone 3 fills this in at mirror time; until then it is declared in the spec, and a model
without it is accepted with a warning rather than being blocked.

**Node pools gained an optional `tenant` field.** SPEC.md says a `dedicated` tenant "needs its
own `nodePools` entry" without saying how a pool is marked as owned. Setting `tenant: <name>`
reserves it; a shared tenant using a model on someone else's dedicated pool is an error.

**`--no-observe` plans against a zero snapshot** of everything the spec declares, so it shows
what building the platform from nothing would take without calling CloudStack.

### Settled: the concurrency rule (SPEC.md section 3, amended)

The original rule — `max_model_len` x `max_num_seqs` must fit the KV budget — treats the
concurrency cap as a reservation. A paged engine never makes that reservation: KV blocks are
allocated per token as sequences grow, and the scheduler queues whatever does not fit. vLLM's
own startup assertion is only that the cache hold **one** `max_model_len` sequence, which is
the separate feasibility gate `capacity.estimate()` already enforces. Read literally the rule
rejected SPEC.md's own example by about 16x.

Replaced with a declared-assumption rule, in two parts:

- **Hard floor.** A cap that cannot be given one 16-token KV block per sequence can never be
  approached at any request length, so it is rejected outright. This catches nonsense like
  `max_num_seqs: 1000000` without any false positives.
- **Declared workload.** Models gained `concurrency.working_context_tokens`: the average
  sequence length the deployment is planned for. When set, `max_num_seqs x
  working_context_tokens` must fit the KV budget, and the error names both numbers that would
  fit. When unset there is no workload to check against, so the spec reports the average
  context at which the cap becomes reachable rather than guessing one.

The example's `max_num_seqs: 256` turns out to be entirely reasonable: it is reachable
whenever sequences average 1742 tokens or fewer, an ordinary chat turn. `examples/platform.yaml`
now declares 1536 for the 70B and 512 for the embedding model. SPEC.md section 3 carries the
amended rule and a note recording why.

### Open questions for the next session

1. **Request signing uses `quote_plus`** (spaces as `+`), matching CloudStack's Java
   `URLEncoder`. Worth confirming against the target release before milestone 2, since
   `quote` (spaces as `%20`) appears in some clients. Also unverified against a live
   management server — only against the shape pinned in `tests/fixtures/cloudstack/`.

2. **Node allocation is counted per host, not per GPU.** A host counts as allocated when it
   runs a VM with the class's service offering. That holds for passthrough; `vgpu` and `mig`
   classes will need per-GPU accounting.

3. The eight open decisions in SPEC.md section 14 are untouched. None blocked milestone 1;
   the serving-layer and gateway choices now block milestone 3.

## Milestone 2 — Provisioning (done)

`provision` for VMs and CKS pools, cloud-init and Packer golden template, `aisvc apply` for
node pools, Postgres run log.

**State:** 243 tests pass, `ruff` clean, `mypy --strict` clean, 94% line coverage.

```
make check      # lint + types + unit + contract + integration
aisvc apply --spec examples/platform.yaml --dry-run
```

### What landed

| Module | What it does |
| --- | --- |
| `aisvc/db` | SQLAlchemy run log (`aisvc_run`, `aisvc_run_action`), Alembic migration `0001`, session plumbing. Dialect-neutral so unit tests run on SQLite; JSON becomes JSONB on Postgres |
| `aisvc/provision` | `NodeProvisioner`: `deployVirtualMachine` with offering, template, host anti-affinity and cloud-init; `destroyVirtualMachine`; `scaleKubernetesCluster` |
| `aisvc/provision/cloudinit.py` | Renders GPU node userdata from a Jinja template and base64-encodes it |
| `aisvc/reconciler/executor.py` | Runs a plan in dependency order with per-action timeouts, recording every outcome |
| `aisvc/cli` | `aisvc apply`, with `--dry-run`, confirmation before writes, and fail-fast config checks |
| `templates/packer/` | Golden GPU image: driver, container toolkit, kubelet baked in; records versions in `/etc/aisvc/image.json` for drift detection |

`tests/integration/` is new, with a stateful CloudStack fake whose VMs persist across calls.
It carries the two cases SPEC.md section 12 names: `test_second_apply_produces_empty_plan`
and `test_dry_run_makes_no_api_writes`.

### Decisions taken, and why

**Node VMs are named `aisvc-<pool>-<ordinal>`.** This is what makes provisioning idempotent:
the pool's membership is derived from the names that already exist, so re-running `apply`
creates nothing. New nodes take the lowest free ordinals; removals take the highest first.

**Cluster join details come from the environment, not the spec.** `AISVC_CKS_JOIN_ENDPOINT`,
`AISVC_CKS_JOIN_TOKEN` and `AISVC_CKS_CA_CERT_HASH`, per SPEC.md section 11 — secrets never
live in `platform.yaml`. `GpuNodeUserdata` and `ClusterJoin` keep them out of `repr()` so they
cannot reach a log line or plan output.

**`aisvc apply` asks before writing** unless given `--yes`, and validates credentials and join
details *before* planning, so an operator is never asked to confirm changes that could not
have been applied.

### Two bugs this milestone found in milestone 1

**`plan_capacity` was comparing `min_nodes` against physical hosts.** A pool is sized in GPU
node VMs; hosts are where they are placed. The snapshot now carries `PoolCapacity` (the pool's
member VMs) alongside `ClassCapacity` (host capacity), and the plan sizes pools by VM count
while warning separately when there are too few free hosts to place them on. Caught by the
first integration test that executed a plan and re-observed.

**The CloudStack client retried API errors and discarded their text.** CloudStack answers API
errors with its own status codes, several of them 5xx — 530, 531, 533. The client treated
those as transient, retried three times, then raised "giving up after 3 retries" with the
actual error (`Insufficient capacity`) lost. It now treats any response carrying
`errorcode`/`errortext` as a definitive answer whatever the HTTP status, and only retries a
bare 5xx with no CloudStack error in it.

### Open questions for the next session

1. **`scale_cluster` is implemented but unused.** `plan_capacity` provisions standalone node
   VMs that join CKS via cloud-init; it never calls `scaleKubernetesCluster`. Both paths are
   in SPEC.md section 4. Which one owns GPU worker nodes needs settling before milestone 3
   renders serving workloads onto them — the answer may differ per CloudStack release.

2. **No leader election yet.** SPEC.md section 4 wants one reconciler leader per zone via a
   Postgres advisory lock. `apply` is a one-shot command so nothing races today, but daemon
   mode (milestone 7) needs it, and two concurrent `apply` runs would both plan the same
   nodes.

3. **Node removal picks the highest ordinal, not the emptiest node.** Fine while nothing is
   scheduled on them; milestone 7's `drain` has to come first, or scaling down will kill
   nodes with running replicas.

4. **The weight-cache device is hardcoded to `/dev/nvme1n1`.** It should come from the GPU
   class or be discovered on the node.

## Decisions

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](docs/adr/0001-serving-layer.md) | Serving layer: plain Kubernetes Deployments rendered by `k8s`; no KServe, llm-d or vLLM production stack in v1 | Accepted 2026-11-07 |
| [0002](docs/adr/0002-gateway.md) | Gateway: LiteLLM Proxy, with hooks importing `aisvc.profiles` rather than reimplementing precedence | Accepted 2026-11-07 |

### ADR-0001 in one paragraph

llm-d and KServe's `LLMInferenceService` (which is built on llm-d) earn their complexity
through multi-node features — disaggregated prefill/decode, expert parallelism, RDMA KV
transfer — and multi-node is an explicit non-goal here: a replica is capped at one 8-GPU host.
The remaining case for KServe was canary and readiness gating, but upstream `canaryTrafficPercent`
is silently ignored in RawDeployment ([kserve#5335](https://github.com/kserve/kserve/issues/5335)),
so canary means Knative plus Istio on CKS — a large dependency for one feature that `lifecycle`
owns anyway. The vLLM production stack is vLLM-only, which conflicts with
`engine: vllm | sglang | trtllm`. Meanwhile routing, autoscaling and rollouts are already
assigned to our own modules in section 4, so every candidate duplicates three or four of them.
Plain Deployments also let the node-local NVMe weight cache work as section 11 describes, which
KServe's per-pod `storageUri` pull works against. Rendering stays behind Jinja templates so a
later move to `LLMInferenceService` is contained.

**One consequence to carry forward:** the gateway must support weighted routing between blue
and green backends. ADR-0002 confirms LiteLLM does this with weighted model-group entries.

### ADR-0002 in one paragraph

Section 14 framed the gateway choice as being about scale, and that is the one dimension where
Envoy wins — but the arithmetic says we are not at that scale by an order of magnitude. A 70B
replica on two H100s completes a few requests per second; ten replicas perhaps thirty; the
spec's own example tenant quota is `rpm: 600`, which is ten. LiteLLM's reported few-hundred-RPS
ceiling sits well above anything our GPU capacity can absorb, and we would buy GPUs long before
the gateway became the constraint. Against that, Envoy's ext_proc model forces our enforcement
logic into a separate gRPC service — in Python, giving back the throughput we adopted Envoy
for; in Go, implementing seven-level profile precedence with `clamp` and `locked` semantics a
second time, where the two copies must agree forever. Section 6 precedence is the most
correctness-critical logic in the platform, and LiteLLM's Python hooks let the gateway call the
same `aisvc.profiles.resolve()` that `getAiEffectiveParameters` calls. Note also that Envoy AI
Gateway was renamed Agent Router and moved from CNCF to the Agentic AI Foundation in September
2026 — not disqualifying, but the spec's name for it is already stale.

## Licensing

`docs/third-party-licenses.md` records the policy and the inventory; `make test-security`
enforces it in CI. Because this is an Apache CloudStack plugin and section 14 keeps open the
option of upstreaming, the dependency tree is held to the ASF Category A/B/X policy, under
which **LGPL is prohibited outright** — even for dynamically linked Python imports.

**One conflict was found and fixed:** `psycopg` 3.x, the Postgres driver added in milestone 2,
is LGPL-3.0-only. Replaced with `pg8000` (BSD-3-Clause), which SQLAlchemy supports as
`postgresql+pg8000://`; the run log is synchronous, so nothing was lost. The tree is now 43
Category A, 3 Category B (`certifi`, `hypothesis`, `pathspec`, all MPL-2.0 and all acceptable),
and nothing in Category X.

Three obligations sit outside a metadata scan and need legal review before anything ships:
the **NVIDIA driver EULA** on the Packer golden image (a redistribution question, not a code
conflict — shipping the `.pkr.hcl` for operators to build is the conservative path we take
today); the **model licences**, where `llama3.x`, `gemma`, `qwen` and `openrail` are open-weight
with use restrictions rather than open source, which matters because tenants pay for inference
on them; and **LiteLLM's split licence**, where `enterprise/` is commercial and we must stay on
the MIT side. `licenses.yaml` now separates OSI-approved from restricted entries so the
distinction is visible at the point of approval.

## Milestone 3 — Serving and sourcing (mostly done)

`registry`, `hf`, `k8s` rendering, `gateway` with hooks, `profiles`. **Not done:** one model
end to end in a lab zone with the cold-start target validated — that needs GPUs.

**State:** 438 tests pass, `ruff` clean, `mypy --strict` clean, 95% line coverage.
`profiles` is at 99% and `gateway` at 100%, both above the 90% the spec sets for them.

### What landed

| Module | What it does |
| --- | --- |
| `aisvc/profiles` | The seven-level precedence chain with `default` / `clamp` / `locked`. Bounds intersect down the chain so a tenant cannot widen an admin's limit; a lock binds every level below it. Backs both `getAiEffectiveParameters` and the gateway hook |
| `aisvc/k8s` | Renders `Deployment`, `Service` and `PodDisruptionBudget` per model per colour (ADR-0001). Host anti-affinity, GPU limits from `tensor_parallel`, `hostPath` weight cache, startup probe sized for the three-minute cold start. Serving profile parameters become engine flags |
| `aisvc/registry` | Content-addressed S3 storage at `<org>/<repo>/<sha>/`, sha256 manifests, verification before load, pickle refusal, per-domain quota accounting. `InMemoryObjectStore` for tests, `S3ObjectStore` for Ceph RGW/MinIO |
| `aisvc/hf` | Compatibility report (architecture, size, context, licence, gating, remote code) with `capacity` saying which GPU classes and TP widths fit; mirroring that resolves to a commit SHA first, filters by allow-pattern, digests, uploads and re-verifies |
| `aisvc/gateway` | Key scopes (model, expiry, CIDR), request enforcement through `aisvc.profiles`, metering records with no prompt content, and LiteLLM config generation with canary weights |
| `gateway-hooks/` | The LiteLLM `async_pre_call_hook` adapter and RFC 9457 error mapping |
| `tests/gpu/` | The lab tests for serving end to end and the cold-start budget, marked `gpu` and skipped by default |

### Decisions taken, and why

**`EffectiveParams` is one object used twice.** `test_the_hook_and_the_effective_parameters_view_agree`
asserts the gateway's enforcement and the reporting view produce identical output from the same
inputs. That test is the executable form of ADR-0002's argument, and it would be impossible to
write with an ext_proc service in another language.

**Bounds intersect rather than overriding.** SPEC.md section 6 says a more specific level wins,
but also that users may edit "within bounds". Taken together that has to mean `min`/`max` narrow
monotonically — otherwise an account profile could widen a domain's limit and escape it.

**`default` policy rejects out-of-range values; `clamp` silently bounds them.** The spec
distinguishes the two ("client may override within min/max" versus "client value forced into
range"), so a `default` parameter outside its bounds is a 400 rather than a silent correction.

**Only pickle-*only* repos are refused.** A repo carrying both safetensors and `.bin` mirrors
fine, because the allow-patterns exclude the pickle files anyway.

**GPU tests are deselected by default** via `addopts = "-m 'not gpu'"`; `make test-gpu`
overrides it. Running the whole suite on a laptop should not look like a failure.

### Open questions for the next session

1. **LiteLLM is not yet a dependency.** `gateway-hooks/enforcement.py` imports its base class
   lazily so it works without it, and `build_litellm_config` renders config nobody has fed to a
   real proxy yet. ADR-0002's action items — pin the version, contract-test the hook signatures,
   prove content logging is off in the real thing — all still stand.

2. **Server-side apply is not written.** `aisvc/k8s` renders; nothing applies. The `kubernetes`
   client is in the mypy ignore list but not in `pyproject.toml`.

3. **`architecture_from_config` guesses parameter count from download size** (total bytes / 2,
   assuming bf16 on the Hub). Good enough to rank GPU classes, wrong for a quantized upload.
   Read it from safetensors metadata instead.

4. **Nothing wires the modules together yet.** `plan_capacity` still only plans GPU nodes; it
   does not plan model deployments, so `aisvc apply` will not serve a model. That is the join
   between milestone 3's pieces and milestone 4's API.

## Milestone 4 — Engine API (next)

FastAPI per section 10, async jobs, caller-context filtering, mTLS, OpenAPI contract tests; CLI
switched to the API. Nothing blocks it.
