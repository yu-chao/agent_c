from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agent.migrations import apply_postgres_migrations

from .models import (
    MemoryAccess,
    MemoryRecord,
    MemorySource,
    MemorySourceKind,
    MemoryVisibility,
)


class MemoryRepository(Protocol):
    def create(self, memory: MemoryRecord) -> MemoryRecord: ...
    def get(self, memory_id: str) -> MemoryRecord | None: ...
    def list_owned(
        self, access: MemoryAccess, *, now: datetime | None = None
    ) -> list[MemoryRecord]: ...
    def list_visible(
        self, access: MemoryAccess, *, now: datetime | None = None
    ) -> list[MemoryRecord]: ...
    def replace(
        self, memory_id: str, replacement: MemoryRecord, access: MemoryAccess
    ) -> MemoryRecord: ...
    def hard_delete(self, memory_id: str, access: MemoryAccess) -> bool: ...
    def purge_expired(self, *, now: datetime | None = None) -> int: ...


class SQLiteMemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                  id TEXT PRIMARY KEY, root_id TEXT NOT NULL,
                  subject TEXT NOT NULL, content TEXT NOT NULL,
                  source_kind TEXT NOT NULL, source_platform TEXT NOT NULL,
                  source_message_id TEXT NOT NULL,
                  source_subject TEXT NOT NULL, source_session_id TEXT,
                  confidence REAL NOT NULL, created_at TEXT NOT NULL,
                  expires_at TEXT, visibility TEXT NOT NULL,
                  conversation_id TEXT, tenant_id TEXT, deleted_at TEXT,
                  superseded_by_id TEXT,
                  FOREIGN KEY(superseded_by_id) REFERENCES memories(id));
                CREATE INDEX IF NOT EXISTS memories_subject_active_idx
                  ON memories(subject,deleted_at,expires_at);
                CREATE INDEX IF NOT EXISTS memories_visibility_idx
                  ON memories(visibility,conversation_id,tenant_id);
                CREATE INDEX IF NOT EXISTS memories_root_idx ON memories(root_id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create(self, memory: MemoryRecord) -> MemoryRecord:
        with self._connect() as db:
            _sqlite_insert(db, memory)
        return memory

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM memories WHERE id=?", (memory_id,)
            ).fetchone()
        return _memory(row) if row else None

    def list_owned(
        self, access: MemoryAccess, *, now: datetime | None = None
    ) -> list[MemoryRecord]:
        current = _time(now or _now())
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE subject=? AND deleted_at IS NULL "
                "AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC",
                (access.subject, current),
            ).fetchall()
        return [_memory(row) for row in rows]

    def list_visible(
        self, access: MemoryAccess, *, now: datetime | None = None
    ) -> list[MemoryRecord]:
        current = _time(now or _now())
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE deleted_at IS NULL "
                "AND (expires_at IS NULL OR expires_at>?) AND ("
                "(visibility='private' AND subject=?) OR "
                "(visibility='conversation' AND conversation_id=?) OR "
                "(visibility='tenant' AND tenant_id IS NOT NULL AND tenant_id=?)) "
                "ORDER BY created_at DESC",
                (current, access.subject, access.conversation_id,
                 access.tenant_id),
            ).fetchall()
        return [_memory(row) for row in rows]

    def replace(
        self, memory_id: str, replacement: MemoryRecord, access: MemoryAccess
    ) -> MemoryRecord:
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM memories WHERE id=? AND subject=? "
                "AND deleted_at IS NULL",
                (memory_id, access.subject),
            ).fetchone()
            if row is None:
                raise KeyError(f"active memory not found: {memory_id}")
            current = _memory(row)
            replacement = replace(replacement, root_id=current.root_id)
            _sqlite_insert(db, replacement)
            changed = db.execute(
                "UPDATE memories SET deleted_at=?,superseded_by_id=? "
                "WHERE id=? AND deleted_at IS NULL",
                (_time(now), replacement.id, memory_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("memory changed concurrently")
        return replacement

    def hard_delete(self, memory_id: str, access: MemoryAccess) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT root_id FROM memories WHERE id=? AND subject=?",
                (memory_id, access.subject),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                "UPDATE memories SET superseded_by_id=NULL WHERE root_id=?",
                (row["root_id"],),
            )
            return db.execute(
                "DELETE FROM memories WHERE root_id=? AND subject=?",
                (row["root_id"], access.subject),
            ).rowcount > 0

    def purge_expired(self, *, now: datetime | None = None) -> int:
        current = _time(now or _now())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE memories SET superseded_by_id=NULL WHERE "
                "superseded_by_id IN (SELECT id FROM memories WHERE "
                "expires_at IS NOT NULL AND expires_at<=?)",
                (current,),
            )
            return db.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL "
                "AND expires_at<=?", (current,)
            ).rowcount


class PostgresMemoryStore:
    def __init__(self, dsn: str, *, migrate: bool = True):
        if not dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        self.dsn = dsn
        if migrate:
            apply_postgres_migrations(dsn)

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "PostgreSQL memory requires: uv sync --extra postgres"
            ) from exc
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def create(self, memory: MemoryRecord) -> MemoryRecord:
        with self._connect() as db:
            _postgres_insert(db, memory)
        return memory

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM memories WHERE id=%s", (memory_id,)
            ).fetchone()
        return _memory(row) if row else None

    def list_owned(
        self, access: MemoryAccess, *, now: datetime | None = None
    ) -> list[MemoryRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE subject=%s AND deleted_at IS NULL "
                "AND (expires_at IS NULL OR expires_at>%s) ORDER BY created_at DESC",
                (access.subject, now or _now()),
            ).fetchall()
        return [_memory(row) for row in rows]

    def list_visible(
        self, access: MemoryAccess, *, now: datetime | None = None
    ) -> list[MemoryRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE deleted_at IS NULL "
                "AND (expires_at IS NULL OR expires_at>%s) AND ("
                "(visibility='private' AND subject=%s) OR "
                "(visibility='conversation' AND conversation_id=%s) OR "
                "(visibility='tenant' AND tenant_id IS NOT NULL AND tenant_id=%s)) "
                "ORDER BY created_at DESC",
                (now or _now(), access.subject, access.conversation_id,
                 access.tenant_id),
            ).fetchall()
        return [_memory(row) for row in rows]

    def replace(
        self, memory_id: str, replacement: MemoryRecord, access: MemoryAccess
    ) -> MemoryRecord:
        now = _now()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM memories WHERE id=%s AND subject=%s "
                "AND deleted_at IS NULL FOR UPDATE",
                (memory_id, access.subject),
            ).fetchone()
            if row is None:
                raise KeyError(f"active memory not found: {memory_id}")
            current = _memory(row)
            replacement = replace(replacement, root_id=current.root_id)
            _postgres_insert(db, replacement)
            changed = db.execute(
                "UPDATE memories SET deleted_at=%s,superseded_by_id=%s "
                "WHERE id=%s AND deleted_at IS NULL",
                (now, replacement.id, memory_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("memory changed concurrently")
        return replacement

    def hard_delete(self, memory_id: str, access: MemoryAccess) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT root_id FROM memories WHERE id=%s AND subject=%s FOR UPDATE",
                (memory_id, access.subject),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                "UPDATE memories SET superseded_by_id=NULL WHERE root_id=%s",
                (row["root_id"],),
            )
            return db.execute(
                "DELETE FROM memories WHERE root_id=%s AND subject=%s",
                (row["root_id"], access.subject),
            ).rowcount > 0

    def purge_expired(self, *, now: datetime | None = None) -> int:
        current = now or _now()
        with self._connect() as db:
            db.execute(
                "UPDATE memories SET superseded_by_id=NULL WHERE "
                "superseded_by_id IN (SELECT id FROM memories WHERE "
                "expires_at IS NOT NULL AND expires_at<=%s)",
                (current,),
            )
            return db.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL "
                "AND expires_at<=%s", (current,)
            ).rowcount


_COLUMNS = (
    "id,root_id,subject,content,source_kind,source_platform,"
    "source_message_id,source_subject,source_session_id,confidence,"
    "created_at,expires_at,visibility,conversation_id,tenant_id,"
    "deleted_at,superseded_by_id"
)


def _values(memory: MemoryRecord) -> tuple[object, ...]:
    source = memory.source
    return (
        memory.id, memory.root_id, memory.subject, memory.content,
        source.kind.value, source.platform, source.message_id, source.subject,
        source.session_id, memory.confidence, memory.created_at,
        memory.expires_at, memory.visibility.value, memory.conversation_id,
        memory.tenant_id, memory.deleted_at, memory.superseded_by_id,
    )


def _sqlite_insert(db: sqlite3.Connection, memory: MemoryRecord) -> None:
    values = tuple(
        _time(value) if isinstance(value, datetime) else value
        for value in _values(memory)
    )
    db.execute(
        f"INSERT INTO memories({_COLUMNS}) VALUES({','.join('?' for _ in values)})",
        values,
    )


def _postgres_insert(db, memory: MemoryRecord) -> None:
    values = _values(memory)
    db.execute(
        f"INSERT INTO memories({_COLUMNS}) "
        f"VALUES({','.join('%s' for _ in values)})",
        values,
    )


def _memory(row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"], root_id=row["root_id"], subject=row["subject"],
        content=row["content"],
        source=MemorySource(
            kind=MemorySourceKind(row["source_kind"]),
            platform=row["source_platform"],
            message_id=row["source_message_id"],
            subject=row["source_subject"],
            session_id=row["source_session_id"],
        ),
        confidence=float(row["confidence"]),
        created_at=_datetime(row["created_at"]),
        expires_at=_datetime(row["expires_at"]) if row["expires_at"] else None,
        visibility=MemoryVisibility(row["visibility"]),
        conversation_id=row["conversation_id"], tenant_id=row["tenant_id"],
        deleted_at=_datetime(row["deleted_at"]) if row["deleted_at"] else None,
        superseded_by_id=row["superseded_by_id"],
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _datetime(value: datetime | str) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)
