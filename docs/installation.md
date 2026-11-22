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
> runbook. [What has and has not run](#what-has-and-has-not-run) is the
> honest list, and `make stack-up` gives you a CloudStack to try it on.

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
- **A Kubernetes cluster** in the target zone, and its join details —
  endpoint, token, CA cert hash.

  A zone names its cluster with `cks_cluster`, which means a CKS
  cluster CloudStack manages. To point at a cluster it does not — your
  own kubeadm, EKS or anything else — use the long spelling instead,
  and preflight stops asking CloudStack about it:

  ```yaml
  kubernetes:
    source: external
    cluster: our-own-cluster
  ```

  Or let the engine build one. With `create: true` on a `cks` zone,
  `aisvc apply` creates the cluster before any GPU node that needs it —
  taking the newest enabled Kubernetes version and the smallest service
  offering that clears its minimums, and naming both in the plan before
  you confirm. Pin either with `version:` and `offering:` if you would
  rather choose.

  That needs CKS itself to be set up by a CloudStack administrator
  first: `cloud.kubernetes.service.enabled`, `endpoint.url`, a default
  network offering, and a registered Kubernetes version ISO. The engine
  never does any of that, and says so by name when something is missing.

  Either way you do not need a kubeconfig for a `cks` cluster: the
  engine fetches it from CloudStack. That is what makes `create: true`
  end to end — otherwise you would have to fetch one by hand in between
  the engine building a cluster and the engine using it.

  Nodes still join with `kubeadm join` whichever source you choose.
  [ADR-0006](adr/0006-gpu-node-join.md) proposes moving that onto CKS;
  none of it is built.
- **NVIDIA or AMD GPU hosts** with host tags, and matching service
  offerings. A replica is capped at one host; there is no multi-node
  tensor parallelism. CloudStack discovers GPUs by PCI class rather
  than by vendor, so AMD cards are inventoried like any other; set
  `vendor: amd` on the `gpuClass` so the engine asks for `amd.com/gpu`
  and runs a ROCm image. **No AMD node has booted** — see
  [What has and has not run](#what-has-and-has-not-run).
- **A GPU device plugin in the Kubernetes cluster.** Nothing in this
  project installs one, for either vendor. Kubernetes does not know a
  GPU exists until a device plugin advertises it as an extended
  resource, and the engine asks for that resource by name —
  `nvidia.com/gpu` or `amd.com/gpu`, from the class's `vendor`. Install
  NVIDIA's `k8s-device-plugin` (or the GPU Operator) for NVIDIA nodes,
  and ROCm's `k8s-device-plugin` (or the AMD GPU Operator) for AMD
  ones. `aisvc setup` checks this for you — one check per declared
  vendor — so you do not have to remember `kubectl describe node`.
- **Postgres**, in HA, with backups. The engine keeps runs, actions,
  usage, budgets, profiles, hashed keys, mirror requests and
  evaluations there. Nothing lives only in memory.

  **Or MariaDB / MySQL**, if you would rather not run a second database
  engine: `pip install -e ".[mariadb]"` and use a
  `mysql+pymysql://…?charset=utf8mb4` URL, or let `install.py
  --database mariadb` build it. Use the `mariadb` extra, not the
  package called `mariadb`, which is LGPL and therefore prohibited
  here.

  **Either way, give it its own server.** The gateway writes a usage
  row per request and the reconciler holds a connection for a whole
  run; a disk filled by AI Services should not be every hypervisor
  operation in the zone failing at once. Sharing CloudStack's MariaDB
  works and is not recommended.
  [ADR-0004](adr/0004-state-store.md) has the reasoning.
- **An S3-compatible object store** — Ceph RGW, MinIO or S3 — for
  mirrored model artifacts.
- **Python 3.12** and **JDK 17** (targeting JDK 11 bytecode).
- **Egress to Hugging Face from the mirror worker only.** Note that the
  network policy meant to deny inference pods any egress is specified
  and **not yet implemented**, so plan to enforce it at the cluster or
  the network until it is.

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
| `--database postgres\|mariadb` | Which backend the engine's state lives in. Default: taken from your URL, else postgres. |
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
| `KUBECONFIG` | `apply`, `serve` | **Usually unnecessary now.** `plan` and `apply` fetch the kubeconfig from CloudStack with `getKubernetesClusterConfig` when a zone names a `cks` cluster. Set it to override that, for an `external` cluster, or for `serve` outside the cluster; omit it in-cluster and the engine uses the pod's service account. |
| `AISVC_KUBECONFIG_DIR` | `plan`, `apply` | Where a fetched kubeconfig is written, `0600` in a `0700` directory. Defaults to a per-user directory under the system temp dir. |
| `AISVC_REGISTRY_BUCKET`, `AISVC_REGISTRY_ENDPOINT` | Mirroring and serving | The object store. The bucket defaults to `models`; leave the endpoint unset for AWS S3. Credentials come from boto3's usual chain. |
| `AISVC_TLS_CERT`, `AISVC_TLS_KEY`, `AISVC_TLS_CLIENT_CA` | `serve` | The engine's mTLS material. See below. |
| `AISVC_HF_TOKEN`, `AISVC_HF_ENDPOINT` | Mirroring | A gated-model token, and a private Hub if you use one. |
| `AISVC_PROMETHEUS_URL` | Autoscaling | **Autoscaling is opt-in on this.** Without it the engine holds at the replica floor. |
| `AISVC_NAMESPACE` | `apply` | Kubernetes namespace for the deployments. |
| `AISVC_REGISTRY_FROM_CLOUDSTACK` | Mirroring and serving | Set it on to take the bucket, endpoint and credentials from CloudStack's object store instead of the three variables above ([ADR-0005](adr/0005-model-artifact-storage.md)). The bucket must already exist; the engine resolves it and never creates one. |
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
cd cloudstack && mvn install -DskipTests -Dcheckstyle.skip -Dlicense.skip -Drat.skip -pl api,utils,framework/config,framework/db,framework/events,engine/schema -am
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
it. The plugin creates its own `ai_deployment` — deliberately the only table the plugin owns, so
CloudStack enforces ownership, list filtering and cleanup natively while
the engine stays the source of truth for runtime state.

### Set the configuration keys

All nine, with their defaults:

| Key | Default | Scope | What it does |
| --- | --- | --- | --- |
| `ai.services.enabled` | `false` | Zone | Turns the section on for a zone. |
| `ai.services.engine.urls` | — | Global | Engine replicas, comma-separated. The client fails over across them. |
| `ai.services.engine.timeout` | `30` | Global | Seconds. |
| `ai.services.engine.tls.client.cert` | — | Global | Path to the **PKCS12 key store** holding the plugin's client certificate and key. |
| `ai.services.engine.tls.client.key` | — | Global | **That key store's password, not a path.** The description said "path" for a while; the code has always read it as a password. It is in the `Secure` category — see the warning below. |
| `ai.services.engine.tls.truststore` | — | Global | Path to the PKCS12 trust store holding the engine's CA. |
| `ai.services.engine.tls.truststore.password` | `changeit` | Global | The trust store's password. It needs one — see below. |
| `ai.services.hf.require.approval` | `true` | Domain | Whether a domain admin may mirror directly or only request it. |
| `ai.services.maintenance.drain.timeout` | `900` | Global | Seconds to wait draining GPU nodes before host maintenance. |
| `ai.services.v2.enabled` | `false` | Global | Registers the Tier 2 commands. See [Turning on v2](#turning-on-v2). |

> **The `Secure` category is a request, not a guarantee.** CloudStack
> encrypts a configuration value only when the management server has
> database encryption switched on in `db.properties`
> (`db.cloud.encryption.type`). Measured on a stock 4.23.0.0 with the
> default `db.cloud.encryption.type=none`, the key store password set
> here came back from `listConfigurations` in cleartext and was stored
> in `cloud.configuration` in cleartext. If that matters to you —
> and for a key store password it should — turn database encryption on
> before setting it, and treat anyone who can call `listConfigurations`
> as able to read it until you have.

**Generate all of it with `install.py`:**

```bash
python3 install.py --make-certs engine.internal
```

That writes a CA, the engine's certificate with `engine.internal` as a
subjectAltName, the plugin's client certificate, and the two PKCS12
stores into `./tls`. It fills in the three `AISVC_TLS_*` settings in
`.env` and prints the four CloudStack keys. Pass an IP instead of a
name and it becomes an IP SAN. It refuses to replace an existing CA
without `--force`, because replacing one invalidates every client
certificate already issued from it.

**The trust store has to be built by `keytool`, not `openssl`.** A
PKCS12 trust store openssl writes loads in Java as **zero
certificates** rather than failing, and the plugin would then trust
nothing and fail every call at the handshake. `install.py` uses keytool
where it finds a working one and prints the single command to run on
the management server where it does not. The plugin now refuses an
empty trust store by name rather than building a context that trusts
nothing.

The TLS keys are not optional. **Never weaken the mTLS
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

`templates/packer/gpu-golden.pkr.hcl` builds the GPU node template, and
`templates/packer/gpu-golden-rocm.pkr.hcl` its ROCm equivalent for a
`gpuClass` declaring `vendor: amd`. `templates/packer/proxy.pkr.hcl`
builds the private-endpoint proxy image, and is only needed if tenants
will attach endpoints to their own VPC tiers.

All three pin the CloudStack Packer plugin at `>= 1.0.2`, which is its
newest release.

The ROCm template takes **two** version variables, `amdgpu_version` and
`rocm_version`, because `repo.radeon.com/amdgpu` and
`repo.radeon.com/rocm/apt` version independently — at the time of
writing `rocm/apt/7.2.4` exists and `amdgpu/7.2.4` is a 404. The
defaults, `7.0.3` for both, are a pair that resolves for `noble` on
both repositories. Check a new pair before changing either.

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
preflight checks — the platform spec, the model registry, the CKS
cluster each zone names, whether the engine can reach the Kubernetes
API, every `gpu_card` against CloudStack's own catalogue, and capacity
for every deployment. A check with nothing observed
reports `skipped` with a remedy rather than failing.

The GPU card check is worth reading too. A `gpu_class` may narrow its
count to one card name, matched exactly against `gpucardname` from
`listGpuDevices` so that a host carrying two kinds of GPU stays
countable. A name matching nothing counts zero GPUs, and the only
symptom is a capacity warning about no room to place a node — on hosts
full of idle cards. The check names what CloudStack does have.

The cluster checks are worth reading before a first apply. `cks_cluster`
was previously never verified against CloudStack, so a typo produced GPU
nodes that deployed, billed and then failed to join a cluster nobody had
looked for. A cluster CloudStack does not list and a CloudStack it could
not ask are reported as two different failures with two different
remedies, because the first means editing `platform.yaml` and the second
does not.

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

## Testing it without a management server of your own

`make stack-up` brings up Apache CloudStack 4.23.0.0 (the project's own
simulator image), Postgres, and MinIO behind a profile. The engine runs
on your host and reaches them on localhost, so `aisvc plan --observe`
here is the same code path an operator runs — only the far end is
simulated.

```bash
make stack-up
```

It takes several minutes: the health check waits for the API to answer
rather than for the container to start, because jetty binds long before
CloudStack is ready. On Apple Silicon the image runs emulated, which
works and is slower.

A fresh simulator has an empty database, so deploy a zone and give it
GPUs before there is anything to observe:

```bash
docker exec $(docker compose -f deploy/compose/docker-compose.yml ps -q cloudstack) python3 -m marvin.deployDataCenter -i /root/setup/dev/advanced.cfg
```

Then register API keys for `admin` through CloudStack's own API, tag
two hosts `h100-sxm`, and create GPU cards and devices on them with
`createGpuCard` and `createGpuDevice`.
[`examples/simulator.yaml`](../examples/simulator.yaml) is a spec that
matches what that leaves you with, and its header has the detail.

```bash
make stack-down   # and `colima stop` if that is your runtime
```

**One caveat worth knowing.** The hosts are simulated. The API surface,
the request signing, the database schema and the responses are a real
CloudStack; the hypervisors and the GPUs are not. It proves the engine
talks to CloudStack correctly. It proves nothing about a GPU.

## What has and has not run

Stated plainly so you know what you are walking into.

**Verified against a real CloudStack 4.23.0.0**, with the plugin loaded
into the management server and the engine answering it over mTLS:

*The engine against CloudStack*

- Request signing, including values containing spaces — which is how a
  signing bug was found, since every offering a simulator ships has one
  in its name.
- `take_snapshot` reading real GPU capacity from tagged hosts, and the
  capacity planner producing the right plan and warnings from it. A
  second bug surfaced here: sixteen GPUs CloudStack reported as
  `state=Error` were counted as free, because only `managedstate` was
  being read.
- `listKubernetesClusters`, `listGpuCards`, `listGpuDevices`, and the
  tenant-network lookup.
- All eight database migrations, on Postgres 16 rather than SQLite.

*The plugin inside the management server*

- **It loads.** All 38 v1 commands register — `listApis` goes from 865
  to 903, exactly the thirty-eight — and with `ai.services.v2.enabled`
  on, to 923 with all twenty v2 commands. The gate works both ways.
- `AiServicesSchema` creates `ai_deployment` at startup, and all ten
  `ai.services.*` settings appear in `listConfigurations`.
- **mTLS end to end.** The engine refuses a caller with no certificate
  — the handshake fails, not a 401 — and answers one with it.
- Commands return real data: `listAiModels`, `listAiEndpoints`,
  `listAiSetupChecks` and the v2 lists.
- **v2 writes work**, which they could not before: a guardrail profile,
  a batch, an evaluation, and a real 1,038-character certificate
  uploaded by an admin and by a tenant.
- **The async bridge runs.** `createAiBatch` returns a job id,
  `queryAsyncJobResult` polls it through CloudStack's own job states,
  and a failure arrives carrying the engine's reason.
- **Caller context is enforced.** One `listAiModels` returns two models
  to root admin, none to an account in `/ROOT` and two to one in
  `/Customers/Acme`, because the models are published to `/Customers`.
- **Failover, in all four branches**: the first replica serves, a dead
  one is skipped, a replica that *answered* is never retried, and with
  none reachable the error names the count and the cause.

*The tests*

- **Marvin: 7 tests, 58 commands, three roles, allowed and denied** —
  SPEC.md section 12's requirement, passing. Note that `nosetests`
  cannot run them, or any CloudStack Marvin test, on Python 3.12;
  `test/integration/plugins/ai-services/run_without_nose.py` does.
- **The whole Playwright suite: 40 passed, 7 skipped**, against the
  plugin and the UI in the same management server. The seven are the
  `ai.services.v2.enabled` *off* group, which cannot run while the
  setting is on, so nothing is skipped for being unbuilt.

**Still never run:**

- **No AMD GPU has been seen by this code.** CloudStack's own discovery
  of AMD cards was verified by reading `gpudiscovery.sh` at the
  `4.23.0.0` tag, and every ROCm repository path, package name and
  image tag the engine uses was checked against the real registries —
  but no ROCm image has been baked and no AMD node has booted. The AMD
  path is unit-tested only.
- **Nothing has been provisioned.** `apply` builds a deployer that
  needs a Kubernetes cluster, so the write half of the reconciler —
  `deployVirtualMachine` and everything after it — is unexercised.
- **Two of the eight Terraform acceptance tests have never run.** The
  other six pass against a live management server; those two skip,
  wanting a real `.safetensors` and a real deployment.
- **No proxy VM has booted.** The provisioner plans, creates and
  destroys; nothing has run it.
- **Nothing has been mirrored from Hugging Face.** The catalogue and
  registry screens list what `platform.yaml` declares, so they are not
  empty, but no artefact has been fetched and no `sha256` verified.
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
- **The simulator's GPUs are simulated.** The API, the signing and the
  schema are real; the cards are not. Its hypervisor does boot VMs,
  though — a CKS cluster's control node and worker both reached
  `Running` — so `deployVirtualMachine` is exercised even where nothing
  runs inside the guest.
- **One command points at an engine route that does not exist.**
  `approveAiModel`. CloudStack accepts the call and the engine answers
  404. It is not waiting on a route: what approval *does* depends on
  SPEC section 14's open question about whether reseller domain admins
  mirror from Hugging Face directly or only request it. It is listed as
  `NOT_YET_SERVED` in `tests/contract/test_engine_api_contract.py`,
  which fails if the list grows or if it quietly starts working.
- **A model is deployed by `platform.yaml` unless it says otherwise.**
  `placement: spec` is the default, and the deployment commands refuse
  such a model by naming the field. Set `placement: api` on a model to
  hand that decision to `deployAiModel`; it is then catalogued and runs
  nowhere until a deployment places it ([ADR-0008](adr/0008-api-deployments.md)).
  A replica count outside the model's declared `min`/`max` is refused,
  not clamped.

## Things that will bite you

- **Renewing an uploaded certificate replaces its proxy VM**, which is
  downtime: a proxy fetches its TLS material once, at boot.
- **A missing GPU device plugin looks like nothing at all.** Without
  one the node never advertises `nvidia.com/gpu` or `amd.com/gpu`, so
  every inference pod sits `Pending` for ever with no error anywhere:
  the engine's reconcile succeeds, the Deployment exists, and the
  scheduler simply never finds a node that satisfies the request.
  `kubectl describe pod` names it — *"Insufficient nvidia.com/gpu"* —
  and nothing else will. Installing the wrong vendor's plugin fails
  identically, which is the mode to watch for on a mixed fleet. The
  preflight check catches both: it fails only once GPU nodes have
  joined and none advertises the resource, and warns rather than fails
  before any node exists, so a fresh zone is not red for no reason.
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
- **A failed command is not recorded as a `Failed` event.** CloudStack
  closes an action event as `Completed` whether it succeeded or not and
  carries the outcome in `level` — `INFO` or `ERROR`. Anything reading
  these rows, including an alert or a report, has to read the level and
  not the state.
- **`ai.services.engine.*` changes need a management-server restart.**
  The keys are declared dynamic, but the plugin caches its engine client
  in a field, so a new URL or certificate path does not take effect
  until the server restarts.
- **`ai.services.enabled` gates nothing.** `isEnabledInZone` has no
  callers.

## Where to look next

| For | Read |
| --- | --- |
| Why this exists at all | [why-cloudstack-ai-services.md](why-cloudstack-ai-services.md) |
| Every limit and default | [feature-spec-sheet.md](feature-spec-sheet.md) |
| The specification | [SPEC.md](../SPEC.md) |
| What is built, and what is not | [PROGRESS.md](../PROGRESS.md) |
| Decisions already made | [docs/adr/](adr/) |
| Building the plugin | [java-plugin/README.md](../java-plugin/README.md) |
| The UI section | [ui/README.md](../ui/README.md) |
| Terraform resources | [terraform/README.md](../terraform/README.md) |
| Licence policy | [third-party-licenses.md](third-party-licenses.md) |
