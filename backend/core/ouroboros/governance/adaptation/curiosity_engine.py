"""CuriosityEngine — autonomous hypothesis-generation primitive.

Per the post-Phase-8 brutal architectural review:

  > Auto-emits hypotheses from POSTMORTEM clusters → drops them
  > into HypothesisLedger → triggers Phase 7.6 probe runs at idle
  > GPU windows. With Phase 8.1 + 8.2, we can OBSERVE whether the
  > curiosity engine is actually producing useful hypotheses
  > (decision-trace shows the routing; confidence drift shows
  > whether the model is converging or oscillating).

This module ships the **front end of the Curiosity Primitive**:
the missing generator that closes the autonomous-curiosity loop.
The Phase 7.6 probe primitive (PR #23176) was a sharp tool waiting
for a wielder; this is the wielder.

## What it does

1. Reads POSTMORTEM clusters (from `postmortem_clusterer.cluster_postmortems`)
2. For each high-recurrence cluster, **synthesizes a falsifiable
   hypothesis claim** + expected outcome
3. Appends `Hypothesis` records to `HypothesisLedger`
4. Optionally triggers `HypothesisProbe.test()` on the most-recent
   open hypotheses (using Phase 7.6's runner with Item #3's
   production prober)

Each step is opt-in via a dedicated master flag; each step is
bounded; each step NEVER raises into the caller.

## Why a separate engine

`SelfGoalFormationEngine` (Phase 2 P1 Slice 2) consumes clusters →
emits `ProposalDraft`s → writes to backlog. That's a DIFFERENT
output channel: backlog entries are *operator-action items*.

CuriosityEngine consumes clusters → emits `Hypothesis` records →
writes to HypothesisLedger. The output channel is *self-formed
falsifiable claims* the system can probe **without operator
intervention** (read-only Venom subset; cost-bounded).

The two engines share the cluster INPUT but produce complementary
OUTPUTS. Both are bounded by `JARVIS_*_ENABLED` master flags so
the operator can wire one without the other.

## Cage rules (load-bearing)

  * **Master flag default false** (3 flags, all opt-in):
    - `JARVIS_CURIOSITY_ENGINE_ENABLED` (master)
    - `JARVIS_CURIOSITY_ENGINE_AUTO_PROBE` (run probes after generation)
    - `JARVIS_CURIOSITY_ENGINE_AUTO_BRIDGE` (route probe verdicts via
      Item #3 bridges to AdaptationLedger / HypothesisLedger.record_outcome)
  * **Per-cycle bounds**: at most `MAX_HYPOTHESES_PER_CYCLE=3`
    new hypotheses + `MAX_PROBES_PER_CYCLE=3` probe invocations
    per call. Operator-review surface stays trim.
  * **Cluster threshold**: only clusters with `member_count >=
    JARVIS_CURIOSITY_CLUSTER_THRESHOLD` (default 3) qualify —
    matches `SelfGoalFormationEngine` precedent.
  * **Cost-bounded probes**: Phase 7.6 runner already enforces
    `MAX_CALLS_PER_PROBE_DEFAULT=5` + `TIMEOUT_S_DEFAULT=30s` +
    diminishing-returns; Item #3 production prober adds `$0.05
    per call + $1.00 cumulative session budget`. CuriosityEngine
    inherits these bounds.
  * **Stdlib + adaptation/hypothesis_ledger only**. No
    orchestrator / phase-runner / scoped-tool-backend imports.
  * **NEVER raises** into caller (every error path is logged +
    converted to a structured CuriosityResult).

## Default-off

`JARVIS_CURIOSITY_ENGINE_ENABLED` (default false until graduation
cadence — tracked in Item #4's `CADENCE_POLICY` registry; see
`adaptation/graduation_ledger.py`).
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Hard cap on hypotheses generated per scan. Operator review surface
# stays trim.
MAX_HYPOTHESES_PER_CYCLE: int = 3

# Hard cap on probes invoked per scan. Each probe is bounded by
# Phase 7.6's per-call cap + per-session budget; this cap bounds
# the OUTER curiosity-cycle so a runaway scan can't burn through
# the whole session budget.
MAX_PROBES_PER_CYCLE: int = 3

# Min cluster member_count required to qualify as a curiosity
# candidate. Below this, the cluster is too noisy for an
# autonomous probe attempt.
DEFAULT_CLUSTER_THRESHOLD: int = 3

# Per-claim character cap (matches HypothesisLedger Hypothesis.claim
# bound).
MAX_CLAIM_CHARS: int = 500

# Per-expected-outcome cap (matches HypothesisLedger).
MAX_EXPECTED_OUTCOME_CHARS: int = 300


def is_engine_enabled() -> bool:
    """Master flag — ``JARVIS_CURIOSITY_ENGINE_ENABLED`` (default
    false until graduation)."""
    return os.environ.get(
        "JARVIS_CURIOSITY_ENGINE_ENABLED", "",
    ).strip().lower() in _TRUTHY


def is_auto_probe_enabled() -> bool:
    """Sub-flag — when on, ``run_cycle()`` invokes the Phase 7.6
    probe runner on each generated hypothesis. When off, hypotheses
    are written to the ledger but NOT probed (the probe is a
    follow-up operator action)."""
    return os.environ.get(
        "JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", "",
    ).strip().lower() in _TRUTHY


def is_auto_bridge_enabled() -> bool:
    """Sub-flag — when on AND auto-probe is on, terminal probe
    verdicts route through Item #3's bridges (CONFIRMED →
    AdaptationLedger; terminal → HypothesisLedger.record_outcome)."""
    return os.environ.get(
        "JARVIS_CURIOSITY_ENGINE_AUTO_BRIDGE", "",
    ).strip().lower() in _TRUTHY


def get_cluster_threshold() -> int:
    """Env-overridable cluster threshold —
    ``JARVIS_CURIOSITY_CLUSTER_THRESHOLD``."""
    raw = os.environ.get("JARVIS_CURIOSITY_CLUSTER_THRESHOLD")
    if raw is None:
        return DEFAULT_CLUSTER_THRESHOLD
    try:
        v = int(raw)
        return v if v >= 1 else DEFAULT_CLUSTER_THRESHOLD
    except ValueError:
        return DEFAULT_CLUSTER_THRESHOLD


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


class CuriosityStatus(str, enum.Enum):
    OK = "ok"
    SKIPPED_MASTER_OFF = "skipped_master_off"
    SKIPPED_NO_CLUSTERS = "skipped_no_clusters"
    SKIPPED_NO_QUALIFYING_CLUSTERS = "skipped_no_qualifying_clusters"
    LEDGER_WRITE_FAILED = "ledger_write_failed"


@dataclass(frozen=True)
class GeneratedHypothesis:
    """One hypothesis the engine synthesized. Mirrors
    `HypothesisLedger.Hypothesis` but pre-persistence."""

    op_id: str
    claim: str
    expected_outcome: str
    cluster_signature_hash: str
    created_unix: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": self.op_id,
            "claim": self.claim,
            "expected_outcome": self.expected_outcome,
            "cluster_signature_hash": self.cluster_signature_hash,
            "created_unix": self.created_unix,
        }


@dataclass(frozen=True)
class CuriosityResult:
    """Terminal result of one curiosity cycle. Frozen for audit
    trail durability."""

    status: CuriosityStatus
    hypotheses_generated: Tuple[GeneratedHypothesis, ...] = ()
    probes_run: int = 0
    bridge_results: Tuple[Any, ...] = ()
    detail: str = ""
    ts_epoch: float = 0.0

    @property
    def is_ok(self) -> bool:
        return self.status is CuriosityStatus.OK

    @property
    def is_skipped(self) -> bool:
        return self.status in (
            CuriosityStatus.SKIPPED_MASTER_OFF,
            CuriosityStatus.SKIPPED_NO_CLUSTERS,
            CuriosityStatus.SKIPPED_NO_QUALIFYING_CLUSTERS,
        )


# ---------------------------------------------------------------------------
# Hypothesis synthesis
# ---------------------------------------------------------------------------


def _synthesize_claim(
    failed_phase: str, root_cause_class: str, member_count: int,
) -> str:
    """Synthesize a falsifiable claim from a cluster signature.

    Pattern: "I think <root_cause> is the dominant failure mode at
    phase <X>; investigation should reveal <N> recent ops with
    matching signature."

    Bounded at MAX_CLAIM_CHARS.
    """
    fp = (failed_phase or "<unknown>").strip()
    rc = (root_cause_class or "<unknown>").strip()
    claim = (
        f"I think failure pattern {rc!r} is the dominant cause of "
        f"recent {fp} phase failures (cluster size: {member_count})"
    )
    return claim[:MAX_CLAIM_CHARS]


def _synthesize_expected_outcome(
    failed_phase: str, root_cause_class: str,
) -> str:
    """Synthesize the falsifiable predicate: what evidence WOULD
    confirm the claim. Read-only investigation should find concrete
    op_ids matching the cluster signature.

    Bounded at MAX_EXPECTED_OUTCOME_CHARS.
    """
    fp = (failed_phase or "<unknown>").strip()
    rc = (root_cause_class or "<unknown>").strip()
    expected = (
        f"if I read recent .ouroboros/sessions/*/postmortem.jsonl "
        f"files, I expect to find multiple ops with phase={fp} and "
        f"normalized root_cause matching {rc!r}"
    )
    return expected[:MAX_EXPECTED_OUTCOME_CHARS]


def _generate_op_id(
    cluster_signature_hash: str, ts_epoch: float,
) -> str:
    """Build a stable curiosity-engine op_id from the cluster sig
    + timestamp. Format: ``curio-<sig_hash[:8]>-<ts_int>``.

    The op_id MUST be stable for re-mining the same cluster across
    multiple curiosity cycles within the same second window
    (HypothesisLedger uses op_id as part of its dedup key). We add
    ts_int seconds-precision so re-mining the same cluster on the
    next cycle DOES produce a new hypothesis (different op_id)
    rather than dedup-skipping silently.
    """
    sig_short = (cluster_signature_hash or "")[:8] or "unknown"
    ts_int = int(ts_epoch)
    return f"curio-{sig_short}-{ts_int}"


# ---------------------------------------------------------------------------
# Public engine
# ---------------------------------------------------------------------------


@dataclass
class CuriosityEngine:
    """Stateless engine — `run_cycle()` is the entry point.

    Construction-time injection points (all optional with safe
    defaults — production wires the real ledgers + prober):
      * ledger: `HypothesisLedger` instance for hypothesis
        persistence
      * adaptation_ledger: `AdaptationLedger` for the auto-bridge
        path
      * probe: `HypothesisProbe` runner (default: Phase 7.6's
        bounded runner with Null sentinel — zero cost)
      * bridge_to_hypothesis_ledger: callable signature matches
        Item #3 ``hypothesis_probe_bridge.bridge_to_hypothesis_ledger``
      * bridge_to_adaptation_ledger: callable signature matches
        Item #3 ``hypothesis_probe_bridge.bridge_confirmed_to_adaptation_ledger``
    """

    ledger: Any = None  # HypothesisLedger; lazy-typed
    adaptation_ledger: Any = None  # AdaptationLedger; lazy-typed
    probe: Any = None  # HypothesisProbe; lazy-typed
    bridge_to_hypothesis_ledger: Any = None  # callable
    bridge_to_adaptation_ledger: Any = None  # callable

    def run_cycle(
        self,
        clusters: Sequence[Any],  # Sequence[ProposalCandidate]
        *,
        threshold: Optional[int] = None,
        max_hypotheses: Optional[int] = None,
        max_probes: Optional[int] = None,
        now_unix: Optional[float] = None,
    ) -> CuriosityResult:
        """Execute one curiosity cycle.

        Steps:
          1. Master-flag pre-check
          2. Filter clusters by `member_count >= threshold`
          3. Synthesize hypotheses (capped at MAX_HYPOTHESES_PER_CYCLE)
          4. Append each to the HypothesisLedger
          5. (Optional, AUTO_PROBE) invoke Phase 7.6 probe on each
             newly-created hypothesis
          6. (Optional, AUTO_BRIDGE) route terminal probe verdicts
             via Item #3 bridges

        Returns a `CuriosityResult` with structured status. NEVER
        raises.
        """
        ts = now_unix if now_unix is not None else time.time()
        if not is_engine_enabled():
            return CuriosityResult(
                status=CuriosityStatus.SKIPPED_MASTER_OFF,
                detail="master_off",
                ts_epoch=ts,
            )
        if not clusters:
            return CuriosityResult(
                status=CuriosityStatus.SKIPPED_NO_CLUSTERS,
                detail="empty_clusters_input",
                ts_epoch=ts,
            )

        thr = threshold if threshold is not None else get_cluster_threshold()
        max_h = (
            max_hypotheses
            if max_hypotheses is not None
            else MAX_HYPOTHESES_PER_CYCLE
        )
        max_p = (
            max_probes
            if max_probes is not None
            else MAX_PROBES_PER_CYCLE
        )

        # Filter by threshold + sort newest-first by member_count
        # (most-recurring first).
        qualifying = [
            c for c in clusters
            if getattr(c, "member_count", 0) >= thr
        ]
        if not qualifying:
            return CuriosityResult(
                status=CuriosityStatus.SKIPPED_NO_QUALIFYING_CLUSTERS,
                detail=(
                    f"no clusters with member_count>={thr} "
                    f"(input_size={len(clusters)})"
                ),
                ts_epoch=ts,
            )
        # Sort by member_count DESC, tie-break by signature_hash
        # for determinism.
        def _sort_key(c: Any) -> Tuple[int, str]:
            return (
                -int(getattr(c, "member_count", 0)),
                str(getattr(c.signature, "signature_hash",
                            lambda: "")()),
            )
        qualifying.sort(key=_sort_key)

        # Generate hypotheses (capped).
        generated: List[GeneratedHypothesis] = []
        for c in qualifying[:max_h]:
            sig = c.signature
            sig_hash = sig.signature_hash() if hasattr(sig, "signature_hash") else ""
            gh = GeneratedHypothesis(
                op_id=_generate_op_id(sig_hash, ts),
                claim=_synthesize_claim(
                    sig.failed_phase, sig.root_cause_class,
                    c.member_count,
                ),
                expected_outcome=_synthesize_expected_outcome(
                    sig.failed_phase, sig.root_cause_class,
                ),
                cluster_signature_hash=sig_hash,
                created_unix=ts,
            )
            generated.append(gh)

        # Persist to HypothesisLedger.
        if self.ledger is not None:
            from backend.core.ouroboros.governance.hypothesis_ledger import (
                Hypothesis, make_hypothesis_id,
            )
            persisted = 0
            for gh in generated:
                hid = make_hypothesis_id(
                    gh.op_id, gh.claim, gh.created_unix,
                )
                hyp = Hypothesis(
                    hypothesis_id=hid,
                    op_id=gh.op_id,
                    claim=gh.claim,
                    expected_outcome=gh.expected_outcome,
                    proposed_signature_hash=gh.cluster_signature_hash,
                    created_unix=gh.created_unix,
                )
                try:
                    ok = self.ledger.append(hyp)
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.warning(
                        "[CuriosityEngine] ledger.append raised %s "
                        "for op_id=%s — continuing",
                        type(exc).__name__, gh.op_id,
                    )
                    ok = False
                if ok:
                    persisted += 1
            if persisted == 0 and len(generated) > 0:
                return CuriosityResult(
                    status=CuriosityStatus.LEDGER_WRITE_FAILED,
                    hypotheses_generated=tuple(generated),
                    detail="all_appends_failed",
                    ts_epoch=ts,
                )

        # Optional probe sweep.
        probes_run = 0
        bridge_results: List[Any] = []
        if (
            is_auto_probe_enabled()
            and self.probe is not None
        ):
            for gh in generated[:max_p]:
                try:
                    probe_result = self.probe.test(
                        gh.claim, gh.expected_outcome,
                    )
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.warning(
                        "[CuriosityEngine] probe.test raised %s — "
                        "skipping op_id=%s",
                        type(exc).__name__, gh.op_id,
                    )
                    continue
                probes_run += 1

                # Optional bridge sweep.
                if is_auto_bridge_enabled():
                    if self.bridge_to_hypothesis_ledger is not None:
                        try:
                            from backend.core.ouroboros.governance.hypothesis_ledger import (
                                make_hypothesis_id,
                            )
                            hid = make_hypothesis_id(
                                gh.op_id, gh.claim, gh.created_unix,
                            )
                            br = self.bridge_to_hypothesis_ledger(
                                probe_result, hid, self.ledger,
                            )
                            bridge_results.append(br)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "[CuriosityEngine] hypothesis bridge "
                                "raised %s — continuing",
                                type(exc).__name__,
                            )

        return CuriosityResult(
            status=CuriosityStatus.OK,
            hypotheses_generated=tuple(generated),
            probes_run=probes_run,
            bridge_results=tuple(bridge_results),
            ts_epoch=ts,
        )


_DEFAULT_ENGINE: Optional[CuriosityEngine] = None


def get_default_engine() -> CuriosityEngine:
    global _DEFAULT_ENGINE
    if _DEFAULT_ENGINE is None:
        _DEFAULT_ENGINE = CuriosityEngine()
    return _DEFAULT_ENGINE


def reset_default_engine() -> None:
    global _DEFAULT_ENGINE
    _DEFAULT_ENGINE = None


__all__ = [
    "CuriosityEngine",
    "CuriosityResult",
    "CuriosityStatus",
    "DEFAULT_CLUSTER_THRESHOLD",
    "GeneratedHypothesis",
    "MAX_CLAIM_CHARS",
    "MAX_EXPECTED_OUTCOME_CHARS",
    "MAX_HYPOTHESES_PER_CYCLE",
    "MAX_PROBES_PER_CYCLE",
    "get_cluster_threshold",
    "get_default_engine",
    "is_auto_bridge_enabled",
    "is_auto_probe_enabled",
    "is_engine_enabled",
    "reset_default_engine",
]
