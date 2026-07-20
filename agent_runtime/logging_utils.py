from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from agent_runtime.observability import CORRELATION_FIELDS, current_context


SENSITIVE_KEYS = {"secret", "api_key", "authorization", "token", "password"}
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message", "asctime"
}


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in current_context().items():
            if key in CORRELATION_FIELDS and not hasattr(record, key):
                setattr(record, key, value)
        return True


class StructuredJsonFormatter(logging.Formatter):
    """Format each record as one sanitized JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in CORRELATION_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS
            and key not in CORRELATION_FIELDS
            and not key.startswith("_")
        }
        if extras:
            payload["fields"] = extras
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(_sanitize(payload), ensure_ascii=False, default=str)


def parse_log_level(level: str | None) -> int:
    normalized = str(level or "INFO").strip().upper()
    return getattr(logging, normalized, logging.INFO)


def setup_logging(
    level: str = "INFO",
    log_dir: str | None = None,
    retention_days: int = 30,
) -> None:
    log_level = parse_log_level(level)
    formatter = StructuredJsonFormatter()
    correlation_filter = CorrelationFilter()

    root = logging.getLogger()
    root.setLevel(log_level)
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(formatter)
    console.addFilter(correlation_filter)
    root.addHandler(console)

    try:
        directory = Path(log_dir or "logs")
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = _build_rotating_file_handler(directory, retention_days)
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(correlation_filter)
        root.addHandler(file_handler)
    except Exception:
        # File logging is optional; console logging remains available.
        logging.getLogger(__name__).exception("file_logging_unavailable")

    logging.getLogger("agent_x").setLevel(log_level)


def _build_rotating_file_handler(directory: Path, retention_days: int) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        directory / "app.log",
        when="midnight",
        interval=1,
        backupCount=max(retention_days, 1),
        encoding="utf-8",
    )
    handler.suffix = "%Y-%m-%d"
    handler.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    handler.namer = _rotated_log_name
    return handler


def _rotated_log_name(default_name: str) -> str:
    path = Path(default_name)
    date_part = path.name.removeprefix("app.log.")
    return str(path.parent / f"app-{date_part}.log")


def safe_preview(value: Any, limit: int = 800) -> str:
    sanitized = _sanitize(value)
    try:
        text = json.dumps(sanitized, ensure_ascii=False, default=str)
    except TypeError:
        text = str(sanitized)
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower()
            if any(part in normalized for part in SENSITIVE_KEYS):
                result[key] = "***"
            else:
                result[key] = _sanitize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value
