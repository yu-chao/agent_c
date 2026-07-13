from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class ApprovalAction(StrEnum):
    CONFIRM = "approval.confirm"
    REJECT = "approval.reject"


@dataclass(frozen=True)
class RuntimeIdentity:
    platform: str
    conversation_id: str
    sender_id: str
    message_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    identity: RuntimeIdentity
    tool_call_id: str
    tool_name: str
    tool_input: dict[str, Any]
    arguments_hash: str
    continuation: dict[str, Any]
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    error: str | None = None
    resumed_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        identity: RuntimeIdentity,
        tool_call_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        continuation: dict[str, Any],
        timeout_seconds: int,
        now: datetime | None = None,
    ) -> "ApprovalRequest":
        created_at = now or datetime.now(timezone.utc)
        return cls(
            id=f"approval_{uuid.uuid4().hex}",
            identity=identity,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_input=tool_input,
            arguments_hash=hash_tool_arguments(tool_name, tool_input),
            continuation=continuation,
            status=ApprovalStatus.PENDING,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=timeout_seconds),
        )


@dataclass(frozen=True)
class ApprovalDecision:
    accepted: bool
    status: ApprovalStatus | None
    message: str
    request: ApprovalRequest | None = None


def hash_tool_arguments(tool_name: str, tool_input: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"tool": tool_name, "input": tool_input},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
