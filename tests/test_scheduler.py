from datetime import datetime

from agent_runtime.core import AgentRuntime
from agent_runtime.models import ModelResponse, TextBlock
from agent_runtime.scheduler import SchedulerService, cron_trigger_id
from agent_runtime.sessions import SQLiteSessionStore
from agent_runtime.tasks import TaskGraph, TaskService
from agent_runtime.tools import ToolRegistry


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return self.responses.pop(0)


def services(tmp_path, responses):
    model = FakeModel(responses)
    runtime = AgentRuntime(
        model, ToolRegistry(),
        session_store=SQLiteSessionStore(tmp_path / "runs.db"),
    )
    tasks = TaskService(TaskGraph(tmp_path / "tasks"), runtime)
    scheduler = SchedulerService(tmp_path / "scheduler.db", tasks)
    return model, tasks, scheduler


def test_same_cron_slot_is_dispatched_only_once(tmp_path):
    model, tasks, scheduler = services(
        tmp_path, [
            ModelResponse([TextBlock("done")]),
            ModelResponse([TextBlock("done again")]),
        ]
    )
    task = tasks.graph.create("once")
    schedule = scheduler.add(task.id, "*/15 * * * *")
    at = datetime(2026, 7, 20, 12, 30, 45)

    first = scheduler.dispatch_due(at)
    second = SchedulerService(
        tmp_path / "scheduler.db", tasks
    ).dispatch_due(at.replace(second=1))

    assert len(first) == 1
    assert second == []
    assert model.calls == 1
    assert scheduler.triggers()[0].id == cron_trigger_id(schedule.id, at)
    assert scheduler.triggers()[0].status == "completed"

    third = scheduler.dispatch_due(at.replace(minute=45, second=0))

    assert len(third) == 1
    assert model.calls == 2
    assert len(scheduler.triggers()) == 2
    assert tasks.graph.load(task.id).trigger_id == third[0][0].id


def test_deferred_trigger_runs_after_dependency_completes(tmp_path):
    model, tasks, scheduler = services(
        tmp_path, [ModelResponse([TextBlock("dependent done")])]
    )
    dependency = tasks.graph.create("dependency")
    dependent = tasks.graph.create("dependent", blocked_by=[dependency.id])
    scheduler.add(dependent.id, "* * * * *")

    scheduler.dispatch_due(datetime(2026, 7, 20, 9, 0))
    assert tasks.graph.load(dependent.id).status == "pending"
    assert scheduler.triggers()[0].status == "deferred"
    assert model.calls == 0

    tasks.graph.claim(dependency.id, "worker")
    tasks.graph.complete(dependency.id)
    scheduler.recover()

    assert tasks.graph.load(dependent.id).status == "completed"
    assert scheduler.triggers()[0].status == "completed"
    assert model.calls == 1
