from __future__ import annotations

import re
from datetime import datetime

from .models import MemoryAccess, MemoryRecord, RetrievedMemory


class MemoryRetriever:
    def __init__(self, store, *, max_results: int = 5):
        self.store = store
        self.max_results = max_results

    def retrieve(
        self,
        query: str,
        access: MemoryAccess,
        *,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> list[RetrievedMemory]:
        candidates = self.store.list_visible(access, now=now)
        query_tokens = _tokens(query)
        ranked = []
        for memory in candidates:
            score = _score(query, query_tokens, memory)
            if score > 0:
                ranked.append(RetrievedMemory(memory, score))
        ranked.sort(
            key=lambda item: (
                item.score,
                item.memory.confidence,
                item.memory.created_at,
            ),
            reverse=True,
        )
        return ranked[: max(0, limit if limit is not None else self.max_results)]


def _score(query: str, query_tokens: set[str], memory: MemoryRecord) -> float:
    content = memory.content.casefold()
    content_tokens = _tokens(content)
    overlap = len(query_tokens & content_tokens)
    if not query_tokens:
        return memory.confidence
    phrase = 1.0 if query.strip().casefold() in content else 0.0
    return (overlap / len(query_tokens) + phrase) * memory.confidence


def _tokens(value: str) -> set[str]:
    normalized = value.casefold()
    tokens = set(re.findall(r"[a-z0-9_]+", normalized))
    chunks = re.findall(r"[\u3400-\u9fff]+", normalized)
    for chunk in chunks:
        tokens.update(chunk)
        tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return {token for token in tokens if token}
