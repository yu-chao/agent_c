from __future__ import annotations

from typing import Any

from agent_runtime.core.continuation import decode_blocks, encode_blocks


class CheckpointCodec:
    """Owns the versioned, provider-neutral checkpoint wire format."""

    version = 1

    def replace_messages(
        self, state: dict[str, Any], messages: list[Any]
    ) -> dict[str, Any]:
        encoded = dict(state)
        encoded["schema_version"] = self.version
        encoded["messages"] = encode_blocks(messages)
        return encoded

    def encode(
        self,
        *,
        action: str,
        messages: list[Any],
        previous_response_id: str | None,
        next_turn: int,
        identity: dict[str, Any] | None,
        remaining_calls: list[Any] | None = None,
        response: str | None = None,
        approval_id: str | None = None,
        summary_version: int | None = None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "schema_version": self.version,
            "action": action,
            "messages": encode_blocks(messages),
            "previous_response_id": previous_response_id,
            "next_turn": next_turn,
            "identity": identity,
        }
        if remaining_calls is not None:
            state["remaining_calls"] = encode_blocks(remaining_calls)
        if response is not None:
            state["response"] = response
        if approval_id is not None:
            state["approval_id"] = approval_id
        if summary_version is not None:
            state["summary_version"] = summary_version
        return state

    def decode(self, state: dict[str, Any]) -> dict[str, Any]:
        version = int(state.get("schema_version", 0))
        if version not in (0, self.version):
            raise ValueError(f"Unsupported checkpoint schema version: {version}")
        decoded = dict(state)
        decoded["messages"] = decode_blocks(state.get("messages", []))
        decoded["remaining_calls"] = decode_blocks(
            state.get("remaining_calls", [])
        )
        return decoded
