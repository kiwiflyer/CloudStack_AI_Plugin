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
"""Stored tuning profiles and their assignments (SPEC.md section 6).

`platform.yaml` declares profiles too, and both reach the same
`aisvc.profiles.resolve()`. These are the ones created through the API,
which is why they carry an owner and the spec's do not: a spec profile
was written by whoever edits the spec, and a stored one by whichever
tenant called the command.

A profile and its binding are separate rows on purpose. The spec
separates them, the same profile may bind at more than one scope, and
collapsing them — as `aisvc_guardrail_profile` does — would make a
precedence chain that no `platform.yaml` could express.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from aisvc.db.models import (
    GenerationProfileRecord,
    ProfileAssignmentRecord,
    ServingProfileRecord,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

__all__ = [
    "AssignmentStore",
    "GenerationProfileStore",
    "ServingProfileStore",
]


def _new_id() -> str:
    return str(uuid.uuid4())


class ServingProfileStore:
    """Engine arguments, platform-wide."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, *, name: str, params: dict[str, Any]) -> ServingProfileRecord:
        record = ServingProfileRecord(id=_new_id(), name=name, params=params)
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, profile_id: str) -> ServingProfileRecord | None:
        return self.session.get(ServingProfileRecord, profile_id)

    def by_name(self, name: str) -> ServingProfileRecord | None:
        return self.session.scalars(
            select(ServingProfileRecord).where(ServingProfileRecord.name == name)
        ).first()

    def all(self) -> list[ServingProfileRecord]:
        return list(
            self.session.scalars(
                select(ServingProfileRecord).order_by(ServingProfileRecord.created_at)
            )
        )

    def update(self, record: ServingProfileRecord, *, params: dict[str, Any]) -> None:
        record.params = params
        self.session.flush()

    def delete(self, record: ServingProfileRecord) -> None:
        self.session.delete(record)
        self.session.flush()


class GenerationProfileStore:
    """Tunable generation parameters, owned by whoever created them."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self, *, name: str, params: dict[str, Any], account: str, domain: str
    ) -> GenerationProfileRecord:
        record = GenerationProfileRecord(
            id=_new_id(), name=name, params=params, account=account, domain=domain
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, profile_id: str) -> GenerationProfileRecord | None:
        return self.session.get(GenerationProfileRecord, profile_id)

    def by_name(self, name: str) -> GenerationProfileRecord | None:
        return self.session.scalars(
            select(GenerationProfileRecord).where(GenerationProfileRecord.name == name)
        ).first()

    def all(self) -> list[GenerationProfileRecord]:
        return list(
            self.session.scalars(
                select(GenerationProfileRecord).order_by(GenerationProfileRecord.created_at)
            )
        )

    def update(self, record: GenerationProfileRecord, *, params: dict[str, Any]) -> None:
        record.params = params
        self.session.flush()

    def delete(self, record: GenerationProfileRecord) -> None:
        self.session.delete(record)
        self.session.flush()


class AssignmentStore:
    """Which profile binds at which scope."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        scope: str,
        target: str,
        serving_profile: str = "",
        generation_profile: str = "",
    ) -> ProfileAssignmentRecord:
        # One binding per (scope, target) and kind. Re-assigning
        # replaces rather than stacking, because two rows naming
        # different profiles at the same level would make resolution
        # depend on insertion order.
        existing = self.for_target(scope=scope, target=target)
        if existing is not None:
            if serving_profile:
                existing.serving_profile = serving_profile
            if generation_profile:
                existing.generation_profile = generation_profile
            self.session.flush()
            return existing

        record = ProfileAssignmentRecord(
            id=_new_id(),
            scope=scope,
            target=target,
            serving_profile=serving_profile,
            generation_profile=generation_profile,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def for_target(self, *, scope: str, target: str) -> ProfileAssignmentRecord | None:
        return self.session.scalars(
            select(ProfileAssignmentRecord)
            .where(ProfileAssignmentRecord.scope == scope)
            .where(ProfileAssignmentRecord.target == target)
        ).first()

    def all(self) -> list[ProfileAssignmentRecord]:
        return list(
            self.session.scalars(
                select(ProfileAssignmentRecord).order_by(ProfileAssignmentRecord.created_at)
            )
        )

    def delete(self, record: ProfileAssignmentRecord) -> None:
        self.session.delete(record)
        self.session.flush()

    def drop_profile(self, *, generation_profile: str = "", serving_profile: str = "") -> int:
        """Unbind a profile that is being deleted.

        A binding naming a profile that no longer exists resolves to
        nothing, so the operator sees an assignment doing no work. Far
        better to remove it with the profile than to leave it pointing
        at a name.
        """
        removed = 0
        for record in self.all():
            if generation_profile and record.generation_profile == generation_profile:
                record.generation_profile = ""
                removed += 1
            if serving_profile and record.serving_profile == serving_profile:
                record.serving_profile = ""
                removed += 1
            if not record.generation_profile and not record.serving_profile:
                self.session.delete(record)
        self.session.flush()
        return removed
