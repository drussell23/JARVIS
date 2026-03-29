"""
FeatureHypothesis & Fingerprinting
=====================================

A ``FeatureHypothesis`` represents a single synthesized insight about a gap
in the codebase — a missing capability, incomplete wiring, stale
implementation, or manifesto violation.

Design principles:
- Fingerprinting ignores UUID and timestamps so that the same logical
  hypothesis generated at different times produces the same fingerprint.
  This allows the hypothesis cache to deduplicate without re-running
  synthesis.
- Staleness is OR-based: a hypothesis is stale when *either* the snapshot
  hash changed *or* the age exceeds the TTL.  This ensures hypothesis
  freshness tracks both content drift and clock-based expiry.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Fingerprint helpers
# ---------------------------------------------------------------------------

def compute_hypothesis_fingerprint(
    description: str,
    evidence_fragments: Tuple[str, ...],
    gap_type: str,
) -> str:
    """Return a 32-character hex fingerprint for a hypothesis.

    Formula::

        sha256(f"{normalized_desc}\\t{sorted_evidence}\\t{gap_type}")[:32]

    ``normalized_desc`` is lowercased and whitespace-collapsed so that
    trivial reformatting does not produce a new fingerprint.

    ``sorted_evidence`` is a comma-joined sorted tuple of source IDs so
    that evidence insertion order does not matter.

    UUID fields and timestamps are deliberately excluded so that the
    fingerprint is stable for deduplication across synthesis runs.
    """
    normalized_desc = " ".join(description.lower().split())
    sorted_evidence = ",".join(sorted(evidence_fragments))
    payload = f"{normalized_desc}\t{sorted_evidence}\t{gap_type}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# FeatureHypothesis
# ---------------------------------------------------------------------------

_VALID_GAP_TYPES: frozenset = frozenset({
    "missing_capability",
    "incomplete_wiring",
    "stale_implementation",
    "manifesto_violation",
})

_VALID_PROVENANCE: frozenset = frozenset({
    "deterministic",
    "model:doubleword-397b",
    "model:claude",
})


@dataclass
class FeatureHypothesis:
    """A synthesized hypothesis about a codebase gap.

    Parameters
    ----------
    hypothesis_id:
        UUID-based unique storage key.  Generated via ``uuid.uuid4()`` when
        not supplied.

    description:
        Human-readable description of the gap or opportunity.

    evidence_fragments:
        ``source_id`` values from :class:`~roadmap.snapshot.SnapshotFragment`
        objects that support this hypothesis.

    gap_type:
        Category of the gap.  Must be one of:
        ``"missing_capability"``, ``"incomplete_wiring"``,
        ``"stale_implementation"``, ``"manifesto_violation"``.

    confidence:
        Float in ``[0, 1]``.  Higher is more certain.

    confidence_rule_id:
        Identifier of the rule or heuristic that produced the confidence
        score, e.g. ``"tier0-spec-vs-impl-diff"`` or
        ``"model:doubleword-397b:chain-of-thought"``.

    urgency:
        Qualitative urgency label, e.g. ``"critical"``, ``"high"``,
        ``"medium"``, ``"low"``.

    suggested_scope:
        Short label indicating what kind of change is suggested, e.g.
        ``"new-agent"``, ``"wire-existing"``, ``"refactor"``.

    suggested_repos:
        Tuple of repository names that should be touched.

    provenance:
        How the hypothesis was generated.  Must start with
        ``"deterministic"`` or ``"model:"``.

    synthesized_for_snapshot_hash:
        ``content_hash`` of the :class:`~roadmap.snapshot.RoadmapSnapshot`
        this hypothesis was synthesized against.

    synthesized_at:
        UTC epoch seconds when this hypothesis was produced.

    synthesis_input_fingerprint:
        Fingerprint of the *input* data fed to the synthesis model/rule, so
        that callers can detect whether the synthesis needs to be re-run even
        if the snapshot hash is the same.

    status:
        Lifecycle status.  Defaults to ``"active"``.  Other values:
        ``"dismissed"``, ``"promoted"``, ``"completed"``.

    hypothesis_fingerprint:
        Computed in ``__post_init__`` via
        :func:`compute_hypothesis_fingerprint`.  Read-only after init.
    """

    hypothesis_id: str
    description: str
    evidence_fragments: Tuple[str, ...]
    gap_type: str
    confidence: float
    confidence_rule_id: str
    urgency: str
    suggested_scope: str
    suggested_repos: Tuple[str, ...]
    provenance: str
    synthesized_for_snapshot_hash: str
    synthesized_at: float
    synthesis_input_fingerprint: str
    status: str = "active"
    hypothesis_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        # Validate gap_type
        if self.gap_type not in _VALID_GAP_TYPES:
            raise ValueError(
                f"gap_type must be one of {sorted(_VALID_GAP_TYPES)}, "
                f"got {self.gap_type!r}"
            )

        # Validate provenance prefix
        if not (
            self.provenance == "deterministic"
            or self.provenance.startswith("model:")
        ):
            raise ValueError(
                f"provenance must be 'deterministic' or start with 'model:', "
                f"got {self.provenance!r}"
            )

        # Validate confidence range
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence must be in [0, 1], got {self.confidence!r}"
            )

        # Compute fingerprint (bypasses frozen restriction — field is not frozen)
        object.__setattr__(
            self,
            "hypothesis_fingerprint",
            compute_hypothesis_fingerprint(
                self.description,
                self.evidence_fragments,
                self.gap_type,
            ),
        )

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def new(
        cls,
        description: str,
        evidence_fragments: Tuple[str, ...],
        gap_type: str,
        confidence: float,
        confidence_rule_id: str,
        urgency: str,
        suggested_scope: str,
        suggested_repos: Tuple[str, ...],
        provenance: str,
        synthesized_for_snapshot_hash: str,
        synthesis_input_fingerprint: str,
        synthesized_at: Optional[float] = None,
    ) -> "FeatureHypothesis":
        """Convenience factory that auto-generates a UUID for ``hypothesis_id``."""
        return cls(
            hypothesis_id=str(uuid.uuid4()),
            description=description,
            evidence_fragments=evidence_fragments,
            gap_type=gap_type,
            confidence=confidence,
            confidence_rule_id=confidence_rule_id,
            urgency=urgency,
            suggested_scope=suggested_scope,
            suggested_repos=suggested_repos,
            provenance=provenance,
            synthesized_for_snapshot_hash=synthesized_for_snapshot_hash,
            synthesized_at=synthesized_at if synthesized_at is not None else time.time(),
            synthesis_input_fingerprint=synthesis_input_fingerprint,
        )

    # ------------------------------------------------------------------
    # Staleness check
    # ------------------------------------------------------------------

    def is_stale(self, current_snapshot_hash: str, ttl_s: float) -> bool:
        """Return ``True`` if this hypothesis should be re-synthesized.

        A hypothesis is stale when **either**:
        - The snapshot hash changed (content drift), OR
        - The age exceeds *ttl_s* seconds (time-based expiry).

        The OR semantics ensure freshness even when content is unchanged but
        the hypothesis is very old.
        """
        hash_mismatch = self.synthesized_for_snapshot_hash != current_snapshot_hash
        age_exceeded = (time.time() - self.synthesized_at) > ttl_s
        return hash_mismatch or age_exceeded
