from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetentionResult:
    operation_id: str
    messages_deleted: int
    summaries_deleted: int
    checkpoints_deleted: int
    attachments_deleted: int = 0
    audit_id: str = ""
