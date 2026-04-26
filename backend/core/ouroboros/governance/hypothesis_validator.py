"""P1.5 Slice 2 — Hypothesis post-op validator.

After an op proposed by the SelfGoalFormationEngine completes, this
module checks whether the paired Hypothesis's ``expected_outcome`` was
borne out by the actual outcome.

Validation strategy (deterministic, zero-LLM):
  Token-overlap matching — extract the salient tokens from
  ``expected_outcome`` (length-2+ alphanumeric, lowercased, stop-words
  removed) and check the overlap ratio against the ``actual_outcome``
  text. ``validated=True`` when overlap >= ``DEFAULT_OVERLAP_THRESHOLD``
  (0.5), ``False`` when overlap is ≤ 0.1, ``None`` (undecidable —
  ledger row records actual but leaves validated open) otherwise.

This is a deliberately conservative validator: the goal isn't to be
clever, it's to give the operator a measurable signal that future
slices (or a smarter validator) can refine. False negatives are fine
(operator can manually mark via REPL); false positives would be bad
(would silently claim a hypothesis was correct when it wasn't), so
the threshold is high enough that random word overlap won't cross it.

Authority invariants (PRD §12.2):
  * No banned imports (orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian).
  * Pure data + I/O via the HypothesisLedger primitive only.
  * Best-effort: malformed inputs return None / False, never raise.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Optional, Set

from backend.core.ouroboros.governance.hypothesis_ledger import (
    HypothesisLedger,
)

logger = logging.getLogger(__name__)


# Token-overlap thresholds — pinned by tests so behaviour stays
# reviewable. Conservative bias toward ``None`` (undecidable) over
# ``True`` (confirmed) to avoid false-positive validation claims.
DEFAULT_OVERLAP_THRESHOLD: float = 0.5
INVALIDATION_OVERLAP_THRESHOLD: float = 0.1


# Stop-words pruned before token-overlap math. Common English filler
# that would create false-positive matches across unrelated outcomes.
_STOP_WORDS: FrozenSet[str] = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "with", "by",
    "is", "was", "are", "were", "be", "been", "being",
    "and", "or", "not", "but", "if", "then", "than",
    "this", "that", "these", "those",
    "i", "we", "you", "they", "it",
    "do", "did", "done", "doing",
    "have", "has", "had",
    "will", "would", "should", "could", "may", "might",
    "what", "when", "where", "why", "how",
})

_TOKEN_RE: re.Pattern[str] = re.compile(r"[a-z0-9]{2,}")


def _tokenize(text: str) -> Set[str]:
    """Extract salient tokens. Lowercased, length-≥2, alphanumeric,
    stop-words removed."""
    if not text:
        return set()
    tokens = _TOKEN_RE.findall(text.lower())
    return {t for t in tokens if t not in _STOP_WORDS}


def overlap_ratio(expected: str, actual: str) -> float:
    """Return the Jaccard-ish overlap ratio between expected + actual
    tokens. ``|expected ∩ actual| / |expected|`` so an actual outcome
    that contains ALL expected tokens scores 1.0 even if it adds noise.

    Returns 0.0 when expected has no salient tokens (defensive: an
    empty hypothesis can never validate)."""
    expected_tokens = _tokenize(expected)
    if not expected_tokens:
        return 0.0
    actual_tokens = _tokenize(actual)
    matched = expected_tokens & actual_tokens
    return len(matched) / len(expected_tokens)


@dataclass(frozen=True)
class ValidationResult:
    """Structured result of a single hypothesis validation."""

    hypothesis_id: str
    validated: Optional[bool]
    overlap: float
    actual_outcome: str
    expected_outcome: str

    def is_decided(self) -> bool:
        return self.validated is not None


def classify(
    expected_outcome: str,
    actual_outcome: str,
    *,
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    invalidation_threshold: float = INVALIDATION_OVERLAP_THRESHOLD,
) -> Optional[bool]:
    """Pure function: classify ``actual`` against ``expected``.

    Returns:
      * ``True``  when overlap ≥ ``overlap_threshold``
      * ``False`` when overlap ≤ ``invalidation_threshold``
      * ``None`` when overlap falls in the undecidable middle band
    """
    o = overlap_ratio(expected_outcome, actual_outcome)
    if o >= overlap_threshold:
        return True
    if o <= invalidation_threshold:
        return False
    return None


def validate_hypothesis(
    hypothesis_id: str,
    actual_outcome: str,
    *,
    project_root: Optional[Path] = None,
    ledger: Optional[HypothesisLedger] = None,
    overlap_threshold: float = DEFAULT_OVERLAP_THRESHOLD,
    invalidation_threshold: float = INVALIDATION_OVERLAP_THRESHOLD,
) -> ValidationResult:
    """End-to-end: look up the hypothesis, classify against actual_outcome,
    record the result back to the ledger.

    Returns a ``ValidationResult`` with ``validated=None`` when the
    hypothesis_id wasn't found OR when the overlap fell in the
    undecidable middle band. The ledger still gets a record_outcome
    row in the latter case so operators can inspect — only the
    not-found case skips the write."""
    resolved_ledger = ledger
    if resolved_ledger is None:
        resolved_ledger = HypothesisLedger(
            project_root=project_root or Path.cwd(),
        )

    h = resolved_ledger.find_by_id(hypothesis_id)
    if h is None:
        logger.debug(
            "[HypothesisValidator] short_circuit reason=hypothesis_not_found "
            "id=%s", hypothesis_id,
        )
        return ValidationResult(
            hypothesis_id=hypothesis_id,
            validated=None,
            overlap=0.0,
            actual_outcome=actual_outcome,
            expected_outcome="",
        )

    o = overlap_ratio(h.expected_outcome, actual_outcome)
    if o >= overlap_threshold:
        decision: Optional[bool] = True
    elif o <= invalidation_threshold:
        decision = False
    else:
        decision = None

    resolved_ledger.record_outcome(
        hypothesis_id=h.hypothesis_id,
        actual_outcome=actual_outcome,
        validated=decision,
    )
    logger.info(
        "[HypothesisValidator] op=engine validated hypothesis id=%s "
        "decision=%s overlap=%.3f thresholds=%.2f/%.2f",
        h.hypothesis_id, decision, o,
        overlap_threshold, invalidation_threshold,
    )
    return ValidationResult(
        hypothesis_id=h.hypothesis_id,
        validated=decision,
        overlap=o,
        actual_outcome=actual_outcome,
        expected_outcome=h.expected_outcome,
    )


__all__ = [
    "DEFAULT_OVERLAP_THRESHOLD",
    "INVALIDATION_OVERLAP_THRESHOLD",
    "ValidationResult",
    "classify",
    "overlap_ratio",
    "validate_hypothesis",
]
