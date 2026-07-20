from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class MemoryVisibility(StrEnum):
    PRIVATE = "private"
    CONVERSATION = "conversation"
    TENANT = "tenant"


class MemorySourceKind(StrEnum):
    USER_MESSAGE = "user_message"
    USER_CORRECTION = "user_correction"


@dataclass(frozen=True)
class MemoryAccess:
    subject: str
    conversation_id: str | None = None
    tenant_id: str | None = None


@dataclass(frozen=True)
class MemorySource:
    kind: MemorySourceKind
    platform: str
    message_id: str
    subject: str
    session_id: str | None = None


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    root_id: str
    subject: str
    content: str
    source: MemorySource
    confidence: float
    created_at: datetime
    expires_at: datetime | None
    visibility: MemoryVisibility
    conversation_id: str | None = None
    tenant_id: str | None = None
    deleted_at: datetime | None = None
    superseded_by_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        subject: str,
        content: str,
        source: MemorySource,
        confidence: float = 1.0,
        expires_at: datetime | None = None,
        visibility: MemoryVisibility = MemoryVisibility.PRIVATE,
        conversation_id: str | None = None,
        tenant_id: str | None = None,
        now: datetime | None = None,
    ) -> "MemoryRecord":
        subject = subject.strip()
        content = content.strip()
        if not subject:
            raise ValueError("memory subject must not be empty")
        if not content:
            raise ValueError("memory content must not be empty")
        if source.subject != subject:
            raise ValueError("memory source subject must match memory subject")
        if not isinstance(source.kind, MemorySourceKind):
            raise ValueError(
                "memory source must be an explicit user message or correction"
            )
        if not source.platform.strip() or not source.message_id.strip():
            raise ValueError("memory source platform and message_id are required")
        if not 0 <= confidence <= 1:
            raise ValueError("memory confidence must be between 0 and 1")
        if visibility is MemoryVisibility.CONVERSATION and not conversation_id:
            raise ValueError("conversation visibility requires conversation_id")
        if visibility is MemoryVisibility.TENANT and not tenant_id:
            raise ValueError("tenant visibility requires tenant_id")
        created_at = now or datetime.now(timezone.utc)
        if expires_at is not None and expires_at <= created_at:
            raise ValueError("memory expires_at must be later than created_at")
        memory_id = f"memory_{uuid.uuid4().hex}"
        return cls(
            id=memory_id,
            root_id=memory_id,
            subject=subject,
            content=content,
            source=source,
            confidence=float(confidence),
            created_at=created_at,
            expires_at=expires_at,
            visibility=visibility,
            conversation_id=conversation_id,
            tenant_id=tenant_id,
        )


@dataclass(frozen=True)
class RetrievedMemory:
    memory: MemoryRecord
    score: float

    @property
    def citation(self) -> str:
        source = self.memory.source
        return f"{source.platform}/{source.message_id}"
