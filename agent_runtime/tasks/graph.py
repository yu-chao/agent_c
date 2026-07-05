from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] | None = None
    worktree: str | None = None


class TaskGraph:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, subject: str, description: str = "", blocked_by: list[str] | None = None) -> Task:
        task = Task(
            id=f"task_{int(time.time() * 1000)}_{len(self.list())}",
            subject=subject,
            description=description,
            blocked_by=blocked_by or [],
        )
        self.save(task)
        return task

    def save(self, task: Task) -> None:
        self._path(task.id).write_text(json.dumps(asdict(task), indent=2), encoding="utf-8")

    def load(self, task_id: str) -> Task:
        data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
        return Task(**data)

    def list(self) -> list[Task]:
        return [Task(**json.loads(path.read_text(encoding="utf-8"))) for path in sorted(self.root.glob("task_*.json"))]

    def can_start(self, task_id: str) -> bool:
        task = self.load(task_id)
        for dep_id in task.blocked_by or []:
            if not self._path(dep_id).exists() or self.load(dep_id).status != "completed":
                return False
        return True

    def claim(self, task_id: str, owner: str) -> str:
        task = self.load(task_id)
        if task.status != "pending":
            return f"Task {task_id} is {task.status}, cannot claim"
        if not self.can_start(task_id):
            blockers = [
                dep_id
                for dep_id in task.blocked_by or []
                if self._path(dep_id).exists() and self.load(dep_id).status != "completed"
            ]
            missing = [dep_id for dep_id in task.blocked_by or [] if not self._path(dep_id).exists()]
            parts = []
            if blockers:
                parts.append(f"blocked by: {blockers}")
            if missing:
                parts.append(f"missing deps: {missing}")
            return "Cannot start: " + ", ".join(parts)
        task.owner = owner
        task.status = "in_progress"
        self.save(task)
        return f"Claimed {task.id} ({task.subject})"

    def complete(self, task_id: str) -> str:
        task = self.load(task_id)
        if task.status != "in_progress":
            return f"Task {task_id} is {task.status}, cannot complete"
        task.status = "completed"
        self.save(task)
        return f"Completed {task.id} ({task.subject})"

    def _path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.json"
