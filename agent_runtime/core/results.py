from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.approval import ApprovalRequest


class Completed(str):
    """A completed turn. Subclassing str preserves existing CLI integrations."""

    @property
    def text(self) -> str:
        return str(self)


@dataclass(frozen=True)
class PendingApproval:
    request: ApprovalRequest

