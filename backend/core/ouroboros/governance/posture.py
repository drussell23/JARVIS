"""StrategicPosture — typed vocabulary for DirectionInferrer output.

Carries the current *disposition* of the organism — what kind of work it
most values right now — as a small deterministic enum plus a structured
reading that records the signal evidence behind the inference.

Authority invariant (same pattern as ConversationBridge / SemanticIndex /
LastSessionSummary): consumed only by advisory surfaces — StrategicDirection
prompt injection, IDE observability GET endpoints, `/posture` REPL. Zero
authority over Iron Gate, UrgencyRouter, risk-tier escalation, policy
engine, FORBIDDEN_PATH matching, ToolExecutor protected-path checks, or
approval gating.

Manifesto alignment:
  * §1 Boundary Principle — advisory disposition, not execution authority
  * §5 Tier 0 (Deterministic Fast-Path) — 4-value enum, deterministic math,
    no LLM in hot path
  * §8 Observability — every reading carries a hash of its input bundle +
    per-signal contribution evidence

Vocabulary (fixed, 4 values — severity lives in confidence, not in a 5th
posture; acute failure belongs to Tier 3 Nervous System Reflex / Iron
Gate, not a passive posture):

  * EXPLORE     — ship new capabilities, take risks, accept churn
  * CONSOLIDATE — close open threads, finish in-flight arcs
  * HARDEN      — stabilize before new features, tighten gates
  * MAINTAIN    — steady state / low-confidence fallback

Schema versioning:
  * ``PostureReading.schema_version`` = ``"1.0"``
  * ``SignalBundle.schema_version`` = ``"1.0"``
  v1 readers reading v2+ payloads must reject / cold-start rather than
  coerce. Slice 4 graduation pins this literal default.
"""
from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


SCHEMA_VERSION = "1.0"


class Posture(str, enum.Enum):
    """Four-value posture vocabulary. Inherits ``str`` so values serialize
    cleanly to JSON without a custom encoder."""

    EXPLORE = "EXPLORE"
    CONSOLIDATE = "CONSOLIDATE"
    HARDEN = "HARDEN"
    MAINTAIN = "MAINTAIN"

    @classmethod
    def all(cls) -> Tuple["Posture", ...]:
        return (cls.CONSOLIDATE, cls.EXPLORE, cls.HARDEN, cls.MAINTAIN)

    @classmethod
    def from_str(cls, value: str) -> "Posture":
        """Strict parse — raises ``ValueError`` on unknown value. Normalizes
        case so ``/posture override explore`` works as well as ``EXPLORE``."""
        normalized = str(value).strip().upper()
        for member in cls:
            if member.value == normalized:
                return member
        raise ValueError(
            f"Unknown posture {value!r}. Valid: {[m.value for m in cls.all()]}"
        )


@dataclass(frozen=True)
class SignalContribution:
    """One signal's contribution to the final posture score.

    ``normalized`` is the signal value after normalization into the
    weighted-sum domain. ``contribution_score`` is ``normalized * weight``
    for the winning posture (populated in evidence list attached to the
    top-scoring posture — not a per-posture fan-out).
    """

    signal_name: str
    raw_value: float
    normalized: float
    weight: float
    contributed_to: Posture
    contribution_score: float


@dataclass(frozen=True)
class SignalBundle:
    """Structured input to ``DirectionInferrer.infer()``.

    All 10 signals required — missing signals must be filled by the caller
    with documented baselines (typically 0.0), not left as ``None``, so the
    inference function stays pure.

    Signal definitions:
      * ``feat_ratio``        — fraction of last N commits typed ``feat:``
      * ``fix_ratio``         — fraction typed ``fix:``
      * ``refactor_ratio``    — fraction typed ``refactor:``
      * ``test_docs_ratio``   — combined ``test:`` + ``docs:`` fraction
      * ``postmortem_failure_rate`` — 0-1 over ``postmortem_window_h``
      * ``iron_gate_reject_rate``   — 0-1 over last 24h
      * ``l2_repair_rate``    — L2 invocations / total ops (24h)
      * ``open_ops_normalized``     — in-flight ops / pool size
      * ``session_lessons_infra_ratio`` — infra-tagged lessons / total
      * ``time_since_last_graduation_inv`` — 1 / (hours_since + 1),
        so 0 when stale; near 1 when fresh
      * ``cost_burn_normalized`` — 24h cost / daily budget cap (0-1)
      * ``worktree_orphan_count`` — integer; normalized internally

    Signals 11-12 (``cost_burn_normalized``, ``worktree_orphan_count``)
    ship in v1 per the 10-core-signal ruling from the design review —
    both are deterministically available at the same collection layer,
    so keeping them to maintain parity with the weight table.
    """

    feat_ratio: float
    fix_ratio: float
    refactor_ratio: float
    test_docs_ratio: float
    postmortem_failure_rate: float
    iron_gate_reject_rate: float
    l2_repair_rate: float
    open_ops_normalized: float
    session_lessons_infra_ratio: float
    time_since_last_graduation_inv: float
    cost_burn_normalized: float
    worktree_orphan_count: int
    commit_window: int = 50
    postmortem_window_h: int = 48
    schema_version: str = SCHEMA_VERSION

    def to_hashable(self) -> str:
        """Deterministic string suitable for hashing. Uses ``sorted`` keys
        so field-order changes don't invalidate hashes."""
        payload = {
            "feat_ratio": self.feat_ratio,
            "fix_ratio": self.fix_ratio,
            "refactor_ratio": self.refactor_ratio,
            "test_docs_ratio": self.test_docs_ratio,
            "postmortem_failure_rate": self.postmortem_failure_rate,
            "iron_gate_reject_rate": self.iron_gate_reject_rate,
            "l2_repair_rate": self.l2_repair_rate,
            "open_ops_normalized": self.open_ops_normalized,
            "session_lessons_infra_ratio": self.session_lessons_infra_ratio,
            "time_since_last_graduation_inv": self.time_since_last_graduation_inv,
            "cost_burn_normalized": self.cost_burn_normalized,
            "worktree_orphan_count": self.worktree_orphan_count,
            "commit_window": self.commit_window,
            "postmortem_window_h": self.postmortem_window_h,
            "schema_version": self.schema_version,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def hash(self) -> str:
        """sha256[:8] of ``to_hashable()`` — idempotence check for tests."""
        return hashlib.sha256(self.to_hashable().encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class PostureReading:
    """One inference result.

    ``evidence`` lists the top contributing signals for the winning
    posture, ordered by ``contribution_score`` descending.

    ``signal_bundle_hash`` is the sha256[:8] of the input bundle —
    two identical bundles produce readings with identical hashes, which
    lets tests assert pure-function behavior without equality-comparing
    every field of the reading itself (``inferred_at`` differs per call).

    ``arc_context`` (P0.5 Slice 2): optional ``ArcContextSignal`` carried
    through from the inferrer call. ``None`` when the caller didn't
    provide one (back-compat with all pre-Slice-2 callers). When present,
    the field is observability-only by default — score adjustment was
    applied iff ``JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED`` was on
    at infer time.
    """

    posture: Posture
    confidence: float
    evidence: Tuple[SignalContribution, ...]
    inferred_at: float
    signal_bundle_hash: str
    all_scores: Tuple[Tuple[Posture, float], ...]
    schema_version: str = SCHEMA_VERSION
    arc_context: Optional[Any] = None  # ArcContextSignal — typed as Any to avoid circular import

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "posture": self.posture.value,
            "confidence": self.confidence,
            "evidence": [
                {
                    "signal_name": c.signal_name,
                    "raw_value": c.raw_value,
                    "normalized": c.normalized,
                    "weight": c.weight,
                    "contributed_to": c.contributed_to.value,
                    "contribution_score": c.contribution_score,
                }
                for c in self.evidence
            ],
            "inferred_at": self.inferred_at,
            "signal_bundle_hash": self.signal_bundle_hash,
            "all_scores": [(p.value, s) for p, s in self.all_scores],
            "schema_version": self.schema_version,
        }
        if self.arc_context is not None:
            try:
                d["arc_context"] = self.arc_context.to_log_dict()
            except Exception:
                d["arc_context"] = None
        return d


def baseline_bundle() -> SignalBundle:
    """All signals at neutral — used as a test fixture base. Produces
    low-confidence MAINTAIN under default weights."""
    return SignalBundle(
        feat_ratio=0.0,
        fix_ratio=0.0,
        refactor_ratio=0.0,
        test_docs_ratio=0.0,
        postmortem_failure_rate=0.0,
        iron_gate_reject_rate=0.0,
        l2_repair_rate=0.0,
        open_ops_normalized=0.0,
        session_lessons_infra_ratio=0.0,
        time_since_last_graduation_inv=0.0,
        cost_burn_normalized=0.0,
        worktree_orphan_count=0,
    )


__all__ = [
    "Posture",
    "SignalContribution",
    "SignalBundle",
    "PostureReading",
    "SCHEMA_VERSION",
    "baseline_bundle",
]
