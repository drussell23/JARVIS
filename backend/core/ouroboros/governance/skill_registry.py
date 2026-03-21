# backend/core/ouroboros/governance/skill_registry.py
"""SkillRegistry — loads domain-specific instruction files from .jarvis/skills/*.yaml.

GAP 4: matches operation target files against per-skill filePattern globs.
Matching skills have their instructions concatenated and returned for injection
into OperationContext.human_instructions via ContextExpander.

YAML schema (each .jarvis/skills/<name>.yaml):
    name: migration_safety
    filePattern: "migrations/**"
    instructions: |
      Always create the migration in a transaction.
      Never drop columns in the same migration that removes all usages.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Skill:
    name: str
    file_pattern: str
    instructions: str


class SkillRegistry:
    """Loads and matches domain skills against operation target files.

    Parameters
    ----------
    repo_root:
        Root of the repository; skills are loaded from
        ``<repo_root>/.jarvis/skills/*.yaml``.
    """

    def __init__(self, repo_root: Path) -> None:
        self._skills: Tuple[_Skill, ...] = tuple(
            self._load_skills(Path(repo_root) / ".jarvis" / "skills")
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(self, file_paths: Sequence[str]) -> str:
        """Return concatenated instructions for all skills that match any of the target files.

        Returns empty string when no skills match.
        """
        matched: List[str] = []
        for skill in self._skills:
            if any(self._matches(fp, skill.file_pattern) for fp in file_paths):
                matched.append(f"### Skill: {skill.name}\n\n{skill.instructions.strip()}")
        return "\n\n".join(matched)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(file_path: str, pattern: str) -> bool:
        """fnmatch-based glob matching (supports *, ?, ** via path normalization)."""
        # Normalise separators to forward slash for consistent matching
        fp = file_path.replace("\\", "/")
        return fnmatch.fnmatch(fp, pattern) or fnmatch.fnmatch(fp.split("/")[-1], pattern)

    @staticmethod
    def _load_skills(skills_dir: Path) -> List[_Skill]:
        if not skills_dir.is_dir():
            return []
        try:
            import yaml  # type: ignore[import]
        except ImportError:
            logger.warning("[SkillRegistry] PyYAML not installed — skills disabled")
            return []

        skills = []
        for path in sorted(skills_dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                name = data.get("name", "")
                pattern = data.get("filePattern", "")
                instructions = data.get("instructions", "")
                if not (name and pattern and instructions):
                    logger.debug("[SkillRegistry] Skipping incomplete skill: %s", path.name)
                    continue
                skills.append(_Skill(name=str(name), file_pattern=str(pattern), instructions=str(instructions)))
                logger.debug("[SkillRegistry] Loaded skill '%s' (pattern: %s)", name, pattern)
            except Exception as exc:
                logger.warning("[SkillRegistry] Skipping malformed skill %s: %s", path.name, exc)
        return skills
