from __future__ import annotations


class AdminAPI:
    """与 Web 框架无关的薄管理 API 门面。

    调用方必须从可信认证中间件构造 actor，不能从请求体接受 actor。
    """

    def __init__(self, service) -> None:
        self.service = service

    def list_sessions(self, actor, **query):
        return self.service.list_sessions(actor, **query)

    def list_runs(self, actor, **query):
        return self.service.list_runs(actor, **query)

    def list_checkpoints(self, actor, run_id, **query):
        return self.service.list_checkpoints(actor, run_id, **query)

    def list_tool_executions(self, actor, run_id, **query):
        return self.service.list_tool_executions(actor, run_id, **query)

    def list_approvals(self, actor, **query):
        return self.service.list_approvals(actor, **query)

    def pause_run(self, actor, run_id, **command):
        return self.service.pause_run(actor, run_id, **command)

    def cancel_run(self, actor, run_id, **command):
        return self.service.cancel_run(actor, run_id, **command)

    def resume_run(self, actor, run_id, **command):
        return self.service.resume_run(actor, run_id, **command)

    def resolve_uncertain_tool(self, actor, run_id, call_id, **command):
        return self.service.resolve_uncertain_tool(
            actor, run_id, call_id, **command
        )

    def export_audit(self, actor, **query):
        return self.service.export_audit(actor, **query)
