from dataclasses import replace
from time import sleep

import pytest

from agent.approval import RuntimeIdentity
from agent.contracts import (
    ModelRequest,
    ModelResponse,
    TextBlock,
    ToolCall,
)
from agent.core import AgentRuntime
from agent.models.errors import (
    PermanentModelError,
    RetryableModelError,
)
from agent.models.resilient import (
    CircuitBreaker,
    ResilientModelProvider,
    RetryPolicy,
)
from agent.sessions import SQLiteSessionStore
from agent.tools import ToolRegistry, ToolSpec


class FakeProvider:
    def __init__(self, provider, model, outcomes):
        self.provider = provider
        self.model = model
        self.outcomes = list(outcomes)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def request(previous_response_id=None):
    return ModelRequest(
        messages=[{'role': 'user', 'content': 'hello'}],
        system='system',
        previous_response_id=previous_response_id,
    )


def test_retryable_failure_uses_exponential_backoff_then_succeeds():
    primary = FakeProvider(
        'openai', 'primary',
        [RetryableModelError('busy'), RetryableModelError('busy'),
         ModelResponse([TextBlock('ok')])],
    )
    delays = []
    provider = ResilientModelProvider(
        primary,
        retry_policy=RetryPolicy(
            max_attempts=3, base_delay_seconds=0.5,
            jitter_ratio=0,
        ),
        sleep=delays.append,
    )

    response = provider.generate(request())

    assert response.text == 'ok'
    assert response.provider == 'openai'
    assert response.model == 'primary'
    assert response.attempts == 3
    assert delays == [0.5, 1.0]


def test_rate_limit_retry_after_takes_precedence_over_backoff():
    primary = FakeProvider(
        'openai', 'primary',
        [RetryableModelError('limited', retry_after_seconds=4),
         ModelResponse([TextBlock('ok')])],
    )
    delays = []
    provider = ResilientModelProvider(
        primary,
        retry_policy=RetryPolicy(max_attempts=2, jitter_ratio=0),
        sleep=delays.append,
    )

    provider.generate(request())

    assert delays == [4]


def test_permanent_failure_is_not_retried_or_fallbacked():
    primary = FakeProvider(
        'openai', 'primary', [PermanentModelError('invalid request')]
    )
    fallback = FakeProvider(
        'anthropic', 'fallback', [ModelResponse([TextBlock('unused')])]
    )
    provider = ResilientModelProvider(primary, fallback=fallback)

    with pytest.raises(PermanentModelError, match='invalid request'):
        provider.generate(request())

    assert len(primary.requests) == 1
    assert fallback.requests == []


def test_timeout_is_retryable_and_can_fallback():
    class SlowProvider(FakeProvider):
        def generate(self, request):
            self.requests.append(request)
            sleep(0.05)
            return ModelResponse([TextBlock('late')])

    primary = SlowProvider('openai', 'primary', [])
    fallback = FakeProvider(
        'anthropic', 'fallback', [ModelResponse([TextBlock('fallback')])]
    )
    provider = ResilientModelProvider(
        primary,
        fallback=fallback,
        retry_policy=RetryPolicy(
            request_timeout_seconds=0.01,
            max_attempts=1,
        ),
    )

    response = provider.generate(request())

    assert response.text == 'fallback'
    assert response.provider == 'anthropic'
    assert response.model == 'fallback'
    assert response.attempts == 2


def test_fallback_rebuilds_request_without_previous_response_id():
    primary = FakeProvider(
        'openai', 'primary', [RetryableModelError('unavailable')]
    )
    fallback = FakeProvider(
        'anthropic', 'fallback', [ModelResponse([TextBlock('ok')])]
    )
    provider = ResilientModelProvider(
        primary, fallback=fallback,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    original = request(previous_response_id='provider-specific-id')

    response = provider.generate(original)

    assert response.text == 'ok'
    assert fallback.requests[0] == replace(
        original, previous_response_id=None
    )


def test_fallback_callback_runs_before_fallback_request():
    events = []

    class OrderedProvider(FakeProvider):
        def generate(self, request):
            events.append('fallback-request')
            return super().generate(request)

    primary = FakeProvider(
        'openai', 'primary', [RetryableModelError('unavailable')]
    )
    fallback = OrderedProvider(
        'anthropic', 'fallback', [ModelResponse([TextBlock('ok')])]
    )
    provider = ResilientModelProvider(
        primary, fallback=fallback,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    model_request = request()
    model_request.on_fallback = lambda provider, model: events.append(
        f'checkpoint:{provider}:{model}'
    )

    provider.generate(model_request)

    assert events == [
        'checkpoint:anthropic:fallback',
        'fallback-request',
    ]


def test_open_circuit_skips_primary_and_uses_fallback():
    primary = FakeProvider(
        'openai', 'primary', [RetryableModelError('unavailable')]
    )
    fallback = FakeProvider(
        'anthropic', 'fallback',
        [
            ModelResponse([TextBlock('first')]),
            ModelResponse([TextBlock('second')]),
        ],
    )
    provider = ResilientModelProvider(
        primary,
        fallback=fallback,
        retry_policy=RetryPolicy(max_attempts=1),
        circuit_breaker=CircuitBreaker(
            failure_threshold=1, recovery_timeout_seconds=60
        ),
    )

    assert provider.generate(request()).text == 'first'
    assert provider.generate(request()).text == 'second'
    assert len(primary.requests) == 1
    assert len(fallback.requests) == 2


def test_runtime_checkpoints_fallback_and_actual_model(tmp_path):
    primary = FakeProvider(
        'openai', 'primary', [RetryableModelError('unavailable')]
    )
    fallback = FakeProvider(
        'anthropic', 'fallback', [ModelResponse([TextBlock('ok')])]
    )
    model = ResilientModelProvider(
        primary,
        fallback=fallback,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    store = SQLiteSessionStore(tmp_path / 'sessions.db')
    runtime = AgentRuntime(model, ToolRegistry(), session_store=store)

    result = runtime.run_turn(
        'hello', RuntimeIdentity('test', 'chat', 'user', 'message-1')
    )

    run = store.get_run(result.run_id) if hasattr(result, 'run_id') else None
    if run is None:
        with store._connect() as database:
            run_id = database.execute(
                'SELECT id FROM runs ORDER BY created_at DESC LIMIT 1'
            ).fetchone()['id']
    else:
        run_id = run.id
    with store._connect() as database:
        fallback_checkpoint = database.execute(
            "SELECT state_json FROM checkpoints "
            "WHERE run_id=? AND phase='before_model_fallback'",
            (run_id,),
        ).fetchone()
    assert fallback_checkpoint is not None
    assert '"model_provider":"anthropic"' in fallback_checkpoint['state_json']
    latest = store.latest_checkpoint(run_id)
    assert latest.state['model_provider'] == 'anthropic'
    assert latest.state['model_name'] == 'fallback'


def test_runtime_drops_previous_response_id_after_fallback_tool_call():
    primary = FakeProvider(
        'openai', 'primary',
        [
            RetryableModelError('unavailable'),
            ModelResponse([TextBlock('done')], response_id='primary-new'),
        ],
    )
    fallback = FakeProvider(
        'anthropic', 'fallback',
        [ModelResponse([ToolCall('call-1', 'echo', {'text': 'hi'})])],
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec('echo', 'Echo text', {'type': 'object'}),
        lambda text: text,
    )
    runtime = AgentRuntime(
        ResilientModelProvider(
            primary,
            fallback=fallback,
            retry_policy=RetryPolicy(max_attempts=1),
        ),
        registry,
    )

    result = runtime._run(
        [{'role': 'user', 'content': 'hello'}],
        'primary-old',
        None,
        0,
    )

    assert result == 'done'
    assert fallback.requests[0].previous_response_id is None
    assert primary.requests[1].previous_response_id is None
