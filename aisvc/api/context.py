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
"""Caller context and visibility rules (SPEC.md sections 9 and 10).

"Every request carries a caller context {user_id, account, domain_path,
role} set by the plugin from the CloudStack session; the engine filters
every response by it."

CloudStack owns authentication and roles. The engine never decides who
someone is — it is unreachable from tenant networks and trusts the
plugin's mTLS-authenticated assertion — but it does decide what they may
see, and that decision lives here so it is applied the same way
everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["CallerContext", "Role"]


class Role(StrEnum):
    """CloudStack's default role types (SPEC.md section 9)."""

    ADMIN = "Admin"
    DOMAIN_ADMIN = "DomainAdmin"
    USER = "User"

    @property
    def is_admin(self) -> bool:
        return self is Role.ADMIN


@dataclass(frozen=True, slots=True)
class CallerContext:
    """Who is asking, as the plugin read it from the CloudStack session."""

    user_id: str
    account: str
    domain_path: str
    role: Role

    def owns_domain(self, domain_path: str) -> bool:
        """Whether this caller's domain contains `domain_path`.

        Domain paths are hierarchical, so /Customers/Acme contains
        /Customers/Acme/Sub but not /Customers/AcmeCorp — the separator
        matters.
        """
        if not domain_path:
            return False
        mine = self.domain_path.rstrip("/") or "/"
        theirs = domain_path.rstrip("/") or "/"
        if mine == "/":
            return True
        return theirs == mine or theirs.startswith(f"{mine}/")

    def may_see(self, *, account: str | None, domain_path: str | None) -> bool:
        """Whether this caller may see a resource owned by that account."""
        if self.role.is_admin:
            return True
        if self.role is Role.DOMAIN_ADMIN:
            return self.owns_domain(domain_path or "")
        return account == self.account
