# PROGRESS

## Milestone 1 — Foundation (done)

Scaffold, CI, `spec` models and validation, CloudStack client with recorded responses,
`inventory`, `capacity`, `aisvc plan`.

**State:** 121 tests pass, `ruff` clean, `mypy --strict` clean, 95% line coverage.

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

### Open questions for the next session

1. **The `max_model_len x max_num_seqs` rule rejects SPEC.md's own example.** Read literally
   (section 3, serving profile validation), `h100-throughput` with `max_num_seqs: 256` against
   `llama-3.3-70b-instruct` at 32768 context needs ~8.4M tokens of KV cache; two H100s hold
   about 13 full-context sequences. A paged engine admits sequences as they fit rather than
   reserving full context for each, so the literal rule would block every realistic config.
   Implemented as a **warning**, not an error. Needs a decision: drop the rule, re-express it
   against an average context fraction, or keep it strict and lower the example's `max_num_seqs`.

2. **Request signing uses `quote_plus`** (spaces as `+`), matching CloudStack's Java
   `URLEncoder`. Worth confirming against the target release before milestone 2, since
   `quote` (spaces as `%20`) appears in some clients. Also unverified against a live
   management server — only against the shape pinned in `tests/fixtures/cloudstack/`.

3. **Node allocation is counted per host, not per GPU.** A host counts as allocated when it
   runs a VM with the class's service offering. That holds for passthrough; `vgpu` and `mig`
   classes will need per-GPU accounting.

4. The eight open decisions in SPEC.md section 14 are untouched. None blocked milestone 1;
   the serving-layer and gateway choices will block milestone 3.

## Milestone 2 — Provisioning (next)

`provision` for VMs and CKS pools, cloud-init and Packer golden template, `aisvc apply` for
node pools, Postgres run log. Requires: a target CloudStack release pinned, and recorded
`deployVirtualMachine` / `scaleKubernetesCluster` responses from a lab zone (`make record`).
