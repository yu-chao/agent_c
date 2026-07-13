from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import List
from dotenv import load_dotenv
from pathlib import Path
from agent_runtime.core import AgentRuntime
from agent_runtime.hooks import HookManager
from agent_runtime.mcp import MCPHub
from agent_runtime.models import create_model_provider
from agent_runtime.security import PermissionPolicy
from agent_runtime.storage import FileStore
from agent_runtime.tools import ToolRegistry, ToolSpec
from agent_runtime.logging_utils import safe_preview



logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    session_id: str
    text: str = ""
    image_paths: List[str] = field(default_factory=list)


class BusinessAssistantService:
    def __init__(self):
        self._dify_conversations: dict[str, str] = {}
        load_dotenv()
        self.runtime = AgentRuntime(
            model=create_model_provider(),
            tools=create_default_registry(Path.cwd()),
            hooks=HookManager(),
            permission_policy=PermissionPolicy(Path.cwd()),
            system_prompt="You are a coding agent. Use tools when useful.",
        )

    # def handle(self, message: InboundMessage) -> str:
    #     logger.info(
    #         "message_received session=%s text=%s images=%s",
    #         message.session_id,
    #         safe_preview(message.text, 300),
    #         len(message.image_paths),
    #     )
    #     agent_input = self.vision.merge_image_context(message.text, message.image_paths)
    #     history = self.memory.get(message.session_id)
    #     logger.info(
    #         "message_context_ready session=%s history_count=%s agent_input=%s",
    #         message.session_id,
    #         len(history),
    #         safe_preview(agent_input, 500),
    #     )
    #     self.memory.add(message.session_id, "user", agent_input)
    #     answer = self.agent.run(agent_input, history=history)
    #     self.memory.add(message.session_id, "assistant", answer)
    #     logger.info(
    #         "message_answered session=%s answer=%s memory_count=%s",
    #         message.session_id,
    #         safe_preview(answer, 500),
    #         len(self.memory.get(message.session_id)),
    #     )
    #     return answer
    async def handle(self, message: InboundMessage) -> str:
        logger.info(
            "message_received session=%s text=%s images=%s",
            message.session_id,
            safe_preview(message.text, 300),
            len(message.image_paths),
        )
        agent_input = message.text.strip()
        if message.image_paths:
            attachments = "Attachments:\n" + "\n".join(message.image_paths)
            agent_input = "\n\n".join(part for part in (agent_input, attachments) if part)
        
        return await asyncio.to_thread(self.runtime.run_turn, agent_input)

def create_default_registry(workdir: Path) -> ToolRegistry:
    store = FileStore(workdir)
    mcp = MCPHub.from_config()
    registry = mcp.connect("PlantMartBusiness")
    # registry.register(
    #     ToolSpec(
    #         "read_file",
    #         "Read a file in the workspace.",
    #         {
    #             "type": "object",
    #             "properties": {"path": {"type": "string"}},
    #             "required": ["path"],
    #             "additionalProperties": False,
    #         },
    #     ),
    #     lambda path: store.read_text(path),
    # )
    # registry.register(
    #     ToolSpec(
    #         "write_file",
    #         "Write a file in the workspace.",
    #         {
    #             "type": "object",
    #             "properties": {
    #                 "path": {"type": "string"},
    #                 "content": {"type": "string"},
    #             },
    #             "required": ["path", "content"],
    #             "additionalProperties": False,
    #         },
    #     ),
    #     lambda path, content: store.write_text(path, content) or f"Wrote {path}",
    # )
    # registry.register(
    #     ToolSpec(
    #         "glob",
    #         "List files matching a workspace glob.",
    #         {
    #             "type": "object",
    #             "properties": {"pattern": {"type": "string"}},
    #             "required": ["pattern"],
    #             "additionalProperties": False,
    #         },
    #     ),
    #     lambda pattern: "\n".join(store.list_files(pattern)) or "(no matches)",
    # )

    return registry
