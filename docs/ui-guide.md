# The AI Services UI section

What a tenant and an operator actually see, screen by screen.

This describes `ui/src/config/section/aiservices.js` and the eleven Vue
views beside it. For how to install the section into a CloudStack `ui/`
tree, see [installation.md](installation.md); for why it is built the
way it is, [ui/README.md](../ui/README.md).

## The one rule that explains the layout

**Nothing here is configured on or off.** Each screen declares the
command it needs:

```js
{ name: 'aiplayground', permission: ['createAiPlaygroundSession'], … }
```

CloudStack drops any route whose `permission` is missing from the
account's own `listApis` answer, and then drops a section with no
children. So the screens a person sees follow the commands the plugin
registered for their role — and the whole Tier 2 group appears and
disappears with `ai.services.v2.enabled` alone, with nothing in the UI
to switch.

That also means the UI can never offer something the API will refuse:
the same role check decides both.

## What each role sees

Nineteen screens. Eleven are visible to anyone with a role, and the
rest are gated:

| Screens | Who |
| --- | --- |
| Model catalog, Endpoints, Playground, Generation profiles, Effective parameters, Usage, Adapters, Batches, Certificates, Private endpoints, Guardrails | anyone, filtered to what they own |
| Hugging Face, Registry, Rollouts, Activity | admin or domain admin |
| Setup, GPU capacity, Fleet health, Serving profiles | root admin |

A tenant is not shown an empty operator screen; the route is not there
at all. Typing its URL gives a 404 rather than a refusal, which is
CloudStack's own behaviour for a route it did not register.

## The tenant's screens

### Model catalog

What this provider sells: the models, what each is for, its licence and
its price tier.

![The model catalog](images/ui/model-catalog.jpg)

The catalogue is the spec's `models:` list filtered by visibility, so a
reseller's tenants see their reseller's selection. Licence is shown
because several open-weight licences carry use restrictions, and a
tenant choosing a model is choosing its terms.

### Endpoints

The OpenAI-compatible base URL per model, with an **API keys** tab and a
**Snippets** tab on each.

![The endpoints list](images/ui/endpoints.jpg)

Keys are scoped: a key may name the models it may use, the CIDRs it may
be called from, and when it expires. The secret is shown once, on
creation, because keys are stored as salted hashes.

The snippets tab carries copy-ready calls — curl, plain Python, and the
OpenAI clients for Python and JavaScript — rendered by the engine and
delivered with the endpoint. They are rendered there rather than in the
browser so there is one implementation of "how you call this", and they
read the key from the environment rather than carrying one: a snippet
gets copied into tickets and chats, where a live key would outlive any
session.

### Playground

A fifteen-minute, model-scoped session for trying an endpoint without
putting a real key in a browser.

![Opening a playground session](images/ui/playground-open-session.jpg)

Pick a model and open a session. Nothing is minted until you ask: a
session key is a credential, and issuing one because somebody opened a
page starts its expiry running against a person who is only reading.

![A session, with its expiry](images/ui/playground-session-open.jpg)

The session shows what it is for and when it ends. **The key itself is
never rendered** — it is short-lived, but this screen gets demonstrated
and screenshotted, and a credential on screen is a credential in
somebody's slide deck.

### Effective parameters

Every generation parameter's resolved value *and the level that set it*.

A resolved value alone tells a tenant nothing; the useful question is
always "why is it that, and may I change it". So each row carries its
source — platform, model default, domain, account, deployment, key or
the request — and marks anything locked above them.

This reads through the same `aisvc.profiles.resolve()` the gateway
enforces with, so the screen cannot disagree with what a request
actually does.

### Generation profiles, usage and budgets

Generation profiles are a tenant's own tuning, bound at a scope they
are allowed to write. Usage shows tokens and GPU-hours with a budgets
tab beside it; a tenant may read their budget and not set it, since an
account able to raise its own ceiling has no ceiling.

### Tier 2: adapters, batches, certificates, private endpoints, guardrails

Present only when `ai.services.v2.enabled` is on. LoRA adapters a
tenant brings; batch jobs; TLS certificates for a custom domain; a
private endpoint on their own network; and guardrail profiles, which a
tenant may tighten for themselves and never loosen below what is locked
above them.

## The operator's screens

### Setup

A preflight wizard: what CloudStack must have before anything can be
deployed, each check passing, failing or skipped with the remedy named.

### GPU capacity and fleet health

A heatmap of GPU capacity by zone and class, and the health of what is
serving. Both read observed state — what CloudStack and Kubernetes
report — rather than anything the engine stores, so they cannot drift.

### Hugging Face

Search the Hub and see, before mirroring, whether a model can be
deployed here at all: its licence against the allow-list, whether it
carries pickle weights, whether it needs `trust_remote_code`.

It does not search until asked. Searching on load would hit the Hub for
every operator who opened the screen.

### Registry, rollouts and activity

What has been mirrored, with an evaluations tab showing the gate result
rather than a bare score; blue/green rollouts with their traffic
weights, promote and rollback; and the reconciler's runs with the
actions each took.

## Two things worth knowing

**Every screen is also CloudMonkey and Terraform.** The section's
config names API commands and nothing else, so anything doable here is
doable from a script — SPEC section 9 requires it, and the config shape
is what keeps it true.

**Most screens are config, not code.** Eight of nineteen are declared
entirely in `aiservices.js`, and CloudStack builds the list, the detail
view, search, pagination, async job notifications and role-based action
visibility from that. Eleven are Vue components, because a table cannot
express a wizard, a heatmap, a chat transcript, a resolved-parameter
table or a Hub search.

## When something goes wrong

Every screen tells you which of three things happened, because they
used to look identical — blank:

- **Could not load**, with the reason the engine gave. `listAiGpuCapacity`
  answers 534 until capacity has been observed, and that sentence names
  the remedy, so it is shown rather than swallowed.
- **Nothing to show**, which is a different thing and says so.
- The data.

None of them leaves an unhandled promise rejection. They used to: nine
of the eleven views called the API with no `catch`, so a refusal put the
reason in the browser console, where nobody is looking, and left the
page blank.
