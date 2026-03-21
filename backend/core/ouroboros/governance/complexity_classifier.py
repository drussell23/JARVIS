"""OperationComplexityClassifier — routes operations by complexity + persistence.

Combines three classification dimensions into a single CLASSIFY-phase decision:

1. **Complexity**: TRIVIAL / SIMPLE / MODERATE / COMPLEX / ARCHITECTURAL
   - Determines pipeline path (fast-path vs full pipeline)
   - Informs brain selection (lightweight vs heavy model)

2. **Persistence**: EPHEMERAL / PERSISTENT / EXISTING
   - EPHEMERAL: one-shot task, sandbox, delete after execution
   - PERSISTENT: recurring need, propose for permanent addition
   - EXISTING: capability already active in TopologyMap, just route to it

3. **Auto-approve eligibility**: Whether this operation can skip human APPROVE gate
   - Only TRIVIAL/SIMPLE + EPHEMERAL operations are candidates
   - ARCHITECTURAL operations NEVER auto-approve
   - Requires graduation gate (100 consecutive auto-approved with zero rollbacks)

Uses TopologyMap for capability gap detection and DurableLedger for
historical frequency analysis. No LLM dependency — pure heuristic classification.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ComplexityClass(str, Enum):
    TRIVIAL = "trivial"           # typo fix, comment, import change
    SIMPLE = "simple"             # single-file bug fix, config change
    MODERATE = "moderate"         # multi-file change, refactor
    COMPLEX = "complex"           # cross-module change, new feature
    ARCHITECTURAL = "architectural"  # new capability, system design change


class PersistenceClass(str, Enum):
    EPHEMERAL = "ephemeral"    # one-shot, sandbox, delete after use
    PERSISTENT = "persistent"  # recurring need, add to codebase permanently
    EXISTING = "existing"      # capability already active, just route to it


@dataclass(frozen=True)
class ClassificationResult:
    """Output of the OperationComplexityClassifier."""
    complexity: ComplexityClass
    persistence: PersistenceClass
    auto_approve_eligible: bool
    fast_path_eligible: bool
    rationale: str
    matched_capability: Optional[str]   # TopologyMap node name if EXISTING
    frequency_count: int                # historical occurrences of similar ops


# ---------------------------------------------------------------------------
# Heuristic patterns for complexity classification
# ---------------------------------------------------------------------------

# File patterns that indicate trivial changes
_TRIVIAL_PATTERNS = frozenset({
    r"\.md$", r"\.txt$", r"\.yaml$", r"\.yml$", r"\.json$",
    r"\.gitignore$", r"\.env\.example$", r"requirements.*\.txt$",
})

# Single-line change indicators in descriptions
_TRIVIAL_KEYWORDS = frozenset({
    "typo", "comment", "docstring", "import", "rename",
    "whitespace", "formatting", "lint",
})

# Architectural indicators
_ARCHITECTURAL_KEYWORDS = frozenset({
    "new capability", "new module", "new service", "architecture",
    "design", "protocol", "schema", "migration", "breaking change",
})

# Minimum frequency for PERSISTENT classification
_PERSISTENCE_FREQUENCY_THRESHOLD = 3


class OperationComplexityClassifier:
    """Classifies operations by complexity and persistence at CLASSIFY phase.

    Injected into the orchestrator. Called before ROUTE to stamp
    complexity_class and persistence_class on the OperationContext.
    """

    def __init__(
        self,
        topology: Any = None,    # TopologyMap
        ledger: Any = None,      # OperationLedger
    ) -> None:
        self._topology = topology
        self._ledger = ledger

    def classify(
        self,
        description: str,
        target_files: List[str],
        op_history: Optional[List[Any]] = None,
    ) -> ClassificationResult:
        """Classify an operation's complexity, persistence, and auto-approve eligibility.

        Args:
            description: Human-readable operation description
            target_files: List of file paths being modified
            op_history: Optional list of recent LedgerEntries for frequency analysis
        """
        complexity = self._classify_complexity(description, target_files)
        persistence, matched_cap, freq = self._classify_persistence(
            description, target_files, op_history,
        )
        auto_approve = self._check_auto_approve(complexity, persistence)
        fast_path = complexity in (ComplexityClass.TRIVIAL, ComplexityClass.SIMPLE)

        rationale = (
            f"Complexity={complexity.value} (files={len(target_files)}, "
            f"description_signals={self._extract_signals(description)}). "
            f"Persistence={persistence.value} "
            f"(matched_cap={matched_cap or 'none'}, freq={freq}). "
            f"Auto-approve={'yes' if auto_approve else 'no'}, "
            f"fast-path={'yes' if fast_path else 'no'}."
        )

        return ClassificationResult(
            complexity=complexity,
            persistence=persistence,
            auto_approve_eligible=auto_approve,
            fast_path_eligible=fast_path,
            rationale=rationale,
            matched_capability=matched_cap,
            frequency_count=freq,
        )

    def _classify_complexity(
        self, description: str, target_files: List[str],
    ) -> ComplexityClass:
        """Determine operation complexity from file count + description signals."""
        desc_lower = description.lower()
        file_count = len(target_files)

        # Check for architectural indicators first (highest priority)
        if any(kw in desc_lower for kw in _ARCHITECTURAL_KEYWORDS):
            return ComplexityClass.ARCHITECTURAL

        # Check for trivial indicators
        if file_count <= 1:
            if any(kw in desc_lower for kw in _TRIVIAL_KEYWORDS):
                return ComplexityClass.TRIVIAL
            if target_files and any(
                re.search(pat, target_files[0]) for pat in _TRIVIAL_PATTERNS
            ):
                return ComplexityClass.TRIVIAL

        # Classify by file count
        if file_count <= 1:
            return ComplexityClass.SIMPLE
        if file_count <= 3:
            return ComplexityClass.MODERATE
        return ComplexityClass.COMPLEX

    def _classify_persistence(
        self,
        description: str,
        target_files: List[str],
        op_history: Optional[List[Any]],
    ) -> Tuple[PersistenceClass, Optional[str], int]:
        """Determine if the operation is ephemeral, persistent, or existing.

        Returns (persistence_class, matched_capability_name, frequency_count).
        """
        # Check TopologyMap for existing capability
        if self._topology is not None:
            matched = self._match_topology(description, target_files)
            if matched:
                return PersistenceClass.EXISTING, matched, 0

        # Check historical frequency in ledger
        freq = self._count_similar_ops(description, op_history)
        if freq >= _PERSISTENCE_FREQUENCY_THRESHOLD:
            return PersistenceClass.PERSISTENT, None, freq

        return PersistenceClass.EPHEMERAL, None, freq

    def _match_topology(
        self, description: str, target_files: List[str],
    ) -> Optional[str]:
        """Check if any active capability in TopologyMap matches this operation."""
        if self._topology is None:
            return None

        desc_lower = description.lower()
        for name, node in self._topology.nodes.items():
            if node.active and (
                name.replace("_", " ") in desc_lower
                or node.domain in desc_lower
                or any(name in f.lower() for f in target_files)
            ):
                return name
        return None

    def _count_similar_ops(
        self, description: str, op_history: Optional[List[Any]],
    ) -> int:
        """Count how many historical operations have similar descriptions."""
        if not op_history:
            return 0

        desc_words = set(description.lower().split())
        count = 0
        for entry in op_history:
            entry_desc = str(entry.data.get("description", "")).lower()
            entry_words = set(entry_desc.split())
            # Jaccard similarity > 0.3 = "similar enough"
            intersection = desc_words & entry_words
            union = desc_words | entry_words
            if union and len(intersection) / len(union) > 0.3:
                count += 1
        return count

    @staticmethod
    def _check_auto_approve(
        complexity: ComplexityClass, persistence: PersistenceClass,
    ) -> bool:
        """Determine auto-approve eligibility. Conservative by default."""
        if complexity == ComplexityClass.ARCHITECTURAL:
            return False  # NEVER auto-approve architectural changes
        if persistence == PersistenceClass.PERSISTENT:
            return False  # New permanent capabilities need human review
        if complexity in (ComplexityClass.TRIVIAL, ComplexityClass.SIMPLE):
            return True   # Low-risk ephemeral/existing operations
        return False

    @staticmethod
    def _extract_signals(description: str) -> str:
        """Extract classification-relevant keywords from description."""
        desc_lower = description.lower()
        signals = []
        for kw in _TRIVIAL_KEYWORDS:
            if kw in desc_lower:
                signals.append(f"+{kw}")
        for kw in _ARCHITECTURAL_KEYWORDS:
            if kw in desc_lower:
                signals.append(f"+ARCH:{kw}")
        return ",".join(signals) if signals else "none"
