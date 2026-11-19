# cloud-plugin-api-ai-services

Building and deploying this is one part of standing the product up; see
[docs/installation.md](../docs/installation.md) for the whole sequence.

The native CloudStack API plugin (SPEC.md section 9), targeting
**CloudStack 4.23.0.0** ([ADR-0003](../docs/adr/0003-cloudstack-release.md)).

It has been loaded into a real 4.23.0.0 management server: all 38 v1
commands register (`listApis` 865 → 903), twenty more with
`ai.services.v2.enabled` on, the `ai_deployment` table is created at
startup, the ten `ai.services.*` settings appear in
`listConfigurations`, and commands answer with data from the engine
over mTLS. The per-role Marvin tests in
[`test/integration/plugins/ai-services/`](../test/integration/plugins/ai-services/)
cover all 58 commands, allowed and denied, and pass.

**Loading it is how the defects were found.** A Spring context that
did not declare a bean it injected took the whole `/client` webapp
down, not just the plugin; seven write commands sent request bodies
missing exactly the parameters whose names needed no translation; a
PEM parameter was capped at `@Parameter`'s default 255 characters;
and both the sync and async paths threw away the engine's explanation
of a failure. Every one compiled and passed its unit tests.

## Building

CloudStack publishes **no Maven artifacts** — not to Maven Central, not to the ASF
repositories. The dependencies must be built from source into your local repository first:

```bash
git clone --depth 1 --branch 4.23.0.0 --single-branch https://github.com/apache/cloudstack.git
```

```bash
cd cloudstack && mvn install -DskipTests -Dcheckstyle.skip -Dlicense.skip -Drat.skip -pl api,utils,framework/config,framework/db,framework/events,engine/schema -am
```

Then, from the repository root:

```bash
make test-java
```

Needs JDK 17 to build (`brew install openjdk@17 maven`); it targets JDK 11 bytecode, which is
what CloudStack 4.23 sets as `cs.jdk.version`. `openjdk@17` is keg-only, so set
`JAVA_HOME=/opt/homebrew/opt/openjdk@17`.

## Layout

| Path | What it is |
| --- | --- |
| `AiServicesConfig` | The ConfigKeys of section 9. Enablement is zone-scoped, Hugging Face approval domain-scoped |
| `AiServicesService` / `AiServicesManagerImpl` | The `PluggableService` and `Configurable` entry point |
| `engine/EngineClient` | Calls the engine, failing over across replicas |
| `engine/HttpEngineTransport`, `engine/MutualTls` | The mTLS transport |
| `engine/CallerContext` | The identity the engine filters every response by |
| `api/command/`, `api/response/` | API commands and their responses |
| `META-INF/db/schema-ai-services.sql` | The `ai_deployment` ownership table |

## Generated code

Thirty-six of the thirty-eight v1 commands, the twenty v2 ones, and twenty-one
response types differ only in name, verb, engine path, parameters and response shape.
(`deployAiModel` and `deleteAiModelDeployment` are the two written by hand: they own
the `ai_deployment` row CloudStack uses for ownership and cleanup, which the generator
does not model.) Writing them out by hand invites the one transposed path or
wrong role no reviewer would spot, so the leaves are generated:

```bash
python3 tools/generate_commands.py
```

Generated files say so in their header. **Edit `tools/generate_commands.py`, not them.** The
interesting parts — the base classes, the engine client, the async bridge, the DAO — are
hand-written.

The generator also emits `src/test/resources/generated-engine-objects.json`, a fully-populated
engine object per response type, so `GeneratedSurfaceTest` exercises every mapped field
without that list being maintained twice.

## The 4.22 LTS floor

[ADR-0003](../docs/adr/0003-cloudstack-release.md) targets 4.23, which is **non-LTS**. The
plugin is held to APIs that also exist in 4.22 LTS so the eventual retarget costs a rebuild
rather than a rewrite. Verify locally by building 4.22's modules the same way and then:

```bash
mvn -Dcs.version=4.22.1.1 test
```

CI runs this on every change.

## Two rules worth keeping

**The caller context is built from `CallContext`, never from request parameters.** The engine
authenticates nobody — it is unreachable from tenant networks and requires a client
certificate — and filters every response by what this plugin asserts. A caller able to
influence it could read another tenant's data.

**The command list is generated, not hand-written.** `aisvc/api/commands.py` holds section 9's
map, and the Python tests check it against the engine's routes. Adding a command there and
forgetting it here should be caught by a test, not by a Marvin run.

## Building it inside a CloudStack tree

Out of tree this pom is deliberately standalone: it inherits no parent, so the module
builds without a CloudStack checkout on the path, and it pins `cs.version` with every
CloudStack dependency `provided`. In tree it inherits `cloudstack-plugins` and takes
versions from the reactor. Three things about its test dependencies cost a build each,
and each fails differently:

- **`junit-jupiter` and `junit-jupiter-params` need an explicit version.** CloudStack's
  own tests are JUnit 4, so the root pom has a `cs.junit.jupiter.version` property but no
  `dependencyManagement` entry. Use that property rather than a second opinion about
  which 5.x to run.
- **`mockito-core` must not be declared.** The root pom puts it in its own
  `<dependencies>`, not in `<dependencyManagement>`, so every module inherits it already
  — and declaring it without a version is what fails, since there is nothing managed to
  inherit.
- **Surefire needs `surefire-junit-platform` named explicitly.** The root puts JUnit 4 on
  every module's classpath, so this module has both 4 and 5 on one test classpath, and
  surefire choosing the JUnit 4 provider runs **none** of these tests while reporting
  success for having run nothing. That failure is silent, which is why it is written down.

**The directory is `plugins/api/ai-services`**, not `plugins/ai-services`: CloudStack's
convention ties the artifactId prefix to the path, so `cloud-plugin-api-ai-services` sits
beside `cloud-plugin-api-discovery` at `plugins/api/discovery`.

Checkstyle runs in tree and does not out of tree, and `cloud-style.xml` makes an unused
import an **error**. Build the module in a real tree before trusting it.
