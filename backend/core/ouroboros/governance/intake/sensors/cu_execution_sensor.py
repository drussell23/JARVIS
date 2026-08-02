"""
CUExecutionSensor — Observes CU (Computer Use) step execution telemetry
and detects recurring failure patterns for Ouroboros self-improvement.

Pillar 6 (Neuroplasticity) + Pillar 7 (Absolute Observability):
  Deterministic: Pattern counting, rolling window, threshold check.
  Agentic: The governance pipeline decides HOW to fix the detected pattern.

Flow:
  ActionDispatcher completes a CU task
    -> calls CUExecutionSensor.record(result)
    -> sensor tracks failure patterns in a rolling window
    -> when a pattern recurs >= GRADUATION_THRESHOLD times,
       emits an IntentEnvelope to the Ouroboros intake router
    -> governance pipeline routes it to a brain for fix generation
    -> fix targets cu_task_planner.py / cu_step_executor.py

This is the organism's nervous system for CU execution quality.
The sensor detects pain; Ouroboros heals the wound.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (env-driven, Manifesto Section 5)
# ---------------------------------------------------------------------------

# How many times a failure pattern must recur before emitting an envelope
_GRADUATION_THRESHOLD = int(
    os.environ.get("JARVIS_CU_FAILURE_THRESHOLD", "3")
)

# Rolling window for pattern tracking (seconds)
_WINDOW_S = float(os.environ.get("JARVIS_CU_FAILURE_WINDOW_S", "86400"))  # 24h

# Cooldown after emitting an envelope for a pattern (avoid spamming)
_EMIT_COOLDOWN_S = float(
    os.environ.get("JARVIS_CU_EMIT_COOLDOWN_S", "3600")
)  # 1 hour


# ---------------------------------------------------------------------------
# Telemetry record
# ---------------------------------------------------------------------------


@dataclass
class CUExecutionRecord:
    """One CU task execution result."""

    goal: str
    success: bool
    steps_completed: int
    steps_total: int
    elapsed_s: float
    error: Optional[str] = None
    is_messaging: bool = False
    contact: Optional[str] = None
    app: Optional[str] = None
    layers_used: Dict[str, int] = field(default_factory=dict)
    antipatterns_blocked: int = 0
    timestamp: float = field(default_factory=time.time)

    @property
    def failure_signature(self) -> str:
        """Unique signature for this failure pattern (dedup key).

        Groups failures by: task type + error category + app context.
        """
        parts = []
        if self.is_messaging:
            parts.append("messaging")
            if self.app:
                parts.append(self.app.lower())
        else:
            # First 3 words of goal for generic tasks
            words = self.goal.split()[:3]
            parts.append("_".join(w.lower() for w in words))

        if self.error:
            # Normalize error to a category
            err = self.error.lower()
            if "target" in err or "not found" in err:
                parts.append("target_miss")
            elif "timeout" in err:
                parts.append("timeout")
            elif "vision" in err or "layer" in err:
                parts.append("vision_fail")
            else:
                parts.append("other")
        else:
            parts.append("partial")

        return ":".join(parts)


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


class CUExecutionSensor:
    """Ouroboros intake sensor for CU execution telemetry.

    Follows the sensor protocol:
      - async start()      — no-op (event-driven, not polling)
      - stop()             — clears state
      - record(result)     — ingest one CU execution result
    """

    _instance: Optional["CUExecutionSensor"] = None

    def __new__(cls, **kwargs: Any) -> "CUExecutionSensor":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        router: Any = None,
        repo: str = "jarvis",
    ) -> None:
        if self._initialized:
            # Allow re-wiring the router after construction.
            #
            # THE BOOT RACE THIS CLOSES. `main.py`'s HUD dispatch reaches this
            # sensor through `get_cu_execution_sensor()`, which constructs the
            # singleton with NO router. `IntakeLayerService` attaches the real
            # router later, during progressive governance boot. A HUD action
            # landing in that window used to reach `_emit_envelope`, find
            # `_router is None`, log a warning and DROP the emission — while
            # the failure window kept the evidence that justified it.
            #
            # Attaching a router is therefore not a field assignment; it is the
            # moment a decision that could not be made becomes makeable. So the
            # attach RECONCILES.
            if router is not None:
                _changed = router is not self._router
                self._router = router
                if _changed:
                    self._schedule_reconcile()
            return
        self._initialized = True
        self._router = router
        self._repo = repo

        # Pattern tracking: signature -> list of timestamps
        self._failure_window: Dict[str, List[float]] = defaultdict(list)
        # Latest record per signature. The window holds TIMESTAMPS only, which
        # is enough to count a pattern and not enough to describe it — an
        # envelope needs the record. Kept under the same lifecycle as the
        # window (pruned together) so it cannot outgrow it.
        self._latest_by_sig: Dict[str, CUExecutionRecord] = {}
        # Track when we last emitted for each signature (cooldown)
        self._last_emitted: Dict[str, float] = {}
        # Total records for observability
        self._total_records = 0
        self._total_failures = 0
        self._total_envelopes_emitted = 0
        # Emissions that qualified while no router was attached. Counted rather
        # than merely logged: "how much did we nearly lose" is the number that
        # says whether this race is rare or routine (Manifesto §7).
        self._deferred_emissions = 0
        self._reconciled_emissions = 0
        # Set when a qualifying pattern was refused for want of a router. The
        # lazy half of the recovery: if no event loop was running at attach
        # time, the next `record()` reconciles instead.
        self._needs_reconcile = False
        self._reconciling = False

        logger.info("[CUExecutionSensor] initialized")

    async def start(self) -> None:
        """No-op — this sensor is event-driven, not polling."""
        logger.info("[CUExecutionSensor] started (event-driven mode)")

    def stop(self) -> None:
        """Clear tracking state."""
        self._failure_window.clear()
        self._latest_by_sig.clear()
        self._last_emitted.clear()
        self._needs_reconcile = False
        logger.info("[CUExecutionSensor] stopped")

    # ------------------------------------------------------------------
    # Router attachment — turning a missed decision into a deferred one
    # ------------------------------------------------------------------

    def _schedule_reconcile(self) -> None:
        """Re-evaluate every live pattern now that a router exists.

        Two paths, because a router can be attached from either world and
        neither may assume the other:

        * **Eager** — `IntakeLayerService` constructs this sensor inside an
          async boot, so a loop is running and the sweep starts immediately.
        * **Lazy** — no running loop (a sync test, a CLI, a boot ordering we
          have not met). A flag is set and the next `record()` reconciles.

        NEVER raises: a failure here must not break governance boot, and the
        lazy path is the fallback for the eager one failing.
        """
        self._needs_reconcile = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[CUExecutionSensor] router attached with no running loop — "
                "reconcile deferred to next record()")
            return
        try:
            loop.create_task(self._reconcile())
        except Exception:  # noqa: BLE001 — the flag still covers us
            logger.debug("[CUExecutionSensor] eager reconcile not scheduled",
                         exc_info=True)

    async def _reconcile(self) -> None:
        """Emit for every pattern that qualified while nothing could receive it.

        Idempotent and cheap: bounded by the number of live signatures, guarded
        against re-entry, and each candidate still passes the ordinary
        threshold and cooldown checks — so a pattern already emitted stays
        silent and a pattern below threshold stays pending. Replaying is
        therefore always safe, which is what lets both the eager and lazy paths
        call it without coordinating.
        """
        if self._reconciling or self._router is None:
            return
        self._reconciling = True
        try:
            self._needs_reconcile = False
            now = time.time()
            # Snapshot: `_maybe_emit` awaits, and the window can be mutated by
            # a concurrent record() while we are suspended.
            for sig in list(self._failure_window.keys()):
                count = self._live_count(sig, now)
                if count < _GRADUATION_THRESHOLD:
                    continue
                rec = self._latest_by_sig.get(sig)
                if rec is None:
                    # Window entry with no record — pre-upgrade state or a
                    # pruned pairing. Cannot describe it honestly, so skip
                    # rather than emit a fabricated envelope.
                    continue
                if await self._maybe_emit(sig, rec, count, deferred=True):
                    self._reconciled_emissions += 1
        except Exception:  # noqa: BLE001
            logger.debug("[CUExecutionSensor] reconcile degraded", exc_info=True)
        finally:
            self._reconciling = False

    def _live_count(self, sig: str, now: float) -> int:
        """Occurrences of *sig* inside the rolling window, pruning in place.

        Pruning here rather than only in `record()` is what makes reconcile
        honest: a pattern that crossed the threshold hours ago must not emit on
        the strength of entries that have since expired.
        """
        cutoff = now - _WINDOW_S
        kept = [t for t in self._failure_window.get(sig, ()) if t > cutoff]
        if kept:
            self._failure_window[sig] = kept
        else:
            # Drop the pairing together so `_latest_by_sig` cannot outlive the
            # window it is indexed by.
            self._failure_window.pop(sig, None)
            self._latest_by_sig.pop(sig, None)
        return len(kept)

    async def _maybe_emit(self, sig: str, rec: "CUExecutionRecord",
                          count: int, *, deferred: bool = False) -> bool:
        """The ONE place that decides whether a pattern emits.

        Extracted from `record()` so the decision can be RE-ENTERED. The bug
        this closes was never lost data — the window kept every occurrence —
        it was that the decision was evaluated exactly once, at a moment chosen
        by a producer with no knowledge of consumer readiness. Making it
        re-entrant is the whole fix; the window is already the buffer.

        Returns True if an envelope was actually ingested.
        """
        if count < _GRADUATION_THRESHOLD:
            return False
        now = time.time()
        last = self._last_emitted.get(sig, 0)
        if now - last < _EMIT_COOLDOWN_S:
            logger.debug(
                "[CUExecutionSensor] Pattern '%s' graduated but in cooldown "
                "(%.0fs remaining)", sig, _EMIT_COOLDOWN_S - (now - last))
            return False
        if self._router is None:
            # Not a failure to emit — a decision that cannot be made YET.
            self._deferred_emissions += 1
            self._needs_reconcile = True
            logger.warning(
                "[CUExecutionSensor] Pattern '%s' qualified (%dx) with no "
                "router attached — emission DEFERRED, will reconcile when "
                "governance boot completes (deferred_total=%d)",
                sig, count, self._deferred_emissions,
            )
            return False
        return await self._emit_envelope(sig, rec, count, deferred=deferred)

    # ------------------------------------------------------------------
    # Public API: called by ActionDispatcher after CU execution
    # ------------------------------------------------------------------

    async def record(self, rec: CUExecutionRecord) -> None:
        """Record a CU execution result.

        On success: logs telemetry.
        On failure: tracks the failure pattern and emits an IntentEnvelope
        when the graduation threshold is reached.
        """
        self._total_records += 1

        if rec.success:
            logger.debug(
                "[CUExecutionSensor] Success: '%s' (%d steps, %.1fs)",
                rec.goal[:60],
                rec.steps_completed,
                rec.elapsed_s,
            )
            return

        self._total_failures += 1
        sig = rec.failure_signature

        # Add to rolling window, keyed on the record's OWN timestamp rather
        # than now(): a record replayed from the HUD's pending-event queue
        # describes when the action failed, not when the IPC reconnected, and
        # a 24h window judged on arrival time would be judging the wrong thing.
        self._failure_window[sig].append(rec.timestamp)
        self._latest_by_sig[sig] = rec

        now = time.time()
        count = self._live_count(sig, now)
        logger.info(
            "[CUExecutionSensor] Failure #%d for pattern '%s': %s",
            count,
            sig,
            rec.error or "partial completion",
        )

        await self._maybe_emit(sig, rec, count)

        # Lazy half of the recovery. If a router arrived while no loop was
        # running — or an earlier pattern was refused for want of one — this
        # is the next moment we are certainly inside the loop, so sweep the
        # OTHER signatures too. Costs one bounded pass and only when something
        # is actually outstanding.
        if self._needs_reconcile and self._router is not None:
            await self._reconcile()

    # ------------------------------------------------------------------
    # Envelope emission
    # ------------------------------------------------------------------

    async def _emit_envelope(
        self,
        signature: str,
        latest: CUExecutionRecord,
        occurrence_count: int,
        *,
        deferred: bool = False,
    ) -> bool:
        """Emit an IntentEnvelope to Ouroboros for a graduated failure pattern.

        Returns True if the router accepted it. *deferred* marks an envelope
        that qualified before a router existed and is only now being sent —
        the evidence says so explicitly, because a late signal that renders
        identically to a fresh one is exactly the provenance failure this
        codebase keeps finding.
        """
        if self._router is None:
            logger.warning(
                "[CUExecutionSensor] No router wired — cannot emit envelope for '%s'",
                signature,
            )
            return False

        description = (
            f"CU execution failure pattern detected ({occurrence_count}x in "
            f"{_WINDOW_S / 3600:.0f}h): {signature}. "
        )

        if latest.is_messaging:
            description += (
                f"Messaging task on {latest.app or 'unknown app'} "
                f"for contact '{latest.contact or 'unknown'}'. "
            )

        if latest.error:
            description += f"Last error: {latest.error}. "

        description += (
            f"Steps completed: {latest.steps_completed}/{latest.steps_total}. "
            f"Goal: '{latest.goal[:80]}'"
        )

        # Target CU retry handler first (small file, fast generation),
        # then planner and executor. The orchestrator uses the first file
        # as primary target for code generation.
        target_files = (
            "backend/vision/cu_retry_handler.py",
            "backend/vision/cu_task_planner.py",
            "backend/vision/cu_step_executor.py",
        )

        evidence = {
            "signature": signature,
            "occurrence_count": occurrence_count,
            "latest_goal": latest.goal,
            "latest_error": latest.error,
            "steps_completed": latest.steps_completed,
            "steps_total": latest.steps_total,
            "is_messaging": latest.is_messaging,
            "contact": latest.contact,
            "app": latest.app,
            "layers_used": latest.layers_used,
            "antipatterns_blocked": latest.antipatterns_blocked,
            "window_hours": _WINDOW_S / 3600,
            "threshold": _GRADUATION_THRESHOLD,
            # Provenance: was this sent when it qualified, or held until a
            # router existed? `age_s` is measured from the occurrence that
            # crossed the threshold, so a postmortem can tell a slow boot from
            # a slow failure.
            "deferred_by_boot": deferred,
            "age_s": round(max(0.0, time.time() - latest.timestamp), 2),
        }

        envelope = make_envelope(
            source="cu_execution",
            description=description,
            target_files=target_files,
            repo=self._repo,
            confidence=min(0.95, 0.5 + occurrence_count * 0.1),
            urgency="high" if occurrence_count >= 5 else "normal",
            evidence=evidence,
            requires_human_ack=False,
        )

        try:
            result = await self._router.ingest(envelope)
            self._last_emitted[signature] = time.time()
            self._total_envelopes_emitted += 1
            logger.info(
                "[CUExecutionSensor] Envelope emitted for '%s' → %s "
                "(count=%d%s)",
                signature,
                result,
                occurrence_count,
                ", DEFERRED-then-reconciled" if deferred else "",
            )
            return True
        except Exception as exc:
            # Deliberately NOT stamping `_last_emitted` on failure: a transient
            # router error must leave the pattern eligible, so the next record
            # or reconcile retries rather than serving a cooldown for an
            # envelope nobody received.
            logger.warning(
                "[CUExecutionSensor] Envelope emission failed for '%s': %s",
                signature,
                exc,
            )
            self._needs_reconcile = True
            return False

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return sensor stats for the telemetry dashboard."""
        return {
            "total_records": self._total_records,
            "total_failures": self._total_failures,
            "total_envelopes_emitted": self._total_envelopes_emitted,
            # The boot-race counters. `deferred_emissions` is how often a
            # pattern qualified with no router attached — it should be small
            # and non-zero on a cold start, and a growing value means
            # governance boot is not completing at all.
            "deferred_emissions": self._deferred_emissions,
            "reconciled_emissions": self._reconciled_emissions,
            "router_attached": self._router is not None,
            "pending_reconcile": self._needs_reconcile,
            "active_patterns": len(self._failure_window),
            "pattern_counts": {
                sig: len(timestamps)
                for sig, timestamps in self._failure_window.items()
            },
        }


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


def get_cu_execution_sensor() -> CUExecutionSensor:
    """Get or create the singleton CUExecutionSensor."""
    return CUExecutionSensor()
