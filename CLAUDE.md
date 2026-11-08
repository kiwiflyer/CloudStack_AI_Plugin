# CLAUDE.md

Guidance for Claude Code working in this repository.

## Current state

**Milestones 1–8 are done bar their noted gaps. Milestone 9 (v2) serves adapters,
embeddings and rerank, runs guardrails and batches, gates publishing, and fronts a
deployment privately — the reconciler plans, creates and destroys the proxy VM.**
See `PROGRESS.md`.
Working today: `aisvc/spec`, `capacity`, `cloudstack`, `inventory`, `provision`, `db`,
`reconciler`, `profiles`, `k8s`, `registry`, `hf`, `gateway`, `api`, `setup`, `playground`,
`observability`, the `aisvc` CLI
(`validate`, `plan`, `apply`, `status`, `serve`), `gateway-hooks/` and `templates/packer/`.

v2 is reachable through the engine API and the CLI, not through CloudStack: its
commands are `Release.V2` and the Java generator emits V1 only. No CloudStack has run the proxy provisioner and no
proxy has booted. Uploaded certificates are watched and can be
replaced, but renewing one replaces its proxy VM — downtime, because a proxy fetches
its material once at boot. ACME endpoints are probed on each apply, so a
failed renewal is seen; nothing acts on it automatically, and a private-only endpoint
may be unreachable from where the engine runs. Nothing fetches an adapter's
artefacts, produces an evaluation or calls the guard model. The batch
worker (`aisvc batch`) has no leader election, so run one.

**A Kubernetes LoadBalancer Service cannot be placed on a chosen network.** The
CloudStack cloud-controller-manager takes a service's network from the nodes' first
NIC; `loadBalancerIP` only picks among public IPs the account already holds. That is
why a private endpoint is a proxy VM on the tenant's tier and not a Service. `ui/` has the section, screens and strings but has
never been loaded by a real CloudStack UI, and its Playwright specs have never run. `terraform/` builds and unit-tests but
its acceptance tests have never run against a live management server.
`setup` plans the self-test but cannot run it: the steps need GPU capacity, so
section 14's first acceptance criterion is not yet met. Host maintenance drains
automatically only where an event bus is configured — CloudStack 4.23 offers no
synchronous pre-maintenance hook, and no bus is enabled by default.
`lifecycle` and `autoscale` are driven by the reconciler, and `observability` now fills
`DeploymentHealth` from Prometheus — autoscaling is opt-in on `AISVC_PROMETHEUS_URL` and
holds at the replica floor without it. The engine exports its own counters on `/metrics`.
`make record` deliberately exits 1 naming the milestone that fills it in.

`aisvc apply` takes an empty zone to a served model in one run — nodes, then deployments, then
routes — and a second run plans nothing. **Still missing:** LiteLLM as a real dependency, any
rollout between blue and green, and a single test against a real API server.

**The engine API trusts its callers' asserted identity**, which is only safe because mTLS
proves who they are. Never weaken `ssl_cert_reqs`, and never bind a non-loopback address
without TLS configured — `aisvc/api/server.py` enforces both. Caller context arrives in
`X-Aisvc-*` headers and every response is filtered by `CallerContext.may_see`.

**Change detection is by config hash.** The rendered manifest is digested **at a normalised
replica count** and stamped as `aisvc.io/spec-hash`; the gateway config's digest goes on its
ConfigMap. Replicas are deliberately excluded: scaling is an in-place change, and folding it
in made every autoscale look like a spec change and start a rollout. Anything that changes
rendering must flow through `config_hash` or convergence breaks silently.

**Which colour is live is derived from the gateway's weights**, never stored — so there is no
rollout state to desync. A spec change on a `bluegreen` model creates the other colour; the
live one keeps serving until an operator promotes.

Before claiming a command works, run it. Before referencing a module, confirm the file exists.

## What is being built

"CloudStack AI Services": a native Apache CloudStack plugin backed by a Python engine that lets
providers source open-weight LLMs from Hugging Face, deploy them on NVIDIA GPU capacity, tune them
through profiles, and sell them to tenants as OpenAI-compatible endpoints.

Division of responsibility — keep this straight, most design mistakes come from blurring it:

- **CloudStack** is the IaaS authority and supplies GPU capacity. Never bypass it for scheduling.
- **CKS (Kubernetes)** serves the models.
- **The Python engine** reconciles desired state (`platform.yaml`) against actual state. It is the
  only thing that touches CloudStack GPU resources.
- **The Java plugin** is the front door: auth, RBAC, async jobs, events, usage, limits. It is the
  only caller of the engine API besides the operator CLI. The engine is never exposed to tenants.
- **The gateway** (LiteLLM, pending decision) serves tenant-facing OpenAI-compatible traffic and
  enforces keys, scopes, profiles, quotas, budgets and guardrails.

The UI never touches CloudStack GPU resources directly — every change goes through the engine.

## Non-goals

Training/fine-tuning pipelines (tenants bring LoRA adapters), multi-node tensor parallelism (a
replica is capped at one 8-GPU host), and replacing CloudStack scheduling.

## Planned layout

```text
SPEC.md                  # the technical specification
CLAUDE.md  PROGRESS.md  pyproject.toml  Makefile
aisvc/                   # Python engine, one package per module
  spec/ cloudstack/ inventory/ capacity/ provision/ k8s/
  hf/ registry/ profiles/ gateway/ autoscale/ lifecycle/ metering/
  setup/ playground/ reconciler/ api/ cli/ db/
  adapters/ batch/ guardrails/ evaluation/      # v2 only
gateway-hooks/           # LiteLLM custom handlers
java-plugin/             # plugins/ai-services — the product surface
ui/                      # aiservices section for the CloudStack ui/ tree
terraform/  templates/  deploy/
tests/                   # unit/ contract/ integration/ gpu/ load/ chaos/ security/ mocks/ fixtures/
test/integration/plugins/ai-services/           # Marvin tests
examples/platform.yaml
```

## Commands

**Building `java-plugin/` needs setup.** CloudStack publishes no Maven artifacts, so its
modules must be built from source into `~/.m2` first — `java-plugin/README.md` has the two
commands. JDK 17 (`JAVA_HOME=/opt/homebrew/opt/openjdk@17`, keg-only), targeting JDK 11
bytecode. `make test-java` fails with a clear message if the artifacts are absent.

```
make install        # editable install into .venv
make check          # lint + types + unit + contract + integration + security
make test-unit      # pytest, no GPU, no network
make test-contract  # schemathesis + recorded CloudStack responses
make test-int       # docker compose: CloudStack simulator, kind, MinIO, Postgres, LiteLLM, mocks
make test-java      # JUnit 5; needs CloudStack artifacts in ~/.m2
make test-ui        # Vitest + Playwright
make test-tf        # Terraform acceptance against the simulator
make test-security  # licence policy; add pip-audit, bandit, semgrep later
make test-gpu       # GPU lab only, not in CI
make record         # refresh recorded fixtures against a lab zone — only deliberately
```

Operator CLI (Typer). Working now:

```
aisvc validate --spec examples/platform.yaml --licenses licenses.yaml
aisvc plan --spec examples/platform.yaml --no-observe
```

```
aisvc apply --spec examples/platform.yaml --dry-run
```

`--observe` (the default) needs `CLOUDSTACK_API_URL`, `CLOUDSTACK_API_KEY` and
`CLOUDSTACK_SECRET_KEY`. `apply` additionally needs `AISVC_DATABASE_URL` and the cluster join
details `AISVC_CKS_JOIN_ENDPOINT`, `AISVC_CKS_JOIN_TOKEN`, `AISVC_CKS_CA_CERT_HASH` — secrets
come from the environment, never from `platform.yaml`. Planned: `status`, `drain <host>` (m7),
`mirror <repo>` (m3), `effective <deployment>` (m3).

**GPU nodes are named `aisvc-<pool>-<ordinal>`.** That naming is what makes provisioning
idempotent — pool membership is read back from it, so never change the scheme without a
migration path for nodes already deployed. A pool is sized in node VMs (`PoolCapacity`);
physical host capacity is the separate `ClassCapacity`.

**Units and conventions worth knowing before you touch sizing:** memory is GiB throughout
(`gpu_mem_gb: 80` means 80 GiB); parameter counts are decimal billions; a model's optional
`architecture` block (`params_b`, `num_layers`, `num_kv_heads`, `head_dim`) is what capacity
sizes from, and milestone 3 fills it in at mirror time.

## Conventions

**Python.** 3.12, fully typed, `mypy --strict`, ruff. Async wherever it talks to an external API.
Each module exposes a narrow interface so it can be unit-tested against fakes.

**Tests first.** Write the test before the implementation. A milestone is done only when its tests
pass in CI. Every bug gets a regression test before the fix. External services are always faked in
unit tests — never hit the real CloudStack, Hugging Face or S3 from a unit test.

**Coverage targets.** 90% line and branch on `spec`, `capacity`, reconciler diffing, `profiles`
resolution and gateway enforcement hooks. 80% overall for the engine, 70% for the Java plugin.
Every native command needs a Marvin test per default role, allowed and denied.

**Idempotence and dry-run.** Every action is idempotent and supports `--dry-run`. A second `apply`
must produce an empty plan. `--dry-run` must make zero API writes. Every action is logged with a
plan diff.

**Reconciler shape.** `observe() -> diff() -> plan() -> execute()`, in dependency order: capacity,
models, profiles, routes. A failed action stops only its dependants. Every run is recorded in
Postgres. One leader per zone via a Postgres advisory lock.

**State.** Postgres holds runs, actions, usage, budgets, profiles, hashed keys, mirror requests and
evaluations. Nothing lives only in memory. `platform.yaml` is the source of truth for desired state
and is updated by Git commit when changes come in through the UI or API.

**Security — non-negotiable.**
- Only the mirror worker has internet egress; inference pods have none, enforced by network policy.
- Pickle weights (`*.bin`, `*.pt`) and `trust_remote_code` are blocked by default.
- Model `source` must pin a commit SHA; artifacts verified by sha256 before load.
- Licenses enforced against an allow-list at both mirror and deploy time.
- Secrets come from a secrets manager or Kubernetes Secrets, never `platform.yaml`.
- API keys stored as salted hashes, shown once. Playground keys expire in 15 minutes.
- Engine↔plugin over mTLS; the engine is unreachable from tenant networks.
- **One endpoint takes no caller context**: `POST /v1/certificates/bootstrap`, where
  a proxy redeems a one-time token for its TLS material. It cannot — the proxy has no
  identity at first boot. The token is single-use, expires in 30 minutes, is bound to
  one certificate and is stored hashed. Do not add a second such endpoint.
- Prompts and outputs are never logged by default. Guardrail blocks record the category only.

**Profile precedence** (most specific wins; a `locked` value beats every level below it):
platform defaults → model defaults (from `generation_config.json`) → domain → account →
deployment → API key → the request itself, within merged bounds. Policies are `default`, `clamp`
or `locked`. `getAiEffectiveParameters` must report both the resolved value and its source level.

**Passthrough GPUs cannot live-migrate.** Drain and replace nodes; never attempt migration.
Drain must **destroy**, not stop: `gpu.detach.on.stop` defaults to false, so a stopped node
keeps its GPU allocated (ADR-0003).

**API conventions.** Async calls return `202` with a job id mirroring CloudStack async job states.
Every engine request carries `{user_id, account, domain_path, role}` set by the plugin from the
CloudStack session, and every response is filtered by it. Errors use RFC 9457 problem JSON.
Breaking changes require a new version prefix.

**Java plugin.** State-changing commands extend `BaseAsyncCmd` and poll `/v1/jobs/{id}`. Keep the
ownership table minimal (`ai_deployment`) so CloudStack enforces ownership, list filtering and
cleanup natively — the engine stays the source of truth for runtime state. Before writing bespoke
Java, check whether the target release's Extensions framework already covers the action.

**UI.** Standard CloudStack list/detail/action patterns so the section inherits search, pagination,
async job notifications and role-based action visibility. Everything in the UI must also work
through CloudMonkey and Terraform.

## Milestones

Build one per session, each ending with green tests and a `PROGRESS.md` update. Each depends on
the previous. 1–8 are v1 GA; 9 is v2.

1. ~~Foundation~~ — **done**, see `PROGRESS.md`
2. ~~Provisioning~~ — **done**, see `PROGRESS.md`
3. Serving and sourcing — `registry`, `hf`, `k8s` rendering, `gateway`, serving + generation profiles
4. Engine API — FastAPI, async jobs, caller-context filtering, mTLS, contract tests
5. Native API plugin — v1 commands, ConfigKeys, async bridge, events, roles, usage, deletion hooks
6. Native UI section — v1 screens, tenant first, i18n, Playwright per role
7. Operations — `autoscale`, `lifecycle`, `metering`, alerts, dashboards
8. Tier 1 operability — setup wizard, maintenance integration, playground, scoped keys, budgets, engine HA, Terraform
9. Tier 2 — multi-LoRA, embeddings/rerank/vision, batch, guardrails, evaluation gate, private endpoints

Work from the spec's section numbers; require `aisvc plan --dry-run` output against
`examples/platform.yaml` before any apply logic merges.

## Decisions already made

Recorded as ADRs in `docs/adr/`. Do not relitigate them without reading the ADR first.

- **ADR-0001 — serving layer:** plain Kubernetes `Deployment`s rendered from Jinja by `k8s`.
  No KServe, llm-d or vLLM production stack in v1. Blue/green canary weighting lives at the
  gateway, not in the serving layer. Revisit only if the one-replica-per-host cap is lifted.

- **ADR-0002 — gateway:** LiteLLM Proxy. Enforcement hooks live in `gateway-hooks/` and
  **import `aisvc.profiles` directly** — never reimplement profile precedence, in any
  language. Stay on LiteLLM's MIT side: nothing from `enterprise/`, no licence-gated features.

**In the Java plugin:** build the caller context from `CallContext`, never from request
parameters — the engine filters every response by what the plugin asserts. Never weaken the
mTLS requirement to a fallback. Failover retries only *unreachable* replicas; a replica that
answered has answered, and retrying a POST elsewhere could do the work twice. Commands and
role permissions are generated from `aisvc/api/commands.py`, not hand-listed.

## Licensing — enforced, not advisory

This is an Apache CloudStack plugin, so the dependency tree follows the ASF Category A/B/X
policy: **GPL, LGPL and AGPL are prohibited**, including for Python imports. `make
test-security` fails CI on any Category X dependency, any licence it cannot classify, and any
new Category B package not already listed. Before adding a dependency, check its licence;
`pg8000` is the Postgres driver because `psycopg` is LGPL. See `docs/third-party-licenses.md`,
which also covers what the scan cannot see: the NVIDIA driver EULA on the golden image, model
licences (`llama3.x`, `gemma`, `qwen`, `openrail` are open-weight with use restrictions, not
open source), and LiteLLM's commercial `enterprise/` directory.

## Open decisions — do not silently pick one

The spec leaves these unresolved. If a task depends on one, ask rather than assume:
- Target CloudStack release, and how much its GPU offerings, resource limits and Extensions
  framework already cover
- Whether reseller domain admins mirror from Hugging Face directly or only request it
- Public Hugging Face Hub vs a private mirror or Enterprise Hub
- Whether dedicated-tier tenants may choose serving profiles for their own node pools
- Initial catalog, license allow-list, price tiers and pricing unit

## Reference

Section numbers in the spec: 3 spec schema and validation rules · 4 engine modules · 5 Hugging Face
sourcing · 6 tuning profiles · 7 Tier 1 operability · 8 Tier 2 · 9 CloudStack integration and the
command map · 10 engine REST contract · 11 non-functional requirements · 12 test framework ·
13 layout and milestones · 14 acceptance criteria.
