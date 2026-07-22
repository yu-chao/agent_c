from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator


CORRELATION_FIELDS = (
    "trace_id",
    "session_id",
    "run_id",
    "message_id",
    "model_request_id",
    "tool_execution_id",
    "approval_id",
)

_correlation: ContextVar[dict[str, Any]] = ContextVar(
    "agent_runtime_observation_context", default={}
)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def trace_id_for_message(platform: str, message_id: str) -> str:
    value = f"{platform}:{message_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:32]


def current_context() -> dict[str, Any]:
    return dict(_correlation.get())


@contextmanager
def bind_context(**fields: Any) -> Iterator[dict[str, Any]]:
    merged = current_context()
    merged.update(
        {key: value for key, value in fields.items() if value is not None}
    )
    token = _correlation.set(merged)
    try:
        yield merged
    finally:
        _correlation.reset(token)


@contextmanager
def ensure_trace(**fields: Any) -> Iterator[dict[str, Any]]:
    values = current_context()
    if not values.get("trace_id"):
        fields = {"trace_id": new_trace_id(), **fields}
    with bind_context(**fields) as bound:
        yield bound
