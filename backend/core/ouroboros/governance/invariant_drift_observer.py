"""Move 4 Slice 3 — InvariantDriftObserver: continuous re-validation.

Slice 1 ships the pure compare primitive; Slice 2 persists a baseline
across boots. Slice 3 closes the *temporal* loop: between boot and
shutdown the organism adapts (Pass C surface miners propose
tightenings, operators approve patches via ``/adapt approve``, env
knobs flip via REPL). Without continuous drift detection, regressions
accumulate silently for hours.

The observer runs in an async task at a posture-aware cadence,
periodically re-captures a snapshot, compares against the on-disk
baseline, and emits drift signals to a pluggable sink. Slice 4 wires
the ``auto_action_router`` bridge as the production sink.

Design pillars (per the directive):

  * **Asynchronous** — ``asyncio.Event``-driven shutdown,
    ``asyncio.wait_for`` cadence pattern. Mirrors ``PostureObserver``
    exactly — same playbook, no duplication of the lifecycle skeleton.

  * **Dynamic** — cadence varies with current posture. HARDEN
    tightens (more vigilant when under pressure); EXPLORE loosens
    (calm exploration). All multipliers env-configurable
    (``JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS`` JSON override).

  * **Adaptive** — vigilance escalation: drift detected → next K
    cycles use ``cadence × vigilance_factor``. After K consecutive
    no-drift cycles, decay back to baseline cadence. Mirrors the
    ``SensorGovernor`` emergency-brake pattern.

  * **Intelligent** — drift signature ring de-duplication. If the
    SAME drift signature appears in N consecutive cycles, the
    observer emits ONE signal, not N. Operator-noise control;
    drift records still appended to history.

  * **Robust** — defensive everywhere. Capture failure → linear
    backoff up to a ceiling. Emitter raises → swallow + log.
    Cancel-safe shutdown. Never raises out of any public method
    except cooperative ``CancelledError``.

  * **No hardcoding** — every cadence, threshold, and multiplier
    has an env knob with a sensible default. Defaults are
    operator-overridable; nothing magic-constant in behavior logic.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + invariant_drift_auditor + invariant_drift_store
    + posture_observer (read-only ``get_default_store`` for cadence
    multiplier — same precedent as Slice 1's ``_capture_posture``).
  * NO orchestrator / phase_runners / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / auto_action_router / subagent_scheduler
    imports.
  * Master-flag-gated. Default off until Slice 5 graduation.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Optional,
    Tuple,
)

from backend.core.ouroboros.governance.invariant_drift_auditor import (
    InvariantDriftRecord,
    InvariantSnapshot,
    capture_snapshot,
    compare_snapshots,
    invariant_drift_auditor_enabled,
)
from backend.core.ouroboros.governance.invariant_drift_store import (
    InvariantDriftStore,
    get_default_store,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env knobs — all overridable; defaults are operator-tunable, not
# hardcoded behavior constants.
# ---------------------------------------------------------------------------


_DEFAULT_INTERVAL_S = 600.0  # 10 min cadence at baseline
_INTERVAL_FLOOR_S = 30.0
_DEFAULT_VIGILANCE_TICKS = 3
_VIGILANCE_TICKS_FLOOR = 1
_DEFAULT_VIGILANCE_FACTOR = 0.5
_VIGILANCE_FACTOR_FLOOR = 0.05
_VIGILANCE_FACTOR_CEILING = 1.0
_DEFAULT_BACKOFF_CEILING_S = 1800.0  # 30 min max backoff
_BACKOFF_CEILING_FLOOR_S = 60.0
_DEFAULT_DEDUP_WINDOW = 5
_DEDUP_WINDOW_FLOOR = 1


def observer_enabled() -> bool:
    """``JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED`` (**graduated
    2026-04-30 Slice 5 — default ``true``**).

    Asymmetric semantics: empty/whitespace = unset = current default
    (post-graduation = ``true``); explicit ``0`` / ``false`` / ``no``
    / ``off`` hot-reverts."""
    raw = os.environ.get(
        "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default — Slice 5
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def base_interval_s() -> float:
    """``JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S`` (default 600s,
    floor 30s)."""
    return _env_float(
        "JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S",
        _DEFAULT_INTERVAL_S,
        minimum=_INTERVAL_FLOOR_S,
    )


def vigilance_ticks() -> int:
    """``JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_TICKS`` (default 3,
    floor 1).

    Number of subsequent cycles to maintain tightened cadence after
    detecting drift."""
    return _env_int(
        "JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_TICKS",
        _DEFAULT_VIGILANCE_TICKS,
        minimum=_VIGILANCE_TICKS_FLOOR,
    )


def vigilance_factor() -> float:
    """``JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_FACTOR`` (default
    0.5, range (0.05, 1.0]). Multiplier applied to cadence during
    vigilance window; ``0.5`` halves the interval (doubles the
    frequency)."""
    raw = _env_float(
        "JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_FACTOR",
        _DEFAULT_VIGILANCE_FACTOR,
        minimum=_VIGILANCE_FACTOR_FLOOR,
    )
    return min(_VIGILANCE_FACTOR_CEILING, raw)


def backoff_ceiling_s() -> float:
    """``JARVIS_INVARIANT_DRIFT_OBSERVER_BACKOFF_CEILING_S`` (default
    1800s = 30min, floor 60s). Maximum interval the observer will
    sleep when capture is consistently failing."""
    return _env_float(
        "JARVIS_INVARIANT_DRIFT_OBSERVER_BACKOFF_CEILING_S",
        _DEFAULT_BACKOFF_CEILING_S,
        minimum=_BACKOFF_CEILING_FLOOR_S,
    )


def dedup_window() -> int:
    """``JARVIS_INVARIANT_DRIFT_OBSERVER_DEDUP_WINDOW`` (default 5,
    floor 1). Number of recent drift signatures to remember for
    de-duplication."""
    return _env_int(
        "JARVIS_INVARIANT_DRIFT_OBSERVER_DEDUP_WINDOW",
        _DEFAULT_DEDUP_WINDOW,
        minimum=_DEDUP_WINDOW_FLOOR,
    )


# Default posture multipliers — operator-overridable, not magic.
# HARDEN tightens; EXPLORE loosens; CONSOLIDATE/MAINTAIN steady.
_DEFAULT_POSTURE_MULTIPLIERS: Dict[str, float] = {
    "EXPLORE": 1.5,
    "CONSOLIDATE": 1.0,
    "HARDEN": 0.5,
    "MAINTAIN": 1.2,
}


def posture_multipliers() -> Dict[str, float]:
    """``JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS`` (JSON object).

    Maps posture string (e.g., ``"HARDEN"``) → cadence multiplier.
    Missing postures fall back to ``1.0``. Malformed JSON is
    silently ignored (defaults are used). NEVER raises."""
    raw = os.environ.get(
        "JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS", "",
    ).strip()
    if not raw:
        return dict(_DEFAULT_POSTURE_MULTIPLIERS)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "[InvariantDriftObserver] posture multiplier env is not "
            "valid JSON; using defaults",
        )
        return dict(_DEFAULT_POSTURE_MULTIPLIERS)
    if not isinstance(parsed, dict):
        return dict(_DEFAULT_POSTURE_MULTIPLIERS)
    out = dict(_DEFAULT_POSTURE_MULTIPLIERS)
    for k, v in parsed.items():
        try:
            out[str(k).upper()] = float(v)
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Pluggable signal emitter (Move 3 pattern)
# ---------------------------------------------------------------------------


class InvariantDriftSignalEmitter:
    """Pluggable sink for drift signals. Slice 4 will register the
    auto_action_router bridge; tests can register a capturing fake;
    Slice 5 default is a no-op until graduation.

    Implementations MUST NOT raise — observer will swallow, but
    a clean implementation never invites that."""

    def emit(
        self,
        snapshot: InvariantSnapshot,
        drift_records: Tuple[InvariantDriftRecord, ...],
    ) -> None:
        raise NotImplementedError


class _NoopEmitter(InvariantDriftSignalEmitter):
    """Default sink — discards drift signals. Active until Slice 4
    wires the auto_action_router bridge."""

    def emit(
        self,
        snapshot: InvariantSnapshot,
        drift_records: Tuple[InvariantDriftRecord, ...],
    ) -> None:
        pass


_default_emitter: InvariantDriftSignalEmitter = _NoopEmitter()
_default_emitter_lock = threading.Lock()


def register_signal_emitter(
    emitter: InvariantDriftSignalEmitter,
) -> None:
    """Install a process-global emitter. Mirrors Move 3's
    ``register_post_postmortem_observer`` pattern. Idempotent on
    identical instance; replaces on differing instance."""
    global _default_emitter
    if not isinstance(emitter, InvariantDriftSignalEmitter):
        logger.warning(
            "[InvariantDriftObserver] register_signal_emitter "
            "rejected non-InvariantDriftSignalEmitter instance",
        )
        return
    with _default_emitter_lock:
        _default_emitter = emitter


def get_signal_emitter() -> InvariantDriftSignalEmitter:
    """Return the current process-global emitter."""
    with _default_emitter_lock:
        return _default_emitter


def reset_signal_emitter() -> None:
    """Restore the no-op default. Test isolation primitive."""
    global _default_emitter
    with _default_emitter_lock:
        _default_emitter = _NoopEmitter()


# ---------------------------------------------------------------------------
# ObserverTickResult — observability, also tests' return shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObserverTickResult:
    """Outcome of one ``run_one_cycle`` call. Frozen for safe
    propagation."""

    captured: Optional[InvariantSnapshot]
    drift_records: Tuple[InvariantDriftRecord, ...]
    emitted: bool
    deduped: bool
    failure_reason: Optional[str]


# ---------------------------------------------------------------------------
# Observer — async lifecycle, posture-aware cadence, adaptive vigilance
# ---------------------------------------------------------------------------


class InvariantDriftObserver:
    """Periodic capture+compare+emit. Mirrors ``PostureObserver``
    lifecycle exactly: ``start()`` spawns the task; ``stop()`` cancels
    cooperatively; ``run_one_cycle()`` is public for tests with no
    sleep.

    Injectable dependencies (tests pass their own; production passes
    none and gets defaults):

      * ``store``           — required ``InvariantDriftStore``
      * ``emitter``         — defaults to the process-global registry
      * ``capture``         — defaults to ``capture_snapshot`` from
                              the auditor
      * ``posture_reader``  — callable returning current posture
                              string or None; defaults to reading
                              ``PostureStore.load_current()``"""

    def __init__(
        self,
        store: InvariantDriftStore,
        *,
        emitter: Optional[InvariantDriftSignalEmitter] = None,
        capture: Optional[
            Callable[[], InvariantSnapshot]
        ] = None,
        posture_reader: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self._store = store
        self._emitter_override = emitter
        self._capture = capture or capture_snapshot
        self._posture_reader = (
            posture_reader or self._default_posture_reader
        )
        self._task: Optional[asyncio.Task[Any]] = None
        self._stop_event = asyncio.Event()
        # Counters
        self._cycles_ok = 0
        self._cycles_failed = 0
        self._signals_emitted = 0
        self._signals_deduped = 0
        self._consecutive_failures = 0
        self._vigilance_ticks_remaining = 0
        # Drift signature dedup ring — bounded.
        self._recent_signatures: Deque[
            Tuple[Tuple[str, Tuple[str, ...]], ...]
        ] = deque(maxlen=dedup_window())
        self._lock = threading.Lock()

    # ---- emitter accessor -------------------------------------------------

    def _emitter(self) -> InvariantDriftSignalEmitter:
        if self._emitter_override is not None:
            return self._emitter_override
        return get_signal_emitter()

    # ---- posture reader (defensive, lazy) --------------------------------

    @staticmethod
    def _default_posture_reader() -> Optional[str]:
        """Read current posture string from PostureStore. NEVER raises;
        returns ``None`` if posture is unread/unavailable."""
        try:
            from backend.core.ouroboros.governance.posture_observer import (  # noqa: E501
                get_default_store as _get_posture_store,
            )
        except Exception:  # noqa: BLE001 — defensive
            return None
        try:
            store = _get_posture_store()
            reading = store.load_current()
        except Exception:  # noqa: BLE001 — defensive
            return None
        if reading is None:
            return None
        try:
            posture_attr = getattr(reading, "posture", None)
            value = (
                getattr(posture_attr, "value", None)
                if posture_attr is not None else None
            )
            return str(value) if value is not None else None
        except Exception:  # noqa: BLE001 — defensive
            return None

    # ---- cadence computation --------------------------------------------

    def compute_interval_s(self) -> float:
        """Return the next-cycle sleep interval in seconds. Composes
        base interval × posture multiplier × (vigilance factor if
        active) × (linear backoff if consecutive failures)."""
        base = base_interval_s()
        # Posture multiplier
        posture = self._posture_reader()
        mults = posture_multipliers()
        if posture and posture in mults:
            base *= mults[posture]
        # Vigilance window — tighter cadence after drift
        if self._vigilance_ticks_remaining > 0:
            base *= vigilance_factor()
        # Failure backoff — linear in consecutive failures, capped
        if self._consecutive_failures > 0:
            base = base * (1 + self._consecutive_failures)
            base = min(base, backoff_ceiling_s())
        # Floor — never zero, never below the configured minimum
        return max(_INTERVAL_FLOOR_S, base)

    # ---- lifecycle --------------------------------------------------------

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Spawn the observer task. NEVER raises. No-op when:
          * master flag off (``JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED``)
          * observer flag off (``JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED``)
          * already running"""
        if not invariant_drift_auditor_enabled():
            logger.info(
                "[InvariantDriftObserver] master flag off; not "
                "starting",
            )
            return
        if not observer_enabled():
            logger.info(
                "[InvariantDriftObserver] observer flag off; not "
                "starting",
            )
            return
        if self.is_running():
            return
        self._stop_event.clear()
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            logger.warning(
                "[InvariantDriftObserver] no running loop; cannot "
                "start",
            )
            return
        self._task = loop.create_task(self._run_forever())
        logger.info(
            "[InvariantDriftObserver] started base_interval=%.1fs "
            "vigilance_ticks=%d dedup_window=%d",
            base_interval_s(), vigilance_ticks(), dedup_window(),
        )

    async def stop(self) -> None:
        """Cooperative shutdown. Cancels the task and awaits cleanup.
        NEVER raises."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ---- one cycle (public for tests) ------------------------------------

    async def run_one_cycle(self) -> ObserverTickResult:
        """Execute one capture-compare-emit cycle. NEVER raises.

        Decision sequence:
          1. Capture snapshot. Failure → backoff + failure result.
          2. Append to history (best-effort; failures swallowed).
          3. Load baseline. None → no-comparison result (still
             productive — history was captured).
          4. Compare. No drift → decay vigilance, return clean result.
          5. Drift → check signature ring. Duplicate → mark deduped,
             skip emit. Novel → record signature + escalate vigilance
             + emit signal."""
        # 1. Capture
        try:
            current = self._capture()
        except Exception as exc:  # noqa: BLE001 — defensive
            with self._lock:
                self._consecutive_failures += 1
                self._cycles_failed += 1
            logger.warning(
                "[InvariantDriftObserver] capture failed (#%d): %s",
                self._consecutive_failures, exc,
            )
            return ObserverTickResult(
                captured=None, drift_records=(),
                emitted=False, deduped=False,
                failure_reason=f"capture: {exc!r}",
            )

        # Reset failure counter on successful capture
        with self._lock:
            self._consecutive_failures = 0

        # 2. Append to history (best-effort — never blocks the cycle)
        try:
            self._store.append_history(current)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[InvariantDriftObserver] history append swallowed",
                exc_info=True,
            )

        # 3. Load baseline
        baseline = self._store.load_baseline()
        if baseline is None:
            with self._lock:
                self._cycles_ok += 1
                # Decay vigilance even on no-baseline cycles —
                # otherwise vigilance could persist indefinitely
                # if baseline gets cleared mid-soak.
                if self._vigilance_ticks_remaining > 0:
                    self._vigilance_ticks_remaining -= 1
            return ObserverTickResult(
                captured=current, drift_records=(),
                emitted=False, deduped=False,
                failure_reason=None,
            )

        # 4. Compare
        drift_records = compare_snapshots(baseline, current)
        if not drift_records:
            with self._lock:
                self._cycles_ok += 1
                if self._vigilance_ticks_remaining > 0:
                    self._vigilance_ticks_remaining -= 1
            return ObserverTickResult(
                captured=current, drift_records=(),
                emitted=False, deduped=False,
                failure_reason=None,
            )

        # 5. Drift detected — check signature ring
        signature = _drift_signature(drift_records)
        with self._lock:
            self._cycles_ok += 1
            already_seen = signature in self._recent_signatures
            if not already_seen:
                self._recent_signatures.append(signature)
                self._vigilance_ticks_remaining = vigilance_ticks()
            else:
                self._signals_deduped += 1

        if already_seen:
            return ObserverTickResult(
                captured=current, drift_records=drift_records,
                emitted=False, deduped=True,
                failure_reason=None,
            )

        # Slice 5 — observability SSE for ALL novel drift (not just
        # actionable). Best-effort, lazy-imported broker; never
        # propagates. Operators see INFO-severity drift here even
        # though the bridge skips it as NO_ACTION.
        try:
            publish_invariant_drift_detected(current, drift_records)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[InvariantDriftObserver] SSE publish swallowed: %s",
                exc,
            )

        # Emit (defensive — emitter raise must NEVER propagate)
        try:
            self._emitter().emit(current, drift_records)
            with self._lock:
                self._signals_emitted += 1
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[InvariantDriftObserver] emitter raised; "
                "swallowing: %s", exc,
            )

        return ObserverTickResult(
            captured=current, drift_records=drift_records,
            emitted=True, deduped=False,
            failure_reason=None,
        )

    # ---- main loop --------------------------------------------------------

    async def _run_forever(self) -> None:
        """Forever loop — runs ``run_one_cycle`` then sleeps for
        the dynamic interval. ``CancelledError`` propagates;
        everything else is logged + counted."""
        while not self._stop_event.is_set():
            try:
                await self.run_one_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — defensive last-resort
                with self._lock:
                    self._cycles_failed += 1
                logger.exception(
                    "[InvariantDriftObserver] unexpected cycle "
                    "exception",
                )
            interval = self.compute_interval_s()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval,
                )
            except asyncio.TimeoutError:
                pass

    # ---- diagnostics ------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Snapshot of observer counters + computed next-interval.
        NEVER raises."""
        with self._lock:
            return {
                "cycles_ok": self._cycles_ok,
                "cycles_failed": self._cycles_failed,
                "signals_emitted": self._signals_emitted,
                "signals_deduped": self._signals_deduped,
                "consecutive_failures": self._consecutive_failures,
                "vigilance_ticks_remaining": (
                    self._vigilance_ticks_remaining
                ),
                "recent_signature_count": len(
                    self._recent_signatures,
                ),
                "next_interval_s": self.compute_interval_s(),
                "is_running": self.is_running(),
            }


def _drift_signature(
    drift_records: Tuple[InvariantDriftRecord, ...],
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """Hashable, ordering-stable signature of a drift set. Two
    drift sets with the same kinds + affected_keys produce the same
    signature regardless of detail-string differences (which can
    contain numerics that drift by tiny amounts cycle-to-cycle).

    NEVER raises."""
    try:
        return tuple(
            sorted(
                (r.drift_kind.value, tuple(r.affected_keys))
                for r in drift_records
            )
        )
    except Exception:  # noqa: BLE001 — defensive
        return ()


# ---------------------------------------------------------------------------
# SSE event — Slice 5 graduation observability surface
# ---------------------------------------------------------------------------


EVENT_TYPE_INVARIANT_DRIFT_DETECTED: str = (
    "invariant_drift_detected"
)


def publish_invariant_drift_detected(
    snapshot: InvariantSnapshot,
    drift_records: Tuple[InvariantDriftRecord, ...],
) -> Optional[str]:
    """Fire the ``invariant_drift_detected`` SSE event for a novel
    drift cycle. Best-effort: broker-missing / publish-error /
    observability-disabled all return ``None`` silently. NEVER
    raises.

    Mirrors ``publish_auto_action_proposal_emitted`` exactly —
    lazy ``ide_observability_stream`` import so the observer
    module doesn't gain a hard dependency on the SSE infrastructure.

    Fires for ALL novel drift (including INFO-severity), giving
    operators visibility into posture moves and other informational
    transitions that the auto-action bridge skips as NO_ACTION."""
    if not drift_records:
        return None
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            get_default_broker,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    # Build payload: severity histogram, drift kinds, top-affected
    # keys; bounded so SSE payload + downstream renderers don't
    # blow up on pathological drift bundles.
    try:
        severity_counts: Dict[str, int] = {}
        kind_counts: Dict[str, int] = {}
        affected: list = []
        for r in drift_records:
            sev = getattr(r.severity, "value", str(r.severity))
            kind = getattr(
                r.drift_kind, "value", str(r.drift_kind),
            )
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            for k in (r.affected_keys or ()):
                if k not in affected:
                    affected.append(str(k))
        affected = affected[:16]  # bounded
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        broker = get_default_broker()
        return broker.publish(
            event_type=EVENT_TYPE_INVARIANT_DRIFT_DETECTED,
            op_id=str(snapshot.snapshot_id or ""),
            payload={
                "schema_version": snapshot.schema_version,
                "snapshot_id": str(snapshot.snapshot_id or ""),
                "captured_at_utc": float(
                    snapshot.captured_at_utc,
                ),
                "drift_count": len(drift_records),
                "severity_counts": severity_counts,
                "kind_counts": kind_counts,
                "affected_keys": affected,
                "posture": str(snapshot.posture_value or ""),
            },
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[InvariantDriftObserver] SSE publish swallowed",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Default observer singleton (mirrors Slice 2's get_default_store)
# ---------------------------------------------------------------------------


_default_observer: Optional[InvariantDriftObserver] = None
_default_observer_lock = threading.Lock()


def get_default_observer(
    store: Optional[InvariantDriftStore] = None,
) -> InvariantDriftObserver:
    """Singleton default observer wrapping the default store. NEVER
    raises. First call wins on the store argument."""
    global _default_observer
    with _default_observer_lock:
        if _default_observer is None:
            target_store = (
                store if store is not None else get_default_store()
            )
            _default_observer = InvariantDriftObserver(target_store)
        return _default_observer


def reset_default_observer() -> None:
    """Drop the singleton — test isolation. Does NOT stop a running
    task; callers must ``stop()`` first if needed."""
    global _default_observer
    with _default_observer_lock:
        _default_observer = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EVENT_TYPE_INVARIANT_DRIFT_DETECTED",
    "InvariantDriftObserver",
    "InvariantDriftSignalEmitter",
    "ObserverTickResult",
    "backoff_ceiling_s",
    "base_interval_s",
    "dedup_window",
    "get_default_observer",
    "get_signal_emitter",
    "observer_enabled",
    "posture_multipliers",
    "publish_invariant_drift_detected",
    "register_signal_emitter",
    "reset_default_observer",
    "reset_signal_emitter",
    "vigilance_factor",
    "vigilance_ticks",
]
