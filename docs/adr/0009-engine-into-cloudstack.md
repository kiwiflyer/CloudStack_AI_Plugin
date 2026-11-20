# ADR-0009: How much of the engine should live inside CloudStack

**Status:** **Proposed, nothing built.** This records the options and a lean; it
settles nothing. The choice touches two accepted ADRs, so it should be made
deliberately rather than as a side effect of a refactor.
**Date:** 2026-11-20
**Deciders:** Simon Weller
**Relates to:** the open question in `CLAUDE.md` about whether to upstream a
generic `ai-services` plugin, and to apache/cloudstack#14733, which upstreams
the plugin and the UI section while leaving the engine out
**Constrained by:** [ADR-0002](0002-gateway.md) (the gateway hooks import
`aisvc.profiles` directly and must never reimplement precedence "in any
language") and [ADR-0004](0004-state-store.md) (the engine's state lives in its
own Postgres, for blast radius)

## Context

The plugin is upstreamable and is upstream. The engine is not: it is 80 Python
files, about 15,700 non-comment lines, and fifteen runtime dependencies —
FastAPI, uvicorn, SQLAlchemy, Alembic, pg8000, boto3, `huggingface-hub`, the
Kubernetes client, `cryptography`. An operator installing the plugin today gets
an API that refuses every call until a second service exists, is configured, and
presents a client certificate.

That is the honest objection a reviewer will raise, and "run this other thing"
is a weak answer for something proposed as a native CloudStack feature.

## The Extensions framework does not apply

Worth stating, because it is the first thing anyone suggests. CloudStack 4.23's
Extensions framework has two types, `Orchestrator` and `NetworkOrchestrator`,
and it is the wrong direction and the wrong shape for this engine.

- **Direction.** An `Orchestrator` extension registered against a Cluster
  delegates CloudStack's *own* VM lifecycle outward — prepare, deploy, start,
  stop, reboot, status, delete. The engine does the opposite: it *consumes*
  CloudStack as the IaaS authority and asks it to deploy VMs. Nothing in this
  project wants to take orchestration away from CloudStack; `CLAUDE.md` says so
  as a rule.
- **Shape.** Invocation is `/path/to/executable <action> <payload_file>
  <timeout_seconds>`, a short-lived process per action, answering with JSON on
  stdout. The reconciler is a long-running loop that holds a leader lock and a
  database connection for a whole run, and whose unit of work is a zone, not a
  VM.

A `NetworkOrchestrator` extension is further still — guest network and VPC
services. The framework is a good fit for someone whose hypervisor or SDN is
external. It is not a transport for a control plane that sits on top of
CloudStack.

## The constraint that shapes every option

`gateway-hooks/enforcement.py` imports five engine packages **in process**, and
LiteLLM loads it on every inference request:

```python
from aisvc.adapters import ADAPTER_SEPARATOR, may_use
from aisvc.gateway import KeyRejected, RequestContext, enforce_request
from aisvc.guardrails import check_input
from aisvc.metering import UsageRecord
from aisvc.profiles import ParameterRejected
```

ADR-0002 forbids reimplementing profile precedence in any language, so those
five cannot simply be ported and copied back. They would have to be reached over
HTTP — on the data path, on every request, adding latency and a failure mode to
every token served.

Measured, the constraint is smaller than it sounds:

| Part | Lines | Movable to Java |
| --- | --- | --- |
| Loaded by the gateway per request: `adapters`, `gateway`, `guardrails`, `metering`, `profiles` | 1,275 (8%) | No, not without reopening ADR-0002 |
| Everything else: `reconciler`, `cloudstack`, `k8s`, `capacity`, `provision`, `spec`, `db`, `api`, `cli`, `hf`, `registry`, `setup`, … | 14,394 (92%) | Yes |

Eight per cent is a library. Ninety-two per cent is the part that makes the
engine a service.

## Options considered

### Option A: port the whole engine to Java, inside the plugin

| Dimension | Assessment |
| --- | --- |
| Complexity | High — ~15,700 lines, and every dependency needs a Java equivalent |
| Blast radius | Large: state moves onto CloudStack's MariaDB |
| Fidelity | Perfect as "native": one service, no mTLS, no second database |
| Team familiarity | The plugin is already Java; the logic is not |

Genuinely native. FastAPI and uvicorn disappear entirely, because the plugin
*is* the API. Every other dependency has an ASF-compatible Java counterpart.

But it breaks the gateway coupling above, and it collides with ADR-0004 head on:
the gateway writes a usage row per request and the reconciler holds a connection
for a whole run, which is exactly the load that ADR kept off CloudStack's
database. Choosing A means reopening both ADRs on purpose.

**Pros:** one process, one install, one database; nothing to configure.
**Cons:** the largest port; puts per-request write load on CloudStack's MariaDB;
either moves enforcement onto an HTTP hop in the request path or duplicates the
precedence logic that ADR-0002 forbids duplicating.

### Option B: port the control plane, keep the gateway library — **lean**

| Dimension | Assessment |
| --- | --- |
| Complexity | High but divisible — package by package, behind the REST contract |
| Blast radius | Chosen per package; the database question can be deferred |
| Fidelity | Good: nothing an operator installs separately except the gateway's own hooks |
| Team familiarity | High |

The 92% becomes Java in the plugin. The five gateway-coupled packages stay
Python and ship with the hooks as a small library — about 1,275 lines, which is
an artefact rather than a service.

The useful property is that it is **incremental**. Each control-plane package is
independently movable behind the existing engine REST contract, so this can
start without a flag day and stop anywhere that turns out to be far enough.

**Pros:** keeps both ADRs intact; no second service to install; can be staged.
**Cons:** two languages in the product for as long as it takes; the engine's
Postgres question still has to be answered for whatever moves.

### Option C: ship the engine as a CloudStack system VM

| Dimension | Assessment |
| --- | --- |
| Complexity | Low in code, real in build and release |
| Blast radius | Small; the appliance is isolated by construction |
| Fidelity | Native operationally, not architecturally |
| Team familiarity | Medium — systemvm templates are their own discipline |

CloudStack already builds and lifecycles system VMs: the SSVM, the console proxy,
the virtual router. The engine becomes one, and CloudStack owns its provisioning,
upgrade and health.

**Pros:** no port at all; a well-trodden CloudStack pattern; the engine keeps its
own database by construction.
**Cons:** a VM per zone to pay for and template to build and ship; upgrades tied
to systemvm template releases.

### Option D: package it beside the management server

| Dimension | Assessment |
| --- | --- |
| Complexity | Lowest |
| Blast radius | Small |
| Fidelity | Native in install and upgrade terms only |
| Team familiarity | High |

A `cloudstack-ai-engine` RPM/DEB with a systemd unit, installed on the management
server, mTLS over loopback. CloudStack already ships Python — Marvin, the
systemvm agent — so the precedent exists.

**Pros:** cheapest by a wide margin; one install and one upgrade path; removes
the "run this other thing" objection without touching the architecture.
**Cons:** still two processes; says nothing about where the engine *should*
live; a reviewer may read it as packaging around the problem.

## Trade-off analysis

A is the purist answer and the one most likely to be asked for by anyone who
reads "native" literally. It is also the only option that requires reopening two
accepted ADRs, and the reasons those ADRs give have not changed: per-request
usage writes are a poor fit for CloudStack's database, and enforcement in the
request path is a poor place for a network hop.

C and D are not really alternatives to A and B — they answer a different
question, which is how an operator *obtains* the engine, not where its code
lives. That makes them stageable in front of either.

B is the only option that moves the code and leaves both ADRs standing, and the
8/92 split is what makes it practical. It is a lean rather than a decision
because its cost — two languages in the product, for a while — is real and is
the sort of thing a maintainer should accept explicitly rather than inherit.

## Consequences

**Easier**

- Under B or D, an operator stops needing to stand up a service by hand before
  the plugin answers anything.
- Under B, each ported package stops needing mTLS, a caller-context header and a
  JSON round trip to do its work.

**Harder**

- Under B, the project carries Java and Python for as long as the port runs, and
  `tests/unit/test_commands.py`-style guards grow a third side to compare.
- Under A or B, `aisvc/db/migrate.py` and the Alembic chain need an answer: the
  plugin cannot use CloudStack's schema upgrade chain today, which is why
  `AiServicesSchema` creates its own table at startup.
- Under C, the engine's release cadence becomes the systemvm template's.

**To revisit**

- ADR-0004 if any state moves to CloudStack's database.
- ADR-0002 if enforcement ever moves off the gateway process.
- Whether `approveAiModel` and the mirror-request table change shape once the
  registry is a CloudStack-native concern.

## Action items

1. [ ] Decide between A and B explicitly, as a decision about ADR-0002 and
       ADR-0004 rather than about Java and Python.
2. [ ] Independently of 1, pick C or D so the engine is obtainable. D is the
       cheapest and does not foreclose anything.
3. [ ] If B: order the packages by how little they touch the gateway —
       `capacity`, `spec` and `k8s` rendering first, `reconciler` last — and keep
       the REST contract as the seam throughout.
4. [ ] Record the per-package Postgres answer before the first package moves,
       not after.

## What this does not decide

- Whether the plugin should be upstreamed in its current shape at all. That is
  the older open question, and it is logically prior to this one: if the answer
  is a smaller generic surface upstream, most of this ADR is moot.
- Where the LiteLLM gateway itself runs, which ADR-0002 left open.
