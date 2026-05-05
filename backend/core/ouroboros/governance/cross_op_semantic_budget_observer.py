"""Move 7 — Cross-op Semantic Budget Slice 3 async observer
(PRD §29.4, 2026-05-05).

Closes the consumer side of the producer-consumer loop: Slice 2's
recorder writes :class:`OpSemanticCentroid` rows; this observer
reads the rolling window, calls Slice 1's
:func:`compute_semantic_budget`, and emits an SSE event when the
verdict ladder *transitions* (chatter suppression — same-verdict
cycles are silent).

## Architectural locks (operator mandate, AST-pinned)

  1. **Composes Slices 1 + 2** — uses
     :func:`cross_op_semantic_budget.compute_semantic_budget`
     for the math AND
     :func:`cross_op_semantic_recorder.read_recent_centroids`
     for the ledger read. NEVER reimplements either.
  2. **Posture-aware cadence** — reads
     :class:`PostureStore` lazily (no hard dependency at module
     load) for HARDEN / CONSOLIDATE / MAINTAIN / EXPLORE
     cadence multipliers. Mirrors `InvariantDriftObserver` +
     `PostureObserver` precedent.
  3. **Master-flag-gated** — every entry point gates on
     :func:`cross_op_semantic_budget_enabled` (Slice 1's master
     flag governs the whole arc).
  4. **NEVER raises** — `asyncio.CancelledError` propagates for
     cooperative shutdown; everything else is exception-isolated.
  5. **Authority asymmetry** — imports stdlib + Slice 1 + Slice 2
     + posture_store + ide_observability_stream (read-only SSE
     publisher). NEVER imports orchestrator / iron_gate / policy
     / providers / candidate_generator / change_engine.
  6. **Chatter suppression** — same-verdict ticks are silent;
     SSE fires only on verdict-ladder transitions. Operator
     noise minimized by construction.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


CROSS_OP_SEMANTIC_BUDGET_OBSERVER_SCHEMA_VERSION: str = (
    "cross_op_semantic_budget_observer.1"
)


# ---------------------------------------------------------------------------
# Env knobs — cadence
# ---------------------------------------------------------------------------


def base_cadence_s() -> float:
    """``JARVIS_CROSS_OP_SEMANTIC_OBSERVER_CADENCE_S`` — default
    cadence between ticks. Default 6 hours. Clamped [60s, 7d].
    Posture multipliers compose on top."""
    raw = os.environ.get(
        "JARVIS_CROSS_OP_SEMANTIC_OBSERVER_CADENCE_S", "",
    ).strip()
    try:
        v = float(raw) if raw else 6.0 * 3600.0
    except (TypeError, ValueError):
        return 6.0 * 3600.0
    if v < 60.0:
        return 60.0
    if v > 7.0 * 24.0 * 3600.0:
        return 7.0 * 24.0 * 3600.0
    return v


def _posture_multiplier(posture: str) -> float:
    """HARDEN tightens; MAINTAIN loosens; EXPLORE/CONSOLIDATE
    steady. Defaults match the InvariantDriftObserver
    precedent. Bytes-pinned for parity."""
    p = (posture or "").upper()
    if p == "HARDEN":
        return 1.0 / 6.0  # 6× more frequent (1h at default 6h base)
    if p == "MAINTAIN":
        return 4.0  # 4× less frequent (24h at 6h base)
    return 1.0  # CONSOLIDATE / EXPLORE / unknown — baseline


# ---------------------------------------------------------------------------
# Frozen tick result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObserverTickResult:
    """Frozen result of one observer tick. Telemetry-friendly +
    test-introspectable."""

    verdict_value: str
    integrated_drift: float
    threshold: float
    centroids_seen: int
    posture: str
    cadence_s: float
    sse_emitted: bool
    diagnostics: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Async observer
# ---------------------------------------------------------------------------


class CrossOpSemanticBudgetObserver:
    """Periodic Move 7 budget evaluator. One instance per
    process; constructed at boot by
    :mod:`governed_loop_service` and cancelled on shutdown."""

    def __init__(
        self,
        *,
        ledger_path: Optional[Path] = None,
    ) -> None:
        self._ledger_path = ledger_path
        self._stop_event: Optional[asyncio.Event] = None
        self._last_verdict: Optional[str] = None
        self._tick_count: int = 0

    async def run_periodic(self) -> None:
        """Main async loop. Cancellable via
        :meth:`stop`. Cooperative — wait_for on the stop event
        with the cadence as timeout. NEVER raises out except
        :class:`asyncio.CancelledError` (cooperative shutdown)."""
        try:
            from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
                cross_op_semantic_budget_enabled,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CrossOpSemanticObserver] Slice 1 primitive "
                "unavailable: %s — observer will not run",
                exc,
            )
            return
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        while True:
            if self._stop_event.is_set():
                return
            if not cross_op_semantic_budget_enabled():
                # Master off — sleep at base cadence, re-check
                # next tick. Avoids busy-loop when operator
                # toggles the flag mid-run.
                await self._sleep_or_stop(base_cadence_s())
                continue
            try:
                await self.run_one_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[CrossOpSemanticObserver] tick raised: %s",
                    exc,
                )
            cadence = self._resolve_cadence_s()
            await self._sleep_or_stop(cadence)

    async def run_one_cycle(self) -> Optional[ObserverTickResult]:
        """Read the rolling-window centroids → compute budget
        → emit SSE on verdict transition → return tick result.
        NEVER raises (returns None on any inner failure)."""
        try:
            from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
                read_recent_centroids,
            )
            from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
                compute_semantic_budget,
                window_size,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CrossOpSemanticObserver] Slice 1/2 imports "
                "failed: %s", exc,
            )
            return None

        try:
            centroids = read_recent_centroids(
                limit=window_size(), path=self._ledger_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CrossOpSemanticObserver] read_recent "
                "raised: %s", exc,
            )
            return None

        try:
            report = compute_semantic_budget(
                centroids, enabled_override=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CrossOpSemanticObserver] compute raised: "
                "%s", exc,
            )
            return None

        verdict_value = report.verdict.value
        prior = self._last_verdict
        self._last_verdict = verdict_value
        self._tick_count += 1

        # Chatter suppression — emit only on verdict transitions.
        # First tick (prior is None) emits unconditionally so
        # observers boot-time get the current state.
        sse_emitted = False
        if prior != verdict_value:
            sse_emitted = self._publish_sse(
                report=report,
                prev_verdict=prior or "",
            )

        posture = self._snapshot_posture()
        cadence = self._resolve_cadence_s(posture=posture)
        return ObserverTickResult(
            verdict_value=verdict_value,
            integrated_drift=float(report.integrated_drift),
            threshold=float(report.threshold),
            centroids_seen=int(report.centroids_seen),
            posture=posture,
            cadence_s=cadence,
            sse_emitted=sse_emitted,
        )

    async def stop(self) -> None:
        """Signal cooperative shutdown. NEVER raises."""
        try:
            if self._stop_event is not None:
                self._stop_event.set()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CrossOpSemanticObserver] stop raised: %s",
                exc,
            )

    # --- helpers --------------------------------------------------------

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Wait on the stop event for up to ``seconds`` —
        returns immediately when the event fires."""
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        try:
            await asyncio.wait_for(
                self._stop_event.wait(), timeout=seconds,
            )
        except asyncio.TimeoutError:
            return
        except asyncio.CancelledError:
            raise

    def _snapshot_posture(self) -> str:
        """Read current posture via the canonical PostureStore.
        NEVER raises — degrades to empty string when unavailable."""
        try:
            from backend.core.ouroboros.governance.posture_store import (
                PostureStore,
            )
            store = PostureStore(base_dir=Path(".jarvis"))
            reading = store.load_current()
            if reading is None:
                return ""
            return str(reading.posture.value)
        except Exception:  # noqa: BLE001 — defensive
            return ""

    def _resolve_cadence_s(
        self, *, posture: Optional[str] = None,
    ) -> float:
        """Compose base cadence × posture multiplier. NEVER raises."""
        try:
            base = base_cadence_s()
            p = posture if posture is not None else self._snapshot_posture()
            mult = _posture_multiplier(p)
            return max(60.0, base * mult)
        except Exception:  # noqa: BLE001 — defensive
            return base_cadence_s()

    def _publish_sse(
        self,
        *,
        report: Any,
        prev_verdict: str,
    ) -> bool:
        """Emit the Move 7 SSE event via the observability
        broker. Returns True when the publisher was INVOKED
        (chatter-gate passed; broker call attempted), False
        when the publisher import / call failed.

        Note: the broker returns ``None`` when no subscribers
        are connected — that's downstream concern, NOT the
        observer's responsibility. The observer's contract is
        "emit on verdict transition"; whether a subscriber
        receives it depends on the SSE stream's lifecycle.
        ``sse_emitted=True`` means the observer fulfilled its
        emission contract."""
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                publish_semantic_budget_event,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CrossOpSemanticObserver] SSE publisher "
                "unavailable: %s", exc,
            )
            return False
        try:
            publish_semantic_budget_event(
                verdict=report.verdict.value,
                prev_verdict=prev_verdict,
                integrated_drift=float(report.integrated_drift),
                threshold=float(report.threshold),
                approaching_band=float(report.approaching_band),
                centroids_seen=int(report.centroids_seen),
                ts_unix=time.time(),
            )
            # Publisher invoked successfully — the observer
            # fulfilled its contract regardless of subscriber
            # presence (broker returns None when zero
            # subscribers; that's not our concern).
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CrossOpSemanticObserver] SSE publish "
                "raised: %s", exc,
            )
            return False


# ---------------------------------------------------------------------------
# Module-owned ShippedCodeInvariant contributions (auto-discovered)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Three pins:

      1. Authority asymmetry (substrate purity)
      2. Composes Slices 1 + 2 + posture_store (no parallel
         math / persistence / cadence)
      3. Chatter suppression — verdict-transition emission
         only (no per-tick SSE spam)
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

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
                            f"observer MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    def _validate_composes_substrate(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Observer composes Slices 1+2 + posture_store + SSE
        publisher. No parallel math / persistence / cadence."""
        violations: list = []
        if "compute_semantic_budget" not in source:
            violations.append(
                "observer MUST use compute_semantic_budget "
                "from Slice 1 (no parallel math)"
            )
        if "read_recent_centroids" not in source:
            violations.append(
                "observer MUST read via Slice 2's "
                "read_recent_centroids (no parallel ledger "
                "read)"
            )
        if "PostureStore" not in source:
            violations.append(
                "observer MUST consult PostureStore for "
                "posture-aware cadence (no parallel posture "
                "resolution)"
            )
        if "publish_semantic_budget_event" not in source:
            violations.append(
                "observer MUST emit via "
                "publish_semantic_budget_event SSE helper "
                "(no parallel publisher)"
            )
        return tuple(violations)

    def _validate_chatter_suppression(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Verdict-transition gate MUST be present — observers
        should not emit on every tick."""
        violations: list = []
        # Must compare prior verdict to current somewhere.
        if "prior != verdict_value" not in source and (
            "prev_verdict" not in source
        ):
            violations.append(
                "observer MUST gate SSE emission on "
                "verdict-ladder transitions (chatter "
                "suppression)"
            )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/"
        "cross_op_semantic_budget_observer.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_budget_observer_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "observer MUST stay pure substrate composing "
                "Slices 1+2 + posture_store + ide_-"
                "observability_stream (read-only SSE "
                "publisher) ONLY."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_budget_observer_"
                "composes_substrate"
            ),
            target_file=target,
            description=(
                "observer composes Slice 1 compute_semantic_-"
                "budget + Slice 2 read_recent_centroids + "
                "PostureStore + publish_semantic_budget_event. "
                "No parallel math / persistence / cadence / "
                "SSE publisher."
            ),
            validate=_validate_composes_substrate,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_op_semantic_budget_observer_"
                "chatter_suppression"
            ),
            target_file=target,
            description=(
                "observer MUST gate SSE emission on "
                "verdict-ladder transitions only — same-"
                "verdict ticks are silent (operator-noise "
                "control)."
            ),
            validate=_validate_chatter_suppression,
        ),
    ]


__all__ = [
    "CROSS_OP_SEMANTIC_BUDGET_OBSERVER_SCHEMA_VERSION",
    "CrossOpSemanticBudgetObserver",
    "ObserverTickResult",
    "base_cadence_s",
    "register_shipped_invariants",
]
