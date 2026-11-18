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
"""Deploying a catalogued model through the API (ADR-0008).

The three deployment commands answered 404 for a long time, and the
reason was not a missing endpoint. A spec model's zone comes from its
node pool and its replicas are a declared range, so there was nothing
for them to decide that `platform.yaml` had not already decided.

`placement` is what gives them something to say. A `spec` model runs
where the file says and these commands refuse it; an `api` model is
catalogued and runs nowhere until a placement row exists. The row
holds placement and nothing else — so the tests that matter here are
the ones proving a `spec` model is refused by name, that the zone is
checked rather than believed, and that bounds declared in the file
still bind a count sent through the API.
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi.testclient import TestClient

from aisvc.api.app import create_app
from aisvc.api.deps import Services as EngineServices
from aisvc.registry import ModelRegistry
from aisvc.spec import load_spec_from_string
from tests.unit.conftest import ADMIN, OTHER_TENANT, TENANT, memory_sessions
from tests.unit.test_spec import LICENSES, MINIMAL

MODEL = "llama-3.3-70b-instruct"


def api_placed_client(registry: ModelRegistry, **model_kw: Any) -> TestClient:
    """A client whose catalogue model hands placement to the API."""
    doc = yaml.safe_load(MINIMAL)
    doc["models"][0]["placement"] = "api"
    doc["models"][0].update(model_kw)
    spec = load_spec_from_string(yaml.safe_dump(doc), allowed_licenses=LICENSES)
    services = EngineServices(spec=spec, registry=registry, session_factory=memory_sessions())
    return TestClient(create_app(services))


def zone_of(client: TestClient) -> str:
    services = client.app.state.services  # type: ignore[attr-defined]
    pool = next(
        p
        for p in services.spec.document.node_pools
        if p.name == services.spec.document.models[0].node_pool
    )
    return str(pool.zone)


def finished(client: TestClient, response: Any, headers: dict[str, str] = ADMIN) -> Any:
    assert response.status_code == 202, response.text
    job = client.get(f"/v1/jobs/{response.json()['jobid']}", headers=headers).json()
    assert job["jobstatus"] == 1, job
    return job["jobresult"]


def failed(client: TestClient, response: Any, headers: dict[str, str] = ADMIN) -> Any:
    assert response.status_code == 202, response.text
    job = client.get(f"/v1/jobs/{response.json()['jobid']}", headers=headers).json()
    assert job["jobstatus"] == 2, job
    return job["jobresult"]


class TestASpecPlacedModelIsRefused:
    """The default, and the one that must not change behaviour.

    An install that has never heard of `placement` keeps deploying
    every catalogued model from the file, and these commands tell the
    caller why they cannot help rather than appearing to work.
    """

    def test_deploying_one_is_refused_by_name(self, client: TestClient) -> None:
        response = client.post(
            "/v1/deployments",
            json={"model": MODEL, "zone_id": "Sandbox", "replicas": 2},
            headers=ADMIN,
        )

        assert response.status_code == 409
        assert "placement" in response.json()["detail"]

    def test_the_refusal_names_the_value_to_set(self, client: TestClient) -> None:
        detail = client.post(
            "/v1/deployments",
            json={"model": MODEL, "zone_id": "Sandbox"},
            headers=ADMIN,
        ).json()["detail"]

        assert "api" in detail


class TestDeployingAnApiPlacedModel:
    def test_it_is_accepted(self, registry: ModelRegistry) -> None:
        with api_placed_client(registry) as client:
            body = finished(
                client,
                client.post(
                    "/v1/deployments",
                    json={"model": MODEL, "zone_id": zone_of(client), "replicas": 2},
                    headers=ADMIN,
                ),
            )

            assert body["model"] == MODEL
            assert body["replicas"] == 2
            assert body["id"]

    def test_an_unknown_model_is_a_404(self, registry: ModelRegistry) -> None:
        with api_placed_client(registry) as client:
            response = client.post(
                "/v1/deployments",
                json={"model": "ghost", "zone_id": zone_of(client)},
                headers=ADMIN,
            )

            assert response.status_code == 404

    def test_a_zone_the_model_does_not_run_in_is_refused(self, registry: ModelRegistry) -> None:
        # The node pool already determines the zone, so a `zoneid`
        # naming another one is a caller believing something untrue.
        # Storing it would make the row disagree with the spec.
        with api_placed_client(registry) as client:
            response = client.post(
                "/v1/deployments",
                json={"model": MODEL, "zone_id": "somewhere-else"},
                headers=ADMIN,
            )

            assert response.status_code == 400
            assert "zone" in response.json()["detail"]

    def test_the_cloudstack_zone_name_is_accepted(self, registry: ModelRegistry) -> None:
        """What the plugin actually sends.

        `deployAiModel` takes a `CommandType.UUID`, which CloudStack
        hands back as an internal numeric id, so the command resolves
        it and sends the zone's *name* — which `platform.yaml` records
        as `cloudstack_zone`, not as the spec's own zone name. Sending
        the raw id instead was a real 422 against a live server.
        """
        with api_placed_client(registry) as client:
            services = client.app.state.services  # type: ignore[attr-defined]
            cloudstack_name = services.spec.document.zones[0].cloudstack_zone

            body = finished(
                client,
                client.post(
                    "/v1/deployments",
                    json={"model": MODEL, "zone_id": cloudstack_name},
                    headers=ADMIN,
                ),
            )

            assert body["zone"] == zone_of(client)

    def test_an_internal_numeric_id_is_refused_as_a_zone(self, registry: ModelRegistry) -> None:
        # Not a 422 about types: a caller sending CloudStack's internal
        # id should be told it is not this model's zone.
        with api_placed_client(registry) as client:
            response = client.post(
                "/v1/deployments", json={"model": MODEL, "zone_id": 12}, headers=ADMIN
            )

            assert response.status_code == 400
            assert "zone" in response.json()["detail"]

    def test_replicas_outside_the_declared_bounds_are_refused(
        self, registry: ModelRegistry
    ) -> None:
        # The file still owns the bounds. Clamping silently would let a
        # tenant ask for 50 and be given 8 with no word about it.
        with api_placed_client(registry, replicas={"min": 1, "max": 4}) as client:
            response = client.post(
                "/v1/deployments",
                json={"model": MODEL, "zone_id": zone_of(client), "replicas": 50},
                headers=ADMIN,
            )

            assert response.status_code == 400
            assert "4" in response.json()["detail"]

    def test_the_replica_floor_is_used_when_none_is_given(self, registry: ModelRegistry) -> None:
        with api_placed_client(registry, replicas={"min": 2, "max": 6}) as client:
            body = finished(
                client,
                client.post(
                    "/v1/deployments",
                    json={"model": MODEL, "zone_id": zone_of(client)},
                    headers=ADMIN,
                ),
            )

            assert body["replicas"] == 2

    def test_deploying_twice_is_refused(self, registry: ModelRegistry) -> None:
        with api_placed_client(registry) as client:
            zone = zone_of(client)
            client.post(
                "/v1/deployments",
                json={"model": MODEL, "zone_id": zone},
                headers=ADMIN,
            )

            again = client.post(
                "/v1/deployments", json={"model": MODEL, "zone_id": zone}, headers=ADMIN
            )

            assert again.status_code == 409


class TestUpdatingAndRemovingAPlacement:
    def place(self, client: TestClient, headers: dict[str, str] = ADMIN) -> str:
        body = finished(
            client,
            client.post(
                "/v1/deployments",
                json={"model": MODEL, "zone_id": zone_of(client), "replicas": 2},
                headers=headers,
            ),
            headers,
        )
        return str(body["id"])

    def test_the_replica_count_can_be_changed(self, registry: ModelRegistry) -> None:
        with api_placed_client(registry, replicas={"min": 1, "max": 8}) as client:
            placement_id = self.place(client)

            body = finished(
                client,
                client.patch(
                    f"/v1/deployments/{placement_id}",
                    json={"replicas": 5},
                    headers=ADMIN,
                ),
            )

            assert body["replicas"] == 5

    def test_an_out_of_bounds_change_is_refused(self, registry: ModelRegistry) -> None:
        with api_placed_client(registry, replicas={"min": 1, "max": 4}) as client:
            placement_id = self.place(client)

            response = client.patch(
                f"/v1/deployments/{placement_id}", json={"replicas": 99}, headers=ADMIN
            )

            assert response.status_code == 400

    def test_it_can_be_removed(self, registry: ModelRegistry) -> None:
        with api_placed_client(registry) as client:
            placement_id = self.place(client)

            removed = client.delete(f"/v1/deployments/{placement_id}", headers=ADMIN)

            assert finished(client, removed)["deleted"] is True

    def test_removing_an_unknown_one_is_a_404(self, registry: ModelRegistry) -> None:
        with api_placed_client(registry) as client:
            assert client.delete("/v1/deployments/nope", headers=ADMIN).status_code == 404

    def test_another_tenant_cannot_remove_it(self, registry: ModelRegistry) -> None:
        with api_placed_client(registry) as client:
            placement_id = self.place(client, TENANT)

            response = client.delete(f"/v1/deployments/{placement_id}", headers=OTHER_TENANT)

            assert response.status_code == 404
