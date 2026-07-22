from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar


class AdminScope(StrEnum):
    READ = "admin.read"
    CONTROL = "admin.control"
    RECONCILE = "admin.reconcile"
    AUDIT_EXPORT = "admin.audit.export"
    RETENTION_EXECUTE = "admin.retention.execute"


@dataclass(frozen=True)
class AdminActor:
    actor_id: str
    tenant_id: str | None
    scopes: frozenset[str] = field(default_factory=frozenset)
    global_access: bool = False

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("actor_id must not be empty")


class AdminAuthorizationError(PermissionError):
    pass


class AdminConflictError(RuntimeError):
    pass


class AdminNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class AdminSession:
    id: str
    tenant_id: str
    platform: str
    conversation_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AdminRun:
    id: str
    tenant_id: str
    session_id: str
    status: str
    inbound_platform: str
    inbound_message_id: str
    created_at: datetime
    updated_at: datetime
    error: str | None = None


@dataclass(frozen=True)
class AdminCheckpoint:
    id: int
    run_id: str
    sequence: int
    phase: str
    created_at: datetime


@dataclass(frozen=True)
class AdminToolExecution:
    run_id: str
    call_id: str
    tool_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    id: str
    tenant_id: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    reason: str
    operation_id: str
    outcome: str
    created_at: datetime
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdminCommandResult:
    operation_id: str
    changed: bool
    outcome: str
    resource_id: str
    audit_id: str


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None = None


class ToolDisposition(StrEnum):
    CONFIRMED_SUCCEEDED = "confirmed_succeeded"
    CONFIRMED_NOT_EXECUTED = "confirmed_not_executed"
