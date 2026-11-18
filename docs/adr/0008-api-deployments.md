# ADR-0008: How a deployment made through the API reaches desired state

**Status:** **Accepted and built, 2026-11-18.** `placement` is on the model,
migration 0010 holds placements, all three commands are served, and the planner
branches on both values. Verified against a live management server.
**Date:** 2026-11-18
**Deciders:** Simon Weller
**Resolves:** the three deployment commands, which with the nine profile ones made
up twelve of the thirteen recorded as `NOT_YET_SERVED` in
`tests/contract/test_engine_api_contract.py`; `approveAiModel` is the thirteenth
and waits on an open decision instead
**Depends on:** [ADR-0004](0004-state-store.md) (the engine's own Postgres),
and the rule in `CLAUDE.md` that `platform.yaml` is the source of truth for
desired state

## Context

`deployAiModel`, `updateAiModelDeployment` and `deleteAiModelDeployment` are
declared in SPEC section 9, generated into the plugin, wired to three actions in
the UI section, and answered by nothing. The engine serves `GET /v1/deployments`
and no other verb on that path.

The obvious reading is that they are missing endpoints. They are not. The
problem is that **it is not clear what they would write**, and the command
signatures are the evidence:

```
deployAiModel(name, zoneid, replicas?)
updateAiModelDeployment(id, replicas?, servingprofile?)
deleteAiModelDeployment(id)
```

`name` is "the catalog name of the model to deploy", so the model already exists
in `platform.yaml`. And a model there already determines everything these
commands appear to decide:

- **The zone is already fixed.** A `Model` declares a `node_pool`, and a
  `NodePool` declares its `zone`. A `zoneid` that disagrees with the model's
  pool contradicts the spec; one that agrees adds nothing.
- **The replica count is already a range the autoscaler moves within.** The
  spec declares `replicas: {min, max}`, and the live count is deliberately
  excluded from `config_hash` because "scaling is an in-place change" — folding
  it in made every autoscale look like a spec change and start a rollout.
- **Deploying is not a separate step.** `reconciler/deployer.py` iterates
  `spec.document.models` and renders a `Deployment` for each. Declaring a model
  *is* declaring a deployment; there is no catalogued-but-not-deployed state for
  a command to move a model out of.

So the three commands cannot mean "create a deployment from a catalogue entry",
because there is no such transition, and they cannot mean "add a model",
because they carry none of what a model needs — no `source`, `license`,
`max_model_len` or `node_pool`.

Meanwhile something has to persist the intent behind a UI action. An operator
edits `platform.yaml` by hand; a tenant clicking **Deploy** in the CloudStack UI
cannot, and the engine has no spec-writing machinery, no Git client, no
credentials and no guarantee that `AISVC_SPEC` even points inside a checkout.

## Decision

**Add one field to the spec that says who owns a model's placement, and store
placement — and only placement — in Postgres for the models that opt in.**

```yaml
models:
  - name: llama-3.3-70b-instruct
    node_pool: h100-pool
    replicas: {min: 1, max: 8}
    placement: api          # spec (default) | api
```

- `placement: spec` — today's behaviour, unchanged. The model runs wherever its
  node pool is, between its declared bounds, and the three commands refuse it
  with a message naming this field. GitOps installs never see a difference.
- `placement: api` — the model is catalogued but runs nowhere until a
  deployment row says otherwise. `deployAiModel` writes one; `delete` removes
  it; `update` changes its desired replica count or binds a serving profile.

A deployment row holds a model name, a zone, a desired replica count and an
optional serving profile. It holds nothing else: no source, no licence, no
engine arguments, no bounds. Those stay in the spec, and a row whose replica
count falls outside the model's declared `min`/`max` is refused at write time
rather than clamped later.

## Options considered

### Option A: write `platform.yaml` and commit it

| Dimension | Assessment |
| --- | --- |
| Complexity | High — YAML round-tripping, a Git client, credentials, push conflicts |
| Blast radius | A new failure domain on the write path of a tenant-facing API |
| Fidelity | Perfect: one file, one truth, full history |
| Team familiarity | New dependency and a new secret to manage |

This is what `CLAUDE.md` describes ("updated by Git commit when changes come in
through the UI or API"), and it is the honest ideal. It is also the one option
that cannot be built from where the engine stands: `AISVC_SPEC` is a path, which
may be a bare file on a host with no repository anywhere near it. Two engine
replicas would race on the same working tree, a failed push would leave desired
state diverged from what the reconciler just acted on, and a tenant's click
would fail for reasons about Git that mean nothing to them.

**Pros:** one source of truth; auditable; GitOps-native.
**Cons:** needs a repository, credentials and a merge strategy the engine has
no business owning; fails a tenant action for infrastructure reasons.

### Option B: a Postgres overlay with no spec involvement

| Dimension | Assessment |
| --- | --- |
| Complexity | Low — one table, no new dependency |
| Blast radius | Small; the engine already owns this database |
| Fidelity | Poor: two sources of desired state, silently |
| Team familiarity | High |

The quick answer, and the one `CLAUDE.md` warns against. Its real defect is not
duplication but **ambiguity**: with the spec still declaring the model and a row
also describing it, `deleteAiModelDeployment` has no defensible meaning. The
spec says deploy it; the row says do not. Whichever wins, an operator reading
the file is misled.

**Pros:** cheap, transactional, works with two replicas.
**Cons:** the file stops telling the truth, and deletion is undefined.

### Option C: retire the three commands

| Dimension | Assessment |
| --- | --- |
| Complexity | Lowest |
| Blast radius | None |
| Fidelity | Honest |
| Cost | Removes three UI actions and a published API surface |

Defensible, and worth stating plainly: declaring a model in the spec already
deploys it, so the commands are arguably redundant. But the UI section wires
all three, SPEC section 9 publishes them, and a platform whose only way to run a
model is an operator editing a file by hand is a weaker product than the one
this project set out to build.

**Pros:** removes a lie from the API surface.
**Cons:** gives up the capability rather than resolving it.

### Option D: spec-declared ownership, Postgres placement — **chosen**

| Dimension | Assessment |
| --- | --- |
| Complexity | Low — one spec field, one table, one branch in the deployer |
| Blast radius | Small; no new dependency, no new secret |
| Fidelity | Good: the file says who decides, so it never misleads |
| Team familiarity | High, and the pattern already exists here |

## Trade-off analysis

Option D is B with the ambiguity removed, and it removes it the way this
codebase already removes this kind of ambiguity. [ADR-0007](0007-kubernetes-cluster-source.md)
faced the same shape — who owns a zone's Kubernetes cluster — and answered it
with a `source` field in the spec plus a separate `create` permission, so that
`check_clusters` could tell "typo" from "not CloudStack's business". `placement`
is that field for deployments.

What it buys is that **`platform.yaml` never stops telling the truth.** A reader
sees either a model that runs per the file, or a model the file explicitly hands
to the API, and in the second case the file still owns everything about *what
the model is* — its source, its licence, its bounds, its engine arguments. The
database owns only *where it runs and how many*, and the second of those is
already runtime state that the autoscaler writes today.

What it costs is a mode. Two models in one file may be governed differently, and
an operator has to look at the field to know which. That is a real cost and it
is the price of the capability; the alternative that avoids it entirely is
Option C.

It is deliberately **not** a general escape hatch. `placement: api` does not
let the API change a model's licence, its pool or its bounds, so none of the
supply-chain or capacity rules move from the spec to a database where nobody
reviews them.

## Consequences

**Easier**

- A tenant or admin can deploy a catalogued model from the UI, which is what the
  three wired actions have always implied.
- `deleteAiModelDeployment` acquires a meaning: remove this placement. A model
  with none runs nowhere, and `listAiModelDeployments` shows that plainly.
- The four remaining `NOT_YET_SERVED` commands become one (`approveAiModel`).

**Harder**

- The reconciler gains a branch: for an `api` model, desired deployments come
  from rows rather than from the pool's zone. That branch has to be covered for
  both values or a model will silently run nowhere.
- Two sources now feed one reconcile, so `aisvc plan` has to print which one a
  deployment came from, or an operator reading a plan cannot tell why a model
  appeared.
- Deleting a `placement: api` model from the spec must also delete its rows, or
  they become orphans describing a model that no longer exists.

**To revisit**

- If spec-writing ever arrives — a real repository, a real commit path — Option
  A becomes available and this becomes a migration rather than a mistake: the
  rows are exactly the placements a commit would have written.
- The `replicas` parameter overlaps the autoscaler. The row holds a *desired*
  count within the declared bounds; the autoscaler still moves the live count.
  If that proves confusing in practice, the parameter should go rather than the
  autoscaler.

## Action items

1. [x] `placement: spec | api` is on `Model`, defaulting to `spec`. The
       bounds rule needed no new validation: `replicas` is already a required
       field, so an `api` model cannot omit them.
2. [x] Migration 0010, `aisvc_deployment_placement`, unique on (model, zone) —
       a second row would make the replica count depend on which the planner
       found first.
3. [x] `POST`, `PATCH` and `DELETE` on `/v1/deployments`, async, each refusing
       a `placement: spec` model by naming the field and a replica count
       outside the model's bounds by naming the bounds. Refused rather than
       clamped: a tenant given 8 after asking for 50 has been told nothing.
4. [x] The planner branches on `placement`, and both values are covered.
       An `api` model with no row is deliberately left out of `declared`, so
       the existing sweep tears its Deployment down — which is what
       `deleteAiModelDeployment` leaves behind.
5. [x] `aisvc plan` prints `placement: spec` or `placement: api` on every
       deployment action. The CLI refuses rather than assuming none when the
       catalogue has an `api` model and the database cannot be read, as
       `_stored_exposures` already did.
6. [x] The three names are out of `NOT_YET_SERVED`. Only `approveAiModel`
       remains.
7. [x] One list. `listAiModelDeployments` observes the cluster, so it shows
       what is running whoever placed it, which is the question an operator is
       asking. The placement rows are the desired state behind it, and the
       plan is where the two are compared.

## What building it turned up

**`deployAiModel` sent CloudStack's internal numeric zone id.** `zoneid` is a
`CommandType.UUID`, which CloudStack hands back as an internal `Long`, and this
hand-written command passed it straight through — the exact defect the generator
avoids with `resolve=`, recorded in `CLAUDE.md` and reintroduced here because
nothing generated this one. A live engine answered 422. The command resolves the
id through `_entityMgr` now and sends the zone's *name*, which is what
`platform.yaml` records as `cloudstack_zone`.

Its unit test had **pinned the bug in place**, asserting
`body.get("zone_id").getAsLong() == 4L` — the same shape as the `name=my+host`
signature test. It asserts the name now, and a second test covers a zone id
CloudStack cannot find.

`GeneratedSurfaceTest` could not have caught it either: it builds every command
and renders its body, but with no `_entityMgr`, so any command that resolves one
fails there with a `NullPointerException` rather than an answer. It supplies a
mocked one now, which also covers the v2 commands that resolve whenever
`ai.services.v2.enabled` is on.

## What this does not decide

- `approveAiModel`, which waits on SPEC section 14's open question about whether
  reseller domain admins mirror from Hugging Face directly or only request it.
- Whether the engine should ever write `platform.yaml`. This ADR routes around
  that question rather than answering it, and says so.
