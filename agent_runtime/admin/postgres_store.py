from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from agent_runtime.admin.models import (
    AdminCheckpoint,
    AdminCommandResult,
    AdminConflictError,
    AuditEvent,
    ToolDisposition,
)
from agent_runtime.admin.store import (
    _run,
    _run_transition,
    _session,
    _tool,
)


class PostgresAdminRepository:
    def __init__(self, dsn: str, *, migrate: bool = True) -> None:
        self.dsn = dsn
        if migrate:
            from agent_runtime.migrations import apply_postgres_migrations

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

    def admin_get_run(self, run_id):
        with self._connect() as db:
            row = db.execute(
                "SELECT r.*,s.tenant_id FROM runs r JOIN sessions s "
                "ON s.id=r.session_id WHERE r.id=%s",
                (run_id,),
            ).fetchone()
        return _run(row) if row else None

    def admin_list_sessions(self, *, tenant_id, before, limit):
        clauses, values = [], []
        if tenant_id is not None:
            clauses.append("tenant_id=%s")
            values.append(tenant_id)
        if before is not None:
            clauses.append("created_at<%s")
            values.append(before)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM sessions" + where
                + " ORDER BY created_at DESC,id DESC LIMIT %s",
                (*values, limit),
            ).fetchall()
        return [_session(row) for row in rows]

    def admin_list_runs(
        self, *, tenant_id, session_id, status, before, limit,
    ):
        clauses, values = [], []
        for clause, value in (
            ("s.tenant_id=%s", tenant_id),
            ("r.session_id=%s", session_id),
            ("r.status=%s", status),
            ("r.created_at<%s", before),
        ):
            if value is not None:
                clauses.append(clause)
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT r.*,s.tenant_id FROM runs r JOIN sessions s "
                "ON s.id=r.session_id" + where
                + " ORDER BY r.created_at DESC,r.id DESC LIMIT %s",
                (*values, limit),
            ).fetchall()
        return [_run(row) for row in rows]

    def admin_list_checkpoints(self, run_id, *, limit):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,run_id,sequence,phase,created_at FROM checkpoints "
                "WHERE run_id=%s ORDER BY sequence DESC LIMIT %s",
                (run_id, limit),
            ).fetchall()
        return [
            AdminCheckpoint(
                id=row["id"], run_id=row["run_id"],
                sequence=row["sequence"], phase=row["phase"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def admin_list_tool_executions(self, run_id, *, status, limit):
        clause, values = "", [run_id]
        if status is not None:
            clause = " AND status=%s"
            values.append(status)
        with self._connect() as db:
            rows = db.execute(
                "SELECT run_id,call_id,tool_name,status,started_at,"
                "finished_at,error FROM tool_executions WHERE run_id=%s"
                + clause + " ORDER BY started_at DESC,call_id DESC LIMIT %s",
                (*values, limit),
            ).fetchall()
        return [_tool(row) for row in rows]

    def admin_export_audit(self, *, tenant_id, since, until, limit):
        clauses, values = [], []
        for clause, value in (
            ("tenant_id=%s", tenant_id),
            ("created_at>=%s", since),
            ("created_at<%s", until),
        ):
            if value is not None:
                clauses.append(clause)
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM audit_events" + where
                + " ORDER BY created_at,id LIMIT %s",
                (*values, limit),
            ).fetchall()
        return [_audit(row) for row in rows]

    def admin_control_run(
        self, *, tenant_id, run_id, action, actor_id, reason, operation_id,
        request_hash,
    ):
        full_action = f"run.{action}"
        with self._connect() as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (operation_id,),
            )
            previous = self._existing(
                db, operation_id, request_hash, tenant_id, actor_id,
                full_action, "run", run_id,
            )
            if previous:
                return previous
            row = db.execute(
                "SELECT r.status FROM runs r JOIN sessions s "
                "ON s.id=r.session_id WHERE r.id=%s AND s.tenant_id=%s "
                "FOR UPDATE",
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
                        "UPDATE runs SET status=%s,updated_at=%s,owner_id=NULL,"
                        "lease_expires_at=NULL,"
                        "execution_token=execution_token+1 "
                        "WHERE id=%s AND status=%s",
                        (target, _now(), run_id, current),
                    ).rowcount == 1
                    outcome = "succeeded" if changed else "conflict"
            return self._record(
                db, operation_id, request_hash, tenant_id, actor_id,
                full_action, "run", run_id, reason, outcome, changed,
                {"sensitive_fields_omitted": True},
            )

    def admin_resolve_tool(
        self, *, tenant_id, run_id, call_id, disposition, output, actor_id,
        reason, operation_id, request_hash,
    ):
        action = f"tool.{disposition.value}"
        resource_id = f"{run_id}:{call_id}"
        with self._connect() as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (operation_id,),
            )
            previous = self._existing(
                db, operation_id, request_hash, tenant_id, actor_id, action,
                "tool_execution", resource_id,
            )
            if previous:
                return previous
            row = db.execute(
                "SELECT t.status FROM tool_executions t "
                "JOIN runs r ON r.id=t.run_id "
                "JOIN sessions s ON s.id=r.session_id "
                "WHERE t.run_id=%s AND t.call_id=%s AND s.tenant_id=%s "
                "FOR UPDATE",
                (run_id, call_id, tenant_id),
            ).fetchone()
            if row is None:
                outcome, changed = "not_found", False
            elif row["status"] != "running":
                outcome, changed = "already_resolved", False
            elif disposition is ToolDisposition.CONFIRMED_SUCCEEDED:
                changed = db.execute(
                    "UPDATE tool_executions SET status='completed',output=%s,"
                    "error=NULL,finished_at=%s WHERE run_id=%s AND call_id=%s "
                    "AND status='running'",
                    (output, _now(), run_id, call_id),
                ).rowcount == 1
                outcome = "succeeded" if changed else "conflict"
            else:
                changed = db.execute(
                    "UPDATE tool_executions SET status='failed',error=%s,"
                    "finished_at=%s WHERE run_id=%s AND call_id=%s "
                    "AND status='running'",
                    ("manually confirmed not executed", _now(), run_id, call_id),
                ).rowcount == 1
                outcome = "succeeded" if changed else "conflict"
            return self._record(
                db, operation_id, request_hash, tenant_id, actor_id, action,
                "tool_execution", resource_id, reason, outcome, changed,
                {"output_recorded": output is not None},
            )

    @staticmethod
    def _existing(
        db, operation_id, request_hash, tenant_id, actor_id, action,
        resource_type, resource_id,
    ):
        row = db.execute(
            "SELECT * FROM admin_operations WHERE operation_id=%s FOR UPDATE",
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
        value = row["result_json"]
        if isinstance(value, str):
            value = json.loads(value)
        return AdminCommandResult(**value)

    @staticmethod
    def _record(
        db, operation_id, request_hash, tenant_id, actor_id, action,
        resource_type, resource_id, reason, outcome, changed, details,
    ):
        from psycopg.types.json import Jsonb

        audit_id = f"audit_{uuid.uuid4().hex}"
        result = AdminCommandResult(
            operation_id=operation_id, changed=changed, outcome=outcome,
            resource_id=resource_id, audit_id=audit_id,
        )
        result_value = dict(result.__dict__)
        now = _now()
        db.execute(
            "INSERT INTO admin_operations("
            "operation_id,tenant_id,actor_id,action,resource_type,resource_id,"
            "request_hash,result_json,created_at,completed_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                operation_id, tenant_id, actor_id, action, resource_type,
                resource_id, request_hash, Jsonb(result_value), now, now,
            ),
        )
        db.execute(
            "INSERT INTO audit_events("
            "id,tenant_id,actor_id,action,resource_type,resource_id,reason,"
            "operation_id,outcome,created_at,details_json) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                audit_id, tenant_id, actor_id, action, resource_type,
                resource_id, reason, operation_id, outcome, now,
                Jsonb(details),
            ),
        )
        return result


def _now():
    return datetime.now(timezone.utc)


def _audit(row):
    details = row["details_json"]
    if isinstance(details, str):
        details = json.loads(details)
    return AuditEvent(
        id=row["id"], tenant_id=row["tenant_id"],
        actor_id=row["actor_id"], action=row["action"],
        resource_type=row["resource_type"], resource_id=row["resource_id"],
        reason=row["reason"], operation_id=row["operation_id"],
        outcome=row["outcome"], created_at=row["created_at"],
        details=details,
    )
