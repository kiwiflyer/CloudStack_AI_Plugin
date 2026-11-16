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
"""Storage for the Tier 2 objects (SPEC.md sections 4 and 8).

Thin stores over the tables in `aisvc.db.models`, in the same shape as
`KeyStore` and `UsageStore`: the policy lives in the v2 modules, and
these only decide what is written down and what is read back.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from aisvc.adapters import may_use
from aisvc.db.models import (
    AdapterRecord,
    BatchRecord,
    CertificateRecord,
    EvaluationRecord,
    GuardrailProfileRecord,
    PrivateEndpointRecord,
    utcnow,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from aisvc.adapters import AdapterRequest
    from aisvc.endpoints import EndpointExposure
    from aisvc.evaluation import GateDecision

__all__ = [
    "TERMINAL_BATCH_STATES",
    "AdapterStore",
    "BatchStore",
    "CertificateStore",
    "EvaluationStore",
    "ExposureStore",
    "GuardrailStore",
]

#: A batch in one of these has finished; cancelling is meaningless.
TERMINAL_BATCH_STATES = frozenset({"completed", "failed", "expired", "cancelled"})


def _new_id() -> str:
    return str(uuid.uuid4())


class AdapterStore:
    """Tenant LoRA adapters."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, request: AdapterRequest, *, uri: str) -> AdapterRecord:
        record = AdapterRecord(
            id=_new_id(),
            name=request.name,
            base_model=request.base_model,
            account=request.account,
            domain=request.domain,
            rank=request.rank,
            size_bytes=request.size_bytes,
            uri=uri,
            shared_with_domain=request.shared_with_domain,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, adapter_id: str) -> AdapterRecord | None:
        return self.session.get(AdapterRecord, adapter_id)

    def all(self) -> list[AdapterRecord]:
        return list(self.session.scalars(select(AdapterRecord).order_by(AdapterRecord.created_at)))

    def list_for(self, *, account: str, domain: str) -> list[AdapterRecord]:
        """Adapters this account owns, for its per-account limit."""
        return [
            record for record in self.all() if (record.account, record.domain) == (account, domain)
        ]

    def visible_to(self, *, account: str, domain: str, role: str) -> list[AdapterRecord]:
        """Adapters this caller may see (SPEC.md section 8).

        Reuses `adapters.may_use` rather than repeating the rule: a
        second copy of a visibility check is a second place for it to
        go wrong.
        """
        return [
            record
            for record in self.all()
            if may_use(record, account=account, domain=domain, role=role)
        ]

    def delete(self, record: AdapterRecord) -> None:
        self.session.delete(record)
        self.session.flush()


class BatchStore:
    """Batch jobs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        account: str,
        domain: str,
        model: str,
        request_count: int,
        input_uri: str = "",
    ) -> BatchRecord:
        record = BatchRecord(
            id=_new_id(),
            account=account,
            domain=domain,
            model=model,
            request_count=request_count,
            input_uri=input_uri,
            state="validating",
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, batch_id: str) -> BatchRecord | None:
        return self.session.get(BatchRecord, batch_id)

    def all(self) -> list[BatchRecord]:
        return list(self.session.scalars(select(BatchRecord).order_by(BatchRecord.created_at)))

    def cancel(self, record: BatchRecord) -> None:
        record.state = "cancelled"
        record.finished_at = utcnow()
        self.session.flush()


class EvaluationStore:
    """Evaluation verdicts, kept against the catalog entry."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, model: str, revision: str, decision: GateDecision) -> EvaluationRecord:
        record = EvaluationRecord(
            id=_new_id(),
            model=model,
            revision=revision,
            passed=decision.passed,
            overridden=decision.overridden,
            summary=decision.summary,
            suites=[
                {
                    "name": r.name,
                    "score": r.score,
                    "baseline": r.baseline,
                    "delta": r.delta,
                    "customer": r.customer,
                }
                for r in decision.regressions
            ],
            performance=[
                {
                    "gpu_class": p.gpu_class,
                    "ttft_p95_ms": p.ttft_p95_ms,
                    "tokens_per_second": p.tokens_per_second,
                }
                for p in decision.performance
            ],
        )
        self.session.add(record)
        self.session.flush()
        return record

    def for_model(self, model: str) -> list[EvaluationRecord]:
        return list(
            self.session.scalars(
                select(EvaluationRecord)
                .where(EvaluationRecord.model == model)
                .order_by(EvaluationRecord.created_at)
            )
        )

    def latest(self, model: str) -> EvaluationRecord | None:
        """The most recent verdict, which is the one publishing turns on."""
        evaluations = self.for_model(model)
        return evaluations[-1] if evaluations else None


class GuardrailStore:
    """Guardrail profiles and where they are bound."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self, *, name: str, scope: str, target: str, checks: dict[str, Any]
    ) -> GuardrailProfileRecord:
        record = GuardrailProfileRecord(
            id=_new_id(), name=name, scope=scope, target=target, checks=checks
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, profile_id: str) -> GuardrailProfileRecord | None:
        return self.session.get(GuardrailProfileRecord, profile_id)

    def all(self) -> list[GuardrailProfileRecord]:
        return list(
            self.session.scalars(
                select(GuardrailProfileRecord).order_by(GuardrailProfileRecord.created_at)
            )
        )

    def update(
        self,
        record: GuardrailProfileRecord,
        *,
        checks: dict[str, Any] | None = None,
        target: str | None = None,
    ) -> None:
        if checks is not None:
            record.checks = checks
        if target is not None:
            record.target = target
        self.session.flush()

    def delete(self, record: GuardrailProfileRecord) -> None:
        self.session.delete(record)
        self.session.flush()


class ExposureStore:
    """How deployments are exposed."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, exposure: EndpointExposure) -> PrivateEndpointRecord:
        record = PrivateEndpointRecord(
            id=_new_id(),
            deployment=exposure.deployment,
            account=exposure.account,
            domain=exposure.domain,
            network_id=exposure.network_id,
            public_enabled=exposure.public_enabled,
            custom_domain=exposure.custom_domain,
            tls=exposure.tls.value,
            certificate_id=exposure.certificate_id,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, exposure_id: str) -> PrivateEndpointRecord | None:
        return self.session.get(PrivateEndpointRecord, exposure_id)

    def all(self) -> list[PrivateEndpointRecord]:
        return list(
            self.session.scalars(
                select(PrivateEndpointRecord).order_by(PrivateEndpointRecord.created_at)
            )
        )

    def delete(self, record: PrivateEndpointRecord) -> None:
        self.session.delete(record)
        self.session.flush()


class CertificateStore:
    """Uploaded TLS material.

    `list_for` deliberately returns the records; every caller that
    renders them uses the metadata only, and the one place that needs
    the material asks for it by id.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        account: str,
        domain: str,
        custom_domain: str,
        certificate: str,
        private_key: str,
        fingerprint: str,
        not_after: Any,
    ) -> CertificateRecord:
        record = CertificateRecord(
            id=_new_id(),
            account=account,
            domain=domain,
            custom_domain=custom_domain,
            certificate=certificate,
            private_key=private_key,
            fingerprint=fingerprint,
            not_after=not_after,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, certificate_id: str) -> CertificateRecord | None:
        return self.session.get(CertificateRecord, certificate_id)

    def all(self) -> list[CertificateRecord]:
        return list(
            self.session.scalars(select(CertificateRecord).order_by(CertificateRecord.created_at))
        )

    def replace_material(
        self,
        record: CertificateRecord,
        *,
        certificate: str,
        private_key: str,
        fingerprint: str,
        not_after: Any,
    ) -> CertificateRecord:
        """Swap the material, keeping the id.

        The id stays because exposures name it; a new one would mean
        rewriting every exposure by hand at the moment the old
        certificate is about to stop working.

        The serial moves, which is what tells the planner a proxy is
        serving superseded material. The notified thresholds reset,
        since the new expiry is a new countdown.
        """
        record.certificate = certificate
        record.private_key = private_key
        record.fingerprint = fingerprint
        record.not_after = not_after
        record.serial = (record.serial or 1) + 1
        record.notified = []
        self.session.flush()
        return record

    def delete(self, record: CertificateRecord) -> None:
        self.session.delete(record)
        self.session.flush()
