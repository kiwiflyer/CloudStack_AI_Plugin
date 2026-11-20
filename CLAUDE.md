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
(`validate`, `plan`, `apply`, `batch`, `status`, `serve`), `gateway-hooks/` and
`templates/packer/`.

v2 is reachable through the engine API, the CLI and now CloudStack: the Java
plugin generates all twenty `Release.V2` commands, but registers them only when
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
why a private endpoint is a proxy VM on the tenant's tier and not a Service. `ui/` **builds and runs inside a real CloudStack UI** — the section, its twelve
views and its utils were copied into a 4.23.0.0 tree, registered in `router.js`,
built for production with `npm run build`, and served by Jetty at `/client`,
where a tenant signs in and uses it. **The compiler writes to stdout, not the
supervisor log file** — a grep of the latter will report no errors whatever
happened. **Compiling proves nothing about rendering**: the section reached
`Compiled successfully` and appeared seven times in `app.js` while every label on
every screen rendered as its own key, because the locale had never been merged.
**A production build dying in `thread-loader` with `EPIPE` is a worker crash, not
your code** — in the compose harness an amd64 image runs emulated on an arm64
Mac, and with the management server and dev server up, under 2 GB of 8 was free;
stop both and set `parallel: false` and it builds in one pass. It is not faster
to test against, either: the same specs took 10 minutes on the built artifact
against 2.4 behind the dev server, because Jetty serves each chunk separately
under emulation. `ui/README.md` has the whole procedure. **Its whole Playwright suite now passes, 40 with 7
skipped**, against the UI and the plugin in **the same management server** — the section
visible per role, every admin screen absent for a domain admin and a tenant, and a tenant
typing an admin screen's URL getting a 404 rather than a refusal, because CloudStack drops
a route whose `permission` is missing from that caller's `listApis`. The 7 skips are the
`ai.services.v2.enabled` *off* group, which cannot run while the setting is on, so nothing
is skipped for being unbuilt. Its nineteen
route-level `permission` entries name seventeen distinct commands, twelve v1 and
five v2, and both halves of that gate are proven: without the plugin `listApis`
advertised none of them; with it loaded all twelve v1 ones are there (thirty-eight
in all) while the five v2 routes drop, which is what the `ai.services.v2.enabled`
default is supposed to do. `terraform/` builds and unit-tests (v1 and v2) but
**6 of its 8 acceptance tests pass** against a live management server and the
other two skip, wanting a real `.safetensors` and a real deployment. Running
them needs a `terraform` binary, which the SDK's own installer could not fetch.
**The Marvin tests exist and pass** — `test/integration/plugins/ai-services/`,
58 commands across three roles, allowed and denied, against a live 4.23.0.0. They
call each command with no arguments, because access is checked before parameters
are, so a role that may call one fails on its arguments (431) and one that may
not fails on the name (432) and nothing is executed. **`nosetests` cannot run
them**, or any of CloudStack's own: `nose` imports `imp`, gone in 3.12, so
`run_without_nose.py` drives them under plain `unittest`.
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
rollout between blue and green, and a single test against a real Kubernetes API server.

**The engine now serves every command SPEC section 9 declares but one.** Milestone 3's
serving- and generation-profile commands answered 404 until migration 0009 gave them
tables, and the three deployment commands until `placement` gave them something to
decide ([ADR-0008](docs/adr/0008-api-deployments.md)). `approveAiModel` is the last, and
it waits on an open decision rather than on code.
`tests/contract/test_engine_api_contract.py` holds the line: it walks every command in
`aisvc/api/commands.py` against the app's own OpenAPI schema, both directions, and
`NOT_YET_SERVED` may only shrink.

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

**Three things a real management server corrected, all of which a fake had agreed
with.** Request signing encodes a space as `%20`, not `+` — CloudStack's servlet
decodes `+` back to a space and re-encodes it as `%20` before verifying, so every
call carrying a space in a value was rejected, and the test pinned `name=my+host`
in place. And `GpuDevice.usable` tested for `managedstate == "Enabled"`, a value
4.23.0.0 never returns: it reports `Managed` or `Unmanaged`, so every real GPU read
as unusable and a platform full of idle cards would have refused every deployment.
The check is now a deny-list, because an allow-list that misses a value fails
silently everywhere while a deny-list that misses one fails loudly in one place.

**And a third, found the same way: `GpuDevice` read one of the two fields
CloudStack gives about a GPU.** `state` was never parsed at all. On the live
zone all 112 devices were `managedstate=Managed` while sixteen of them — every
H100, the only inference-capable capacity there — carried `state=Error`, so the
engine counted 16 usable, 16 free, and the class reported a zone with room in
it. `GpuDevice.State` is `Allocated`, `Free`, `Error`, `PartiallyAllocated`
(read from the management server's source, since the API reference was wrong
about the field beside it); only `Error` means "not capacity", and `Allocated`
now marks a device taken even where this caller cannot see the VM holding it.
Absence is read differently for the two fields on purpose: a missing
`managedstate` leaves the primary question unanswered and is distrusted, while a
missing `state` reports no fault — treating silence as a fault would make any
server that trims the field a zero-capacity platform, which is exactly how the
`managedstate` bug failed. **Whenever CloudStack reports two fields about one
thing, read both.**

**The harness container is run by supervisord, so never restart anything in
it by hand.** `supervisorctl status` lists `cloudstack`, `cloudstack-ui` and
`mysqld`, and the `cloudstack` program's command is exactly
`mvn -pl client jetty:run -Dsimulator -Dorg.eclipse.jetty.annotations.maxWait=120`.
A `pkill -f jetty:run` followed by launching that line yourself does not replace
the server — supervisord restarts its own copy, so **two management servers run
at once**, each loading CloudStack under emulation. The container has about
7.7 GiB; two servers plus the UI dev server reach 83% and it is
**OOM-killed** (`docker inspect --format '{{.State.OOMKilled}}'` reports it),
which surfaces as `configurationDaoImpl` failing with an
`ExceptionInInitializerError` on a refused JDBC connection and
`Failed startup of context /client` — a database fault that is not one. Use
`supervisorctl restart cloudstack`, and
`supervisorctl stop cloudstack-ui` first when memory is tight: the dev server
costs 1.25 GiB and supervisord respawns it within seconds of a `kill`, so only
`supervisorctl` actually stops it. One supervised server alone peaks near 45%.

**What has actually run against a real CloudStack 4.23.0.0** (`make stack-up`):
request signing, `take_snapshot` reading GPU capacity from tagged hosts, the
capacity planner and its warnings on that observed data, `listKubernetesClusters`,
`listGpuCards`, `listGpuDevices`, the tenant-network lookup, and all eight
migrations on Postgres. **And now the Java plugin itself** — built, installed,
loaded into the running management server, and talking to the engine over
mTLS: `listAiModels`, `listAiEndpoints` and `listAiSetupChecks` all answer
200 through a signed CloudStack API call. **With `ai.services.v2.enabled` on**, `listApis`
goes to 923 and all twenty v2 commands register, and a real certificate,
guardrail profile, batch and evaluation have been created through them. **And for
tenants** — the
same `listAiModels` returns two models to root admin, two to an account in
`/Customers/Acme`, and none to one in `/ROOT`, because the spec publishes to
`/Customers` and the engine filters on the domain the plugin asserts from
`CallContext`. That invariant had never run outside a unit test. All thirty-eight v1 commands register
(`listApis` goes from 865 to 903, exactly the thirty-eight) while the nineteen v2
commands stay away, so the `ai.services.v2.enabled` gate holds on a real server;
`AiServicesSchema` creates `ai_deployment` at startup; all ten `ai.services.*`
ConfigKeys appear in `listConfigurations`, `engine.tls.client.key` among them in
the `Secure` category, which until now had only been read out of the bytecode; and
a signed `listAiModels` travels the whole way in — dispatch, RBAC, the manager,
the engine client — to be refused with *"mTLS is required to call the AI Services
engine; set ai.services.engine.tls.client.cert"*. **What has not:** the write half
of the reconciler, because `apply` builds a deployer that needs a Kubernetes
cluster and there is none — so `deployVirtualMachine` and everything after it is
unexercised. The simulator's hosts are simulated: the API, the signing and the
schema are real, the GPUs are not.

**But the simulator does boot VMs, so that last gap is reachable.** Driving CKS
end to end proved it: `createKubernetesCluster` deployed a control node and a
worker, both reported `Running` with addresses on a guest network CloudStack
created for them. Three things had to be fixed first, none of them ours, and the
order they surfaced in is the useful part. `addKubernetesSupportedVersion` marks
`url` optional on the `@Parameter` and then rejects an empty one in
`KubernetesVersionManagerImpl`, but the ISO it names is never fetched here — the
simulator reports `isostate: Ready` at once. `createKubernetesCluster` then
refused with a bare *"Unable to complete this operation"*, which the log explains
as `endpoint.url` still being `http://localhost:8080/client/api`; a node could
never call back on that, so CKS blocks the create. And the control VM failed on
`NoSuchFileException: /usr/share/cloudstack-management/cks/conf/k8s-control-node.yml`,
because CKS reads its cloud-init templates from the **installed-package** path
while this image runs from source — copying `plugins/integrations/kubernetes-service/target/classes/conf`
there fixes it. After all three the cluster reaches `Starting` and stays there,
which is the real ceiling: the hypervisor is simulated, so nothing runs inside
those VMs, `kubeadm` never executes and the Kubernetes API never answers. **So
CKS is not a way to get a cluster here — but `deployVirtualMachine` working is
the gate on the reconciler's write half**, and it is open.

**A plugin that fails to wire is not an optional plugin — it is a dead management
server.** The Spring context declared the manager and never `AiDeploymentDaoImpl`,
the one injected type CloudStack's own contexts cannot supply, so
`DefaultModuleDefinitionSet` threw `Failed to load module [ai-services]` and Jetty
reported `Failed startup of context /client`: no API at all, for anything. The
first load into a real server is the only thing that could have shown this — 451
Java unit tests passed throughout, because they construct the manager directly and
set its fields. `SpringContextTest` now walks `AiServicesManagerImpl`'s `@Inject`
fields and fails on any type in `org.apache.cloudstack.ai.*` without a bean. Note
what this means for `AiServicesSchema`'s "logged, never fatal" promise: that holds
for *table creation*, not for bean wiring, where there is no such thing as a
non-fatal failure.

**API keys moved out of `cloud.user` in 4.21+.** They live in `api_keypair`
(a user may hold several), so a harness script that selects `api_key` from
`cloud.user` fails with `Unknown column` on 4.23.

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

**A column or detail CloudStack has no word for needs a `customTitle`.** A screen
declaring `columns: () => ['name', 'task', …]` gets each rendered as
`label.<field>`, so a field CloudStack does not define shows raw — the model
catalogue's headers read `label.task`, `label.license`, `label.pricetier` on a
live server, and the section had 53 such fields out of 83. **Do not answer that
by defining `label.task` here.** Our keys are merged into CloudStack's own
`en.json`, so a bare key both collides with whatever CloudStack may add and
silently overrides it in every language. `AutogenView.vue` and `DetailsTab.vue`
both accept `{ field: 'task', customTitle: 'ai.task' }` and render
`label.<customTitle>`, which keeps every string this repo owns inside
`label.ai.*` — a rule `i18n.spec.js` enforces. That test now also checks each
field resolves: either CloudStack defines `label.<field>`, recorded for 4.23.0.0
in `ui/tests/fixtures/cloudstack-label-fields.json`, or the field carries a
`customTitle` pointing at one of ours.

**A CloudStack list links one column and one only.** `ListView.vue` renders the
way into a record's detail view where
`['name', 'provider'].includes(column.key)`, so a screen whose columns include
no `name` has no route to its own detail page and every `dataView: true` action
on it is unreachable however correctly declared — eleven of ours were, across
six screens, until each was given a first column keyed `name`:
`{ field: 'name', customTitle: 'ai.customdomain', name: r => r.customdomain }`.
That works because `AutogenView` writes a column's custom render back onto the
record (`items[idx][key] = func(items[idx])`), so the column carries the key
CloudStack links on, our wording, and the right text. **Its action buttons are
icon-only** as well — `<button><span role="img" aria-label="reload">` — with the
words in a hover tooltip, so `getByRole('button', { name: /replace/i })` matches
nothing; `tests/e2e/table.js` locates them by icon.

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

- **ADR-0001 — serving layer:** plain Kubernetes `Deployment`s rendered by `k8s` (as
  Python dicts — **not** Jinja, whatever the ADR's own wording said; `config_hash`
  digests those dicts, and the repository's one `.j2` is the node cloud-init).
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

- **ADR-0008 — deployments made through the API:** **accepted and built.**
  `deployAiModel` and its two siblings could not mean what they looked like: a
  spec model's zone comes from its node pool and its replica count is a declared
  range, so there was nothing for them to decide that the spec had not. A
  `placement: spec | api` field per model says who owns placement. `spec` is the
  default and changes nothing; `api` models run only where a row in
  `aisvc_deployment_placement` says, and that row holds placement alone — never
  a source, a licence or a bound. **The planner leaving an `api` model out of
  `declared` is not an oversight**: that absence is what the existing sweep acts
  on, and it is how `deleteAiModelDeployment` takes effect.

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

**A password belongs in the `Secure` category — but the category alone protects
nothing.** `ConfigurationVO.isEncrypted()` is a literal
`equalsAny(getCategory(), "Hidden", "Secure")`, verified against the bytecode, so
the category is the only way to ask for encryption and
`ai.services.engine.tls.client.key` sits there. **What it buys depends on the
deployment.** With a real value set on a live 4.23.0.0, `listConfigurations`
returned that password in cleartext and `cloud.configuration` stored it in
cleartext, because that server has `db.cloud.encryption.type=none` — the default
in the simulator image. The category marks a value as *to be* encrypted; whether
anything encrypts it is the operator's `db.properties`. Say so in the install
guide rather than implying the category is a control. The trust store's password
stays `Advanced` because a trust store holds public certificates and its password
protects nothing.

**Guardrails scan every shape a prompt arrives in**, not just string chat content:
the `text` parts of a vision content list, `prompt`, `input`, `query` and rerank
`documents`. Text the scan does not reach is a locked guardrail silently not
applying, which is the thing a domain admin was sold.

**A screen that only takes props renders empty, for ever — and seven of the
eleven views were exactly that.** CloudStack renders a section's `component` as
the route view and **passes it nothing**, and hands a *tab* its parent
`resource`, never a named array. So a view whose only data source is a
`keys`/`budgets`/`actions`/`models`/`parameters`/`session` prop can never show
anything. Only `GpuCapacity`, `FleetHealth`, `SetupWizard` and `EvaluationsTab`
ever fetched; `GpuCapacity` even says why in a comment, and
`ui/tests/unit/fetching.spec.js` is named for the rule — it just covered four
views out of eleven. Six are wired now (`Playground`, `EffectiveParameters`,
`HuggingFaceBrowser`, `ApiKeysTab`, `BudgetsTab`, `RunActionsTab`), each
fetching when its prop is null so the rendering tests still pass data in.
`SnippetsTab` needed more than wiring: its comment is right that the engine
renders snippets, and no endpoint-facing command returned them. **The response
generator now carries nested object lists** — `f(..., nested="AiSnippetResponse")`
emits a `List<T>` with `@Param(responseObject = …)` and a reader that maps the
JSON array — so `listAiEndpoints` delivers four snippets with each endpoint and
the tab reads them off `resource`. Before that every `Field` was a scalar, and a
response that cannot hold a list silently drops one.

**Nothing in a section's config can display a sync command's response**, so
anything returning a credential must be minted from its own view. The two
hooks that would show one — `action.response` and `successMethod` — are both
inside `$pollJob` in `AutogenView.vue`, so they fire for an **async** command
only. `createAiApiKey` is sync, so CloudStack's dialog closed without
displaying the secret it had just returned — measured live, `apikey` is a
66-character `aisvc-…` string — and because the engine stores a salted hash, a
key created in the UI could never be used by anyone. `ApiKeysTab.vue` posts the
command itself now and shows the secret once in a dismissable panel, the way
`Playground.vue` mints a session; the autogen action is gone, so there is one
path rather than two with one of them broken. **Removing an action orphans its
argument keys** — `label.ai.create.api.key` and the three bare ones that only
that dialog used — and `i18n.spec.js` catches exactly that.

**A dialog field's label is `$t('label.' + the command's parameter name)` and
nothing overrides it.** `AutogenView.vue` renders `:title="$t('label.' +
field.name)"`, so unlike a column — which takes a `customTitle` — a dialog
argument has no alias, and 25 of the section's 39 arguments had no key
anywhere: the Create API key dialog read `label.models`, `label.expires` and
`label.ratelimitrpm` beside a correct "Name". **This is the one place the
`label.ai.*` rule cannot hold**, so those keys are bare, and
`cloudstack-label-fields.json` now also records which argument names
CloudStack owns so none of ours shadows one. `i18n.spec.js` checks every
argument resolves, and that we shadow nothing — it had only ever checked keys
the section names as strings, and an argument's key is named nowhere.

**Four Playwright specs could never have passed**, and the recorded reason —
"sign-in timeouts and one screen that wants a mirrored model to click" — was
wrong. They clicked `getByRole('row').nth(1)`, and **clicking a row does
nothing**; one looked for a button by its words, and **an action button is
icon-only**. Both facts were already in this file, and `tests/e2e/table.js`
exports `rowLink` and `action` for exactly them. **The whole suite passes, 40
with 7 skipped**, measured against the plugin and the UI in the same
management server; the seven are the `ai.services.v2.enabled` *off* group,
which cannot run while the setting is on, so nothing is skipped for being
unbuilt.

**When every published port on this machine stops carrying data**, the
signature is worth knowing: TCP connects and nothing ever flows, on Postgres
as much as on HTTP, while `docker exec` keeps working because it uses the unix
socket. That is not the port forwarder — the bridge devices were gone from the
Colima VM (`colima ssh -- ls /sys/class/net` showing no `docker0` and no
`br-…`, only dangling veths, while `docker network ls` still named the
network). `colima ssh -- sudo service docker restart` rebuilds them without a
VM boot; every container then needs starting by hand and CloudStack takes
about ten minutes to come back. **Do not `docker restart` a container while
its bridge is missing** — it exits 128 with *"adding interface vethXXXX to
bridge br-… failed: Device does not exist"* and will not come back until the
bridges are rebuilt.

**Every detail view in the section showed the first record in its list.**
CloudStack opens one by calling the *same list command* with `id=<the row's
key>`; no list command declared an `id`, so the parameter was dropped in
silence, the engine answered with everything, and `AutogenView` rendered
`items[0]`. Measured live: the route `/aiguardrail/c8daccc7…` drew `pw-test`,
which is `b8365209…`. Thirteen screens, eleven commands — clicking any row
opened the wrong record, while the actions on that page still carried the
route's id, so what was shown and what would be acted on were different
objects. **A row's key is `record.id || record.name`** (`ListView.vue`), so
`AiServicesListCmd` filters on the response's own `id` where it has one and on
`identity` otherwise — `model` for endpoints and usage, `name` for models.
`filtersById()` defaults to **false** on purpose: a screen with no detail view
is never called with an `id`, and filtering one that is would silently empty a
list that worked. The filter is in the plugin rather than the engine because it
fixes eleven commands in one place; pushing it down is the follow-up when list
sizes justify fetching one row instead of all of them.

**And the mirror image, in the UI: thirteen actions offered a field the command
does not take.** CloudStack drops an unknown parameter in silence, so a dialog
can collect a value that goes nowhere. **`createAiApiKey` was the Terraform
`allowedmodels` bug exactly** — same three names, `allowedmodels`, `cidrs` and
`expiresat`, against a command taking `models`, `cidrlist` and `expires` — so a
tenant who restricted a key to two models and a CIDR got a key with neither,
and no expiry. The profile dialogs offered `temperature`, `topp` and the rest
individually while the command takes a single `params` map, so every field was
dropped and the profile was created empty; `runAiSetupSelfTest` sent `zone` for
`zoneid`, and both Hugging Face actions `repo` for `repoid`. Three were the
command's fault instead, because the engine accepts what the dialog offered:
**`createAiPrivateEndpoint` had no `tls` parameter at all**, so every private
endpoint was created with the engine's default of `none` and the
`certificateid` beside it could never mean anything — a TLS-terminated private
endpoint could not be made through CloudStack; `updateAiBudget` had no
`currency`, which `BudgetPatch` takes, so a budget's currency could be set once
and never corrected; and `drainAiNode` had no `force`, the query parameter that
is the whole reason to drain a tight zone.
`TestEveryUiActionSendsParametersItsCommandTakes` compares every action's `args`
against its command, the way `test_commands.py` already did for the Terraform
provider — the same test, pointed at the other caller.

**A field the engine sends that no Java response declares is dropped in
silence, and ten were.** The contract test asked only the other question — does
the Java read a field the engine never sends, which arrives empty — so this
whole direction was unchecked. `AiAdapterResponse`, `AiCertificateResponse` and
`AiPrivateEndpointResponse` each dropped `created`; `AiBatchResponse` dropped
`inputuri` and `outputuri`; `AiReconcilerRunResponse` dropped a run's `actions`
entirely; `AiGpuCapacityResponse` dropped `nodes_free`; and
`AiGuardrailProfileResponse` dropped `checks` — a guardrail profile without its
checks, which is the whole of what a domain admin was sold. Every one was
declared in the UI and blank on every server. `AiBudgetResponse` dropped
`percent_used` and `blocked`, so the budgets tab showed an empty percentage and
never marked an account whose spend was being refused. **Nothing about the Java
looks wrong** — this is the snippets defect again, but for scalars, where there
is no missing `List<T>` to notice. `TestNoFieldTheEngineSendsIsSilentlyDropped`
walks each engine row builder against its response class; a field may be left
out only by appearing in `NOT_CARRIED` with a reason. That is why
`_capacity_row` exists at all: it was an inline dict in the route, which the
test could not see.

**A guardrail profile's checks are keyed by name, so `nested_map=True`.** The
generator's `nested` reads a JSON array; the engine sends
`{"pii": {"action": "block", "policy": "locked"}}`. The map reader merges each
key into a copy of its value as `name` and calls the ordinary `from`, because a
nested class's fields are private and only its own factory can set them. **A
list cannot go in `details`**: CloudStack's `DetailsTab` takes a field and a
title and prints the value, so a list of objects renders `[object Object]`. It
is a tab, the same shape the snippets fix used.

**Three screens read the engine's spelling rather than CloudStack's.** A
response field is serialised under its lower-cased `api` name, so `percent_used`
arrives as `percentused` and `on_exceed` as `onexceed`. Vue renders an undefined
as nothing, so this never raises: the budgets tab printed `%` with no number.
Its "Set in" column was worse — it read `budget.source`, a field the engine has
never sent, so every budget claimed to come from `platform.yaml`. The column is
gone rather than guessed at. **And `ui/src/utils/capacity.js` read `total`,
`allocated`, `free` and `unhealthy`**, none of which CloudStack sends: the
heatmap took `undefined` for every count, which makes utilisation 0 and the
status `healthy`, so a zone with every GPU allocated and a dead node drew the
same green cell as an idle one. `allocated` and `unhealthy` are not sent at all
— they are differences, derived in one place now. **Every fixture for these
agreed with the code**, which is why ten tests passed over a screen that could
not work.

**`RunActionsTab` fetched the run list and read action fields off each run.**
Every cell was empty whatever had happened, and there was no data source either
way, since `actions` was one of the dropped fields. A tab is handed the row the
operator opened; the run carries its actions. **Its test asserted that it
fetched**, so correcting the component turned the test red — the same shape as
`assertEquals(4L, body.get("zone_id"))` and `name=my+host`.

**`FleetHealth` asks for data the engine does not have.** It renders a table of
hosts with template, driver, state and drift, from `listAiGpuCapacity` — which
answers per GPU *class*: zone, class, node and GPU counts. There is no
host-level route anywhere in `aisvc/api/app.py`, so this is not a mapping bug to
fix but a screen with no source. Recorded rather than patched; it needs an
engine route before it can show anything.

**The setup wizard had never shown a check, and the fix names the rule.**
A live `listAiSetupChecks` answers with all eight — three failures, each
carrying the remedy that names the setting to change — and the screen drew
*"No setup checks have been run yet."* It read
`listaisetupchecksresponse.checks`; CloudStack serialises the list as
`aisetupcheck`. **`items_key` is the key the Java reads out of the engine's
JSON; a `Response`'s `object_name` is what CloudStack sends on**, and the UI
is on the far side of the second one. The evaluations tab had the same bug
for the same reason. Neither could be caught by a rendering test, which
passes the array in as a prop, nor by a fetching test, which only asserts the
command was called — and the vitest fixture agreed with the bug, keying its
envelope `checks` under a comment that said it was what CloudStack returns.
`tests/unit/test_commands.py` now parses every `json?.…response?.…` in the
views and compares the key against the generator's own table, naming
`items_key` explicitly when that is what was used. Same family as the
playground session reading `session_key` for `apikey`: only the far side can
say what the far side sends.

**A refusal's reason is one level deeper than it looks.** CloudStack wraps
every answer in a key named after the command, errors included —
`{"listaigpucapacityresponse": {"errorcode": 534, "errortext": …}}` — so a
screen reading `response.data.errortext` gets nothing and falls through to
axios's `message`, which is *"Request failed with status code 534"*. Measured
live: that is what the capacity screen showed while the engine was saying
*"capacity has not been observed yet"*, the sentence that names the remedy.
`ui/src/utils/errors.js` reads it out of whatever single wrapper is there,
because a refusal that never reached the command comes back under
`errorresponse` instead, so reconstructing the key from the command name is
not reliable. **The test double had agreed with the bug**: it queued a bare
`new Error(reason)`, whose `.message` *is* the reason, so `.message` passed
its test and failed on a real server. `refusal()` in `ui/tests/doubles/api.js`
now builds the shape read off the wire.

**And eight of the nine screens never showed a reason at all** — their
`catch` set the list to `[]`, so a screen that could not reach the engine
rendered identically to one with nothing to show, which is the conflation the
three-state work was supposed to have ended. Only the heatmap had the three
states; the rest had one. All nine have them now, and the setup wizard's
**Continue** is disabled when the checks could not be read, where before
`canProceed([])` was true and a refused preflight read as a clean one.

**Nine of the eleven views called the API with no `catch`.** A refusal became an
unhandled rejection: blank screen, reason in the browser console. That mattered
most on capacity, where `listAiGpuCapacity` answers 534 until capacity has been
observed — the normal state of a fresh install, rendering identically to a broken
one. Each screen now separates *could not load* (with the engine's own sentence,
which names the remedy) from *nothing to show*, and `ui/tests/doubles/api.js`
rejects when an `Error` is queued, so a screen with no handler no longer tests
the same as one with.

**A test that says "does not X until asked" is only half a test.**
`the Hugging Face browser does not search until asked` passed because the
browser could not search *at all* — it took a `models` prop and called nothing,
ever. The assertion was true and documented a screen that did not work. Its pair
now exists: search when asked, and check the query reaches the command.

The playground was the screen the failure was reported on: it
took a `session` prop nothing supplied, so it showed *"Open a session to try
this endpoint."* with no control to open one, and **`createAiPlaygroundSession`
was never called at all** — a browser network log on that screen shows no AI
request whatever. It fetches `listAiModels` on mount now and mints a session
only when asked, because minting one on page load issues a credential nobody
requested and starts its fifteen minutes running against someone who is reading
the page.

**And when it was called by hand it returned a session with no key.**
`AiPlaygroundSessionResponse` read `session_key`, `base_url` and `expires_at`;
the engine sends `apikey`, `expires` and `id`, and did not send `base_url` at
all — it baked the URL into the snippets. Only `model` matched, so a live
CloudStack answered `{"aiplaygroundsession": {"model": "..."}}`: the one thing a
playground session exists to deliver was dropped at the one moment it exists.
Same family as `createAiApiKey` reading `secret` for `apikey`. The engine sends
`base_url` now, and **`responseHasSensitiveInfo` is true for this command** —
it was false, which was accidentally correct only while the key was being lost.

**The Java round-trip test could not have caught any of it.** `render_fixture()`
builds its fixture from the generator's own declared engine field names, so it
round-trips whatever the generator believes and agrees with itself. Only the
engine can say what the engine sends:
`tests/contract/test_engine_api_contract.py` now calls the route and compares
the keys it returns against the names the generator reads.

**CloudStack ignores a parameter it does not know, in silence.** The Terraform
provider sent `allowedmodels`, `cidrs`, `expiresat` and `ratelimitrpm` to
`createAiApiKey`, which takes `models`, `cidrlist` and `expires` and had no rate
limit at all — so every key was created unrestricted and never expiring while
the plan said otherwise. `tests/unit/test_commands.py` now compares every
parameter the provider sends against the command that receives it. **The same
family, caught the same way:** a new key's secret is `apikey` on the engine and
was read as `secret`, so it was dropped at the one moment it existed;
`allowed_models` and `cidrs` are `list[str]` and were sent as JSON strings (422
out) and read back from arrays with `getAsString()` (*Array must have size 1*
in); and a created object's id sits under its own name, so five resources read
`unwrap(body)["id"]`, set an empty id, and Terraform reported *Root object was
present, but now absent* in the apply that created it.

**`/` is a domain path, not an empty one.** `set_budget` stored
`domain.rstrip("/")`, which leaves the root domain as `""`. Lookups match both
spellings through `_domain_variants`, so nothing noticed until an operator was
told their root-domain budget belonged to `""` and Terraform planned to replace
it on every run.

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

**Five files in CloudStack's own tree are still needed** to ship this: `plugins/pom.xml`
and `client/pom.xml` (no auto-discovery; the shade plugin bundles declared dependencies
only), the `ui/` section plus its `router.js` registration, **`ui/public/locales/en.json`,
into which this repo's 150 keys must be merged**, and — upstream — the table in
`engine/schema`. The locale was the one nobody had noticed: the section was copied into a
real UI tree and compiled without it, so every label rendered as its own key —
`label.ai.model.catalog` as a page title — and a build that says `Compiled successfully`
says nothing about that. Everything else is self-contained.

**The v2 commands had never been called, and three things were wrong with how
they are generated.** All three are the same shape: a default nobody checked.
**A parameter travels in the body only if it has a `wire=` name**, and `wire=`
had been set only where the engine's field name *differed* from the Java one
(`baseModel` to `base_model`), on the assumption that it defaults to the field
name. It defaults to `None`, meaning "not a body field" — so the parameters that
needed no translation were exactly the ones never sent, across seven write
commands. `createAiGuardrailProfile` posted an empty body and got a 422;
`updateAiGuardrailProfile` got a clean 200 and changed nothing, which is worse.
`wire` now defaults to the snake_case of the Java name and the five deliberate
renames keep their explicit value. **`@Parameter`'s `length()` defaults to 255**,
which no PEM can meet, so `uploadAiCertificate` could never accept a
certificate — CloudStack's own `UploadCustomCertificateCmd` sets `length = 65535`
and ours now does too. **And `AiServicesAsyncCmd` discarded the engine's
explanation** exactly as the sync path had: a refused batch said "HTTP 400" while
the body said "the batch input file has no requests". `EngineResponse.explain()`
is now the one implementation both paths use.

**Three warts the first end-to-end round trip exposed, two still open.**
`AiServicesSyncCmd` used to report only the status — "the engine returned 503 for
/v1/capacity" — while the engine's RFC 9457 body said "capacity has not been
observed yet", the sentence naming the remedy. The engine speaks problem JSON so
its caller can explain itself and the plugin is its only caller; the detail is now
appended to the status, and a body that is not problem JSON yields nothing,
because a proxy may have answered. **Still open:** `getEngineClient()` caches the
client in a field, so every `ai.services.engine.*` key is declared dynamic
(`true`) and none of them takes effect without a management-server restart; and
`isEnabledInZone` has no callers, so `ai.services.enabled` gates nothing.

**An `@ActionEvent` on a command records nothing — it has to be on a bean.**
Every AI row in a live `listEvents` read `Scheduled` and nothing followed it, so an
operator saw work start and never learned whether it finished. The obvious patch is
to annotate `AiServicesAsyncCmd.execute()`; it was written, built, deployed and
measured, and changed nothing. Two reasons, both read out of 4.23.0.0's source.
The interceptor is a Spring AOP advisor —
`execution(* *(..)) && @annotation(ActionEvent)` in
`spring-core-system-context-inheritable.xml` — so it only wraps *beans*, and a
command is not one: `ApiDispatcher` builds it with `ComponentContext.inject`, which
calls `autowireBean` and returns **the same instance**, never a proxy. That is why
CloudStack's own commands carry no such annotation and the *manager* method does,
visible in a CKS stack trace where `ActionEventInterceptor.invoke` sits under
`$Proxy608.startKubernetesCluster` with `CreateKubernetesClusterCmd.execute`
calling in from outside. And even on a proxied method `eventType = ""` records
nothing: `interceptComplete` returns early on an empty type, and the fallback it
would use — `ctx.getEventType()` — **is never set, because nothing in the API path
calls `CallContext.setEventType`**.

So `AiServicesService.recordEvent(type, description, work)` is a method on the
manager, carrying the annotation, and both base classes run their work inside it;
the manager stamps the `CallContext` first, which is what gives
`interceptComplete` and `interceptException` a type to record against. Measured
live: a sync write now records `AI.PROFILE.UPDATE Completed INFO "Successfully
completed creating guardrail profile …"`, and a refused one
`AI.PROFILE.UPDATE Completed ERROR "Error while creating guardrail profile …"` —
note that **a failure is not a `Failed` state**; CloudStack closes the event as
`Completed` and carries the outcome in `level`, so anything reading these rows
must read the level, not the state. An async command now shows both its
`Scheduled` row and the row that closes it. `async = false` is deliberate — `ApiServer` has already written the Scheduled event for an
async command, and `interceptStart` would add a second opener against one close.
**Whether anything is recorded is simply whether the command declares an event
type**: `AiServicesSyncCmd.getEventType()` returns null by default, every list
command inherits that, and reads stay out of the log for free. Fourteen write
commands declared none and so completed in silence; they do now, which needed five
new constants (`BUDGET_UPDATE` beside the existing threshold alert,
`PLAYGROUND_SESSION`, and three for certificates). `AiServicesEvents.all()` had
also never listed the four v2 events, so nothing that walks it ever checked them.
`tests/unit/test_commands.py` now fails on a write with no event, a read with one,
and an event a command names that `all()` does not list.

**Eleven of the sixteen async commands cannot succeed, and fixing the events is
what made it visible.** `AiServicesAsyncCmd` requires a 202 — a 200 is refused as
"did not accept", deliberately, because treating it as success would report work
no job ever did. Only five engine routes return one (`/models/mirror`,
`/setup/selftest`, both rollout routes and `/nodes/{id}/drain`). Six more exist
and return 200 (`uploadAiAdapter`, `createAiBatch`, `cancelAiBatch`,
`runAiModelEvaluation`, `createAiPrivateEndpoint`, `deleteAiPrivateEndpoint`), so
the plugin rejects every one of them; five have no engine route at all
(`updateAiModelDeployment`, and all four serving-profile and tuning-profile
commands). Measured, not inferred: `runAiModelEvaluation` returns *"did not accept
AI.MODEL.UPDATE: HTTP 200"* and `createAiServingProfile` *"HTTP 404"*. These all
used to fail into silence — an operator saw `Scheduled` and nothing more, which is
exactly how it went unnoticed; now each one writes an ERROR row naming itself.
Decide per command which side moves, the engine's status or the plugin's contract.

**Eleven of the sixteen async commands could not succeed, and fixing the events is
what made it visible.** `AiServicesAsyncCmd` requires a 202 — a 200 is refused as
"did not accept", deliberately, because treating it as success would report work
no job ever did. Only five engine routes returned one. **Fixed**: five more now
answer 202 with a job (`/v1/adapters`, `/v1/batches`,
`/v1/models/{name}/evaluations`, and both `/v1/endpoints/private` routes), and
`cancelAiBatch` became sync, because SPEC section 8's table marks only batch
*creation* async and the command map had it wrong. Validation still answers 4xx,
*before* the job exists: a caller who sent a bad rank wants a parameter error,
not a job that failed.

**That left the other half: a command cannot succeed if it omits a field the
engine requires, and CloudStack drops an unknown parameter in silence.** Two
more of the same family as the Terraform `allowedmodels` bug — `uploadAiAdapter`
had no `files` parameter at all, so `validate_adapter` saw an empty list and
refused every upload with "no adapter weights found"; `createAiBatch` had only
`inputuri` while the engine parses `input` unconditionally, so every batch was
refused with "the batch input file has no requests". Both are declared now, the
batch's with `length = 65535`. **Still broken, and recorded rather than fixed:**
the engine never reads `input_uri`, so a batch can only be submitted inline.

**Section 6's profiles are served now, and the shape of the thing they feed was
wrong.** Nine commands had no engine route because there was no table: the
precedence logic in `aisvc/profiles/` could only ever read a profile out of
`platform.yaml`. Migration 0009 adds three tables — a serving profile, a
generation profile, and the assignment that binds one — three and not two
because the spec separates a profile from its binding and the same profile may
bind at more than one scope, which `aisvc_guardrail_profile` collapsing them
cannot express. `_layers_for` merges spec and stored assignments into one chain,
stored last so a tenant tightening their own account beats the operator's
baseline at the same level, and stored params are validated back into the spec's
own `GenerationValue` because `resolve()` asks each value for `.min` and
`.policy`. **Authority is checked at the assignment, not at creation** — the
same `aisvc/guardrails/authority.py` rules, because the question is identical:
describing parameters imposes nothing, binding them does.

**`getAiEffectiveParameters` returned an empty body every time**, and had since
it was written. `EffectiveParams.describe()` returned a mapping keyed by
parameter name, which reads naturally; section 9 declares the command a *list*
command, so the plugin reads `parameters` as an array, and
`ui/src/utils/profiles.js` asks each entry for `parameter.name`. Three consumers
expected a list and the engine was the one that disagreed. `describe()` returns
a list now, each entry naming itself, with `count` beside it; `by_name()` is the
keyed form, kept for the metering record, which is looked up rather than
iterated.

**One command still points at an engine route that does not exist**:
`approveAiModel`, and it is not waiting on a route. What approval *does* depends
on SPEC section 14's open question about whether reseller domain admins mirror
from Hugging Face directly or only request it — with direct mirroring there is
no request to approve, and `mirrorAiModel` already works. There is no
mirror-request table either, though this file has listed one as Postgres state
for some time.

**`deployAiModel` sent CloudStack's internal numeric zone id**, which is the
defect the generator's `resolve=` exists to prevent, reintroduced because this
command is hand-written. A live engine answered 422. It resolves through
`_entityMgr` and sends the zone's *name* now, which is what `platform.yaml`
records as `cloudstack_zone`. **Its unit test had pinned the bug** —
`assertEquals(4L, body.get("zone_id").getAsLong())` — the same shape as the
`name=my+host` signature test. And `GeneratedSurfaceTest` could not have caught
it: it renders every command's body with no `_entityMgr`, so a command that
resolves one dies there with a `NullPointerException` instead. It supplies a
mock now. `tests/contract/test_engine_api_contract.py` lists them as
`NOT_YET_SERVED` and fails if the list grows *or* if one starts working without
leaving it.

**That whole class of bug existed because the contract test checked one path.**
`test_async_commands_declare_202` asserted it of `/v1/models/mirror` and nothing
else, and `REQUIRED_PATHS` listed twelve paths, every one a read bar that mirror.
Nineteen commands were wrong and every suite was green. The test now walks every
command in `aisvc/api/commands.py` against the app's own OpenAPI schema, both
directions: an async command must answer 202, a sync one must not.

**A unit test used to overwrite the repository's `.env`.** `install.py
--make-certs` falls through into the rest of the installer, and `install.py`
takes its root from `__file__` rather than the working directory, so no `cwd`
could protect it — the installer test rewrote a developer's real database URL,
CloudStack keys and TLS paths with pytest temp paths. It is git-ignored, so
nothing showed in a diff; it surfaced as a live mTLS handshake failure against a
management server that no longer trusted the engine. The test passes
`--dry-run` now and asserts the file is byte-identical afterwards.

**Default role access is `@APICommand(authorized = ...)`, not a schema seed.**
`DynamicRoleBasedAPIAccessChecker.start()` walks every `PluggableService`'s
commands, reads that attribute, and consults the resulting map whenever no
explicit `role_permissions` rule matches the caller's role — so a plugin
declares its defaults on the annotation and needs no migration, no
`commands.properties` and no operator step. Without it, a plugin's commands
reach root admin alone, which is exactly what a live 4.23.0.0 did: a tenant was
refused all thirty-eight, and root admin worked only because the checker
short-circuits for role id 1. The generator emits the list from
`aisvc/api/commands.py`, so it cannot drift from what the engine enforces; the
two hand-written commands carry it by hand and the same test holds them to it.
**An explicit rule still wins** — the checker returns on the first matching
rule, which is why the restricted default roles are unaffected: Read-Only and
Support, admin and user alike, all end in `*  DENY` and never reach the
fallback. **`ResourceAdmin` is never emitted**: the engine models three roles,
and inventing a grant for a fourth is worse than leaving it to an operator.

**Failover is proven, in all four of its branches.** Two engine replicas in
`ai.services.engine.urls`, driven against the live plugin: with both up the first
serves and the second is untouched; kill the first and the second answers with
nothing visible to the caller; and **a replica that answered is never retried** —
a POST the engine refused with a 400 reached one replica only, which is the rule
that matters, because repeating a state-changing call elsewhere could apply it
twice. With neither reachable the error names the count and the last failure.
That last message used to end in `: null`, because the `IOException` behind a
refused connection can carry no message; `HttpEngineTransport.describe` falls
back to the exception's class name.

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

The spec leaves these unresolved. If a task depends on one, ask rather than assume.
(The target CloudStack release used to be here and is not: SPEC section 14 marks it
decided, 4.23.0.0, by [ADR-0003](docs/adr/0003-cloudstack-release.md).)
- Whether to upstream a generic `ai-services` plugin, keeping metering, guardrails,
  evaluation and LoRA as the differentiated part
- Whether reseller domain admins mirror from Hugging Face directly or only request it.
  **This is what blocks `approveAiModel`**, the one command with no engine route: with
  direct mirroring there is nothing to approve, and `mirrorAiModel` already works
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
templates are deliberately excluded.** There is exactly one —
`aisvc/templates/cloud-init/gpu-node.yaml.j2` — and it renders into a node's
cloud-init, where fourteen comment lines are fourteen lines a node must
download and a human must scroll past to read what actually runs. (An earlier
version of this note justified the exclusion by `config_hash`; that was wrong.
`config_hash` digests the Python dicts `aisvc/k8s` builds, and no Jinja is
involved in a manifest.) New files need the header; the Java generator already
emits it.

**The surface test must iterate the v2 commands too.** `GeneratedSurfaceTest`
drives its five invariants from `new AiServicesManagerImpl().getCommands()`, and
that list omits the twenty v2 commands unless the setting is on — which it is
not, by default, in a unit test. So those classes were never *constructed*:
Codecov put `UploadAiAdapterCmd` at 0% and about 947 lines across the twenty
were untouched. The source now overrides `v2Enabled()`, which took the Java
suite from 481 to 581 — five parameterised tests times twenty commands. Whether
a command is *registered* is `V2CommandRegistrationTest`'s question; the
invariants here are properties of a generated command, not of a release.

**CloudStack runs `codespell` as a pre-commit hook, over identifiers too.** A
private test helper named `deployIn(zoneId, zoneName)` failed the commit four
times over — `deployIn ==> deploying, deploy in`. It is a false positive, but
the fix belongs on our side: renaming it to `deployCommandFor` costs nothing,
while adding a word to their ignore list edits one of their files for our
convenience. Prefer identifiers that do not look like a run-together phrase.
`insert-license`, `codespell`, `flake8` and `markdownlint` all pass over the
branch; the npm-based hooks (`doctoc`, `markdownlint`) cannot install on this
machine — npm refuses the git fetch with `EALLOWGIT` — so markdownlint has to
be run another way, for example `npx markdownlint-cli` inside the harness
container.

**CloudStack's `ui/` runs Jest, ours runs Vitest, and the versions differ.**
Theirs is `@vue/test-utils` **2.0.0** with Vue 3.2.37 under
`@vue/cli-plugin-unit-jest`; ours is 2.4.6 under Vitest. A spec ports by
deleting the `import … from 'vitest'` line — Jest supplies those as globals —
and replacing the config alias for `@/api` with
`jest.mock('@/api', () => require('…/doubles/api.js'))`, because in their tree
`@/api` is the real one. `utils.spec.js` and `fetching.spec.js` port cleanly and
pass there, 88 tests. **`components.spec.js` does not**: on test-utils 2.0.0 the
fourth and later `mount` in a file throws
`Cannot set properties of undefined (setting 'hasOwnProperty')`, which is their
version, not our components — it passes here on 2.4.6. Do not ship it upstream
and do not bump their dependency to make it fit.

**A `.vue` header is `//` line comments, not `<!-- -->`.** CloudStack's
`.pre-commit-config.yaml` runs `insert-license` per file type with an explicit
comment style — `|//|` for Vue, `|#|` for properties, `|--|` for SQL — and
fuzzy-matches the text against
`.github/workflows/license-templates/LICENSE.txt`. The same licence re-wrapped
is **not** a match: properties and SQL get a `TODO: This license is not
consistent` inserted, and a `.vue` file with an HTML-comment header gets a
second, `//`-style one prepended on top of it. Every upstream `.vue` starts
`// Licensed to the Apache…`, so ours do too, and the twelve views here are
byte-identical to what the hook produces. Keep them that way or the next sync
into a CloudStack tree fails the commit.

`docs/installation.md` is the operator-facing install and setup guide: prerequisites,
the four pieces and their order, every config key and environment variable, and an
honest list of what has never been run. Keep it current when any of those change.


Section numbers in the spec: 3 spec schema and validation rules · 4 engine modules · 5 Hugging Face
sourcing · 6 tuning profiles · 7 Tier 1 operability · 8 Tier 2 · 9 CloudStack integration and the
command map · 10 engine REST contract · 11 non-functional requirements · 12 test framework ·
13 layout and milestones · 14 acceptance criteria.
