# Documentation

Everything written down about CloudStack AI Services, and where to
start depending on why you are here.

## Start here

| If you want to | Read |
| --- | --- |
| Install and run it | [installation.md](installation.md) |
| Know what it does, exactly | [feature-spec-sheet.md](feature-spec-sheet.md) |
| See what it looks like | [ui-guide.md](ui-guide.md) |
| Understand why it exists | [why-cloudstack-ai-services.md](why-cloudstack-ai-services.md) |
| Know how it is put together | [the architecture walkthrough](https://claude.ai/artifact/8CQa9yPN8jDbYYDkKNGpdH) |
| Change the code | [../CLAUDE.md](../CLAUDE.md), then the ADRs below |

## In this folder

- **[installation.md](installation.md)** — the operator-facing install
  and setup guide: prerequisites, the four pieces and the order they go
  in, every configuration key and environment variable, how to bring up
  a CloudStack to test against with `make stack-up`, and an honest list
  of what has and has not been run.
- **[ui-guide.md](ui-guide.md)** — the AI Services UI section screen by
  screen, with screenshots: what each role sees, why screens appear and
  disappear with the commands rather than with any setting, and the
  gaps that are still there.
- **[feature-spec-sheet.md](feature-spec-sheet.md)** — the
  limits-and-defaults sheet. Every threshold, TTL, supported value and
  surface count, each read from the code rather than the specification.
  A gap is marked **·not built** rather than left out.
- **[why-cloudstack-ai-services.md](why-cloudstack-ai-services.md)** —
  the positioning document: who this is for and what it lets an
  operator sell. Every product claim in it is traceable to
  [SPEC.md](../SPEC.md).
- **[third-party-licenses.md](third-party-licenses.md)** — the
  dependency licence policy and what the automated scan cannot see.
- **[../test/integration/plugins/ai-services/](../test/integration/plugins/ai-services/)**
  — the Marvin tests: every command, every default role, allowed and
  denied, with a runner for the Python 3.12 images where `nosetests`
  cannot import.
- **[adr/](adr/)** — the decisions, and why. Read one before
  relitigating it. 0001–0005 are accepted, 0008 is accepted and built;
  0006, 0007 and 0009 are proposed, and each says what is built and
  what is not.

## Presentations

These are published pages rather than files in the repository, so they
are linked rather than stored. They are snapshots: where one disagrees
with the documents above, the documents are current.

| Deck | What it covers |
| --- | --- |
| [Why AI Services Belongs in CloudStack](https://claude.ai/artifact/Q9XPJof6dFL9WvGpqCZ1Vn) | The commercial case, in 13 slides. The narrative form of `why-cloudstack-ai-services.md`. |
| [Installing CloudStack AI Services](https://claude.ai/artifact/PLSZB6d3x26j2RxMAMDbye) | The install, in 14 slides, with the architecture flowchart. The clear steps, without the caveats `installation.md` carries. |
| [CloudStack AI Services End to End](https://claude.ai/artifact/8CQa9yPN8jDbYYDkKNGpdH) | How a request travels the whole system, and every feature in one page. |
| [Technical Specification](https://claude.ai/artifact/KaK3r9ryDdP4XEs4sc7VPQ) | A rendering of [SPEC.md](../SPEC.md). |

## Architecture decisions

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](adr/0001-serving-layer.md) | Plain Kubernetes `Deployment`s from Jinja — no KServe or vLLM production stack in v1 | Accepted |
| [0002](adr/0002-gateway.md) | LiteLLM Proxy, MIT side only; hooks import `aisvc.profiles` directly | Accepted |
| [0003](adr/0003-cloudstack-release.md) | Target 4.23, hold the compile surface to 4.22 LTS | Accepted |
| [0004](adr/0004-state-store.md) | The engine's own Postgres, not CloudStack's MariaDB — blast radius, not features | Accepted |
| [0005](adr/0005-model-artifact-storage.md) | Secondary storage cannot hold model artifacts; CloudStack object storage can supply the bucket | Partly accepted |
| [0006](adr/0006-gpu-node-join.md) | Hand GPU node VMs to CKS instead of `kubeadm join` | **Proposed** — gated on one experiment |
| [0007](adr/0007-kubernetes-cluster-source.md) | A zone declares where its cluster comes from; CKS is the paved road, not the only road | **Proposed** — schema and preflight built, creation built, join unchanged |
| [0008](adr/0008-api-deployments.md) | `placement: spec \| api` says who owns a model's placement; API placements live in Postgres | Accepted and built |
| [0009](adr/0009-engine-into-cloudstack.md) | How much of the engine should live inside CloudStack; the Extensions framework does not apply | **Proposed** — nothing built |

## Progress

[PROGRESS.md](../PROGRESS.md) is the unvarnished running log: what each
milestone actually built, what it found, and what is still not true.
It is long and deliberately so — it is the place where things that do
not work are written down.
