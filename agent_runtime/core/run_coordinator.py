from __future__ import annotations

from contextlib import contextmanager
from threading import Event, Thread
from typing import Any, Iterator

from agent_runtime.core.checkpoints import CheckpointCodec
from agent_runtime.core.ports import SessionRepository
from agent_runtime.core.run_state import RunLeaseLost, RunRecord, RunStatus


class RunCoordinator:
    """Coordinates run ownership, checkpoints and durable completion."""

    def __init__(
        self,
        repository: SessionRepository,
        *,
        recent_message_limit: int,
        codec: CheckpointCodec | None = None,
        context_manager: Any | None = None,
    ):
        self.repository = repository
        self.recent_message_limit = recent_message_limit
        self.codec = codec or CheckpointCodec()
        self.context_manager = context_manager

    def start(self, *, identity: Any, user_content: str) -> tuple[Any, dict[str, Any]]:
        initial = self.codec.encode(
            action="model",
            messages=[{"role": "user", "content": user_content}],
            previous_response_id=None,
            next_turn=0,
            identity=self.encode_identity(identity),
        )
        started = self.repository.start_inbound(
            platform=identity.platform,
            conversation_id=identity.conversation_id,
            sender_id=identity.sender_id,
            message_id=identity.message_id,
            metadata=identity.metadata,
            user_content=user_content,
            initial_checkpoint=initial,
            recent_message_limit=self.recent_message_limit,
        )
        checkpoint = self.repository.latest_checkpoint(started.run.id)
        state = self.codec.decode(checkpoint.state) if checkpoint else {}
        if started.is_new and self.context_manager is not None:
            built = self.context_manager.build(
                started.run.session_id, started.run.id
            )
            state['messages'] = built.messages
            if built.summary_version is not None:
                state['summary_version'] = built.summary_version
            encoded = self.codec.encode(
                action='model', messages=built.messages,
                previous_response_id=None, next_turn=0,
                identity=self.encode_identity(identity),
                summary_version=built.summary_version,
            )
            self.repository.save_checkpoint(
                started.run.id, 'context_built', encoded,
                execution_token=started.run.execution_token,
            )
        return started, state

    def save(
        self,
        run: RunRecord,
        phase: str,
        *,
        action: str,
        messages: list[Any],
        previous_response_id: str | None,
        next_turn: int,
        identity: Any,
        remaining_calls: list[Any] | None = None,
        response: str | None = None,
        approval_id: str | None = None,
        summary_version: int | None = None,
    ) -> None:
        state = self.codec.encode(
            action=action,
            messages=messages,
            previous_response_id=previous_response_id,
            next_turn=next_turn,
            identity=self.encode_identity(identity),
            remaining_calls=remaining_calls,
            response=response,
            approval_id=approval_id,
            summary_version=summary_version,
        )
        self.repository.save_checkpoint(
            run.id, phase, state, execution_token=run.execution_token
        )

    def complete(self, run: RunRecord, response: str) -> None:
        if not self.repository.complete_run(
            run.id, response, execution_token=run.execution_token
        ):
            raise RunLeaseLost(f"Cannot complete run without ownership: {run.id}")

    def interrupt(self, run: RunRecord, error: Exception) -> None:
        self.repository.transition_run(
            run.id,
            RunStatus.INTERRUPTED,
            str(error),
            execution_token=run.execution_token,
        )

    def claim(
        self, run_id: str, expected_statuses: set[RunStatus]
    ) -> RunRecord | None:
        if not self.repository.claim_run(run_id, expected_statuses):
            return None
        return self.repository.get_run(run_id)

    @contextmanager
    def heartbeat(self, run: RunRecord | None) -> Iterator[None]:
        if run is None:
            yield
            return
        stopped = Event()
        lost = Event()

        def renew() -> None:
            while not stopped.wait(self.repository.lease_refresh_interval):
                if not self.repository.renew_run(run.id, run.execution_token):
                    lost.set()
                    return

        if not self.repository.renew_run(run.id, run.execution_token):
            raise RunLeaseLost(f"Run lease lost: {run.id}")
        worker = Thread(target=renew, name=f"run-heartbeat:{run.id}", daemon=True)
        worker.start()
        try:
            yield
            if lost.is_set():
                raise RunLeaseLost(f"Run lease lost: {run.id}")
        finally:
            stopped.set()
            worker.join(timeout=1)

    @staticmethod
    def encode_identity(identity: Any) -> dict[str, Any] | None:
        if identity is None:
            return None
        return {
            "platform": identity.platform,
            "conversation_id": identity.conversation_id,
            "sender_id": identity.sender_id,
            "message_id": identity.message_id,
            "metadata": identity.metadata,
        }
