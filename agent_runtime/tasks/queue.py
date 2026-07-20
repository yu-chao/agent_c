from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agent_runtime.migrations import apply_postgres_migrations


@dataclass(frozen=True)
class QueuedRun:
    run_id: str
    attempts: int


class PostgresRunQueue:
    """仅投递 Run ID；消费者必须再从 SessionRepository 校验真实状态。"""

    def __init__(self, dsn: str, *, migrate: bool = True):
        self.dsn = dsn
        if migrate:
            apply_postgres_migrations(dsn)

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "PostgreSQL queue requires: uv sync --extra postgres"
            ) from exc
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def enqueue(
        self, run_id: str, *, available_at: datetime | None = None
    ) -> bool:
        with self._connect() as db:
            changed = db.execute(
                "INSERT INTO run_queue(run_id,available_at) VALUES(%s,%s) "
                "ON CONFLICT(run_id) DO NOTHING",
                (run_id, available_at or datetime.now(timezone.utc)),
            ).rowcount
        return changed == 1

    def claim(
        self, owner_id: str, *, lease_seconds: int = 30
    ) -> QueuedRun | None:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as db:
            row = db.execute(
                "WITH candidate AS ("
                "SELECT run_id FROM run_queue WHERE available_at<=%s AND "
                "(lease_expires_at IS NULL OR lease_expires_at<=%s) "
                "ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE run_queue queue SET owner_id=%s,lease_expires_at=%s,"
                "attempts=attempts+1 FROM candidate "
                "WHERE queue.run_id=candidate.run_id "
                "RETURNING queue.run_id,queue.attempts",
                (now, now, owner_id, expires),
            ).fetchone()
        return QueuedRun(row["run_id"], row["attempts"]) if row else None

    def acknowledge(self, run_id: str, owner_id: str) -> bool:
        with self._connect() as db:
            changed = db.execute(
                "DELETE FROM run_queue WHERE run_id=%s AND owner_id=%s",
                (run_id, owner_id),
            ).rowcount
        return changed == 1

    def release(
        self, run_id: str, owner_id: str, *, delay_seconds: float = 0
    ) -> bool:
        available = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        with self._connect() as db:
            changed = db.execute(
                "UPDATE run_queue SET owner_id=NULL,lease_expires_at=NULL,"
                "available_at=%s WHERE run_id=%s AND owner_id=%s",
                (available, run_id, owner_id),
            ).rowcount
        return changed == 1
