# backend/core/ouroboros/governance/context_memory_loader.py
"""
ContextMemoryLoader — 3-tier OUROBOROS.md hierarchy reader.

Loads human-authored instructions from up to 3 sources (global, project,
local) and merges them into a single string for injection into generation
prompts. All levels are optional; missing files are silently skipped.

Priority order (all merge, not override):
  1. ~/.jarvis/OUROBOROS.md          — global personal defaults
  2. <repo>/OUROBOROS.md             — project constraints (committed)
  3. <repo>/.jarvis/OUROBOROS.md     — local personal overrides (gitignored)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_FILENAME = "OUROBOROS.md"


class ContextMemoryLoader:
    """Reads and merges up to 3 OUROBOROS.md files into a single instruction block."""

    def __init__(
        self,
        project_root: Path,
        global_dir: Optional[Path] = None,
    ) -> None:
        self._project_root = Path(project_root)
        self._global_dir = (
            Path(global_dir) if global_dir is not None else Path.home() / ".jarvis"
        )

    def load(self) -> str:
        """Return merged instruction text from all available OUROBOROS.md files."""
        sections: list[str] = []
        paths = [
            (self._global_dir / _FILENAME, "global"),
            (self._project_root / _FILENAME, "project"),
            (self._project_root / ".jarvis" / _FILENAME, "local"),
        ]
        for path, label in paths:
            text = self._read_safe(path, label)
            if text:
                sections.append(text.strip())
        return "\n\n".join(sections)

    @staticmethod
    def _read_safe(path: Path, label: str) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as exc:
            logger.debug(
                "ContextMemoryLoader: could not read %s OUROBOROS.md at %s: %s",
                label,
                path,
                exc,
            )
            return ""
