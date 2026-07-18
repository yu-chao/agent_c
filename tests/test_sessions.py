import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent_runtime.core.run_state import RunLeaseLost
from agent_runtime.sessions import RunStatus, SQLiteSessionStore


def begin(store, message_id="message-1"):
    return store.begin_inbound(
        platform="wecom",
        conversation_id="chat-1",
        sender_id="user-1",
        message_id=message_id,
        metadata={"request_id": "request-1"},
    )


def test_begin_inbound_atomically_creates_session_and_run(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    started = begin(store)

    assert started.is_new
    assert started.run.status is RunStatus.RUNNING
    assert started.run.session_id.startswith("session_")


def test_start_inbound_atomically_stores_user_message_and_initial_checkpoint(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")

    started = store.start_inbound(
        platform="wecom",
        conversation_id="chat-1",
        sender_id="user-1",
        message_id="message-1",
        user_content="hello",
        initial_checkpoint={"action": "model", "messages": []},
        recent_message_limit=10,
    )

    messages = store.recent_messages(started.run.session_id, 10)
    checkpoint = store.latest_checkpoint(started.run.id)
    assert [(item.role, item.content) for item in messages] == [("user", "hello")]
    assert checkpoint.phase == "inbound_started"
    assert checkpoint.state == {
        "action": "model",
        "schema_version": 1,
        "messages": [{"type": "dict", "value": {
            "role": "user", "content": "hello"
        }}],
    }


def test_session_identity_cannot_collide_when_values_contain_separator(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    first = store.begin_inbound(
        platform="a:b", conversation_id="c", sender_id="u",
        message_id="m1",
    ).run
    second = store.begin_inbound(
        platform="a", conversation_id="b:c", sender_id="u",
        message_id="m2",
    ).run

    assert first.session_id != second.session_id


def test_start_inbound_initial_checkpoint_contains_recent_session_history(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    first = store.start_inbound(
        platform="wecom", conversation_id="chat-1", sender_id="user-1",
        message_id="message-1", user_content="first",
        initial_checkpoint={"action": "model", "messages": []},
        recent_message_limit=2,
    )
    store.complete_run(first.run.id, "answer")
    second = store.start_inbound(
        platform="wecom", conversation_id="chat-1", sender_id="user-1",
        message_id="message-2", user_content="second",
        initial_checkpoint={"action": "model", "messages": []},
        recent_message_limit=2,
    )

    assert store.latest_checkpoint(second.run.id).state["messages"] == [
        {"type": "dict", "value": {
            "role": "assistant", "content": "answer"
        }},
        {"type": "dict", "value": {
            "role": "user", "content": "second"
        }},
    ]


def test_duplicate_inbound_reuses_run_and_completed_response(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    first = begin(store)
    store.complete_run(first.run.id, "cached answer")

    duplicate = begin(store)

    assert not duplicate.is_new
    assert duplicate.run.id == first.run.id
    assert duplicate.cached_response == "cached answer"


def test_recent_messages_keeps_chronological_sliding_window(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    started = begin(store)
    for index in range(5):
        store.append_message(
            started.run.session_id,
            started.run.id,
            "user" if index % 2 == 0 else "assistant",
            f"message-{index}",
        )

    messages = store.recent_messages(started.run.session_id, limit=3)

    assert [(item.role, item.content) for item in messages] == [
        ("user", "message-2"),
        ("assistant", "message-3"),
        ("user", "message-4"),
    ]


def test_checkpoint_sequence_and_startup_recovery(tmp_path):
    path = tmp_path / "sessions.db"
    store = SQLiteSessionStore(path)
    started = begin(store)
    first = store.save_checkpoint(started.run.id, "before_model", {"turn": 0})
    second = store.save_checkpoint(started.run.id, "after_model", {"turn": 1})

    restarted = SQLiteSessionStore(path, owner_id=store.owner_id)
    interrupted = restarted.interrupt_incomplete_runs()

    assert (first.sequence, second.sequence) == (1, 2)
    assert restarted.latest_checkpoint(started.run.id).state == {"turn": 1}
    assert [item.id for item in interrupted] == [started.run.id]
    assert restarted.get_run(started.run.id).status is RunStatus.INTERRUPTED


def test_running_tool_is_uncertain_after_crash_and_is_not_reclaimed(tmp_path):
    path = tmp_path / "sessions.db"
    store = SQLiteSessionStore(path)
    run = begin(store).run

    first_claim = store.claim_tool(run.id, "call-1", "create_order", {"sku": "A"})
    restarted = SQLiteSessionStore(path)
    second_claim = restarted.claim_tool(
        run.id, "call-1", "create_order", {"sku": "A"}
    )

    assert first_claim.should_execute
    assert not second_claim.should_execute
    assert second_claim.is_uncertain


def test_completed_tool_result_is_reused_without_execution(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    run = begin(store).run
    store.claim_tool(run.id, "call-1", "lookup", {"id": 1})
    store.complete_tool(run.id, "call-1", "result")

    claim = store.claim_tool(run.id, "call-1", "lookup", {"id": 1})

    assert not claim.should_execute
    assert claim.output == "result"
    assert not claim.is_uncertain


def test_claim_run_is_compare_and_swap(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    run = begin(store).run
    assert store.transition_run(run.id, RunStatus.INTERRUPTED)

    assert store.claim_run(run.id, {RunStatus.INTERRUPTED})
    assert not store.claim_run(run.id, {RunStatus.INTERRUPTED})
    assert store.get_run(run.id).status is RunStatus.RUNNING


def test_transition_run_rejects_illegal_source_status(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    run = begin(store).run
    store.complete_run(run.id, "done")

    assert not store.transition_run(run.id, RunStatus.RUNNING)
    assert store.get_run(run.id).status is RunStatus.COMPLETED


def test_complete_run_only_writes_response_when_running_cas_succeeds(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    run = begin(store).run
    assert store.transition_run(run.id, RunStatus.INTERRUPTED)
    assert not store.complete_run(run.id, "must not persist")
    assert store.cached_response(run.id) is None
    assert store.recent_messages(run.session_id, 10) == []


def test_complete_run_is_idempotent_only_for_same_completed_response(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    run = begin(store).run
    assert store.complete_run(run.id, "done")
    assert store.complete_run(run.id, "done")
    assert not store.complete_run(run.id, "different")
    assert store.cached_response(run.id) == "done"
    assert [item.content for item in store.recent_messages(run.session_id, 10)] == ["done"]


def test_new_store_does_not_interrupt_another_owner_with_valid_lease(tmp_path):
    path = tmp_path / "sessions.db"
    first = SQLiteSessionStore(path, lease_seconds=60)
    run = begin(first).run
    assert SQLiteSessionStore(path, lease_seconds=60).interrupt_incomplete_runs() == []
    assert first.get_run(run.id).status is RunStatus.RUNNING


def test_new_store_interrupts_expired_running_lease(tmp_path):
    path = tmp_path / "sessions.db"
    first = SQLiteSessionStore(path, lease_seconds=60)
    run = begin(first).run
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE runs SET lease_expires_at=? WHERE id=?", (expired, run.id))
    recovered = SQLiteSessionStore(path).interrupt_incomplete_runs()
    assert [item.id for item in recovered] == [run.id]
    assert first.get_run(run.id).status is RunStatus.INTERRUPTED


def test_owner_can_renew_its_run_lease(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db", lease_seconds=60)
    run = begin(store).run
    assert store.renew_run(run.id)
    assert not SQLiteSessionStore(store.path).renew_run(run.id)


def test_stale_execution_token_cannot_write_or_complete_run(tmp_path):
    path = tmp_path / "sessions.db"
    first = SQLiteSessionStore(path)
    run = begin(first).run
    assert first.transition_run(
        run.id, RunStatus.INTERRUPTED, execution_token=run.execution_token
    )
    second = SQLiteSessionStore(path)
    assert second.claim_run(run.id, {RunStatus.INTERRUPTED})

    with pytest.raises(RunLeaseLost):
        first.save_checkpoint(
            run.id, "stale", {"schema_version": 1},
            execution_token=run.execution_token,
        )
    assert not first.complete_run(
        run.id, "stale", execution_token=run.execution_token
    )


def test_tool_execution_identity_conflict_is_rejected(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    run = begin(store).run
    assert store.claim_tool(run.id, "call-1", "lookup", {"id": 1}).should_execute
    with pytest.raises(ValueError, match="conflicts"):
        store.claim_tool(run.id, "call-1", "other", {"id": 1})
    with pytest.raises(ValueError, match="conflicts"):
        store.claim_tool(run.id, "call-1", "lookup", {"id": 2})


def test_tool_execution_arguments_are_compared_canonically(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.db")
    run = begin(store).run
    store.claim_tool(run.id, "call-1", "lookup", {"a": 1, "b": 2})
    store.complete_tool(run.id, "call-1", "done")

    claim = store.claim_tool(run.id, "call-1", "lookup", {"b": 2, "a": 1})

    assert claim.output == "done"
