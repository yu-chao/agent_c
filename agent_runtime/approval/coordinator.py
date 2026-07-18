from __future__ import annotations

from agent_runtime.approval.models import (
    ApprovalRequest,
    RuntimeIdentity,
    hash_tool_arguments,
)
from agent_runtime.approval.ports import ApprovalRepository
from agent_runtime.contracts import ToolCall


class ApprovalCoordinator:
    def __init__(
        self,
        repository: ApprovalRepository,
        timeout_seconds: int = 600,
    ):
        self.repository = repository
        self.timeout_seconds = timeout_seconds

    def create_request(
        self,
        *,
        identity: RuntimeIdentity,
        call: ToolCall,
        continuation: dict,
    ) -> ApprovalRequest:
        request = ApprovalRequest.create(
            identity=identity,
            tool_call_id=call.id,
            tool_name=call.name,
            tool_input=call.input,
            continuation=continuation,
            timeout_seconds=self.timeout_seconds,
        )
        return self.repository.create(request)

    def load_for_resume(self, approval_id: str) -> ApprovalRequest | None:
        self.repository.expire_pending()
        return self.repository.get(approval_id)

    def matches(self, request: ApprovalRequest, call: ToolCall) -> bool:
        return (
            call.name == request.tool_name
            and hash_tool_arguments(call.name, call.input)
            == request.arguments_hash
        )

    def recoverable(self) -> list[ApprovalRequest]:
        self.repository.expire_pending()
        return self.repository.list_resumable()
