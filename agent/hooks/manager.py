from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

def log_hook(block):
    """PreToolUse: log every tool call."""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK------------] {block.name}({args_preview})\033[0m")
    return None

class HookManager:
    def __init__(self):
        self._hooks: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self.register("PreToolUse", log_hook)




    def register(self, event: str, callback: Callable[..., Any]):
        self._hooks[event].append(callback)

    def trigger(self, event: str, *args):
        for callback in self._hooks[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None
