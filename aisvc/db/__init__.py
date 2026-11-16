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
"""Persistence: SQLAlchemy models and the run log (SPEC.md section 4)."""

from __future__ import annotations

from aisvc.db.keystore import KeyStore, UsageStore
from aisvc.db.models import (
    ActionOutcome,
    ActionRecord,
    ApiKeyRecord,
    Base,
    BudgetRecord,
    RunRecord,
    RunStatus,
    UsageRow,
    utcnow,
)
from aisvc.db.runlog import RunLog
from aisvc.db.session import create_session_factory, session_scope

__all__ = [
    "ActionOutcome",
    "ActionRecord",
    "ApiKeyRecord",
    "Base",
    "BudgetRecord",
    "KeyStore",
    "RunLog",
    "RunRecord",
    "RunStatus",
    "UsageRow",
    "UsageStore",
    "create_session_factory",
    "session_scope",
    "utcnow",
]
