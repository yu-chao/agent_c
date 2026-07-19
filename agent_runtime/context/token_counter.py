from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from typing import Any


class ApproximateTokenCounter:
    """可替换的保守近似计数器，不依赖特定 provider SDK。"""

    def count_text(self, value: str) -> int:
        if not value:
            return 0
        return max(1, math.ceil(len(value.encode('utf-8')) / 4))

    def count(self, value: Any) -> int:
        rendered = json.dumps(
            _json_value(value), ensure_ascii=False, separators=(',', ':'),
            sort_keys=True,
        )
        return self.count_text(rendered)

    def count_request(
        self, system: str, tools: list[Any], messages: list[Any]
    ) -> int:
        return self.count_text(system) + self.count(tools) + self.count(messages)


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, '__dict__'):
        return _json_value(vars(value))
    return str(value)
