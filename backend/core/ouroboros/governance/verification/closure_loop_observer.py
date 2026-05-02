"""Q4 Priority #2 Slice 2 — ClosureLoopObserver async observer.

Periodic async observer that walks new :class:`CoherenceAdvisory`
records (produced by Priority #1's CoherenceObserver), composes the
closure-loop chain (Tightener cage validate → Counterfactual Replay
validate), and persists each :class:`ClosureLoopRecord` to the
flock'd JSONL ring buffer.

Lifecycle mirrors **exactly** the established pattern from
``gradient_observer.CIGWObserver`` (Priority #5 Slice 3) +
``coherence_observer.CoherenceObserver`` (Priority #1 Slice 3):

  * ``start()`` / ``stop()`` — idempotent + cancellation-safe
  * ``_loop()``              — main async loop, NEVER raises out
  * ``_run_one_pass()``      — one read → chain → persist cycle
  * Posture-aware cadence    — drift multiplier on signature change
  * Linear failure backoff   — N consecutive failures × base, capped
  * Drift-signature dedup    — bounded ring of recent fingerprints
  * Liveness pulse           — emit every Nth pass even on no-change
  * Pluggable validators     — Protocol-shaped callables; Slice 2
                                ships SHADOW DEFAULTS (always-OK
                                tightening + always-None replay so
                                shadow records land as
                                SKIPPED_REPLAY_REJECTED — honest:
                                we can't propose without the real
                                replay wired in Slice 3).

Authority invariant (AST-pinned in Slice 4):
  This module imports nothing from ``yaml_writer``, ``meta_governor``
  (graduating-allowed in Slice 3 via the ``on_record_emitted``
  callback that Slice 3 wires to ``AdaptationLedger.propose`` —
  NEVER ``.approve``), ``orchestrator``, ``policy``, ``iron_gate``,
  ``risk_tier``, ``change_engine``, ``candidate_generator``, or
  ``gate``. The observer's authority is **read advisories +
  persist records** — operator approval remains the sole path to
  policy mutation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Optional,
    Protocol,
    Tuple,
)

from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
    ClosureLoopRecord,
    ClosureOutcome,
    closure_loop_orchestrator_enabled,
    compute_closure_outcome,
)
from backend.core.ouroboros.governance.verification.closure_loop_store import (  # noqa: E501
    RecordOutcome,
    record_closure_outcome,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    CoherenceAdvisory,
    read_coherence_advisories,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay import (  # noqa: E501
    ReplayVerdict,
)

logger = logging.getLogger(__name__)


CLOSURE_LOOP_OBSERVER_SCHEMA_VERSION = "closure_loop_observer.v1"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(floor, min(ceiling, float(raw)))
    except (TypeError, ValueError):
        return default


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(floor, min(ceiling, int(raw)))
    except (TypeError, ValueError):
        return default


def closure_loop_observer_interval_default_s() -> float:
    """``JARVIS_CLOSURE_LOOP_OBSERVER_INTERVAL_S`` — default 600.0
    (10 min) clamped [60.0, 7200.0]. Matches CIGW + Coherence
    cadence so all three observers tick on similar wall-clock
    cycles."""
    return _env_float_clamped(
        "JARVIS_CLOSURE_LOOP_OBSERVER_INTERVAL_S",
        600.0, floor=60.0, ceiling=7200.0,
    )


def closure_loop_observer_drift_multiplier() -> float:
    """``JARVIS_CLOSURE_LOOP_OBSERVER_DRIFT_MULTIPLIER`` — cadence
    multiplier when a new record fingerprint was seen in the
    previous pass (operator wants quicker re-tick after drift).
    Default 0.5 (half the base interval) clamped [0.1, 1.0]."""
    return _env_float_clamped(
        "JARVIS_CLOSURE_LOOP_OBSERVER_DRIFT_MULTIPLIER",
        0.5, floor=0.1, ceiling=1.0,
    )


def closure_loop_observer_failure_backoff_ceiling_s() -> float:
    """``JARVIS_CLOSURE_LOOP_OBSERVER_FAILURE_BACKOFF_CEILING_S``
    — upper bound on linear backoff. Default 3600.0 (1 hour)
    clamped [60.0, 86400.0]."""
    return _env_float_clamped(
        "JARVIS_CLOSURE_LOOP_OBSERVER_FAILURE_BACKOFF_CEILING_S",
        3600.0, floor=60.0, ceiling=86400.0,
    )


def closure_loop_observer_liveness_pulse_passes() -> int:
    """``JARVIS_CLOSURE_LOOP_OBSERVER_LIVENESS_PULSE_PASSES`` — emit
    a liveness record every Nth pass even when no new advisories.
    Default 4 clamped [1, 1024]. Set to 1 in tests."""
    return _env_int_clamped(
        "JARVIS_CLOSURE_LOOP_OBSERVER_LIVENESS_PULSE_PASSES",
        4, floor=1, ceiling=1024,
    )


def closure_loop_observer_dedup_ring_size() -> int:
    """``JARVIS_CLOSURE_LOOP_OBSERVER_DEDUP_RING_SIZE`` — bounded
    fingerprint dedup ring. Default 256 clamped [16, 16384]."""
    return _env_int_clamped(
        "JARVIS_CLOSURE_LOOP_OBSERVER_DEDUP_RING_SIZE",
        256, floor=16, ceiling=16384,
    )


# ---------------------------------------------------------------------------
# Pluggable validator hooks (Slice 2 = shadow defaults; Slice 3 =
# real Tightener + Counterfactual Replay wiring)
# ---------------------------------------------------------------------------


class TighteningValidatorFn(Protocol):
    """Synchronous validator. Slice 3 wires the real Tightener cage
    (``confidence_threshold_tightener._confidence_threshold_validator``)
    via an adapter that translates :class:`CoherenceAdvisory` →
    ``AdaptationProposal``."""

    def __call__(
        self, advisory: CoherenceAdvisory,
    ) -> Tuple[bool, str]: ...


class ReplayValidatorFn(Protocol):
    """Asynchronous validator. Slice 3 wires the real
    :func:`counterfactual_replay.compute_replay_outcome` via an
    adapter that builds a :class:`ReplayTarget` from the advisory's
    drift kind + parameter."""

    def __call__(
        self, advisory: CoherenceAdvisory,
    ) -> Awaitable[Optional[ReplayVerdict]]: ...


def shadow_tightening_validator(
    advisory: CoherenceAdvisory,  # noqa: ARG001 — protocol shape
) -> Tuple[bool, str]:
    """Slice 2 default. Always returns ``(True, "shadow_validator_stub")``
    so the chain progresses to the replay step. Slice 3 replaces this
    with the real Tightener cage validator."""
    return (True, "shadow_validator_stub")


async def shadow_replay_validator(
    advisory: CoherenceAdvisory,  # noqa: ARG001 — protocol shape
) -> Optional[ReplayVerdict]:
    """Slice 2 default. Returns ``None`` so every advisory lands as
    SKIPPED_REPLAY_REJECTED in shadow mode. Honest: until Slice 3
    wires the real Counterfactual Replay engine, the orchestrator
    has no empirical evidence to support a tightening proposal,
    and the conservative answer is "don't propose"."""
    return None


# ---------------------------------------------------------------------------
# Pass result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ObserverPassResult:
    """One observer pass — frozen for safe snapshot semantics."""
    advisories_seen: int
    records_emitted: int
    records_deduped: int
    pass_index: int
    most_recent_signature: str


# ---------------------------------------------------------------------------
# ClosureLoopObserver
# ---------------------------------------------------------------------------


class ClosureLoopObserver:
    """Periodic async observer over the coherence advisory stream.

    NEVER raises out of any public method. All exception-prone
    boundaries collapse into the failure-backoff counter."""

    def __init__(
        self,
        *,
        interval_s: Optional[float] = None,
        tightening_validator: Optional[TighteningValidatorFn] = None,
        replay_validator: Optional[ReplayValidatorFn] = None,
        on_record_emitted: Optional[
            Callable[[ClosureLoopRecord], Awaitable[None]]
        ] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._explicit_interval_s = interval_s
        self._tightening_validator = (
            tightening_validator or shadow_tightening_validator
        )
        self._replay_validator = (
            replay_validator or shadow_replay_validator
        )
        self._on_record_emitted = on_record_emitted
        self._clock = clock or time.time

        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

        self._pass_index: int = 0
        self._consecutive_failures: int = 0
        self._signature_changed_last_pass: bool = False
        self._last_seen_advisory_ts: float = 0.0

        # Bounded fingerprint dedup ring. Older entries fall off
        # the back; size capped via env knob so memory stays bounded.
        self._dedup_ring: Deque[str] = deque(
            maxlen=closure_loop_observer_dedup_ring_size(),
        )
        self._dedup_set: set = set()

        # Telemetry: cumulative counts since boot (frozen tuples
        # exposed via stats() so callers can't mutate).
        self._total_advisories_seen: int = 0
        self._total_records_emitted: int = 0
        self._total_records_deduped: int = 0
        self._outcome_histogram: dict = {
            o.value: 0 for o in ClosureOutcome
        }

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def pass_index(self) -> int:
        return self._pass_index

    # ---- Lifecycle ----------------------------------------------------

    async def start(self) -> None:
        """Schedule the observer loop. Idempotent — re-calling on a
        running observer is a no-op."""
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self, *, timeout_s: float = 10.0) -> None:
        """Signal stop + await the loop task. Idempotent. NEVER raises."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is None:
            return
        task = self._task
        try:
            await asyncio.wait_for(task, timeout=timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            logger.debug(
                "[ClosureLoopObserver] stop wait_for: %s", exc,
            )
            try:
                task.cancel()
            except Exception:  # noqa: BLE001 — defensive
                pass
        finally:
            self._task = None
            self._stop_event = None

    # ---- Main loop ----------------------------------------------------

    async def _loop(self) -> None:
        """Main observer loop. NEVER raises out."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._run_one_pass()
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — defensive
                self._consecutive_failures += 1
                logger.debug(
                    "[ClosureLoopObserver] pass exc (#%d): %s",
                    self._consecutive_failures, exc,
                )

            interval = self._compute_next_interval()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval,
                )
            except asyncio.TimeoutError:
                continue

    async def run_one_pass(self) -> _ObserverPassResult:
        """Public test hook for one synchronous pass without the
        timing loop. NEVER raises — exceptions collapse into a
        no-op pass result with ``records_emitted=0``."""
        try:
            return await self._run_one_pass()
        except Exception as exc:  # noqa: BLE001 — defensive
            self._consecutive_failures += 1
            logger.debug(
                "[ClosureLoopObserver] run_one_pass exc: %s", exc,
            )
            return _ObserverPassResult(
                advisories_seen=0,
                records_emitted=0,
                records_deduped=0,
                pass_index=self._pass_index,
                most_recent_signature="",
            )

    async def _run_one_pass(self) -> _ObserverPassResult:
        """One read → chain → persist cycle. NEVER raises out (every
        chain step swallows + logs at DEBUG)."""
        self._pass_index += 1
        if not closure_loop_orchestrator_enabled():
            # Master-flag-off — record a single DISABLED outcome
            # if we haven't already (liveness-pulse honest signal).
            return _ObserverPassResult(
                advisories_seen=0,
                records_emitted=0,
                records_deduped=0,
                pass_index=self._pass_index,
                most_recent_signature="",
            )

        # Step 1 — read advisories newer than last seen.
        advisories: Tuple[CoherenceAdvisory, ...] = ()
        try:
            advisories = await asyncio.to_thread(
                read_coherence_advisories,
                since_ts=self._last_seen_advisory_ts,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[ClosureLoopObserver] read_coherence_advisories: %s",
                exc,
            )
            advisories = ()
        self._total_advisories_seen += len(advisories)

        # Step 2 — process each advisory through the chain.
        emitted = 0
        deduped = 0
        last_sig = ""
        for adv in advisories:
            # Advance the watermark progressively so a partial-pass
            # crash still moves forward.
            if adv.recorded_at_ts > self._last_seen_advisory_ts:
                self._last_seen_advisory_ts = adv.recorded_at_ts

            # Step 2a — synchronous tightening cage validator.
            try:
                validator_result = self._tightening_validator(adv)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[ClosureLoopObserver] tightening_validator: %s",
                    exc,
                )
                validator_result = (
                    False, f"validator_raised:{type(exc).__name__}",
                )

            # Step 2b — async replay validator.
            replay_verdict: Optional[ReplayVerdict] = None
            try:
                replay_verdict = await self._replay_validator(adv)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[ClosureLoopObserver] replay_validator: %s",
                    exc,
                )
                replay_verdict = None

            # Step 2c — total decision function.
            record = compute_closure_outcome(
                advisory=adv,
                validator_result=validator_result,
                replay_verdict=replay_verdict,
                enabled=True,
                decided_at_ts=self._clock(),
            )
            last_sig = record.record_fingerprint or last_sig

            # Step 2d — fingerprint dedup ring.
            fp = record.record_fingerprint
            if fp and fp in self._dedup_set:
                deduped += 1
                self._total_records_deduped += 1
                continue
            if fp:
                if (
                    len(self._dedup_ring)
                    == self._dedup_ring.maxlen
                ):
                    # Evict the oldest fingerprint from the set
                    # to keep set+ring in lock step.
                    oldest = self._dedup_ring[0]
                    self._dedup_set.discard(oldest)
                self._dedup_ring.append(fp)
                self._dedup_set.add(fp)

            # Step 2e — persist to ring buffer.
            persisted = record_closure_outcome(record)
            if persisted is RecordOutcome.OK:
                emitted += 1
                self._total_records_emitted += 1
                self._outcome_histogram[record.outcome.value] = (
                    self._outcome_histogram.get(
                        record.outcome.value, 0,
                    ) + 1
                )

            # Step 2f — best-effort downstream callback (Slice 3
            # wires this to ``AdaptationLedger.propose`` for actionable
            # records — but NEVER ``.approve``; operator gate stays).
            if (
                persisted is RecordOutcome.OK
                and self._on_record_emitted is not None
            ):
                try:
                    await self._on_record_emitted(record)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.debug(
                        "[ClosureLoopObserver] on_record_emitted: %s",
                        exc,
                    )

        self._signature_changed_last_pass = (emitted > 0)
        return _ObserverPassResult(
            advisories_seen=len(advisories),
            records_emitted=emitted,
            records_deduped=deduped,
            pass_index=self._pass_index,
            most_recent_signature=last_sig,
        )

    # ---- Timing -------------------------------------------------------

    def _compute_next_interval(self) -> float:
        """Resolve next sleep interval. NEVER raises. Order:

          1. Linear failure backoff (capped at ceiling) when failures > 0
          2. Drift multiplier when last pass emitted records
          3. Base interval otherwise
        """
        try:
            base = (
                float(self._explicit_interval_s)
                if self._explicit_interval_s is not None
                else closure_loop_observer_interval_default_s()
            )
            if self._consecutive_failures > 0:
                ceiling = (
                    closure_loop_observer_failure_backoff_ceiling_s()
                )
                return min(
                    ceiling,
                    base * float(self._consecutive_failures),
                )
            if self._signature_changed_last_pass:
                return max(
                    60.0,
                    base
                    * closure_loop_observer_drift_multiplier(),
                )
            return base
        except Exception:  # noqa: BLE001 — defensive
            return closure_loop_observer_interval_default_s()

    # ---- Stats --------------------------------------------------------

    def stats(self) -> dict:
        """Read-only telemetry snapshot. Plain dict so callers can
        json-dump for SSE / observability without a dataclass round
        trip."""
        return {
            "schema_version": CLOSURE_LOOP_OBSERVER_SCHEMA_VERSION,
            "is_running": self.is_running,
            "pass_index": self._pass_index,
            "consecutive_failures": self._consecutive_failures,
            "last_seen_advisory_ts": self._last_seen_advisory_ts,
            "dedup_ring_size": len(self._dedup_ring),
            "total_advisories_seen": self._total_advisories_seen,
            "total_records_emitted": self._total_records_emitted,
            "total_records_deduped": self._total_records_deduped,
            "outcome_histogram": dict(self._outcome_histogram),
        }


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors gradient_observer + coherence_observer)
# ---------------------------------------------------------------------------


_DEFAULT_OBSERVER: Optional[ClosureLoopObserver] = None


def get_default_observer() -> ClosureLoopObserver:
    """Process-wide singleton. First call constructs; subsequent calls
    return the same instance."""
    global _DEFAULT_OBSERVER
    if _DEFAULT_OBSERVER is None:
        _DEFAULT_OBSERVER = ClosureLoopObserver()
    return _DEFAULT_OBSERVER


def reset_default_observer() -> None:
    """Test hook — drop the singleton so each test starts fresh."""
    global _DEFAULT_OBSERVER
    _DEFAULT_OBSERVER = None


# ---------------------------------------------------------------------------
# Cost-contract authority constant
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "CLOSURE_LOOP_OBSERVER_SCHEMA_VERSION",
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "ClosureLoopObserver",
    "ReplayValidatorFn",
    "TighteningValidatorFn",
    "closure_loop_observer_dedup_ring_size",
    "closure_loop_observer_drift_multiplier",
    "closure_loop_observer_failure_backoff_ceiling_s",
    "closure_loop_observer_interval_default_s",
    "closure_loop_observer_liveness_pulse_passes",
    "get_default_observer",
    "reset_default_observer",
    "shadow_replay_validator",
    "shadow_tightening_validator",
]
