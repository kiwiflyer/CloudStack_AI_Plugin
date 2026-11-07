# CLAUDE.md

Guidance for Claude Code working in this repository.

## Current state

**This repo is spec-only.** The single file is `SPEC.md`. There is no code, no `pyproject.toml`,
no `Makefile`, no git repository, no CI. Every command and path below is **planned**, not working,
until the milestone that creates it lands.

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

## Planned commands

```
make test-unit      # pytest, no GPU, no network
make test-contract  # schemathesis + recorded CloudStack responses
make test-int       # docker compose: CloudStack simulator, kind, MinIO, Postgres, LiteLLM, mocks
make test-java      # JUnit 5 + Mockito + Marvin
make test-ui        # Vitest + Playwright
make test-tf        # Terraform acceptance against the simulator
make test-gpu       # GPU lab only, not in CI
make record         # refresh recorded fixtures against a lab zone — only deliberately
```

Operator CLI (Typer): `aisvc plan`, `aisvc apply`, `aisvc status`, `aisvc drain <host>`,
`aisvc mirror <repo>`, `aisvc effective <deployment>`.

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
- Prompts and outputs are never logged by default. Guardrail blocks record the category only.

**Profile precedence** (most specific wins; a `locked` value beats every level below it):
platform defaults → model defaults (from `generation_config.json`) → domain → account →
deployment → API key → the request itself, within merged bounds. Policies are `default`, `clamp`
or `locked`. `getAiEffectiveParameters` must report both the resolved value and its source level.

**Passthrough GPUs cannot live-migrate.** Drain and replace nodes; never attempt migration.

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

1. Foundation — scaffold, CI, `spec` models and validation, CloudStack client, `inventory`, `capacity`, `aisvc plan`
2. Provisioning — `provision`, cloud-init, Packer golden template, `aisvc apply` for node pools
3. Serving and sourcing — `registry`, `hf`, `k8s` rendering, `gateway`, serving + generation profiles
4. Engine API — FastAPI, async jobs, caller-context filtering, mTLS, contract tests
5. Native API plugin — v1 commands, ConfigKeys, async bridge, events, roles, usage, deletion hooks
6. Native UI section — v1 screens, tenant first, i18n, Playwright per role
7. Operations — `autoscale`, `lifecycle`, `metering`, alerts, dashboards
8. Tier 1 operability — setup wizard, maintenance integration, playground, scoped keys, budgets, engine HA, Terraform
9. Tier 2 — multi-LoRA, embeddings/rerank/vision, batch, guardrails, evaluation gate, private endpoints

Work from the spec's section numbers; require `aisvc plan --dry-run` output against
`examples/platform.yaml` before any apply logic merges.

## Open decisions — do not silently pick one

The spec leaves these unresolved. If a task depends on one, ask rather than assume:

- Serving layer: KServe vs the vLLM production stack (or llm-d)
- Gateway: LiteLLM vs Envoy AI Gateway
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
