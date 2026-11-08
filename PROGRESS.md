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

2. ~~**Node allocation is counted per host, not per GPU.**~~ **Answered by
   [ADR-0003](docs/adr/0003-cloudstack-release.md):** CloudStack 4.21+ discovers GPU devices
   per host, so per-GPU accounting comes from CloudStack. `inventory` still infers capacity
   from `host_tag` and so mis-counts `vgpu` and `mig`; moving it onto the GPU device APIs is
   an open action item on that ADR.

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
| [0003](docs/adr/0003-cloudstack-release.md) | Target CloudStack 4.23.0.0; GPU offerings and limits are native, Extensions do not cover the plugin | Accepted 2026-11-07 |

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

### ADR-0003 in one paragraph

CloudStack **4.23.0.0** is the target. GPU and vGPU offerings and the `max.*.gpus` resource
limits have been native since 4.21, so the plugin configures them rather than building a
parallel quota system. The Extensions framework, which section 9 told us to evaluate before
writing bespoke Java, **does not** cover this plugin: its custom actions attach only to
`VirtualMachine`, `Network` and `Vpc`, and it cannot define new first-class resource types
with their own API commands — so milestone 5's Java plugin is necessary, and that question is
closed. It stays relevant for v2 private endpoints, where attaching to a tenant VPC tier is
exactly a `Vpc` custom action.

**One factor to keep in view:** 4.23 is a **non-LTS** release, supported only until the next
regular or LTS release, while 4.22 is the current LTS and 4.24 is planned as the next one.
Providers run LTS. The ADR's mitigation is to hold the plugin's compile-time surface to APIs
that also exist in 4.22 LTS, with a CI job proving it, so retargeting 4.24 LTS for GA costs a
rebuild rather than a rewrite.

**Two findings changed existing code.** GPU devices are discovered natively per host since
4.21; `inventory` now reads `listGpuDevices` rather than inferring capacity from `host_tag`,
which fixes vGPU and MIG counting (see below). And `gpu.detach.on.stop` defaults to false, so
milestone 7's drain must destroy rather than stop, or drained nodes will hold GPUs they are
not using.

### Per-GPU capacity (ADR-0003 action item 3, done)

`inventory` reads `listGpuDevices` and counts allocation from each device's
`virtualmachineid`. Three things fell out of doing it properly:

- **A carved physical GPU is a container, not a unit.** Counting a parent and its vGPU
  children would double-count the same silicon, so only leaf devices count — and a child
  whose parent is missing from a page still counts, so paging can never lose capacity.
- **`free_by_host` and `allocated_by_host` are tracked separately.** A vGPU host is partly
  allocated and partly free at once, which subtracting free from a total cannot express.
  `nodes_allocated` was briefly wrong for exactly this reason.
- **`gpus_per_node` means GPUs per node VM, not per host.** It bounds `tensor_parallel`; the
  host's physical count comes from CloudStack. A contract assertion conflating the two is
  what surfaced the distinction.

`can_host` is now a real answer rather than a guess: it asks whether *one host* has that many
free GPUs, so eight GPUs scattered across four hosts correctly cannot run an 8-way replica.
`gpuClasses[].gpu_card` is new and optional, narrowing counting on hosts with mixed cards.

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

## Milestone 3 — Serving and sourcing (done, less the GPU lab)

`registry`, `hf`, `k8s` rendering, `gateway` with hooks, `profiles`. **Not done:** one model
end to end in a lab zone with the cold-start target validated — that needs GPUs.

**State:** 504 tests pass, `ruff` clean, `mypy --strict` clean, 94% line coverage.
`profiles` is at 99% and `gateway` at 100%, both above the 90% the spec sets for them.

`aisvc apply` now takes an empty zone to a served model in one run: GPU nodes, then model
deployments, then gateway routes, in dependency order, and a second run plans nothing.

### What landed

| Module | What it does |
| --- | --- |
| `aisvc/profiles` | The seven-level precedence chain with `default` / `clamp` / `locked`. Bounds intersect down the chain so a tenant cannot widen an admin's limit; a lock binds every level below it. Backs both `getAiEffectiveParameters` and the gateway hook |
| `aisvc/k8s` | Renders `Deployment`, `Service` and `PodDisruptionBudget` per model per colour (ADR-0001). Host anti-affinity, GPU limits from `tensor_parallel`, `hostPath` weight cache, startup probe sized for the three-minute cold start. Serving profile parameters become engine flags |
| `aisvc/registry` | Content-addressed S3 storage at `<org>/<repo>/<sha>/`, sha256 manifests, verification before load, pickle refusal, per-domain quota accounting. `InMemoryObjectStore` for tests, `S3ObjectStore` for Ceph RGW/MinIO |
| `aisvc/hf` | Compatibility report (architecture, size, context, licence, gating, remote code) with `capacity` saying which GPU classes and TP widths fit; mirroring that resolves to a commit SHA first, filters by allow-pattern, digests, uploads and re-verifies |
| `aisvc/gateway` | Key scopes (model, expiry, CIDR), request enforcement through `aisvc.profiles`, metering records with no prompt content, and LiteLLM config generation with canary weights |
| `gateway-hooks/` | The LiteLLM `async_pre_call_hook` adapter and RFC 9457 error mapping |
| `aisvc/k8s/client.py` | Server-side apply with a fixed field manager, deletion, and reading back what is running. `FakeKubernetes` for tests, `ClusterKubernetes` for real |
| `aisvc/reconciler/state.py` | `ActualState` — CloudStack capacity and Kubernetes workloads observed into one object, as section 4 describes |
| `aisvc/reconciler/deployer.py` | Applies model deployments and writes the gateway config as a ConfigMap |
| `plan_all` | The ordered diff: capacity, models, routes, with dependency edges so a deployment waits for its pool's nodes |
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

### Wiring, and what it exposed

Change detection is by **spec hash**: the rendered manifest is digested and stamped on the
Deployment as an annotation, and the gateway config's digest on its ConfigMap. The next plan
compares digests rather than diffing against a server that fills in its own defaults. That is
what makes the whole-platform version of `test_second_apply_produces_empty_plan` honest.

Two real bugs surfaced while wiring, both now fixed:

**The gateway config would have re-applied on every run.** The integration harness tracked the
route digest itself, which hid the fact that nothing read it back from the cluster. The
harness now observes exactly the way the CLI does, and `observe_route_hash` reads the
annotation. A test harness that is more capable than the product proves nothing.

**A registry outage looked exactly like "nothing is mirrored".** `S3ObjectStore.get` turned
every exception into `KeyError`, and `ModelRegistry.has` turned every `RegistryError` into
`False` — so an unreachable S3 would have silently skipped every model deployment and reported
a clean run. Now only a genuine 404 is `ManifestNotFound`; anything else propagates and fails
the run.

**A model is only deployed once its weights are mirrored.** A deployment pointing at an
unmirrored revision would start, fail to load and crash-loop. Unmirrored models are a warning,
and the nodes are still built, so the mirror job and the hardware can proceed in parallel.

### Open questions for the next session

1. **LiteLLM is not yet a dependency.** `gateway-hooks/enforcement.py` imports its base class
   lazily so it works without it, and `build_litellm_config` renders config nobody has fed to a
   real proxy yet. ADR-0002's action items — pin the version, contract-test the hook signatures,
   prove content logging is off in the real thing — all still stand.

2. **`architecture_from_config` guesses parameter count from download size** (total bytes / 2,
   assuming bf16 on the Hub). Good enough to rank GPU classes, wrong for a quantized upload.
   Read it from safetensors metadata instead.

3. **Only the blue colour is ever deployed.** `ACTIVE_COLOUR` is fixed; green exists in the
   renderer and the gateway config but nothing drives a rollout. That is `lifecycle`, milestone 7.

4. **`ClusterKubernetes` is unexercised.** Every test runs against `FakeKubernetes`; the real
   client's apply-then-create fallback has never touched an API server.

## Milestone 4 — Engine API (done)

FastAPI per section 10, async jobs, caller-context filtering, mTLS, OpenAPI contract tests,
CLI switched to the API.

**State:** 618 tests pass, `ruff` clean, `mypy --strict` clean, 94% line coverage.

```
aisvc serve                      # run the engine API
aisvc status                     # read it back through the API, as the plugin does
```

### What landed

| Module | What it does |
| --- | --- |
| `aisvc/api/context.py` | `CallerContext` and `Role`, and the visibility rules every response is filtered by |
| `aisvc/api/jobs.py` | Async jobs whose three states map straight onto CloudStack's, so the plugin needs no translation |
| `aisvc/api/errors.py` | Every failure as an RFC 9457 problem document, with a stable machine-readable `type` |
| `aisvc/api/deps.py` | Caller context from `X-Aisvc-*` headers, the admin guard, a transaction per request |
| `aisvc/api/app.py` | health, ready, jobs, models, capacity, runs, deployments, endpoints, effective parameters, HF search/check, async mirror |
| `aisvc/api/server.py` | mTLS configuration, with client certificates **required** |
| `aisvc/api/client.py` | The CLI's client, so `aisvc` exercises the same surface the plugin does |

### Decisions taken, and why

**The engine authenticates nothing; the transport does.** The plugin asserts the caller in
headers, which is only safe because the engine is unreachable from tenant networks and
requires a client certificate. So `ssl_cert_reqs` is `CERT_REQUIRED`, never optional, a
half-configured TLS setup refuses to start, and **with no TLS configured at all the engine
binds loopback only** — an engine on every interface trusting asserted headers is exactly what
section 11 forbids.

**A job you may not see returns 404, not 403.** Otherwise job ids could be probed to learn
that another tenant's work exists.

**`aisvc status` goes through the REST API rather than importing the engine's modules.** If a
response shape would break the Java client, it breaks the CLI too, in CI, rather than in
integration.

**A registry outage is 503, never 404.** The distinction drawn in the previous commit is
carried through to the API: "cannot reach the registry" must not reach a tenant as "no such
model". The catalog still lists during an outage, with `mirrored: false`.

### Open questions for the next session

1. **Async jobs run inline.** `POST /v1/models/mirror` returns 202 with a job id, but the
   mirror has already finished by the time it answers. Correct from the plugin's side — it
   polls and gets a terminal state — but a real mirror takes minutes, so this needs a worker
   before it meets anyone's timeout.

2. **Only read endpoints and mirror exist.** The section 9 command map lists roughly thirty v1
   commands; deployments, keys, profiles, budgets, playground, drain and the rest are absent.
   Several need spec writeback via Git commit (section 10), which is unwritten.

3. **`schemathesis` is installed but unused.** The contract tests assert the schema's shape by
   hand. Property-based fuzzing against the schema is the obvious next step.

4. **mTLS is configured but never exercised.** `uvicorn_options` is unit-tested; no test has
   completed a handshake with a client certificate.

## Milestone 5 — Native API plugin (commands done)

**State:** **all 35 v1 commands** from section 9 are implemented and registered. 361 JUnit
tests pass against real CloudStack 4.23.0.0 classes at **74% line coverage**, past the 70% the
spec sets. 730 Python tests still pass.

```
make test-java
```

### Getting a build environment

CloudStack publishes **no Maven artifacts** — absent from Maven Central and from the ASF
release and public repositories. Its modules must be built from source into `~/.m2` first;
`java-plugin/README.md` has the two commands, and the CI job caches the result.

JDK 17 and Maven are required (`brew install openjdk@17 maven`). `openjdk@17` is keg-only, so
`JAVA_HOME=/opt/homebrew/opt/openjdk@17`. The plugin targets **JDK 11 bytecode**, which is
what 4.23 sets as `cs.jdk.version`.

### What landed

| Class | What it does |
| --- | --- |
| `AiServicesConfig` | Section 9's ConfigKeys. Enablement zone-scoped, Hugging Face approval domain-scoped |
| `AiServicesService` / `AiServicesManagerImpl` | `PluggableService` and `Configurable`: command registration and settings |
| `EngineClient` | Calls the engine, failing over across replicas |
| `HttpEngineTransport` / `MutualTls` | The mTLS transport |
| `CallerContext` | The identity the engine filters every response by |
| `EngineJob` / `EngineJobPoller` | The async bridge: polls `/v1/jobs/{id}` until the engine job leaves pending |
| `AiServicesAsyncCmd` | Base class turning a CloudStack async job into an engine job |
| `AiServicesEvents` / `AiServicesUsageTypes` | Section 9's thirteen action events and five usage types |
| `AiDeploymentVO` / `AiDeploymentDao` | The ownership row and its DAO |
| `ListAiModelsCmd`, `DeployAiModelCmd`, `DeleteAiModelDeploymentCmd` | Three commands, sync and async |
| `schema-ai-services.sql` | The `ai_deployment` ownership table |

### Decisions taken, and why

**Failover retries only unreachable replicas.** A replica that answers — even with a 500 —
has answered, and its answer is returned rather than retried elsewhere: repeating a POST
against the next replica could do the work twice. Only a connection failure raises the
`isUnreachable` form of `EngineException`, which is the one thing `EngineClient` moves on
from.

**The caller context is built from `CallContext`, never from request parameters.** The engine
authenticates nobody and filters every response by this assertion, so a caller able to
influence it could read another tenant's data. `CallerContext` refuses to construct with any
field blank.

**mTLS failures refuse rather than fall back.** An unconfigured or unreadable keystore throws;
connecting without a client certificate would let anything that reaches the engine claim to be
any tenant.

**The pom is standalone rather than inheriting `cloudstack-plugins`,** so the module builds
from this repository without a CloudStack checkout on the path. Upstreaming swaps it for the
usual `<parent>` block.

**The ownership table is deliberately minimal** — uuid, account, domain, zone, engine id,
state. The engine stays the source of truth for runtime state; this exists only so CloudStack
can enforce ownership, filter lists and clean up on deletion.

### More decisions

**A 202 is the contract for an async command.** Anything else means the engine did not accept
it, and treating a 200 as success would report work that never started.

**A poll timeout is never reported as success.** The engine job may still be running, and
calling it done is a lie the operator would act on.

**Engine errors map onto CloudStack error codes rather than collapsing to one.** A 403 is a
permission decision, and surfacing it as an internal error would hide from the tenant why
their call failed.

**Usage type ids start at 100.** CloudStack's own stop well below that, so a provider's
billing pipeline can tell ours apart without ambiguity.

**`cleanupAccount` removes only ownership rows.** The engine owns the running workloads and
removes them when it next reconciles, because the spec they came from no longer lists them;
deleting in both places would race.

**The domain path is resolved from the domain DAO** — the placeholder is gone. The engine
decides domain-admin visibility by prefix-matching it, so a partial path would hide a domain's
own resources or expose a sibling's. An unresolvable domain now refuses rather than guessing,
and CloudStack's trailing slash is stripped because the engine compares prefixes.

### Two things the compiler caught

Worth recording, because both would have shipped as plausible-looking nonsense had the plugin
been written without a build: `ConfigKey` has **no `getScope()`** in 4.23 — it is `getScopes()`
returning a list — and `com.sun.net.httpserver.Headers` only offers `getFirst` on its own type,
not through the `Map` supertype.

### The commands are generated

Thirty-five commands and a dozen response types differ only in name, verb, path, parameters
and response shape. Hand-writing them invites the one transposed path or wrong role no
reviewer would catch, so the leaves come from `tools/generate_commands.py` and the interesting
parts — base classes, engine client, async bridge, DAO — stay hand-written. Generated files
say so in their header.

Three base classes carry the behaviour: `AiServicesSyncCmd` (call, check, map),
`AiServicesListCmd` (extract the collection, map each item) and `AiServicesAsyncCmd` (202,
then poll). A command is then a path, a verb, its parameters and a response type.

The generator also emits a test fixture — a fully-populated engine object per response type —
so `GeneratedSurfaceTest` checks every mapped field, every path placeholder being substituted,
every body being valid JSON and every read carrying no body, across all 35 commands, without
that list existing twice.

`CommandRegistrationTest` holds the registration to section 9's table in both directions:
nothing missing, nothing extra, the async set exactly the twelve the spec marks async, and
every async command going through our bridge rather than extending `BaseAsyncCmd` directly and
skipping the 202 check.

### What milestone 5 still needs

1. **The account deletion hook is not wired.** `cleanupAccount` works and is tested, but
   CloudStack 4.23 exposes no plugin-facing account-cleanup interface — `AccountCleanupTask`
   is internal to `AccountManagerImpl` in the `server` module. This needs either an upstream
   extension point or an event listener, and it is the one place where "deleting an account
   removes its deployments" (section 14) is not yet true.
2. **The host maintenance listener.** Section 7's drain-before-maintenance flow.
4. **The schema upgrade is not wired.** `schema-ai-services.sql` exists but nothing applies it.
5. **Marvin tests** per default role, allowed and denied. These need a running CloudStack, so
   they belong with a simulator environment rather than this repo's unit suite.
6. The **4.22 LTS compile job** from [ADR-0003](docs/adr/0003-cloudstack-release.md), which
   needs a second CloudStack build.
6. `AiDeploymentDaoImpl` is untested — CloudStack's `GenericDaoBase` needs a database, so it
   wants the integration environment rather than JUnit.
7. The commands are tested for shape, not behaviour against a real engine. That is what the
   Marvin tests and the integration environment are for.

## Milestone 7 — Operations (lifecycle and autoscale wired in)

`autoscale`, `lifecycle` and `metering`, plus the engine endpoints the plugin's commands were
calling into thin air.

**State:** 845 Python tests pass, `ruff` clean, `mypy --strict` clean, 94% line coverage.
361 Java tests still pass.

`lifecycle` and `autoscale` are no longer decision functions nobody calls: the reconciler
drives both. A spec change on a blue/green model brings the other colour up beside the one
serving, and replica counts follow the metrics inside the declared band.

### What landed

| Module | What it does |
| --- | --- |
| `aisvc/lifecycle` | Blue/green rollout as a small state machine producing traffic weights, and `plan_drain` |
| `aisvc/metering` | Usage records, per-tier pricing, and budget thresholds |
| `aisvc/autoscale` | Replica scaling on queue depth, TTFT p95 and GPU utilisation |
| Engine API | Rollout promote and rollback, node drain, usage and budgets |

**19 v1 commands still have no engine route**, down from 25. The count is pinned by a test, so
it cannot drift quietly.

### Decisions taken, and why

**A rollout never swaps `active` and `candidate`.** The first cut did, and two tests caught
that it makes the pair ambiguous — the same two fields meaning different things depending on
how the rollout got there. `active` is what was live when the rollout started, `candidate` is
the new colour, neither moves, and `live_colour` answers "what is serving now?".

**Rollouts are immutable.** `promote()` and `rollback()` return a new rollout, so a caller
cannot half-apply a transition and leave the weights disagreeing with the phase.

**Draining destroys rather than stopping**, closing [ADR-0003](docs/adr/0003-cloudstack-release.md)'s
action item — a stopped node keeps its GPUs allocated and strands capacity nobody is using. A
drain without room elsewhere refuses until an admin forces it, and a forced drain says in its
reason what it is costing.

**Input and output tokens are priced separately.** Output runs about three times input in the
example tiers, so a blended rate would misbill every tenant in one direction or the other.

**A budget threshold fires once.** `already_notified` means a tenant gets one alert at 80%,
not one per request.

**Scaling is deliberately asymmetric** — up after a minute, down after fifteen, one replica at
a time. Loading weights costs minutes even with the NVMe cache, so flapping is far more
expensive than carrying a spare replica; and adding three at once just queues three cold
starts behind the same bottleneck. A breach outranks idleness, because being over SLO is
tenant-visible while a spare replica is only cost.

### ADR-0003's LTS mitigation is verified

The plugin **compiles and passes all 361 tests against 4.22.1.1 LTS** as well as 4.23.0.0, so
nothing 4.23-only has crept in. A CI job runs it on every change, so the eventual retarget
stays a rebuild rather than a rewrite.

### Wiring them in, and what it exposed

**The live colour is derived, not stored.** `ActualState.live_colour` reads the gateway's own
weights back from the ConfigMap, so there is no rollout state to fall out of step with
reality — the same argument as the spec hash. A promotion is therefore just a weight change
the next plan notices.

**Scaling is not a spec change, and finding that out took a bug.** The first wiring folded the
replica count into the spec hash, so autoscaling to three replicas looked like a configuration
change and started a blue/green rollout over a number. `config_hash` now renders at a
normalised replica count and the deployer stamps that, while replica drift is handled as an
in-place update. The two concepts were conflated and the tests separated them.

**Scaling is skipped mid-rollout.** Moving replicas while traffic is shifting makes both
harder to reason about, and neither is urgent enough to do at once.

**A raised replica floor still takes effect without metrics.** The first version kept whatever
was deployed, which silently ignored a spec change to `replicas.min`.

### What milestone 7 still needs

1. **Metering has no driver.** Pricing and budget logic are tested, but nothing pulls usage
   from the gateway hourly or writes CloudStack usage records. Section 14's "usage reconciles
   to gateway token counts within 1%" cannot be checked until something meters.
2. **No Prometheus client.** `DeploymentHealth` is a dataclass the reconciler reads; nothing
   fills it in, so `aisvc apply` never autoscales in practice. The decision logic is driven
   and tested; the data source is not written.
3. **Promote and rollback write no weights yet.** The API endpoints compute the split and
   record it on a job, but nothing applies it to the gateway ConfigMap — so a real promotion
   still needs the operator to change the weights.
4. **Alerts and Grafana dashboards** (section 11) are unwritten.


## Milestone 8 — Tier 1 operability (setup wizard)

**Status:** the `setup` module is in and reachable over the API. The rest of
milestone 8 — maintenance integration, playground, scoped keys, budgets, engine
HA, Terraform — is not started.

Chosen first because it is section 14's opening acceptance criterion ("a new
zone goes from empty to a passing self-test using only the setup wizard") and
because `runAiSetupSelfTest` had been a generated command pointing at an
endpoint that did not exist.

### What landed

- `aisvc/setup/` — preflight checks, GPU-class discovery and the self-test plan.
  30 unit tests, 98% line coverage.
- Three engine endpoints: `GET /v1/setup/preflight`, `GET /v1/setup/gpu-discovery`,
  `POST /v1/setup/selftest`, with 10 API tests covering shape, RBAC and refusals.
- Two new CloudStack commands, `listAiSetupChecks` and `discoverAiGpuClasses`,
  generated into the Java plugin like the rest. 377 Java tests still pass.

### Every failing check carries a remedy

An operator working through a wizard cannot read the source to find out what
"no capacity observed" means. So `CheckResult` has a `remedy` field, and a test
asserts every `fail` fills it in. The GPU checks go furthest: CloudStack reports
neither IOMMU nor the nouveau driver, but a tagged host with no GPU devices is
exactly what both look like, so that check names them (ADR-0003 action item 5).

Checks also all run even after one fails, so a misconfigured zone is fixed in
one pass rather than one problem per round trip.

### The spec's command table was incomplete

Section 9 lists only `runAiSetupSelfTest`, but section 7 describes a wizard
whose steps "validate live" and whose GPU discovery "reads host tags" — neither
reachable from the UI without a command. The drift guards caught this from both
ends: the Python test found engine routes with no command, and the Java
`CommandRegistrationTest` found commands not in the spec's table.

Amended SPEC.md section 9 to add both rather than loosening either guard. The
guards exist to make the spec and the code disagree loudly; silencing them
would have left the wizard unbuildable in milestone 6.

### Three bugs the tests found after the code was written

The module's unit tests passed before any of these surfaced, which is the
argument for testing at the layer the caller actually uses.

1. **An unknown zone returned 202 with a failed job.** Creating a job for a
   typo fills the run log with validation errors and hands the caller a job id
   for work that was never going to run. Now validated before the job exists,
   returning 404 like `/v1/profiles/effective?model=ghost` already did.

2. **Preflight 503'd when capacity had not been observed.** Which is every
   brand-new zone — exactly the case section 14 asks the wizard to handle. The
   capacity checks now come back `skipped` with a remedy naming what to run
   first, and the spec and registry checks still run. `CheckStatus.SKIPPED` had
   been defined and never produced, which was the tell.

3. **Unexpected exceptions returned plain-text 500s.** Found by smoke-testing
   against a real server with no Postgres running: the engine answered
   `text/plain` "Internal Server Error", which the plugin's client cannot parse.
   Every *known* exception had a handler; nothing caught the rest. Added a
   catch-all returning problem JSON with a deliberately generic detail — the
   exception text had the database connection string in it, password included —
   and logging the real one server-side. This was an API-wide gap, not a setup
   one.

### Verified, not assumed

`aisvc serve` on loopback, against `examples/platform.yaml`:

- `/v1/setup/preflight` → 200, spec check passes, registry check fails with
  "Unable to locate credentials" and its remedy, both GPU classes skipped.
- `/v1/setup/gpu-discovery` → 200, no classes (nothing observed).
- `/v1/setup/selftest?zone=ghost` → 404 problem JSON.
- The same as a tenant → 403.
- `/v1/setup/selftest?zone=zone-east-1` with Postgres down → 500 problem JSON,
  no connection string in the body.

### What milestone 8 still needs

The self-test **plans** its five steps and records them on the job; it does not
run them. Mirroring, deploying, sending a request and checking the usage record
all need a zone with GPU capacity, and metering has no driver yet (milestone 7's
outstanding gap), so step 4 could not pass today even with hardware. Section
14's first criterion is therefore **not yet met** — the wizard reports honestly
on a zone, but cannot take one from empty to serving on its own.

Also outstanding: maintenance integration, playground, scoped keys, budgets,
engine HA and Terraform.

## Milestone 8 — Tier 1 operability (the rest)

**Status:** all seven of section 7's features now have code and tests.
Three have limits that are worth stating plainly rather than burying,
and they are below under "What is still not true".

1020 Python tests, 399 Java, 21 Go. `make test-tf` runs for the first
time instead of exiting 1.

### What landed

- **Scoped keys.** `mint_key` / `KeyStore`: minted once, stored as a
  per-key salt and HMAC, verified in one indexed lookup. Scopes are
  checked at mint time as well as at the gateway, so a key that could
  never be honoured is refused while the operator is still looking at
  the form. Rotation is overlapping keys, which falls out of issuing a
  second one; `/v1/keys` POST, GET and DELETE back it.
- **Budgets.** `UsageStore` holds usage and budgets, and the gateway
  hook refuses a blocked account with 402 before the request goes
  upstream. 402 and not 429: this is not a rate to wait out.
- **The metering driver.** This was milestone 7's outstanding gap —
  nothing produced usage records. `AisvcEnforcement.async_log_success_event`
  now does. That hook rather than `async_post_call_success_hook`
  because it fires for streaming responses too, and the playground
  streams; the name was checked against LiteLLM's source, not guessed.
- **Playground.** A 15-minute, single-model session key plus snippets
  for curl, httpx and the OpenAI SDK in both languages.
- **Engine HA.** `ZoneLeadership` takes a Postgres advisory lock per
  zone, wired into `aisvc apply`. The Java side's multi-URL failover
  was already there from milestone 5.
- **Host maintenance.** `MaintenanceDrain` asks the engine to drain
  each GPU node on a host before it enters maintenance, because
  passthrough GPUs cannot live-migrate (ADR-0003).
- **Terraform.** Five resources and a data source in `terraform/`,
  with import support.

### CloudStack 4.23 has no pre-maintenance hook

Checked rather than assumed, and all three candidates fail:

- `ResourceStateAdapter` covers host create and delete only.
- The agent `Listener` covers connect, disconnect and removal.
- `ResourceState` is driven by the older `StateMachine`, which — unlike
  `StateMachine2` — registers no listeners.

That leaves the event bus, which is asynchronous (the host has already
begun its transition) and **not enabled in a default install**: no
event-bus module is in `client/pom.xml` and the in-memory one ships no
`module.properties`.

So `AiMaintenanceListener` subscribes where it can and, where it cannot,
logs at startup exactly what is inactive and that `drainAiNode` must be
run by hand first. A listener that silently never fires would be worse
than none, because an operator would believe drains were automatic.

This is the same shape as milestone 5's account-cleanup gap, and it
should go to upstream CloudStack as a feature request rather than be
worked around here.

### Which GPU nodes are on a host is CloudStack's question

The engine does not track node-to-host — `PoolCapacity` holds node names
only. Rather than teach it, the plugin asks CloudStack directly
(`VMInstanceDao.listUpByHostId`, filtered by the `aisvc-` prefix).
CloudStack is the IaaS authority and already knows what runs where; the
engine would have to be told the same thing to answer it.

### Usage and budgets were reading stubs

`/v1/usage` and `/v1/budgets` read `getattr(services, "usage")` and
`getattr(services, "spend")` — attributes nothing ever set. Both read
from the stores now, and four tests that set those attributes were
rewritten to write real rows. One of them had been passing for the wrong
reason: it asserted another tenant saw nothing, which was true because
*nobody* saw anything.

Budgets now have two declarations — `platform.yaml` as desired state and
the database as what is in force — so the list endpoint merges them with
the stored one winning, and reports which it used in `source`. Without
that, raising a limit through the API would appear to do nothing.

### A path parameter that collided with a header

`PATCH /v1/budgets/{account}` would not register at all: FastAPI matches
dependency parameters to path placeholders by name, `caller_context`
takes an `account` header, and so it read that header parameter as a
path parameter and refused the route for having a default. Renamed to
`{budget_id}`. Worth knowing before adding `/v1/.../{domain}` or
`{role}`, which would do the same thing.

### Verified, not assumed

Against `aisvc serve`, after `alembic upgrade head` (0003 applies clean):

- `POST /v1/keys` → the secret, once, with its warning.
- `GET /v1/keys` → metadata, no secret, `lastused` null until used.
- `POST /v1/playground/sessions` → a 15-minute key and four snippets,
  none of which contains the key.
- `POST /v1/budgets` as admin → 200; as a tenant → 403.
- `GET /v1/budgets` → the stored budget, `source: api`.

`make test-tf`, `make check` and the Java suite all pass.

### What is still not true

- **The self-test still only plans.** Its five steps need GPU capacity,
  so section 14's first criterion is not met. The metering step could
  pass now that there is a driver; the rest cannot without hardware.
- **Automatic drain is off in a default install**, per above. The
  DCGM XID/ECC auto-quarantine in section 7 is not written at all —
  nothing reads DCGM yet.
- **Budget notifications are half done.** `take_notifications` returns
  the newly crossed thresholds exactly once, but nothing sends them:
  no CloudStack event, no email, no HMAC-signed webhook.
- **The playground console is a UI screen** (milestone 6). The engine
  side of it is done.
- **The Terraform acceptance tests have never been run.** They need the
  simulator from `make test-int`, which is not part of `make check`.
  Nothing in `terraform/` has touched a live management server, so
  treat the command and response-key names there as unverified.
- **Engine HA's operational half** — Postgres in HA, daily backups, a
  quarterly restore drill — is deployment work, not code, and belongs
  with `deploy/`.
