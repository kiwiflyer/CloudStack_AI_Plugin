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

v2 is reachable through the engine API, the CLI and now CloudStack: the Java
plugin generates all nineteen `Release.V2` commands, but registers them only when
`ai.services.v2.enabled` is on (global, default false), so a GA install does not
advertise an API for features it does not run. The UI has the v2 screens — adapters, batches, certificates,
private endpoints, guardrails and an evaluations tab — gated by nothing of their own:
CloudStack drops a route whose `permission` is missing from `listApis`, so the
plugin's setting is the only switch. Terraform has the five v2 resources and an
evaluations data source, declared unconditionally — a v1 install fails at apply with
an error naming the setting, because Terraform cannot ask CloudStack what is
registered before a plan. No CloudStack has run the proxy provisioner and no
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
why a private endpoint is a proxy VM on the tenant's tier and not a Service. `ui/` **compiles inside a real CloudStack UI** — the section, its eleven views and
its utils were copied into a 4.23.0.0 tree, registered in `router.js`, and built
by its own webpack with no errors and no lint complaints, appearing in the served
`app.js`. Its Playwright specs still have never run: they assert the section is
*visible* per role, and the section gates fifteen AI commands that a CloudStack
without the plugin does not advertise — verified against a live `listApis`, which
returned none of them. The gate works; there is nothing behind it yet. `terraform/` builds and unit-tests (v1 and v2) but
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

**Two things a real management server corrected, both of which a fake had agreed
with.** Request signing encodes a space as `%20`, not `+` — CloudStack's servlet
decodes `+` back to a space and re-encodes it as `%20` before verifying, so every
call carrying a space in a value was rejected, and the test pinned `name=my+host`
in place. And `GpuDevice.usable` tested for `managedstate == "Enabled"`, a value
4.23.0.0 never returns: it reports `Managed` or `Unmanaged`, so every real GPU read
as unusable and a platform full of idle cards would have refused every deployment.
The check is now a deny-list, because an allow-list that misses a value fails
silently everywhere while a deny-list that misses one fails loudly in one place.

**What has actually run against a real CloudStack 4.23.0.0** (`make stack-up`):
request signing, `take_snapshot` reading GPU capacity from tagged hosts, the
capacity planner and its warnings on that observed data, `listKubernetesClusters`,
`listGpuCards`, `listGpuDevices`, the tenant-network lookup, and all eight
migrations on Postgres. **What has not:** the write half of the reconciler, because
`apply` builds a deployer that needs a Kubernetes cluster and there is none — so
`deployVirtualMachine` and everything after it is unexercised. The simulator's
hosts are simulated: the API, the signing and the schema are real, the GPUs are not.

**`call_paged` stops on the server's `count`, not only on a short page.** Some
commands ignore `page` and `pagesize` entirely — `listApis` on 4.23.0.0 returns
all 865 every time — and 865 is never shorter than a 500-row page, so the old
rule never fired and the call hung, accumulating the same rows. A page cap turns
a server reporting neither signal into an error instead of a reconcile that holds
its database connection forever.

**The engine client presents its certificate through an `ssl.SSLContext`**, never
httpx's `cert=`. That parameter is deprecated in httpx 0.28 and **silently sends
nothing** — still in the signature, only a DeprecationWarning — so `AISVC_TLS_CERT`
and `AISVC_TLS_KEY` appeared to work while no certificate was ever presented, and
the engine's refusal surfaced as "Server disconnected without sending a response".
mTLS over loopback has now completed a real handshake in both directions: refused
without a certificate, HTTP 200 with one.

**Anything defined after `install.py`'s `__main__` guard does not exist when it
runs.** The guard calls `main()`, so it must stay last. Appending helpers below it
made `install.py --make-certs` die with a `NameError` while all eighty installer
tests passed, because they import the module rather than execute it.

Before claiming a command works, run it against `make stack-up`, not a fake.
Before referencing a module, confirm the file exists.

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
modules must be built from source into `~/.m2` first — all six the pom names:
`api,utils,framework/config,framework/db,framework/events,engine/schema`. That list
is repeated in both CI jobs and two documents and derived from nothing, so
`tests/unit/test_java_build.py` compares every one of them against the pom. Two
modules were missing from all four for three milestones, and local builds kept
passing because an earlier, wider build had already put the jars in `~/.m2` — `java-plugin/README.md` has the two
commands. JDK 17 (`JAVA_HOME=/opt/homebrew/opt/openjdk@17`, keg-only), targeting JDK 11
bytecode. `make test-java` fails with a clear message if the artifacts are absent.

```
make install        # editable install into .venv
make check          # lint + types + unit + contract + integration + security
make test-unit      # pytest, no GPU, no network
make test-contract  # schemathesis + recorded CloudStack responses
make test-int      # pytest against tests/integration/fake_cloudstack.py — fakes, not containers
make stack-up      # the real thing: CloudStack simulator, Postgres, MinIO (deploy/compose/)
make stack-down    # and tear it down, volumes included
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
- Only the mirror worker is meant to have internet egress. **The network policy that would
  enforce it does not exist** — no `NetworkPolicy` is rendered anywhere, and SPEC section 12's
  `test_inference_pod_has_no_internet_egress` has never been written. The supply-chain gates
  at mirror and deploy time are real; the runtime fence behind them is not. Do not repeat the
  claim as though it holds.
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

- **ADR-0004 — state store:** the engine's state lives in **its own Postgres**, not in
  CloudStack's MariaDB. The reason is blast radius, not the engine: the gateway writes a
  usage row per request, and the reconciler holds a connection for a whole run. The only
  Postgres-specific code was the advisory lock in `aisvc/reconciler/leader.py`, which now
  branches on the dialect and takes a real `GET_LOCK` on MariaDB — **never let a backend
  fall through to the no-lock branch**, which exists for SQLite only and would let two
  replicas both provision a pool. MariaDB is supported through the `mariadb` extra
  (PyMySQL, MIT — not the LGPL package called `mariadb`); Postgres stays the default.

- **ADR-0005 — model artifact storage:** CloudStack *secondary* storage cannot hold
  model artifacts — NFS only, SSVM-mediated, no object API, and it holds
  CloudStack-tracked resources. CloudStack *object* storage (4.19+) is a different
  feature that can: `createBucket` returns `accesskey`, `usersecretkey` and `url`, so it
  provisions a bucket on a backing MinIO or Ceph and hands back S3 credentials. It does
  not remove the S3 dependency, it moves who provisions it, and `S3ObjectStore` does not
  change. **Resolution is implemented** — `aisvc/registry/bucket.py`, behind
  `AISVC_REGISTRY_FROM_CLOUDSTACK` — and deliberately resolves without ever creating,
  because a bucket quota in GiB is a number only an operator can choose. A missing or
  credential-less bucket is an error, never a fallback to the configured endpoint.

- **ADR-0006 — how a GPU node joins the cluster:** **proposed, nothing built.** A node
  today runs `kubeadm join` from cloud-init with an endpoint, token and CA hash an
  operator pasted into the environment; the token expires in 24 hours, the failure
  lands on a node that is already billed, and CKS does not know the node exists.
  `addNodesToKubernetesCluster` (4.21+, so inside the 4.22 floor) would hand the
  deployed VM to CloudStack instead. **Do not start building it** until the one
  experiment in the ADR has run: nothing in CKS's documentation mentions GPUs, and a
  CKS operation that stops rather than destroys a passthrough node leaks its GPU.

- **ADR-0007 — where the cluster comes from:** **proposed, nothing built.** Amends
  0006: a zone declares its cluster's `source` (`cks` or `external`) and whether the
  engine may `create` one, so CKS is the paved road rather than the only road and
  `kubeadm join` stays for clusters CloudStack does not manage. Installing and
  enabling CKS is the CloudStack administrator's one-time job and **out of scope** —
  this project requires only that a Kubernetes service is available. Note the defect
  it names: `check_clusters` fails a zone CloudStack has never heard of, which is
  right only while every cluster is assumed to be a CKS one.

**A private endpoint's network is checked against CloudStack**, not against anything
the engine keeps: `aisvc/cloudstack/networks.py` resolves the caller's domain path to a
domain id and lists that account's networks, and `build_services` wires it as
`networks_for`. Before that nothing set it, only the test conftest did, so every real
VPC attachment was refused as "not available to this account" while every test passed.
**Never let that lookup return `[]` on an error** — it reads as "owns nothing" and
refuses the attachment with a message about permissions.

**`networkid` is a typed entity parameter**, so CloudStack checks the caller may
see the network before the command runs. A `CommandType.UUID` parameter is handed
back as the **internal numeric id**, and the engine matches what it is sent against
the UUIDs CloudStack returns from its own list calls — so the generator's `resolve=`
translates it back through `_entityMgr` before the body is built. Sending the raw
`Long` would record an id matching nothing. Setting `entity=` without `resolve=` is
that bug; the generator emits the imports for both, which it previously did not,
so `entity=` alone used to produce Java that did not compile.

**The domain is part of a tenant's identity.** A CloudStack account name is unique
only within its domain, so `CallerContext.may_see` compares the `(account, domain)`
pair and `KeyStore.list_for` / `UsageStore.list_for` **require** a domain — an
optional one defaulting to "any" is the defect, not a convenience. **`BudgetRecord` carries a
surrogate id with `(account, domain)` beside it**, unique together, the same shape
as adapters and certificates — and the API identifies a budget by that id, because
an account name cannot. `createAiBudget` therefore requires a domain.

**A batch serves exactly one model.** `parse_requests` requires the declared model
and refuses any line naming another, filling in a line that names none. The runner
posts each body verbatim under one shared operator key, so by the time the gateway
sees a request the submitting account is gone and every scope, budget and guardrail
check runs against that key — a line naming a different model would reach it with no
tenant-level check at all.

**`custom_domain` is checked against a hostname allow-list** (`valid_hostname`),
at the API body and again in `validate_exposure` — two different doors. It is
rendered into the proxy's cloud-init, into a `runcmd` string entry that cloud-init
runs through a shell and into the nginx `server_name`, so a value carrying `;` or a
newline escapes both. An allow-list rather than escaping: what is a hostname is
small and known, what is dangerous in a shell *and* YAML *and* nginx is not.

**The fetched kubeconfig's directory is not taken as found.** The path is
predictable, so a local user can plant a directory and a symlink in advance — no
race to win — and `chmod 0700` narrows a mode without changing an owner. So the
directory must be owned by this user and not writable by others, and the file is
opened `O_NOFOLLOW`.

**A password belongs in the `Secure` category.** `ConfigurationVO.isEncrypted()` is
a literal `equalsAny(getCategory(), "Hidden", "Secure")`, verified against the
bytecode — anything else is stored and returned by `listConfigurations` in
cleartext. `ai.services.engine.tls.client.key` is the key store's password and sits
there; the trust store's password stays `Advanced` because a trust store holds
public certificates and its password protects nothing.

**Guardrails scan every shape a prompt arrives in**, not just string chat content:
the `text` parts of a vision content list, `prompt`, `input`, `query` and rerank
`documents`. Text the scan does not reach is a locked guardrail silently not
applying, which is the thing a domain admin was sold.

**The Terraform client POSTs**, never GETs. `uploadAiCertificate` carries an
unencrypted PEM key, and CloudStack's query-string log scrubber covers
password/accesskey/secretkey but not `privatekey` — though its JSON scrubber does,
so CloudStack itself classifies the field as a secret. The signature is computed
over the canonical query whatever the method, so nothing about authentication
changes.

**Guardrail profiles are scoped to the caller**, not to a role: a caller writes at
their own level and no higher (`aisvc/guardrails/authority.py`), so a tenant tightens
their own account and cannot reach the platform profile. Deployment scope stays root
admin's until there is a deployment-ownership lookup. A check `locked` at a higher
scope is refused at authoring time, not merely ignored at resolution.

**One command still promises more than the engine serves:** `createAiBudget` is
granted to every role and served `AdminDep`. Here the engine is right — a tenant who
could set their own budget could lift the limit blocking them — so the map is the side
to narrow, which changes a published grant. `TestTheGrantMatchesTheEngine` pins it;
do not add a second without deciding which side moves.

**The Java generator's command table is a second copy** of `aisvc/api/commands.py` —
it needs parameter types, response classes and event names the engine's map does not
model. `tests/unit/test_commands.py` compares them, and parses SPEC section 9's table
against the map as well. Add a command in one place and those guards name the others.

**The plugin creates its own `ai_deployment` table at startup** (`AiServicesSchema`),
because CloudStack's schema upgrade chain is closed to a plugin JAR and the bundled
`.sql` had nothing to run it. A failure is logged, never fatal: an optional plugin must
not stop a management server from starting. Upstream the table belongs in
`engine/schema` and this class should go.

**Four files in CloudStack's own tree are still needed** to ship this: `plugins/pom.xml`
and `client/pom.xml` (no auto-discovery; the shade plugin bundles declared dependencies
only), the `ui/` section plus its `router.js` registration, and — upstream — the table in
`engine/schema`. Everything else is self-contained.

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
- What quota a registry bucket should carry, who owns it, and what should happen when it
  is deleted out from under a running engine ([ADR-0005](docs/adr/0005-model-artifact-storage.md)).
  Resolving the bucket is built; these three are sidestepped, not answered.
- Whether GPU nodes join through CKS or keep running `kubeadm join` themselves
  ([ADR-0006](docs/adr/0006-gpu-node-join.md)), gated on an experiment nobody has run.
  [ADR-0007](docs/adr/0007-kubernetes-cluster-source.md) answers the cluster-source
  half — three sources, CKS as the default — and leaves open who should own a cluster
  the engine creates and what happens if it is deleted from under a running engine.

## Reference

**The database schema comes from the migrations, never from `create_all`.**
`aisvc/db/migrate.py` is the only way a long-running command builds one; `create_all`
skipped `alembic_version` and missed any table whose module that path had not imported
(`aisvc_job`, on the apply path). A source scan over `aisvc/cli/main.py` guards it.

`docs/feature-spec-sheet.md` is the limits-and-defaults sheet: every threshold, TTL,
supported value and surface count, each read from the code. A gap is marked **·not built**
rather than omitted. When you change a default, change it there too.

`docs/why-cloudstack-ai-services.md` is the positioning document: the case for the
product, who it is for and what it lets an operator sell. Every product claim in it
is traceable to SPEC.md; keep it that way.

`install.py` at the repository root scripts everything outside CloudStack: virtualenv,
dependencies, `.env`, migrations, reachability. **Standard library only** — it builds the
virtualenv the engine lives in, so it can never import `aisvc` or any dependency. It is in
`ruff`'s targets and mypy's `files`.

`README.md` is the front door: what this is, the five pieces and their one rule
each, a quick start and the layout. `docs/README.md` indexes everything written
down, including the published presentations — they are pages, not files, so they
are linked rather than stored.

**The repository is Apache 2.0 licensed.** `LICENSE` is the canonical text and
`NOTICE` is the ASF attribution file. Every source file carries the ASF header:
`.py`, `.java`, `.go`, `.js`, `.ts`, `.vue`, `.tf`, `.hcl`, `.yaml`. **Jinja
templates are deliberately excluded** — `.j2` files render into manifests whose
digest is `config_hash`, and fourteen comment lines would change every spec-hash
and start a rollout everywhere. New files need the header; the Java generator
already emits it.

`docs/installation.md` is the operator-facing install and setup guide: prerequisites,
the four pieces and their order, every config key and environment variable, and an
honest list of what has never been run. Keep it current when any of those change.


Section numbers in the spec: 3 spec schema and validation rules · 4 engine modules · 5 Hugging Face
sourcing · 6 tuning profiles · 7 Tier 1 operability · 8 Tier 2 · 9 CloudStack integration and the
command map · 10 engine REST contract · 11 non-functional requirements · 12 test framework ·
13 layout and milestones · 14 acceptance criteria.
