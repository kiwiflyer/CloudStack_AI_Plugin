# Terraform support (SPEC.md section 7)

The AI Services resources for the CloudStack Terraform provider.

These are meant to land in `terraform-provider-cloudstack` upstream.
They live here so they can be written and tested alongside the API
commands they call, which are generated from `aisvc/api/commands.py` and
change with the engine.

## Resources

| Resource | Commands |
| --- | --- |
| `cloudstack_ai_deployment` | `deployAiModel`, `listAiModelDeployments`, `updateAiModelDeployment`, `deleteAiModelDeployment` |
| `cloudstack_ai_api_key` | `createAiApiKey`, `listAiApiKeys`, `revokeAiApiKey` |
| `cloudstack_ai_generation_profile` | `createAiGenerationProfile`, … |
| `cloudstack_ai_serving_profile` | `createAiServingProfile`, … (admin) |
| `cloudstack_ai_budget` | `createAiBudget`, `listAiBudgets`, `updateAiBudget`, `deleteAiBudget` |
| `cloudstack_ai_models` (data source) | `listAiModels` |

### Tier 2 (v2)

| Resource | Commands |
| --- | --- |
| `cloudstack_ai_adapter` | `uploadAiAdapter`, `listAiAdapters`, `deleteAiAdapter` |
| `cloudstack_ai_batch` | `createAiBatch`, `listAiBatches`, `cancelAiBatch` |
| `cloudstack_ai_certificate` | `uploadAiCertificate`, `listAiCertificates`, `replaceAiCertificate`, `deleteAiCertificate` |
| `cloudstack_ai_private_endpoint` | `createAiPrivateEndpoint`, `listAiPrivateEndpoints`, `deleteAiPrivateEndpoint` |
| `cloudstack_ai_guardrail_profile` | `createAiGuardrailProfile`, `listAiGuardrailProfiles`, `updateAiGuardrailProfile`, `deleteAiGuardrailProfile` |
| `cloudstack_ai_evaluations` (data source) | `listAiEvaluations` |

**These need `ai.services.v2.enabled` set on the management server.**
The plugin registers the Tier 2 commands only when it is on, so without
it every one of these fails at apply with CloudStack reporting an
unknown command.

The provider declares them either way. Terraform cannot ask CloudStack
what is registered before a plan, and a resource that came and went
would be a schema error rather than a message anyone can act on. So the
failure happens at apply and says which setting to set.

Their acceptance tests are gated on `CLOUDSTACK_AI_V2=1` as well as
`TF_ACC`, so a v1 zone skips them rather than failing for a reason that
is not a bug.

Every resource supports `terraform import` by its id. A budget's id is
the account it belongs to.

## What the Tier 2 resources are, and are not

- **A batch is work, not desired state.** `terraform destroy` cancels
  what has not run; requests already completed stay billed and their
  results stay available. Nothing about a submitted batch can be
  changed, so every argument is `ForceNew`.
- **An adapter is immutable.** There is no `updateAiAdapter`: its rank
  has to match what vLLM was started with, so a change is another
  upload. Read back `served_model_name` — `<base>:<adapter>` — because
  the adapter's own name addresses nothing.
- **A certificate renews in place.** New material calls
  `replaceAiCertificate` rather than making a new resource, so every
  private endpoint naming that id keeps naming it. The proxy is rebuilt
  to pick the material up, which interrupts traffic: a proxy reads its
  certificate once, at boot.
- **A private endpoint cannot be edited.** There is no update command,
  because the proxy VM is built from these values at boot. It is a proxy
  and not a Kubernetes `LoadBalancer` Service because the CloudStack
  cloud-controller takes a Service's network from the nodes' first NIC,
  so a Service cannot be placed on a chosen tier.
- **A guardrail profile can be retargeted, not rescoped.**
  `updateAiGuardrailProfile` takes the target and nothing else — and
  the new target is checked for authority too, or the scope rule would
  be a formality: create it for yourself, then move it.
- **Guardrails are scoped to the caller, not to a role.** A tenant may
  write at the account scope with their own account as the target; a
  domain admin inside their own domain; the platform and deployment
  scopes are a root admin's. An apply that reaches above the caller is
  refused with a message naming what they may set instead. A check
  another scope has `locked` is refused outright rather than stored and
  then ignored when requests are screened.

### The certificate body and key

The same write-once problem as the API key secret, for the same reason:
`listAiCertificates` returns metadata only — never the certificate body,
never the key. So `Read` touches neither, an imported certificate has
both empty, and the acceptance test ignores them on import. Both are
marked `Sensitive`; the body is not secret, but it travels with the key
and showing one while hiding the other invites pasting them the wrong
way round.

**The private key is in Terraform state**, with everything that implies
for how state is stored.

## The API key secret

`createAiApiKey` returns the secret once and never again. So:

- `api_key` is `Computed` and `Sensitive`, and is written only at
  creation.
- `Read` deliberately does not touch it. Reading it back would write an
  empty string over the real one on the next refresh, and nothing could
  recover it.
- An **imported** key therefore has an empty `api_key`. That is correct,
  not a bug: CloudStack cannot tell us the secret of a key it issued
  earlier. The acceptance test's `ImportStateVerifyIgnore` records this.
- Every scope is `ForceNew`: scopes cannot change without a new secret,
  which is also how SPEC.md section 7's "rotation by overlapping keys"
  works — add the new key, move traffic, then remove the old one.

**The secret is in Terraform state.** State should be stored encrypted
and treated as a credential, as with any provider that issues one.

## Why not the CloudStack Go SDK

`cloudstack-go` is generated from upstream's command set and knows
nothing of the AI commands. Request signing is a dozen lines, so
`internal/provider/client.go` does it rather than carrying a fork.

One detail is pinned by a test because it has bitten this project
before: CloudStack lowercases the **whole** query string before signing,
percent-escapes included, so `%2C` must be `%2c`. A signature over the
uppercase form is rejected with a bare 401.

## Tests

```
make test-tf
```

runs the unit tests: signing, job polling, error propagation, schema
validation, and the read-back behaviour above. They need no CloudStack.

The acceptance tests create and destroy real objects and are skipped
unless `TF_ACC` is set, as is Terraform's convention:

```
TF_ACC=1 CLOUDSTACK_API_URL=http://localhost:8080/client/api \
  CLOUDSTACK_API_KEY=... CLOUDSTACK_SECRET_KEY=... go test ./... -v
```

Point them at the simulator from `make test-int`. **They have not been
run yet** — the simulator is not part of `make check`, and nothing here
has been exercised against a live management server.
