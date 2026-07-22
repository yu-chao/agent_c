from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.admin.models import AdminConflictError

from .models import RetentionResult


_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class SQLiteRetentionRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS admin_operations (
                  operation_id TEXT PRIMARY KEY,
                  tenant_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                  action TEXT NOT NULL, resource_type TEXT NOT NULL,
                  resource_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  created_at TEXT NOT NULL, completed_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS admin_operations_tenant_idx
                  ON admin_operations(tenant_id, created_at);
                CREATE TABLE IF NOT EXISTS audit_events (
                  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                  actor_id TEXT NOT NULL, action TEXT NOT NULL,
                  resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
                  reason TEXT NOT NULL, operation_id TEXT NOT NULL,
                  outcome TEXT NOT NULL, created_at TEXT NOT NULL,
                  details_json TEXT NOT NULL,
                  UNIQUE(operation_id));
                CREATE INDEX IF NOT EXISTS audit_events_tenant_created_idx
                  ON audit_events(tenant_id, created_at, id);
                CREATE TABLE IF NOT EXISTS attachments (
                  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                  session_id TEXT NOT NULL, storage_key TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(session_id) REFERENCES sessions(id));
                CREATE INDEX IF NOT EXISTS attachments_tenant_created_idx
                  ON attachments(tenant_id, created_at, id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def purge_tenant_data(
        self,
        *,
        tenant_id: str,
        before: datetime,
        actor_id: str,
        reason: str,
        operation_id: str,
        request_hash: str,
    ) -> RetentionResult:
        cutoff = before.astimezone(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM admin_operations "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    tenant_id, actor_id, "retention.purge", "tenant",
                    tenant_id, request_hash,
                )
                actual = tuple(existing[name] for name in (
                    "tenant_id", "actor_id", "action", "resource_type",
                    "resource_id", "request_hash",
                ))
                if actual != expected:
                    raise AdminConflictError(
                        "operation_id is already bound to another request"
                    )
                return _result(json.loads(existing["result_json"]))

            terminal = _TERMINAL_STATUSES
            summaries_deleted = db.execute(
                "DELETE FROM session_summaries WHERE through_message_id IN ("
                "SELECT m.id FROM messages m JOIN sessions s ON s.id=m.session_id "
                "WHERE s.tenant_id=? AND m.created_at<? AND NOT EXISTS ("
                "SELECT 1 FROM runs active WHERE active.session_id=s.id "
                "AND active.status NOT IN (?,?,?)))",
                (tenant_id, cutoff, *terminal),
            ).rowcount
            messages_deleted = db.execute(
                "DELETE FROM messages WHERE id IN ("
                "SELECT m.id FROM messages m JOIN sessions s ON s.id=m.session_id "
                "WHERE s.tenant_id=? AND m.created_at<? AND NOT EXISTS ("
                "SELECT 1 FROM runs active WHERE active.session_id=s.id "
                "AND active.status NOT IN (?,?,?)))",
                (tenant_id, cutoff, *terminal),
            ).rowcount
            checkpoints_deleted = db.execute(
                "DELETE FROM checkpoints WHERE id IN ("
                "SELECT c.id FROM checkpoints c JOIN runs r ON r.id=c.run_id "
                "JOIN sessions s ON s.id=r.session_id WHERE s.tenant_id=? "
                "AND c.created_at<? AND NOT EXISTS (SELECT 1 FROM runs active "
                "WHERE active.session_id=s.id AND active.status NOT IN (?,?,?)))",
                (tenant_id, cutoff, *terminal),
            ).rowcount
            attachments_deleted = db.execute(
                "DELETE FROM attachments WHERE id IN ("
                "SELECT a.id FROM attachments a "
                "JOIN sessions s ON s.id=a.session_id "
                "WHERE a.tenant_id=? AND s.tenant_id=? "
                "AND a.created_at<? AND NOT EXISTS ("
                "SELECT 1 FROM runs active WHERE active.session_id=s.id "
                "AND active.status NOT IN (?,?,?)))",
                (tenant_id, tenant_id, cutoff, *terminal),
            ).rowcount

            audit_id = f"audit_{uuid.uuid4().hex}"
            result = RetentionResult(
                operation_id=operation_id,
                messages_deleted=messages_deleted,
                summaries_deleted=summaries_deleted,
                checkpoints_deleted=checkpoints_deleted,
                attachments_deleted=attachments_deleted,
                audit_id=audit_id,
            )
            result_json = json.dumps(
                result.__dict__, ensure_ascii=False, separators=(",", ":")
            )
            db.execute(
                "INSERT INTO admin_operations("
                "operation_id,tenant_id,actor_id,action,resource_type,"
                "resource_id,request_hash,result_json,created_at,completed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id, tenant_id, actor_id, "retention.purge",
                    "tenant", tenant_id, request_hash, result_json, now, now,
                ),
            )
            db.execute(
                "INSERT INTO audit_events("
                "id,tenant_id,actor_id,action,resource_type,resource_id,reason,"
                "operation_id,outcome,created_at,details_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    audit_id, tenant_id, actor_id, "retention.purge", "tenant",
                    tenant_id, reason, operation_id, "completed", now,
                    result_json,
                ),
            )
            return result


class PostgresRetentionRepository:
    def __init__(self, dsn: str, *, migrate: bool = True) -> None:
        self.dsn = dsn
        if migrate:
            from agent.migrations import apply_postgres_migrations

            apply_postgres_migrations(dsn)

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "PostgreSQL storage requires: uv sync --extra postgres"
            ) from exc
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def purge_tenant_data(
        self,
        *,
        tenant_id: str,
        before: datetime,
        actor_id: str,
        reason: str,
        operation_id: str,
        request_hash: str,
    ) -> RetentionResult:
        from psycopg.types.json import Jsonb

        cutoff = before.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        terminal = _TERMINAL_STATUSES
        with self._connect() as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (operation_id,),
            )
            existing = db.execute(
                "SELECT request_hash,result_json,tenant_id,actor_id,action,"
                "resource_type,resource_id FROM admin_operations "
                "WHERE operation_id=%s FOR UPDATE",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                actual = tuple(existing[name] for name in (
                    "request_hash", "tenant_id", "actor_id", "action",
                    "resource_type", "resource_id",
                ))
                expected = (
                    request_hash, tenant_id, actor_id, "retention.purge",
                    "tenant", tenant_id,
                )
                if actual != expected:
                    raise AdminConflictError(
                        "operation_id is already bound to another request"
                    )
                value = existing["result_json"]
                if isinstance(value, str):
                    value = json.loads(value)
                return _result(value)

            summaries_deleted = len(db.execute(
                "DELETE FROM session_summaries ss USING messages m,sessions s "
                "WHERE ss.through_message_id=m.id AND m.session_id=s.id "
                "AND s.tenant_id=%s AND m.created_at<%s AND NOT EXISTS ("
                "SELECT 1 FROM runs active WHERE active.session_id=s.id "
                "AND active.status NOT IN (%s,%s,%s)) RETURNING ss.id",
                (tenant_id, cutoff, *terminal),
            ).fetchall())
            messages_deleted = len(db.execute(
                "DELETE FROM messages m USING sessions s "
                "WHERE m.session_id=s.id AND s.tenant_id=%s "
                "AND m.created_at<%s AND NOT EXISTS (SELECT 1 FROM runs active "
                "WHERE active.session_id=s.id "
                "AND active.status NOT IN (%s,%s,%s)) RETURNING m.id",
                (tenant_id, cutoff, *terminal),
            ).fetchall())
            checkpoints_deleted = len(db.execute(
                "DELETE FROM checkpoints c USING runs r,sessions s "
                "WHERE c.run_id=r.id AND r.session_id=s.id "
                "AND s.tenant_id=%s AND c.created_at<%s "
                "AND NOT EXISTS (SELECT 1 FROM runs active "
                "WHERE active.session_id=s.id "
                "AND active.status NOT IN (%s,%s,%s)) RETURNING c.id",
                (tenant_id, cutoff, *terminal),
            ).fetchall())
            attachments_deleted = len(db.execute(
                "DELETE FROM attachments a USING sessions s "
                "WHERE a.session_id=s.id AND a.tenant_id=%s "
                "AND s.tenant_id=%s AND a.created_at<%s "
                "AND NOT EXISTS (SELECT 1 FROM runs active "
                "WHERE active.session_id=s.id "
                "AND active.status NOT IN (%s,%s,%s)) RETURNING a.id",
                (tenant_id, tenant_id, cutoff, *terminal),
            ).fetchall())

            audit_id = f"audit_{uuid.uuid4().hex}"
            result = RetentionResult(
                operation_id=operation_id,
                messages_deleted=messages_deleted,
                summaries_deleted=summaries_deleted,
                checkpoints_deleted=checkpoints_deleted,
                attachments_deleted=attachments_deleted,
                audit_id=audit_id,
            )
            value = dict(result.__dict__)
            db.execute(
                "INSERT INTO admin_operations("
                "operation_id,tenant_id,actor_id,action,resource_type,"
                "resource_id,request_hash,result_json,created_at,completed_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    operation_id, tenant_id, actor_id, "retention.purge",
                    "tenant", tenant_id, request_hash, Jsonb(value), now, now,
                ),
            )
            db.execute(
                "INSERT INTO audit_events("
                "id,tenant_id,actor_id,action,resource_type,resource_id,reason,"
                "operation_id,outcome,created_at,details_json) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    audit_id, tenant_id, actor_id, "retention.purge", "tenant",
                    tenant_id, reason, operation_id, "completed", now,
                    Jsonb(value),
                ),
            )
            return result


def _result(value: dict[str, object]) -> RetentionResult:
    return RetentionResult(
        operation_id=str(value["operation_id"]),
        messages_deleted=int(value["messages_deleted"]),
        summaries_deleted=int(value["summaries_deleted"]),
        checkpoints_deleted=int(value["checkpoints_deleted"]),
        attachments_deleted=int(value.get("attachments_deleted", 0)),
        audit_id=str(value.get("audit_id", "")),
    )
