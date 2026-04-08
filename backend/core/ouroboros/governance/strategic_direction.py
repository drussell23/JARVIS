"""StrategicDirectionService — gives Ouroboros awareness of the developer's vision.

Reads the Manifesto (README.md) and architecture docs on boot, distills them
into a reusable strategic context digest (~2000 tokens), and provides it to
every operation context so the organism generates code aligned with the
developer's architectural direction.

Boundary Principle (Manifesto §4 — The Synthetic Soul):
  Deterministic: File reading, section extraction, digest caching.
  Agentic: How the provider uses the strategic context during generation.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Documents to read, in priority order. Paths relative to project root.
_STRATEGIC_DOCS = [
    ("README.md", "manifesto"),
    ("docs/architecture/OUROBOROS.md", "pipeline"),
    ("docs/architecture/BRAIN_ROUTING.md", "routing"),
]

# Max chars to read from each doc (avoid blowing up prompt budget)
_MAX_DOC_CHARS = 20_000


class StrategicDirectionService:
    """Reads the developer's strategic vision and injects it into operations.

    On boot, reads the Manifesto and architecture docs, extracts the core
    principles and direction, and caches a digest for injection into every
    operation's ``strategic_memory_prompt``.

    Parameters
    ----------
    project_root:
        Repository root directory.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root.resolve()
        self._principles: List[str] = []
        self._digest: str = ""
        self._loaded: bool = False

    async def load(self) -> None:
        """Read strategic docs and build the cached digest."""
        sections: List[str] = []

        # Extract Manifesto principles from README.md
        readme_path = self._root / "README.md"
        if readme_path.exists():
            try:
                content = readme_path.read_text(encoding="utf-8", errors="replace")
                self._principles = self._extract_principles(content)
                manifesto = self._extract_manifesto(content)
                if manifesto:
                    sections.append(manifesto)
            except Exception:
                logger.debug("[Strategic] Failed to read README.md", exc_info=True)

        # Read architecture docs
        for rel_path, label in _STRATEGIC_DOCS[1:]:
            doc_path = self._root / rel_path
            if doc_path.exists():
                try:
                    content = doc_path.read_text(encoding="utf-8", errors="replace")
                    overview = self._extract_overview(content, max_chars=3000)
                    if overview:
                        sections.append(f"## {label.title()} Architecture\n\n{overview}")
                except Exception:
                    logger.debug("[Strategic] Failed to read %s", rel_path, exc_info=True)

        # Build the digest
        self._digest = self._build_digest(sections)
        self._loaded = True
        logger.info(
            "[Strategic] Loaded: %d principles, %d char digest from %d sources",
            len(self._principles), len(self._digest), len(sections),
        )

    @property
    def digest(self) -> str:
        """Cached strategic context digest (~2000 tokens)."""
        return self._digest

    @property
    def principles(self) -> List[str]:
        """The Manifesto's 7 principles as a list."""
        return list(self._principles)

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def format_for_prompt(self) -> str:
        """Format the digest for injection into strategic_memory_prompt."""
        if not self._digest:
            return ""
        return (
            "## Strategic Direction (Manifesto v4)\n\n"
            "You are generating code for the JARVIS Trinity AI Ecosystem — "
            "an autonomous, self-evolving AI Operating System. Every change "
            "must align with these principles:\n\n"
            f"{self._digest}\n\n"
            "MANDATE: Structural repair, not patches. No brute-force retries "
            "without diagnosis. No hardcoded routing. If a subsystem fails, "
            "dismantle the flawed assumption and rebuild — do not bypass."
        )

    # ------------------------------------------------------------------
    # Internal extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_principles(readme_content: str) -> List[str]:
        """Extract the 7 numbered principles from the README manifesto."""
        principles: List[str] = []
        # Match patterns like "**1. The unified organism (tri-partite microkernel)**"
        pattern = re.compile(
            r"\*\*(\d+)\.\s+([^*]+)\*\*",
            re.MULTILINE,
        )
        for match in pattern.finditer(readme_content[:_MAX_DOC_CHARS]):
            num = match.group(1)
            title = match.group(2).strip()
            if 1 <= int(num) <= 7:
                principles.append(f"{num}. {title}")
        return principles[:7]

    @staticmethod
    def _extract_manifesto(readme_content: str) -> str:
        """Extract the manifesto section from README.md."""
        # Find the manifesto header and extract ~2000 chars
        markers = [
            "## Symbiotic AI-Native Manifesto",
            "### The seven principles",
            "**1. The unified organism",
        ]
        start = -1
        for marker in markers:
            idx = readme_content.find(marker)
            if idx >= 0:
                start = idx
                break

        if start < 0:
            return ""

        # Extract from start to the zero-shortcut mandate (end of principles)
        end_markers = [
            "### Five core execution contexts",
            "### The zero-shortcut mandate",
            "## ",  # next h2
        ]
        end = len(readme_content)
        for em in end_markers:
            idx = readme_content.find(em, start + 100)
            if idx > start:
                end = min(end, idx)

        section = readme_content[start:end].strip()
        # Cap at ~4000 chars to stay within prompt budget
        if len(section) > 4000:
            section = section[:4000] + "\n\n[... truncated for prompt budget]"
        return section

    @staticmethod
    def _extract_overview(content: str, max_chars: int = 3000) -> str:
        """Extract the Overview section from an architecture doc."""
        # Find ## Overview
        idx = content.find("## Overview")
        if idx < 0:
            # Try first 3000 chars as fallback
            return content[:max_chars].strip() if len(content) > 100 else ""

        # Find next ## section
        next_h2 = content.find("\n## ", idx + 20)
        if next_h2 < 0:
            section = content[idx:idx + max_chars]
        else:
            section = content[idx:min(next_h2, idx + max_chars)]

        return section.strip()

    def _build_digest(self, sections: List[str]) -> str:
        """Combine sections into a single digest."""
        if not sections:
            return ""

        parts: List[str] = []

        # Principles first (always)
        if self._principles:
            parts.append("### Core Principles\n")
            for p in self._principles:
                parts.append(f"- {p}")
            parts.append("")

        # Architecture sections
        for section in sections:
            # Trim each section to avoid bloat
            trimmed = section[:3000]
            if len(section) > 3000:
                trimmed += "\n[... see full doc for details]"
            parts.append(trimmed)
            parts.append("")

        return "\n".join(parts)
