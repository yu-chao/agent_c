from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Task:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    worktree: str | None = None
    run_id: str | None = None
    trigger_id: str | None = None
    error: str | None = None
    version: int = 1


class TaskGraph:
    """持久化任务图；领取和状态迁移均使用 SQLite CAS。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.db"
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, subject TEXT NOT NULL,
                description TEXT NOT NULL, status TEXT NOT NULL, owner TEXT,
                blocked_by TEXT NOT NULL, worktree TEXT, run_id TEXT,
                trigger_id TEXT, error TEXT, version INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        self._import_legacy()

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def create(self, subject: str, description: str = "",
               blocked_by: list[str] | None = None, *,
               task_id: str | None = None) -> Task:
        task = Task(task_id or f"task_{uuid.uuid4().hex}", subject,
                    description, blocked_by=list(blocked_by or []))
        now = _now()
        with self._db() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       _values(task, now, now))
        return task

    def save(self, task: Task) -> None:
        with self._db() as db:
            changed = db.execute(
                "UPDATE tasks SET subject=?,description=?,status=?,owner=?,"
                "blocked_by=?,worktree=?,run_id=?,trigger_id=?,error=?,"
                "version=version+1,updated_at=? WHERE id=? AND version=?",
                (task.subject, task.description, task.status, task.owner,
                 _json(task.blocked_by), task.worktree, task.run_id,
                 task.trigger_id, task.error, _now(), task.id, task.version),
            ).rowcount
        if changed != 1:
            raise RuntimeError(f"Task changed concurrently: {task.id}")

    def load(self, task_id: str) -> Task:
        with self._db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(f"Task not found: {task_id}")
        return _task(row)

    def list(self, statuses: set[str] | None = None) -> list[Task]:
        query, args = "SELECT * FROM tasks", ()
        if statuses:
            args = tuple(sorted(statuses))
            query += " WHERE status IN (" + ",".join("?" for _ in args) + ")"
        with self._db() as db:
            rows = db.execute(query + " ORDER BY created_at,id", args).fetchall()
        return [_task(row) for row in rows]

    def find_by_run(self, run_id: str) -> Task | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE run_id=? ORDER BY created_at LIMIT 1",
                (run_id,)).fetchone()
        return _task(row) if row else None

    def can_start(self, task_id: str) -> bool:
        task = self.load(task_id)
        with self._db() as db:
            blocked, missing = _dependency_problems(db, task.blocked_by)
        return not blocked and not missing

    def claim(self, task_id: str, owner: str) -> str:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return f"Task {task_id} was not found"
            task = _task(row)
            if task.status != "pending":
                return f"Task {task_id} is {task.status}, cannot claim"
            blocked, missing = _dependency_problems(db, task.blocked_by)
            if blocked or missing:
                parts = []
                if blocked:
                    parts.append(f"blocked by: {blocked}")
                if missing:
                    parts.append(f"missing deps: {missing}")
                return "Cannot start: " + ", ".join(parts)
            changed = db.execute(
                "UPDATE tasks SET status='in_progress',owner=?,version=version+1,"
                "updated_at=? WHERE id=? AND status='pending' AND version=?",
                (owner, _now(), task_id, task.version)).rowcount
            if changed != 1:
                return f"Task {task_id} changed concurrently, cannot claim"
        return f"Claimed {task.id} ({task.subject})"

    def complete(self, task_id: str) -> str:
        task = self.load(task_id)
        if not self.transition(task_id, {"in_progress"}, "completed"):
            return f"Task {task_id} is {task.status}, cannot complete"
        return f"Completed {task.id} ({task.subject})"

    def transition(self, task_id: str, expected: set[str], status: str, *,
                   owner: str | None = None, run_id: str | None = None,
                   trigger_id: str | None = None,
                   error: str | None = None) -> bool:
        if not expected:
            return False
        fields, args = ["status=?", "version=version+1", "updated_at=?"], [status, _now()]
        for name, value in (("owner", owner), ("run_id", run_id),
                            ("trigger_id", trigger_id), ("error", error)):
            if value is not None:
                fields.append(f"{name}=?")
                args.append(value[:1000] if name == "error" else value)
        statuses = tuple(sorted(expected))
        args.extend((task_id, *statuses))
        with self._db() as db:
            changed = db.execute(
                f"UPDATE tasks SET {','.join(fields)} WHERE id=? AND status IN ("
                + ",".join("?" for _ in statuses) + ")", args).rowcount
        return changed == 1

    def bind_run(self, task_id: str, run_id: str, trigger_id: str) -> bool:
        with self._db() as db:
            return db.execute(
                "UPDATE tasks SET run_id=?,trigger_id=?,version=version+1,"
                "updated_at=? WHERE id=? AND status='in_progress' AND "
                "(run_id IS NULL OR run_id=?)",
                (run_id, trigger_id, _now(), task_id, run_id)).rowcount == 1

    def reset_for_trigger(self, task_id: str, trigger_id: str) -> bool:
        """Prepare a completed recurring task for a different cron slot."""
        with self._db() as db:
            return db.execute(
                "UPDATE tasks SET status='pending',owner=NULL,run_id=NULL,"
                "trigger_id=?,error=NULL,version=version+1,updated_at=? "
                "WHERE id=? AND status='completed' AND "
                "(trigger_id IS NULL OR trigger_id<>?)",
                (trigger_id, _now(), task_id, trigger_id)).rowcount == 1

    def _import_legacy(self):
        for path in self.root.glob("task_*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                task = Task(**raw)
                with self._db() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        _values(task, _now(), _now()))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue


def _dependency_problems(db, dependency_ids):
    blocked, missing = [], []
    for task_id in dependency_ids:
        row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            missing.append(task_id)
        elif row["status"] != "completed":
            blocked.append(task_id)
    return blocked, missing


def _values(task, created, updated):
    return (task.id, task.subject, task.description, task.status, task.owner,
            _json(task.blocked_by), task.worktree, task.run_id, task.trigger_id,
            task.error, task.version, created, updated)


def _task(row):
    return Task(row["id"], row["subject"], row["description"], row["status"],
                row["owner"], json.loads(row["blocked_by"]), row["worktree"],
                row["run_id"], row["trigger_id"], row["error"], row["version"])


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now():
    return datetime.now(timezone.utc).isoformat()
