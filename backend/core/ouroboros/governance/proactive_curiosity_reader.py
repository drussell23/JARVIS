"""Move 8 Slice 1 — Proactive curiosity reader (substrate primitive).

PRD §29.7 / §35 Move 8: closes the auto-spawn-exploration-ops half
of the M9 CuriosityGradient producer-consumer loop. M9 (CLOSED
2026-05-04) wired three producers — GENERATE logprob entropy /
post-VERIFY prophecy error / CoherenceAuditor RECURRENCE_DRIFT —
into :class:`CuriosityCollector`, but no reader translates the
collector's accumulated state into actionable exploration intents.
This module is that reader.

It is the **substrate-only half** of Move 8. Slice 2 wires it as
a third signal source inside the existing
:class:`ProactiveExplorationSensor.scan_once` loop (alongside the
LearningConsolidator failure-rule path and the
codebase_character cluster-coverage path) — composes existing
files, no parallel poll loop.

Architectural locks (operator mandate):

  * **Pure-function reader** — :func:`rank_curious_clusters`
    accepts a ``CuriosityCollector`` snapshot (or the
    process-singleton via :func:`curiosity_collector.
    get_default_collector`) and returns a frozen tuple of
    :class:`CuriosityRanking` artifacts. NO I/O, NO env reads
    inside the math (caller injects clamped knobs); NEVER raises.
  * **Authority asymmetry** (AST-pinned) — module MUST NOT
    import orchestrator / iron_gate / policy / providers /
    candidate_generator / urgency_router / change_engine /
    semantic_guardian. Substrate stays pure.
  * **Master flag default-FALSE** per §33.1 graduation-contract
    pattern — flips only after Slice 3's empirical contract
    proves the loop doesn't overrun
    :class:`SensorGovernor` caps. AST pin enforces (synthetic
    test proves the pin DOES fire on premature flip).
  * **Closed taxonomy** — :class:`CuriosityRankingDecision` is a
    5-value closed enum (taxonomy-pinned). Future drift
    requires explicit scope-doc + pin update.
  * **Composes M9 substrate** — reads via
    :meth:`CuriosityCollector.snapshot_all` (the sanctioned
    pull-side surface). Forbids parallel score computation
    (AST-pinned: no calls to ``compute_curiosity`` from this
    module).
  * **§33.5 Versioned-Artifact-Contract** — :class:`CuriosityRanking`
    carries ``schema_version`` + symmetric ``to_dict()`` /
    ``from_dict()`` projection.

What this DOES:

  1. Reads ``CuriosityCollector.snapshot_all()`` (cheap; no
     side effects).
  2. Filters by magnitude floor + cold-start exclusion + decay-
     reason exclusion (cold-start scores are inert per M9
     Decision A1; decayed scores are explicitly de-prioritized
     by their decay reason).
  3. Sorts by magnitude descending (tie-break on
     ``last_updated_at_unix`` descending — most-recent wins).
  4. Caps at top-K (env-knob, default 3, clamped [1, 16]).
  5. Returns a frozen tuple of :class:`CuriosityRanking`
     artifacts.

What this does NOT do:

  * Emit IntentEnvelopes — that's Slice 2's job inside the
    ProactiveExplorationSensor poll loop.
  * Talk to SensorGovernor / orchestrator / risk-tier — that's
    intake's job downstream.
  * Persist anything — pure read.
  * Schedule its own poll — composes the existing
    ProactiveExplorationSensor cadence.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


PROACTIVE_CURIOSITY_READER_SCHEMA_VERSION: str = (
    "proactive_curiosity_reader.1"
)


# ---------------------------------------------------------------------------
# Env knobs (all clamped, all default-conservative)
# ---------------------------------------------------------------------------


def proactive_curiosity_reader_enabled() -> bool:
    """``JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED`` — master kill
    switch. **Default FALSE** per §33.1 Graduation Contract
    Pattern — flips only after Slice 3's empirical contract
    proves the curiosity loop doesn't overrun SensorGovernor
    caps. AST-pinned: future PR that flips default-true without
    the contract handoff fails the pin."""
    raw = os.environ.get(
        "JARVIS_PROACTIVE_CURIOSITY_READER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default — operator-pinned
    return raw in ("1", "true", "yes", "on")


def top_k() -> int:
    """``JARVIS_PROACTIVE_CURIOSITY_TOP_K`` — number of curious
    clusters to surface per scan. Default 3 (matches the
    existing per-scan emit-cap discipline of
    ``cluster_coverage`` — small enough to avoid intake
    flood). Clamped [1, 16]."""
    raw = os.environ.get(
        "JARVIS_PROACTIVE_CURIOSITY_TOP_K", "",
    ).strip()
    try:
        n = int(raw) if raw else 3
        if n < 1:
            return 1
        if n > 16:
            return 16
        return n
    except (TypeError, ValueError):
        return 3


def magnitude_floor() -> float:
    """``JARVIS_PROACTIVE_CURIOSITY_MAGNITUDE_FLOOR`` — minimum
    curiosity magnitude to even consider for ranking. Default
    0.40 (matches the existing
    ``JARVIS_EXPLORATION_ENTROPY_THRESHOLD`` precedent for
    "this is interesting enough to surface"). Clamped [0.0, 1.0]."""
    raw = os.environ.get(
        "JARVIS_PROACTIVE_CURIOSITY_MAGNITUDE_FLOOR", "",
    ).strip()
    try:
        v = float(raw) if raw else 0.40
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v
    except (TypeError, ValueError):
        return 0.40


def cooldown_seconds() -> int:
    """``JARVIS_PROACTIVE_CURIOSITY_COOLDOWN_S`` — minimum interval
    between repeated rankings of the same cluster_id.
    De-duplication is across calls, not across collector ticks
    (the collector itself doesn't dedup at the ranking layer).
    Default 14400 (4h — long enough to give the cluster a chance
    to drift to a new shape; short enough to re-fire within a
    work session). Clamped [60, 7d]."""
    raw = os.environ.get(
        "JARVIS_PROACTIVE_CURIOSITY_COOLDOWN_S", "",
    ).strip()
    try:
        n = int(raw) if raw else 14_400
        if n < 60:
            return 60
        if n > 7 * 24 * 3600:
            return 7 * 24 * 3600
        return n
    except (TypeError, ValueError):
        return 14_400


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class CuriosityRankingDecision(str, enum.Enum):
    """Closed taxonomy: why a candidate cluster ended up in (or
    out of) the ranking. Slice 3's graduation contract reads the
    distribution of these decisions to prove the reader isn't
    starving on stale collector state."""

    SURFACED = "surfaced"
    """Cluster met magnitude floor + non-cold-start + non-decayed
    + cooldown clear → included in ranking."""

    BELOW_FLOOR = "below_floor"
    """Magnitude under :func:`magnitude_floor` — not interesting
    enough yet."""

    COLD_START = "cold_start"
    """:meth:`CuriosityScore.is_cold_start` is True — too few
    samples; structurally inert."""

    DECAY_SUPPRESSED = "decay_suppressed"
    """:attr:`CuriosityScore.decay_reason` is non-NONE — operator
    chose to suppress regardless of magnitude."""

    COOLDOWN = "cooldown"
    """Recently surfaced — within :func:`cooldown_seconds`
    window for this cluster_id."""


# ---------------------------------------------------------------------------
# Frozen artifact (§33.5 versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuriosityRanking:
    """One row of the ranked snapshot. Slice 2 consumes this to
    build IntentEnvelope.target_files + description; Slice 3's
    contract reads ``decision`` distribution + ``magnitude`` for
    the freshness gate.

    Frozen — immutable atomic value. §33.5 versioned —
    ``schema_version`` + symmetric ``to_dict()`` / ``from_dict()``
    so future cross-runner readers can detect drift structurally.
    """

    cluster_id: str
    magnitude: float
    confidence: float
    samples_count: int
    dominant_source: str
    """Stringified ``CuriositySource`` value — string rather than
    enum so the artifact serializes deterministically without
    leaking the enum import to consumers."""

    decay_reason: str
    """Stringified ``CuriosityDecayReason`` value, same
    rationale."""

    last_updated_at_unix: float
    rank: int
    """Position in the descending-magnitude ranking, 1-indexed.
    -1 when ``decision != SURFACED``."""

    decision: CuriosityRankingDecision
    schema_version: str = field(
        default=PROACTIVE_CURIOSITY_READER_SCHEMA_VERSION,
    )

    def to_dict(self) -> dict:
        return {
            "cluster_id": str(self.cluster_id),
            "magnitude": float(self.magnitude),
            "confidence": float(self.confidence),
            "samples_count": int(self.samples_count),
            "dominant_source": str(self.dominant_source),
            "decay_reason": str(self.decay_reason),
            "last_updated_at_unix": float(
                self.last_updated_at_unix,
            ),
            "rank": int(self.rank),
            "decision": self.decision.value,
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, raw: Any,
    ) -> "Optional[CuriosityRanking]":
        """§33.5 defensive parse — None on any malformed input.
        NEVER raises."""
        if not isinstance(raw, dict):
            return None
        try:
            decision_raw = raw.get("decision", "")
            try:
                decision = CuriosityRankingDecision(decision_raw)
            except (ValueError, TypeError):
                return None
            return cls(
                cluster_id=str(raw.get("cluster_id", "")),
                magnitude=float(raw.get("magnitude", 0.0)),
                confidence=float(raw.get("confidence", 0.0)),
                samples_count=int(raw.get("samples_count", 0)),
                dominant_source=str(raw.get(
                    "dominant_source", "",
                )),
                decay_reason=str(raw.get("decay_reason", "")),
                last_updated_at_unix=float(raw.get(
                    "last_updated_at_unix", 0.0,
                )),
                rank=int(raw.get("rank", -1)),
                decision=decision,
                schema_version=str(raw.get(
                    "schema_version",
                    PROACTIVE_CURIOSITY_READER_SCHEMA_VERSION,
                )),
            )
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# In-process cooldown ledger
# ---------------------------------------------------------------------------


# Maps cluster_id → unix timestamp of last SURFACED ranking.
# In-process only — survives within a sensor lifetime; reset
# across process restarts (intentional: the curiosity score
# itself is persisted, so a restart re-evaluates against the
# current scoreboard).
_COOLDOWN_LEDGER: dict[str, float] = {}


def _cooldown_active(
    cluster_id: str, *, now_unix: float, window_s: int,
) -> bool:
    last = _COOLDOWN_LEDGER.get(cluster_id, 0.0)
    if last <= 0.0:
        return False
    return (now_unix - last) < float(window_s)


def _mark_surfaced(cluster_id: str, *, now_unix: float) -> None:
    _COOLDOWN_LEDGER[cluster_id] = now_unix


def reset_cooldown_ledger_for_tests() -> None:
    """Test-only — production code never calls. Pinned via
    naming convention (``_for_tests`` suffix)."""
    _COOLDOWN_LEDGER.clear()


# ---------------------------------------------------------------------------
# Pure-function reader
# ---------------------------------------------------------------------------


def rank_curious_clusters(
    *,
    snapshot: Optional[Tuple[Any, ...]] = None,
    collector: Optional[Any] = None,
    enabled_override: Optional[bool] = None,
    top_k_override: Optional[int] = None,
    magnitude_floor_override: Optional[float] = None,
    cooldown_seconds_override: Optional[int] = None,
    now_unix: Optional[float] = None,
) -> Tuple[CuriosityRanking, ...]:
    """Pure-function reader — composes
    :meth:`CuriosityCollector.snapshot_all` and returns the
    ranked top-K curious clusters.

    Caller-injection convention:

      * ``snapshot`` — pre-computed tuple of ``CuriosityScore``;
        if ``None``, falls back to ``collector.snapshot_all()``.
      * ``collector`` — explicit collector instance; if ``None``,
        falls back to
        :func:`curiosity_collector.get_default_collector`.
      * Override knobs accept caller-clamped values (testing /
        Slice 3 contract); ``None`` reads env via the public
        helpers.
      * ``now_unix`` — caller-injected clock (testing /
        contract); ``None`` reads :func:`time.time`.

    Returns a frozen tuple. Empty when:

      * Master flag off (returns empty + logs once at debug —
        the contract is "no spurious work when disabled").
      * Collector unavailable (ImportError-safe — Slice 2's
        wire-up tolerates this gracefully).
      * No candidates pass filtering.

    NEVER raises. The contract with the Slice 2 caller is "this
    function returned cleanly; consume the tuple at face value."
    """
    is_enabled = (
        enabled_override
        if enabled_override is not None
        else proactive_curiosity_reader_enabled()
    )
    if not is_enabled:
        return ()

    k = (
        int(top_k_override)
        if top_k_override is not None
        else top_k()
    )
    floor = (
        float(magnitude_floor_override)
        if magnitude_floor_override is not None
        else magnitude_floor()
    )
    cd_window_s = (
        int(cooldown_seconds_override)
        if cooldown_seconds_override is not None
        else cooldown_seconds()
    )
    now = (
        float(now_unix)
        if now_unix is not None
        else time.time()
    )

    # Resolve snapshot — caller-injected path takes precedence.
    if snapshot is None:
        coll = collector
        if coll is None:
            try:
                from backend.core.ouroboros.governance.curiosity_collector import (  # noqa: E501
                    get_default_collector,
                )
                coll = get_default_collector()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[proactive_curiosity_reader] "
                    "get_default_collector unavailable",
                    exc_info=True,
                )
                return ()
        try:
            snapshot = coll.snapshot_all()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[proactive_curiosity_reader] "
                "snapshot_all raised — returning empty",
                exc_info=True,
            )
            return ()

    if not snapshot:
        return ()

    # Classify every score — even rejects get a Ranking with
    # decision != SURFACED so Slice 3's contract can read the
    # distribution. But only SURFACED rows get a real rank.
    classified: list[Tuple[CuriosityRankingDecision, Any]] = []
    for score in snapshot:
        try:
            cluster_id = str(getattr(score, "cluster_id", ""))
            if not cluster_id:
                continue
            mag = float(getattr(score, "magnitude", 0.0))
            # Cold-start exclusion (M9 Decision A1 — score is
            # structurally inert).
            try:
                if score.is_cold_start():
                    classified.append(
                        (CuriosityRankingDecision.COLD_START,
                         score),
                    )
                    continue
            except Exception:  # noqa: BLE001
                # If is_cold_start raises, treat as cold-start
                # (defensive — never let a malformed score
                # surface).
                classified.append(
                    (CuriosityRankingDecision.COLD_START,
                     score),
                )
                continue
            # Decay-reason exclusion — operator-chosen
            # suppression; respect it.
            decay_reason_obj = getattr(
                score, "decay_reason", None,
            )
            decay_value = getattr(decay_reason_obj, "value", "")
            if decay_value and decay_value != "none":
                classified.append(
                    (CuriosityRankingDecision.DECAY_SUPPRESSED,
                     score),
                )
                continue
            # Magnitude floor.
            if mag < floor:
                classified.append(
                    (CuriosityRankingDecision.BELOW_FLOOR,
                     score),
                )
                continue
            # Cooldown gate (cross-call dedup).
            if _cooldown_active(
                cluster_id,
                now_unix=now,
                window_s=cd_window_s,
            ):
                classified.append(
                    (CuriosityRankingDecision.COOLDOWN,
                     score),
                )
                continue
            classified.append(
                (CuriosityRankingDecision.SURFACED, score),
            )
        except Exception:  # noqa: BLE001
            # Defensive — one bad score doesn't poison the
            # whole ranking.
            logger.debug(
                "[proactive_curiosity_reader] skipping malformed "
                "score",
                exc_info=True,
            )
            continue

    # Sort SURFACED by magnitude desc, tie-break on
    # last_updated_at_unix desc. Top-K cut produces the
    # rank-bearing slice; ranks 1..K assigned in sort order
    # (not input order — input order is meaningless to the
    # contract).
    surfaced = [
        s for d, s in classified
        if d is CuriosityRankingDecision.SURFACED
    ]
    surfaced.sort(
        key=lambda s: (
            float(getattr(s, "magnitude", 0.0)),
            float(getattr(s, "last_updated_at_unix", 0.0)),
        ),
        reverse=True,
    )
    surfaced = surfaced[:k]
    # cluster_id → rank (1-indexed) for in-top-K rows.
    rank_by_id: dict[str, int] = {}
    for idx, score in enumerate(surfaced):
        cluster_id = str(getattr(score, "cluster_id", ""))
        if cluster_id:
            rank_by_id[cluster_id] = idx + 1
            _mark_surfaced(cluster_id, now_unix=now)

    # Emit SURFACED rows first (in rank order, 1..K) so Slice 2
    # can iterate top-K in priority order without re-sorting.
    # Then emit non-SURFACED rows in original input order
    # (forensic preservation for Slice 3's contract — the
    # rejected-by-decision distribution is order-independent).
    surfaced_rankings: list[CuriosityRanking] = []
    rejected_rankings: list[CuriosityRanking] = []
    for decision, score in classified:
        cluster_id = str(getattr(score, "cluster_id", ""))
        if decision is CuriosityRankingDecision.SURFACED:
            rank = rank_by_id.get(cluster_id, -1)
            if rank == -1:
                # Passed all filters but lost the top-K cut.
                # Drop silently — emitting it with a different
                # decision would lie about why; emitting it as
                # SURFACED with rank=-1 contradicts the
                # contract that SURFACED rows always have a
                # real rank. Slice 3's contract reads truthful
                # decision distributions; "more candidates
                # than K" is a separate question.
                continue
        else:
            rank = -1
        decay_reason_obj = getattr(score, "decay_reason", None)
        dominant_source_obj = getattr(
            score, "dominant_source", None,
        )
        ranking = CuriosityRanking(
            cluster_id=cluster_id,
            magnitude=float(getattr(
                score, "magnitude", 0.0,
            )),
            confidence=float(getattr(
                score, "confidence", 0.0,
            )),
            samples_count=int(getattr(
                score, "samples_count", 0,
            )),
            dominant_source=str(getattr(
                dominant_source_obj, "value", "",
            )),
            decay_reason=str(getattr(
                decay_reason_obj, "value", "",
            )),
            last_updated_at_unix=float(getattr(
                score, "last_updated_at_unix", 0.0,
            )),
            rank=rank,
            decision=decision,
        )
        if decision is CuriosityRankingDecision.SURFACED:
            surfaced_rankings.append(ranking)
        else:
            rejected_rankings.append(ranking)

    surfaced_rankings.sort(key=lambda r: r.rank)
    return tuple(surfaced_rankings) + tuple(rejected_rankings)


# ---------------------------------------------------------------------------
# Auto-discovered AST pins (§32.11 Slice 2 / shipped_code_invariants)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered by
    :func:`shipped_code_invariants._discover_module_provided_invariants`.

    Pins:
      1. ``proactive_curiosity_reader_master_flag_stays_default_false``
         — operator binding (§33.1): master flag default is
         FALSE until the Slice 3 graduation contract proves the
         loop doesn't overrun SensorGovernor caps.
      2. ``proactive_curiosity_reader_authority_asymmetry`` —
         substrate stays pure (no orchestrator / iron_gate /
         policy / providers / candidate_generator imports).
      3. ``proactive_curiosity_reader_decision_taxonomy_5_values``
         — closed-enum integrity. New decisions require explicit
         scope-doc + this pin update.
      4. ``proactive_curiosity_reader_composes_m9_substrate`` —
         module MUST NOT call ``compute_curiosity`` directly
         (would parallelize M9's authoritative scoring); MUST
         compose via ``snapshot_all`` / ``score_for_cluster``
         only.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        target = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if (
                    node.name
                    == "proactive_curiosity_reader_enabled"
                ):
                    target = node
                    break
        if target is None:
            violations.append(
                "proactive_curiosity_reader_enabled function "
                "missing"
            )
            return tuple(violations)
        has_default_false = False
        for node in ast.walk(target):
            if isinstance(node, ast.Return):
                if isinstance(node.value, ast.Constant):
                    if node.value.value is False:
                        has_default_false = True
                        break
        if not has_default_false:
            violations.append(
                "proactive_curiosity_reader_enabled MUST return "
                "False on the unset-env path (§33.1 operator "
                "binding — master flag default-FALSE until the "
                "graduation contract proves it safe)"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"proactive_curiosity_reader.py MUST "
                            f"NOT import {module!r}"
                        )
        return tuple(violations)

    def _validate_decision_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "SURFACED", "BELOW_FLOOR", "COLD_START",
            "DECAY_SUPPRESSED", "COOLDOWN",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "CuriosityRankingDecision":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    extra = seen - required
                    missing = required - seen
                    if extra:
                        violations.append(
                            f"CuriosityRankingDecision has extra "
                            f"values {sorted(extra)} — taxonomy "
                            f"is closed; update the pin if "
                            f"intentional"
                        )
                    if missing:
                        violations.append(
                            f"CuriosityRankingDecision missing "
                            f"required values {sorted(missing)}"
                        )
        return tuple(violations)

    def _validate_composes_m9_substrate(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Forbid direct call to ``compute_curiosity``
                # — that's M9's authoritative scoring entry
                # point; this module composes the result via
                # snapshot_all, never re-computes.
                if isinstance(func, ast.Name):
                    if func.id == "compute_curiosity":
                        violations.append(
                            "proactive_curiosity_reader.py MUST "
                            "NOT call compute_curiosity directly "
                            "— compose via "
                            "CuriosityCollector.snapshot_all"
                        )
                elif isinstance(func, ast.Attribute):
                    if func.attr == "compute_curiosity":
                        violations.append(
                            "proactive_curiosity_reader.py MUST "
                            "NOT call compute_curiosity directly"
                        )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "proactive_curiosity_reader.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name=(
                "proactive_curiosity_reader_master_flag_"
                "stays_default_false"
            ),
            target_file=target,
            description=(
                "Move 8 Slice 1 — §33.1 operator binding: master "
                "flag stays default-FALSE until the graduation "
                "contract proves the loop respects SensorGovernor "
                "caps."
            ),
            validate=_validate_master_flag_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "proactive_curiosity_reader_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 8 Slice 1 — substrate purity: reader MUST "
                "NOT import orchestrator / iron_gate / policy / "
                "providers / candidate_generator / etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "proactive_curiosity_reader_decision_"
                "taxonomy_5_values"
            ),
            target_file=target,
            description=(
                "Move 8 Slice 1 — CuriosityRankingDecision is a "
                "5-value closed enum."
            ),
            validate=_validate_decision_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "proactive_curiosity_reader_composes_m9_substrate"
            ),
            target_file=target,
            description=(
                "Move 8 Slice 1 — composes M9 via snapshot_all "
                "only; never re-computes scores."
            ),
            validate=_validate_composes_m9_substrate,
        ),
    ]


__all__ = [
    "CuriosityRanking",
    "CuriosityRankingDecision",
    "PROACTIVE_CURIOSITY_READER_SCHEMA_VERSION",
    "cooldown_seconds",
    "magnitude_floor",
    "proactive_curiosity_reader_enabled",
    "rank_curious_clusters",
    "register_shipped_invariants",
    "reset_cooldown_ledger_for_tests",
    "top_k",
]
