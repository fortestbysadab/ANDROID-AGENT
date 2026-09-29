"""Trusted local skill loader with lightweight progressive disclosure."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    skill_id: str
    description: str
    triggers: tuple[str, ...]
    instructions: str
    always: bool = False


class SkillRouter:
    """Load trusted, version-controlled skills and select relevant guidance.

    Skills add procedural context only. They cannot add tools or permissions.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.skills = self._load()

    @classmethod
    def bundled(cls) -> "SkillRouter":
        return cls(Path(__file__).with_name("bundled"))

    def instructions_for(self, user_text: str) -> str:
        lowered = user_text.casefold()
        selected = [
            skill
            for skill in self.skills
            if skill.always or any(trigger.casefold() in lowered for trigger in skill.triggers)
        ]
        return "\n\n".join(
            f"## Skill: {skill.skill_id}\n{skill.instructions.strip()}" for skill in selected
        )

    def _load(self) -> tuple[Skill, ...]:
        loaded = []
        if not self.root.is_dir():
            return ()
        for directory in sorted(path for path in self.root.iterdir() if path.is_dir()):
            manifest_path = directory / "skill.json"
            instructions_path = directory / "SKILL.md"
            if not manifest_path.is_file() or not instructions_path.is_file():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            loaded.append(
                Skill(
                    skill_id=str(manifest["id"]),
                    description=str(manifest["description"]),
                    triggers=tuple(str(item) for item in manifest.get("triggers", [])),
                    instructions=instructions_path.read_text(encoding="utf-8"),
                    always=bool(manifest.get("always", False)),
                )
            )
        return tuple(sorted(loaded, key=lambda skill: (not skill.always, skill.skill_id)))
