from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_runtime.core.checkpoints import CheckpointCodec
from agent_runtime.core.run_state import RunLeaseLost, RunStateMachine
from agent_runtime.context.models import SessionSummary

from .models import (
    Checkpoint,
    InboundStart,
    RunRecord,
    RunStatus,
    StoredMessage,
    ToolClaim,
)


class SQLiteSessionStore:
    def __init__(
        self, path: str | Path, *, owner_id: str | None = None,
        lease_seconds: int = 30,
        checkpoint_codec: CheckpointCodec | None = None,
    ):
        self.path = Path(path)
        self.owner_id = owner_id or f"owner_{uuid.uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.lease_refresh_interval = max(0.1, lease_seconds / 3)
        self.checkpoint_codec = checkpoint_codec or CheckpointCodec()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                  conversation_id TEXT NOT NULL, created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(platform, conversation_id));
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                  inbound_platform TEXT NOT NULL,
                  inbound_message_id TEXT NOT NULL, status TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  error TEXT, owner_id TEXT, lease_expires_at TEXT,
                  execution_token INTEGER NOT NULL DEFAULT 1,
                  FOREIGN KEY(session_id) REFERENCES sessions(id));
                CREATE TABLE IF NOT EXISTS inbound_messages (
                  platform TEXT NOT NULL, message_id TEXT NOT NULL,
                  run_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                  metadata_json TEXT NOT NULL, response TEXT,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(platform, message_id),
                  FOREIGN KEY(run_id) REFERENCES runs(id));
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL, run_id TEXT NOT NULL,
                  role TEXT NOT NULL, content TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(session_id) REFERENCES sessions(id),
                  FOREIGN KEY(run_id) REFERENCES runs(id));
                CREATE INDEX IF NOT EXISTS messages_session_idx
                  ON messages(session_id, id);
                CREATE TABLE IF NOT EXISTS checkpoints (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                  phase TEXT NOT NULL, state_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(run_id, sequence),
                  FOREIGN KEY(run_id) REFERENCES runs(id));
                CREATE TABLE IF NOT EXISTS tool_executions (
                  run_id TEXT NOT NULL, call_id TEXT NOT NULL,
                  tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL,
                  status TEXT NOT NULL, output TEXT, error TEXT,
                  started_at TEXT NOT NULL, finished_at TEXT,
                  PRIMARY KEY(run_id, call_id),
                  FOREIGN KEY(run_id) REFERENCES runs(id));
                CREATE TABLE IF NOT EXISTS session_summaries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_id TEXT NOT NULL, version INTEGER NOT NULL,
                  content TEXT NOT NULL, through_message_id INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(session_id, version),
                  FOREIGN KEY(session_id) REFERENCES sessions(id),
                  FOREIGN KEY(through_message_id) REFERENCES messages(id));
                CREATE INDEX IF NOT EXISTS session_summaries_session_idx
                  ON session_summaries(session_id, version);
                """
            )
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(runs)")
            }
            if "owner_id" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN owner_id TEXT")
            if "lease_expires_at" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN lease_expires_at TEXT")
            if "execution_token" not in columns:
                db.execute(
                    "ALTER TABLE runs ADD COLUMN execution_token "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            db.execute("PRAGMA user_version=1")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

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
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT run_id,response FROM inbound_messages "
                "WHERE platform=? AND message_id=?",
                (platform, message_id),
            ).fetchone()
            if existing:
                run = _run(db.execute(
                    "SELECT * FROM runs WHERE id=?", (existing["run_id"],)
                ).fetchone())
                return InboundStart(False, run, existing["response"])
            session = db.execute(
                "SELECT id FROM sessions WHERE platform=? AND conversation_id=?",
                (platform, conversation_id),
            ).fetchone()
            session_id = session["id"] if session else _session_id(
                platform, conversation_id
            )
            db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?) "
                "ON CONFLICT(platform,conversation_id) DO UPDATE SET "
                "updated_at=excluded.updated_at",
                (session_id, platform, conversation_id, now, now),
            )
            run_id = f"run_{uuid.uuid4().hex}"
            db.execute(
                "INSERT INTO runs(id,session_id,inbound_platform,inbound_message_id,"
                "status,created_at,updated_at,error,owner_id,lease_expires_at,"
                "execution_token) VALUES(?,?,?,?,?,?,?,NULL,?,?,1)",
                (run_id, session_id, platform, message_id, RunStatus.RUNNING,
                 now, now, self.owner_id, self._lease_expiry()),
            )
            db.execute(
                "INSERT INTO inbound_messages VALUES(?,?,?,?,?,NULL,?)",
                (platform, message_id, run_id, sender_id, _json(metadata or {}), now),
            )
            if user_content is not None:
                assert initial_checkpoint is not None
                db.execute(
                    "INSERT INTO messages(session_id,run_id,role,content,created_at) "
                    "VALUES(?,?,'user',?,?)",
                    (session_id, run_id, user_content, now),
                )
                rows = db.execute(
                    "SELECT role,content FROM (SELECT id,role,content FROM messages "
                    "WHERE session_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
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
                    "VALUES(?,1,'inbound_started',?,?)",
                    (run_id, _json(checkpoint), now),
                )
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return InboundStart(True, _run(row))

    def start_inbound(
        self,
        *,
        platform: str,
        conversation_id: str,
        sender_id: str,
        message_id: str,
        user_content: str,
        initial_checkpoint: dict[str, Any],
        recent_message_limit: int = 20,
        metadata: dict[str, Any] | None = None,
    ) -> InboundStart:
        return self.begin_inbound(
            platform=platform,
            conversation_id=conversation_id,
            sender_id=sender_id,
            message_id=message_id,
            metadata=metadata,
            user_content=user_content,
            initial_checkpoint=initial_checkpoint,
            recent_message_limit=recent_message_limit,
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _run(row) if row else None

    def append_message(
        self, session_id: str, run_id: str, role: str, content: str
    ) -> StoredMessage:
        if role not in {"user", "assistant"}:
            raise ValueError(f"Unsupported message role: {role}")
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO messages(session_id,run_id,role,content,created_at) "
                "VALUES(?,?,?,?,?)",
                (session_id, run_id, role, content, _now()),
            )
            row = db.execute(
                "SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        return _message(row)

    def recent_messages(self, session_id: str, limit: int) -> list[StoredMessage]:
        if limit <= 0:
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM (SELECT * FROM messages WHERE session_id=? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (session_id, limit),
            ).fetchall()
        return [_message(row) for row in rows]

    def messages_after(
        self, session_id: str, through_message_id: int = 0
    ) -> list[StoredMessage]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id",
                (session_id, through_message_id),
            ).fetchall()
        return [_message(row) for row in rows]

    def latest_summary(self, session_id: str) -> SessionSummary | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM session_summaries WHERE session_id=? "
                "ORDER BY version DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return _summary(row) if row else None

    def save_summary(
        self, session_id: str, content: str, through_message_id: int
    ) -> SessionSummary:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            covered = db.execute(
                "SELECT id FROM messages WHERE id=? AND session_id=?",
                (through_message_id, session_id),
            ).fetchone()
            if covered is None:
                raise ValueError(
                    "summary through_message_id does not belong to session"
                )
            latest = db.execute(
                "SELECT version,through_message_id FROM session_summaries "
                "WHERE session_id=? ORDER BY version DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if latest and through_message_id <= latest["through_message_id"]:
                raise ValueError("summary coverage must advance")
            version = int(latest["version"]) + 1 if latest else 1
            cursor = db.execute(
                "INSERT INTO session_summaries(session_id,version,content,"
                "through_message_id,created_at) VALUES(?,?,?,?,?)",
                (session_id, version, content, through_message_id, _now()),
            )
            row = db.execute(
                "SELECT * FROM session_summaries WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone()
        return _summary(row)

    def save_checkpoint(
        self,
        run_id: str,
        phase: str,
        state: dict[str, Any],
        *,
        execution_token: int | None = None,
    ) -> Checkpoint:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_owner(db, run_id, execution_token)
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM checkpoints WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            cursor = db.execute(
                "INSERT INTO checkpoints(run_id,sequence,phase,state_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (run_id, sequence, phase, _json(state), _now()),
            )
            row = db.execute(
                "SELECT * FROM checkpoints WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        return _checkpoint(row)

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return _checkpoint(row) if row else None

    def transition_run(
        self,
        run_id: str,
        status: RunStatus,
        error: str | None = None,
        *,
        execution_token: int | None = None,
    ) -> bool:
        sources = tuple(RunStateMachine.sources_for(status))
        if not sources:
            return False
        placeholders = ",".join("?" for _ in sources)
        with self._connect() as db:
            ownership = ""
            values: list[Any] = [
                status, _now(), error[:1000] if error else None, run_id,
                *sources,
            ]
            if execution_token is not None:
                ownership = " AND owner_id=? AND execution_token=?"
                values.extend((self.owner_id, execution_token))
            changed = db.execute(
                "UPDATE runs SET status=?,updated_at=?,error=? WHERE id=? "
                f"AND status IN ({placeholders}){ownership}", values,
            ).rowcount
        return changed == 1

    def claim_run(
        self, run_id: str, expected_statuses: set[RunStatus]
    ) -> bool:
        if not expected_statuses:
            return False
        statuses = tuple(expected_statuses)
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as db:
            changed = db.execute(
                "UPDATE runs SET status=?,updated_at=?,error=NULL,owner_id=?,"
                "lease_expires_at=?,execution_token=execution_token+1 WHERE id=? "
                f"AND status IN ({placeholders})",
                (RunStatus.RUNNING, _now(), self.owner_id, self._lease_expiry(),
                 run_id, *statuses),
            ).rowcount
        return changed == 1

    def complete_run(
        self,
        run_id: str,
        response: str,
        *,
        execution_token: int | None = None,
    ) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                "SELECT session_id,status,owner_id,execution_token FROM runs "
                "WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                return False
            if run["status"] == RunStatus.COMPLETED:
                existing = db.execute(
                    "SELECT response FROM inbound_messages WHERE run_id=?", (run_id,)
                ).fetchone()
                return existing is not None and existing["response"] == response
            if run["status"] != RunStatus.RUNNING:
                return False
            token = execution_token or int(run["execution_token"])
            if run["owner_id"] != self.owner_id or int(
                run["execution_token"]
            ) != token:
                return False
            changed = db.execute(
                "UPDATE runs SET status=?,updated_at=?,error=NULL,owner_id=NULL,"
                "lease_expires_at=NULL WHERE id=? AND status=? AND owner_id=? "
                "AND execution_token=?",
                (RunStatus.COMPLETED, _now(), run_id, RunStatus.RUNNING,
                 self.owner_id, token),
            ).rowcount
            if changed != 1:
                return False
            db.execute(
                "INSERT INTO messages(session_id,run_id,role,content,created_at) "
                "VALUES(?,?,'assistant',?,?)",
                (run["session_id"], run_id, response, _now()),
            )
            db.execute(
                "UPDATE inbound_messages SET response=? WHERE run_id=?",
                (response, run_id),
            )
        return True

    def cached_response(self, run_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT response FROM inbound_messages WHERE run_id=?", (run_id,)
            ).fetchone()
        return row["response"] if row else None

    def interrupt_incomplete_runs(self) -> list[RunRecord]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            rows = db.execute(
                "SELECT id FROM runs WHERE status=? AND (owner_id=? OR "
                "lease_expires_at IS NULL OR lease_expires_at<=?) ORDER BY created_at",
                (RunStatus.RUNNING, self.owner_id, now),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE runs SET status=?,updated_at=?,owner_id=NULL,"
                    "lease_expires_at=NULL WHERE id=? AND status=?",
                    (RunStatus.INTERRUPTED, now, row["id"], RunStatus.RUNNING),
                )
            return [
                _run(db.execute("SELECT * FROM runs WHERE id=?", (row["id"],)).fetchone())
                for row in rows
            ]

    def renew_run(
        self, run_id: str, execution_token: int | None = None
    ) -> bool:
        if execution_token is None:
            run = self.get_run(run_id)
            if run is None or run.owner_id != self.owner_id:
                return False
            execution_token = run.execution_token
        with self._connect() as db:
            changed = db.execute(
                "UPDATE runs SET lease_expires_at=?,updated_at=? WHERE id=? "
                "AND status=? AND owner_id=? AND execution_token=?",
                (self._lease_expiry(), _now(), run_id, RunStatus.RUNNING,
                 self.owner_id, execution_token),
            ).rowcount
        return changed == 1

    def _lease_expiry(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(
            seconds=self.lease_seconds
        )).isoformat()

    def list_recoverable_runs(self) -> list[RunRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE status=? ORDER BY created_at",
                (RunStatus.INTERRUPTED,),
            ).fetchall()
        return [_run(row) for row in rows]

    def claim_tool(
        self,
        run_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        execution_token: int | None = None,
    ) -> ToolClaim:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tool_executions WHERE run_id=? AND call_id=?",
                (run_id, call_id),
            ).fetchone()
            if row:
                try:
                    stored_arguments = _json(json.loads(row["arguments_json"]))
                except (TypeError, ValueError):
                    stored_arguments = None
                if (row["tool_name"] != tool_name or
                        stored_arguments != _json(arguments)):
                    raise ValueError(
                        "tool execution identity conflicts with existing call_id"
                    )
                if row["status"] == "completed":
                    return ToolClaim(False, output=row["output"])
                return ToolClaim(False, is_uncertain=row["status"] == "running")
            self._assert_owner(db, run_id, execution_token)
            db.execute(
                "INSERT INTO tool_executions VALUES(?,?,?,?,?,NULL,NULL,?,NULL)",
                (run_id, call_id, tool_name, _json(arguments), "running", _now()),
            )
            return ToolClaim(True)

    def get_tool(
        self,
        run_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolClaim | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM tool_executions WHERE run_id=? AND call_id=?",
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
        row: sqlite3.Row, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        try:
            stored_arguments = _json(json.loads(row["arguments_json"]))
        except (TypeError, ValueError):
            stored_arguments = None
        if row["tool_name"] != tool_name or stored_arguments != _json(arguments):
            raise ValueError(
                "tool execution identity conflicts with existing call_id"
            )

    def complete_tool(
        self,
        run_id: str,
        call_id: str,
        output: str,
        *,
        execution_token: int | None = None,
    ) -> bool:
        with self._connect() as db:
            self._assert_owner(db, run_id, execution_token)
            changed = db.execute(
                "UPDATE tool_executions SET status='completed',output=?,finished_at=? "
                "WHERE run_id=? AND call_id=? AND status='running'",
                (output, _now(), run_id, call_id),
            ).rowcount
        return changed == 1

    def fail_tool(
        self,
        run_id: str,
        call_id: str,
        error: str,
        *,
        execution_token: int | None = None,
    ) -> bool:
        with self._connect() as db:
            self._assert_owner(db, run_id, execution_token)
            changed = db.execute(
                "UPDATE tool_executions SET status='failed',error=?,finished_at=? "
                "WHERE run_id=? AND call_id=? AND status='running'",
                (error[:1000], _now(), run_id, call_id),
            ).rowcount
        return changed == 1

    def _assert_owner(
        self,
        db: sqlite3.Connection,
        run_id: str,
        execution_token: int | None,
    ) -> int:
        row = db.execute(
            "SELECT status,owner_id,execution_token FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        token = execution_token or (
            int(row["execution_token"]) if row is not None else None
        )
        if (
            row is None
            or row["status"] != RunStatus.RUNNING
            or row["owner_id"] != self.owner_id
            or int(row["execution_token"]) != token
        ):
            raise RunLeaseLost(f"Run lease lost: {run_id}")
        return token


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_id(platform: str, conversation_id: str) -> str:
    value = json.dumps(
        [platform, conversation_id], ensure_ascii=False, separators=(",", ":")
    )
    return "session_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        session_id=row["session_id"],
        inbound_platform=row["inbound_platform"],
        inbound_message_id=row["inbound_message_id"],
        status=RunStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        error=row["error"],
        owner_id=row["owner_id"],
        lease_expires_at=(
            datetime.fromisoformat(row["lease_expires_at"])
            if row["lease_expires_at"] else None
        ),
        execution_token=int(row["execution_token"]),
    )


def _message(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=row["id"], session_id=row["session_id"], run_id=row["run_id"],
        role=row["role"], content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _checkpoint(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        id=row["id"], run_id=row["run_id"], sequence=row["sequence"],
        phase=row["phase"], state=json.loads(row["state_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _summary(row: sqlite3.Row) -> SessionSummary:
    return SessionSummary(
        id=row['id'], session_id=row['session_id'], version=row['version'],
        content=row['content'], through_message_id=row['through_message_id'],
        created_at=datetime.fromisoformat(row['created_at']),
    )
