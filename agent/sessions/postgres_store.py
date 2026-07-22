from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.context.models import SessionSummary
from agent.core.checkpoints import CheckpointCodec
from agent.core.run_state import RunLeaseLost, RunStateMachine
from agent.migrations import apply_postgres_migrations

from .models import (
    Checkpoint,
    InboundStart,
    RunRecord,
    RunStatus,
    StoredMessage,
    ToolClaim,
)


class PostgresSessionStore:
    """与 SQLiteSessionStore 具有相同 CAS 与租约语义的 PostgreSQL 实现。"""

    def __init__(
        self,
        dsn: str,
        *,
        owner_id: str | None = None,
        lease_seconds: int = 30,
        checkpoint_codec: CheckpointCodec | None = None,
        migrate: bool = True,
    ):
        if not dsn:
            raise ValueError("PostgreSQL DSN must not be empty")
        self.dsn = dsn
        self.owner_id = owner_id or f"owner_{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.lease_refresh_interval = max(0.1, lease_seconds / 3)
        self.checkpoint_codec = checkpoint_codec or CheckpointCodec()
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

    def begin_inbound(
        self,
        *,
        platform: str,
        conversation_id: str,
        sender_id: str,
        message_id: str,
        metadata: dict[str, Any] | None = None,
        user_content: str | None = None,
        initial_checkpoint: dict[str, Any] | None = None,
        recent_message_limit: int = 20,
    ) -> InboundStart:
        if (user_content is None) != (initial_checkpoint is None):
            raise ValueError(
                "user_content and initial_checkpoint must be provided together"
            )
        now = _now()
        tenant_id = _tenant_id(metadata)
        with self._connect() as db:
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (_advisory_lock_key(platform, message_id),),
            )
            existing = db.execute(
                "SELECT run_id,response FROM inbound_messages "
                "WHERE platform=%s AND message_id=%s",
                (platform, message_id),
            ).fetchone()
            if existing:
                run = _run(db.execute(
                    "SELECT * FROM runs WHERE id=%s", (existing["run_id"],)
                ).fetchone())
                return InboundStart(False, run, existing["response"])
            session_id = _session_id(tenant_id, platform, conversation_id)
            db.execute(
                "INSERT INTO sessions(id,platform,conversation_id,created_at,"
                "updated_at,tenant_id) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(tenant_id,platform,conversation_id) DO UPDATE SET "
                "updated_at=EXCLUDED.updated_at",
                (session_id, platform, conversation_id, now, now, tenant_id),
            )
            session_id = db.execute(
                "SELECT id FROM sessions WHERE tenant_id=%s AND platform=%s "
                "AND conversation_id=%s",
                (tenant_id, platform, conversation_id),
            ).fetchone()["id"]
            run_id = f"run_{uuid.uuid4().hex}"
            db.execute(
                "INSERT INTO runs(id,session_id,inbound_platform,inbound_message_id,"
                "status,created_at,updated_at,error,owner_id,lease_expires_at,"
                "execution_token) VALUES(%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,1)",
                (run_id, session_id, platform, message_id,
                 RunStatus.RUNNING.value, now, now, self.owner_id,
                 self._lease_expiry()),
            )
            db.execute(
                "INSERT INTO inbound_messages VALUES(%s,%s,%s,%s,%s,NULL,%s)",
                (platform, message_id, run_id, sender_id,
                 _jsonb(metadata or {}), now),
            )
            if user_content is not None:
                assert initial_checkpoint is not None
                db.execute(
                    "INSERT INTO messages(session_id,run_id,role,content,created_at) "
                    "VALUES(%s,%s,'user',%s,%s)",
                    (session_id, run_id, user_content, now),
                )
                rows = db.execute(
                    "SELECT role,content FROM (SELECT id,role,content FROM messages "
                    "WHERE session_id=%s ORDER BY id DESC LIMIT %s) recent "
                    "ORDER BY id",
                    (session_id, max(0, recent_message_limit)),
                ).fetchall()
                history = [
                    {"role": row["role"], "content": row["content"]}
                    for row in rows
                ]
                checkpoint = self.checkpoint_codec.replace_messages(
                    initial_checkpoint, history
                )
                db.execute(
                    "INSERT INTO checkpoints(run_id,sequence,phase,state_json,created_at) "
                    "VALUES(%s,1,'inbound_started',%s,%s)",
                    (run_id, _jsonb(checkpoint), now),
                )
            row = db.execute("SELECT * FROM runs WHERE id=%s", (run_id,)).fetchone()
            return InboundStart(True, _run(row))

    def start_inbound(
        self, *, platform: str, conversation_id: str, sender_id: str,
        message_id: str, user_content: str,
        initial_checkpoint: dict[str, Any], recent_message_limit: int = 20,
        metadata: dict[str, Any] | None = None,
    ) -> InboundStart:
        return self.begin_inbound(
            platform=platform, conversation_id=conversation_id,
            sender_id=sender_id, message_id=message_id, metadata=metadata,
            user_content=user_content, initial_checkpoint=initial_checkpoint,
            recent_message_limit=recent_message_limit,
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=%s", (run_id,)).fetchone()
        return _run(row) if row else None

    def get_inbound(self, platform: str, message_id: str) -> InboundStart | None:
        with self._connect() as db:
            existing = db.execute(
                "SELECT run_id,response FROM inbound_messages "
                "WHERE platform=%s AND message_id=%s", (platform, message_id)
            ).fetchone()
            if existing is None:
                return None
            row = db.execute(
                "SELECT * FROM runs WHERE id=%s", (existing["run_id"],)
            ).fetchone()
        return InboundStart(False, _run(row), existing["response"])

    def append_message(
        self, session_id: str, run_id: str, role: str, content: str
    ) -> StoredMessage:
        if role not in {"user", "assistant"}:
            raise ValueError(f"Unsupported message role: {role}")
        with self._connect() as db:
            row = db.execute(
                "INSERT INTO messages(session_id,run_id,role,content,created_at) "
                "VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (session_id, run_id, role, content, _now()),
            ).fetchone()
        return _message(row)

    def recent_messages(self, session_id: str, limit: int) -> list[StoredMessage]:
        if limit <= 0:
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=%s "
                "ORDER BY id DESC LIMIT %s) recent ORDER BY id",
                (session_id, limit),
            ).fetchall()
        return [_message(row) for row in rows]

    def messages_after(
        self, session_id: str, through_message_id: int = 0
    ) -> list[StoredMessage]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE session_id=%s AND id>%s ORDER BY id",
                (session_id, through_message_id),
            ).fetchall()
        return [_message(row) for row in rows]

    def latest_summary(self, session_id: str) -> SessionSummary | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM session_summaries WHERE session_id=%s "
                "ORDER BY version DESC LIMIT 1", (session_id,)
            ).fetchone()
        return _summary(row) if row else None

    def save_summary(
        self, session_id: str, content: str, through_message_id: int
    ) -> SessionSummary:
        with self._connect() as db:
            db.execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (session_id,))
            covered = db.execute(
                "SELECT id FROM messages WHERE id=%s AND session_id=%s",
                (through_message_id, session_id),
            ).fetchone()
            if covered is None:
                raise ValueError("summary through_message_id does not belong to session")
            latest = db.execute(
                "SELECT version,through_message_id FROM session_summaries "
                "WHERE session_id=%s ORDER BY version DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if latest and through_message_id <= latest["through_message_id"]:
                raise ValueError("summary coverage must advance")
            version = int(latest["version"]) + 1 if latest else 1
            row = db.execute(
                "INSERT INTO session_summaries(session_id,version,content,"
                "through_message_id,created_at) VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (session_id, version, content, through_message_id, _now()),
            ).fetchone()
        return _summary(row)

    def save_checkpoint(
        self, run_id: str, phase: str, state: dict[str, Any], *,
        execution_token: int | None = None,
    ) -> Checkpoint:
        with self._connect() as db:
            self._assert_owner(db, run_id, execution_token, lock=True)
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS sequence "
                "FROM checkpoints WHERE run_id=%s", (run_id,)
            ).fetchone()["sequence"]
            row = db.execute(
                "INSERT INTO checkpoints(run_id,sequence,phase,state_json,created_at) "
                "VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (run_id, sequence, phase, _jsonb(state), _now()),
            ).fetchone()
        return _checkpoint(row)

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE run_id=%s "
                "ORDER BY sequence DESC LIMIT 1", (run_id,)
            ).fetchone()
        return _checkpoint(row) if row else None

    def transition_run(
        self, run_id: str, status: RunStatus, error: str | None = None, *,
        execution_token: int | None = None,
    ) -> bool:
        sources = tuple(RunStateMachine.sources_for(status))
        if not sources:
            return False
        placeholders = ",".join("%s" for _ in sources)
        ownership = ""
        values: list[Any] = [
            status.value, _now(), error[:1000] if error else None, run_id,
            *(source.value for source in sources),
        ]
        if execution_token is not None:
            ownership = " AND owner_id=%s AND execution_token=%s"
            values.extend((self.owner_id, execution_token))
        with self._connect() as db:
            changed = db.execute(
                "UPDATE runs SET status=%s,updated_at=%s,error=%s WHERE id=%s "
                f"AND status IN ({placeholders}){ownership}", values,
            ).rowcount
        return changed == 1

    def claim_run(self, run_id: str, expected_statuses: set[RunStatus]) -> bool:
        if not expected_statuses:
            return False
        statuses = tuple(expected_statuses)
        placeholders = ",".join("%s" for _ in statuses)
        with self._connect() as db:
            changed = db.execute(
                "UPDATE runs SET status=%s,updated_at=%s,error=NULL,owner_id=%s,"
                "lease_expires_at=%s,execution_token=execution_token+1 WHERE id=%s "
                f"AND status IN ({placeholders})",
                (RunStatus.RUNNING.value, _now(), self.owner_id,
                 self._lease_expiry(), run_id,
                 *(status.value for status in statuses)),
            ).rowcount
        return changed == 1

    def complete_run(
        self, run_id: str, response: str, *, execution_token: int | None = None
    ) -> bool:
        with self._connect() as db:
            run = db.execute(
                "SELECT session_id,status,owner_id,execution_token FROM runs "
                "WHERE id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if run is None:
                return False
            if run["status"] == RunStatus.COMPLETED.value:
                existing = db.execute(
                    "SELECT response FROM inbound_messages WHERE run_id=%s", (run_id,)
                ).fetchone()
                return existing is not None and existing["response"] == response
            if run["status"] != RunStatus.RUNNING.value:
                return False
            token = execution_token or int(run["execution_token"])
            if run["owner_id"] != self.owner_id or int(run["execution_token"]) != token:
                return False
            changed = db.execute(
                "UPDATE runs SET status=%s,updated_at=%s,error=NULL,owner_id=NULL,"
                "lease_expires_at=NULL WHERE id=%s AND status=%s AND owner_id=%s "
                "AND execution_token=%s",
                (RunStatus.COMPLETED.value, _now(), run_id,
                 RunStatus.RUNNING.value, self.owner_id, token),
            ).rowcount
            if changed != 1:
                return False
            db.execute(
                "INSERT INTO messages(session_id,run_id,role,content,created_at) "
                "VALUES(%s,%s,'assistant',%s,%s)",
                (run["session_id"], run_id, response, _now()),
            )
            db.execute(
                "UPDATE inbound_messages SET response=%s WHERE run_id=%s",
                (response, run_id),
            )
        return True

    def cached_response(self, run_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT response FROM inbound_messages WHERE run_id=%s", (run_id,)
            ).fetchone()
        return row["response"] if row else None

    def interrupt_incomplete_runs(self) -> list[RunRecord]:
        with self._connect() as db:
            now = _now()
            ids = db.execute(
                "SELECT id FROM runs WHERE status=%s AND (owner_id=%s OR "
                "lease_expires_at IS NULL OR lease_expires_at<=%s) "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED",
                (RunStatus.RUNNING.value, self.owner_id, now),
            ).fetchall()
            rows = []
            for item in ids:
                row = db.execute(
                    "UPDATE runs SET status=%s,updated_at=%s,owner_id=NULL,"
                    "lease_expires_at=NULL WHERE id=%s AND status=%s RETURNING *",
                    (RunStatus.INTERRUPTED.value, now, item["id"],
                     RunStatus.RUNNING.value),
                ).fetchone()
                if row:
                    rows.append(row)
        return [_run(row) for row in rows]

    def renew_run(self, run_id: str, execution_token: int | None = None) -> bool:
        if execution_token is None:
            run = self.get_run(run_id)
            if run is None or run.owner_id != self.owner_id:
                return False
            execution_token = run.execution_token
        with self._connect() as db:
            changed = db.execute(
                "UPDATE runs SET lease_expires_at=%s,updated_at=%s WHERE id=%s "
                "AND status=%s AND owner_id=%s AND execution_token=%s",
                (self._lease_expiry(), _now(), run_id, RunStatus.RUNNING.value,
                 self.owner_id, execution_token),
            ).rowcount
        return changed == 1

    def _lease_expiry(self) -> datetime:
        return _now() + timedelta(seconds=self.lease_seconds)

    def list_recoverable_runs(self) -> list[RunRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE status=%s ORDER BY created_at",
                (RunStatus.INTERRUPTED.value,),
            ).fetchall()
        return [_run(row) for row in rows]

    def claim_tool(
        self, run_id: str, call_id: str, tool_name: str,
        arguments: dict[str, Any], *, execution_token: int | None = None,
    ) -> ToolClaim:
        with self._connect() as db:
            self._assert_owner(db, run_id, execution_token, lock=True)
            row = db.execute(
                "SELECT * FROM tool_executions WHERE run_id=%s AND call_id=%s",
                (run_id, call_id),
            ).fetchone()
            if row:
                self._validate_tool_identity(row, tool_name, arguments)
                if row["status"] == "completed":
                    return ToolClaim(False, output=row["output"])
                return ToolClaim(False, is_uncertain=row["status"] == "running")
            db.execute(
                "INSERT INTO tool_executions VALUES(%s,%s,%s,%s,'running',"
                "NULL,NULL,%s,NULL)",
                (run_id, call_id, tool_name, _jsonb(arguments), _now()),
            )
            return ToolClaim(True)

    def get_tool(
        self, run_id: str, call_id: str, tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolClaim | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM tool_executions WHERE run_id=%s AND call_id=%s",
                (run_id, call_id),
            ).fetchone()
        if row is None:
            return None
        self._validate_tool_identity(row, tool_name, arguments)
        if row["status"] == "completed":
            return ToolClaim(False, output=row["output"])
        return ToolClaim(False, is_uncertain=row["status"] == "running")

    @staticmethod
    def _validate_tool_identity(
        row: dict[str, Any], tool_name: str, arguments: dict[str, Any]
    ) -> None:
        if (
            row["tool_name"] != tool_name
            or _canonical(row["arguments_json"]) != _canonical(arguments)
        ):
            raise ValueError("tool execution identity conflicts with existing call_id")

    def complete_tool(
        self, run_id: str, call_id: str, output: str, *,
        execution_token: int | None = None,
    ) -> bool:
        with self._connect() as db:
            self._assert_owner(db, run_id, execution_token, lock=True)
            changed = db.execute(
                "UPDATE tool_executions SET status='completed',output=%s,"
                "finished_at=%s WHERE run_id=%s AND call_id=%s AND status='running'",
                (output, _now(), run_id, call_id),
            ).rowcount
        return changed == 1

    def fail_tool(
        self, run_id: str, call_id: str, error: str, *,
        execution_token: int | None = None,
    ) -> bool:
        with self._connect() as db:
            self._assert_owner(db, run_id, execution_token, lock=True)
            changed = db.execute(
                "UPDATE tool_executions SET status='failed',error=%s,finished_at=%s "
                "WHERE run_id=%s AND call_id=%s AND status='running'",
                (error[:1000], _now(), run_id, call_id),
            ).rowcount
        return changed == 1

    def _assert_owner(
        self, db: Any, run_id: str, execution_token: int | None, *,
        lock: bool = False,
    ) -> int:
        suffix = " FOR UPDATE" if lock else ""
        row = db.execute(
            "SELECT status,owner_id,execution_token FROM runs WHERE id=%s" + suffix,
            (run_id,),
        ).fetchone()
        token = execution_token or (
            int(row["execution_token"]) if row is not None else None
        )
        if (
            row is None or row["status"] != RunStatus.RUNNING.value
            or row["owner_id"] != self.owner_id
            or int(row["execution_token"]) != token
        ):
            raise RunLeaseLost(f"Run lease lost: {run_id}")
        return token


def _advisory_lock_key(*parts: str) -> str:
    # PostgreSQL text parameters cannot contain NUL (0x00) bytes.
    return json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_id(tenant_id: str, platform: str, conversation_id: str) -> str:
    value = json.dumps(
        [tenant_id, platform, conversation_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "session_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _tenant_id(metadata: dict[str, Any] | None) -> str:
    tenant_id = (metadata or {}).get("tenant_id", "default")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("metadata.tenant_id must be a non-empty string")
    return tenant_id.strip()


def _jsonb(value: Any):
    from psycopg.types.json import Jsonb
    return Jsonb(value)


def _canonical(value: Any) -> str:
    if isinstance(value, str):
        value = json.loads(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _run(row: dict[str, Any]) -> RunRecord:
    return RunRecord(
        id=row["id"], session_id=row["session_id"],
        inbound_platform=row["inbound_platform"],
        inbound_message_id=row["inbound_message_id"],
        status=RunStatus(row["status"]), created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]), error=row["error"],
        owner_id=row["owner_id"],
        lease_expires_at=_datetime(row["lease_expires_at"])
        if row["lease_expires_at"] else None,
        execution_token=int(row["execution_token"]),
    )


def _message(row: dict[str, Any]) -> StoredMessage:
    return StoredMessage(
        id=row["id"], session_id=row["session_id"], run_id=row["run_id"],
        role=row["role"], content=row["content"],
        created_at=_datetime(row["created_at"]),
    )


def _checkpoint(row: dict[str, Any]) -> Checkpoint:
    state = row["state_json"]
    return Checkpoint(
        id=row["id"], run_id=row["run_id"], sequence=row["sequence"],
        phase=row["phase"], state=json.loads(state) if isinstance(state, str) else state,
        created_at=_datetime(row["created_at"]),
    )


def _summary(row: dict[str, Any]) -> SessionSummary:
    return SessionSummary(
        id=row["id"], session_id=row["session_id"], version=row["version"],
        content=row["content"], through_message_id=row["through_message_id"],
        created_at=_datetime(row["created_at"]),
    )


def _datetime(value: datetime | str) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)
