# Third-party licences

**Last reviewed:** 2026-11-07 (milestone 2, at ADR-0002)
**Enforced by:** `tests/security/test_licenses.py`, run by `make test-security` in CI.

## Why this file exists

This is a plugin for Apache CloudStack, an Apache Software Foundation project under
Apache-2.0, and SPEC.md section 14 keeps open the option of upstreaming a generic
`ai-services` plugin. So the dependency tree is held to the ASF's third-party licensing
policy from the start, rather than discovering a conflict at the point of upstreaming.

| Category | Licences | Policy |
| --- | --- | --- |
| **A** | Apache-2.0, MIT, BSD, ISC, PSF, CC0, Unlicense | Fine |
| **B** | MPL, EPL, CDDL, CPL | Allowed as a dependency, with attribution; not bundled as source into a release |
| **X** | GPL, LGPL, AGPL, SSPL, Commons Clause, BUSL, proprietary | **Prohibited** |

LGPL is Category X at the ASF, and that holds for dynamically linked Python imports too.
That rule is stricter than LGPL itself requires, and it is the rule this project follows.

## Current state

**No Category X dependencies.** Category A throughout except the four below, and nothing
unclassified. Re-checked when `boto3` and `huggingface-hub` were added in milestone 3.

One package needs an override because its metadata is uninformative: **`python-dateutil`**
publishes only "Dual License", and its upstream LICENSE is Apache-2.0 and BSD-3-Clause —
Category A either way. The override and its reason are in
`tests/security/license_policy.py`.

### Category B — accepted, with reasons

| Package | Licence | Why it is acceptable |
| --- | --- | --- |
| `certifi` | MPL-2.0 | Runtime, via `httpx`. A CA-certificate bundle, used unmodified as a dependency; no MPL-covered source is bundled into our release. |
| `hypothesis` | MPL-2.0 | Test-only, never shipped. |
| `pathspec` | MPL-2.0 | Development-only, via `mypy`. Never shipped. |
| `tqdm` | MPL-2.0 AND MIT | Runtime, via `huggingface-hub`. Progress bars, used unmodified; the MPL-covered files are not modified or bundled as source. |

MPL-2.0 is file-level copyleft: the obligation attaches to modified MPL files, and we modify
none of them. Adding a fourth Category B dependency should be a deliberate decision — the
test fails until it is listed in `ACCEPTED_CATEGORY_B` and here.

### One conflict found and fixed

**`psycopg` 3.x is LGPL-3.0-only** and was the Postgres driver chosen in milestone 2. It was
replaced with **`pg8000` (BSD-3-Clause)**, a pure-Python driver SQLAlchemy supports as the
`postgresql+pg8000://` dialect. The run log is synchronous SQLAlchemy, so nothing was lost.

`psycopg2` and `psycopg2-binary` are also LGPL and equally out. If an async driver is ever
needed, **`asyncpg` is Apache-2.0** and is the right next choice. The test asserts by name
that no psycopg variant comes back.

## What this does *not* cover

The scan reads Python distribution metadata. Three obligations sit outside it and need a
human — and before any of this is shipped to customers, a lawyer.

### 1. GPU drivers on the golden images — two licences, two different problems

Both golden images install a GPU driver, and the two vendors' terms are not the same kind of
obligation. Neither is a **code-licence conflict**: nothing in this repository links against
either, they are installed on a node image the way they would be on any GPU host, and the
Category X rule above governs the *dependency tree of what the ASF distributes* — Python
imports and Maven artifacts — not a kernel driver on a VM an operator builds. Both are
**redistribution** questions about an artifact we hand to providers.

**NVIDIA — proprietary EULA.** `templates/packer/gpu-golden.pkr.hcl` installs the NVIDIA
driver and container toolkit. These are proprietary, under NVIDIA's own licence, not
Apache-2.0, and that licence governs whether a prebuilt template may be passed on at all.

**AMD — GPL-2.0, which restricts less and obliges more.**
`templates/packer/gpu-golden-rocm.pkr.hcl` installs `amdgpu-dkms` from repo.radeon.com, AMD's
packaging of the `amdgpu` kernel driver that is in mainline Linux under **GPL-2.0**, plus
`amd-smi-lib`, `rocm-smi-lib` and `rocminfo` for the node's boot check. The SMI tooling is
permissively licensed; ROCm userspace proper is not on the host at all, since it lives in the
engine container. Note the inversion: GPL-2.0 does not restrict redistribution the way an EULA
does, but it attaches **source-availability obligations** to shipping a prebuilt image, which a
proprietary driver does not. The repository also pulls GPU firmware, which is separately
licensed and not GPL.

**Do not read the GPL here as licence to relax the rule above.** It applies to a driver on a
node image, not to anything this project imports or ships as code, and `pg8000` is still the
Postgres driver for exactly the reason stated earlier.

Three practical consequences:

- Review NVIDIA's current driver and container-toolkit terms, and AMD's GPL-2.0 source
  obligations and firmware licence, before distributing prebuilt templates, as opposed to
  shipping the Packer files and having each provider build their own.
- Shipping the `.pkr.hcl` files and letting the operator build is the conservative path, and is
  what the repository does today. It sidesteps both problems at once.
- A provider who does distribute a prebuilt ROCm image needs an answer to the source offer.
  That is a lawyer's question, and it has not been asked.

### 2. Model weights — "open weights" is not "open source"

`licenses.yaml` is the allow-list SPEC.md section 5 enforces at mirror and deploy time. It
deliberately mixes two very different kinds of licence, and the file now says so:

- **OSI-approved** (`apache-2.0`, `mit`, `bsd-3-clause`, `cc-by-4.0`): no use restrictions.
- **Open-weight, restricted** (`llama3.x`, `gemma`, `qwen`, `openrail`): redistribution and
  acceptable-use conditions, naming and attribution requirements, and in Llama's case a
  monthly-active-user threshold above which a separate licence from Meta is required.

This matters more here than in most projects, because **we resell inference on these models to
tenants**. The platform mechanism is right — a root-admin-approved allow-list, enforced twice —
but the default list should not be read as a legal clearance. Each entry needs a commercial
review against the intended business model before it is offered to paying tenants.

### 3. LiteLLM — MIT core, proprietary `enterprise/`

[ADR-0002](adr/0002-gateway.md) selects LiteLLM Proxy. Its repository is **split**: the root
`LICENSE` places `enterprise/` under a separate commercial BerriAI licence, and everything
outside it under MIT.

**We must stay on the MIT side.** The hooks in `gateway-hooks/` use the ordinary proxy hook
API, which is MIT. The constraint to hold going forward:

- Do not import from, copy, or depend on anything under `enterprise/`.
- Do not enable features gated behind an enterprise licence key.
- The features we need — virtual keys, budgets, RPM/TPM limits, spend tracking, custom hooks —
  are in the MIT core. The enterprise tier is SSO, fine-grained RBAC and support, and
  [ADR-0002](adr/0002-gateway.md) notes we need none of them: CloudStack owns auth and RBAC.
- Pin the LiteLLM version, and re-check the licence split on each upgrade; a GitHub issue
  argues the premium gating lives in MIT-licensed files, which is unsettled.

When LiteLLM lands as a dependency in milestone 3, add it to this file and confirm the scan
reports it as MIT.

### Also outside the scan

- **Java plugin** (milestone 5): it goes into a CloudStack source checkout, so every Maven
  dependency it adds is subject to the same ASF policy. Audit it when it exists — this scan is
  Python-only.
- **UI** (milestone 6): npm dependencies, same policy, same gap.
- **Container images** (vLLM, SGLang, TensorRT-LLM): vLLM is Apache-2.0; **TensorRT-LLM pulls
  in proprietary NVIDIA components**, which is worth knowing before `engine: trtllm` is
  offered commercially.

## Running the check

```bash
make test-security
```

It fails on any Category X dependency, any licence it cannot classify, and any new Category B
package not listed above. `tests/security/license_policy.py` holds the classifier and an
`_OVERRIDES` table for packages whose metadata is wrong — each override needs a stated reason.
