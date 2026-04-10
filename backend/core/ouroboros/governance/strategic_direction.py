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

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Documents to read, in priority order. Paths relative to project root.
_STRATEGIC_DOCS = [
    ("README.md", "manifesto"),
    ("docs/architecture/OUROBOROS.md", "pipeline"),
    ("docs/architecture/BRAIN_ROUTING.md", "routing"),
]

# Max chars to read from each doc (avoid blowing up prompt budget)
_MAX_DOC_CHARS = 20_000

# Git-history direction inference (Manifesto §4 — synthetic soul).
_GIT_HISTORY_ENABLED = os.environ.get(
    "JARVIS_STRATEGIC_GIT_HISTORY_ENABLED", "true"
).lower() not in ("false", "0", "no", "off")
_GIT_HISTORY_MAX_COMMITS = int(
    os.environ.get("JARVIS_STRATEGIC_GIT_MAX_COMMITS", "50")
)
# Regex for Conventional Commits: `type(scope): subject`. Scope is optional.
_CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?:\s*(?P<subject>.+)$"
)


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
        self._git_themes: List[str] = []
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

        # Infer recent development momentum from git history (Manifesto §4
        # — synthetic soul: the organism remembers what it has been working
        # on). Conventional-commit parsing, zero model inference.
        if _GIT_HISTORY_ENABLED:
            self._git_themes = self._extract_git_themes(
                self._root, max_commits=_GIT_HISTORY_MAX_COMMITS,
            )
            momentum_section = self._format_git_themes(self._git_themes)
            if momentum_section:
                sections.append(momentum_section)

        # Build the digest
        self._digest = self._build_digest(sections)
        self._loaded = True
        logger.info(
            "[Strategic] Loaded: %d principles, %d git themes, %d char digest from %d sources",
            len(self._principles), len(self._git_themes), len(self._digest), len(sections),
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
    def _extract_git_themes(
        project_root: Path,
        max_commits: int = 50,
    ) -> List[str]:
        """Infer active development themes from recent git history.

        Runs ``git log`` for the last ``max_commits`` commits and parses
        Conventional Commit subjects to build:
          • a histogram of the most touched scopes (the "(governance)"
            / "(sensors)" tags — reveal where the momentum is)
          • a histogram of commit types (``feat`` / ``fix`` / ``refactor``
            — reveal whether we're building or hardening)
          • the three most recent subject lines (reveal freshest work)

        Returns a list of short theme strings. Empty list on any failure
        (no git, shallow clone, subprocess timeout). Zero model inference.

        Manifesto §4 rationale: the synthetic soul remembers what it has
        been working on — this turns raw git history into explicit context
        the organism can reason over during GENERATE.
        """
        try:
            import subprocess
            result = subprocess.run(
                ["git", "log", f"-{int(max_commits)}", "--pretty=format:%s"],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return []
        if result.returncode != 0:
            return []

        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if not lines:
            return []

        scope_counts: Dict[str, int] = {}
        type_counts: Dict[str, int] = {}
        subjects: List[str] = []
        for line in lines:
            m = _CONVENTIONAL_COMMIT_RE.match(line)
            if not m:
                # Non-conventional commit (e.g., "Merge branch ..."): still
                # record the first ~60 chars as a raw subject so we never
                # drop information completely.
                subjects.append(line[:60])
                continue
            t = (m.group("type") or "").lower()
            s = (m.group("scope") or "").lower()
            sub = (m.group("subject") or "").strip()
            if t:
                type_counts[t] = type_counts.get(t, 0) + 1
            if s:
                scope_counts[s] = scope_counts.get(s, 0) + 1
            if sub:
                subjects.append(sub[:60])

        themes: List[str] = []

        if scope_counts:
            top_scopes = sorted(
                scope_counts.items(), key=lambda kv: (-kv[1], kv[0])
            )[:5]
            themes.append(
                "Active scopes: "
                + ", ".join(f"{name} ({count})" for name, count in top_scopes)
            )

        if type_counts:
            top_types = sorted(
                type_counts.items(), key=lambda kv: (-kv[1], kv[0])
            )[:4]
            themes.append(
                "Commit mix: "
                + ", ".join(f"{name}={count}" for name, count in top_types)
            )

        if subjects:
            themes.append("Latest work: " + " | ".join(subjects[:3]))

        return themes

    @staticmethod
    def _format_git_themes(themes: List[str]) -> str:
        """Render git-derived themes as a digest section. Empty → empty."""
        if not themes:
            return ""
        body = "\n".join(f"- {t}" for t in themes)
        return (
            "## Recent Development Momentum\n\n"
            "Derived deterministically from the last commits via Conventional "
            "Commit parsing. Treat this as *where the organism has been focused* "
            "— a hint about current themes, not a mandate to repeat past work.\n\n"
            f"{body}"
        )

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


# ---------------------------------------------------------------------------
# GoalTracker — dynamic goal-directed prioritization (P2.4)
# ---------------------------------------------------------------------------

_GOAL_FILE = ".jarvis/active_goals.json"
_MAX_GOALS = int(os.environ.get("JARVIS_MAX_ACTIVE_GOALS", "5"))

# Priority boost for goal-aligned signals (subtracted from priority score,
# so lower = higher priority).  Moderate boost — doesn't override urgency
# or source type, but breaks ties in favor of goal-aligned work.
_GOAL_ALIGNMENT_BOOST = int(os.environ.get("JARVIS_GOAL_ALIGNMENT_BOOST", "2"))


@dataclass
class ActiveGoal:
    """A user-defined strategic goal that influences O+V prioritization."""
    goal_id: str                 # Short slug: "test-coverage", "reduce-governance-complexity"
    description: str             # Human-readable: "Improve test coverage in governance/"
    keywords: Tuple[str, ...]    # Matching keywords: ("test", "coverage", "pytest")
    path_patterns: Tuple[str, ...] = ()  # File path prefixes: ("backend/core/ouroboros/governance/",)
    priority_weight: float = 1.0  # 0.5 = mild preference, 1.0 = standard, 2.0 = strong focus
    created_at: float = field(default_factory=time.time)


class GoalTracker:
    """Tracks 3-5 active user goals and provides alignment scoring.

    Goals are persisted to ``.jarvis/active_goals.json`` so they survive
    across sessions.  The intake router queries ``alignment_boost()`` to
    bias priority toward goal-aligned signals.  The prompt builders query
    ``format_for_prompt()`` to inject goal context into generation.

    Boundary Principle (Manifesto §5):
      Deterministic: Keyword/path matching, priority arithmetic.
      Agentic: How the model interprets goal context during generation.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root.resolve()
        self._goals: List[ActiveGoal] = []
        self._load()

    @property
    def active_goals(self) -> List[ActiveGoal]:
        return list(self._goals)

    def set_goals(self, goals: List[ActiveGoal]) -> None:
        """Replace all active goals. Persists to disk."""
        self._goals = goals[:_MAX_GOALS]
        self._persist()
        logger.info(
            "[GoalTracker] Set %d active goals: %s",
            len(self._goals),
            ", ".join(g.goal_id for g in self._goals),
        )

    def add_goal(self, goal: ActiveGoal) -> None:
        """Add a goal. If at capacity, drops the oldest."""
        # Deduplicate by goal_id
        self._goals = [g for g in self._goals if g.goal_id != goal.goal_id]
        self._goals.append(goal)
        if len(self._goals) > _MAX_GOALS:
            dropped = self._goals.pop(0)
            logger.info("[GoalTracker] Dropped oldest goal: %s", dropped.goal_id)
        self._persist()

    def remove_goal(self, goal_id: str) -> bool:
        """Remove a goal by ID. Returns True if found."""
        before = len(self._goals)
        self._goals = [g for g in self._goals if g.goal_id != goal_id]
        if len(self._goals) < before:
            self._persist()
            return True
        return False

    def alignment_boost(
        self,
        description: str,
        target_files: Sequence[str] = (),
    ) -> int:
        """Compute priority boost for a signal based on goal alignment.

        Returns a non-negative integer to subtract from priority score
        (lower = higher priority).  Returns 0 if no goals match.

        Matching logic:
        - Keyword match: any goal keyword appears in the description
        - Path match: any target file starts with a goal path pattern
        - Both must match at least one goal to earn the boost
        """
        if not self._goals:
            return 0

        desc_lower = description.lower()
        best_weight = 0.0

        for goal in self._goals:
            matched = False
            # Keyword matching
            for kw in goal.keywords:
                if kw.lower() in desc_lower:
                    matched = True
                    break
            # Path pattern matching (supplement keyword match)
            if not matched and target_files and goal.path_patterns:
                for tf in target_files:
                    for pat in goal.path_patterns:
                        if tf.startswith(pat):
                            matched = True
                            break
                    if matched:
                        break
            if matched and goal.priority_weight > best_weight:
                best_weight = goal.priority_weight

        if best_weight <= 0:
            return 0
        return max(1, int(_GOAL_ALIGNMENT_BOOST * best_weight))

    def format_for_prompt(self) -> str:
        """Format active goals for injection into generation prompts.

        Returns a compact (~100-150 token) block that communicates the
        user's current priorities so the model makes aligned decisions
        about what to fix, how to approach the work, and what to test.
        """
        if not self._goals:
            return ""
        lines = [
            "## Active Goals (user-defined priorities)\n",
            "Align your changes with these current objectives:\n",
        ]
        for g in self._goals:
            weight_tag = ""
            if g.priority_weight >= 2.0:
                weight_tag = " [HIGH PRIORITY]"
            elif g.priority_weight <= 0.5:
                weight_tag = " [low priority]"
            paths = ""
            if g.path_patterns:
                paths = f" (focus: {', '.join(g.path_patterns[:3])})"
            lines.append(f"- **{g.goal_id}**: {g.description}{paths}{weight_tag}")
        return "\n".join(lines)

    def _persist(self) -> None:
        try:
            path = self._root / _GOAL_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "goal_id": g.goal_id,
                    "description": g.description,
                    "keywords": list(g.keywords),
                    "path_patterns": list(g.path_patterns),
                    "priority_weight": g.priority_weight,
                    "created_at": g.created_at,
                }
                for g in self._goals
            ]
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.debug("[GoalTracker] Persist failed", exc_info=True)

    def _load(self) -> None:
        try:
            path = self._root / _GOAL_FILE
            if not path.exists():
                return
            data = json.loads(path.read_text())
            self._goals = [
                ActiveGoal(
                    goal_id=g["goal_id"],
                    description=g["description"],
                    keywords=tuple(g.get("keywords", ())),
                    path_patterns=tuple(g.get("path_patterns", ())),
                    priority_weight=g.get("priority_weight", 1.0),
                    created_at=g.get("created_at", 0.0),
                )
                for g in data[:_MAX_GOALS]
            ]
            if self._goals:
                logger.info(
                    "[GoalTracker] Loaded %d active goals: %s",
                    len(self._goals),
                    ", ".join(g.goal_id for g in self._goals),
                )
        except Exception:
            logger.debug("[GoalTracker] Load failed", exc_info=True)
