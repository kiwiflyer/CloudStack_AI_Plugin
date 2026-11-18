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
"""Serving and generation profiles, and what binds them (SPEC.md section 6).

Section 9 declares nine commands for these and the engine served none
of them: the precedence logic in `aisvc/profiles/` could only ever read
a profile out of `platform.yaml`, so a profile created through the API
had nowhere to go. CloudStack accepted all nine and the engine answered
404.

The point of storing them is that they must reach the same
`resolve()` the gateway enforces with, which is why the test that
matters most here is the one asserting a stored profile changes what
`/v1/profiles/effective` reports.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from tests.unit.conftest import ADMIN, OTHER_TENANT, TENANT

#: A domain admin over /Customers/Acme, who may bind at their own
#: domain but not at the platform.
DOMAIN_ADMIN = {
    "X-Aisvc-User-Id": "da-1",
    "X-Aisvc-Account": "acme-admin",
    "X-Aisvc-Domain": "/Customers/Acme",
    "X-Aisvc-Role": "DomainAdmin",
}

GENERATION_PARAMS = {
    "temperature": {"default": 0.7, "min": 0.0, "max": 1.0, "policy": "clamp"},
}
SERVING_PARAMS = {"max_model_len": 4096, "gpu_memory_utilization": 0.9}


def finished(client: TestClient, response: Any, headers: dict[str, str] = ADMIN) -> Any:
    """The result of the job an async route started."""
    assert response.status_code == 202, response.text
    job = client.get(f"/v1/jobs/{response.json()['jobid']}", headers=headers).json()
    assert job["jobstatus"] == 1, job
    return job["jobresult"]


def _named(body: Any, parameter: str) -> Any:
    """One parameter out of the list `getAiEffectiveParameters` returns.

    A list and not a mapping: section 9 declares this a list command,
    so the plugin reads `parameters` as an array, and the UI asks each
    entry for its own `name`.
    """
    for entry in body.get("parameters", []):
        if entry["name"] == parameter:
            return entry
    return {}


class TestServingProfiles:
    """Engine arguments. Root admin's alone — a tenant setting
    `gpu_memory_utilization` could take down a host they share."""

    def create(self, client: TestClient, headers: dict[str, str] = ADMIN, **kw: Any) -> Any:
        body = {"name": "fast", "params": json.dumps(SERVING_PARAMS)}
        body.update(kw)
        return client.post("/v1/profiles/serving", json=body, headers=headers)

    def test_a_serving_profile_is_created(self, client: TestClient) -> None:
        body = finished(client, self.create(client))

        assert body["id"]
        assert body["name"] == "fast"

    def test_it_is_listed_afterwards(self, client: TestClient) -> None:
        self.create(client)

        body = client.get("/v1/profiles/serving", headers=ADMIN).json()

        assert [p["name"] for p in body["profiles"]] == ["fast"]

    def test_its_parameters_come_back(self, client: TestClient) -> None:
        self.create(client)

        body = client.get("/v1/profiles/serving", headers=ADMIN).json()

        assert body["profiles"][0]["params"]["max_model_len"] == 4096

    def test_a_tenant_cannot_create_one(self, client: TestClient) -> None:
        assert self.create(client, headers=TENANT).status_code == 403

    def test_a_tenant_cannot_list_them(self, client: TestClient) -> None:
        assert client.get("/v1/profiles/serving", headers=TENANT).status_code == 403

    def test_it_can_be_updated(self, client: TestClient) -> None:
        profile_id = finished(client, self.create(client))["id"]

        changed = client.patch(
            f"/v1/profiles/serving/{profile_id}",
            json={"params": json.dumps({"max_model_len": 8192})},
            headers=ADMIN,
        )

        assert finished(client, changed)["params"]["max_model_len"] == 8192

    def test_it_can_be_deleted(self, client: TestClient) -> None:
        profile_id = finished(client, self.create(client))["id"]

        removed = client.delete(f"/v1/profiles/serving/{profile_id}", headers=ADMIN)

        assert finished(client, removed)["deleted"] is True
        assert client.get("/v1/profiles/serving", headers=ADMIN).json()["count"] == 0

    def test_an_unknown_id_is_a_404(self, client: TestClient) -> None:
        assert client.delete("/v1/profiles/serving/nope", headers=ADMIN).status_code == 404

    def test_a_duplicate_name_is_refused(self, client: TestClient) -> None:
        # Assignments bind by name, so two profiles sharing one would
        # make the chain ambiguous and resolution arbitrary.
        self.create(client)

        assert self.create(client).status_code == 409


class TestGenerationProfiles:
    """Tunable generation parameters. Any role may author one."""

    def create(self, client: TestClient, headers: dict[str, str] = TENANT, **kw: Any) -> Any:
        body = {"name": "careful", "params": json.dumps(GENERATION_PARAMS)}
        body.update(kw)
        return client.post("/v1/profiles/generation", json=body, headers=headers)

    def test_a_tenant_can_create_one(self, client: TestClient) -> None:
        response = self.create(client)

        assert response.status_code == 200, response.text
        assert response.json()["name"] == "careful"

    def test_it_is_listed_for_its_owner(self, client: TestClient) -> None:
        self.create(client)

        body = client.get("/v1/profiles/generation", headers=TENANT).json()

        assert [p["name"] for p in body["profiles"]] == ["careful"]

    def test_another_tenant_does_not_see_it(self, client: TestClient) -> None:
        self.create(client, headers=TENANT)

        body = client.get("/v1/profiles/generation", headers=OTHER_TENANT).json()

        assert body["count"] == 0

    def test_root_admin_sees_every_profile(self, client: TestClient) -> None:
        self.create(client, headers=TENANT)

        body = client.get("/v1/profiles/generation", headers=ADMIN).json()

        assert body["count"] == 1

    def test_a_malformed_policy_is_refused(self, client: TestClient) -> None:
        # The values are the spec's `GenerationValue`, so a policy the
        # resolver has no branch for must not reach storage: it would
        # be read back and silently ignored at every request.
        response = self.create(client, params=json.dumps({"temperature": {"policy": "whatever"}}))

        assert response.status_code == 400

    def test_params_that_are_not_json_are_refused(self, client: TestClient) -> None:
        assert self.create(client, params="not json").status_code == 400

    def test_it_can_be_updated_by_its_owner(self, client: TestClient) -> None:
        profile_id = self.create(client).json()["id"]

        changed = client.patch(
            f"/v1/profiles/generation/{profile_id}",
            json={"params": json.dumps({"temperature": {"default": 0.2}})},
            headers=TENANT,
        )

        assert changed.status_code == 200
        assert changed.json()["params"]["temperature"]["default"] == 0.2

    def test_another_tenant_cannot_update_it(self, client: TestClient) -> None:
        profile_id = self.create(client, headers=TENANT).json()["id"]

        changed = client.patch(
            f"/v1/profiles/generation/{profile_id}",
            json={"params": json.dumps({"temperature": {"default": 1.9}})},
            headers=OTHER_TENANT,
        )

        assert changed.status_code == 404

    def test_another_tenant_cannot_delete_it(self, client: TestClient) -> None:
        profile_id = self.create(client, headers=TENANT).json()["id"]

        assert (
            client.delete(f"/v1/profiles/generation/{profile_id}", headers=OTHER_TENANT).status_code
            == 404
        )

    def test_its_owner_can_delete_it(self, client: TestClient) -> None:
        profile_id = self.create(client).json()["id"]

        removed = client.delete(f"/v1/profiles/generation/{profile_id}", headers=TENANT)

        assert removed.status_code == 200


class TestAssignments:
    """Where a profile binds. The authority rules are section 6's, the
    same ones guardrail profiles are written under."""

    def a_profile(self, client: TestClient, headers: dict[str, str] = TENANT) -> str:
        response = client.post(
            "/v1/profiles/generation",
            json={"name": "careful", "params": json.dumps(GENERATION_PARAMS)},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return str(response.json()["name"])

    def assign(self, client: TestClient, headers: dict[str, str] = TENANT, **kw: Any) -> Any:
        body = {"scope": "account", "target": "acme", "profile": "careful"}
        body.update(kw)
        return client.post("/v1/profiles/assignments", json=body, headers=headers)

    def test_a_tenant_binds_a_profile_to_their_own_account(self, client: TestClient) -> None:
        self.a_profile(client)

        body = finished(client, self.assign(client), TENANT)

        assert body["scope"] == "account"
        assert body["generation_profile"] == "careful"

    def test_a_tenant_cannot_bind_at_the_platform(self, client: TestClient) -> None:
        self.a_profile(client)

        assert self.assign(client, scope="platform", target="").status_code == 403

    def test_a_tenant_cannot_bind_over_another_account(self, client: TestClient) -> None:
        self.a_profile(client)

        assert self.assign(client, target="someone-else").status_code == 403

    def test_a_domain_admin_binds_inside_their_domain(self, client: TestClient) -> None:
        self.a_profile(client, headers=DOMAIN_ADMIN)

        response = self.assign(
            client,
            headers=DOMAIN_ADMIN,
            scope="domain",
            target="/Customers/Acme/Sub",
        )

        assert finished(client, response, DOMAIN_ADMIN)["target"] == "/Customers/Acme/Sub"

    def test_binding_an_unknown_profile_is_refused(self, client: TestClient) -> None:
        # Otherwise the assignment resolves to nothing and the operator
        # sees a binding that does not bind.
        assert self.assign(client, profile="ghost").status_code == 404

    def test_an_unknown_scope_is_refused(self, client: TestClient) -> None:
        self.a_profile(client)

        assert self.assign(client, scope="galaxy").status_code == 400


class TestAStoredProfileReachesResolution:
    """The whole point. A profile nobody resolves against is a row.

    `/v1/profiles/effective` and the gateway both call
    `aisvc.profiles.resolve()`, so a stored profile that does not reach
    it would show in the console and change nothing about what a
    request actually does — which is the failure mode a tenant cannot
    see and cannot report.
    """

    def test_a_bound_profile_changes_the_effective_parameters(self, client: TestClient) -> None:
        before = client.get(
            "/v1/profiles/effective",
            params={"model": "llama-3.3-70b-instruct"},
            headers=TENANT,
        ).json()

        client.post(
            "/v1/profiles/generation",
            json={
                "name": "careful",
                "params": json.dumps({"temperature": {"default": 0.1, "policy": "locked"}}),
            },
            headers=TENANT,
        )
        assignment = client.post(
            "/v1/profiles/assignments",
            json={"scope": "account", "target": "acme", "profile": "careful"},
            headers=TENANT,
        )
        finished(client, assignment, TENANT)

        after = client.get(
            "/v1/profiles/effective",
            params={"model": "llama-3.3-70b-instruct"},
            headers=TENANT,
        ).json()

        assert _named(before, "temperature").get("value") != 0.1
        assert _named(after, "temperature")["value"] == 0.1

    def test_the_source_level_is_reported(self, client: TestClient) -> None:
        client.post(
            "/v1/profiles/generation",
            json={"name": "careful", "params": json.dumps(GENERATION_PARAMS)},
            headers=TENANT,
        )
        finished(
            client,
            client.post(
                "/v1/profiles/assignments",
                json={"scope": "account", "target": "acme", "profile": "careful"},
                headers=TENANT,
            ),
            TENANT,
        )

        body = client.get(
            "/v1/profiles/effective",
            params={"model": "llama-3.3-70b-instruct"},
            headers=TENANT,
        ).json()

        assert _named(body, "temperature")["source"] == "account"
