from __future__ import annotations

from typing import Any


class ModelError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class RetryableModelError(ModelError):
    """A transient failure that may succeed when attempted again."""


class PermanentModelError(ModelError):
    """A request failure that must not be retried automatically."""


def classify_model_error(error: Exception) -> ModelError:
    if isinstance(error, ModelError):
        return error
    status_code = _status_code(error)
    retry_after = _retry_after_seconds(error)
    message = str(error) or error.__class__.__name__
    if status_code in {408, 409, 425, 429} or (
        status_code is not None and status_code >= 500
    ):
        return RetryableModelError(
            message,
            status_code=status_code,
            retry_after_seconds=retry_after,
        )
    if status_code is not None:
        return PermanentModelError(message, status_code=status_code)
    error_name = error.__class__.__name__.lower()
    if isinstance(error, (TimeoutError, ConnectionError)) or any(
        marker in error_name for marker in ('timeout', 'connection')
    ):
        return RetryableModelError(message)
    return PermanentModelError(message)


def _status_code(error: Exception) -> int | None:
    direct = getattr(error, 'status_code', None)
    response = getattr(error, 'response', None)
    value = direct if direct is not None else getattr(response, 'status_code', None)
    return value if isinstance(value, int) else None


def _retry_after_seconds(error: Exception) -> float | None:
    response = getattr(error, 'response', None)
    headers: Any = getattr(response, 'headers', None)
    if headers is None:
        headers = getattr(error, 'headers', None)
    if not headers:
        return None
    value = headers.get('retry-after') or headers.get('Retry-After')
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, delay)
