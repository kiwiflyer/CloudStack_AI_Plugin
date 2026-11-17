# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements. See the NOTICE file distributed with this
# work for additional information regarding copyright ownership. The ASF
# licenses this file to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Every AI Services command, per default role, allowed and denied.

SPEC.md section 12 asks for exactly this pair for every native command,
and until now there was nothing here to ask it of: a live 4.23.0.0
refused a tenant all thirty-eight v1 commands, because a plugin
declares its default role access on `@APICommand(authorized = ...)`
and ours declared none.

**Nothing here executes a command.** Each is called with no parameters
at all, which separates the two questions cleanly, because CloudStack
checks access before it validates parameters:

    denied   432, "The API [x] does not exist or is not available
                   for the account y"
    allowed  431 for a missing required parameter, or an error from
             the engine, or a result — anything but 432

So a command a role may call fails on its arguments and a command it
may not fails on its name, and no deployment is created, no node is
drained and no certificate is replaced in the course of finding out.

The expectations are generated from `aisvc/api/commands.py` into
`role_permissions.json` beside this file, because Marvin runs under
CloudStack's own Python where `aisvc` is not importable.
`tests/unit/test_commands.py` keeps the two in step.
"""
import json
import os

from marvin.cloudstackTestCase import cloudstackTestCase
from marvin.lib.base import Account, Domain
from marvin.lib.common import get_domain
from nose.plugins.attrib import attr

#: CloudStack's own code for "you may not call this".
UNAVAILABLE = "does not exist or is not available"

HERE = os.path.dirname(os.path.abspath(__file__))


def expectations():
    with open(os.path.join(HERE, "role_permissions.json")) as handle:
        return json.load(handle)["roles"]


def command(name, **params):
    """A command object Marvin will send without knowing it.

    Marvin's bindings are generated from a management server, so they
    have no class for a command a plugin added afterwards. It only
    needs the class name and some attributes: `__sanitizeCmd` takes
    the command's name from `cmd.__class__.__name__` and its payload
    from everything that is not a dunder.
    """
    shim = type(str(name + "Cmd"), (object,), {"isAsync": "false", "required": []})
    cmd = shim()
    for key, value in params.items():
        setattr(cmd, key, value)
    return cmd


class TestAiServicesRolePermissions(cloudstackTestCase):
    """What each default role may and may not call."""

    @classmethod
    def setUpClass(cls):
        cls.testClient = super().getClsTestClient()
        cls.apiclient = cls.testClient.getApiClient()
        cls.services = cls.testClient.getParsedTestDataConfig()
        cls.domain = get_domain(cls.apiclient)
        cls.cleanup = []

        cls.child = Domain.create(cls.apiclient, services={"name": "AiServicesRoles"},
                                  parentdomainid=cls.domain.id)
        cls.cleanup.append(cls.child)

        # accounttype 2 is a domain admin, 0 an ordinary user.
        cls.domain_admin = Account.create(
            cls.apiclient, cls.services["account"], admin=False,
            domainid=cls.child.id, accounttype=2)
        cls.cleanup.append(cls.domain_admin)
        cls.user = Account.create(
            cls.apiclient, cls.services["account"], admin=False,
            domainid=cls.child.id)
        cls.cleanup.append(cls.user)

        cls.clients = {
            "Admin": cls.apiclient,
            "DomainAdmin": cls.testClient.getUserApiClient(
                UserName=cls.domain_admin.name, DomainName=cls.child.name, type=2),
            "User": cls.testClient.getUserApiClient(
                UserName=cls.user.name, DomainName=cls.child.name, type=0),
        }
        cls.expected = expectations()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()

    def refused(self, client, name):
        """Whether CloudStack refused this command by name.

        A command that gets as far as complaining about its arguments
        was allowed; that is the distinction being drawn.
        """
        try:
            client.connection.marvinRequest(command(name))
            return False
        except Exception as answered:
            # The message, not the exception type: Marvin wraps a
            # refusal in more than one class depending on where it
            # notices, and reading the type would quietly call a
            # refusal an allowance. Any other failure means the
            # command ran and failed on its own terms, which is
            # still access granted.
            return UNAVAILABLE in str(answered)

    def registered(self):
        response = self.apiclient.connection.marvinRequest(command("listApis"))
        return {api["name"] for api in response["api"]}

    def check(self, role, release):
        """Every command in this release, for this role, both ways."""
        live = self.registered()
        allowed = set(self.expected[release + "." + role])
        everything = set(self.expected[release + ".Admin"]) | allowed
        client = self.clients[role]

        if not (everything & live):
            self.skipTest(
                f"no {release} AI Services commands are registered; the plugin "
                "is absent or ai.services.v2.enabled is off")

        wrong = []
        for name in sorted(everything & live):
            denied = self.refused(client, name)
            if name in allowed and denied:
                wrong.append(f"{role} may call {name} and was refused")
            if name not in allowed and not denied:
                wrong.append(f"{role} may not call {name} and was not refused")

        self.assertEqual([], wrong, "\n".join(wrong))

    @attr(tags=["advanced", "smoke"], required_hardware="false")
    def test_01_root_admin_v1(self):
        self.check("Admin", "v1")

    @attr(tags=["advanced", "smoke"], required_hardware="false")
    def test_02_domain_admin_v1(self):
        self.check("DomainAdmin", "v1")

    @attr(tags=["advanced", "smoke"], required_hardware="false")
    def test_03_user_v1(self):
        self.check("User", "v1")

    @attr(tags=["advanced", "smoke"], required_hardware="false")
    def test_04_root_admin_v2(self):
        self.check("Admin", "v2")

    @attr(tags=["advanced", "smoke"], required_hardware="false")
    def test_05_domain_admin_v2(self):
        self.check("DomainAdmin", "v2")

    @attr(tags=["advanced", "smoke"], required_hardware="false")
    def test_06_user_v2(self):
        self.check("User", "v2")

    @attr(tags=["advanced", "smoke"], required_hardware="false")
    def test_07_a_command_nobody_declared_is_refused(self):
        """The check is capable of seeing a refusal.

        Without this, a `refused` that always returned False would
        make every allowed-direction assertion pass and every
        denied-direction one fail loudly — but a bug that made it
        always return True would pass the denied half silently.
        """
        self.assertTrue(
            self.refused(self.clients["User"], "listAiNothingAtAll"),
            "a command that does not exist was not refused")
