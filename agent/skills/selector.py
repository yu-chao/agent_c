from __future__ import annotations

import re
from collections.abc import Iterable

from .models import LoadedSkill


class SkillSelector:
    def __init__(self, max_active: int = 3):
        if max_active < 0:
            raise ValueError("max_active must not be negative")
        self.max_active = max_active

    def select(
        self, request: str, skills: Iterable[LoadedSkill]
    ) -> tuple[LoadedSkill, ...]:
        normalized = request.casefold()
        request_tokens = set(_tokens(normalized))
        ranked: list[tuple[int, str, LoadedSkill]] = []
        for skill in skills:
            keywords = skill.manifest.activation.keywords
            keyword_score = sum(
                10 for keyword in keywords if keyword.casefold() in normalized
            )
            description_tokens = set(
                _tokens(
                    f"{skill.manifest.name} {skill.manifest.description}".casefold()
                )
            )
            score = keyword_score + len(request_tokens & description_tokens)
            if score:
                ranked.append((score, skill.manifest.name, skill))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked[:self.max_active])


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[\w-]+", value, flags=re.UNICODE))
