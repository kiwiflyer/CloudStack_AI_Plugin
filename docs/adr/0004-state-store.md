# ADR-0004: State store

**Status:** Accepted
**Date:** 2026-11-08
**Deciders:** Simon Weller
**Resolves:** a choice SPEC.md assumed and never argued — "State lives in Postgres"
(section 4) was in the first draft, and the alternative was never written down
**Constrained by:** [ADR-0002](0002-gateway.md) — LiteLLM's own keys, budgets and spend
tracking are backed by Postgres and Redis
**Affects:** `aisvc/db/`, `aisvc/reconciler/leader.py`, `docs/installation.md`

## Context

The engine keeps runs, actions, usage, budgets, profiles, hashed API keys, mirror
requests, adapters, batches, evaluations, certificates and private endpoints in a
relational database. SPEC.md section 4 says that database is Postgres, and every
milestone since has been built on that sentence.

**It was an assumption, not a decision.** This ADR exists because it was questioned
and nothing in the repository could answer. MariaDB is not mentioned anywhere in the
spec, the ADRs or the code, and a CloudStack operator already runs one — in HA, with
backups and a runbook — for the management server. Standing up a second database is
a real cost that was never weighed against the alternative.

The question is not "Postgres or MariaDB" as engines. It is **whether the engine's
state belongs in the CloudStack management database or in one of its own.**

## Decision

**Keep the engine's state in a separate database, and keep Postgres as the default.**

The reason is lifecycle independence and blast radius, not any property of Postgres
that MariaDB lacks. Supporting MariaDB as an *optional* backend stays open, and is
cheaper than it looks.

## How Postgres-specific the code actually is

Less than the decision implies, which is worth knowing before anyone treats this as
irreversible.

| Thing | State | Cost to port |
| --- | --- | --- |
| Advisory lock (`aisvc/reconciler/leader.py`) | `pg_try_advisory_lock` / `pg_advisory_unlock` as raw SQL | The only hard dependency. MariaDB's `GET_LOCK(name, 0)` / `RELEASE_LOCK(name)` is connection-scoped the same way, which is the property the module's docstring already relies on. A dialect branch, roughly ten lines. |
| JSON columns | `JSON().with_variant(JSONB(), "postgresql")` | None. Already portable by construction — the test suite runs the whole schema on SQLite. |
| Driver licence | `pg8000` (BSD-3-Clause), chosen because psycopg is LGPL and therefore Category X | Easier, not harder. PyMySQL is MIT; verify against `docs/third-party-licenses.md` before relying on it. |
| Migrations | Alembic, dialect-neutral | Low. Column types would need checking, not rewriting. |
| Queries | SQLAlchemy ORM throughout; no raw SQL outside the lock | None found. |

So the lock is the decision's only technical anchor. Everything else is habit.

## Options considered

### Option A: a separate Postgres (chosen)

| Dimension | Assessment |
| --- | --- |
| Complexity | Higher — a second database to install, back up, replicate and monitor |
| Operational cost to the operator | Real, and newly visible in `docs/installation.md` |
| Blast radius | Contained to AI Services |
| Upgrade coupling | None with CloudStack |
| Team familiarity | High |

### Option B: CloudStack's MariaDB

| Dimension | Assessment |
| --- | --- |
| Complexity | Lower — nothing new to install |
| Operational cost to the operator | Near zero; they already run it in HA |
| Blast radius | The whole cloud |
| Upgrade coupling | Shares a schema namespace with CloudStack's own migrations |
| Team familiarity | High for the operator, lower for us |

**Option B is genuinely attractive**, and the honest summary is that it wins on every
dimension except one. That one decides it.

## Why blast radius decides it

Three specifics, in order of how much they worry me.

**Usage is written once per request.** `gateway-hooks/enforcement.py` writes an
`aisvc_usage` row from the gateway's success hook, per completion. A busy endpoint
therefore puts sustained write volume and unbounded row growth onto whatever database
it lands in. On the management database, a filled disk is not a degraded AI feature —
it is every hypervisor operation in the zone failing at once.

**The reconciler holds a connection for the length of a run.** The advisory lock
belongs to a connection, not a transaction, so one connection is checked out and held
until the run finishes — minutes, when a run is provisioning node VMs. That is a fine
thing to do to our own pool and an uncomfortable thing to do to CloudStack's.

**Alembic would be managing tables inside a schema CloudStack's upgrade tooling
inspects.** Thirteen tables CloudStack does not know about are generally tolerated,
but they make every CloudStack upgrade a slightly riskier conversation, and they make
a support question about a broken management database harder to answer.

There is also a design argument, weaker but consistent with the rest of the project.
CLAUDE.md draws the division of responsibility at the API: the plugin is the only
caller of the engine, over mTLS, and it owns exactly one table so CloudStack can
enforce ownership natively. Sharing a database blurs the boundary that the mTLS
boundary exists to draw.

## Consequences

**What this costs.** An operator installing AI Services must provide Postgres, in HA,
with backups — on top of the MariaDB they already run. `docs/installation.md` lists it
as a prerequisite, and `install.py` can check it is reachable but cannot create it.
This is the single largest piece of infrastructure the product asks for that CloudStack
did not already require, and it should be stated plainly in any pre-sales conversation
rather than discovered during an install.

**What it buys.** AI Services can fill a disk, hold a lock, run a bad migration or grow
a table without any of it reaching the management database. The engine can be upgraded,
restored or moved on its own schedule.

**What stays open.** MariaDB as an optional backend. The work is the advisory lock, a
driver, and a CI matrix — not an architectural change. Worth doing if an operator
declines the product over the second database, which is a plausible objection and the
signal to revisit this.

**What does not change.** Postgres stays the default and the only tested backend.
Nothing in the code should start branching on dialect speculatively.

## Action items

1. [ ] Say in `docs/installation.md` why Postgres is separate, so an operator reading
   the prerequisite list gets the reason with the requirement.
2. [ ] If MariaDB support is ever taken up: put the lock behind a dialect branch, add
   PyMySQL after a licence check, and run the integration suite against both.
3. [ ] Revisit if an operator declines over the second database, or if usage row growth
   turns out to need partitioning — which would argue for separation more strongly, not
   less.
