from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillPermissions:
    filesystem: str = "none"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillActivation:
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillManifest:
    name: str
    version: str
    description: str
    entrypoint: str
    required_tools: tuple[str, ...] = ()
    permissions: SkillPermissions = field(default_factory=SkillPermissions)
    activation: SkillActivation = field(default_factory=SkillActivation)


@dataclass(frozen=True)
class SkillSnapshot:
    name: str
    version: str
    content_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "version": self.version,
            "content_digest": self.content_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillSnapshot":
        return cls(
            name=str(value["name"]),
            version=str(value["version"]),
            content_digest=str(value["content_digest"]),
        )


@dataclass(frozen=True)
class LoadedSkill:
    manifest: SkillManifest
    root: Path
    content: str
    snapshot: SkillSnapshot


class SkillSnapshotMismatch(RuntimeError):
    """The installed Skill no longer matches a persisted run snapshot."""
