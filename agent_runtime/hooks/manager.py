from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable


class HookManager:
    def __init__(self):
        self._hooks: dict[str, list[Callable[..., Any]]] = defaultdict(list)

    def register(self, event: str, callback: Callable[..., Any]):
        self._hooks[event].append(callback)

    def trigger(self, event: str, *args):
        for callback in self._hooks[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None
