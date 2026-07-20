from __future__ import annotations

from agent_runtime.admin.models import (
    AdminActor,
    AdminAuthorizationError,
    AdminScope,
)


class AdminAuthorizer:
    def require(
        self,
        actor: AdminActor,
        scope: AdminScope,
        tenant_id: str | None = None,
    ) -> None:
        if scope.value not in actor.scopes:
            raise AdminAuthorizationError(f"missing admin scope: {scope.value}")
        if tenant_id is None:
            if actor.tenant_id is None and not actor.global_access:
                raise AdminAuthorizationError("tenant scope is required")
            return
        if not actor.global_access and actor.tenant_id != tenant_id:
            # Do not expose whether a resource in another tenant exists.
            raise AdminAuthorizationError("resource is not available")


def require_reason_and_operation(reason: str, operation_id: str) -> None:
    if not reason.strip():
        raise ValueError("reason must not be empty")
    if not operation_id.strip():
        raise ValueError("operation_id must not be empty")
