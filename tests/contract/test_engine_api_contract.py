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
"""The engine API against its own OpenAPI schema (SPEC.md sections 10, 12).

"The section 9 command map is the authoritative list of endpoints, and
the FastAPI-generated OpenAPI schema is the contract." These tests hold
the schema to the conventions the plugin relies on, so a change that
would break the Java client fails here rather than in integration.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from aisvc.api.app import API_VERSION, create_app
from aisvc.api.commands import COMMANDS
from aisvc.api.deps import Services
from aisvc.db import Base
from aisvc.registry import InMemoryObjectStore, ModelRegistry
from aisvc.spec import load_spec_from_string
from tests.unit.test_spec import LICENSES, MINIMAL

ADMIN = {
    "X-Aisvc-User-Id": "root",
    "X-Aisvc-Account": "admin",
    "X-Aisvc-Domain": "/",
    "X-Aisvc-Role": "Admin",
}

#: Endpoints SPEC.md section 10 lists as engine-only, plus the v1
#: commands whose modules exist today. Milestones 5 and 7-9 add the rest.
REQUIRED_PATHS = {
    "/v1/health",
    "/v1/ready",
    "/v1/jobs/{job_id}",
    "/v1/models",
    "/v1/models/mirror",
    "/v1/deployments",
    "/v1/endpoints",
    "/v1/capacity",
    "/v1/runs",
    "/v1/profiles/effective",
    "/v1/hf/search",
    "/v1/hf/check",
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    services = Services(
        spec=load_spec_from_string(MINIMAL, allowed_licenses=LICENSES),
        registry=ModelRegistry(InMemoryObjectStore(), bucket="models"),
        session_factory=sessionmaker(engine),
    )
    with TestClient(create_app(services)) as c:
        yield c


@pytest.fixture
def schema(client: TestClient) -> dict[str, Any]:
    response = client.get(f"/{API_VERSION}/openapi.json")
    response.raise_for_status()
    return response.json()  # type: ignore[no-any-return]


class TestSchemaShape:
    def test_the_schema_is_valid_openapi_3(self, schema: dict[str, Any]) -> None:
        assert schema["openapi"].startswith("3.")
        assert schema["info"]["title"]
        assert schema["info"]["version"]

    def test_every_path_is_under_the_version_prefix(self, schema: dict[str, Any]) -> None:
        """Breaking changes require a new version prefix, so everything is in one."""
        assert all(path.startswith(f"/{API_VERSION}/") for path in schema["paths"])

    def test_the_commands_with_modules_today_are_all_present(self, schema: dict[str, Any]) -> None:
        missing = REQUIRED_PATHS - set(schema["paths"])
        assert not missing, f"missing endpoints: {sorted(missing)}"

    def test_every_operation_has_a_summary(self, schema: dict[str, Any]) -> None:
        """The summary is what the generated Java client's method is named from."""
        missing = [
            f"{method.upper()} {path}"
            for path, operations in schema["paths"].items()
            for method, operation in operations.items()
            if not operation.get("summary")
        ]
        assert not missing, f"operations without a summary: {missing}"

    def test_every_operation_is_tagged(self, schema: dict[str, Any]) -> None:
        untagged = [
            f"{method.upper()} {path}"
            for path, operations in schema["paths"].items()
            for method, operation in operations.items()
            if not operation.get("tags")
        ]
        assert not untagged, f"untagged operations: {untagged}"


class TestCallerContextIsDocumented:
    @pytest.mark.parametrize("path", ["/v1/models", "/v1/deployments", "/v1/runs", "/v1/endpoints"])
    def test_the_caller_headers_are_declared(self, schema: dict[str, Any], path: str) -> None:
        """The plugin generates its client from this; the headers must be in it."""
        parameters = schema["paths"][path]["get"].get("parameters", [])
        headers = {p["name"] for p in parameters if p.get("in") == "header"}
        assert {
            "X-Aisvc-User-Id",
            "X-Aisvc-Account",
            "X-Aisvc-Domain",
            "X-Aisvc-Role",
        } <= headers

    def test_health_does_not_require_caller_context(self, schema: dict[str, Any]) -> None:
        """Load balancers probe it; they have no CloudStack session."""
        parameters = schema["paths"]["/v1/health"]["get"].get("parameters", [])
        assert parameters == []


class TestAsyncConvention:
    def test_async_commands_declare_202(self, schema: dict[str, Any]) -> None:
        """SPEC.md section 10: async calls return 202 with a job id."""
        responses = schema["paths"]["/v1/models/mirror"]["post"]["responses"]
        assert "202" in responses

    def test_async_commands_do_not_also_declare_200(self, schema: dict[str, Any]) -> None:
        responses = schema["paths"]["/v1/models/mirror"]["post"]["responses"]
        assert "200" not in responses


class TestLiveResponsesMatchTheSchema:
    @pytest.mark.parametrize(
        "path",
        ["/v1/models", "/v1/deployments", "/v1/endpoints", "/v1/runs"],
    )
    def test_documented_endpoints_answer(self, client: TestClient, path: str) -> None:
        response = client.get(path, headers=ADMIN)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")

    def test_health_and_ready_answer_without_headers(self, client: TestClient) -> None:
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/ready").status_code == 200


class TestErrorContract:
    @pytest.mark.parametrize(
        ("path", "headers", "expected"),
        [
            ("/v1/models", {}, 401),
            ("/v1/runs", {**ADMIN, "X-Aisvc-Role": "User"}, 403),
            ("/v1/jobs/does-not-exist", ADMIN, 404),
            ("/v1/profiles/effective?model=ghost", ADMIN, 404),
        ],
    )
    def test_errors_are_problem_json_with_the_right_status(
        self, client: TestClient, path: str, headers: dict[str, str], expected: int
    ) -> None:
        response = client.get(path, headers=headers)

        assert response.status_code == expected
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["status"] == expected
        assert body["type"].startswith("https://")
        assert body["detail"]

    def test_a_problem_type_is_stable_and_machine_readable(self, client: TestClient) -> None:
        body = client.get("/v1/models").json()
        assert body["type"].endswith("/missing_caller_context")


# --------------------------------------------------------------------------
#
# Section 9's map says every command maps onto a `/v1` route, and this
# module's own docstring promised the two were checked against each
# other. They were not: `test_async_commands_declare_202` asserted it
# of `/v1/models/mirror` and nothing else, and `REQUIRED_PATHS` lists
# twelve paths, every one of them a read bar that same mirror. So
# nineteen commands pointed at routes that answer the wrong status or
# do not exist, and every test stayed green.
#
# The engine is the authority on which operations are long-running, but
# SPEC section 9 is the authority on which commands are async, and the
# Java plugin is generated from that. A mismatch is not cosmetic: a
# command declared async refuses anything but a 202 — on purpose, since
# treating a 200 as acceptance would report work that no job ever did —
# so it cannot succeed, ever.

#: Commands SPEC names that the engine does not serve yet.
#:
#: Each one is a feature that is not built, not a defect in the route
#: table: the serving and generation profiles of section 6 have no
#: storage model at all, and deployments are created by the reconciler
#: rather than through the API. Listing them here keeps the gap
#: counted and the test honest — a name may leave this set, and
#: `test_the_unserved_list_has_not_rotted` fails if one is served
#: after all, but adding a name means admitting a new hole.
NOT_YET_SERVED = frozenset(
    {
        # the mirror-approval step. Blocked on an open decision, not on
        # a missing route: SPEC section 14 still asks whether reseller
        # domain admins mirror from Hugging Face directly or only
        # request it, and what `approve` does depends on the answer.
        "approveAiModel",
    }
)


def _shape(path: str) -> str:
    """A path with its parameter names removed.

    The command map spells a path parameter `{id}` and the engine
    spells it `{model_name}`; they are the same route.
    """
    return re.sub(r"\{[^}]*\}", "{}", path)


class TestEveryCommandMapsOntoARoute:
    """Section 9's map against the routes the engine actually serves."""

    @staticmethod
    def _routes(schema: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (verb.upper(), _shape(path)): operation
            for path, operations in schema["paths"].items()
            for verb, operation in operations.items()
        }

    def test_every_command_has_an_engine_route(self, schema: dict[str, Any]) -> None:
        routes = self._routes(schema)
        missing = sorted(
            command.name
            for command in COMMANDS
            if command.name not in NOT_YET_SERVED
            and (command.method, _shape(command.path)) not in routes
        )

        assert missing == [], (
            "these commands point at engine routes that do not exist: " + ", ".join(missing)
        )

    def test_the_unserved_list_has_not_rotted(self, schema: dict[str, Any]) -> None:
        routes = self._routes(schema)
        by_name = {command.name: command for command in COMMANDS}

        unknown = sorted(NOT_YET_SERVED - set(by_name))
        assert unknown == [], f"not commands at all: {unknown}"

        served = sorted(
            name
            for name in NOT_YET_SERVED
            if (by_name[name].method, _shape(by_name[name].path)) in routes
        )
        assert served == [], "these are served now and should leave NOT_YET_SERVED: " + ", ".join(
            served
        )

    def test_every_async_command_answers_202(self, schema: dict[str, Any]) -> None:
        # A command the plugin declares async refuses anything but a
        # 202, so a route that answers 200 makes it permanently
        # unusable rather than merely inconsistent.
        routes = self._routes(schema)
        wrong = []
        for command in COMMANDS:
            if not command.is_async or command.name in NOT_YET_SERVED:
                continue
            operation = routes.get((command.method, _shape(command.path)))
            if operation is None:
                continue
            if "202" not in operation["responses"]:
                wrong.append(f"{command.name} ({sorted(operation['responses'])})")

        assert wrong == [], "these are async commands, so the engine must answer 202: " + ", ".join(
            sorted(wrong)
        )

    def test_no_sync_command_answers_202(self, schema: dict[str, Any]) -> None:
        # The opposite mistake: a 202 reaching a sync command would be
        # read as the response body itself, with no job to poll.
        routes = self._routes(schema)
        wrong = []
        for command in COMMANDS:
            if command.is_async or command.name in NOT_YET_SERVED:
                continue
            operation = routes.get((command.method, _shape(command.path)))
            if operation is not None and "202" in operation["responses"]:
                wrong.append(command.name)

        assert wrong == [], "these are sync commands and must not answer 202: " + ", ".join(
            sorted(wrong)
        )


class TestAResponseIsMappedFromTheFieldsTheEngineSends:
    """What the plugin reads against what the engine returns.

    `createAiPlaygroundSession` answered a live CloudStack with a
    session carrying nothing but a model name: no key, no expiry, no
    id. The command worked, the plugin reported success, and the one
    thing a playground session exists to deliver — a short-lived key —
    was dropped in the mapping, because `AiPlaygroundSessionResponse`
    read `session_key`, `base_url` and `expires_at` while the engine
    sends `apikey`, `base_url` and `expires`.

    **The Java round-trip test could not have caught it.** Its fixture
    is generated from the same declared field names, so it round-trips
    whatever the generator believes and agrees with itself. Only the
    engine can say what the engine sends, which is why this test calls
    it.
    """

    def _declared(self, response_name: str) -> set[str]:
        """The engine field names the Java generator reads for a response."""
        import importlib.util
        import sys
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2] / "java-plugin" / "tools" / "generate_commands.py"
        )
        spec = importlib.util.spec_from_file_location("aisvc_java_generator_resp", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        response = next(r for r in module.RESPONSES if r.name == response_name)
        # `wire` is the engine's JSON field; `api` is the name
        # CloudStack answers with. Only the first can be wrong
        # against the engine.
        return {field.wire for field in response.fields}

    def test_the_playground_session_reads_what_the_engine_sends(self, client: TestClient) -> None:
        response = client.post(
            "/v1/playground/sessions",
            json={"model": "llama-3.3-70b-instruct"},
            headers=ADMIN,
        )
        assert response.status_code == 200, response.text
        sent = set(response.json())

        unread = self._declared("AiPlaygroundSessionResponse") - sent
        assert unread == set(), (
            "AiPlaygroundSessionResponse reads fields the engine never sends, "
            f"so they arrive empty: {sorted(unread)}. The engine sends "
            f"{sorted(sent)}."
        )

    def test_the_key_and_its_expiry_survive_the_mapping(self, client: TestClient) -> None:
        # The two that matter. A session without a key cannot be used,
        # and one without an expiry cannot be trusted to be short-lived.
        declared = self._declared("AiPlaygroundSessionResponse")

        assert "apikey" in declared, "the session key is what this command is for"
        assert "expires" in declared


class TestNoFieldTheEngineSendsIsSilentlyDropped:
    """The other direction, which nothing checked.

    `TestAResponseIsMappedFromTheFieldsTheEngineSends` asks whether the
    Java reads a field the engine never sends — those arrive empty. This
    asks the opposite: a field the engine sends that no Java response
    declares is **dropped in silence**, and CloudStack answers without it.

    Five v2 detail rows were exactly that. The adapters, certificates and
    private-endpoint screens each showed a `created` row, the batches
    screen `inputuri` and `outputuri`, and the guardrails screen `checks` —
    all six blank for ever, on every server, while the engine had been
    sending every one of them. Same family as the snippets list, where a
    response that could not hold one dropped it; these are scalars, which
    is worse, because nothing about the Java looks wrong.

    Read statically from the row builders rather than by calling the
    routes: the builders are the one place the engine names these fields,
    and seeding five kinds of record through the API to learn what is
    already in the source would prove less, not more.
    """

    #: engine row builder -> the Java response class CloudStack answers with
    ROWS: ClassVar[dict[str, str]] = {
        "_adapter_row": "AiAdapterResponse",
        "_batch_row": "AiBatchResponse",
        "_guardrail_row": "AiGuardrailProfileResponse",
        "_certificate_row": "AiCertificateResponse",
        "_capacity_row": "AiGpuCapacityResponse",
        "_exposure_row": "AiPrivateEndpointResponse",
        "_evaluation_row": "AiEvaluationResponse",
        "_budget_row": "AiBudgetResponse",
    }

    #: Fields the engine sends that the plugin deliberately does not carry.
    #: Each needs a reason, because the default is to carry it.
    NOT_CARRIED: ClassVar[set[tuple[str, str]]] = {
        # The resolved exposure sentence is rendered for the CLI; the UI
        # builds its own from the fields beside it.
        ("AiPrivateEndpointResponse", "exposure"),
        # Per-suite scores behind the verdict. The evaluations tab shows
        # whether publishing was gated and why in one sentence, which is
        # the question being asked there; nothing reads the breakdown, and
        # carrying a list no screen renders is surface without a consumer.
        # If a screen ever wants it, carry it — do not widen this list.
        ("AiEvaluationResponse", "regressions"),
        ("AiEvaluationResponse", "performance"),
    }

    def _engine_fields(self, builder: str) -> set[str]:
        import ast
        import inspect

        import aisvc.api.app as app

        tree = ast.parse(inspect.getsource(app))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == builder:
                keys: set[str] = set()
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Dict):
                        keys |= {
                            k.value
                            for k in inner.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)
                        }
                return keys
        raise AssertionError(f"{builder} is not in aisvc/api/app.py")

    def _java_fields(self, response_name: str) -> set[str]:
        import importlib.util
        import sys
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2] / "java-plugin" / "tools" / "generate_commands.py"
        )
        spec = importlib.util.spec_from_file_location("aisvc_java_generator_drop", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        response = next(r for r in module.RESPONSES if r.name == response_name)
        return {field.wire for field in response.fields}

    @pytest.mark.parametrize("builder,response_name", sorted(ROWS.items()))
    def test_the_java_carries_every_field_the_engine_sends(
        self, builder: str, response_name: str
    ) -> None:
        sends = self._engine_fields(builder)
        carries = self._java_fields(response_name)
        dropped = {
            f for f in sends - carries if (response_name, f) not in self.NOT_CARRIED
        }

        assert dropped == set(), (
            f"{response_name} does not declare {sorted(dropped)}, which {builder} "
            "sends — so CloudStack drops them and anything reading them is blank. "
            "Add the field to the generator, or list it in NOT_CARRIED with a reason."
        )
