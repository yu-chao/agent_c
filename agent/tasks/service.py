from __future__ import annotations

import uuid
from typing import Any

from agent.approval import RuntimeIdentity
from agent.core.results import Completed, InProgress, PendingApproval
from agent.core.run_state import RunStatus

from .graph import TaskGraph


class TaskService:
    """通过 AgentRuntime 的统一 Run 生命周期执行持久化任务。"""

    def __init__(self, graph: TaskGraph, runtime: Any, *,
                 owner: str | None = None):
        if runtime.session_store is None:
            raise ValueError("Task execution requires session storage")
        self.graph = graph
        self.runtime = runtime
        self.owner = owner or runtime.owner_id or f"task_owner_{uuid.uuid4().hex}"

    def execute(self, task_id: str, trigger_id: str | None = None, *,
                recurring: bool = False):
        task = self.graph.load(task_id)
        trigger_id = trigger_id or f"manual_{uuid.uuid4().hex}"
        if recurring and task.status == "completed":
            self.graph.reset_for_trigger(task_id, trigger_id)
            task = self.graph.load(task_id)
        if task.status in {"interrupted", "paused"} and task.run_id:
            run = self.runtime.session_store.get_run(task.run_id)
            if run and run.status is RunStatus.WAITING_APPROVAL:
                return InProgress(task.run_id)
            return self.resume(task_id)
        claimed = self.graph.claim(task_id, self.owner)
        if not claimed.startswith("Claimed "):
            return InProgress(task.run_id or task.id)
        identity = RuntimeIdentity(
            "task", task.id, self.owner, trigger_id,
            {"task_id": task.id, "trigger_id": trigger_id},
        )
        prompt = task.subject + (f"\n\n{task.description}" if task.description else "")
        try:
            result = self.runtime.run_turn(
                prompt,
                identity,
                lambda run: self.graph.bind_run(task.id, run.id, trigger_id),
            )
        except Exception as exc:
            run_id = self.runtime._identity_run_id(identity)
            if run_id:
                self.graph.bind_run(task.id, run_id, trigger_id)
            self.graph.transition(
                task.id, {"in_progress"}, "interrupted", run_id=run_id,
                trigger_id=trigger_id, error=str(exc))
            raise
        run_id = self.runtime._identity_run_id(identity)
        if run_id:
            self.graph.bind_run(task.id, run_id, trigger_id)
        return self._apply_result(task.id, result)

    def resume(self, task_id: str):
        task = self.graph.load(task_id)
        if not task.run_id:
            raise ValueError(f"Task has no Run to resume: {task_id}")
        run = self.runtime.session_store.get_run(task.run_id)
        if run and run.status is RunStatus.WAITING_APPROVAL:
            return InProgress(task.run_id)
        if not self.graph.transition(
                task_id, {"interrupted", "paused"}, "in_progress",
                owner=self.owner):
            return InProgress(task.run_id)
        try:
            result = self.runtime.resume_run(task.run_id)
        except Exception as exc:
            self.graph.transition(task_id, {"in_progress"}, "interrupted",
                                  error=str(exc))
            raise
        return self._apply_result(task_id, result)

    def pause(self, task_id: str) -> bool:
        task = self.graph.load(task_id)
        changed = self.graph.transition(
            task_id, {"in_progress", "interrupted"}, "paused")
        if changed and task.run_id:
            self._transition_run(task.run_id, RunStatus.INTERRUPTED, "task paused")
        return changed

    def cancel(self, task_id: str) -> bool:
        return self._terminal(task_id, "cancelled", RunStatus.CANCELLED)

    def fail(self, task_id: str, error: str) -> bool:
        return self._terminal(task_id, "failed", RunStatus.FAILED, error)

    def recover(self):
        return [(task.id, self.resume(task.id))
                for task in self.graph.list({"interrupted"})]

    def reconcile_run(self, run_id: str):
        task = self.graph.find_by_run(run_id)
        run = self.runtime.session_store.get_run(run_id)
        if task is None or run is None:
            return task
        target = {
            RunStatus.COMPLETED: "completed",
            RunStatus.FAILED: "failed",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.INTERRUPTED: "interrupted",
            RunStatus.WAITING_APPROVAL: "paused",
            RunStatus.RUNNING: "in_progress",
        }[run.status]
        if task.status != target:
            self.graph.transition(
                task.id, {task.status}, target, error=run.error)
        return self.graph.load(task.id)

    def _apply_result(self, task_id, result):
        if isinstance(result, Completed):
            self.graph.transition(task_id, {"in_progress"}, "completed")
        elif isinstance(result, PendingApproval):
            self.graph.transition(task_id, {"in_progress"}, "paused")
        return result

    def _terminal(self, task_id, task_status, run_status, error=None):
        task = self.graph.load(task_id)
        changed = self.graph.transition(
            task_id, {"pending", "in_progress", "interrupted", "paused"},
            task_status, error=error)
        if changed and task.run_id:
            self._transition_run(task.run_id, run_status, error)
        return changed

    def _transition_run(self, run_id, status, error=None):
        run = self.runtime.session_store.get_run(run_id)
        if run is None or run.status not in {
                RunStatus.RUNNING, RunStatus.INTERRUPTED,
                RunStatus.WAITING_APPROVAL}:
            return False
        token = run.execution_token if run.status is RunStatus.RUNNING else None
        return self.runtime.session_store.transition_run(
            run.id, status, error, execution_token=token)
