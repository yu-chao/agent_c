from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import ApprovalAction, ApprovalDecision, ApprovalRequest
from .models import ApprovalStatus, RuntimeIdentity


class SQLiteApprovalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            db.executescript("""
            CREATE TABLE IF NOT EXISTS approvals (
              id TEXT PRIMARY KEY, platform TEXT, conversation_id TEXT,
              sender_id TEXT, message_id TEXT, metadata_json TEXT,
              tool_call_id TEXT, tool_name TEXT, tool_input_json TEXT,
              arguments_hash TEXT, continuation_json TEXT, status TEXT,
              created_at TEXT, expires_at TEXT, decided_by TEXT,
              decided_at TEXT, error TEXT, resumed_at TEXT);
            CREATE INDEX IF NOT EXISTS approvals_status_idx
              ON approvals(status,resumed_at);
            CREATE TABLE IF NOT EXISTS approval_events (
              message_id TEXT PRIMARY KEY, approval_id TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS approval_links (
              run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
              approval_id TEXT NOT NULL UNIQUE,
              PRIMARY KEY(run_id, tool_call_id),
              FOREIGN KEY(approval_id) REFERENCES approvals(id));
            """)
            if version < 1:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute(
                    "SELECT id,tool_call_id,continuation_json FROM approvals"
                ).fetchall()
                for row in rows:
                    try:
                        continuation = json.loads(row["continuation_json"])
                    except (TypeError, ValueError):
                        continue
                    run_id = (
                        continuation.get("run_id")
                        if isinstance(continuation, dict) else None
                    )
                    if isinstance(run_id, str) and run_id and row["tool_call_id"]:
                        db.execute(
                            "INSERT OR IGNORE INTO approval_links VALUES(?,?,?)",
                            (run_id, row["tool_call_id"], row["id"]),
                        )
                db.execute("PRAGMA user_version=1")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create(self, item: ApprovalRequest):
        i = item.identity
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run_id = item.continuation.get("run_id")
            if run_id:
                existing = db.execute(
                    "SELECT approvals.* FROM approval_links "
                    "JOIN approvals ON approvals.id=approval_links.approval_id "
                    "WHERE approval_links.run_id=? AND approval_links.tool_call_id=?",
                    (run_id, item.tool_call_id),
                ).fetchone()
                if existing:
                    return _request(existing)
            db.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
                (item.id, i.platform, i.conversation_id, i.sender_id, i.message_id,
                 _json(i.metadata), item.tool_call_id, item.tool_name,
                 _json(item.tool_input), item.arguments_hash,
                 _json(item.continuation), item.status, _time(item.created_at),
                 _time(item.expires_at)))
            if run_id:
                db.execute(
                    "INSERT INTO approval_links VALUES(?,?,?)",
                    (run_id, item.tool_call_id, item.id),
                )
        return item

    def get(self, approval_id: str):
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return _request(row) if row else None

    def decide(self, approval_id, action, identity, event_message_id, *, now=None):
        current = now or datetime.now(timezone.utc)
        try:
            action = ApprovalAction(action)
        except ValueError:
            return ApprovalDecision(False, None, "unsupported approval action")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row:
                return ApprovalDecision(False, None, "approval not found")
            item = _request(row)
            original = item.identity
            if (original.platform, original.conversation_id, original.sender_id) != (
                identity.platform, identity.conversation_id, identity.sender_id):
                return ApprovalDecision(False, item.status, "unauthorized", item)
            if item.status is ApprovalStatus.PENDING and item.expires_at <= current:
                db.execute("UPDATE approvals SET status=? WHERE id=? AND status=?",
                           (ApprovalStatus.EXPIRED, approval_id, ApprovalStatus.PENDING))
                item = _get(db, approval_id)
                return ApprovalDecision(False, item.status, "approval expired", item)
            if db.execute("SELECT 1 FROM approval_events WHERE message_id=?",
                          (event_message_id,)).fetchone():
                return ApprovalDecision(False, item.status, "event already handled", item)
            if item.status is not ApprovalStatus.PENDING:
                return ApprovalDecision(False, item.status, "approval already handled", item)
            target = (ApprovalStatus.APPROVED if action is ApprovalAction.CONFIRM
                      else ApprovalStatus.REJECTED)
            db.execute("INSERT INTO approval_events VALUES(?,?,?)",
                       (event_message_id, approval_id, _time(current)))
            changed = db.execute(
                """UPDATE approvals SET status=?,decided_by=?,decided_at=?
                WHERE id=? AND status=?""",
                (target, identity.sender_id, _time(current), approval_id,
                 ApprovalStatus.PENDING)).rowcount
            item = _get(db, approval_id)
            return ApprovalDecision(changed == 1, item.status, target.value, item)

    def expire_pending(self, *, now=None):
        current = now or datetime.now(timezone.utc)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id FROM approvals WHERE status=? AND expires_at<=?",
                (ApprovalStatus.PENDING, _time(current))).fetchall()
            db.execute("UPDATE approvals SET status=? WHERE status=? AND expires_at<=?",
                       (ApprovalStatus.EXPIRED, ApprovalStatus.PENDING, _time(current)))
            return [_get(db, row["id"]) for row in rows]

    def claim_execution(self, approval_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE approvals SET status=? WHERE id=? AND status=?",
                (ApprovalStatus.EXECUTING, approval_id,
                 ApprovalStatus.APPROVED)).rowcount
            return _get(db, approval_id) if changed else None

    def complete(self, approval_id):
        return self._transition(approval_id, ApprovalStatus.EXECUTING,
                                ApprovalStatus.COMPLETED)

    def fail(self, approval_id, error):
        with self._connect() as db:
            return db.execute(
                "UPDATE approvals SET status=?,error=? WHERE id=? AND status=?",
                (ApprovalStatus.FAILED, str(error)[:1000], approval_id,
                 ApprovalStatus.EXECUTING)).rowcount == 1

    def mark_consumed(self, approval_id):
        with self._connect() as db:
            return db.execute(
                """UPDATE approvals SET resumed_at=? WHERE id=? AND resumed_at IS NULL
                AND status IN (?,?)""",
                (_time(datetime.now(timezone.utc)), approval_id,
                 ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED)).rowcount == 1

    def list_resumable(self):
        return self._list(
            """SELECT * FROM approvals WHERE status=? OR
            (status IN (?,?) AND resumed_at IS NULL) ORDER BY created_at""",
            (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED,
             ApprovalStatus.EXPIRED))

    def list_uncertain(self):
        return self._list("SELECT * FROM approvals WHERE status=?",
                          (ApprovalStatus.EXECUTING,))

    def _list(self, query, values):
        with self._connect() as db:
            return [_request(row) for row in db.execute(query, values).fetchall()]

    def _transition(self, approval_id, source, target):
        with self._connect() as db:
            return db.execute(
                "UPDATE approvals SET status=? WHERE id=? AND status=?",
                (target, approval_id, source)).rowcount == 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _time(value):
    return value.astimezone(timezone.utc).isoformat()


def _get(db, approval_id):
    return _request(db.execute(
        "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())


def _request(row):
    def optional_time(value):
        return datetime.fromisoformat(value) if value else None
    return ApprovalRequest(
        id=row["id"],
        identity=RuntimeIdentity(
            row["platform"], row["conversation_id"], row["sender_id"],
            row["message_id"], json.loads(row["metadata_json"])),
        tool_call_id=row["tool_call_id"], tool_name=row["tool_name"],
        tool_input=json.loads(row["tool_input_json"]),
        arguments_hash=row["arguments_hash"],
        continuation=json.loads(row["continuation_json"]),
        status=ApprovalStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        decided_by=row["decided_by"], decided_at=optional_time(row["decided_at"]),
        error=row["error"], resumed_at=optional_time(row["resumed_at"]))
