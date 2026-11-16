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
"""Signed CloudStack API client (SPEC.md section 4).

Driven through httpx's MockTransport, so the client's own request building,
retry and polling logic runs for real against a scripted server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable

import httpx
import pytest

from aisvc.cloudstack import (
    AsyncJobFailed,
    CloudStackClient,
    CloudStackError,
    Credentials,
    canonical_query,
    sign,
)

CREDS = Credentials(
    endpoint="https://cloud.example.com/client/api",
    api_key="apikey-123",
    secret_key="secret-456",
)


def transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def client(handler: Callable[[httpx.Request], httpx.Response], **kw: object) -> CloudStackClient:
    return CloudStackClient(CREDS, transport=transport(handler), **kw)  # type: ignore[arg-type]


def ok(payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=payload)


class TestCanonicalQuery:
    def test_pairs_are_sorted_by_key(self) -> None:
        assert canonical_query({"zebra": "1", "alpha": "2"}) == "alpha=2&zebra=1"

    def test_whole_string_is_lower_cased(self) -> None:
        assert canonical_query({"Command": "ListHosts"}) == "command=listhosts"

    def test_values_are_url_encoded(self) -> None:
        # %20, not +. This asserted `name=my+host` and so pinned the
        # bug in place: the test shared the code's wrong assumption.
        assert canonical_query({"name": "my host"}) == "name=my%20host"

    def test_signature_is_never_part_of_the_signed_string(self) -> None:
        assert canonical_query({"a": "1", "signature": "xyz"}) == "a=1"

    def test_none_values_are_dropped(self) -> None:
        assert canonical_query({"a": "1", "b": None}) == "a=1"

    def test_booleans_are_rendered_lower_case(self) -> None:
        assert canonical_query({"listall": True}) == "listall=true"

    def test_lists_are_joined_with_commas(self) -> None:
        # The whole string is lower-cased after encoding, so %2C becomes %2c.
        assert canonical_query({"ids": ["a", "b"]}) == "ids=a%2cb"


class TestSign:
    def test_signature_is_base64_hmac_sha1_of_the_canonical_query(self) -> None:
        params = {"command": "listHosts", "apikey": "apikey-123"}
        expected = base64.b64encode(
            hmac.new(
                b"secret-456",
                canonical_query(params).encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        assert sign(params, "secret-456") == expected

    def test_signature_changes_with_the_secret(self) -> None:
        params = {"command": "listHosts"}
        assert sign(params, "secret-456") != sign(params, "other-secret")


class TestCall:
    async def test_request_carries_command_apikey_response_and_signature(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return ok({"listhostsresponse": {"count": 0, "host": []}})

        async with client(handler) as cs:
            await cs.call("listHosts")

        params = dict(httpx.QueryParams(seen[0].url.query.decode()))
        assert params["command"] == "listHosts"
        assert params["apikey"] == "apikey-123"
        assert params["response"] == "json"
        assert params["signature"]

    async def test_response_envelope_is_unwrapped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"listhostsresponse": {"count": 1, "host": [{"id": "h1"}]}})

        async with client(handler) as cs:
            result = await cs.call("listHosts")

        assert result == {"count": 1, "host": [{"id": "h1"}]}

    async def test_api_error_is_raised_with_code_and_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                431,
                json={
                    "listhostsresponse": {
                        "errorcode": 431,
                        "errortext": "Unable to execute API command",
                    }
                },
            )

        async with client(handler) as cs:
            with pytest.raises(CloudStackError) as exc:
                await cs.call("listHosts")

        assert exc.value.error_code == 431
        assert "Unable to execute" in str(exc.value)

    async def test_parameters_are_passed_through(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return ok({"listhostsresponse": {}})

        async with client(handler) as cs:
            await cs.call("listHosts", type="Routing", listall=True)

        params = dict(httpx.QueryParams(seen[0].url.query.decode()))
        assert params["type"] == "Routing"
        assert params["listall"] == "true"


class TestRetries:
    async def test_server_errors_are_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(503, text="busy")
            return ok({"listhostsresponse": {"count": 0}})

        async with client(handler, max_retries=3, backoff_base=0.0) as cs:
            await cs.call("listHosts")

        assert attempts == 3

    async def test_retries_are_bounded(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, text="busy")

        async with client(handler, max_retries=2, backoff_base=0.0) as cs:
            with pytest.raises(CloudStackError):
                await cs.call("listHosts")

        assert attempts == 3  # one attempt plus two retries

    async def test_client_errors_are_not_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                431, json={"listhostsresponse": {"errorcode": 431, "errortext": "bad"}}
            )

        async with client(handler, max_retries=3, backoff_base=0.0) as cs:
            with pytest.raises(CloudStackError):
                await cs.call("listHosts")

        assert attempts == 1

    async def test_a_cloudstack_error_body_is_not_retried_whatever_the_status(
        self,
    ) -> None:
        """CloudStack answers API errors with 5xx codes such as 530.

        Those are definitive answers, not transient failures: retrying
        wastes time and loses the error text.
        """
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                530,
                json={
                    "deployvirtualmachineresponse": {
                        "errorcode": 533,
                        "errortext": "Unable to create a deployment",
                    }
                },
            )

        async with client(handler, max_retries=3, backoff_base=0.0) as cs:
            with pytest.raises(CloudStackError, match="Unable to create a deployment"):
                await cs.call("deployVirtualMachine")

        assert attempts == 1

    async def test_a_5xx_without_an_error_body_is_still_retried(self) -> None:
        """A bare 502 from a proxy is transient and worth retrying."""
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                return httpx.Response(502, text="<html>bad gateway</html>")
            return ok({"listhostsresponse": {"count": 0}})

        async with client(handler, max_retries=3, backoff_base=0.0) as cs:
            await cs.call("listHosts")

        assert attempts == 2

    async def test_giving_up_keeps_the_last_error_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="busy")

        async with client(handler, max_retries=1, backoff_base=0.0) as cs:
            with pytest.raises(CloudStackError, match="503"):
                await cs.call("listHosts")

    async def test_transport_errors_are_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("boom", request=request)
            return ok({"listhostsresponse": {}})

        async with client(handler, max_retries=2, backoff_base=0.0) as cs:
            await cs.call("listHosts")

        assert attempts == 2


class TestPagination:
    async def test_all_pages_are_followed(self) -> None:
        pages = {
            "1": [{"id": "h1"}, {"id": "h2"}],
            "2": [{"id": "h3"}],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(httpx.QueryParams(request.url.query.decode()))
            page = params.get("page", "1")
            return ok({"listhostsresponse": {"count": 3, "host": pages.get(page, [])}})

        async with client(handler) as cs:
            items = await cs.call_paged("listHosts", "host", page_size=2)

        assert [i["id"] for i in items] == ["h1", "h2", "h3"]

    async def test_empty_result_yields_no_items(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"listhostsresponse": {"count": 0}})

        async with client(handler) as cs:
            assert await cs.call_paged("listHosts", "host") == []

    async def test_pagination_stops_when_a_page_comes_back_short(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return ok({"listhostsresponse": {"count": 99, "host": [{"id": "h1"}]}})

        async with client(handler) as cs:
            items = await cs.call_paged("listHosts", "host", page_size=10)

        assert calls == 1
        assert len(items) == 1


class TestAsyncJobs:
    async def test_pending_job_is_polled_until_it_succeeds(self) -> None:
        polls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal polls
            params = dict(httpx.QueryParams(request.url.query.decode()))
            if params["command"] == "queryAsyncJobResult":
                polls += 1
                if polls < 3:
                    return ok({"queryasyncjobresultresponse": {"jobstatus": 0}})
                return ok(
                    {
                        "queryasyncjobresultresponse": {
                            "jobstatus": 1,
                            "jobresult": {"virtualmachine": {"id": "vm-1"}},
                        }
                    }
                )
            return ok({"deployvirtualmachineresponse": {"jobid": "job-1"}})

        async with client(handler, poll_interval=0.0) as cs:
            result = await cs.call_async("deployVirtualMachine", name="n1")

        assert polls == 3
        assert result == {"virtualmachine": {"id": "vm-1"}}

    async def test_failed_job_raises_with_the_error_text(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(httpx.QueryParams(request.url.query.decode()))
            if params["command"] == "queryAsyncJobResult":
                return ok(
                    {
                        "queryasyncjobresultresponse": {
                            "jobstatus": 2,
                            "jobresult": {
                                "errorcode": 530,
                                "errortext": "Insufficient capacity",
                            },
                        }
                    }
                )
            return ok({"deployvirtualmachineresponse": {"jobid": "job-1"}})

        async with client(handler, poll_interval=0.0) as cs:
            with pytest.raises(AsyncJobFailed, match="Insufficient capacity"):
                await cs.call_async("deployVirtualMachine")

    async def test_polling_gives_up_after_the_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(httpx.QueryParams(request.url.query.decode()))
            if params["command"] == "queryAsyncJobResult":
                return ok({"queryasyncjobresultresponse": {"jobstatus": 0}})
            return ok({"deployvirtualmachineresponse": {"jobid": "job-1"}})

        async with client(handler, poll_interval=0.0) as cs:
            with pytest.raises(TimeoutError, match="job-1"):
                await cs.call_async("deployVirtualMachine", job_timeout=0.05)

    async def test_synchronous_response_without_a_jobid_is_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"deployvirtualmachineresponse": {"id": "vm-1"}})

        async with client(handler, poll_interval=0.0) as cs:
            with pytest.raises(CloudStackError, match="jobid"):
                await cs.call_async("deployVirtualMachine")


class TestASpaceIsSignedAsPercentTwenty:
    """CloudStack rejects a `+` in the signed string.

    Found against a real 4.23.0.0 management server, not in a fake:
    every call whose parameter value contained a space came back
    "unable to verify user credentials and/or request signature", and
    every call without one worked. `a+b` and `a/b` were fine, so it is
    the space specifically.

    The two sides of our own request agreed — `quote_plus` and httpx
    both render a space as `+`. The server is the one that does not:
    the servlet decodes `+` to a space and re-encodes it as `%20` when
    it rebuilds the string to verify, so a `+` signature can never
    match. Only the signature needs fixing; the request may still send
    `+`, because the server decodes it before looking.

    `proxy_offering: Small Instance` is in the shipped
    examples/platform.yaml, so this was reachable with the defaults.
    """

    def test_a_space_becomes_percent_twenty(self) -> None:
        assert "%20" in canonical_query({"keyword": "Small Instance"})

    def test_a_space_never_becomes_a_plus(self) -> None:
        assert "+" not in canonical_query({"keyword": "Small Instance"})

    def test_a_literal_plus_is_still_escaped(self) -> None:
        # %2B, not a bare +: the server would read a bare one as a
        # space and verify a different string than we signed.
        assert canonical_query({"keyword": "a+b"}) == "keyword=a%2bb"

    def test_a_slash_is_still_escaped(self) -> None:
        assert canonical_query({"path": "/Customers/Acme"}) == "path=%2fcustomers%2facme"

    def test_ordinary_values_are_unchanged(self) -> None:
        assert canonical_query({"command": "listZones"}) == "command=listzones"


class TestPagingTerminatesOnACommandThatIgnoresIt:
    """`call_paged` looped forever on `listApis`.

    Measured against a real 4.23.0.0 server: `listApis` ignores `page`
    and `pagesize` entirely and returns all 865 commands every time.
    The pager stopped only when a page came back *shorter* than the
    page size, and 865 is never shorter than 500 — so it requested
    page after page, accumulating the same 865 rows, and never
    returned.

    Nothing in the engine pages `listApis` today, so this was latent.
    It is still the worst failure a reconciler can have: a run that
    holds its database connection and never finishes, with no error.

    The server says `count`, which terminates whatever the paging
    behaviour, and a page cap catches anything that reports neither.
    """

    def ignoring_pager(
        self, total: int = 865
    ) -> tuple[Callable[[httpx.Request], httpx.Response], list[int]]:
        """A server that returns everything regardless of the page asked for."""
        rows = [{"name": f"cmd{i}"} for i in range(total)]
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(int(request.url.params.get("page", 1)))
            return ok({"listapisresponse": {"count": total, "api": rows}})

        return handler, seen

    async def test_it_returns_instead_of_looping(self) -> None:
        handler, seen = self.ignoring_pager()
        async with client(handler) as cl:
            got = await cl.call_paged("listApis", "api")

        assert len(got) == 865
        assert seen == [1], f"asked for pages {seen}; it should have stopped after one"

    async def test_a_well_behaved_command_still_pages(self) -> None:
        pages = {1: 500, 2: 500, 3: 20}

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", 1))
            rows = [{"id": f"h{page}-{i}"} for i in range(pages.get(page, 0))]
            return ok({"listhostsresponse": {"count": 1020, "host": rows}})

        async with client(handler) as cl:
            got = await cl.call_paged("listHosts", "host", page_size=500)

        assert len(got) == 1020

    async def test_a_response_with_no_count_still_stops_on_a_short_page(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", 1))
            rows = [{"id": str(i)} for i in range(3 if page == 1 else 0)]
            return ok({"listzonesresponse": {"zone": rows}})

        async with client(handler) as cl:
            got = await cl.call_paged("listZones", "zone", page_size=500)

        assert len(got) == 3

    async def test_a_server_reporting_neither_is_capped_not_hung(self) -> None:
        # Neither a count nor a short page: without a cap this is the
        # original hang. An error an operator can read beats a run that
        # never ends.
        def handler(request: httpx.Request) -> httpx.Response:
            return ok({"listhostsresponse": {"host": [{"id": "h"} for _ in range(2)]}})

        async with client(handler) as cl:
            with pytest.raises(CloudStackError, match="page"):
                await cl.call_paged("listHosts", "host", page_size=2)
