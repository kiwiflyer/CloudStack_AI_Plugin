"""Async jobs (SPEC.md section 10).

"Async calls return 202 with a job id; job states mirror CloudStack async
jobs (pending, success, failure with error text)."
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from aisvc.api.context import CallerContext, Role
from aisvc.api.jobs import JobRecord, JobState, JobStore
from aisvc.db import Base

CALLER = CallerContext(
    user_id="u-1",
    account="acme",
    domain_path="/Customers/Acme",
    role=Role.USER,
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as s:
        yield s


@pytest.fixture
def jobs(session: Session) -> JobStore:
    return JobStore(session)


class TestCreate:
    def test_a_new_job_is_pending(self, jobs: JobStore) -> None:
        job = jobs.create("mirrorAiModel", CALLER)

        assert isinstance(job, JobRecord)
        assert job.state is JobState.PENDING
        assert job.command == "mirrorAiModel"

    def test_the_job_id_is_a_uuid_not_a_counter(self, jobs: JobStore) -> None:
        """Job ids reach tenants; sequential ids leak platform volume."""
        job = jobs.create("mirrorAiModel", CALLER)

        assert len(job.id) == 36
        assert job.id.count("-") == 4

    def test_the_caller_is_recorded(self, jobs: JobStore) -> None:
        job = jobs.create("mirrorAiModel", CALLER)

        assert job.account == "acme"
        assert job.domain_path == "/Customers/Acme"
        assert job.user_id == "u-1"

    def test_two_jobs_get_different_ids(self, jobs: JobStore) -> None:
        assert jobs.create("a", CALLER).id != jobs.create("b", CALLER).id


class TestOutcomes:
    def test_success_carries_the_result(self, jobs: JobStore) -> None:
        job = jobs.create("deployAiModel", CALLER)

        jobs.succeed(job, {"deployment": {"id": "d-1"}})

        assert job.state is JobState.SUCCESS
        assert job.result == {"deployment": {"id": "d-1"}}
        assert job.finished_at is not None

    def test_failure_carries_the_error_text(self, jobs: JobStore) -> None:
        job = jobs.create("deployAiModel", CALLER)

        jobs.fail(job, "Insufficient capacity", error_code=533)

        assert job.state is JobState.FAILURE
        assert job.error_text == "Insufficient capacity"
        assert job.error_code == 533

    def test_a_failure_without_a_code_is_allowed(self, jobs: JobStore) -> None:
        job = jobs.create("deployAiModel", CALLER)
        jobs.fail(job, "something went wrong")

        assert job.error_code is None


class TestCloudStackShape:
    def test_a_pending_job_renders_as_jobstatus_zero(self, jobs: JobStore) -> None:
        """The plugin polls this and maps it onto a CloudStack async job."""
        job = jobs.create("mirrorAiModel", CALLER)

        assert job.as_response()["jobstatus"] == 0

    def test_a_successful_job_renders_as_jobstatus_one(self, jobs: JobStore) -> None:
        job = jobs.create("mirrorAiModel", CALLER)
        jobs.succeed(job, {"ok": True})

        body = job.as_response()
        assert body["jobstatus"] == 1
        assert body["jobresult"] == {"ok": True}

    def test_a_failed_job_renders_as_jobstatus_two(self, jobs: JobStore) -> None:
        job = jobs.create("mirrorAiModel", CALLER)
        jobs.fail(job, "nope", error_code=530)

        body = job.as_response()
        assert body["jobstatus"] == 2
        assert body["jobresult"]["errortext"] == "nope"
        assert body["jobresult"]["errorcode"] == 530

    def test_the_response_always_carries_the_job_id(self, jobs: JobStore) -> None:
        job = jobs.create("mirrorAiModel", CALLER)

        assert job.as_response()["jobid"] == job.id


class TestLookupAndFiltering:
    def test_a_job_can_be_fetched_by_id(self, jobs: JobStore) -> None:
        job = jobs.create("mirrorAiModel", CALLER)

        assert jobs.get(job.id, CALLER) is not None

    def test_a_missing_job_is_none(self, jobs: JobStore) -> None:
        assert jobs.get("00000000-0000-0000-0000-000000000000", CALLER) is None

    def test_another_account_cannot_see_the_job(self, jobs: JobStore) -> None:
        """SPEC.md section 10: every response is filtered by caller context."""
        job = jobs.create("mirrorAiModel", CALLER)
        intruder = CallerContext(
            user_id="u-2",
            account="other",
            domain_path="/Customers/Other",
            role=Role.USER,
        )

        assert jobs.get(job.id, intruder) is None

    def test_a_root_admin_sees_any_job(self, jobs: JobStore) -> None:
        job = jobs.create("mirrorAiModel", CALLER)
        admin = CallerContext(user_id="root", account="admin", domain_path="/", role=Role.ADMIN)

        assert jobs.get(job.id, admin) is not None

    def test_a_domain_admin_sees_jobs_in_their_domain(self, jobs: JobStore) -> None:
        job = jobs.create("mirrorAiModel", CALLER)
        domain_admin = CallerContext(
            user_id="da",
            account="acme-admin",
            domain_path="/Customers/Acme",
            role=Role.DOMAIN_ADMIN,
        )

        assert jobs.get(job.id, domain_admin) is not None

    def test_a_domain_admin_cannot_see_another_domain(self, jobs: JobStore) -> None:
        job = jobs.create("mirrorAiModel", CALLER)
        elsewhere = CallerContext(
            user_id="da",
            account="other-admin",
            domain_path="/Customers/Other",
            role=Role.DOMAIN_ADMIN,
        )

        assert jobs.get(job.id, elsewhere) is None
