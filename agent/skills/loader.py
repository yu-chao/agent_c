from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .models import (
    LoadedSkill,
    SkillActivation,
    SkillManifest,
    SkillPermissions,
    SkillSnapshot,
    SkillSnapshotMismatch,
)


_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MANIFEST_NAMES = ("skill.yaml", "skill.yml")
_FILESYSTEM_LEVELS = {"none": 0, "read": 1, "write": 2}
_TOOL_FILESYSTEM_LEVELS = {
    "read_file": "read",
    "write_file": "write",
    "edit_file": "write",
    "bash": "write",
}
_MANIFEST_FIELDS = frozenset(
    {"name", "version", "description", "entrypoint", "required_tools",
     "permissions", "activation"}
)


class SkillLoader:
    """Discovers and validates declarative Skills without executing them."""

    def __init__(
        self,
        roots: Iterable[str | Path],
        *,
        available_tools: Iterable[str] = (),
        allowed_permissions: Mapping[str, Any] | None = None,
    ):
        self.roots = tuple(_normalize_root(root) for root in roots)
        self.available_tools = frozenset(available_tools)
        self.allowed_permissions = dict(allowed_permissions or {})

    def load(self) -> tuple[LoadedSkill, ...]:
        loaded: list[LoadedSkill] = []
        names: set[str] = set()
        for manifest_path in self._manifest_paths():
            skill = self._load_one(manifest_path)
            name = skill.manifest.name
            if name in names:
                raise ValueError(f"Duplicate skill name: {name}")
            names.add(name)
            loaded.append(skill)
        return tuple(sorted(loaded, key=lambda item: item.manifest.name))

    def restore(
        self, snapshots: Iterable[SkillSnapshot]
    ) -> tuple[LoadedSkill, ...]:
        current = {skill.manifest.name: skill for skill in self.load()}
        restored: list[LoadedSkill] = []
        for snapshot in snapshots:
            skill = current.get(snapshot.name)
            if skill is None:
                raise SkillSnapshotMismatch(
                    f"Skill is no longer installed: {snapshot.name}"
                )
            if skill.manifest.version != snapshot.version:
                raise SkillSnapshotMismatch(
                    f"Skill version changed: {snapshot.name} "
                    f"{snapshot.version} -> {skill.manifest.version}"
                )
            if skill.snapshot.content_digest != snapshot.content_digest:
                raise SkillSnapshotMismatch(
                    f"Skill content changed without a compatible snapshot: "
                    f"{snapshot.name}"
                )
            restored.append(skill)
        return tuple(restored)

    def _manifest_paths(self) -> tuple[Path, ...]:
        paths: set[Path] = set()
        for root in self.roots:
            if not root.exists():
                continue
            if not root.is_dir():
                raise ValueError(f"Skill root must be a directory: {root}")
            for name in _MANIFEST_NAMES:
                for candidate in root.glob(f"*/{name}"):
                    paths.add(self._inside_root(candidate, root))
                candidate = root / name
                if candidate.is_file():
                    paths.add(self._inside_root(candidate, root))
        return tuple(sorted(paths))

    @staticmethod
    def _inside_root(candidate: Path, root: Path) -> Path:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"Skill manifest escapes configured root: {candidate}"
            ) from exc
        return resolved

    def _load_one(self, manifest_path: Path) -> LoadedSkill:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Skill manifest must be a mapping: {manifest_path}")
        unknown = sorted(str(key) for key in raw if key not in _MANIFEST_FIELDS)
        if unknown:
            raise ValueError(f"Unknown skill manifest field: {unknown[0]}")
        manifest = self._parse_manifest(raw, manifest_path)
        skill_root = manifest_path.parent.resolve()
        entrypoint = (skill_root / manifest.entrypoint).resolve()
        try:
            entrypoint.relative_to(skill_root)
        except ValueError as exc:
            raise ValueError(
                f"Skill entrypoint escapes its directory: {manifest.entrypoint}"
            ) from exc
        if not entrypoint.is_file():
            raise ValueError(f"Skill entrypoint does not exist: {entrypoint}")
        content = entrypoint.read_text(encoding="utf-8")
        digest_payload = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\0" + content.encode("utf-8")
        digest = hashlib.sha256(digest_payload).hexdigest()
        return LoadedSkill(
            manifest=manifest,
            root=skill_root,
            content=content,
            snapshot=SkillSnapshot(manifest.name, manifest.version, digest),
        )

    def _parse_manifest(
        self, raw: dict[str, Any], manifest_path: Path
    ) -> SkillManifest:
        name = _required_text(raw, "name", manifest_path)
        if not _NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid skill name: {name}")
        version = _required_text(raw, "version", manifest_path)
        if not _SEMVER_PATTERN.fullmatch(version):
            raise ValueError(f"Invalid skill version: {version}")
        description = _required_text(raw, "description", manifest_path)
        entrypoint = _required_text(raw, "entrypoint", manifest_path)
        required_tools = _text_list(raw.get("required_tools", ()), "required_tools")
        missing = sorted(set(required_tools) - self.available_tools)
        if missing:
            raise ValueError(
                f"Skill {name} requires unavailable tools: {', '.join(missing)}"
            )
        permissions = self._permissions(raw.get("permissions", {}), name)
        for tool in required_tools:
            required_level = _TOOL_FILESYSTEM_LEVELS.get(tool)
            if required_level is not None and (
                _FILESYSTEM_LEVELS[permissions.filesystem]
                < _FILESYSTEM_LEVELS[required_level]
            ):
                raise ValueError(
                    f"Skill {name} tool {tool} exceeds declared filesystem "
                    "permission"
                )
        activation_raw = raw.get("activation", {})
        if not isinstance(activation_raw, dict):
            raise ValueError("Skill activation must be a mapping")
        unknown_activation = set(activation_raw) - {"keywords"}
        if unknown_activation:
            raise ValueError(
                f"Unknown skill activation field: {sorted(unknown_activation)[0]}"
            )
        activation = SkillActivation(
            _text_list(activation_raw.get("keywords", ()), "activation.keywords")
        )
        return SkillManifest(
            name, version, description, entrypoint, required_tools,
            permissions, activation,
        )

    def _permissions(self, value: Any, skill_name: str) -> SkillPermissions:
        if not isinstance(value, dict):
            raise ValueError("Skill permissions must be a mapping")
        filesystem = value.get("filesystem", "none")
        if filesystem not in _FILESYSTEM_LEVELS:
            raise ValueError("Skill filesystem permission must be none, read or write")
        allowed_filesystem = self.allowed_permissions.get("filesystem", "write")
        if allowed_filesystem not in _FILESYSTEM_LEVELS:
            raise ValueError("Configured filesystem permission is invalid")
        if _FILESYSTEM_LEVELS[filesystem] > _FILESYSTEM_LEVELS[allowed_filesystem]:
            raise ValueError(
                f"Skill {skill_name} filesystem permission exceeds configured scope"
            )
        extra = {key: item for key, item in value.items() if key != "filesystem"}
        for key, requested in extra.items():
            if key not in self.allowed_permissions or (
                requested != self.allowed_permissions[key]
            ):
                raise ValueError(
                    f"Skill {skill_name} permission exceeds configured scope: {key}"
                )
        return SkillPermissions(filesystem, extra)


def _required_text(raw: dict[str, Any], key: str, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Skill {key} is required: {path}")
    return value.strip()


def _normalize_root(root: str | Path) -> Path:
    """Convert a configured Skill root to a stable absolute path."""
    expanded = os.path.expandvars(str(root))
    return Path(expanded).expanduser().resolve()


def _text_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"Skill {field_name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)
