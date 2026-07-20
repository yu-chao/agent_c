from __future__ import annotations

from datetime import datetime
import hashlib


def validate_cron(cron_expr: str) -> str | None:
    parts = cron_expr.split()
    if len(parts) != 5:
        return "cron must have 5 fields"
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    for index, (field, bounds) in enumerate(zip(parts, ranges), start=1):
        error = _validate_field(field, *bounds)
        if error:
            return f"cron field {index} {error}"
    return None


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    if validate_cron(cron_expr):
        return False
    minute, hour, day, month, weekday = cron_expr.split()
    values = [dt.minute, dt.hour, dt.day, dt.month, dt.weekday()]
    return all(_field_matches(field, value) for field, value in zip([minute, hour, day, month, weekday], values))


def cron_slot(dt: datetime) -> str:
    """Return the canonical minute used by the durable trigger key."""
    return dt.replace(second=0, microsecond=0).isoformat()


def cron_trigger_id(schedule_id: str, dt: datetime) -> str:
    value = f"{schedule_id}:{cron_slot(dt)}".encode("utf-8")
    return "trigger_" + hashlib.sha256(value).hexdigest()[:32]


def _validate_field(field: str, lo: int, hi: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        return _validate_int(field[2:], lo, hi, step=True)
    for part in field.split(","):
        error = _validate_int(part, lo, hi)
        if error:
            return error
    return None


def _validate_int(raw: str, lo: int, hi: int, step: bool = False) -> str | None:
    try:
        value = int(raw)
    except ValueError:
        return f"value {raw} is not an integer"
    if step and value <= 0:
        return f"value {value} must be positive"
    if not step and not lo <= value <= hi:
        return f"value {value} outside {lo}-{hi}"
    return None


def _field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        return value % int(field[2:]) == 0
    return value in {int(part) for part in field.split(",")}
