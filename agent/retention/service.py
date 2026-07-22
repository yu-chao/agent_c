from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Protocol

from agent.admin.authorization import (
    AdminAuthorizer,
    require_reason_and_operation,
)
from agent.admin.models import AdminActor, AdminScope

from .models import RetentionResult


class RetentionRepository(Protocol):
    def purge_tenant_data(
        self, *, tenant_id: str, before: datetime, actor_id: str,
        reason: str, operation_id: str, request_hash: str,
    ) -> RetentionResult: ...


class RetentionService:
    def __init__(
        self,
        repository: RetentionRepository,
        authorizer: AdminAuthorizer | None = None,
    ) -> None:
        self.repository = repository
        self.authorizer = authorizer or AdminAuthorizer()

    def purge(
        self,
        actor: AdminActor,
        tenant_id: str,
        before: datetime,
        *,
        reason: str,
        operation_id: str,
    ) -> RetentionResult:
        require_reason_and_operation(reason, operation_id)
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("before must be timezone-aware")
        self.authorizer.require(actor, AdminScope.RETENTION_EXECUTE, tenant_id)
        request_hash = _request_hash({
            "action": "retention.purge",
            "tenant_id": tenant_id,
            "before": before.isoformat(),
        })
        return self.repository.purge_tenant_data(
            tenant_id=tenant_id,
            before=before,
            actor_id=actor.actor_id,
            reason=reason,
            operation_id=operation_id,
            request_hash=request_hash,
        )


def _request_hash(value: dict[str, str]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
