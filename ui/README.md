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
CLOUDSTACK_USER_DOMAIN=Customers/Acme \
  npm run test:e2e
```

An account outside ROOT needs its domain: CloudStack's login form has a
third field, and without it the sign-in simply never completes.
`CLOUDSTACK_AI_V2=1` additionally runs the v2 group, which is skipped
while the setting is off.

They check each default role sees what it should and not what it should
not, and that a tenant reaching an admin screen by URL cannot get to it.
**The whole suite passes — 39, with 8 skipped**, against a management
server with the plugin loaded and `ai.services.v2.enabled` on.

It used to be 22 role specs plus 9 of 13 in the v2 group, with the rest
put down to "sign-in timeouts and one screen that wants a mirrored model
to click". That was the wrong diagnosis. Four specs clicked
`getByRole('row').nth(1)`, and **clicking a row does nothing** —
CloudStack puts the link on the first column — while one looked for a
button by its words, and **an action button is icon-only** with the words
in a hover tooltip. Both facts were already written down, and `table.js`
exports `rowLink` and `action` for exactly them; the specs simply did not
use them. They do now.

One of them was `test.fixme` for a while, because the product was what
was wrong: `createAiApiKey` returns the secret and CloudStack's dialog
closed without showing it, and the hooks that would have shown it are
inside `$pollJob`, so they fire for an async command only. The API keys
tab mints the key itself now, so the spec asserts the real thing again
and nothing is skipped for being unbuilt.

**The counts above were last measured before that change.** The suite
has not been re-run since, because every published port on every
container on this machine stopped carrying data — not just this
harness, and Postgres as well as HTTP. The management server itself
stayed healthy throughout, answering 401 to an unsigned call from
inside its own container. Re-run the suite before trusting the
numbers.

### Pointing them at a real CloudStack

The section has to be *in* a CloudStack UI tree, and that tree has to be
built and served at `/client`, which is where these specs look:

```
cp -r src/config/section/aiservices.js  <cloudstack>/ui/src/config/section/
cp -r src/views/aiservices  <cloudstack>/ui/src/views/
cp -r src/utils/*.js  <cloudstack>/ui/src/utils/
# and merge this repo's keys into CloudStack's own locale
python3 -c "import json,sys; a=json.load(open(sys.argv[1])); b=json.load(open(sys.argv[2])); \
a.update(b); json.dump(dict(sorted(a.items())), open(sys.argv[1],'w'), indent=2)" \
  <cloudstack>/ui/public/locales/en.json public/locales/en.json
cd <cloudstack>/ui && npm run build
cp -a dist/. <cloudstack>/client/target/classes/META-INF/webapp/
```

**`npm run serve` is not a substitute.** The dev server hosts the app at
`/` and forwards every `/client` path to the management server, so
`/client/` returns CloudStack's "you're in developer mode" placeholder
while the specs, correctly, use `/client/#/…`.

**If the build dies in `thread-loader` with `EPIPE`**, it is a worker
crash, not your code. Measured in the compose harness, where an amd64
image runs emulated on an arm64 Mac: with the management server and the
dev server both up, under 2 GB of the container's 8 GB was free, and
parallel workers under emulation did not survive it. Stopping both
services first and setting `parallel: false` in CloudStack's
`vue.config.js` builds cleanly in one pass. A native amd64 host with
memory to spare should not need either.

**A production build is not faster to test against.** It was tried for
that reason and the run took 10 minutes rather than 2.4, because Jetty
serves each chunk separately under emulation. Build for fidelity — it is
the artifact that ships — not for speed.

## Two toolchain notes

- The package is deliberately **not** `"type": "module"`. Playwright
  1.49 hangs at config load when it is, on Node 25 — no output, no
  timeout, in `--list` as much as in a run. Vitest's config carries the
  `.mjs` extension instead.
- `@/store` and `@/components` are aliased to doubles for Vitest. Those
  live in CloudStack's build, and the shipped files import them exactly
  as every other section does, so only the test config knows.
