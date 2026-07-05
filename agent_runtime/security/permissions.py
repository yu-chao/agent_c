from __future__ import annotations

from pathlib import Path

from agent_runtime.models import ToolCall


class PermissionPolicy:
    def __init__(self, workdir: str | Path | None = None):
        self.workdir = Path(workdir or Path.cwd()).resolve()
        self.deny_list = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
        self.destructive_tokens = ["rm ", "> /etc/", "chmod 777"]

    def check(self, call: ToolCall) -> str | None:
        if call.name == "bash":
            command = call.input.get("command", "")
            for pattern in self.deny_list:
                if pattern in command:
                    return f"Permission denied: '{pattern}' is on the deny list"
            if any(token in command for token in self.destructive_tokens):
                return "Permission denied: destructive command requires approval"
        if call.name in ("write_file", "edit_file"):
            path = call.input.get("path", "")
            if not self._inside_workdir(path):
                return f"Permission denied: path escapes workspace: {path}"
        if call.name.startswith("mcp__") and "deploy" in call.name:
            return "Permission denied: destructive MCP tool requires approval"
        return None

    def _inside_workdir(self, path: str) -> bool:
        try:
            resolved = (self.workdir / path).resolve()
            resolved.relative_to(self.workdir)
            return True
        except ValueError:
            return False
