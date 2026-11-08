# cloud-plugin-api-ai-services

The native CloudStack API plugin (SPEC.md section 9), targeting
**CloudStack 4.23.0.0** ([ADR-0003](../docs/adr/0003-cloudstack-release.md)).

## Building

CloudStack publishes **no Maven artifacts** — not to Maven Central, not to the ASF
repositories. The dependencies must be built from source into your local repository first:

```bash
git clone --depth 1 --branch 4.23.0.0 --single-branch https://github.com/apache/cloudstack.git
```

```bash
cd cloudstack && mvn install -DskipTests -Dcheckstyle.skip -Dlicense.skip -Drat.skip -pl api,utils,framework/config,framework/db -am
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

Thirty-five v1 commands and a dozen response types differ only in name, verb, engine path,
parameters and response shape. Writing them out by hand invites the one transposed path or
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

## Two rules worth keeping

**The caller context is built from `CallContext`, never from request parameters.** The engine
authenticates nobody — it is unreachable from tenant networks and requires a client
certificate — and filters every response by what this plugin asserts. A caller able to
influence it could read another tenant's data.

**The command list is generated, not hand-written.** `aisvc/api/commands.py` holds section 9's
map, and the Python tests check it against the engine's routes. Adding a command there and
forgetting it here should be caught by a test, not by a Marvin run.
