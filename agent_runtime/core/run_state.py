from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunLeaseLost(RuntimeError):
    """The caller no longer owns the run it attempted to mutate."""


class RunStateMachine:
    _sources = {
        RunStatus.RUNNING: {RunStatus.INTERRUPTED, RunStatus.WAITING_APPROVAL},
        RunStatus.WAITING_APPROVAL: {
            RunStatus.RUNNING,
            RunStatus.INTERRUPTED,
        },
        RunStatus.INTERRUPTED: {RunStatus.RUNNING},
        RunStatus.FAILED: {RunStatus.RUNNING},
        RunStatus.CANCELLED: {
            RunStatus.RUNNING,
            RunStatus.WAITING_APPROVAL,
            RunStatus.INTERRUPTED,
        },
    }

    @classmethod
    def sources_for(cls, target: RunStatus) -> frozenset[RunStatus]:
        return frozenset(cls._sources.get(target, set()))

    @classmethod
    def can_transition(cls, current: RunStatus, target: RunStatus) -> bool:
        return current in cls.sources_for(target)


@dataclass(frozen=True)
class RunRecord:
    id: str
    session_id: str
    inbound_platform: str
    inbound_message_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    owner_id: str | None = None
    lease_expires_at: datetime | None = None
    execution_token: int = 1


@dataclass(frozen=True)
class InboundStart:
    is_new: bool
    run: RunRecord
    cached_response: str | None = None


@dataclass(frozen=True)
class StoredMessage:
    id: int
    session_id: str
    run_id: str
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True)
class Checkpoint:
    id: int
    run_id: str
    sequence: int
    phase: str
    state: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class ToolClaim:
    should_execute: bool
    is_uncertain: bool = False
    output: str | None = None

