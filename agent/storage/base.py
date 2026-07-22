from __future__ import annotations

from typing import Protocol


class Storage(Protocol):
    def read_text(self, path: str) -> str:
        ...

    def write_text(self, path: str, content: str) -> None:
        ...

    def append_text(self, path: str, content: str) -> None:
        ...

    def list_files(self, pattern: str) -> list[str]:
        ...

    def delete(self, path: str) -> None:
        ...
