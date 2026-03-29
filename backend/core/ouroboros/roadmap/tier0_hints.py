"""
Tier 0 Deterministic Gap Hints
================================

Zero-token gap detection that cross-references P0 spec/plan/backlog fragment
content against the Oracle symbol graph.

No model calls are made.  Every hint carries ``provenance="deterministic"``
and ``confidence_rule_id="spec_symbol_miss"``.

Usage::

    from backend.core.ouroboros.roadmap.tier0_hints import generate_tier0_hints

    hints = generate_tier0_hints(snapshot, oracle)
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from .hypothesis import FeatureHypothesis
from .snapshot import RoadmapSnapshot, SnapshotFragment


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fragment types scanned for capability references.
_TARGET_FRAGMENT_TYPES: FrozenSet[str] = frozenset({"spec", "plan", "backlog"})

#: Common English words that are never capability names.
_STOPWORDS: FrozenSet[str] = frozenset({
    "the", "this", "that", "these", "those",
    "some", "any", "new", "old", "each", "all",
    "for", "and", "but", "not", "with", "from",
    "into", "onto", "upon", "its", "our", "your",
    "their", "both", "one", "two", "per",
})

#: Minimum character length for a capability name to be considered.
_MIN_CAP_NAME_LEN: int = 3

#: Regex patterns mapping to capability type labels.
#: Each tuple is (compiled_pattern, cap_type_label).
_CAP_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(\w+)\s+agent\b", re.IGNORECASE), "agent"),
    (re.compile(r"\b(\w+)\s+sensor\b", re.IGNORECASE), "sensor"),
    (re.compile(r"\b(\w+)\s+integration\b", re.IGNORECASE), "integration"),
    (re.compile(r"\b(\w+)\s+provider\b", re.IGNORECASE), "provider"),
)

#: Confidence assigned to every deterministic hint.
_CONFIDENCE: float = 0.85

#: Rule identifier embedded in every deterministic hint.
_CONFIDENCE_RULE_ID: str = "spec_symbol_miss"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_tier0_hints(
    snapshot: RoadmapSnapshot,
    oracle: Optional[Any],
) -> List[FeatureHypothesis]:
    """Generate deterministic gap hints from P0 fragments.

    Parameters
    ----------
    snapshot:
        Current roadmap snapshot.  Only tier-0 fragments with
        ``fragment_type`` in ``{"spec", "plan", "backlog"}`` are examined.
    oracle:
        Object exposing ``find_nodes_by_name(name: str, fuzzy: bool) -> List``.
        When ``None``, returns an empty list immediately (zero oracle calls).

    Returns
    -------
    List[FeatureHypothesis]
        One hypothesis per unique ``(cap_name, cap_type)`` pair that has no
        corresponding symbol in the Oracle.  Empty when oracle is ``None``
        or no gaps are detected.
    """
    if oracle is None:
        return []

    # Pass 1: extract capability references from each eligible fragment.
    # Map (cap_name_lower, cap_type) → set of source_ids that mentioned it.
    cap_to_sources: Dict[Tuple[str, str], Set[str]] = {}

    for fragment in snapshot.fragments:
        if fragment.tier != 0:
            continue
        if fragment.fragment_type not in _TARGET_FRAGMENT_TYPES:
            continue

        refs = _extract_capability_refs(fragment.summary)
        for cap_name, cap_type in refs:
            key = (cap_name, cap_type)
            cap_to_sources.setdefault(key, set()).add(fragment.source_id)

    # Pass 2: cross-reference oracle; emit hypothesis when symbol is missing.
    hints: List[FeatureHypothesis] = []
    snapshot_hash = snapshot.content_hash
    now = time.time()

    for (cap_name, cap_type), source_ids in cap_to_sources.items():
        nodes = oracle.find_nodes_by_name(cap_name, fuzzy=True)
        if nodes:
            continue  # symbol exists — no gap

        evidence = tuple(sorted(source_ids))
        description = f"Missing {cap_type}: {cap_name}"
        synth_fp = _synthesis_fingerprint(cap_name, cap_type, snapshot_hash)

        hint = FeatureHypothesis.new(
            description=description,
            evidence_fragments=evidence,
            gap_type="missing_capability",
            confidence=_CONFIDENCE,
            confidence_rule_id=_CONFIDENCE_RULE_ID,
            urgency="medium",
            suggested_scope="new-agent",
            suggested_repos=(),
            provenance="deterministic",
            synthesized_for_snapshot_hash=snapshot_hash,
            synthesis_input_fingerprint=synth_fp,
            synthesized_at=now,
        )
        hints.append(hint)

    return hints


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_capability_refs(text: str) -> Set[Tuple[str, str]]:
    """Return a set of ``(cap_name_lower, cap_type)`` pairs found in *text*.

    Applies all patterns in :data:`_CAP_PATTERNS`, filters stopwords and
    short tokens, and deduplicates by ``(cap_name_lower, cap_type)``.
    """
    refs: Set[Tuple[str, str]] = set()
    for pattern, cap_type in _CAP_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1)
            normalized = raw.lower().strip()
            if len(normalized) < _MIN_CAP_NAME_LEN:
                continue
            if normalized in _STOPWORDS:
                continue
            refs.add((normalized, cap_type))
    return refs


def _synthesis_fingerprint(cap_name: str, cap_type: str, snapshot_hash: str) -> str:
    """Return a short fingerprint for the synthesis inputs of one hint."""
    payload = f"{cap_name}\t{cap_type}\t{snapshot_hash}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]
