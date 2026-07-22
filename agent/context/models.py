from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


class ContextOverflowError(ValueError):
    """固定开销或不可分割的当前消息无法放入输入窗口。"""


@dataclass(frozen=True)
class SessionSummary:
    id: int
    session_id: str
    version: int
    content: str
    through_message_id: int
    created_at: datetime


@dataclass(frozen=True)
class ContextBuildResult:
    messages: list[dict[str, Any]]
    summary_version: int | None = None
