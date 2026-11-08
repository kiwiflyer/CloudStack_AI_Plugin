# Why AI Services belongs in CloudStack

**The case, in one paragraph.** Every CloudStack operator already has
the three things an AI inference business needs: tenants with billing
relationships, a multi-tenant control plane with domains, accounts,
quotas and RBAC, and the ability to rack GPUs. What they do not have is
the twenty-seven thousand lines between a GPU host and an invoice-able
OpenAI-compatible endpoint. Today those operators watch their own
customers send workloads — and the data that comes with them — to a
hyperscaler, because that is the only place the endpoint exists. AI
Services closes that gap natively, so a provider can turn GPU capacity
into a metered, multi-tenant inference product using the tenancy,
billing and operational model they already run.

---

## What changed

Three things, at roughly the same time.

**Open-weight models got good enough to sell.** Llama, Qwen, Mistral
and Gemma now serve the majority of commercial workloads that used to
need a frontier API. A provider can host them. A provider cannot host
GPT-5.

**Inference overtook training as the thing people buy.** Training is
concentrated in a few dozen organisations. Inference is every
application, forever, and it is far more sensitive to latency,
egress cost and where the data physically goes — all of which favour a
provider close to the customer.

**Regulation turned "where" into a purchase requirement.** For
healthcare, government, education, finance and defence buyers, the
question stopped being "is it cheap" and became "whose law governs this
data". That question has an answer a regional provider can give and a
hyperscaler's regional region cannot.

## The distinction that creates the opening

Buyers and analysts increasingly separate two things that marketing
departments blur:

- **Data residency** is where the servers sit.
- **Data sovereignty** is who controls the infrastructure and under
  whose jurisdiction they operate.

A US-incorporated provider operating a datacentre in Frankfurt gives
you residency. Under the CLOUD Act it does not give you sovereignty.
Hyperscalers have responded with sovereign-branded variants, which is
itself the clearest signal that the demand is real — and the one thing
those variants cannot change is where the parent company is
incorporated.

**That is a moat a regional provider owns and cannot be out-spent on.**
It is also worthless without a product to sell through it. Most
CloudStack operators have the moat and no product.

> Figures circulating on this market — inference as a growing share of
> AI infrastructure spend, European sovereign cloud spend growing
> sharply into 2026 — come largely from vendors selling into it, quoting
> analysts secondhand. The structural argument above does not depend on
> any of them, and this document deliberately does not lean on numbers
> it has not verified.

---

## Why *in* CloudStack, and not beside it

An operator could bolt a separate inference stack onto their cloud.
Several do. Here is what that costs them, and what being native avoids.

| Bolted on | Native in CloudStack |
| --- | --- |
| A second tenancy model to map, reconcile and keep in sync | Domains and accounts **are** the tenants. One identity, one hierarchy, one set of RBAC rules. |
| A second billing pipeline, reconciled monthly by hand | Usage records flow into the **existing** CloudStack billing integration — tokens, GPU-hours, adapter premiums, batch. |
| A second portal, with its own login and its own support burden | One UI section alongside Compute and Storage. Tenants do not learn a new product. |
| A second RBAC system to audit, and a second place to get it wrong | CloudStack's roles, inherited. Every command is permission-checked by the platform the operator already trusts. |
| GPU capacity managed outside the scheduler, drifting from reality | CloudStack remains the IaaS authority. Nothing bypasses it for scheduling. |
| Automation that stops at the boundary | Everything in the UI also works through CloudMonkey and Terraform, because the UI only calls the same API commands. |

The pattern matters beyond convenience. **An operator's hardest problem
is not serving a model — vLLM does that — it is selling one**: metering
it, limiting it, invoicing it, isolating one tenant from another and
proving that isolation to an auditor. CloudStack already solves those
problems for compute, storage and networking. Doing it natively means
inheriting the solution rather than rebuilding it, less well, alongside.

## What an operator can actually sell

Not a roadmap — these are the product lines the design supports.

**Metered inference endpoints.** OpenAI-compatible, so a customer
changes a base URL and a key and their existing application works.
Priced per token against a catalog price tier, with GPU-hours metered
alongside.

**Tiers that mean something.** Shared pools for price-sensitive
customers; dedicated node pools and a tenant's own VPC tier for those
who will pay not to share. The same catalog, two margins.

**Private endpoints as a premium product.** A deployment can be
attached to the tenant's own VPC tier and the public endpoint turned
off entirely, with a custom domain and the tenant's own TLS
certificate. For a regulated buyer this is not a feature, it is the
procurement requirement — and it is the thing a public API endpoint
structurally cannot offer.

**Customer-specific models without training anything.** Tenants upload
LoRA adapters and get `<base-model>:<their-adapter>` on shared base
capacity, metered at the base price plus a premium. The operator sells
differentiation; the GPUs stay pooled.

**Batch at a discount.** Spare capacity sold at a lower rate, preempted
by online traffic. Revenue from the troughs, without touching the peaks.

**Guardrails as a sellable control, not a cost.** Education-safe
presets for school districts, PII redaction for health and finance.
Profiles apply at platform, domain or account scope with lock
semantics, so a provider can impose a floor a tenant can tighten but
never loosen.

**Embeddings and rerank on cheap silicon.** Small models pack onto L40S
or MIG classes, which turns hardware that is uneconomic for chat into
margin.

## What it means for you

**If you run a public cloud on CloudStack**, this is a new product line
on hardware you can finance against existing demand, sold to customers
you already bill, through a portal they already use. The tenancy,
quota and invoicing work — usually the majority of the effort — is
inherited rather than built.

**If you are an MSP or systems integrator**, it is a managed-AI
offering you can stand up for a client in their own tenancy, with the
compliance story written down rather than improvised: no egress from
inference pods, prompts never logged by default, artifacts verified by
checksum, licences enforced at both mirror and deploy time.

**If you are a telco or sovereign provider**, it is the inference layer
for the sovereignty argument you are already making — and the private
endpoint is the proof. The data path can be made to never leave the
customer's network.

**If you run a large private cloud**, it is internal AI platform-as-a-
service with departmental chargeback, budgets and guardrails, instead
of every team expensing their own frontier-API account and nobody
knowing what is being sent where.

**If you are a CloudStack contributor**, it is a reason for an
operator to stay on, or move to, the platform. Infrastructure projects
are chosen for their next five years. AI capability is currently a
reason to choose something else.

## Why this is defensible work

The easy 20% of this is deploying vLLM. The parts that take the time —
and the parts a competitor has to redo — are:

- **Capacity planning that refuses impossible things up front.** The
  engine sizes a model from its architecture and the GPU class —
  weights, KV cache and headroom — and fails *validation* with the
  shortfall in GiB, before anything is provisioned at all, rather than
  after a pod has spent twenty minutes pulling weights to OOM.
- **A supply chain you can defend in an audit.** Models pinned to a
  commit SHA, artifacts verified by sha256, pickle weights and
  `trust_remote_code` blocked by default, licences enforced against an
  allow-list at mirror time and again at deploy time.
- **Tuning that cannot be argued with.** Generation and guardrail
  parameters resolve through platform → model → domain → account →
  deployment → key → request, where a `locked` value beats every level
  below it, and the API will tell a tenant both the resolved value and
  which level set it.
- **Convergence, not scripts.** A declarative spec, a reconcile loop
  and a plan diff, where a second apply does nothing and a dry run
  writes nothing.
- **Operational honesty about GPUs.** Passthrough GPUs cannot
  live-migrate, so the system drains and replaces nodes — and drains by
  destroying, because a stopped node keeps its GPU allocated.

None of that is glamorous and all of it is the difference between a
demo and a product.

## Where this is today

Stated plainly, because a positioning document that oversells is the
expensive kind of marketing.

The platform is **built and tested but not yet proven in production**.
All nine milestones are implemented, each with gaps recorded rather
than glossed: the engine, the native API plugin, the UI section,
Terraform support, metering, budgets, autoscaling, and the Tier 2
differentiators. The test suites pass — engine, plugin, UI and
provider.

What has not happened: **no CloudStack management server has loaded the
plugin, and no proxy VM has booted.** Every test that needs a live
management server, a CKS cluster or a GPU host is skipped. The next
milestone is a lab deployment that exercises the self-test end to end.

Read [PROGRESS.md](../PROGRESS.md) for the unvarnished version and
[docs/installation.md](installation.md) for what standing it up
involves.

## What would move this forward

- **A lab zone with real GPU capacity** to run the acceptance criteria
  the test suite already describes.
- **An operator willing to be first**, with a real tenant and a real
  invoice at the end of it.
- **Upstream engagement** — the resources are written to land in the
  CloudStack Terraform provider, and the UI section follows CloudStack's
  own patterns precisely so it can be contributed rather than forked.
  The [CloudStack Collaboration Conference](https://cloudstack.apache.org/)
  is the natural venue for that conversation.
- **Answers to the open questions** in [SPEC.md](../SPEC.md): the
  initial catalog, the licence allow-list, the price tiers and the
  pricing unit. Those are commercial decisions, not engineering ones.

---

### Sources

- [Apache CloudStack](https://cloudstack.apache.org/) and its
  [users page](https://cloudstack.apache.org/users), for the service
  provider, MSP and telco audience the project serves.
- [EU sovereign AI and the CSP/telco GPU cloud](https://www.hosted.ai/blogs/sovereign-ai-2026-eu-csp-telco-gpu-cloud)
  and [EU-sovereign inference platform comparison](https://lyceum.technology/magazine/eu-sovereign-inference-platform-comparison/),
  for the residency-versus-sovereignty distinction. Both are vendor
  sources with an interest in the conclusion; the structural argument
  here does not rest on their figures.
- [Data residency for AI workloads](https://www.gmicloud.ai/en/blog/data-residency-for-ai-workloads-what-enterprises-need-to-know-before-choosing-a-cloud-provider),
  on the jurisdiction question for regulated buyers.
