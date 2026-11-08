# AI Services UI section (SPEC.md section 9)

Installing this is one part of standing the product up; see
[docs/installation.md](../docs/installation.md) for the whole sequence.

The `aiservices` section for the CloudStack `ui/` tree.

## What goes where

Paths here mirror CloudStack's own, so files copy across unchanged:

| Here | In CloudStack's `ui/` |
| --- | --- |
| `src/config/section/aiservices.js` | same path |
| `src/views/aiservices/*.vue` | same path |
| `public/locales/en.json` | merged into theirs |

The section also has to be imported and listed in
`src/config/router.js`, beside `compute`, `storage` and the rest.

## Declarative on purpose

Almost every screen is config, not code. CloudStack builds list views,
detail views, search, pagination, async job notifications and
role-based action visibility from that config, so a screen described
there inherits all of it and behaves like every other section. That is
also what keeps SPEC.md's "everything in the UI is also available
through CloudMonkey and Terraform" true: the config names API commands,
and nothing else.

Five screens are components because a table cannot express them: the
setup wizard (a sequence), the GPU heatmap, effective parameters (a
value is meaningless without the level it came from), the Hugging Face
browser and the playground.

## Tests

```
make test-ui
```

runs Vitest. Four of those tests are drift guards and are the reason
most of the others can stay thin:

- **Every `api:` the section names exists in `aisvc/api/commands.py`.**
  The UI is the only place that could reference a command the engine
  does not map; a rename would otherwise leave a button that fails with
  nothing in the console to explain it.
- **Every component path resolves.** A typo in a dynamic import is a
  blank screen and a chunk-load error.
- **Every `label.*` key exists in `en.json`, and none is unused.**
  CloudStack renders a missing key as the key itself.
- **Every locale file has English's keys, sorted.**

The Playwright tests sign in as real accounts against a real CloudStack
and are skipped unless one is pointed at, as the Terraform acceptance
tests are:

```
CLOUDSTACK_UI_URL=http://localhost:8080 \
CLOUDSTACK_ADMIN_USER=admin CLOUDSTACK_ADMIN_PASSWORD=password \
CLOUDSTACK_USER_USER=tenant CLOUDSTACK_USER_PASSWORD=password \
  npm run test:e2e
```

They check each default role sees what it should and not what it should
not, and that a tenant reaching an admin screen by URL is refused by the
plugin rather than only hidden by the menu. **They have not been run
yet** — no CloudStack has served this section.

## Two toolchain notes

- The package is deliberately **not** `"type": "module"`. Playwright
  1.49 hangs at config load when it is, on Node 25 — no output, no
  timeout, in `--list` as much as in a run. Vitest's config carries the
  `.mjs` extension instead.
- `@/store` and `@/components` are aliased to doubles for Vitest. Those
  live in CloudStack's build, and the shipped files import them exactly
  as every other section does, so only the test config knows.
