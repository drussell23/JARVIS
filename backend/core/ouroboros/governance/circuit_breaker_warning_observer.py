"""§37 Slice 8 — Circuit-breaker approach-to-trip observer.

Closes Tier 1 #6 from the §37 UX roadmap. Surfaces approaching-
trip warnings to operators BEFORE the breaker flips CLOSED →
OPEN. Currently the harness's circuit breakers (rate_limiter
``CircuitBreaker``, ``ClaudeCircuitBreaker``, etc.) trip
silently — operators only see the OPEN state AFTER it happens
(if they happen to be looking at logs / debug artifacts).

This observer composes Slice 5's band-crossing discipline
(`cost_warning_observer`) applied to the failure-count vs
trip-threshold ratio so operators see the band ladder
**before** the trip:

  * ``OK``       — failure_count == 0 (clean)
  * ``NOTICE``   — failure_count >= notice_pct × threshold
                   (default 33% — first hint trouble is brewing)
  * ``WARN``     — failure_count >= warn_pct × threshold
                   (default 66% — operator should check)
  * ``CRITICAL`` — failure_count >= critical_pct × threshold
                   (default 90% — next failure trips)
  * ``BREACH``   — failure_count >= threshold (trip imminent
                   or just occurred)

Per the operator binding "fully leverage existing files...
build cleanly... no duplication":

  * **Composes existing broker substrate** — emits via the
    canonical ``StreamEventBroker.publish()`` (same surface
    Slice 2's ``/listen`` reads). NO parallel queue, NO
    second event spine.
  * **Mirrors Slice 5 chatter-suppression discipline** —
    band-crossings only; same-band ticks return None.
    AST-pinned via the same shape.
  * **Single-pipeline** — one observer instance per process
    (singleton accessor matches Slice 5 pattern); breaker
    sites call ``record_failure(breaker_id, count, threshold)``
    on each ``CircuitBreaker.record_failure()``.
  * **Authority asymmetry** — substrate purity (no
    orchestrator / iron_gate / providers imports).
  * **NEVER raises** — every code path defensive. The breaker
    site must NEVER break on observer error.

5-band ladder is the SAME closed taxonomy from Slice 5 —
re-uses ``CostBand`` enum verbatim. Future generalization
(`band_observer.py` substrate) can dedupe the band-crossing
logic; for now the duplication is the chatter-suppression
state machine (~30 LOC), and passing 46 cost-warning tests
remain stable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Reuse the 5-band closed taxonomy from Slice 5 — same shape,
# different domain (cost % vs failure count %). Same chatter-
# suppression discipline. Same SSE-emission convention.
from backend.core.ouroboros.governance.cost_warning_observer import (
    CostBand,
)


logger = logging.getLogger(
    "Ouroboros.CircuitBreakerWarningObserver",
)


CIRCUIT_BREAKER_WARNING_OBSERVER_SCHEMA_VERSION: str = (
    "circuit_breaker_warning_observer.1"
)


# ---------------------------------------------------------------------------
# Frozen artifact — band crossing event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakerBandCrossing:
    """Recorded transition between two CostBand values for a
    specific circuit-breaker stream. Frozen for safe propagation.
    Emitted only when ``record_failure()`` observes a band
    CHANGE on this breaker_id."""

    breaker_id: str
    """Component identifier — e.g., 'claude_circuit_breaker' /
    'dw_topology_sentinel:gemma-4-31b' / generic per-instance
    breaker handle."""

    from_band: CostBand
    to_band: CostBand
    failure_count: int
    threshold: int
    ratio: float
    schema_version: str = field(
        default=(
            CIRCUIT_BREAKER_WARNING_OBSERVER_SCHEMA_VERSION
        ),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "breaker_id": str(self.breaker_id),
            "from_band": self.from_band.value,
            "to_band": self.to_band.value,
            "failure_count": int(self.failure_count),
            "threshold": int(self.threshold),
            "ratio": float(self.ratio),
            "schema_version": str(self.schema_version),
        }


# ---------------------------------------------------------------------------
# Env-tunable thresholds (clamped, sane defaults — distinct from cost
# warning thresholds since the failure-count semantic is different)
# ---------------------------------------------------------------------------


def _clamp_pct(
    raw: str, default: int, low: int, high: int,
) -> int:
    try:
        n = int(raw) if raw.strip() else default
    except (TypeError, ValueError):
        return default
    if n < low:
        return low
    if n > high:
        return high
    return n


def notice_threshold_pct() -> int:
    """``JARVIS_BREAKER_WARN_BAND_NOTICE_PCT`` — first-warning
    band entry. Default 33 (one third of the way to trip).
    Clamped [1, 99]."""
    return _clamp_pct(
        os.environ.get(
            "JARVIS_BREAKER_WARN_BAND_NOTICE_PCT", "",
        ),
        default=33, low=1, high=99,
    )


def warn_threshold_pct() -> int:
    """``JARVIS_BREAKER_WARN_BAND_WARN_PCT`` — main warning
    band entry. Default 66 (two thirds of the way to trip).
    Clamped [1, 99]."""
    return _clamp_pct(
        os.environ.get(
            "JARVIS_BREAKER_WARN_BAND_WARN_PCT", "",
        ),
        default=66, low=1, high=99,
    )


def critical_threshold_pct() -> int:
    """``JARVIS_BREAKER_WARN_BAND_CRITICAL_PCT`` — last-warning
    band before breach. Default 90 (next-failure-trips zone).
    Clamped [1, 99]."""
    return _clamp_pct(
        os.environ.get(
            "JARVIS_BREAKER_WARN_BAND_CRITICAL_PCT", "",
        ),
        default=90, low=1, high=99,
    )


# ---------------------------------------------------------------------------
# Pure-function band classifier (specialized for breaker semantics)
# ---------------------------------------------------------------------------


def classify_breaker_band(
    failure_count: int,
    threshold: int,
    *,
    notice_pct: Optional[int] = None,
    warn_pct: Optional[int] = None,
    critical_pct: Optional[int] = None,
) -> CostBand:
    """Classify a circuit-breaker failure-count snapshot into
    a CostBand. Pure function; NEVER raises.

    Specialized semantics:
      * failure_count == 0 → ``OK`` regardless of thresholds
        (clean breaker is unambiguously OK; no band creep)
      * failure_count >= threshold → ``BREACH`` (trip moment)
      * In between, classify by ratio against env percentages

    Caller-injection enables testing without env mocking.
    """
    try:
        fc = int(failure_count)
        thr = int(threshold)
    except (TypeError, ValueError):
        return CostBand.OK
    if fc <= 0:
        return CostBand.OK
    if thr <= 0:
        # Defensive: undefined threshold → can't classify; OK.
        return CostBand.OK
    if fc >= thr:
        return CostBand.BREACH
    notice = (
        notice_pct if notice_pct is not None
        else notice_threshold_pct()
    )
    warn = (
        warn_pct if warn_pct is not None
        else warn_threshold_pct()
    )
    critical = (
        critical_pct if critical_pct is not None
        else critical_threshold_pct()
    )
    ratio = fc / thr
    if ratio >= critical / 100.0:
        return CostBand.CRITICAL
    if ratio >= warn / 100.0:
        return CostBand.WARN
    if ratio >= notice / 100.0:
        return CostBand.NOTICE
    return CostBand.OK


# ---------------------------------------------------------------------------
# CircuitBreakerWarningObserver — stateful band-crossing detector
# ---------------------------------------------------------------------------


class CircuitBreakerWarningObserver:
    """Observes circuit-breaker failure_count samples; emits
    ``BreakerBandCrossing`` only on band transitions
    (chatter-suppression structural).

    Multiple breaker_ids are tracked independently — e.g.,
    'claude_circuit_breaker' + 'dw_topology_sentinel:<model>'
    each get their own band history.

    NEVER raises.
    """

    def __init__(self) -> None:
        self._last_band_per_breaker: Dict[str, CostBand] = {}

    def record_failure(
        self,
        *,
        breaker_id: str,
        failure_count: int,
        threshold: int,
        publish_sse: bool = True,
    ) -> Optional[BreakerBandCrossing]:
        """Sample a breaker observation. Returns a
        BreakerBandCrossing when the band CHANGED from the last
        observation on this breaker_id; returns None when the
        band stayed the same (chatter-suppression structural).

        Args:
            breaker_id: stable identifier for this breaker
                instance (e.g. 'claude_circuit_breaker',
                'dw_topology_sentinel:gemma-4-31b').
            failure_count: current consecutive failure count
                (read from CircuitBreaker._failure_count).
            threshold: trip threshold (read from
                CircuitBreaker._failure_threshold).
            publish_sse: emit SSE on band crossing (default
                True; tests can disable).

        Defensive: any error in classification or SSE emit is
        swallowed; observer state remains coherent. NEVER raises.
        """
        try:
            fc = int(failure_count)
            thr = int(threshold)
        except (TypeError, ValueError):
            return None
        if thr <= 0:
            return None
        new_band = classify_breaker_band(fc, thr)
        prev_band = self._last_band_per_breaker.get(breaker_id)
        if prev_band == new_band:
            return None
        # First-observation discipline: streams that boot at OK
        # don't emit spurious OK→OK. (Matches Slice 5 pattern.)
        if prev_band is None and new_band == CostBand.OK:
            self._last_band_per_breaker[breaker_id] = new_band
            return None
        # Band crossed! Update + emit.
        self._last_band_per_breaker[breaker_id] = new_band
        from_band = prev_band or CostBand.OK
        ratio = fc / thr if thr > 0 else 0.0
        crossing = BreakerBandCrossing(
            breaker_id=str(breaker_id),
            from_band=from_band,
            to_band=new_band,
            failure_count=fc,
            threshold=thr,
            ratio=ratio,
        )
        logger.info(
            "[CircuitBreakerWarningObserver] band crossed "
            "%s → %s (breaker=%s count=%d/%d ratio=%.2f)",
            from_band.value, new_band.value,
            breaker_id, fc, thr, ratio,
        )
        if publish_sse:
            self._publish_to_broker(crossing)
        return crossing

    def reset(
        self, breaker_id: Optional[str] = None,
    ) -> None:
        """Clear last-band state. ``breaker_id=None`` clears
        ALL breakers (test isolation); a specific id clears
        only that one. Composes ``CircuitBreaker.record_success``
        clearing semantics — when the breaker resets to CLOSED
        with failure_count=0, this observer sees the OK band
        on the next failure observation."""
        if breaker_id is None:
            self._last_band_per_breaker.clear()
        else:
            self._last_band_per_breaker.pop(
                breaker_id, None,
            )

    def last_band(
        self, breaker_id: str,
    ) -> Optional[CostBand]:
        """Return the last-observed band for this breaker, or
        None if no observation has been recorded yet."""
        return self._last_band_per_breaker.get(breaker_id)

    @staticmethod
    def _publish_to_broker(
        crossing: BreakerBandCrossing,
    ) -> None:
        """Emit canonical SSE event. Composes existing broker
        (Slice 2). Defensive — error swallowed."""
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                EVENT_TYPE_CIRCUIT_BREAKER_APPROACHING,
                get_default_broker,
            )
            broker = get_default_broker()
            if broker is None:
                return
            broker.publish(
                event_type=(
                    EVENT_TYPE_CIRCUIT_BREAKER_APPROACHING
                ),
                op_id="",
                payload=crossing.to_dict(),
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[CircuitBreakerWarningObserver] SSE publish "
                "failed",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_DEFAULT_OBSERVER: Optional[
    CircuitBreakerWarningObserver
] = None


def get_default_observer() -> CircuitBreakerWarningObserver:
    """Return the process-wide default
    :class:`CircuitBreakerWarningObserver` singleton.
    Created lazily on first access."""
    global _DEFAULT_OBSERVER
    if _DEFAULT_OBSERVER is None:
        _DEFAULT_OBSERVER = CircuitBreakerWarningObserver()
    return _DEFAULT_OBSERVER


def reset_default_observer_for_tests() -> None:
    """Test-only — production code never calls. Pinned via
    naming convention (``_for_tests`` suffix)."""
    global _DEFAULT_OBSERVER
    _DEFAULT_OBSERVER = None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``circuit_breaker_warning_observer_chatter_suppression``
         — record_failure returns None when band unchanged.
      2. ``circuit_breaker_warning_observer_authority_asymmetry``
         — substrate purity.
      3. ``circuit_breaker_warning_observer_composes_canonical_broker``
         — SSE emission via canonical broker.
      4. ``circuit_breaker_warning_observer_reuses_cost_band_taxonomy``
         — composes Slice 5's CostBand enum verbatim (no
         parallel taxonomy).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "circuit_breaker_warning_observer.py"
    )

    def _validate_chatter_suppression(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_method = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "record_failure":
                    target_method = node
                    break
        if target_method is None:
            violations.append(
                "record_failure method missing"
            )
            return tuple(violations)
        has_same_band_early_return = False
        for sub in ast.walk(target_method):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            if not isinstance(test, ast.Compare):
                continue
            if not test.ops or not isinstance(
                test.ops[0], ast.Eq,
            ):
                continue
            for body_stmt in sub.body:
                if isinstance(body_stmt, ast.Return):
                    if (
                        isinstance(body_stmt.value, ast.Constant)
                        and body_stmt.value.value is None
                    ):
                        has_same_band_early_return = True
                        break
        if not has_same_band_early_return:
            violations.append(
                "record_failure MUST contain `if prev == "
                "new: return None` early-return"
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
                            f"circuit_breaker_warning_"
                            f"observer.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    def _validate_composes_canonical_broker(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "StreamEventBroker"
                ):
                    violations.append(
                        "circuit_breaker_warning_observer.py "
                        "MUST NOT construct StreamEventBroker"
                    )
        return tuple(violations)

    def _validate_reuses_cost_band_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Module MUST import CostBand from
        cost_warning_observer (no parallel taxonomy
        construction)."""
        violations: list = []
        has_cost_band_import = False
        has_parallel_band_class = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "cost_warning_observer" in module:
                    for alias in node.names:
                        if alias.name == "CostBand":
                            has_cost_band_import = True
            if isinstance(node, ast.ClassDef):
                if node.name in ("CostBand", "BreakerBand"):
                    if node.name == "CostBand":
                        has_parallel_band_class = True
        if not has_cost_band_import:
            violations.append(
                "circuit_breaker_warning_observer.py MUST "
                "import CostBand from cost_warning_observer "
                "(reuse Slice 5 taxonomy; no duplication)"
            )
        if has_parallel_band_class:
            violations.append(
                "circuit_breaker_warning_observer.py MUST "
                "NOT define a parallel CostBand class"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "circuit_breaker_warning_observer_"
                "chatter_suppression"
            ),
            target_file=target,
            description=(
                "§37 Slice 8 — record_failure emits "
                "BreakerBandCrossing only on band change."
            ),
            validate=_validate_chatter_suppression,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "circuit_breaker_warning_observer_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§37 Slice 8 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "circuit_breaker_warning_observer_"
                "composes_canonical_broker"
            ),
            target_file=target,
            description=(
                "§37 Slice 8 — single-pipeline guardrail."
            ),
            validate=_validate_composes_canonical_broker,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "circuit_breaker_warning_observer_"
                "reuses_cost_band_taxonomy"
            ),
            target_file=target,
            description=(
                "§37 Slice 8 — composes Slice 5's CostBand "
                "enum (no parallel taxonomy)."
            ),
            validate=_validate_reuses_cost_band_taxonomy,
        ),
    ]


__all__ = [
    "BreakerBandCrossing",
    "CIRCUIT_BREAKER_WARNING_OBSERVER_SCHEMA_VERSION",
    "CircuitBreakerWarningObserver",
    "classify_breaker_band",
    "critical_threshold_pct",
    "get_default_observer",
    "notice_threshold_pct",
    "register_shipped_invariants",
    "reset_default_observer_for_tests",
    "warn_threshold_pct",
]
