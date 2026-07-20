from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any, Iterator, Protocol

from .context import current_context
from .metrics import MetricsRegistry


@dataclass(frozen=True)
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    started_at: float
    ended_at: float
    status: str
    attributes: dict[str, Any]
    error: str | None = None

    @property
    def duration_seconds(self) -> float:
        return self.ended_at - self.started_at


class SpanExporter(Protocol):
    def export(self, span: SpanRecord) -> None: ...


class SpanHandle:
    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes = attributes

    def set_attribute(self, key: str, value: Any) -> None:
        if value is not None:
            self.attributes[key] = value


class InMemorySpanExporter:
    def __init__(self) -> None:
        self._lock = Lock()
        self._spans: list[SpanRecord] = []

    def export(self, span: SpanRecord) -> None:
        with self._lock:
            self._spans.append(span)

    def spans(self, *, run_id: str | None = None) -> list[SpanRecord]:
        with self._lock:
            values = list(self._spans)
        if run_id is None:
            return values
        return [span for span in values if span.attributes.get("run_id") == run_id]


_current_span: ContextVar[str | None] = ContextVar(
    "agent_runtime_current_span", default=None
)


class Tracer:
    def __init__(self, exporter: SpanExporter | None = None) -> None:
        self.exporter = exporter or InMemorySpanExporter()

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[SpanHandle]:
        started = time.time()
        initial = current_context()
        trace_id = str(initial.get("trace_id") or uuid.uuid4().hex)
        span_id = uuid.uuid4().hex[:16]
        parent_id = _current_span.get()
        token = _current_span.set(span_id)
        handle = SpanHandle(dict(attributes))
        status = "ok"
        error_text = None
        try:
            yield handle
        except BaseException as error:
            status = "error"
            error_text = f"{type(error).__name__}: {error}"
            raise
        finally:
            final = {**initial, **current_context(), **handle.attributes}
            final.pop("trace_id", None)
            record = SpanRecord(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_id,
                name=name,
                started_at=started,
                ended_at=time.time(),
                status=status,
                attributes=final,
                error=error_text,
            )
            _current_span.reset(token)
            try:
                self.exporter.export(record)
            except Exception:
                # Exporter 故障绝不能改变业务执行结果。
                pass


class Observability:
    """集中提供 fail-open 的指标和 Trace 操作。"""

    def __init__(
        self,
        metrics: MetricsRegistry | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.metrics = metrics or MetricsRegistry()
        self.tracer = tracer or Tracer()

    def increment(self, name: str, value: float = 1, **labels: object) -> None:
        try:
            self.metrics.increment(name, value, labels)
        except Exception:
            pass

    def observe(self, name: str, value: float, **labels: object) -> None:
        try:
            self.metrics.observe(name, value, labels)
        except Exception:
            pass

    @contextmanager
    def span(self, name: str, **attributes: Any):
        try:
            manager = self.tracer.span(name, **attributes)
            handle = manager.__enter__()
        except Exception:
            yield None
            return
        try:
            yield handle
        except BaseException as error:
            try:
                manager.__exit__(
                    type(error), error, error.__traceback__
                )
            except Exception:
                pass
            raise
        else:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass


DEFAULT_OBSERVABILITY = Observability()
