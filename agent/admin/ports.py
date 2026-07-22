from __future__ import annotations

from datetime import datetime
from typing import Protocol

from agent.admin.models import (
    AdminCheckpoint,
    AdminCommandResult,
    AdminRun,
    AdminSession,
    AdminToolExecution,
    AuditEvent,
    ToolDisposition,
)


class AdminRepository(Protocol):
    def admin_get_run(self, run_id: str) -> AdminRun | None: ...
    def admin_list_sessions(
        self, *, tenant_id: str | None, before: datetime | None, limit: int
    ) -> list[AdminSession]: ...
    def admin_list_runs(
        self, *, tenant_id: str | None, session_id: str | None,
        status: str | None, before: datetime | None, limit: int
    ) -> list[AdminRun]: ...
    def admin_list_checkpoints(
        self, run_id: str, *, limit: int
    ) -> list[AdminCheckpoint]: ...
    def admin_list_tool_executions(
        self, run_id: str, *, status: str | None, limit: int
    ) -> list[AdminToolExecution]: ...
    def admin_control_run(
        self, *, tenant_id: str, run_id: str, action: str, actor_id: str,
        reason: str, operation_id: str, request_hash: str,
    ) -> AdminCommandResult: ...
    def admin_resolve_tool(
        self, *, tenant_id: str, run_id: str, call_id: str,
        disposition: ToolDisposition, output: str | None, actor_id: str,
        reason: str, operation_id: str, request_hash: str,
    ) -> AdminCommandResult: ...
    def admin_export_audit(
        self, *, tenant_id: str | None, since: datetime | None,
        until: datetime | None, limit: int,
    ) -> list[AuditEvent]: ...
