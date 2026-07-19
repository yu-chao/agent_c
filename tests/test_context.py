from agent_runtime.context import (
    ApproximateTokenCounter,
    ContextManager,
    ContextWindow,
)
from agent_runtime.contracts import ToolCall, ToolResult
from agent_runtime.sessions import SQLiteSessionStore


def test_context_window_keeps_current_user_and_latest_tool_result():
    counter = ApproximateTokenCounter()
    window = ContextWindow(
        counter=counter,
        max_input_tokens=90,
        tool_result_max_tokens=12,
    )
    messages = [
        {'role': 'user', 'content': 'old-' + 'x' * 160},
        {'role': 'assistant', 'content': 'old answer'},
        {'role': 'user', 'content': 'current question'},
        ToolCall('call-1', 'lookup', {'query': 'current'}),
        ToolResult('call-1', 'result-' + 'y' * 200),
    ]

    fitted = window.fit(messages, system='system prompt', tools=[])

    assert {'role': 'user', 'content': 'current question'} in fitted
    result = next(item for item in fitted if isinstance(item, ToolResult))
    assert result.tool_call_id == 'call-1'
    assert 'truncated' in result.content
    assert counter.count_request('system prompt', [], fitted) <= 90


def test_context_window_preserves_summary_when_it_fits():
    window = ContextWindow(max_input_tokens=100)
    summary = {
        'role': 'assistant',
        'content': '[Conversation summary]\nuser: earlier fact',
        'summary_version': 2,
    }

    fitted = window.fit(
        [summary, {'role': 'user', 'content': 'follow up'}],
        system='system',
        tools=[],
    )

    assert fitted[0] == summary


def test_context_manager_compacts_old_messages_and_versions_summary(tmp_path):
    store = SQLiteSessionStore(tmp_path / 'sessions.db')
    started = store.begin_inbound(
        platform='test', conversation_id='chat', sender_id='user',
        message_id='message-1', user_content='first fact',
        initial_checkpoint={'action': 'model', 'messages': []},
    )
    store.complete_run(started.run.id, 'first answer')
    second = store.begin_inbound(
        platform='test', conversation_id='chat', sender_id='user',
        message_id='message-2', user_content='current question',
        initial_checkpoint={'action': 'model', 'messages': []},
    )
    manager = ContextManager(
        store,
        max_input_tokens=200,
        summary_trigger_tokens=1,
        recent_message_limit=1,
    )

    built = manager.build(second.run.session_id, second.run.id)

    summary = store.latest_summary(second.run.session_id)
    assert summary is not None
    assert summary.version == 1
    assert summary.through_message_id > 0
    assert built.summary_version == 1
    assert built.messages[0]['summary_version'] == 1
    assert built.messages[-1] == {
        'role': 'user', 'content': 'current question'
    }


def test_context_manager_summary_versions_are_monotonic(tmp_path):
    store = SQLiteSessionStore(tmp_path / 'sessions.db')
    first = store.begin_inbound(
        platform='test', conversation_id='chat', sender_id='user',
        message_id='message-1', user_content='one',
        initial_checkpoint={'action': 'model', 'messages': []},
    )
    store.complete_run(first.run.id, 'answer one')
    manager = ContextManager(store, recent_message_limit=0)

    assert manager.compact(first.run.session_id) is not None
    store.append_message(first.run.session_id, first.run.id, 'user', 'two')
    assert manager.compact(first.run.session_id) is not None

    assert store.latest_summary(first.run.session_id).version == 2


def test_context_manager_never_summarizes_current_run_when_recent_limit_is_zero(
    tmp_path,
):
    store = SQLiteSessionStore(tmp_path / 'sessions.db')
    first = store.begin_inbound(
        platform='test', conversation_id='chat', sender_id='user',
        message_id='message-1', user_content='old fact',
        initial_checkpoint={'action': 'model', 'messages': []},
    )
    store.complete_run(first.run.id, 'old answer')
    current = store.begin_inbound(
        platform='test', conversation_id='chat', sender_id='user',
        message_id='message-2', user_content='must remain verbatim',
        initial_checkpoint={'action': 'model', 'messages': []},
    )
    manager = ContextManager(
        store, summary_trigger_tokens=1, recent_message_limit=0
    )

    built = manager.build(current.run.session_id, current.run.id)

    assert built.messages[-1] == {
        'role': 'user', 'content': 'must remain verbatim'
    }
    summary = store.latest_summary(current.run.session_id)
    current_message = store.messages_after(current.run.session_id)[-1]
    assert summary.through_message_id < current_message.id
