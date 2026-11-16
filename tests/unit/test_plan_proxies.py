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
"""Planning private-endpoint proxies (SPEC.md sections 4 and 8).

`ProxyProvisioner` was correct and uncalled: creating an exposure took
a deployment off the public gateway without bringing up the VM meant to
replace it, which left it unreachable rather than privately reachable.

Ordering is the substance here. The proxy has to exist *before* the
model leaves the public gateway, or there is a window — a whole
reconcile, in the worst case — where the tenant has no route at all.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import yaml

from aisvc.endpoints import EndpointExposure, TlsSource
from aisvc.endpoints.proxy import proxy_name
from aisvc.inventory import CapacitySnapshot
from aisvc.reconciler import ActionKind, plan_all
from aisvc.reconciler.plan import plan_proxies
from aisvc.reconciler.state import ActualState
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import PlatformSpec, load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

BASE = "llama-3.3-70b-instruct"


def spec(*, proxy: bool = True) -> PlatformSpec:
    doc = yaml.safe_load(MINIMAL)
    if proxy:
        doc["zones"][0]["proxy_template"] = "aisvc-proxy-1.0"
        doc["zones"][0]["proxy_offering"] = "small"
    return load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)


def exposure(**kw: Any) -> EndpointExposure:
    defaults: dict[str, Any] = {
        "deployment": BASE,
        "account": "acme",
        "domain": "/Customers/Acme",
        "public_enabled": False,
        "network_id": "net-1",
    }
    defaults.update(kw)
    return EndpointExposure(**defaults)


def state(**kw: Any) -> ActualState:
    return ActualState(capacity=CapacitySnapshot(taken_at=dt.datetime.now(dt.UTC)), **kw)


def registry() -> ModelRegistry:
    return ModelRegistry(InMemoryObjectStore(), bucket="models")


def proxy_actions(actions: Any) -> list[Any]:
    return [a for a in actions if a.resource_type == "endpoint-proxy"]


class TestWhatIsPlanned:
    def test_a_new_exposure_plans_a_proxy(self) -> None:
        actions = plan_proxies(spec(), state(exposures=(exposure(),)))

        assert [a.kind for a in actions] == [ActionKind.CREATE]
        assert actions[0].name == proxy_name("acme", BASE)

    def test_an_existing_proxy_plans_nothing(self) -> None:
        """A second apply must produce an empty plan."""
        name = proxy_name("acme", BASE)

        actions = plan_proxies(spec(), state(exposures=(exposure(),), proxies={name: "vm-1"}))

        assert actions == []

    def test_a_public_only_exposure_plans_no_proxy(self) -> None:
        actions = plan_proxies(
            spec(),
            state(exposures=(exposure(public_enabled=True, network_id=""),)),
        )

        assert actions == []

    def test_an_exposure_that_is_gone_plans_a_delete(self) -> None:
        """Its VM keeps a NIC on the tenant's tier until it is removed."""
        name = proxy_name("acme", BASE)

        actions = plan_proxies(spec(), state(exposures=(), proxies={name: "vm-1"}))

        assert [a.kind for a in actions] == [ActionKind.DELETE]
        assert actions[0].name == name

    def test_a_proxy_nobody_asked_for_is_left_alone(self) -> None:
        """Only names this engine would have minted are ours to delete."""
        actions = plan_proxies(spec(), state(exposures=(), proxies={"someone-elses-vm": "vm-9"}))

        assert actions == []

    def test_two_tenants_get_two_proxies(self) -> None:
        actions = plan_proxies(
            spec(),
            state(
                exposures=(
                    exposure(),
                    exposure(account="other", domain="/Customers/Other"),
                )
            ),
        )

        assert len(actions) == 2

    def test_nothing_to_do_without_exposures(self) -> None:
        assert plan_proxies(spec(), state()) == []


class TestTheZoneHasToBeConfigured:
    def test_a_zone_without_a_proxy_template_warns(self) -> None:
        """Planning a VM with no template would fail at execute time
        with a CloudStack error nobody can act on."""
        actions = plan_proxies(spec(proxy=False), state(exposures=(exposure(),)))

        assert actions == []

    def test_the_warning_says_what_to_configure(self) -> None:
        plan = plan_all(
            spec(proxy=False),
            state(exposures=(exposure(),)),
            registry=registry(),
        )

        assert any("proxy_template" in w for w in plan.warnings)


class TestOrdering:
    def plan(self, **kw: Any) -> Any:
        return plan_all(spec(), state(**kw), registry=registry())

    def test_the_proxy_is_created_before_the_routes_change(self) -> None:
        """Otherwise the model leaves the public gateway while nothing
        private is up, and the tenant has no route at all."""
        plan = self.plan(exposures=(exposure(),))

        routes = next(a for a in plan.actions if a.resource_type == "gateway-config")
        proxy = next(a for a in proxy_actions(plan.actions))

        assert proxy.key in routes.depends_on

    def test_a_proxy_delete_does_not_block_the_routes(self) -> None:
        """Removing an exposure re-publishes the model; that should not
        wait on tearing a VM down."""
        name = proxy_name("acme", BASE)
        plan = self.plan(exposures=(), proxies={name: "vm-1"})

        routes = [a for a in plan.actions if a.resource_type == "gateway-config"]
        deletes = [a for a in proxy_actions(plan.actions) if a.kind is ActionKind.DELETE]

        assert deletes
        for route in routes:
            assert deletes[0].key not in route.depends_on

    def test_proxies_appear_in_the_plan(self) -> None:
        plan = self.plan(exposures=(exposure(),))

        assert proxy_actions(plan.actions)

    def test_a_settled_platform_plans_nothing_for_proxies(self) -> None:
        plan = self.plan(exposures=(exposure(),), proxies={proxy_name("acme", BASE): "vm-1"})

        assert proxy_actions(plan.actions) == []


class TestRendering:
    def test_the_action_says_which_network(self) -> None:
        actions = plan_proxies(spec(), state(exposures=(exposure(),)))

        assert "net-1" in actions[0].detail

    def test_the_action_says_why(self) -> None:
        actions = plan_proxies(spec(), state(exposures=(exposure(),)))

        assert actions[0].reason


class TestExecuting:
    """The executor has to know what an endpoint-proxy action means, or
    the plan prints it and nothing happens."""

    def executor(self, provisioner: Any) -> Any:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from aisvc.db import Base, RunLog
        from aisvc.reconciler import Executor

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(engine)()
        return Executor(
            provisioner=None,
            run_log=RunLog(session),
            proxies=provisioner,
        )

    class FakeProxies:
        def __init__(self) -> None:
            self.ensured: list[str] = []
            self.removed: list[str] = []

        async def ensure_named(self, name: str, *, dry_run: bool = False) -> list[str]:
            self.ensured.append(name)
            return [name]

        async def remove_named(self, name: str, *, dry_run: bool = False) -> list[str]:
            self.removed.append(name)
            return [name]

    async def test_a_create_reaches_the_provisioner(self) -> None:
        from aisvc.reconciler import Plan

        proxies = self.FakeProxies()
        actions = plan_proxies(spec(), state(exposures=(exposure(),)))

        await self.executor(proxies).execute(Plan(actions=actions), zone="zone-east-1")

        assert proxies.ensured == [proxy_name("acme", BASE)]

    async def test_a_delete_reaches_it_too(self) -> None:
        from aisvc.reconciler import Plan

        name = proxy_name("acme", BASE)
        proxies = self.FakeProxies()
        actions = plan_proxies(spec(), state(exposures=(), proxies={name: "vm-1"}))

        await self.executor(proxies).execute(Plan(actions=actions), zone="zone-east-1")

        assert proxies.removed == [name]

    async def test_a_dry_run_changes_nothing(self) -> None:
        from aisvc.reconciler import Plan

        proxies = self.FakeProxies()
        actions = plan_proxies(spec(), state(exposures=(exposure(),)))

        run = await self.executor(proxies).execute(
            Plan(actions=actions), zone="zone-east-1", dry_run=True
        )

        assert run.status.value == "succeeded"

    async def test_without_a_provisioner_the_action_is_not_silently_dropped(
        self,
    ) -> None:
        """A plan that prints a proxy and quietly skips it is worse than
        one that fails: the operator believes it was built."""
        from aisvc.reconciler import Plan

        executor = self.executor(None)
        actions = plan_proxies(spec(), state(exposures=(exposure(),)))

        run = await executor.execute(Plan(actions=actions), zone="zone-east-1")

        assert run.status.value != "succeeded"


class TestARenewalMakesAProxyStale:
    """A proxy fetched its material once at first boot and has no way
    to be told about a replacement. So renewing a certificate has to
    replace the VM, which is downtime — the honest cost of the
    one-time-token design, and better than a proxy quietly serving an
    expired certificate.
    """

    def exposed(self, *, serial: int = 1) -> Any:
        return exposure(
            custom_domain="ai.acme.example",
            tls=TlsSource.UPLOADED,
            certificate_id="c-1",
            proxy_certificate_serial=serial,
        )

    def plan(self, *, built_with: int, current: int) -> list[Any]:
        name = proxy_name("acme", BASE)
        return plan_proxies(
            spec(),
            state(
                exposures=(self.exposed(serial=built_with),),
                proxies={name: "vm-1"},
                certificate_serials={"c-1": current},
            ),
        )

    def test_a_current_proxy_is_left_alone(self) -> None:
        assert self.plan(built_with=1, current=1) == []

    def test_a_superseded_proxy_is_replaced(self) -> None:
        actions = self.plan(built_with=1, current=2)

        assert [a.kind for a in actions] == [ActionKind.DELETE, ActionKind.CREATE]

    def test_the_replacement_says_why(self) -> None:
        actions = self.plan(built_with=1, current=2)

        assert "certificate" in actions[0].reason.lower()

    def test_the_delete_comes_first(self) -> None:
        """CloudStack will not have two VMs of the same name."""
        actions = self.plan(built_with=1, current=2)

        assert actions[0].kind is ActionKind.DELETE
        assert actions[1].depends_on == (actions[0].key,)

    def test_an_unknown_serial_does_not_churn(self) -> None:
        """A certificate the engine cannot see should not make the
        planner replace a working proxy on every run."""
        name = proxy_name("acme", BASE)
        actions = plan_proxies(
            spec(),
            state(
                exposures=(self.exposed(serial=1),),
                proxies={name: "vm-1"},
                certificate_serials={},
            ),
        )

        assert actions == []

    def test_a_plain_endpoint_is_never_stale(self) -> None:
        """No certificate, nothing to supersede."""
        name = proxy_name("acme", BASE)
        actions = plan_proxies(
            spec(),
            state(
                exposures=(exposure(),),
                proxies={name: "vm-1"},
                certificate_serials={"c-1": 9},
            ),
        )

        assert actions == []
