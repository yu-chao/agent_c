from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from agent_runtime.migrations import apply_postgres_migrations

from .models import (
    ApprovalAction,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    RuntimeIdentity,
)


class PostgresApprovalStore:
    """使用行锁和唯一约束实现多实例安全的审批存储。"""

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
                "PostgreSQL storage requires: uv sync --extra postgres"
            ) from exc
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def create(self, item: ApprovalRequest) -> ApprovalRequest:
        identity = item.identity
        run_id = item.continuation.get("run_id")
        with self._connect() as db:
            if run_id:
                db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"{run_id}\0{item.tool_call_id}",),
                )
                existing = db.execute(
                    "SELECT approvals.* FROM approval_links JOIN approvals "
                    "ON approvals.id=approval_links.approval_id "
                    "WHERE approval_links.run_id=%s "
                    "AND approval_links.tool_call_id=%s",
                    (run_id, item.tool_call_id),
                ).fetchone()
                if existing:
                    return _request(existing)
            db.execute(
                "INSERT INTO approvals VALUES("
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "NULL,NULL,NULL,NULL)",
                (item.id, identity.platform, identity.conversation_id,
                 identity.sender_id, identity.message_id,
                 _jsonb(identity.metadata), item.tool_call_id, item.tool_name,
                 _jsonb(item.tool_input), item.arguments_hash,
                 _jsonb(item.continuation), item.status.value,
                 item.created_at, item.expires_at),
            )
            if run_id:
                db.execute(
                    "INSERT INTO approval_links VALUES(%s,%s,%s)",
                    (run_id, item.tool_call_id, item.id),
                )
        return item

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE id=%s", (approval_id,)
            ).fetchone()
        return _request(row) if row else None

    def decide(
        self, approval_id: str, action: str, identity: RuntimeIdentity,
        event_message_id: str, *, now: datetime | None = None,
    ) -> ApprovalDecision:
        current = now or datetime.now(timezone.utc)
        try:
            selected = ApprovalAction(action)
        except ValueError:
            return ApprovalDecision(False, None, "unsupported approval action")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE id=%s FOR UPDATE", (approval_id,)
            ).fetchone()
            if not row:
                return ApprovalDecision(False, None, "approval not found")
            item = _request(row)
            original = item.identity
            if (original.platform, original.conversation_id, original.sender_id) != (
                identity.platform, identity.conversation_id, identity.sender_id
            ):
                return ApprovalDecision(False, item.status, "unauthorized", item)
            if item.status is ApprovalStatus.PENDING and item.expires_at <= current:
                db.execute(
                    "UPDATE approvals SET status=%s WHERE id=%s AND status=%s",
                    (ApprovalStatus.EXPIRED.value, approval_id,
                     ApprovalStatus.PENDING.value),
                )
                item = _get(db, approval_id)
                return ApprovalDecision(False, item.status, "approval expired", item)
            duplicate = db.execute(
                "SELECT 1 FROM approval_events WHERE message_id=%s",
                (event_message_id,),
            ).fetchone()
            if duplicate:
                return ApprovalDecision(False, item.status, "event already handled", item)
            if item.status is not ApprovalStatus.PENDING:
                return ApprovalDecision(False, item.status, "approval already handled", item)
            target = (
                ApprovalStatus.APPROVED
                if selected is ApprovalAction.CONFIRM else ApprovalStatus.REJECTED
            )
            db.execute(
                "INSERT INTO approval_events VALUES(%s,%s,%s)",
                (event_message_id, approval_id, current),
            )
            changed = db.execute(
                "UPDATE approvals SET status=%s,decided_by=%s,decided_at=%s "
                "WHERE id=%s AND status=%s",
                (target.value, identity.sender_id, current, approval_id,
                 ApprovalStatus.PENDING.value),
            ).rowcount
            item = _get(db, approval_id)
            return ApprovalDecision(changed == 1, item.status, target.value, item)

    def expire_pending(
        self, *, now: datetime | None = None
    ) -> list[ApprovalRequest]:
        current = now or datetime.now(timezone.utc)
        with self._connect() as db:
            rows = db.execute(
                "UPDATE approvals SET status=%s WHERE status=%s AND expires_at<=%s "
                "RETURNING *",
                (ApprovalStatus.EXPIRED.value, ApprovalStatus.PENDING.value, current),
            ).fetchall()
        return [_request(row) for row in rows]

    def claim_execution(self, approval_id: str) -> ApprovalRequest | None:
        with self._connect() as db:
            row = db.execute(
                "UPDATE approvals SET status=%s WHERE id=%s AND status=%s "
                "RETURNING *",
                (ApprovalStatus.EXECUTING.value, approval_id,
                 ApprovalStatus.APPROVED.value),
            ).fetchone()
        return _request(row) if row else None

    def complete(self, approval_id: str) -> bool:
        return self._transition(
            approval_id, ApprovalStatus.EXECUTING, ApprovalStatus.COMPLETED
        )

    def fail(self, approval_id: str, error: str) -> bool:
        with self._connect() as db:
            changed = db.execute(
                "UPDATE approvals SET status=%s,error=%s WHERE id=%s AND status=%s",
                (ApprovalStatus.FAILED.value, str(error)[:1000], approval_id,
                 ApprovalStatus.EXECUTING.value),
            ).rowcount
        return changed == 1

    def mark_consumed(self, approval_id: str) -> bool:
        with self._connect() as db:
            changed = db.execute(
                "UPDATE approvals SET resumed_at=%s WHERE id=%s "
                "AND resumed_at IS NULL AND status IN (%s,%s)",
                (datetime.now(timezone.utc), approval_id,
                 ApprovalStatus.REJECTED.value, ApprovalStatus.EXPIRED.value),
            ).rowcount
        return changed == 1

    def list_resumable(self) -> list[ApprovalRequest]:
        return self._list(
            "SELECT * FROM approvals WHERE status=%s OR "
            "(status IN (%s,%s) AND resumed_at IS NULL) ORDER BY created_at",
            (ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value,
             ApprovalStatus.EXPIRED.value),
        )

    def list_uncertain(self) -> list[ApprovalRequest]:
        return self._list(
            "SELECT * FROM approvals WHERE status=%s",
            (ApprovalStatus.EXECUTING.value,),
        )

    def _list(self, query: str, values: tuple[Any, ...]) -> list[ApprovalRequest]:
        with self._connect() as db:
            rows = db.execute(query, values).fetchall()
        return [_request(row) for row in rows]

    def _transition(
        self, approval_id: str, source: ApprovalStatus, target: ApprovalStatus
    ) -> bool:
        with self._connect() as db:
            changed = db.execute(
                "UPDATE approvals SET status=%s WHERE id=%s AND status=%s",
                (target.value, approval_id, source.value),
            ).rowcount
        return changed == 1


def _jsonb(value: Any):
    from psycopg.types.json import Jsonb
    return Jsonb(value)


def _get(db: Any, approval_id: str) -> ApprovalRequest:
    return _request(db.execute(
        "SELECT * FROM approvals WHERE id=%s", (approval_id,)
    ).fetchone())


def _request(row: dict[str, Any]) -> ApprovalRequest:
    def value(name: str) -> Any:
        item = row[name]
        return json.loads(item) if isinstance(item, str) else item

    return ApprovalRequest(
        id=row["id"],
        identity=RuntimeIdentity(
            row["platform"], row["conversation_id"], row["sender_id"],
            row["message_id"], value("metadata_json"),
        ),
        tool_call_id=row["tool_call_id"], tool_name=row["tool_name"],
        tool_input=value("tool_input_json"), arguments_hash=row["arguments_hash"],
        continuation=value("continuation_json"),
        status=ApprovalStatus(row["status"]),
        created_at=_datetime(row["created_at"]),
        expires_at=_datetime(row["expires_at"]),
        decided_by=row["decided_by"],
        decided_at=_datetime(row["decided_at"]) if row["decided_at"] else None,
        error=row["error"],
        resumed_at=_datetime(row["resumed_at"]) if row["resumed_at"] else None,
    )


def _datetime(value: datetime | str) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)
