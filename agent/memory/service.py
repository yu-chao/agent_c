from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import (
    MemoryAccess,
    MemoryRecord,
    MemorySource,
    MemorySourceKind,
    MemoryVisibility,
    RetrievedMemory,
)
from .retrieval import MemoryRetriever
from .store import MemoryRepository


class MemoryService:
    """显式管理用户陈述；绝不从模型输出自动提取事实。"""

    def __init__(
        self,
        store: MemoryRepository,
        *,
        default_ttl_days: int | None = 365,
        max_results: int = 5,
    ):
        self.store = store
        self.default_ttl_days = default_ttl_days
        self.retriever = MemoryRetriever(store, max_results=max_results)

    def remember_user_statement(
        self,
        identity: Any,
        content: str,
        *,
        session_id: str | None = None,
        confidence: float = 1.0,
        expires_at: datetime | None = None,
        visibility: MemoryVisibility | str = MemoryVisibility.PRIVATE,
        now: datetime | None = None,
    ) -> MemoryRecord:
        current = now or datetime.now(timezone.utc)
        access = self.access_for(identity)
        selected_visibility = MemoryVisibility(visibility)
        expiry = expires_at
        if expiry is None and self.default_ttl_days is not None:
            expiry = current + timedelta(days=self.default_ttl_days)
        memory = MemoryRecord.create(
            subject=access.subject,
            content=content,
            source=self.source_for(
                identity, MemorySourceKind.USER_MESSAGE, session_id=session_id
            ),
            confidence=confidence,
            expires_at=expiry,
            visibility=selected_visibility,
            conversation_id=(
                access.conversation_id
                if selected_visibility is MemoryVisibility.CONVERSATION else None
            ),
            tenant_id=(
                access.tenant_id
                if selected_visibility is MemoryVisibility.TENANT else None
            ),
            now=current,
        )
        return self.store.create(memory)

    def what_is_remembered(
        self, identity: Any, *, now: datetime | None = None
    ) -> list[MemoryRecord]:
        return self.store.list_owned(self.access_for(identity), now=now)

    def correct(
        self,
        identity: Any,
        memory_id: str,
        content: str,
        *,
        session_id: str | None = None,
        confidence: float = 1.0,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> MemoryRecord:
        access = self.access_for(identity)
        existing = self.store.get(memory_id)
        if existing is None or existing.subject != access.subject:
            raise KeyError(f"memory not found: {memory_id}")
        current = now or datetime.now(timezone.utc)
        expiry = expires_at
        if expiry is None and self.default_ttl_days is not None:
            expiry = current + timedelta(days=self.default_ttl_days)
        replacement = MemoryRecord.create(
            subject=access.subject,
            content=content,
            source=self.source_for(
                identity, MemorySourceKind.USER_CORRECTION,
                session_id=session_id,
            ),
            confidence=confidence,
            expires_at=expiry,
            visibility=existing.visibility,
            conversation_id=existing.conversation_id,
            tenant_id=existing.tenant_id,
            now=current,
        )
        return self.store.replace(memory_id, replacement, access)

    def forget(self, identity: Any, memory_id: str) -> bool:
        """物理删除整条纠正链，确保旧值也不会残留。"""
        return self.store.hard_delete(memory_id, self.access_for(identity))

    def retrieve(
        self, identity: Any, query: str, *, now: datetime | None = None
    ) -> list[RetrievedMemory]:
        return self.retriever.retrieve(
            query, self.access_for(identity), now=now
        )

    def purge_expired(self, *, now: datetime | None = None) -> int:
        return self.store.purge_expired(now=now)

    @staticmethod
    def access_for(identity: Any) -> MemoryAccess:
        metadata = getattr(identity, "metadata", {}) or {}
        platform = str(identity.platform)
        return MemoryAccess(
            subject=_identity_key("subject", platform, identity.sender_id),
            conversation_id=_identity_key(
                "conversation", platform, identity.conversation_id
            ),
            tenant_id=(
                _identity_key("tenant", platform, metadata["tenant_id"])
                if metadata.get("tenant_id") is not None else None
            ),
        )

    @classmethod
    def source_for(
        cls,
        identity: Any,
        kind: MemorySourceKind,
        *,
        session_id: str | None = None,
    ) -> MemorySource:
        access = cls.access_for(identity)
        return MemorySource(
            kind=kind,
            platform=str(identity.platform),
            message_id=str(identity.message_id),
            subject=access.subject,
            session_id=session_id,
        )


def _identity_key(kind: str, platform: object, value: object) -> str:
    encoded = json.dumps(
        [str(platform), str(value)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"{kind}_{digest}"
