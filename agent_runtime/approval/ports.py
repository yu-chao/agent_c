from __future__ import annotations

from datetime import datetime
from typing import Protocol

from agent_runtime.approval.models import (
    ApprovalDecision,
    ApprovalRequest,
    RuntimeIdentity,
)


class ApprovalRepository(Protocol):
    def create(self, item: ApprovalRequest) -> ApprovalRequest: ...

    def get(self, approval_id: str) -> ApprovalRequest | None: ...

    def decide(
        self,
        approval_id: str,
        action: str,
        identity: RuntimeIdentity,
        event_message_id: str,
    ) -> ApprovalDecision: ...

    def expire_pending(
        self,
        *,
        now: datetime | None = None,
    ) -> list[ApprovalRequest]: ...

    def claim_execution(self, approval_id: str) -> ApprovalRequest | None: ...

    def complete(self, approval_id: str): ...

    def fail(self, approval_id: str, error: str): ...

    def mark_consumed(self, approval_id: str) -> bool: ...

    def list_resumable(self) -> list[ApprovalRequest]: ...

    def list_uncertain(self) -> list[ApprovalRequest]: ...

    def list_for_admin(
        self, *, tenant_id: str | None, status: str | None, limit: int
    ) -> list[ApprovalRequest]: ...
