from __future__ import annotations

from pathlib import Path

from agent.storage import FileStore
from agent.tools.registry import ToolRegistry, ToolSpec


def build_default_tool_registry(workdir: str | Path | None = None) -> ToolRegistry:
    """构建绑定到工作目录的内置工具注册表。"""
    store = FileStore(workdir or Path.cwd())
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="read_file",
            description="读取工作目录中的 UTF-8 文本文件。",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于工作目录的文件路径。",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        lambda path: _read_file(store, path),
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description="将 UTF-8 文本写入工作目录中的文件。",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于工作目录的文件路径。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的完整文件内容。",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        lambda path, content: _write_file(store, path, content),
    )
    return registry


def _read_file(store: FileStore, path: str) -> str:
    if store.is_directory(path):
        pattern = str(Path(path) / "*")
        entries = store.list_files(pattern)
        listing = "\n".join(entries) if entries else "(empty directory)"
        return f"Directory listing for {path}:\n{listing}"
    return store.read_text(path)


def _write_file(store: FileStore, path: str, content: str) -> str:
    store.write_text(path, content)
    return f"Wrote file: {path}"
