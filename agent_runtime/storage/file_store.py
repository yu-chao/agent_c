from __future__ import annotations

from pathlib import Path


class FileStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def read_text(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def append_text(self, path: str, content: str) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(content)

    def list_files(self, pattern: str) -> list[str]:
        return sorted(str(path.relative_to(self.root)) for path in self.root.glob(pattern))

    def delete(self, path: str) -> None:
        self._resolve(path).unlink()

    def _resolve(self, path: str) -> Path:
        resolved = (self.root / path).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Path escapes storage root: {path}") from exc
        return resolved
