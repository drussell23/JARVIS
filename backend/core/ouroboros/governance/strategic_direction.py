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
from typing import Dict, List, Optional, Sequence, Tuple

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
# GoalTracker — dynamic goal-directed prioritization (P2.4 + Week 2 hardening)
# ---------------------------------------------------------------------------
#
# The goal tracker stores user-defined strategic goals, scores them for
# relevance to the current operation, and injects only the relevant ones
# into the generation prompt. Every tunable is env-driven — nothing is
# hardcoded above the default level — and the schema is versioned so
# future migrations don't break existing ``.jarvis/active_goals.json``
# files written by earlier sessions.

import enum

_GOAL_FILE = ".jarvis/active_goals.json"
_GOAL_SCHEMA_VERSION = 2  # v1 = pre-Week-2; v2 adds status/tags/due/updated_at


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _env_set(name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    """Parse a comma-separated env var into a tuple of lowercase strings."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parts or default


_MAX_GOALS = _env_int("JARVIS_MAX_ACTIVE_GOALS", 5)

# Priority boost for goal-aligned signals (subtracted from priority score,
# so lower = higher priority). Moderate boost — doesn't override urgency
# or source type, but breaks ties in favor of goal-aligned work.
_GOAL_ALIGNMENT_BOOST = _env_int("JARVIS_GOAL_ALIGNMENT_BOOST", 2)

# Score-strength scaling for the v2 alignment path. The raw relevance score
# (``find_relevant``) is divided by ``_GOAL_SCORE_DIVISOR`` to map the path-
# overlap space (~10-50+) onto a 1.0-3.0 multiplier range that scales the
# base boost. Operators can tune all three without code changes so the
# intake router reacts more (or less) aggressively to high-confidence goal
# matches. Clamp bounds keep one monster-score goal from starving every
# other signal in the priority queue.
_GOAL_SCORE_DIVISOR = _env_float("JARVIS_GOAL_SCORE_DIVISOR", 10.0)
_GOAL_SCORE_MULT_MIN = _env_float("JARVIS_GOAL_SCORE_MULT_MIN", 1.0)
_GOAL_SCORE_MULT_MAX = _env_float("JARVIS_GOAL_SCORE_MULT_MAX", 3.0)

# Relevance scoring weights — tuneable via env so operators can bias the
# injection pipeline without code changes. Higher = stronger signal.
_SCORE_PATH_MATCH = _env_float("JARVIS_GOAL_SCORE_PATH", 10.0)
_SCORE_TAG_MATCH = _env_float("JARVIS_GOAL_SCORE_TAG", 6.0)
_SCORE_KEYWORD_MATCH = _env_float("JARVIS_GOAL_SCORE_KEYWORD", 4.0)
_SCORE_MIN_RELEVANCE = _env_float("JARVIS_GOAL_MIN_RELEVANCE", 1.0)

# Staleness decay — goals created N days ago lose half their score every
# ``JARVIS_GOAL_HALFLIFE_DAYS`` days. Set to 0 to disable.
_STALENESS_HALFLIFE_DAYS = _env_float("JARVIS_GOAL_HALFLIFE_DAYS", 14.0)

# How many top-scoring goals surface in format_for_prompt() for a given op.
# Separate from _MAX_GOALS which caps *total* stored goals.
_MAX_PROMPT_GOALS = _env_int("JARVIS_GOAL_PROMPT_MAX", 3)

# Default stopwords for auto-keyword extraction. Every word <4 chars is
# already dropped; this list removes common words that survive the length
# filter but carry no semantic weight.
_DEFAULT_STOPWORDS = _env_set(
    "JARVIS_GOAL_STOPWORDS",
    (
        "with", "from", "that", "this", "have", "been", "what", "when",
        "where", "which", "will", "would", "could", "should", "their",
        "there", "into", "some", "such", "than", "then", "they", "them",
        "about", "after", "again", "also", "because", "before", "between",
    ),
)


class GoalStatus(enum.Enum):
    """Lifecycle states for an ActiveGoal."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"

    @classmethod
    def from_str(cls, raw: str) -> "GoalStatus":
        """Parse a status from its serialized form, defaulting to ACTIVE."""
        if not raw:
            return cls.ACTIVE
        try:
            return cls(raw.strip().lower())
        except ValueError:
            return cls.ACTIVE


@dataclass
class ActiveGoal:
    """A user-defined strategic goal that influences O+V prioritization.

    The schema is backward-compatible with the v1 (P2.4) format:
    ``status``, ``tags``, ``due_at``, and ``updated_at`` default when the
    persisted file doesn't contain them.
    """

    goal_id: str                 # Short slug: "test-coverage", "reduce-governance-complexity"
    description: str             # Human-readable: "Improve test coverage in governance/"
    keywords: Tuple[str, ...]    # Matching keywords: ("test", "coverage", "pytest")
    path_patterns: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()            # Semantic labels: ("reliability", "sprint-2")
    priority_weight: float = 1.0
    status: GoalStatus = GoalStatus.ACTIVE
    due_at: Optional[float] = None        # Unix epoch seconds, None = open-ended
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def is_active(self) -> bool:
        return self.status is GoalStatus.ACTIVE

    def touch(self) -> None:
        """Bump the updated_at timestamp without mutating anything else."""
        self.updated_at = time.time()


@dataclass(frozen=True)
class GoalAlignment:
    """Rich alignment result for an intake signal.

    Returned by :meth:`GoalTracker.alignment_context`. Carries both the
    final priority ``boost`` (the legacy ``alignment_boost`` int) *and*
    the raw signals that produced it so downstream phases can trace *why*
    a signal was prioritized — the intake router stashes this on
    ``IntentEnvelope.evidence`` and Zone 6.8 surfaces it in postmortems.

    Fields
    ------
    boost:
        Priority score units to subtract from the queue key. ``0`` when
        no goal cleared the relevance threshold. Always non-negative.
    raw_score:
        The untouched relevance score of the top matching goal, as
        produced by :meth:`GoalTracker.find_relevant`. Before scaling by
        ``score_multiplier``. Zero when no match.
    top_goal_id:
        ``goal_id`` of the highest-scoring goal, or empty string if none.
    matched_count:
        How many distinct goals cleared ``_SCORE_MIN_RELEVANCE``. Useful
        for traceability — a signal hitting 3 goals is structurally
        stronger than one hitting a single goal even at equal raw score.
    score_multiplier:
        The clamped ``raw_score / _GOAL_SCORE_DIVISOR`` scalar applied
        on top of ``_GOAL_ALIGNMENT_BOOST * priority_weight``. Always in
        ``[_GOAL_SCORE_MULT_MIN, _GOAL_SCORE_MULT_MAX]``. Defaults to
        ``_GOAL_SCORE_MULT_MIN`` on a no-match so arithmetic callers
        don't need to branch on ``matched_count``.
    """

    boost: int = 0
    raw_score: float = 0.0
    top_goal_id: str = ""
    matched_count: int = 0
    score_multiplier: float = field(default=_GOAL_SCORE_MULT_MIN)

    @property
    def is_match(self) -> bool:
        return self.matched_count > 0

    def as_evidence(self) -> Dict[str, float]:
        """Serialize for ``IntentEnvelope.evidence`` stashing."""
        return {
            "goal_alignment_boost": float(self.boost),
            "goal_relevance_score": round(self.raw_score, 3),
            "goal_top_goal_id": self.top_goal_id,  # type: ignore[dict-item]
            "goal_matched_count": float(self.matched_count),
            "goal_score_multiplier": round(self.score_multiplier, 3),
        }


class GoalTracker:
    """Tracks user-defined strategic goals with relevance-scored injection.

    Storage
    -------
    Goals are persisted to ``.jarvis/active_goals.json`` with a versioned
    schema (``_GOAL_SCHEMA_VERSION``). v1 files load transparently; v2
    adds status/tags/due_at/updated_at.

    Relevance scoring
    -----------------
    When an operation enters CONTEXT_EXPANSION, the orchestrator calls
    :meth:`format_for_prompt` with the op's ``target_files`` and
    ``description``. The tracker scores each *active* goal against those
    signals and injects the top-N matches — not the whole set — so noisy
    goals don't hijack the generation prompt.

    Scoring signals (highest→lowest weight):
      * Path overlap (``_SCORE_PATH_MATCH``, default 10)
      * Tag match against description tokens (``_SCORE_TAG_MATCH``, 6)
      * Keyword match against description (``_SCORE_KEYWORD_MATCH``, 4)
      * priority_weight multiplier applied to the combined score
      * Staleness decay (half-life ``_STALENESS_HALFLIFE_DAYS``, default 14)

    Boundary Principle (Manifesto §5):
      Deterministic: Keyword/path/tag matching, staleness arithmetic.
      Agentic: How the model interprets goal context during generation.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root.resolve()
        self._goals: List[ActiveGoal] = []
        self._load()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @property
    def all_goals(self) -> List[ActiveGoal]:
        """Every goal, regardless of status."""
        return list(self._goals)

    @property
    def active_goals(self) -> List[ActiveGoal]:
        """Only goals with ``GoalStatus.ACTIVE`` — the v1 API surface."""
        return [g for g in self._goals if g.is_active]

    def goals_by_status(self, status: GoalStatus) -> List[ActiveGoal]:
        return [g for g in self._goals if g.status is status]

    def get(self, goal_id: str) -> Optional[ActiveGoal]:
        for g in self._goals:
            if g.goal_id == goal_id:
                return g
        return None

    # ------------------------------------------------------------------
    # Mutate
    # ------------------------------------------------------------------

    def set_goals(self, goals: List[ActiveGoal]) -> None:
        """Replace all active goals. Persists to disk."""
        self._goals = list(goals)[:_MAX_GOALS]
        self._persist()
        logger.info(
            "[GoalTracker] Set %d goals: %s",
            len(self._goals),
            ", ".join(g.goal_id for g in self._goals),
        )

    def add_goal(self, goal: ActiveGoal) -> None:
        """Add a goal. If at capacity, drops the oldest *inactive* first;
        if none are inactive, drops the oldest active.

        Deduped by ``goal_id`` — adding the same id upserts, preserving
        created_at but bumping updated_at.
        """
        existing = self.get(goal.goal_id)
        if existing is not None:
            # Upsert — preserve created_at, bump updated_at.
            goal.created_at = existing.created_at
            goal.updated_at = time.time()
            self._goals = [g for g in self._goals if g.goal_id != goal.goal_id]

        self._goals.append(goal)

        if len(self._goals) > _MAX_GOALS:
            # Prefer dropping a non-active goal over an active one.
            inactives = [g for g in self._goals if not g.is_active]
            if inactives:
                dropped = inactives[0]
                self._goals.remove(dropped)
            else:
                dropped = self._goals.pop(0)
            logger.info("[GoalTracker] Dropped goal at capacity: %s", dropped.goal_id)

        self._persist()

    def remove_goal(self, goal_id: str) -> bool:
        """Remove a goal by ID. Returns True if found."""
        before = len(self._goals)
        self._goals = [g for g in self._goals if g.goal_id != goal_id]
        if len(self._goals) < before:
            self._persist()
            return True
        return False

    def set_status(self, goal_id: str, status: GoalStatus) -> bool:
        """Update a goal's lifecycle status. Returns True if found."""
        goal = self.get(goal_id)
        if goal is None:
            return False
        goal.status = status
        goal.touch()
        self._persist()
        logger.info(
            "[GoalTracker] %s → %s", goal_id, status.value,
        )
        return True

    def pause(self, goal_id: str) -> bool:
        return self.set_status(goal_id, GoalStatus.PAUSED)

    def resume(self, goal_id: str) -> bool:
        return self.set_status(goal_id, GoalStatus.ACTIVE)

    def complete(self, goal_id: str) -> bool:
        return self.set_status(goal_id, GoalStatus.COMPLETED)

    def purge_completed(self) -> int:
        """Remove all completed goals. Returns count removed."""
        before = len(self._goals)
        self._goals = [
            g for g in self._goals if g.status is not GoalStatus.COMPLETED
        ]
        removed = before - len(self._goals)
        if removed:
            self._persist()
            logger.info("[GoalTracker] Purged %d completed goals", removed)
        return removed

    # ------------------------------------------------------------------
    # Relevance scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> set:
        """Split free-text into lowercase word tokens for tag matching."""
        if not text:
            return set()
        # Split on any non-alphanumeric, drop empties and <4-char words.
        return {
            w for w in re.split(r"[^a-z0-9_]+", text.lower())
            if len(w) >= 4
        }

    @staticmethod
    def _staleness_multiplier(created_at: float, halflife_days: float) -> float:
        """Exponential decay: score *= 0.5 ** (age_days / halflife_days)."""
        if halflife_days <= 0 or created_at <= 0:
            return 1.0
        age_s = max(0.0, time.time() - created_at)
        age_days = age_s / 86400.0
        if age_days <= 0:
            return 1.0
        return 0.5 ** (age_days / halflife_days)

    @staticmethod
    def _score_goal(
        goal: ActiveGoal,
        *,
        description: str,
        target_files: Sequence[str],
        halflife_days: float = _STALENESS_HALFLIFE_DAYS,
    ) -> float:
        """Return a non-negative relevance score for ``goal`` against the op.

        Scores combine path overlap (strongest), tag match, and keyword
        match, multiplied by priority_weight and staleness decay. Inactive
        goals always score 0.
        """
        if not goal.is_active:
            return 0.0

        score = 0.0
        desc_lower = (description or "").lower()
        desc_tokens = GoalTracker._tokenize(description or "")

        # Path overlap — any target file starting with any goal path pattern
        if target_files and goal.path_patterns:
            for tf in target_files:
                tf_str = str(tf)
                for pat in goal.path_patterns:
                    if pat and tf_str.startswith(pat):
                        score += _SCORE_PATH_MATCH
                        break

        # Tag match — goal tags that appear as tokens in the description
        if goal.tags and desc_tokens:
            tag_set = {t.lower() for t in goal.tags if t}
            overlap = tag_set & desc_tokens
            if overlap:
                score += _SCORE_TAG_MATCH * len(overlap)

        # Keyword match — keyword substring hits in description
        if goal.keywords and desc_lower:
            for kw in goal.keywords:
                if kw and kw.lower() in desc_lower:
                    score += _SCORE_KEYWORD_MATCH

        if score <= 0:
            return 0.0

        # Priority weight multiplier + staleness decay.
        score *= max(0.1, goal.priority_weight)
        score *= GoalTracker._staleness_multiplier(goal.created_at, halflife_days)
        return score

    def find_relevant(
        self,
        *,
        description: str = "",
        target_files: Sequence[str] = (),
        limit: Optional[int] = None,
    ) -> List[Tuple[ActiveGoal, float]]:
        """Return the top-N active goals by relevance score, descending.

        ``limit`` defaults to ``_MAX_PROMPT_GOALS``. Goals scoring below
        ``_SCORE_MIN_RELEVANCE`` are dropped entirely — no partial matches.
        """
        if limit is None:
            limit = _MAX_PROMPT_GOALS
        scored: List[Tuple[ActiveGoal, float]] = []
        for goal in self._goals:
            s = self._score_goal(
                goal,
                description=description,
                target_files=target_files,
            )
            if s >= _SCORE_MIN_RELEVANCE:
                scored.append((goal, s))
        scored.sort(key=lambda gs: (-gs[1], -gs[0].updated_at))
        return scored[:limit]

    # ------------------------------------------------------------------
    # Intake router alignment boost (v1 API, preserved)
    # ------------------------------------------------------------------

    def alignment_context(
        self,
        description: str,
        target_files: Sequence[str] = (),
    ) -> "GoalAlignment":
        """Rich alignment result — exposes raw score + top goal + boost.

        Supersedes :meth:`alignment_boost`. Returns a :class:`GoalAlignment`
        instead of a bare int, so the intake router can stash diagnostics
        on every envelope (``evidence.goal_relevance_score`` etc.) and
        priority math can reflect match *strength*, not just match/no-match.

        The boost scales with three factors, in order of operator-tunable
        strength:
          1. Base constant (``_GOAL_ALIGNMENT_BOOST``)
          2. Top goal's ``priority_weight``
          3. Clamped raw-score multiplier (``raw_score / _GOAL_SCORE_DIVISOR``)
        """
        matches = self.find_relevant(
            description=description,
            target_files=target_files,
            limit=_MAX_PROMPT_GOALS,
        )
        if not matches:
            return GoalAlignment()

        best_goal, best_score = matches[0]

        # Raw-score → clamped multiplier. Higher-scoring matches push the
        # boost up within the operator-defined band without letting any
        # single goal starve every other signal in the queue.
        if _GOAL_SCORE_DIVISOR <= 0:
            mult = _GOAL_SCORE_MULT_MIN
        else:
            mult = best_score / _GOAL_SCORE_DIVISOR
        mult = max(_GOAL_SCORE_MULT_MIN, min(_GOAL_SCORE_MULT_MAX, mult))

        scaled = _GOAL_ALIGNMENT_BOOST * max(0.1, best_goal.priority_weight) * mult
        boost = max(1, int(round(scaled)))

        return GoalAlignment(
            boost=boost,
            raw_score=float(best_score),
            top_goal_id=best_goal.goal_id,
            matched_count=len(matches),
            score_multiplier=float(mult),
        )

    def alignment_boost(
        self,
        description: str,
        target_files: Sequence[str] = (),
    ) -> int:
        """Compute priority boost for a signal based on goal alignment.

        Returns a non-negative integer to subtract from priority score
        (lower = higher priority). Returns 0 if no goals match above the
        minimum relevance threshold.

        Thin compat wrapper around :meth:`alignment_context` — prefer the
        context method when you also need the raw relevance score or the
        matched goal ID for observability.
        """
        return self.alignment_context(description, target_files).boost

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def format_for_prompt(
        self,
        *,
        target_files: Sequence[str] = (),
        description: str = "",
    ) -> str:
        """Format the most relevant active goals for the generation prompt.

        When called with no scoping args, falls back to "show every active
        goal" behavior (v1 compatible). When either signal is present,
        the output is scoped to the top-N matches by relevance score,
        filtering out goals that score below the minimum threshold —
        noisy goals are never injected into an unrelated op's prompt.
        """
        actives = self.active_goals
        if not actives:
            return ""

        if description or target_files:
            relevant = self.find_relevant(
                description=description,
                target_files=target_files,
            )
            if not relevant:
                return ""
            goals_to_render: List[Tuple[ActiveGoal, float]] = relevant
        else:
            goals_to_render = [(g, 0.0) for g in actives[:_MAX_PROMPT_GOALS]]

        lines = [
            "## Active Goals (user-defined priorities)\n",
            "Align your changes with these current objectives:\n",
        ]
        for goal, score in goals_to_render:
            weight_tag = ""
            if goal.priority_weight >= 2.0:
                weight_tag = " [HIGH PRIORITY]"
            elif goal.priority_weight <= 0.5:
                weight_tag = " [low priority]"
            score_tag = f" [relevance={score:.1f}]" if score > 0 else ""
            paths = ""
            if goal.path_patterns:
                paths = f" (focus: {', '.join(goal.path_patterns[:3])})"
            tags = ""
            if goal.tags:
                tags = f" #{' #'.join(goal.tags[:4])}"
            lines.append(
                f"- **{goal.goal_id}**: {goal.description}{paths}{tags}"
                f"{weight_tag}{score_tag}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Keyword / slug helpers
    # ------------------------------------------------------------------

    @staticmethod
    def extract_keywords(
        description: str,
        *,
        limit: int = 8,
        min_len: int = 4,
        stopwords: Tuple[str, ...] = _DEFAULT_STOPWORDS,
    ) -> Tuple[str, ...]:
        """Auto-extract keywords from a goal description.

        Lowercase, min-length filtered, stopword filtered. Used by the
        REPL ``/goal add`` handler so the user doesn't have to think up
        keywords manually, and centralised here so the stopword list
        isn't duplicated across call sites.
        """
        if not description:
            return ()
        stop = set(stopwords)
        words: List[str] = []
        seen: set = set()
        for raw in re.split(r"[^a-zA-Z0-9_]+", description):
            w = raw.lower()
            if len(w) < min_len or w in stop or w in seen:
                continue
            seen.add(w)
            words.append(w)
            if len(words) >= limit:
                break
        return tuple(words)

    @staticmethod
    def slugify(description: str, *, max_len: int = 40) -> str:
        """Turn a free-text description into a lowercase slug id."""
        if not description:
            return "goal"
        s = re.sub(r"[^a-z0-9]+", "-", description.lower())[:max_len].strip("-")
        return s or "goal"

    # ------------------------------------------------------------------
    # Persistence (v1-compatible loader, v2 writer)
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        try:
            path = self._root / _GOAL_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "schema_version": _GOAL_SCHEMA_VERSION,
                "goals": [
                    {
                        "goal_id": g.goal_id,
                        "description": g.description,
                        "keywords": list(g.keywords),
                        "path_patterns": list(g.path_patterns),
                        "tags": list(g.tags),
                        "priority_weight": g.priority_weight,
                        "status": g.status.value,
                        "due_at": g.due_at,
                        "created_at": g.created_at,
                        "updated_at": g.updated_at,
                    }
                    for g in self._goals
                ],
            }
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.debug("[GoalTracker] Persist failed", exc_info=True)

    def _load(self) -> None:
        try:
            path = self._root / _GOAL_FILE
            if not path.exists():
                return
            raw = json.loads(path.read_text())

            # v1 format was a bare list; v2 is a dict with schema_version.
            if isinstance(raw, list):
                entries = raw
            elif isinstance(raw, dict):
                entries = raw.get("goals", [])
            else:
                return

            goals: List[ActiveGoal] = []
            for g in entries[:_MAX_GOALS]:
                if not isinstance(g, dict):
                    continue
                goals.append(ActiveGoal(
                    goal_id=str(g.get("goal_id", "")),
                    description=str(g.get("description", "")),
                    keywords=tuple(g.get("keywords", ())),
                    path_patterns=tuple(g.get("path_patterns", ())),
                    tags=tuple(g.get("tags", ())),
                    priority_weight=float(g.get("priority_weight", 1.0)),
                    status=GoalStatus.from_str(g.get("status", "active")),
                    due_at=g.get("due_at"),
                    created_at=float(g.get("created_at", 0.0)),
                    updated_at=float(g.get("updated_at", g.get("created_at", 0.0))),
                ))
            self._goals = [g for g in goals if g.goal_id]

            if self._goals:
                logger.info(
                    "[GoalTracker] Loaded %d goals: %s",
                    len(self._goals),
                    ", ".join(g.goal_id for g in self._goals),
                )
        except Exception:
            logger.debug("[GoalTracker] Load failed", exc_info=True)
