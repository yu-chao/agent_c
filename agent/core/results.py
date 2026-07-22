from __future__ import annotations

from dataclasses import dataclass

from agent.approval import ApprovalRequest


class Completed(str):
    """A completed turn. Subclassing str preserves existing CLI integrations."""

    @property
    def text(self) -> str:
        return str(self)


class InProgress(str):
    """A duplicate inbound message whose original run is still active."""

    def __new__(cls, run_id: str):
        value = super().__new__(cls, "Message is already being processed; retry later.")
        value.run_id = run_id
        return value


@dataclass(frozen=True)
class PendingApproval:
    request: ApprovalRequest

