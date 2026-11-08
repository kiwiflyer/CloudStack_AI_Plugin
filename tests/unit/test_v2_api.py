"""The v2 engine surface (SPEC.md sections 8 and 10).

Every v2 feature had decision logic nothing could reach: no endpoint,
so no CloudStack command, so nothing a tenant or operator could call.
These are the endpoints, and they are where the decision modules get
used rather than merely imported.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.conftest import ADMIN, OTHER_TENANT, TENANT

ADAPTER = {
    "name": "legal-tone",
    "base_model": "llama-3.3-70b-instruct",
    "rank": 16,
    "size_bytes": 1024,
    "files": ["adapter_model.safetensors", "adapter_config.json"],
    "uri": "s3://adapters/legal-tone/",
}
BATCH_LINE = (
    '{"custom_id":"a","method":"POST","url":"/v1/chat/completions",'
    '"body":{"model":"llama-3.3-70b-instruct","messages":[]}}'
)


class TestAdapters:
    def upload(self, client: TestClient, headers: dict[str, str] = TENANT, **kw: Any) -> Any:
        body = dict(ADAPTER)
        body.update(kw)
        return client.post("/v1/adapters", json=body, headers=headers)

    def test_an_adapter_is_accepted(self, client: TestClient) -> None:
        response = self.upload(client)

        assert response.status_code == 200
        assert response.json()["id"]

    def test_it_is_listed_afterwards(self, client: TestClient) -> None:
        self.upload(client)

        body = client.get("/v1/adapters", headers=TENANT).json()

        assert [a["name"] for a in body["adapters"]] == ["legal-tone"]

    def test_the_gateway_name_is_reported(self, client: TestClient) -> None:
        """A tenant needs the `<base>:<adapter>` form to call it."""
        body = self.upload(client).json()

        assert body["servedmodelname"] == "llama-3.3-70b-instruct:legal-tone"

    def test_a_rank_over_the_limit_is_refused_with_a_reason(self, client: TestClient) -> None:
        response = self.upload(client, rank=4096)

        assert response.status_code == 400
        assert "rank" in response.json()["detail"].lower()

    def test_pickle_weights_are_refused(self, client: TestClient) -> None:
        response = self.upload(client, files=["adapter_model.bin", "adapter_config.json"])

        assert response.status_code == 400

    def test_an_unknown_base_is_refused(self, client: TestClient) -> None:
        response = self.upload(client, base_model="ghost")

        assert response.status_code == 400

    def test_another_tenant_does_not_see_it(self, client: TestClient) -> None:
        self.upload(client, headers=TENANT)

        body = client.get("/v1/adapters", headers=OTHER_TENANT).json()

        assert body["count"] == 0

    def test_an_adapter_can_be_deleted(self, client: TestClient) -> None:
        adapter_id = self.upload(client).json()["id"]

        assert client.delete(f"/v1/adapters/{adapter_id}", headers=TENANT).status_code == 200

    def test_another_tenant_cannot_delete_it(self, client: TestClient) -> None:
        adapter_id = self.upload(client, headers=TENANT).json()["id"]

        response = client.delete(f"/v1/adapters/{adapter_id}", headers=OTHER_TENANT)

        assert response.status_code == 404

    def test_the_account_limit_is_enforced(self, client: TestClient) -> None:
        """SPEC.md section 8: count limits per account."""
        for index in range(20):
            self.upload(client, name=f"a{index}")

        assert self.upload(client, name="one-too-many").status_code == 400


class TestBatches:
    def create(self, client: TestClient, headers: dict[str, str] = TENANT, **kw: Any) -> Any:
        body: dict[str, Any] = {
            "model": "llama-3.3-70b-instruct",
            "input": BATCH_LINE,
        }
        body.update(kw)
        return client.post("/v1/batches", json=body, headers=headers)

    def test_a_batch_is_accepted(self, client: TestClient) -> None:
        response = self.create(client)

        assert response.status_code == 200
        assert response.json()["status"] == "validating"

    def test_the_request_count_is_reported(self, client: TestClient) -> None:
        body = self.create(
            client, input="\n".join([BATCH_LINE, BATCH_LINE.replace('"a"', '"b"')])
        ).json()

        assert body["requestcount"] == 2

    def test_a_malformed_file_is_refused_with_the_line(self, client: TestClient) -> None:
        response = self.create(client, input=f"{BATCH_LINE}\nnot json")

        assert response.status_code == 400
        assert "line 2" in response.json()["detail"]

    def test_batches_are_listed(self, client: TestClient) -> None:
        self.create(client)

        assert client.get("/v1/batches", headers=TENANT).json()["count"] == 1

    def test_another_tenant_sees_none(self, client: TestClient) -> None:
        self.create(client, headers=TENANT)

        assert client.get("/v1/batches", headers=OTHER_TENANT).json()["count"] == 0

    def test_a_batch_can_be_cancelled(self, client: TestClient) -> None:
        batch_id = self.create(client).json()["id"]

        response = client.post(f"/v1/batches/{batch_id}/cancel", headers=TENANT)

        assert response.json()["status"] == "cancelled"

    def test_cancelling_a_finished_batch_is_refused(self, client: TestClient) -> None:
        batch_id = self.create(client).json()["id"]
        client.post(f"/v1/batches/{batch_id}/cancel", headers=TENANT)

        response = client.post(f"/v1/batches/{batch_id}/cancel", headers=TENANT)

        assert response.status_code == 409


class TestGuardrailProfiles:
    def create(self, client: TestClient, headers: dict[str, str] = ADMIN, **kw: Any) -> Any:
        body: dict[str, Any] = {
            "name": "school-safe",
            "scope": "domain",
            "target": "/Customers/Acme",
            "checks": {"pii": {"action": "block", "policy": "locked"}},
        }
        body.update(kw)
        return client.post("/v1/profiles/guardrail", json=body, headers=headers)

    def test_a_profile_is_created(self, client: TestClient) -> None:
        assert self.create(client).status_code == 200

    def test_profiles_are_listed(self, client: TestClient) -> None:
        self.create(client)

        assert client.get("/v1/profiles/guardrail", headers=ADMIN).json()["count"] == 1

    def test_a_tenant_cannot_create_one(self, client: TestClient) -> None:
        """A guardrail an account could remove is not a guardrail."""
        assert self.create(client, headers=TENANT).status_code == 403

    def test_a_preset_can_be_used(self, client: TestClient) -> None:
        body = self.create(client, preset="education-safe", checks=None).json()

        assert body["checks"]["topics"]["action"] == "block"

    def test_an_unknown_preset_is_refused(self, client: TestClient) -> None:
        assert self.create(client, preset="nonsense", checks=None).status_code == 404

    def test_a_profile_can_be_updated(self, client: TestClient) -> None:
        profile_id = self.create(client).json()["id"]

        response = client.patch(
            f"/v1/profiles/guardrail/{profile_id}",
            json={"checks": {"pii": {"action": "flag", "policy": "default"}}},
            headers=ADMIN,
        )

        assert response.json()["checks"]["pii"]["action"] == "flag"

    def test_a_profile_can_be_deleted(self, client: TestClient) -> None:
        profile_id = self.create(client).json()["id"]

        assert (
            client.delete(f"/v1/profiles/guardrail/{profile_id}", headers=ADMIN).status_code == 200
        )


class TestEvaluations:
    def run(self, client: TestClient, headers: dict[str, str] = ADMIN, **kw: Any) -> Any:
        body: dict[str, Any] = {
            "revision": "0f3f4a6c",
            "suites": [{"name": "mmlu", "score": 70.0, "baseline": 70.0}],
            "performance": [
                {"gpu_class": "h100-8x", "ttft_p95_ms": 320, "tokens_per_second": 1800}
            ],
        }
        body.update(kw)
        return client.post(
            "/v1/models/llama-3.3-70b-instruct/evaluations", json=body, headers=headers
        )

    def test_a_passing_evaluation_is_recorded(self, client: TestClient) -> None:
        body = self.run(client).json()

        assert body["passed"] is True

    def test_a_regression_blocks(self, client: TestClient) -> None:
        body = self.run(client, suites=[{"name": "gsm8k", "score": 60.0, "baseline": 70.0}]).json()

        assert body["passed"] is False
        assert "gsm8k" in body["summary"]

    def test_evaluations_are_listed_for_the_model(self, client: TestClient) -> None:
        self.run(client)

        body = client.get("/v1/models/llama-3.3-70b-instruct/evaluations", headers=ADMIN).json()
        assert body["count"] == 1

    def test_a_tenant_cannot_run_one(self, client: TestClient) -> None:
        assert self.run(client, headers=TENANT).status_code == 403

    def test_an_unknown_model_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/v1/models/ghost/evaluations",
            json={"revision": "x", "suites": []},
            headers=ADMIN,
        )

        assert response.status_code == 404


class TestPublishIsGated:
    """SPEC.md section 8: publishing is blocked by a regression."""

    def evaluate(self, client: TestClient, score: float) -> None:
        client.post(
            "/v1/models/llama-3.3-70b-instruct/evaluations",
            json={
                "revision": "0f3f4a6c",
                "suites": [{"name": "mmlu", "score": score, "baseline": 70.0}],
            },
            headers=ADMIN,
        )

    def publish(self, client: TestClient, **kw: Any) -> Any:
        return client.post(
            "/v1/models/llama-3.3-70b-instruct/publish",
            json={"price_tier": "standard", **kw},
            headers=ADMIN,
        )

    def test_publishing_after_a_passing_evaluation_works(self, client: TestClient) -> None:
        self.evaluate(client, 70.0)

        assert self.publish(client).status_code == 200

    def test_publishing_after_a_regression_is_blocked(self, client: TestClient) -> None:
        self.evaluate(client, 50.0)

        response = self.publish(client)

        assert response.status_code == 409
        assert "mmlu" in response.json()["detail"]

    def test_publishing_without_any_evaluation_is_blocked(self, client: TestClient) -> None:
        """An unevaluated model is exactly what the gate exists for."""
        response = self.publish(client)

        assert response.status_code == 409

    def test_a_root_admin_may_override_with_a_reason(self, client: TestClient) -> None:
        self.evaluate(client, 50.0)

        response = self.publish(client, override_reason="known contamination")

        assert response.status_code == 200
        assert response.json()["overridden"] is True

    def test_an_override_without_a_reason_does_not_count(self, client: TestClient) -> None:
        self.evaluate(client, 50.0)

        assert self.publish(client, override_reason="   ").status_code == 409


class TestPrivateEndpoints:
    def create(self, client: TestClient, headers: dict[str, str] = TENANT, **kw: Any) -> Any:
        body: dict[str, Any] = {
            "deployment": "llama-3.3-70b-instruct",
            "network_id": "net-1",
            "public_enabled": False,
        }
        body.update(kw)
        return client.post("/v1/endpoints/private", json=body, headers=headers)

    def test_an_exposure_is_created(self, client: TestClient) -> None:
        assert self.create(client).status_code == 200

    def test_it_describes_how_it_is_reachable(self, client: TestClient) -> None:
        body = self.create(client).json()

        assert "private" in body["exposure"]

    def test_an_unreachable_exposure_is_refused(self, client: TestClient) -> None:
        response = self.create(client, network_id="", public_enabled=False)

        assert response.status_code == 400
        assert "reach" in response.json()["detail"].lower()

    def test_a_network_the_tenant_does_not_own_is_refused(self, client: TestClient) -> None:
        response = self.create(client, network_id="net-someone-else")

        assert response.status_code == 400

    def test_acme_on_a_private_only_endpoint_is_refused(self, client: TestClient) -> None:
        response = self.create(client, custom_domain="ai.acme.example", tls="acme")

        assert response.status_code == 400

    def test_an_exposure_can_be_removed(self, client: TestClient) -> None:
        exposure_id = self.create(client).json()["id"]

        assert (
            client.delete(f"/v1/endpoints/private/{exposure_id}", headers=TENANT).status_code == 200
        )


class TestBatchInputAndResults:
    """A batch is only useful if its input is kept and its results come
    back. The JSONL goes to the registry rather than a column: these run
    to hundreds of megabytes."""

    def create(self, client: TestClient, **kw: Any) -> Any:
        body: dict[str, Any] = {"model": "llama-3.3-70b-instruct", "input": BATCH_LINE}
        body.update(kw)
        return client.post("/v1/batches", json=body, headers=TENANT)

    def test_the_input_is_stored(self, client: TestClient) -> None:
        body = self.create(client).json()

        assert body["inputuri"]

    def test_results_are_not_ready_before_it_runs(self, client: TestClient) -> None:
        batch_id = self.create(client).json()["id"]

        response = client.get(f"/v1/batches/{batch_id}/results", headers=TENANT)

        assert response.status_code == 409

    def test_results_come_back_once_it_has(self, client: TestClient) -> None:
        from aisvc.batch.runner import output_key

        batch_id = self.create(client).json()["id"]
        services = cast(FastAPI, client.app).state.services
        session = services.session_factory()
        from aisvc.db.tier2 import BatchStore

        record = BatchStore(session).get(batch_id)
        assert record is not None
        services.registry.store.put(output_key(record), b'{"custom_id":"a"}')
        record.state = "completed"
        record.output_uri = output_key(record)
        session.commit()

        response = client.get(f"/v1/batches/{batch_id}/results", headers=TENANT)

        assert response.status_code == 200
        assert "custom_id" in response.text

    def test_another_tenant_cannot_fetch_them(self, client: TestClient) -> None:
        batch_id = self.create(client).json()["id"]

        response = client.get(f"/v1/batches/{batch_id}/results", headers=OTHER_TENANT)

        assert response.status_code == 404

    def test_results_are_served_as_jsonl(self, client: TestClient) -> None:
        from aisvc.batch.runner import output_key
        from aisvc.db.tier2 import BatchStore

        batch_id = self.create(client).json()["id"]
        services = cast(FastAPI, client.app).state.services
        session = services.session_factory()
        record = BatchStore(session).get(batch_id)
        assert record is not None
        services.registry.store.put(output_key(record), b'{"custom_id":"a"}')
        record.state = "completed"
        record.output_uri = output_key(record)
        session.commit()

        response = client.get(f"/v1/batches/{batch_id}/results", headers=TENANT)

        assert response.headers["content-type"].startswith("application/x-ndjson")


class TestNetworkOwnershipComesFromCloudStack:
    """The engine must not guess at a tenant's VPC tiers.

    Which networks an account holds is CloudStack's to answer; a guess
    here would let one tenant name another's network and get an address
    inside it.
    """

    def client_with(self, networks: Any) -> Any:
        from fastapi.testclient import TestClient

        from aisvc.api.app import create_app
        from aisvc.registry import InMemoryObjectStore, ModelRegistry
        from tests.unit.conftest import EngineServices, memory_sessions, spec_with

        services = EngineServices(
            spec=spec_with(),
            registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
            session_factory=memory_sessions(),
            networks=networks,
        )
        return TestClient(create_app(services))

    def attach(self, client: Any, network_id: str = "net-1") -> Any:
        return client.post(
            "/v1/endpoints/private",
            json={
                "deployment": "llama-3.3-70b-instruct",
                "network_id": network_id,
                "public_enabled": False,
            },
            headers=TENANT,
        )

    def test_a_network_the_account_holds_is_accepted(self) -> None:
        assert self.attach(self.client_with(("net-1",))).status_code == 200

    def test_one_it_does_not_hold_is_refused(self) -> None:
        assert self.attach(self.client_with(("net-9",))).status_code == 400

    def test_an_account_with_no_networks_can_attach_to_none(self) -> None:
        assert self.attach(self.client_with(())).status_code == 400

    def test_the_refusal_does_not_name_the_others(self) -> None:
        response = self.attach(self.client_with(("net-7", "net-8")))

        assert "net-7" not in response.text


class TestCertificates:
    """Uploading and serving TLS material (SPEC.md section 8).

    The private key is the most sensitive thing a tenant gives us. It
    goes out exactly once, to the proxy that needs it, and never in a
    list or an error.
    """

    def pair(self, domain: str = "ai.acme.example") -> tuple[str, str]:
        from tests.unit.test_certificates import make_cert

        return make_cert(domains=(domain,))

    def upload(self, client: TestClient, headers: Any = TENANT, **kw: Any) -> Any:
        cert, key = self.pair()
        body: dict[str, Any] = {
            "custom_domain": "ai.acme.example",
            "certificate": cert,
            "private_key": key,
        }
        body.update(kw)
        return client.post("/v1/certificates", json=body, headers=headers)

    def test_a_valid_pair_is_accepted(self, client: TestClient) -> None:
        response = self.upload(client)

        assert response.status_code == 200
        assert response.json()["id"]

    def test_the_response_carries_no_key(self, client: TestClient) -> None:
        response = self.upload(client)

        assert "PRIVATE KEY" not in response.text

    def test_the_fingerprint_and_expiry_come_back(self, client: TestClient) -> None:
        """Enough to tell two uploads apart without the material."""
        body = self.upload(client).json()

        assert body["fingerprint"]
        assert body["expires"]

    def test_a_mismatched_key_is_refused(self, client: TestClient) -> None:
        _, other_key = self.pair()
        cert, _ = self.pair()

        response = self.upload(client, certificate=cert, private_key=other_key)

        assert response.status_code == 400
        assert "PRIVATE KEY" not in response.text

    def test_a_certificate_for_another_domain_is_refused(self, client: TestClient) -> None:
        cert, key = self.pair("elsewhere.example")

        response = self.upload(client, certificate=cert, private_key=key)

        assert response.status_code == 400

    def test_listing_never_includes_material(self, client: TestClient) -> None:
        self.upload(client)

        body = client.get("/v1/certificates", headers=TENANT).text

        assert "PRIVATE KEY" not in body
        assert "BEGIN CERTIFICATE" not in body

    def test_a_tenant_sees_only_its_own(self, client: TestClient) -> None:
        self.upload(client, headers=TENANT)

        assert client.get("/v1/certificates", headers=OTHER_TENANT).json()["count"] == 0

    def test_the_proxy_can_fetch_the_material(self, client: TestClient) -> None:
        cert_id = self.upload(client).json()["id"]

        response = client.get(f"/v1/certificates/{cert_id}/material", headers=ADMIN)

        assert response.status_code == 200
        assert "BEGIN CERTIFICATE" in response.json()["certificate"]
        assert "PRIVATE KEY" in response.json()["privatekey"]

    def test_a_tenant_cannot_fetch_material(self, client: TestClient) -> None:
        """Not even their own: it leaves the platform once, to the
        proxy, over mTLS. A tenant already has what they uploaded."""
        cert_id = self.upload(client).json()["id"]

        response = client.get(f"/v1/certificates/{cert_id}/material", headers=TENANT)

        assert response.status_code == 403

    def test_an_unknown_certificate_is_a_404(self, client: TestClient) -> None:
        assert client.get("/v1/certificates/nope/material", headers=ADMIN).status_code == 404

    def test_a_certificate_can_be_deleted(self, client: TestClient) -> None:
        cert_id = self.upload(client).json()["id"]

        assert client.delete(f"/v1/certificates/{cert_id}", headers=TENANT).status_code == 200

    def test_another_tenant_cannot_delete_it(self, client: TestClient) -> None:
        cert_id = self.upload(client, headers=TENANT).json()["id"]

        assert client.delete(f"/v1/certificates/{cert_id}", headers=OTHER_TENANT).status_code == 404

    def test_an_exposure_can_name_an_uploaded_certificate(self, client: TestClient) -> None:
        cert_id = self.upload(client).json()["id"]

        response = client.post(
            "/v1/endpoints/private",
            json={
                "deployment": "llama-3.3-70b-instruct",
                "network_id": "net-1",
                "public_enabled": False,
                "custom_domain": "ai.acme.example",
                "tls": "uploaded",
                "certificate_id": cert_id,
            },
            headers=TENANT,
        )

        assert response.status_code == 200

    def test_an_exposure_naming_an_unknown_certificate_is_refused(self, client: TestClient) -> None:
        """Otherwise the proxy boots, cannot fetch its certificate, and
        serves nothing — with the failure hours from the action."""
        response = client.post(
            "/v1/endpoints/private",
            json={
                "deployment": "llama-3.3-70b-instruct",
                "network_id": "net-1",
                "public_enabled": False,
                "custom_domain": "ai.acme.example",
                "tls": "uploaded",
                "certificate_id": "ghost",
            },
            headers=TENANT,
        )

        assert response.status_code == 400


class TestBootstrapRedemption:
    """The one endpoint with no caller context (SPEC.md section 8).

    A proxy has no identity at first boot — that is the whole reason
    the token exists — so this cannot ask for one. The token is the
    credential, and everything that makes that safe lives in
    `aisvc.certificates.bootstrap`.
    """

    def certificate(self, client: TestClient) -> str:
        from tests.unit.test_certificates import make_cert

        cert, key = make_cert(domains=("ai.acme.example",))
        certificate_id: str = client.post(
            "/v1/certificates",
            json={
                "custom_domain": "ai.acme.example",
                "certificate": cert,
                "private_key": key,
            },
            headers=TENANT,
        ).json()["id"]
        return certificate_id

    def token_for(self, client: TestClient, certificate_id: str) -> str:
        from aisvc.certificates.bootstrap import BootstrapTokens

        services = cast(FastAPI, client.app).state.services
        session = services.session_factory()
        minted = BootstrapTokens(session).issue(
            certificate_id=certificate_id, proxy="aisvc-gw-acme-m"
        )
        session.commit()
        return minted.plaintext

    def redeem(self, client: TestClient, token: str) -> Any:
        # Deliberately no X-Aisvc-* headers: the proxy has none to send.
        return client.post(
            "/v1/certificates/bootstrap",
            json={"token": token},
            headers={"Content-Type": "application/json"},
        )

    def test_a_proxy_with_no_identity_can_redeem(self, client: TestClient) -> None:
        token = self.token_for(client, self.certificate(client))

        response = self.redeem(client, token)

        assert response.status_code == 200

    def test_it_returns_the_material(self, client: TestClient) -> None:
        token = self.token_for(client, self.certificate(client))

        body = self.redeem(client, token).json()

        assert "BEGIN CERTIFICATE" in body["certificate"]
        assert "PRIVATE KEY" in body["privatekey"]

    def test_a_second_attempt_is_refused(self, client: TestClient) -> None:
        """The property the whole design rests on."""
        token = self.token_for(client, self.certificate(client))
        self.redeem(client, token)

        response = self.redeem(client, token)

        assert response.status_code == 403
        assert "already" in response.json()["detail"].lower()

    def test_a_forged_token_is_refused(self, client: TestClient) -> None:
        self.certificate(client)

        response = self.redeem(client, "aisvc-boot-deadbeef-nonsense")

        assert response.status_code == 403

    def test_the_refusal_does_not_echo_the_token(self, client: TestClient) -> None:
        response = self.redeem(client, "aisvc-boot-abc-supersecretvalue")

        assert "supersecretvalue" not in response.text

    def test_a_missing_token_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/v1/certificates/bootstrap", json={}, headers={"Content-Type": "application/json"}
        )

        assert response.status_code in (403, 422)

    def test_it_is_not_in_the_published_schema(self, client: TestClient) -> None:
        """Nothing a tenant or the plugin should ever call."""
        paths = client.get("/v1/openapi.json").json()["paths"]

        assert "/v1/certificates/bootstrap" not in paths

    def test_caller_context_is_still_required_everywhere_else(self, client: TestClient) -> None:
        """The exception has to be this one path and no other."""
        assert client.get("/v1/models").status_code == 401
