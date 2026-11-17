<!--
Licensed to the Apache Software Foundation (ASF) under one or more
contributor license agreements. See the NOTICE file distributed with this
work for additional information regarding copyright ownership. The ASF
licenses this file to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance with the
License. You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
License for the specific language governing permissions and limitations
under the License.
-->

# Marvin tests for AI Services

SPEC.md section 12 asks for a test per native command per default
role, allowed and denied. This is that, for all fifty-eight.

## What they check, and what they do not

Every command is called **with no parameters at all**, and the two
outcomes are told apart by which complaint comes back:

```
denied   432  The API [x] does not exist or is not available for the account y
allowed  431  Unable to execute API command x due to missing parameter z
```

CloudStack checks access before it validates parameters — measured on
4.23.0.0, not assumed — so a command a role may call fails on its
arguments and one it may not fails on its name. **Nothing is
executed**: no deployment is created, no node drained, no certificate
replaced in the course of finding out.

That also means these tests say nothing about whether a command
*works*. They are about who may reach it.

## Running them

They need a management server with the plugin loaded. The v2 tests
skip themselves unless `ai.services.v2.enabled` is on, since the
commands are not registered otherwise.

```bash
# from a CloudStack checkout with Marvin installed
nosetests --with-marvin --marvin-config=setup/dev/advanced.cfg \
  test/integration/plugins/ai-services/test_ai_services_roles.py
```

**That will not work on Python 3.12.** `nose` imports `imp`, which
3.12 removed, so it cannot be imported at all — and neither can any
of CloudStack's own Marvin tests, every one of which imports
`nose.plugins.attrib`. Nothing in Marvin itself needs nose: its
plugin only builds a test client and hangs it on the test class.
`run_without_nose.py` beside this file does that much and runs the
suite under plain `unittest`:

```bash
python3 run_without_nose.py /root/setup/dev/advanced.cfg
```

The accounts are created and removed by the test: a child domain, a
domain admin and an ordinary user. They are tracked in `cls.created`
and not `cls.cleanup`, because `cloudstackTestCase.tearDown` empties
`cleanup` after *every* test — put them there and the domain is gone
after the first one, and the rest run against accounts that no
longer exist.

## Why the expectations are a JSON file

Marvin runs under CloudStack's own Python, where `aisvc` cannot be
imported, so `role_permissions.json` carries a copy of what
`aisvc/api/commands.py` grants. It is generated, not written, and
`tests/unit/test_commands.py` fails if the two drift — the same
arrangement as the Java generator's command table.

## Marvin does not know these commands

Its bindings are generated from a management server, so there is no
class for a command a plugin added afterwards. `command()` builds a
stand-in: `__sanitizeCmd` takes the name from
`cmd.__class__.__name__` and the payload from the instance's
attributes, which is all it needs.

## What running them proved

All seven pass against CloudStack 4.23.0.0 with the plugin loaded and
`ai.services.v2.enabled` on: 58 commands, three roles, both
directions.

Getting there took four rounds, and every failure was in the test
rather than the product:

- `Account.create` has no `accounttype`; Marvin's own rule is
  `accounttype = 2 if (admin and domainid) else int(admin)`
- the api client's `id` is None unless something sets the client's
  identifier, and `Account.create` builds a username from it
- `marvinRequest` unwraps the envelope and returns a list of
  `jsonLoader` objects, not `{"api": [...]}`
- the accounts lived in `cls.cleanup`, so they were deleted after the
  first test and the next five reported fifteen admin-only commands
  as unrefused for a tenant — with the product behaving correctly
  throughout

The last one is worth remembering: a broken client makes every
command look permitted, because the call fails for a reason that is
not a refusal. `test_07` exists to catch exactly that, and it was
failing all along.
