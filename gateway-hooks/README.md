# gateway-hooks

For the whole install, see [docs/installation.md](../docs/installation.md).

LiteLLM custom handlers (SPEC.md section 13, [ADR-0002](../docs/adr/0002-gateway.md)).

This is **not a Python package** — the directory name has a hyphen, and the modules are
mounted into the LiteLLM proxy container and referenced by path in its config:

```yaml
litellm_settings:
  callbacks: ["enforcement.AisvcEnforcement"]
```

The adapters here are deliberately thin. Every rule lives in `aisvc.gateway` and
`aisvc.profiles`, which the engine also uses, so what the gateway enforces and what
`getAiEffectiveParameters` reports come from one implementation and cannot drift. That
is the reason ADR-0002 chose LiteLLM.

| Module | What it does |
| --- | --- |
| `enforcement.py` | `async_pre_call_hook`: key scopes, resolved generation parameters, metering record with no prompt content |

Milestone 9 adds guardrail pre/post hooks here, including streamed-response redaction.
