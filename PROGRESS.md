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

## Milestone 5 — Native API plugin (mostly done)

**State:** 133 JUnit tests pass against real CloudStack 4.23.0.0 classes, **73% line
coverage** — past the 70% the spec sets for the Java plugin. 730 Python tests still pass.

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

### What milestone 5 still needs

1. **22 more v1 commands.** Three of the 25 are written; the rest are mechanical now that
   `AiServicesAsyncCmd` and the response-mapping pattern exist. `aisvc/api/commands.py` is the
   list, and the Python tests track how many lack an engine route.
2. **The account deletion hook is not wired.** `cleanupAccount` works and is tested, but
   CloudStack 4.23 exposes no plugin-facing account-cleanup interface — `AccountCleanupTask`
   is internal to `AccountManagerImpl` in the `server` module. This needs either an upstream
   extension point or an event listener, and it is the one place where "deleting an account
   removes its deployments" (section 14) is not yet true.
3. **The host maintenance listener.** Section 7's drain-before-maintenance flow.
4. **The schema upgrade is not wired.** `schema-ai-services.sql` exists but nothing applies it.
5. **Marvin tests** per default role, allowed and denied. These need a running CloudStack, so
   they belong with a simulator environment rather than this repo's unit suite.
6. The **4.22 LTS compile job** from [ADR-0003](docs/adr/0003-cloudstack-release.md), which
   needs a second CloudStack build.
7. `AiDeploymentDaoImpl` is untested — CloudStack's `GenericDaoBase` needs a database, so it
   wants the integration environment rather than JUnit.
