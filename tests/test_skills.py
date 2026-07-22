from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.approval import RuntimeIdentity
from agent_runtime.contracts import ModelResponse, TextBlock, ToolCall
from agent_runtime.core import AgentRuntime
from agent_runtime.sessions import SQLiteSessionStore
from agent_runtime.settings import SkillsSettings, load_settings
from agent_runtime.skills import (
    SkillLoader,
    SkillSelector,
    SkillSnapshotMismatch,
)
from agent_runtime.tools import ToolRegistry, ToolSpec


def _write_skill(
    root: Path,
    directory: str,
    *,
    name: str = "code-review",
    version: str = "1.0.0",
    entrypoint: str = "SKILL.md",
    required_tools: tuple[str, ...] = ("read_file",),
    filesystem: str = "read",
    keywords: tuple[str, ...] = ("代码审查", "review"),
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    tools = "\n".join(f"  - {tool}" for tool in required_tools)
    words = "\n".join(f"      - {word}" for word in keywords)
    (skill_dir / "skill.yaml").write_text(
        f"""name: {name}
version: {version}
description: 审查代码变更
entrypoint: {entrypoint}
required_tools:
{tools or '  []'}
permissions:
  filesystem: {filesystem}
activation:
  keywords:
{words or '      []'}
""",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("请检查安全性与回归风险。", encoding="utf-8")
    return skill_dir


def test_loader_discovers_and_validates_skill(tmp_path: Path):
    _write_skill(tmp_path, "review")

    skills = SkillLoader(
        (tmp_path,),
        available_tools={"read_file"},
        allowed_permissions={"filesystem": "read"},
    ).load()

    assert [skill.manifest.name for skill in skills] == ["code-review"]
    assert skills[0].content == "请检查安全性与回归风险。"
    assert len(skills[0].snapshot.content_digest) == 64


def test_loader_expands_environment_variable_in_root(
    tmp_path: Path, monkeypatch
):
    _write_skill(tmp_path, "review")
    monkeypatch.setenv("AGENT_TEST_SKILL_ROOT", str(tmp_path))

    skills = SkillLoader(
        ("$AGENT_TEST_SKILL_ROOT",),
        available_tools={"read_file"},
    ).load()

    assert [skill.manifest.name for skill in skills] == ["code-review"]


@pytest.mark.parametrize("version", ["1", "latest", "1.0", "v1.0.0"])
def test_loader_rejects_invalid_semver(tmp_path: Path, version: str):
    _write_skill(tmp_path, "review", version=version)
    with pytest.raises(ValueError, match="version"):
        SkillLoader((tmp_path,), available_tools={"read_file"}).load()


def test_loader_rejects_duplicate_names(tmp_path: Path):
    _write_skill(tmp_path, "one")
    _write_skill(tmp_path, "two")
    with pytest.raises(ValueError, match="Duplicate skill name"):
        SkillLoader((tmp_path,), available_tools={"read_file"}).load()


def test_loader_rejects_entrypoint_escape(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "review", entrypoint="../outside.md")
    (skill_dir.parent / "outside.md").write_text("bad", encoding="utf-8")
    with pytest.raises(ValueError, match="entrypoint escapes"):
        SkillLoader((tmp_path,), available_tools={"read_file"}).load()


def test_loader_rejects_linked_skill_outside_root(tmp_path: Path):
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    root.mkdir()
    _write_skill(outside, "review")
    try:
        (root / "linked").symlink_to(
            outside / "review", target_is_directory=True
        )
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="escapes configured root"):
        SkillLoader((root,), available_tools={"read_file"}).load()


def test_loader_rejects_missing_tools_and_permission_escalation(tmp_path: Path):
    _write_skill(tmp_path, "review", required_tools=("write_file",))
    with pytest.raises(ValueError, match="unavailable tools"):
        SkillLoader((tmp_path,), available_tools={"read_file"}).load()

    (tmp_path / "review" / "skill.yaml").unlink()
    _write_skill(tmp_path, "write", filesystem="write")
    with pytest.raises(ValueError, match="permission exceeds"):
        SkillLoader(
            (tmp_path,),
            available_tools={"read_file"},
            allowed_permissions={"filesystem": "read"},
        ).load()


def test_loader_rejects_tool_above_declared_permission(tmp_path: Path):
    _write_skill(
        tmp_path, "write", required_tools=("write_file",), filesystem="read"
    )
    with pytest.raises(ValueError, match="exceeds declared filesystem"):
        SkillLoader(
            (tmp_path,),
            available_tools={"write_file"},
            allowed_permissions={"filesystem": "write"},
        ).load()


def test_selector_activates_only_ranked_small_set(tmp_path: Path):
    first = _write_skill(tmp_path, "review")
    _write_skill(
        tmp_path, "docs", name="write-docs", required_tools=(),
        keywords=("文档",),
    )
    skills = SkillLoader((tmp_path,), available_tools={"read_file"}).load()

    selected = SkillSelector(max_active=1).select("请做一次代码审查", skills)

    assert [skill.manifest.name for skill in selected] == ["code-review"]
    assert selected[0].root == first.resolve()


def test_skill_settings_parse_yaml_and_environment(tmp_path: Path, monkeypatch):
    config = tmp_path / "settings.yaml"
    config.write_text(
        "skills:\n"
        "  enabled: true\n"
        "  paths: [project-skills, shared-skills]\n"
        "  max_active: 2\n"
        "  allowed_filesystem: none\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_SKILLS_MAX_ACTIVE", "1")

    settings = load_settings(config)

    assert settings.skills == SkillsSettings(
        enabled=True,
        paths=(Path("project-skills"), Path("shared-skills")),
        max_active=1,
        allowed_filesystem="none",
    )


def test_loader_rejects_changed_snapshot_on_resume(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "review")
    loader = SkillLoader((tmp_path,), available_tools={"read_file"})
    snapshot = loader.load()[0].snapshot
    (skill_dir / "SKILL.md").write_text("changed", encoding="utf-8")

    with pytest.raises(SkillSnapshotMismatch, match="content changed"):
        loader.restore((snapshot,))


class _Model:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _SequenceModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


def test_runtime_injects_selected_skill_and_saves_snapshot(tmp_path: Path):
    _write_skill(tmp_path / "skills", "review")
    registry = ToolRegistry()
    registry.register(
        ToolSpec("read_file", "read", {"type": "object"}), lambda: "ok"
    )
    loader = SkillLoader(
        (tmp_path / "skills",), available_tools={"read_file"}
    )
    model = _Model(ModelResponse([TextBlock("done")]))
    store = SQLiteSessionStore(tmp_path / "runtime.db")
    runtime = AgentRuntime(
        model,
        registry,
        session_store=store,
        skill_loader=loader,
        skill_selector=SkillSelector(max_active=1),
    )

    result = runtime.run_turn(
        "请进行代码审查",
        RuntimeIdentity("wecom", "chat", "user", "skill-message"),
    )

    assert result == "done"
    assert "请检查安全性与回归风险。" in model.requests[0].system
    run = store.begin_inbound(
        platform="wecom", conversation_id="chat", sender_id="user",
        message_id="skill-message",
    ).run
    snapshots = store.latest_checkpoint(run.id).state["skill_snapshots"]
    assert snapshots[0]["name"] == "code-review"
    assert snapshots[0]["version"] == "1.0.0"


def test_resume_rejects_skill_snapshot_drift_before_claim(tmp_path: Path):
    skill_dir = _write_skill(tmp_path / "skills", "review")
    registry = ToolRegistry()
    registry.register(
        ToolSpec("read_file", "read", {"type": "object"}), lambda: "ok"
    )
    loader = SkillLoader(
        (tmp_path / "skills",), available_tools={"read_file"}
    )
    store = SQLiteSessionStore(tmp_path / "runtime.db")
    identity = RuntimeIdentity("wecom", "chat", "user", "interrupted")
    with pytest.raises(RuntimeError, match="model failed"):
        AgentRuntime(
            _Model(RuntimeError("model failed")), registry,
            session_store=store, skill_loader=loader,
            skill_selector=SkillSelector(),
        ).run_turn("代码审查", identity)
    run = store.begin_inbound(
        platform="wecom", conversation_id="chat", sender_id="user",
        message_id="interrupted",
    ).run
    (skill_dir / "SKILL.md").write_text("changed", encoding="utf-8")

    with pytest.raises(SkillSnapshotMismatch):
        AgentRuntime(
            _Model(ModelResponse([TextBlock("wrong")])) , registry,
            session_store=store, skill_loader=loader,
            skill_selector=SkillSelector(),
        ).resume_run(run.id)

    assert store.get_run(run.id).status.value == "interrupted"


def test_active_skill_cannot_call_undeclared_tool(tmp_path: Path):
    _write_skill(tmp_path / "skills", "review")
    registry = ToolRegistry()
    called = []
    registry.register(
        ToolSpec("read_file", "read", {"type": "object"}), lambda: "read"
    )
    registry.register(
        ToolSpec("write_file", "write", {"type": "object"}),
        lambda: called.append(True) or "written",
    )
    loader = SkillLoader(
        (tmp_path / "skills",),
        available_tools={"read_file", "write_file"},
    )
    model = _SequenceModel([
        ModelResponse([ToolCall("call-1", "write_file", {})]),
        ModelResponse([TextBlock("done")]),
    ])

    result = AgentRuntime(
        model, registry, skill_loader=loader,
        skill_selector=SkillSelector(),
    ).run_turn("代码审查")

    assert result == "done"
    assert called == []
    assert [spec.name for spec in model.requests[0].tools] == ["read_file"]


def test_cached_inbound_does_not_reload_changed_skill(tmp_path: Path):
    skill_dir = _write_skill(tmp_path / "skills", "review")
    registry = ToolRegistry()
    registry.register(
        ToolSpec("read_file", "read", {"type": "object"}), lambda: "ok"
    )
    loader = SkillLoader(
        (tmp_path / "skills",), available_tools={"read_file"}
    )
    store = SQLiteSessionStore(tmp_path / "runtime.db")
    runtime = AgentRuntime(
        _Model(ModelResponse([TextBlock("cached")])), registry,
        session_store=store, skill_loader=loader,
        skill_selector=SkillSelector(),
    )
    identity = RuntimeIdentity("wecom", "chat", "user", "cached-message")
    assert runtime.run_turn("代码审查", identity) == "cached"
    (skill_dir / "skill.yaml").write_text("version: invalid", encoding="utf-8")

    assert runtime.run_turn("代码审查", identity) == "cached"
