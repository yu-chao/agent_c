import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.core import AgentRuntime
from agent.models import ModelResponse, TextBlock
from agent.sessions import RunStatus, SQLiteSessionStore
from agent.tasks import TaskGraph, TaskService
from agent.tools import ToolRegistry


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.before_generate = None

    def generate(self, request):
        if self.before_generate:
            self.before_generate()
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_service(tmp_path, responses):
    store = SQLiteSessionStore(tmp_path / "runs.db")
    runtime = AgentRuntime(
        FakeModel(responses), ToolRegistry(), session_store=store
    )
    graph = TaskGraph(tmp_path / "tasks")
    return TaskService(graph, runtime), store


def test_task_ids_are_stable_uuids_and_claim_uses_cas(tmp_path):
    graph = TaskGraph(tmp_path)
    task = graph.create("only once")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda owner: graph.claim(task.id, owner), ["a", "b"]))

    assert task.id.startswith("task_")
    assert len(task.id) == len("task_") + 32
    assert sum(result.startswith("Claimed ") for result in results) == 1
    assert graph.load(task.id).version == 2


def test_dependencies_must_complete_before_task_is_claimed(tmp_path):
    graph = TaskGraph(tmp_path)
    dependency = graph.create("dependency")
    task = graph.create("dependent", blocked_by=[dependency.id])

    assert not graph.can_start(task.id)
    assert graph.load(task.id).status == "pending"
    graph.claim(dependency.id, "worker")
    graph.complete(dependency.id)

    assert graph.can_start(task.id)
    assert graph.claim(task.id, "worker").startswith("Claimed ")


def test_task_execution_creates_run_and_records_trigger_metadata(tmp_path):
    service, store = make_service(
        tmp_path, [ModelResponse([TextBlock("done")])]
    )
    task = service.graph.create("scheduled work", "use the normal runtime")
    service.runtime.model.before_generate = lambda: (
        service.graph.load(task.id).run_id is not None
        or pytest.fail("Run must be bound before model execution")
    )

    result = service.execute(task.id, "trigger-1")

    persisted = service.graph.load(task.id)
    assert result == "done"
    assert persisted.status == "completed"
    assert persisted.run_id is not None
    assert persisted.trigger_id == "trigger-1"
    assert store.get_run(persisted.run_id).status is RunStatus.COMPLETED
    with sqlite3.connect(store.path) as db:
        metadata = db.execute(
            "SELECT metadata_json FROM inbound_messages WHERE run_id=?",
            (persisted.run_id,),
        ).fetchone()[0]
    assert json.loads(metadata) == {
        "task_id": task.id,
        "trigger_id": "trigger-1",
    }


def test_interrupted_task_resumes_the_same_run(tmp_path):
    service, store = make_service(tmp_path, [RuntimeError("temporary")])
    task = service.graph.create("recover me")

    with pytest.raises(RuntimeError, match="temporary"):
        service.execute(task.id, "trigger-recovery")
    interrupted = service.graph.load(task.id)
    original_run_id = interrupted.run_id
    assert interrupted.status == "interrupted"
    assert store.get_run(original_run_id).status is RunStatus.INTERRUPTED

    recovered_runtime = AgentRuntime(
        FakeModel([ModelResponse([TextBlock("recovered")])]),
        ToolRegistry(),
        session_store=SQLiteSessionStore(store.path),
    )
    recovered = TaskService(
        TaskGraph(tmp_path / "tasks"), recovered_runtime
    ).resume(task.id)

    assert recovered == "recovered"
    persisted = service.graph.load(task.id)
    assert persisted.status == "completed"
    assert persisted.run_id == original_run_id


def test_task_cancel_propagates_to_bound_run(tmp_path):
    service, store = make_service(tmp_path, [])
    task = service.graph.create("cancel me")
    service.graph.claim(task.id, service.owner)
    run = store.begin_inbound(
        platform="task", conversation_id=task.id, sender_id=service.owner,
        message_id="trigger-cancel",
    ).run
    service.graph.bind_run(task.id, run.id, "trigger-cancel")

    assert service.cancel(task.id)
    assert service.graph.load(task.id).status == "cancelled"
    assert store.get_run(run.id).status is RunStatus.CANCELLED
