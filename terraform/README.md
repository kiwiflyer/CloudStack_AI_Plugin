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

Every resource supports `terraform import` by its id. A budget's id is
the account it belongs to.

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
