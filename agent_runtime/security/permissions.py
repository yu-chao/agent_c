from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from enum import StrEnum

from agent_runtime.contracts import ToolCall


class PermissionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PermissionDecision:
    action: PermissionAction
    reason: str = ""

    def __str__(self) -> str:
        return self.reason

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.reason == other
        if isinstance(other, PermissionDecision):
            return (self.action, self.reason) == (other.action, other.reason)
        return NotImplemented


class PermissionPolicy:
    def __init__(self, workdir: str | Path | None = None, approval_tools=()):
        self.workdir = Path(workdir or Path.cwd()).resolve()
        self.approval_tools = frozenset(approval_tools)
        self.deny_list = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
        self.destructive_tokens = ["rm ", "> /etc/", "chmod 777"]

    def check(self, call: ToolCall) -> PermissionDecision:
        if call.name == "bash":
            command = call.input.get("command", "")
            for pattern in self.deny_list:
                if pattern in command:
                    return PermissionDecision(
                        PermissionAction.DENY,
                        f"Permission denied: '{pattern}' is on the deny list",
                    )
            if any(token in command for token in self.destructive_tokens):
                return PermissionDecision(
                    PermissionAction.REQUIRE_APPROVAL,
                    "Permission denied: destructive command requires approval",
                )
        if call.name in ("write_file", "edit_file"):
            path = call.input.get("path", "")
            if not self._inside_workdir(path):
                return PermissionDecision(
                    PermissionAction.DENY,
                    f"Permission denied: path escapes workspace: {path}",
                )
        if call.name.startswith("mcp__") and "deploy" in call.name:
            return PermissionDecision(
                PermissionAction.DENY,
                "Permission denied: destructive MCP tool is blocked",
            )
        if call.name in self.approval_tools:
            return PermissionDecision(
                PermissionAction.REQUIRE_APPROVAL,
                "Tool call requires user approval",
            )
        return PermissionDecision(PermissionAction.ALLOW)

    def _inside_workdir(self, path: str) -> bool:
        try:
            resolved = (self.workdir / path).resolve()
            resolved.relative_to(self.workdir)
            return True
        except ValueError:
            return False
