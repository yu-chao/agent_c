from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Mapping


Labels = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class HistogramValue:
    count: int
    total: float
    minimum: float
    maximum: float


@dataclass(frozen=True)
class MetricsSnapshot:
    counters: dict[tuple[str, Labels], float]
    histograms: dict[tuple[str, Labels], HistogramValue]


class MetricsRegistry:
    """进程内、线程安全的指标注册表，可由外部采集器定期读取。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[tuple[str, Labels], float] = {}
        self._histograms: dict[tuple[str, Labels], list[float]] = {}

    def increment(
        self,
        name: str,
        value: float = 1,
        labels: Mapping[str, object] | None = None,
    ) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + float(value)

    def observe(
        self,
        name: str,
        value: float,
        labels: Mapping[str, object] | None = None,
    ) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._histograms.setdefault(key, []).append(float(value))

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            histograms = {
                key: HistogramValue(
                    count=len(values),
                    total=sum(values),
                    minimum=min(values),
                    maximum=max(values),
                )
                for key, values in self._histograms.items()
                if values
            }
            return MetricsSnapshot(dict(self._counters), histograms)


def _labels(values: Mapping[str, object] | None) -> Labels:
    return tuple(
        sorted(
            (str(key), str(value))
            for key, value in (values or {}).items()
        )
    )
