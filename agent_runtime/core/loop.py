from __future__ import annotations

import json
import logging

from agent_runtime.approval import (
    ApprovalRequest,
    ApprovalRepository,
    ApprovalStatus,
    RuntimeIdentity,
    ApprovalCoordinator,
)
from agent_runtime.contracts import ModelRequest, ToolCall, ToolResult
from agent_runtime.core.continuation import decode_blocks, encode_blocks
from agent_runtime.core.ports import (
    HookDispatcher,
    ModelPort,
    NullHooks,
    PermissionChecker,
    SessionRepository,
    ToolCatalog,
)
from agent_runtime.core.results import Completed, InProgress, PendingApproval
from agent_runtime.core.run_coordinator import RunCoordinator
from agent_runtime.core.run_state import RunRecord, RunStatus
from agent_runtime.core.tool_coordinator import ToolExecutionCoordinator
from agent_runtime.core.tool_execution import ToolExecutor
from agent_runtime.security import PermissionAction, PermissionDecision
from agent_runtime.skills import LoadedSkill, SkillSnapshot


logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        model: ModelPort,
        tools: ToolCatalog,
        hooks: HookDispatcher | None = None,
        permission_policy: PermissionChecker | None = None,
        system_prompt: str = "You are a coding agent.",
        max_turns: int = 30,
        approval_store: ApprovalRepository | None = None,
        approval_timeout_seconds: int = 600,
        session_store: SessionRepository | None = None,
        recent_message_limit: int = 20,
        context_manager=None,
        skill_loader=None,
        skill_selector=None,
    ):
        self.model = model
        self.tools = tools
        self.hooks = hooks or NullHooks()
        self.permission_policy = permission_policy
        self.tool_executor = ToolExecutor(
            self.hooks,
            permission_policy,
        )
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.approval_store = approval_store
        self.approval_timeout_seconds = approval_timeout_seconds
        self.session_store = session_store
        self.recent_message_limit = recent_message_limit
        self.context_manager = context_manager
        self.skill_loader = skill_loader
        self.skill_selector = skill_selector
        self.run_coordinator = (
            RunCoordinator(
                session_store,
                recent_message_limit=recent_message_limit,
                context_manager=context_manager,
            )
            if session_store is not None else None
        )
        self.tool_execution = ToolExecutionCoordinator(
            self.tool_executor, session_store
        )
        self.approvals = (
            ApprovalCoordinator(
                approval_store,
                approval_timeout_seconds,
            )
            if approval_store is not None
            else None
        )

    @property
    def owner_id(self) -> str | None:
        return self.session_store.owner_id if self.session_store else None

    def renew_run(self, run_id: str) -> bool:
        if self.session_store is None:
            return False
        return self.session_store.renew_run(run_id)

    def run_turn(
        self, user_input: str, identity: RuntimeIdentity | None = None
    ) -> Completed | PendingApproval | InProgress:
        self.hooks.trigger("UserPromptSubmit", user_input)
        if self.session_store is not None and identity is not None:
            existing = self.session_store.get_inbound(
                identity.platform, identity.message_id
            )
            if existing is not None:
                if existing.cached_response is not None:
                    return Completed(existing.cached_response)
                if existing.run.status is RunStatus.INTERRUPTED:
                    return self.resume_run(existing.run.id)
                if existing.run.status in (
                    RunStatus.FAILED, RunStatus.CANCELLED
                ):
                    return Completed(
                        f"Run is {existing.run.status.value}; "
                        "manual action is required."
                    )
                return InProgress(existing.run.id)
        active_skills = self._select_skills(user_input)
        if self.session_store is None or identity is None:
            return self._run(
                [{"role": "user", "content": user_input}], None, identity, 0,
                active_skills=active_skills,
            )
        started, state = self.run_coordinator.start(
            identity=identity, user_content=user_input,
            skill_snapshots=self._snapshot_dicts(active_skills),
        )
        if not started.is_new:
            if started.cached_response is not None:
                return Completed(started.cached_response)
            if started.run.status is RunStatus.INTERRUPTED:
                return self.resume_run(started.run.id)
            if started.run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
                return Completed(
                    f"Run is {started.run.status.value}; manual action is required."
                )
            return InProgress(started.run.id)
        run = started.run
        messages = state["messages"]
        try:
            return self._run(
                messages, None, identity, 0, run,
                active_skills=active_skills,
            )
        except Exception as exc:
            self.run_coordinator.interrupt(run, exc)
            raise

    def _run(
        self, messages, previous_response_id, identity, start_turn,
        run: RunRecord | None = None,
        active_skills: tuple[LoadedSkill, ...] = (),
    ):
        effective_system = self._effective_system(active_skills)
        for turn in range(start_turn, self.max_turns):
            tool_specs, handlers = self.tools.assemble()
            tool_specs, handlers = self._skill_tool_catalog(
                active_skills, tool_specs, handlers
            )
            if self.context_manager is not None:
                messages = self.context_manager.prepare(
                    messages, system=effective_system, tools=tool_specs
                )
            self._save_checkpoint(
                run,
                "before_model",
                messages,
                previous_response_id,
                turn,
                identity,
                action="model",
                active_skills=active_skills,
            )
            with self._heartbeat(run):
                response = self.model.generate(
                    ModelRequest(
                        messages=self._provider_messages(messages),
                        system=effective_system,
                        tools=tool_specs,
                        previous_response_id=previous_response_id,
                        on_fallback=lambda provider, model: (
                            self._save_checkpoint(
                                run,
                                "before_model_fallback",
                                messages,
                                None,
                                turn,
                                identity,
                                action="model",
                                model_provider=provider,
                                model_name=model,
                                active_skills=active_skills,
                            )
                        ),
                    )
                )
            if response.used_fallback:
                previous_response_id = response.response_id
            else:
                previous_response_id = (
                    response.response_id or previous_response_id
                )
            if not response.tool_calls:
                self._save_checkpoint(
                    run,
                    "before_finalize",
                    messages,
                    previous_response_id,
                    turn,
                    identity,
                    action="finalize",
                    response=response.text,
                    model_provider=response.provider,
                    model_name=response.model,
                    active_skills=active_skills,
                )
                self.hooks.trigger("Stop", messages)
                return self._complete(run, response.text)
            messages.extend(response.blocks)
            self._save_checkpoint(
                run,
                "before_tools",
                messages,
                previous_response_id,
                turn + 1,
                identity,
                action="tools",
                remaining_calls=response.tool_calls,
                model_provider=response.provider,
                model_name=response.model,
                active_skills=active_skills,
            )
            pending = self._process_calls(
                response.tool_calls,
                messages,
                handlers,
                identity,
                previous_response_id,
                turn + 1,
                run,
                active_skills,
            )
            if pending is not None:
                return pending
        return self._complete(run, "Agent stopped after reaching max_turns.")

    def _decision(
        self,
        call: ToolCall,
        active_skills: tuple[LoadedSkill, ...] = (),
    ) -> PermissionDecision:
        if active_skills and call.name not in self._declared_tools(active_skills):
            return PermissionDecision(
                PermissionAction.DENY,
                f"Permission denied: active Skills did not declare {call.name}",
            )
        return self.tool_executor.decision(call)

    def _process_calls(
        self, calls, messages, handlers, identity, previous_response_id, next_turn,
        run: RunRecord | None = None,
        active_skills: tuple[LoadedSkill, ...] = (),
    ):
        results = []
        for index, call in enumerate(calls):
            decision = self._decision(call, active_skills)
            if decision.action is PermissionAction.REQUIRE_APPROVAL:
                if self.approvals is None or identity is None:
                    results.append(
                        self._not_executed(call, "approval channel unavailable")
                    )
                    continue
                continuation = {
                    "messages": encode_blocks(messages + results),
                    "remaining_calls": encode_blocks(calls[index:]),
                    "previous_response_id": previous_response_id,
                    "next_turn": next_turn,
                    "run_id": run.id if run else None,
                    "session_id": run.session_id if run else None,
                    "skill_snapshots": self._snapshot_dicts(active_skills),
                }
                request = self.approvals.create_request(
                    identity=identity,
                    call=call,
                    continuation=continuation,
                )
                self._save_checkpoint(
                    run,
                    "waiting_approval",
                    messages + results,
                    previous_response_id,
                    next_turn,
                    identity,
                    action="approval",
                    approval_id=request.id,
                    active_skills=active_skills,
                )
                logger.info(
                    "approval_created id=%s tool=%s status=%s",
                    request.id,
                    request.tool_name,
                    request.status,
                )
                if self.session_store is not None and run is not None:
                    self.session_store.transition_run(
                        run.id,
                        RunStatus.WAITING_APPROVAL,
                        execution_token=run.execution_token,
                    )
                return PendingApproval(request)
            if decision.action is PermissionAction.DENY:
                output = decision.reason
            else:
                output = self._invoke_tool(call, handlers, run)
            results.append(ToolResult(call.id, str(output)))
            remaining = calls[index + 1:]
            self._save_checkpoint(
                run,
                "after_tool",
                messages + results,
                previous_response_id,
                next_turn,
                identity,
                action="tools" if remaining else "model",
                remaining_calls=remaining if remaining else None,
                active_skills=active_skills,
            )
        messages.extend(results)
        return None

    def _invoke_tool(self, call, handlers, run):
        with self._heartbeat(run):
            return self.tool_execution.invoke(call, handlers, run)

    def resume(
        self, approval_id: str
    ) -> Completed | PendingApproval | InProgress:
        if self.approvals is None:
            return Completed("Approval storage is unavailable.")
        request = self.approvals.load_for_resume(approval_id)
        if request is None:
            return Completed("Approval request was not found.")
        if request.status is ApprovalStatus.PENDING:
            return PendingApproval(request)
        context = request.continuation
        run_id = context.get("run_id")
        run = None
        if self.session_store is not None and run_id is not None:
            run = self.session_store.get_run(run_id)
            if run is None:
                return Completed("Run was not found.")
            if run.status is RunStatus.COMPLETED:
                return Completed(self.session_store.cached_response(run_id) or "")
            if run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
                return Completed(
                    f"Run is {run.status.value}; manual action is required."
                )
            active_skills = self._restore_skills(
                context.get("skill_snapshots", [])
            )
            run = self.run_coordinator.claim(
                run_id, {RunStatus.WAITING_APPROVAL, RunStatus.INTERRUPTED}
            )
            if run is None:
                return InProgress(run_id)
        elif request.status in (
            ApprovalStatus.EXECUTING,
            ApprovalStatus.COMPLETED,
            ApprovalStatus.FAILED,
        ):
            return Completed("Approval was already processed; tool was not replayed.")
        else:
            active_skills = self._restore_skills(
                context.get("skill_snapshots", [])
            )
        messages = decode_blocks(context["messages"])
        calls = decode_blocks(context["remaining_calls"])
        current = calls[0]
        if not isinstance(current, ToolCall):
            return Completed("Invalid approval continuation.")
        result = self._resume_result(request, current, run, active_skills)
        if result is None:
            return Completed(
                "Approval result is uncertain; run requires manual reconciliation."
            )
        messages.append(result)
        self._save_checkpoint(
            run,
            "after_approval",
            messages,
            context.get("previous_response_id"),
            int(context.get("next_turn", 0)),
            request.identity,
            action="tools" if len(calls) > 1 else "model",
            remaining_calls=calls[1:] if len(calls) > 1 else None,
            active_skills=active_skills,
        )
        _, handlers = self.tools.assemble()
        pending = self._process_calls(
            calls[1:],
            messages,
            handlers,
            request.identity,
            context.get("previous_response_id"),
            int(context.get("next_turn", 0)),
            run,
            active_skills,
        )
        if pending is not None:
            return pending
        return self._run(
            messages,
            context.get("previous_response_id"),
            request.identity,
            int(context.get("next_turn", 0)),
            run,
            active_skills=active_skills,
        )

    def resume_run(
        self, run_id: str
    ) -> Completed | PendingApproval | InProgress:
        if self.session_store is None:
            return Completed("Session storage is unavailable.")
        run = self.session_store.get_run(run_id)
        if run is None:
            return Completed("Run was not found.")
        if run.status is RunStatus.COMPLETED:
            return Completed(self.session_store.cached_response(run_id) or "")
        if run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
            return Completed(
                f"Run is {run.status.value}; manual action is required."
            )
        if run.status is RunStatus.WAITING_APPROVAL:
            return Completed("Run is waiting for approval.")
        checkpoint = self.session_store.latest_checkpoint(run_id)
        if checkpoint is None:
            return Completed("Run has no recoverable checkpoint.")
        state = self.run_coordinator.codec.decode(checkpoint.state)
        active_skills = self._restore_skills(
            state.get("skill_snapshots", [])
        )
        if state.get("action") == "approval":
            approval_id = state.get("approval_id")
            if self.approvals is None or not approval_id:
                return Completed("Run has no recoverable approval request.")
            request = self.approvals.load_for_resume(approval_id)
            if request is None:
                return Completed("Approval request was not found.")
            if request.status is ApprovalStatus.PENDING:
                self.session_store.transition_run(run_id, RunStatus.WAITING_APPROVAL)
                return PendingApproval(request)
            return self.resume(approval_id)
        run = self.run_coordinator.claim(run_id, {RunStatus.INTERRUPTED})
        if run is None:
            return InProgress(run_id)
        messages = state["messages"]
        identity = self._decode_identity(state.get("identity"))
        previous_response_id = state.get("previous_response_id")
        next_turn = int(state.get("next_turn", 0))
        try:
            action = state.get("action", "model")
            if action == "finalize":
                return self._complete(run, str(state.get("response", "")))
            if action == "tools":
                calls = state.get("remaining_calls", [])
                _, handlers = self.tools.assemble()
                pending = self._process_calls(
                    calls, messages, handlers, identity, previous_response_id,
                    next_turn, run, active_skills,
                )
                if pending is not None:
                    return pending
            return self._run(
                messages, previous_response_id, identity, next_turn,
                run, active_skills=active_skills,
            )
        except Exception as exc:
            self.run_coordinator.interrupt(run, exc)
            raise

    def _save_checkpoint(
        self, run, phase, messages, previous_response_id, next_turn,
        identity, *, action, remaining_calls=None, response=None,
        approval_id=None, model_provider=None, model_name=None,
        active_skills: tuple[LoadedSkill, ...] = (),
    ):
        if self.run_coordinator is None or run is None:
            return
        self.run_coordinator.save(
            run,
            phase,
            action=action,
            messages=messages,
            previous_response_id=previous_response_id,
            next_turn=next_turn,
            identity=identity,
            remaining_calls=remaining_calls,
            response=response,
            approval_id=approval_id,
            summary_version=self._summary_version(messages),
            model_provider=model_provider,
            model_name=model_name,
            skill_snapshots=self._snapshot_dicts(active_skills),
        )

    def _select_skills(self, user_input: str) -> tuple[LoadedSkill, ...]:
        if self.skill_loader is None or self.skill_selector is None:
            return ()
        return self.skill_selector.select(user_input, self.skill_loader.load())

    def _restore_skills(
        self, values: list[dict[str, str]]
    ) -> tuple[LoadedSkill, ...]:
        if not values:
            return ()
        if self.skill_loader is None:
            raise RuntimeError("Run requires Skills but Skill loading is disabled")
        snapshots = tuple(SkillSnapshot.from_dict(value) for value in values)
        return self.skill_loader.restore(snapshots)

    @staticmethod
    def _snapshot_dicts(
        skills: tuple[LoadedSkill, ...]
    ) -> list[dict[str, str]]:
        return [skill.snapshot.to_dict() for skill in skills]

    def _effective_system(self, skills: tuple[LoadedSkill, ...]) -> str:
        if not skills:
            return self.system_prompt
        sections = [self.system_prompt, "\n\nActive Skills:"]
        for skill in skills:
            sections.append(
                f"\n\n## {skill.manifest.name} "
                f"({skill.manifest.version})\n{skill.content}"
            )
        return "".join(sections)

    @staticmethod
    def _declared_tools(skills: tuple[LoadedSkill, ...]) -> frozenset[str]:
        return frozenset(
            tool
            for skill in skills
            for tool in skill.manifest.required_tools
        )

    def _skill_tool_catalog(self, skills, tool_specs, handlers):
        if not skills:
            return tool_specs, handlers
        declared = self._declared_tools(skills)
        return (
            [spec for spec in tool_specs if spec.name in declared],
            {name: handler for name, handler in handlers.items()
             if name in declared},
        )

    def _complete(self, run, response):
        if self.run_coordinator is not None and run is not None:
            self.run_coordinator.complete(run, response)
        return Completed(response)

    @staticmethod
    def _encode_identity(identity):
        return RunCoordinator.encode_identity(identity)

    def _heartbeat(self, run):
        if self.run_coordinator is None:
            from contextlib import nullcontext
            return nullcontext()
        return self.run_coordinator.heartbeat(run)

    @staticmethod
    def _summary_version(messages) -> int | None:
        for item in messages:
            if isinstance(item, dict) and item.get('summary_version') is not None:
                return int(item['summary_version'])
        return None

    @staticmethod
    def _provider_messages(messages):
        return [
            {key: value for key, value in item.items()
             if key != 'summary_version'}
            if isinstance(item, dict) else item
            for item in messages
        ]

    @staticmethod
    def _decode_identity(value):
        if not value:
            return None
        return RuntimeIdentity(**value)

    def _resume_result(
        self,
        request: ApprovalRequest,
        call: ToolCall,
        run: RunRecord | None,
        active_skills: tuple[LoadedSkill, ...] = (),
    ):
        if request.status in (ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED):
            self.approval_store.mark_consumed(request.id)
            reason = (
                "user rejected the tool call"
                if request.status is ApprovalStatus.REJECTED
                else "approval expired"
            )
            return self._not_executed(call, reason)
        if request.status in (
            ApprovalStatus.COMPLETED,
            ApprovalStatus.EXECUTING,
            ApprovalStatus.FAILED,
        ):
            if self.session_store is None or run is None:
                return None
            claim = self.session_store.get_tool(
                run.id, call.id, call.name, call.input
            )
            if claim is not None and claim.output is not None:
                if request.status is ApprovalStatus.EXECUTING:
                    self.approval_store.complete(request.id)
                return ToolResult(call.id, claim.output)
            if request.status is ApprovalStatus.FAILED:
                return self._not_executed(
                    call, request.error or "approved tool execution failed"
                )
            self.session_store.transition_run(
                run.id,
                RunStatus.FAILED,
                "approved tool result is uncertain",
                execution_token=run.execution_token,
            )
            return None
        if request.status is not ApprovalStatus.APPROVED:
            return None
        decision = self._decision(call, active_skills)
        valid = (
            decision.action is PermissionAction.REQUIRE_APPROVAL
            and self.approvals is not None
            and self.approvals.matches(request, call)
        )
        claimed = self.approval_store.claim_execution(request.id)
        if claimed is None:
            return None
        logger.info(
            "approval_claimed id=%s tool=%s status=%s",
            request.id,
            request.tool_name,
            ApprovalStatus.EXECUTING,
        )
        if not valid:
            self.approval_store.fail(request.id, "approval validation failed")
            return self._not_executed(call, "approval validation failed")
        _, handlers = self.tools.assemble()
        try:
            output = self._invoke_tool(call, handlers, run)
        except Exception as exc:
            self.approval_store.fail(request.id, str(exc))
            logger.warning(
                "approval_failed id=%s tool=%s status=%s",
                request.id,
                request.tool_name,
                ApprovalStatus.FAILED,
            )
            return ToolResult(call.id, f"Tool execution failed: {exc}")
        self.approval_store.complete(request.id)
        logger.info(
            "approval_completed id=%s tool=%s status=%s",
            request.id,
            request.tool_name,
            ApprovalStatus.COMPLETED,
        )
        return ToolResult(call.id, str(output))

    @staticmethod
    def _not_executed(call: ToolCall, reason: str) -> ToolResult:
        content = json.dumps(
            {"executed": False, "reason": reason}, ensure_ascii=False
        )
        return ToolResult(call.id, content)

    def recoverable_approvals(self) -> list[ApprovalRequest]:
        if self.approvals is None:
            return []
        return self.approvals.recoverable()
