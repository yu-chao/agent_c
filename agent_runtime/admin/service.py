from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable

from agent_runtime.admin.authorization import (
    AdminAuthorizer,
    require_reason_and_operation,
)
from agent_runtime.admin.models import (
    AdminActor,
    AdminNotFoundError,
    AdminScope,
    Page,
    ToolDisposition,
)
from agent_runtime.admin.ports import AdminRepository


class AdminService:
    def __init__(
        self,
        repository: AdminRepository,
        *,
        approval_repository=None,
        resume_callback: Callable[[str], Any] | None = None,
        authorizer: AdminAuthorizer | None = None,
    ) -> None:
        self.repository = repository
        self.approval_repository = approval_repository
        self.resume_callback = resume_callback
        self.authorizer = authorizer or AdminAuthorizer()

    def list_sessions(
        self, actor: AdminActor, *, before: datetime | None = None,
        limit: int = 100,
    ) -> Page:
        self._require(actor, AdminScope.READ)
        return Page(tuple(self.repository.admin_list_sessions(
            tenant_id=self._tenant_filter(actor), before=before,
            limit=_limit(limit),
        )))

    def list_runs(
        self, actor: AdminActor, *, session_id: str | None = None,
        status: str | None = None, before: datetime | None = None,
        limit: int = 100,
    ) -> Page:
        self._require(actor, AdminScope.READ)
        return Page(tuple(self.repository.admin_list_runs(
            tenant_id=self._tenant_filter(actor), session_id=session_id,
            status=status, before=before, limit=_limit(limit),
        )))

    def list_checkpoints(
        self, actor: AdminActor, run_id: str, *, limit: int = 100,
    ) -> Page:
        run = self._run_for(actor, run_id, AdminScope.READ)
        return Page(tuple(self.repository.admin_list_checkpoints(
            run.id, limit=_limit(limit)
        )))

    def list_tool_executions(
        self, actor: AdminActor, run_id: str, *, status: str | None = None,
        limit: int = 100,
    ) -> Page:
        run = self._run_for(actor, run_id, AdminScope.READ)
        return Page(tuple(self.repository.admin_list_tool_executions(
            run.id, status=status, limit=_limit(limit)
        )))

    def list_approvals(
        self, actor: AdminActor, *, status: str | None = None,
        limit: int = 100,
    ) -> Page:
        self._require(actor, AdminScope.READ)
        if self.approval_repository is None:
            return Page(())
        items = self.approval_repository.list_for_admin(
            tenant_id=self._tenant_filter(actor), status=status,
            limit=_limit(limit),
        )
        return Page(tuple(items))

    def pause_run(self, actor: AdminActor, run_id: str, *, reason: str,
                  operation_id: str):
        return self._control(actor, run_id, "pause", reason, operation_id)

    def cancel_run(self, actor: AdminActor, run_id: str, *, reason: str,
                   operation_id: str):
        return self._control(actor, run_id, "cancel", reason, operation_id)

    def resume_run(self, actor: AdminActor, run_id: str, *, reason: str,
                   operation_id: str):
        result = self._control(
            actor, run_id, "recover", reason, operation_id,
            scope=AdminScope.RECONCILE,
        )
        if result.changed and self.resume_callback is not None:
            self.resume_callback(run_id)
        return result

    def resolve_uncertain_tool(
        self, actor: AdminActor, run_id: str, call_id: str, *,
        disposition: ToolDisposition | str, reason: str, operation_id: str,
        output: str | None = None,
    ):
        require_reason_and_operation(reason, operation_id)
        run = self._run_for(actor, run_id, AdminScope.RECONCILE)
        disposition = ToolDisposition(disposition)
        if disposition is ToolDisposition.CONFIRMED_SUCCEEDED and output is None:
            raise ValueError("output is required when confirming success")
        request = {
            "action": "resolve_tool", "run_id": run_id,
            "call_id": call_id, "disposition": disposition.value,
            "output": output,
        }
        return self.repository.admin_resolve_tool(
            tenant_id=run.tenant_id, run_id=run_id, call_id=call_id,
            disposition=disposition, output=output, actor_id=actor.actor_id,
            reason=reason, operation_id=operation_id,
            request_hash=_request_hash(request),
        )

    def export_audit(
        self, actor: AdminActor, *, since: datetime | None = None,
        until: datetime | None = None, limit: int = 1000,
    ) -> tuple:
        self._require(actor, AdminScope.AUDIT_EXPORT)
        return tuple(self.repository.admin_export_audit(
            tenant_id=self._tenant_filter(actor), since=since, until=until,
            limit=_limit(limit, maximum=10000),
        ))

    def _control(
        self, actor, run_id, action, reason, operation_id, *,
        scope: AdminScope = AdminScope.CONTROL,
    ):
        require_reason_and_operation(reason, operation_id)
        run = self._run_for(actor, run_id, scope)
        request = {"action": action, "run_id": run_id}
        return self.repository.admin_control_run(
            tenant_id=run.tenant_id, run_id=run_id, action=action,
            actor_id=actor.actor_id, reason=reason,
            operation_id=operation_id, request_hash=_request_hash(request),
        )

    def _run_for(self, actor, run_id, scope):
        self._require(actor, scope)
        run = self.repository.admin_get_run(run_id)
        if run is None:
            raise AdminNotFoundError("run not found")
        try:
            self.authorizer.require(actor, scope, run.tenant_id)
        except PermissionError as exc:
            raise AdminNotFoundError("run not found") from exc
        return run

    def _require(self, actor, scope):
        self.authorizer.require(actor, scope)

    @staticmethod
    def _tenant_filter(actor: AdminActor) -> str | None:
        return None if actor.global_access else actor.tenant_id


def _limit(value: int, *, maximum: int = 100) -> int:
    if value <= 0:
        raise ValueError("limit must be greater than zero")
    return min(value, maximum)


def _request_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
