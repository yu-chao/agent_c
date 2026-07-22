from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.admin.models import (
    AdminCheckpoint,
    AdminCommandResult,
    AdminConflictError,
    AdminRun,
    AdminSession,
    AdminToolExecution,
    AuditEvent,
    ToolDisposition,
)


class SQLiteAdminRepository:
    """SQLite 管理读模型、幂等命令与追加式审计适配器。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(sessions)")
            }
            if columns and "tenant_id" not in columns:
                db.execute(
                    "ALTER TABLE sessions ADD COLUMN tenant_id TEXT "
                    "NOT NULL DEFAULT 'default'"
                )
            db.executescript("""
                CREATE TABLE IF NOT EXISTS admin_operations (
                  operation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  action TEXT NOT NULL, resource_type TEXT NOT NULL,
                  resource_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                  result_json TEXT NOT NULL, created_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit_events (
                  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  action TEXT NOT NULL, resource_type TEXT NOT NULL,
                  resource_id TEXT NOT NULL, reason TEXT NOT NULL,
                  operation_id TEXT NOT NULL UNIQUE, outcome TEXT NOT NULL,
                  created_at TEXT NOT NULL, details_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS audit_events_tenant_time_idx
                  ON audit_events(tenant_id,created_at,id);
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def admin_get_run(self, run_id: str) -> AdminRun | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT r.*,s.tenant_id FROM runs r JOIN sessions s "
                "ON s.id=r.session_id WHERE r.id=?",
                (run_id,),
            ).fetchone()
        return _run(row) if row else None

    def admin_list_sessions(self, *, tenant_id, before, limit):
        where, values = [], []
        if tenant_id is not None:
            where.append("tenant_id=?")
            values.append(tenant_id)
        if before is not None:
            where.append("created_at<?")
            values.append(_time(before))
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM sessions" + clause
                + " ORDER BY created_at DESC,id DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_session(row) for row in rows]

    def admin_list_runs(
        self, *, tenant_id, session_id, status, before, limit,
    ):
        where, values = [], []
        if tenant_id is not None:
            where.append("s.tenant_id=?")
            values.append(tenant_id)
        if session_id is not None:
            where.append("r.session_id=?")
            values.append(session_id)
        if status is not None:
            where.append("r.status=?")
            values.append(status)
        if before is not None:
            where.append("r.created_at<?")
            values.append(_time(before))
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT r.*,s.tenant_id FROM runs r JOIN sessions s "
                "ON s.id=r.session_id" + clause
                + " ORDER BY r.created_at DESC,r.id DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_run(row) for row in rows]

    def admin_list_checkpoints(self, run_id: str, *, limit: int):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,run_id,sequence,phase,created_at FROM checkpoints "
                "WHERE run_id=? ORDER BY sequence DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        return [
            AdminCheckpoint(
                id=row["id"], run_id=row["run_id"],
                sequence=row["sequence"], phase=row["phase"],
                created_at=_datetime(row["created_at"]),
            )
            for row in rows
        ]

    def admin_list_tool_executions(self, run_id: str, *, status, limit):
        where = "run_id=?"
        values: list[Any] = [run_id]
        if status is not None:
            where += " AND status=?"
            values.append(status)
        with self._connect() as db:
            rows = db.execute(
                "SELECT run_id,call_id,tool_name,status,started_at,"
                "finished_at,error FROM tool_executions "
                f"WHERE {where} ORDER BY started_at DESC,call_id DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_tool(row) for row in rows]

    def admin_control_run(
        self, *, tenant_id, run_id, action, actor_id, reason, operation_id,
        request_hash,
    ):
        full_action = f"run.{action}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._existing_operation(
                db, operation_id, request_hash, tenant_id, actor_id,
                full_action, "run", run_id,
            )
            if previous:
                return previous
            row = db.execute(
                "SELECT r.status FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=? AND s.tenant_id=?",
                (run_id, tenant_id),
            ).fetchone()
            if row is None:
                outcome, changed = "not_found", False
            else:
                current = row["status"]
                target, sources = _run_transition(action)
                if current == target or (
                    action == "recover" and current == "interrupted"
                ):
                    outcome, changed = "already_applied", False
                elif current not in sources:
                    outcome, changed = "invalid_state", False
                else:
                    changed = db.execute(
                        "UPDATE runs SET status=?,updated_at=?,owner_id=NULL,"
                        "lease_expires_at=NULL,execution_token=execution_token+1 "
                        "WHERE id=? AND status=?",
                        (target, _now(), run_id, current),
                    ).rowcount == 1
                    outcome = "succeeded" if changed else "conflict"
            return self._record(
                db, operation_id=operation_id, request_hash=request_hash,
                tenant_id=tenant_id, actor_id=actor_id, action=full_action,
                resource_type="run", resource_id=run_id, reason=reason,
                outcome=outcome, changed=changed,
                details={"sensitive_fields_omitted": True},
            )

    def admin_resolve_tool(
        self, *, tenant_id, run_id, call_id, disposition, output, actor_id,
        reason, operation_id, request_hash,
    ):
        action = f"tool.{disposition.value}"
        resource_id = f"{run_id}:{call_id}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._existing_operation(
                db, operation_id, request_hash, tenant_id, actor_id, action,
                "tool_execution", resource_id,
            )
            if previous:
                return previous
            row = db.execute(
                "SELECT t.status FROM tool_executions t "
                "JOIN runs r ON r.id=t.run_id "
                "JOIN sessions s ON s.id=r.session_id "
                "WHERE t.run_id=? AND t.call_id=? AND s.tenant_id=?",
                (run_id, call_id, tenant_id),
            ).fetchone()
            if row is None:
                outcome, changed = "not_found", False
            elif row["status"] != "running":
                outcome, changed = "already_resolved", False
            elif disposition is ToolDisposition.CONFIRMED_SUCCEEDED:
                changed = db.execute(
                    "UPDATE tool_executions SET status='completed',output=?,"
                    "error=NULL,finished_at=? WHERE run_id=? AND call_id=? "
                    "AND status='running'",
                    (output, _now(), run_id, call_id),
                ).rowcount == 1
                outcome = "succeeded" if changed else "conflict"
            else:
                changed = db.execute(
                    "UPDATE tool_executions SET status='failed',error=?,"
                    "finished_at=? WHERE run_id=? AND call_id=? "
                    "AND status='running'",
                    ("manually confirmed not executed", _now(), run_id, call_id),
                ).rowcount == 1
                outcome = "succeeded" if changed else "conflict"
            return self._record(
                db, operation_id=operation_id, request_hash=request_hash,
                tenant_id=tenant_id, actor_id=actor_id, action=action,
                resource_type="tool_execution", resource_id=resource_id,
                reason=reason, outcome=outcome, changed=changed,
                details={"output_recorded": output is not None},
            )

    def admin_export_audit(self, *, tenant_id, since, until, limit):
        where, values = [], []
        if tenant_id is not None:
            where.append("tenant_id=?")
            values.append(tenant_id)
        if since is not None:
            where.append("created_at>=?")
            values.append(_time(since))
        if until is not None:
            where.append("created_at<?")
            values.append(_time(until))
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM audit_events" + clause
                + " ORDER BY created_at,id LIMIT ?",
                (*values, limit),
            ).fetchall()
        return [_audit(row) for row in rows]

    @staticmethod
    def _existing_operation(
        db, operation_id, request_hash, tenant_id, actor_id, action,
        resource_type, resource_id,
    ):
        row = db.execute(
            "SELECT * FROM admin_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        expected = (
            request_hash, tenant_id, actor_id, action, resource_type, resource_id,
        )
        actual = tuple(row[name] for name in (
            "request_hash", "tenant_id", "actor_id", "action",
            "resource_type", "resource_id",
        ))
        if actual != expected:
            raise AdminConflictError(
                "operation_id is already bound to a different request"
            )
        return AdminCommandResult(**json.loads(row["result_json"]))

    @staticmethod
    def _record(
        db, *, operation_id, request_hash, tenant_id, actor_id, action,
        resource_type, resource_id, reason, outcome, changed, details,
    ):
        audit_id = f"audit_{uuid.uuid4().hex}"
        result = AdminCommandResult(
            operation_id=operation_id, changed=changed, outcome=outcome,
            resource_id=resource_id, audit_id=audit_id,
        )
        now = _now()
        result_json = json.dumps({
            "operation_id": result.operation_id,
            "changed": result.changed,
            "outcome": result.outcome,
            "resource_id": result.resource_id,
            "audit_id": result.audit_id,
        })
        db.execute(
            "INSERT INTO admin_operations("
            "operation_id,tenant_id,actor_id,action,resource_type,resource_id,"
            "request_hash,result_json,created_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id, tenant_id, actor_id, action, resource_type,
                resource_id, request_hash, result_json, now, now,
            ),
        )
        db.execute(
            "INSERT INTO audit_events("
            "id,tenant_id,actor_id,action,resource_type,resource_id,reason,"
            "operation_id,outcome,created_at,details_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                audit_id, tenant_id, actor_id, action, resource_type,
                resource_id, reason, operation_id, outcome, now,
                json.dumps(
                    details, ensure_ascii=False, separators=(",", ":")
                ),
            ),
        )
        return result


def _run_transition(action: str) -> tuple[str, frozenset[str]]:
    transitions = {
        "pause": ("interrupted", frozenset({"running", "waiting_approval"})),
        "cancel": (
            "cancelled",
            frozenset({"running", "waiting_approval", "interrupted", "failed"}),
        ),
        "recover": ("interrupted", frozenset({"failed"})),
    }
    try:
        return transitions[action]
    except KeyError as exc:
        raise ValueError(f"unsupported run action: {action}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _datetime(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _session(row) -> AdminSession:
    return AdminSession(
        id=row["id"],
        tenant_id=row["tenant_id"],
        platform=row["platform"],
        conversation_id=row["conversation_id"],
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _run(row) -> AdminRun:
    return AdminRun(
        id=row["id"],
        tenant_id=row["tenant_id"],
        session_id=row["session_id"],
        status=row["status"],
        inbound_platform=row["inbound_platform"],
        inbound_message_id=row["inbound_message_id"],
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
        error=row["error"],
    )


def _tool(row) -> AdminToolExecution:
    return AdminToolExecution(
        run_id=row["run_id"],
        call_id=row["call_id"],
        tool_name=row["tool_name"],
        status=row["status"],
        started_at=_datetime(row["started_at"]),
        finished_at=(
            _datetime(row["finished_at"]) if row["finished_at"] else None
        ),
        error=row["error"],
    )


def _audit(row) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        operation_id=row["operation_id"],
        tenant_id=row["tenant_id"],
        actor_id=row["actor_id"],
        action=row["action"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        reason=row["reason"],
        outcome=row["outcome"],
        created_at=_datetime(row["created_at"]),
        details=json.loads(row["details_json"]),
    )
