# Installing CloudStack AI Services

For an operator standing this up for the first time. It covers the four
pieces, the order they have to go in, and the places where the install
will bite you.

> **Read this first.** Milestones 1–9 are implemented and tested, but
> **nothing in this guide has been run end to end against a real
> CloudStack management server.** The engine's own suites pass, the
> plugin compiles and unit-tests, and the UI and Terraform suites pass —
> but every integration test that needs a live management server, a CKS
> cluster or a GPU host is skipped, and `make record` deliberately exits
> 1. Treat this as a build-and-deploy guide for a lab, not a production
> runbook. [What has never run](#what-has-never-run) is the honest list.

## What you are installing

Four pieces, and keeping them straight saves a lot of confusion:

| Piece | Where it runs | What it is |
| --- | --- | --- |
| **Engine** (`aisvc/`) | A host that can reach CloudStack, Postgres, the object store and the CKS API server | Python control plane. Reconciles `platform.yaml` against reality. The only thing that touches CloudStack GPU resources. |
| **Java plugin** (`java-plugin/`) | Inside the CloudStack management server | The front door: auth, RBAC, async jobs, events, usage, limits. The only caller of the engine API besides the operator CLI. |
| **UI section** (`ui/`) | Inside the CloudStack `ui/` tree | The `aiservices` section. Every change goes through the engine, never CloudStack directly. |
| **Gateway** (LiteLLM + `gateway-hooks/`) | In the CKS cluster | Serves tenant-facing OpenAI-compatible traffic and enforces keys, scopes, profiles, quotas, budgets and guardrails. |

Tenants talk to the gateway. Operators and the plugin talk to the
engine. **The engine is never exposed to tenant networks.**

## Before you start

### You need

- **Apache CloudStack 4.23.0.0.** See
  [ADR-0003](adr/0003-cloudstack-release.md) for why, and
  [`java-plugin/README.md`](../java-plugin/README.md) for the 4.22 LTS
  floor the plugin is held to.
- **A CKS (Kubernetes) cluster** in the target zone, and its join
  details — endpoint, token, CA cert hash.
- **NVIDIA GPU hosts** with host tags, and matching service offerings.
  A replica is capped at one host; there is no multi-node tensor
  parallelism.
- **Postgres**, in HA, with backups. The engine keeps runs, actions,
  usage, budgets, profiles, hashed keys, mirror requests and
  evaluations there. Nothing lives only in memory.

  **Yes, this is a second database**, alongside the MariaDB your
  management server already runs, and it is the largest piece of
  infrastructure this product asks for that CloudStack did not already
  require. It is separate on purpose: the gateway writes a usage row
  per request, and a disk filled by AI Services should not be every
  hypervisor operation in the zone failing at once.
  [ADR-0004](adr/0004-state-store.md) has the full reasoning, including
  what it would take to support MariaDB instead.
- **An S3-compatible object store** — Ceph RGW, MinIO or S3 — for
  mirrored model artifacts.
- **Python 3.12** and **JDK 17** (targeting JDK 11 bytecode).
- **Egress to Hugging Face from the mirror worker only.** Inference pods
  get none, enforced by network policy.

### Decisions to make first

Some of these are still open in [SPEC.md](../SPEC.md) and the install
will ask you for an answer:

- Whether reseller domain admins mirror from Hugging Face directly or
  only request it (`ai.services.hf.require.approval`).
- Public Hugging Face Hub, a private mirror, or Enterprise Hub.
- Your initial catalog, licence allow-list and price tiers.

---

## The quick way: `install.py`

Everything in Part 1 — and the reachability checks you would otherwise
run by hand — is scripted:

```bash
python3.12 install.py
```

It prompts for each setting, creates the virtualenv, installs the
engine, writes a mode-0600 `.env`, runs the migrations, validates your
spec, and probes every dependency you configured. For a scripted
install, put the answers in a JSON file:

```bash
python3.12 install.py --answers answers.json --units /etc/systemd/system
```

| Flag | What it does |
| --- | --- |
| `--answers FILE` | Settings as JSON; implies `--yes`. An unknown key is an error, not a typo it ignores. |
| `--yes` | Take what is already in `.env` and the environment; ask nothing. |
| `--dry-run` | Write nothing. Prints every command it would run. |
| `--dev` | Install the test dependencies too. |
| `--units DIR` | Write `aisvc-api.service` and `aisvc-batch.service`. |
| `--skip-migration` | Leave the database alone. |

It is **standard library only** — its first job is to build the
virtualenv the engine lives in, so it cannot import what it installs.
It is idempotent: a second run reports `unchanged` rather than redoing
work, and it reads your previous answers back out of `.env`.

**It does nothing inside CloudStack**, and ends by listing what is
still yours to do — the plugin JAR, the configuration keys, the UI
section and the gateway hooks. Those need a management server, a
restart and decisions it cannot make for you.

The rest of this guide is what the script automates, plus everything it
deliberately leaves alone. Read it before running the script on
anything you care about.

---

## Part 1 — the engine

### Install

`make install` expects the virtualenv to exist already, so create it
first:

```bash
python3.12 -m venv .venv
```

```bash
make install
```

Check it:

```bash
.venv/bin/aisvc --help
```

### Create the database schema

**Run the migrations before anything else touches the database.**

```bash
AISVC_DATABASE_URL="postgresql+pg8000://aisvc:...@db.example/aisvc" .venv/bin/alembic upgrade head
```

Run it from the repository root — `alembic.ini` uses a relative
`script_location`, and it fails from anywhere else.

> **Why this is a step of its own.** `aisvc apply` and `aisvc batch`
> will migrate the database themselves if you forget, but `aisvc serve`
> will not — it is the one command that runs as several replicas, and
> nothing serialises concurrent DDL, so replicas racing to migrate on
> startup is a worse failure than this step. Migrate as a deployment
> step and the auto-migration never fires.
>
> If you are upgrading from a build before this one, see
> [a database built the old way](#a-database-built-the-old-way).

#### A database built the old way

Builds before this one created the schema with
`Base.metadata.create_all()` instead of the migrations. That left two
problems, both of which `upgrade head` now refuses rather than papers
over:

- No `alembic_version` row, so Alembic cannot tell which revision the
  schema is.
- **No `aisvc_job` table**, because `create_all` only builds tables
  whose modules have been imported and the apply path did not import
  that one. Async commands through the API would fail against it.

Migrating such a database stops with a message naming the fix. Because
it may genuinely be missing a table, the safe answer is to drop and
recreate it; only if you have checked it against the migrations and
know it is current should you stamp it:

```bash
AISVC_DATABASE_URL=... .venv/bin/alembic stamp head
```

### Configure it

Secrets come from the environment, never from `platform.yaml`.

| Variable | Needed for | What it is |
| --- | --- | --- |
| `CLOUDSTACK_API_URL`, `CLOUDSTACK_API_KEY`, `CLOUDSTACK_SECRET_KEY` | Anything that observes or writes | The CloudStack API. |
| `AISVC_DATABASE_URL` | `apply`, `serve`, `batch` | Postgres, as a SQLAlchemy URL. |
| `AISVC_CKS_JOIN_ENDPOINT`, `AISVC_CKS_JOIN_TOKEN`, `AISVC_CKS_CA_CERT_HASH` | `apply` | How a new GPU node joins the cluster. |
| `KUBECONFIG` | `apply`, `serve` | Omit it in-cluster and the engine uses the pod's service account. |
| `AISVC_REGISTRY_BUCKET`, `AISVC_REGISTRY_ENDPOINT` | Mirroring and serving | The object store. The bucket defaults to `models`; leave the endpoint unset for AWS S3. Credentials come from boto3's usual chain. |
| `AISVC_TLS_CERT`, `AISVC_TLS_KEY`, `AISVC_TLS_CLIENT_CA` | `serve` | The engine's mTLS material. See below. |
| `AISVC_HF_TOKEN`, `AISVC_HF_ENDPOINT` | Mirroring | A gated-model token, and a private Hub if you use one. |
| `AISVC_PROMETHEUS_URL` | Autoscaling | **Autoscaling is opt-in on this.** Without it the engine holds at the replica floor. |
| `AISVC_NAMESPACE` | `apply` | Kubernetes namespace for the deployments. |
| `AISVC_GATEWAY_URL`, `AISVC_GATEWAY_SERVICE` | `apply`, `batch` | The gateway's base URL, and its in-cluster service name. The service defaults to `aisvc-gateway.aisvc-serving.svc.cluster.local`. |

Two more are read by `aisvc serve` when the API loads the spec, and by
`aisvc status` when it calls the engine from outside:

| Variable | Needed for | What it is |
| --- | --- | --- |
| `AISVC_SPEC`, `AISVC_LICENSES` | `serve` | Where the API reads `platform.yaml` and the licence allow-list. `AISVC_SPEC` defaults to `platform.yaml` in the working directory. |
| `AISVC_ENGINE_URL`, `AISVC_TLS_CA` | `status` | The engine's base URL, and the CA to verify it against. `status` also uses `AISVC_TLS_CERT` / `AISVC_TLS_KEY` as its client certificate. |

### Run the API

```bash
.venv/bin/aisvc serve --host 0.0.0.0 --port 8443
```

**The engine trusts the caller identity its clients assert**, which is
only safe because mTLS proves who they are. `aisvc/api/server.py`
enforces this: with no `AISVC_TLS_*` set it binds **loopback only**, and
it will not bind a non-loopback address without TLS configured. Never
weaken `ssl_cert_reqs`.

Generate a server certificate and a client CA, give the management
server a client certificate signed by that CA, and keep the engine off
tenant networks.

---

## Part 2 — the Java plugin

### Build CloudStack's modules first

CloudStack publishes **no Maven artifacts** — not to Maven Central, not
to the ASF repositories. Build the dependencies into your local
repository:

```bash
git clone --depth 1 --branch 4.23.0.0 --single-branch https://github.com/apache/cloudstack.git
```

```bash
cd cloudstack && mvn install -DskipTests -Dcheckstyle.skip -Dlicense.skip -Drat.skip -pl api,utils,framework/config,framework/db -am
```

JDK 17 is needed to build. On macOS `openjdk@17` is keg-only:

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
```

### Build and test the plugin

```bash
make test-java
```

It fails with a clear message if the CloudStack artifacts are absent.

### Deploy it

Drop the built JAR into the management server's classpath and restart
it. `META-INF/db/schema-ai-services.sql` creates the `ai_deployment`
ownership table — deliberately the only table the plugin owns, so
CloudStack enforces ownership, list filtering and cleanup natively while
the engine stays the source of truth for runtime state.

### Set the configuration keys

All nine, with their defaults:

| Key | Default | Scope | What it does |
| --- | --- | --- | --- |
| `ai.services.enabled` | `false` | Zone | Turns the section on for a zone. |
| `ai.services.engine.urls` | — | Global | Engine replicas, comma-separated. The client fails over across them. |
| `ai.services.engine.timeout` | `30` | Global | Seconds. |
| `ai.services.engine.tls.client.cert` | — | Global | The plugin's client certificate. |
| `ai.services.engine.tls.client.key` | — | Global | Its key. |
| `ai.services.engine.tls.truststore` | — | Global | What it trusts the engine against. |
| `ai.services.hf.require.approval` | `true` | Domain | Whether a domain admin may mirror directly or only request it. |
| `ai.services.maintenance.drain.timeout` | `900` | Global | Seconds to wait draining GPU nodes before host maintenance. |
| `ai.services.v2.enabled` | `false` | Global | Registers the Tier 2 commands. See [Turning on v2](#turning-on-v2). |

The three TLS keys are not optional. **Never weaken the mTLS
requirement to a fallback** — the engine filters every response by what
the plugin asserts about the caller, and a caller who could influence
that could read another tenant's data.

---

## Part 3 — the UI section

Paths mirror CloudStack's own, so the files copy across unchanged:

| Here | In CloudStack's `ui/` |
| --- | --- |
| `ui/src/config/section/aiservices.js` | same path |
| `ui/src/views/aiservices/*.vue` | same path |
| `ui/public/locales/en.json` | merged into theirs |

Then import and list the section in `src/config/router.js`, beside
`compute`, `storage` and the rest, and rebuild the UI.

You do not configure which screens appear. CloudStack drops any route
whose `permission` is missing from the account's `listApis` answer, and
then drops a section with no children — so screens follow the commands
the plugin registered, and the v2 screens appear and disappear with
`ai.services.v2.enabled` alone.

---

## Part 4 — the gateway

LiteLLM Proxy, per [ADR-0002](adr/0002-gateway.md). Mount
`gateway-hooks/` into the proxy container and reference it by path:

```yaml
litellm_settings:
  callbacks: ["enforcement.AisvcEnforcement"]
```

`gateway-hooks/` is **not a Python package** — the directory name has a
hyphen. The handlers are thin on purpose: every rule lives in
`aisvc.gateway` and `aisvc.profiles`, which the engine also uses, so
what the gateway enforces and what `getAiEffectiveParameters` reports
come from one implementation and cannot drift.

Stay on LiteLLM's MIT side. Nothing from `enterprise/`, no licence-gated
features — see [third-party-licenses.md](third-party-licenses.md).

---

## Part 5 — Terraform (optional)

The resources in `terraform/` are meant to land in
`terraform-provider-cloudstack` upstream; they live here so they can be
tested alongside the commands they call. See
[`terraform/README.md`](../terraform/README.md) for the resource list
and the write-once handling of key and certificate material.

---

## Bringing up your first zone

### 1. Build the golden image

`templates/packer/gpu-golden.pkr.hcl` builds the GPU node template.
`templates/packer/proxy.pkr.hcl` builds the private-endpoint proxy
image, and is only needed if tenants will attach endpoints to their own
VPC tiers.

Both pin the CloudStack Packer plugin at `>= 1.0.2`, which is its newest
release.

### 2. Write your `platform.yaml`

Start from [`examples/platform.yaml`](../examples/platform.yaml). The
prices there are placeholders, not recommendations.

Two conventions to know before you size anything:

- **Memory is GiB throughout.** `gpu_mem_gb: 80` means 80 GiB.
- **Parameter counts are decimal billions**, and a model's optional
  `architecture` block is what capacity sizes from. It is filled in
  automatically at mirror time; the example writes it out so `aisvc
  plan` can size a model before anything has been mirrored.

### 3. Validate

```bash
.venv/bin/aisvc validate --spec examples/platform.yaml
```

Offline, no credentials. It reports every rule in SPEC.md section 3 at
once rather than stopping at the first.

### 4. Plan

Against nothing, to see the shape:

```bash
.venv/bin/aisvc plan --spec examples/platform.yaml --no-observe
```

Against live CloudStack capacity (the default; needs the three
`CLOUDSTACK_*` variables):

```bash
.venv/bin/aisvc plan --spec examples/platform.yaml
```

### 5. Dry-run the apply

```bash
.venv/bin/aisvc apply --spec examples/platform.yaml --dry-run
```

**`--dry-run` makes zero API writes.** It records the run, takes no zone
lock, and prints the plan diff.

### 6. Apply

```bash
.venv/bin/aisvc apply --spec examples/platform.yaml
```

It converges in dependency order — capacity, models, profiles, routes —
taking a Postgres advisory lock so one leader runs per zone. A failed
action stops only its dependants. `-y` skips the confirmation.

**A second `apply` must produce an empty plan.** If it does not,
something is not converging and that is a bug, not a quirk.

### 7. Check it

`status` asks the engine's API rather than CloudStack, so point it at
the engine and give it a client certificate:

```bash
AISVC_ENGINE_URL=https://engine.internal:8443 AISVC_TLS_CERT=client.pem AISVC_TLS_KEY=client.key AISVC_TLS_CA=ca.pem .venv/bin/aisvc status
```

Without `AISVC_ENGINE_URL` it says so and exits rather than guessing.

Admins also get the setup wizard in the UI, which runs the same
preflight checks — the platform spec, the model registry, and capacity
for every deployment. A capacity check with nothing observed reports
`skipped` with a remedy rather than failing.

---

## Turning on v2

Tier 2 — multi-LoRA adapters, embeddings and rerank, batch, guardrails,
the evaluation gate and private endpoints — is off by default. A GA
install should not advertise an API for features it does not run.

Set `ai.services.v2.enabled` to `true` and restart the management
server. That one setting registers the nineteen Tier 2 commands, which
makes them reachable through CloudMonkey, makes the v2 UI screens appear
on their own, and makes the Terraform v2 resources work. There is no
second switch anywhere.

The batch worker is separate, and **has no leader election — run
exactly one**:

```bash
.venv/bin/aisvc batch
```

---

## What has never run

Stated plainly so you know what you are walking into:

- **No CloudStack has loaded the plugin.** It compiles and 423 unit
  tests pass; it has never been deployed to a management server.
- **No real CloudStack UI has loaded the section.** All 47 Playwright
  specs skip without a live server.
- **No Terraform acceptance test has run** against a live management
  server, v1 or v2.
- **No proxy VM has booted.** The provisioner plans, creates and
  destroys; nothing has run it.
- **The self-test cannot run.** `setup` plans it, but its steps need GPU
  capacity, so SPEC.md section 14's first acceptance criterion is unmet.
- **LiteLLM is not yet a real dependency**, and there is no rollout
  between blue and green.
- **Adapter artefacts are not fetched, evaluations are not produced, and
  the guard model is not called.** The v2 screens list what the API
  records.
- **Host maintenance drains automatically only where an event bus is
  configured.** CloudStack 4.23 offers no synchronous pre-maintenance
  hook, and no bus is enabled by default.

## Things that will bite you

- **Renewing an uploaded certificate replaces its proxy VM**, which is
  downtime: a proxy fetches its TLS material once, at boot.
- **Passthrough GPUs cannot live-migrate.** Draining **destroys** a
  node, it does not stop it — `gpu.detach.on.stop` defaults to false, so
  a stopped node keeps its GPU allocated ([ADR-0003](adr/0003-cloudstack-release.md)).
- **GPU nodes are named `aisvc-<pool>-<ordinal>`**, and pool membership
  is read back from that name. Changing the scheme without a migration
  path strands every node already deployed.
- **A Kubernetes LoadBalancer Service cannot be placed on a chosen
  network.** The CloudStack cloud-controller-manager takes a service's
  network from the nodes' first NIC. That is why a private endpoint is a
  proxy VM and not a Service.
- **`createAiBudget` is granted to every role but served admin-only.**
  A tenant calling it gets a 403 from the engine. The UI already hides
  the action; CloudMonkey and Terraform do not.

## Where to look next

| For | Read |
| --- | --- |
| Why this exists at all | [why-cloudstack-ai-services.md](why-cloudstack-ai-services.md) |
| The specification | [SPEC.md](../SPEC.md) |
| What is built, and what is not | [PROGRESS.md](../PROGRESS.md) |
| Decisions already made | [docs/adr/](adr/) |
| Building the plugin | [java-plugin/README.md](../java-plugin/README.md) |
| The UI section | [ui/README.md](../ui/README.md) |
| Terraform resources | [terraform/README.md](../terraform/README.md) |
| Licence policy | [third-party-licenses.md](third-party-licenses.md) |
