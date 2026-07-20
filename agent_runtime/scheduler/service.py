from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .cron import cron_matches, cron_slot, cron_trigger_id, validate_cron


@dataclass(frozen=True)
class Schedule:
    id: str
    task_id: str
    cron: str
    enabled: bool = True


@dataclass(frozen=True)
class ScheduleTrigger:
    id: str
    schedule_id: str
    task_id: str
    scheduled_for: str
    status: str
    error: str | None = None


class SchedulerService:
    """持久化 cron 调度器；同一计划和时间片只产生一个触发。"""

    def __init__(self, path: str | Path, task_service):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.task_service = task_service
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schedules (
                  id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                  cron TEXT NOT NULL, enabled INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS schedule_triggers (
                  id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL,
                  task_id TEXT NOT NULL, scheduled_for TEXT NOT NULL,
                  status TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL,
                  UNIQUE(schedule_id, scheduled_for));
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        return db

    def add(self, task_id: str, cron: str, *, schedule_id: str | None = None):
        error = validate_cron(cron)
        if error:
            raise ValueError(error)
        schedule = Schedule(schedule_id or f"schedule_{uuid.uuid4().hex}",
                            task_id, cron)
        with self._db() as db:
            db.execute("INSERT INTO schedules VALUES(?,?,?,1)",
                       (schedule.id, task_id, cron))
        return schedule

    def dispatch_due(self, at: datetime):
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM schedules WHERE enabled=1 ORDER BY id").fetchall()
        dispatched = []
        for row in rows:
            if not cron_matches(row["cron"], at):
                continue
            trigger = self._claim(row["id"], row["task_id"], at)
            if trigger is not None:
                dispatched.append(self._execute(trigger))
        return dispatched

    def recover(self):
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM schedule_triggers WHERE status IN "
                "('running','deferred','interrupted') ORDER BY scheduled_for,id"
            ).fetchall()
        return [self._execute(_trigger(row)) for row in rows]

    def triggers(self):
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM schedule_triggers ORDER BY scheduled_for,id").fetchall()
        return [_trigger(row) for row in rows]

    def _claim(self, schedule_id, task_id, at):
        trigger = ScheduleTrigger(
            cron_trigger_id(schedule_id, at), schedule_id, task_id,
            cron_slot(at), "running")
        with self._db() as db:
            changed = db.execute(
                "INSERT OR IGNORE INTO schedule_triggers "
                "VALUES(?,?,?,?,?,NULL,?)",
                (trigger.id, schedule_id, task_id, trigger.scheduled_for,
                 trigger.status, _now())).rowcount
        return trigger if changed == 1 else None

    def _execute(self, trigger):
        try:
            result = self.task_service.execute(
                trigger.task_id, trigger.id, recurring=True)
            task_status = self.task_service.graph.load(trigger.task_id).status
            status = {"pending": "deferred", "interrupted": "interrupted",
                      "failed": "failed", "cancelled": "cancelled",
                      "completed": "completed"}.get(task_status, "running")
            self._set_status(trigger.id, status)
            return trigger, result
        except Exception as exc:
            self._set_status(trigger.id, "interrupted", str(exc))
            raise

    def _set_status(self, trigger_id, status, error=None):
        with self._db() as db:
            db.execute(
                "UPDATE schedule_triggers SET status=?,error=?,updated_at=? "
                "WHERE id=?", (status, error, _now(), trigger_id))


def _trigger(row):
    return ScheduleTrigger(row["id"], row["schedule_id"], row["task_id"],
                           row["scheduled_for"], row["status"], row["error"])


def _now():
    return datetime.now(timezone.utc).isoformat()
