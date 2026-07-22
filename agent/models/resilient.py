from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from threading import Lock
from typing import Callable

from agent.contracts import ModelProvider, ModelRequest, ModelResponse
from agent.models.errors import RetryableModelError


@dataclass(frozen=True)
class RetryPolicy:
    request_timeout_seconds: float = 60.0
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.2


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = Lock()

    def allow_request(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if self.clock() - self._opened_at >= self.recovery_timeout_seconds:
                self._opened_at = None
                self._failures = 0
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = self.clock()


class ResilientModelProvider:
    def __init__(
        self,
        primary: ModelProvider,
        fallback: ModelProvider | None = None,
        retry_policy: RetryPolicy | None = None,
        *,
        circuit_breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.retry_policy = retry_policy or RetryPolicy()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.sleep = sleep
        self.random_value = random_value
        self.provider = getattr(primary, 'provider', primary.__class__.__name__)
        self.model = primary.model

    def generate(self, request: ModelRequest) -> ModelResponse:
        primary_error: RetryableModelError | None = None
        primary_attempts = 0
        if self.circuit_breaker.allow_request():
            try:
                response, primary_attempts = self._generate_with_retry(
                    self.primary, request
                )
            except RetryableModelError as error:
                primary_error = error
                primary_attempts = self.retry_policy.max_attempts
                self.circuit_breaker.record_failure()
            else:
                self.circuit_breaker.record_success()
                return self._annotate(
                    response, self.primary, primary_attempts,
                    used_fallback=False,
                )
        else:
            primary_error = RetryableModelError('model circuit is open')

        if self.fallback is None:
            raise primary_error
        fallback_request = replace(request, previous_response_id=None)
        if request.on_fallback is not None:
            request.on_fallback(
                getattr(self.fallback, 'provider', self.fallback.__class__.__name__),
                self.fallback.model,
            )
        response, fallback_attempts = self._generate_with_retry(
            self.fallback, fallback_request
        )
        return self._annotate(
            response,
            self.fallback,
            primary_attempts + fallback_attempts,
            used_fallback=True,
        )

    def _generate_with_retry(
        self, provider: ModelProvider, request: ModelRequest
    ) -> tuple[ModelResponse, int]:
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                return self._generate_with_timeout(provider, request), attempt
            except RetryableModelError as error:
                if attempt >= self.retry_policy.max_attempts:
                    raise
                self.sleep(self._retry_delay(error, attempt))
        raise AssertionError('retry loop exhausted unexpectedly')

    def _generate_with_timeout(
        self, provider: ModelProvider, request: ModelRequest
    ) -> ModelResponse:
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(provider.generate, request)
        timed_out = False
        try:
            return future.result(
                timeout=self.retry_policy.request_timeout_seconds
            )
        except FutureTimeout as error:
            timed_out = True
            future.cancel()
            raise RetryableModelError('model request timed out') from error
        finally:
            executor.shutdown(wait=not timed_out, cancel_futures=True)

    def _retry_delay(
        self, error: RetryableModelError, attempt: int
    ) -> float:
        if error.retry_after_seconds is not None:
            return error.retry_after_seconds
        base = min(
            self.retry_policy.max_delay_seconds,
            self.retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
        )
        jitter = base * self.retry_policy.jitter_ratio * self.random_value()
        return base + jitter

    @staticmethod
    def _annotate(
        response: ModelResponse,
        provider: ModelProvider,
        attempts: int,
        *,
        used_fallback: bool,
    ) -> ModelResponse:
        response.provider = getattr(
            provider, 'provider', provider.__class__.__name__
        )
        response.model = provider.model
        response.attempts = attempts
        response.used_fallback = used_fallback
        return response
