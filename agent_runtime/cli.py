from __future__ import annotations

import argparse
from pathlib import Path

from agent_runtime.core import AgentRuntime
from agent_runtime.hooks import HookManager
from agent_runtime.models import create_model_provider
from agent_runtime.security import PermissionPolicy
from agent_runtime.storage import FileStore
from agent_runtime.tools import ToolRegistry, ToolSpec


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Run the generic agent runtime.")
    parser.add_argument("--provider", choices=["anthropic", "openai"], default="anthropic")
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)

    config = {"model": {"provider": args.provider, "name": args.model}}
    model = create_model_provider(config)
    registry = create_default_registry(Path.cwd())
    runtime = AgentRuntime(
        model=model,
        tools=registry,
        hooks=HookManager(),
        permission_policy=PermissionPolicy(Path.cwd()),
        system_prompt="You are a coding agent. Use tools when useful.",
    )

    print("agent-runtime: enter a question, empty line to quit")
    while True:
        try:
            query = input("agent >> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not query.strip():
            break
        print(runtime.run_turn(query))


def create_default_registry(workdir: Path) -> ToolRegistry:
    store = FileStore(workdir)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "read_file",
            "Read a file in the workspace.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        lambda path: store.read_text(path),
    )
    registry.register(
        ToolSpec(
            "write_file",
            "Write a file in the workspace.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        lambda path, content: store.write_text(path, content) or f"Wrote {path}",
    )
    registry.register(
        ToolSpec(
            "glob",
            "List files matching a workspace glob.",
            {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
        ),
        lambda pattern: "\n".join(store.list_files(pattern)) or "(no matches)",
    )
    return registry
