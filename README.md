# CloudStack AI Services

A native [Apache CloudStack](https://cloudstack.apache.org/) plugin,
backed by a Python engine, that lets a cloud provider source open-weight
LLMs from Hugging Face, deploy them on NVIDIA GPU capacity, tune them
through profiles, and sell them to tenants as OpenAI-compatible
endpoints.

The tenant sees a familiar API. The operator sees GPUs, quotas, budgets
and usage records in the same console they already run their cloud from.

> **Status: not production software.** Every milestone is implemented
> and the test suites are green. More to the point, it now runs against
> a real Apache CloudStack 4.23.0.0 — `make stack-up` brings one up.
> **The plugin loads into the management server**, registers its
> commands, talks to the engine over mTLS, and answers with real data;
> the UI section is built and served at `/client`; the Marvin tests pass
> for all 58 commands across three roles, allowed and denied; and
> failover between engine replicas behaves as specified.
>
> But **no GPU node has booted and no model has served a token.** The
> write half of the reconciler needs a Kubernetes cluster, nothing has
> been mirrored from Hugging Face, and the simulator's GPUs are
> simulated. [docs/installation.md](docs/installation.md) ends with an
> honest list of what has and has not run, and
> [PROGRESS.md](PROGRESS.md) keeps the long version.

## How it fits together

```
tenant ──► Gateway (LiteLLM)  ──►  model Deployments on CKS
                │                          ▲
                │ usage, keys, budgets     │ renders, scales, rolls out
                ▼                          │
operator ──► CloudStack UI / API ──► Java plugin ──mTLS──► Python engine
                                                              │
                                      reconciles platform.yaml│
                                                              ▼
                                            CloudStack: GPU hosts, VMs,
                                            networks, object storage
```

Five pieces, with one rule each:

- **CloudStack** is the IaaS authority and supplies GPU capacity.
  Nothing bypasses it for scheduling.
- **CKS (Kubernetes)** serves the models.
- **The Python engine** (`aisvc/`) reconciles desired state
  (`platform.yaml`) against reality. It is the only thing that touches
  CloudStack GPU resources.
- **The Java plugin** (`java-plugin/`) is the front door: auth, RBAC,
  async jobs, events, usage, limits. It is the only caller of the engine
  API besides the operator CLI, and the engine is never exposed to
  tenants.
- **The gateway** (`gateway-hooks/`) serves tenant traffic and enforces
  keys, scopes, profiles, quotas, budgets and guardrails.

## Quick start

```bash
python3 install.py
```

That builds the virtualenv, installs the engine, writes `.env`, runs the
database migrations and checks it can reach what it needs. Then:

```bash
.venv/bin/aisvc validate --spec examples/platform.yaml --licenses licenses.yaml
```

```bash
.venv/bin/aisvc plan --spec examples/platform.yaml --no-observe
```

```bash
python3 install.py --make-certs engine.internal
```

The last one generates the mTLS material both sides need — the engine
refuses to bind a non-loopback address without it, and the plugin
refuses to call the engine without it.

The full guide, including the Java plugin, the UI section and the
gateway, is [docs/installation.md](docs/installation.md).

## Layout

| Path | What it is |
| --- | --- |
| `aisvc/` | The Python engine, one package per module |
| `java-plugin/` | `plugins/ai-services` — the CloudStack plugin |
| `ui/` | The `aiservices` section for CloudStack's `ui/` tree |
| `gateway-hooks/` | LiteLLM handlers; they import `aisvc.profiles` directly |
| `terraform/` | The Terraform provider |
| `templates/` | Packer images for GPU nodes and endpoint proxies |
| `examples/` | A `platform.yaml` to start from |
| `docs/` | [Everything written down](docs/README.md) |
| `tests/` | unit, contract, integration, gpu, load, chaos, security |

## Working on it

```bash
make check
```

Lint, types, unit, contract, integration and the licence policy. The
suites on their own:

```bash
make test-unit && make test-java && make test-ui && make test-tf
```

Building `java-plugin/` needs CloudStack's own modules in `~/.m2` first
— CloudStack publishes no Maven artifacts. `java-plugin/README.md` has
the two commands, and `make test-java` says so plainly if they are
missing.

Three conventions worth knowing before you change anything:

- **Tests first.** Write the test, watch it fail, then write the code.
  Every bug gets a regression test before the fix.
- **Idempotence and dry-run.** Every action supports `--dry-run`, which
  makes zero API writes, and a second `apply` must produce an empty
  plan.
- **Say what is not true.** Where something is unbuilt or unverified,
  the documents say so rather than omitting it. Keep it that way.

[CLAUDE.md](CLAUDE.md) is the working brief: current state, the
division of responsibility, the decisions already made and the ones
deliberately left open.

## Licence

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

The dependency tree follows the ASF Category A/B/X policy, which is
enforced rather than advisory: **GPL, LGPL and AGPL are prohibited**,
Python imports included. `make test-security` fails CI on any Category
X dependency, any licence it cannot classify, and any new Category B
package not already listed.

[docs/third-party-licenses.md](docs/third-party-licenses.md) covers
what the scan cannot see — the NVIDIA driver EULA on the golden image,
the use restrictions in open-weight model licences, and LiteLLM's
commercially licensed directory, which this project does not use.
