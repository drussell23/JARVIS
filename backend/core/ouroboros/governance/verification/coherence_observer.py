"""Priority #1 Slice 3 — Async observer with posture-aware adaptive cadence.

Periodic auditor that drives the Coherence Auditor pipeline:

  1. Collect a ``WindowData`` from existing artifacts (posture
     history + module fingerprints + policy observations + the
     extension hooks for op records / recurrence / p99 / apply
     event paths).
  2. Compute ``BehavioralSignature`` (Slice 1) over the window.
  3. Record signature into the bounded ring buffer (Slice 2).
  4. Read the most-recent prior signature.
  5. Compute ``BehavioralDriftVerdict`` (Slice 1).
  6. Record verdict into the audit log (Slice 2, append-only).
  7. Publish SSE event ``EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED``
     for non-DISABLED + non-COHERENT verdicts (master-flag-gated
     + best-effort).
  8. Adapt cadence: tighten on drift detected, decay on coherent,
     backoff on failure, dedup by drift signature within window.

Architectural mirror of Move 4's ``InvariantDriftObserver`` —
identical lifecycle (start/stop/run_one_cycle), identical
posture-aware-multiplier + adaptive-vigilance + failure-backoff
state machine, parameterized differently. Same J.A.R.M.A.T.R.I.X.
discipline (5-value ``ObserverTickOutcome`` closed enum). NEVER
raises out of the periodic loop.

Direct-solve principles:

  * **Asynchronous-ready** — single async task per observer
    instance. ``asyncio.wait_for`` on the cancellation event
    drives the cadence sleep so cancellation propagates within
    one loop iteration. Sync collector calls (file I/O) wrapped
    in ``asyncio.to_thread`` so the loop never blocks. Mirrors
    Move 5 Slice 3's pattern.

  * **Dynamic** — every cadence value, vigilance multiplier,
    backoff ceiling, dedup window length is env-tunable with
    floor+ceiling clamps. NO hardcoded magic. Posture-aware
    multipliers are independently tunable per posture
    (HARDEN/DEFAULT/MAINTAIN — EXPLORE+CONSOLIDATE share
    DEFAULT).

  * **Adaptive** — drift detected → tighten next cadence × 0.5
    (env-tunable) for N ticks. Coherent cycles → decay vigilance.
    K consecutive failures → linear backoff capped at ceiling.
    Drift signature seen within dedup window → suppress emit
    (still records audit so operator sees recurrence count).

  * **Intelligent** — defensive layered: collector failure
    increments failure counter without crashing the loop; SSE
    publish failure swallowed; posture reader failure falls to
    DEFAULT cadence; broker-missing best-effort returns None.
    Each layer is independently failure-bounded.

  * **Robust** — every public method NEVER raises out. Internal
    exceptions are logged and converted to ``ObserverTickOutcome
    .FAILED`` results. State machine is reentrant-safe via
    ``threading.Lock`` for the in-process counters; the file
    I/O is cross-process safe via Tier 1 #3 flock (Slice 2).

  * **No hardcoding** — collector is injectable Protocol;
    posture reader is injectable; emitter is injectable. Tests
    pass stubs; production passes None and gets real defaults.
    Tracked-modules list is env-tunable (default: empty — no
    auto-discovery to keep Slice 3 strictly read-only).

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + Slice 1 (coherence_auditor) + Slice 2
    (coherence_window_store) + Move 4's posture_observer (read-
    only safe wrapper) + Tier 1 #2's posture_health
    (safe_load_posture_value) + Move 6 Slice 2 (compute_ast_
    signature, optional, only invoked from default collector
    when tracked modules configured) + lazy
    ide_observability_stream (best-effort SSE publish).
  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.
  * No mutation tools.
  * No exec/eval/compile.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)

from backend.core.ouroboros.governance.verification.coherence_auditor import (
    BehavioralDriftVerdict,
    BehavioralSignature,
    CoherenceOutcome,
    DriftBudgets,
    DriftSeverity,
    OpRecord,
    PostureRecord,
    WindowData,
    coherence_auditor_enabled,
    compute_behavioral_drift,
    compute_behavioral_signature,
)
from backend.core.ouroboros.governance.verification.coherence_window_store import (
    AuditReadResult,
    WindowOutcome,
    WindowReadResult,
    read_window,
    record_drift_audit,
    record_signature,
    window_hours_default,
)

logger = logging.getLogger(__name__)


COHERENCE_OBSERVER_SCHEMA_VERSION: str = "coherence_observer.1"


# ---------------------------------------------------------------------------
# Sub-gate flag
# ---------------------------------------------------------------------------


def observer_enabled() -> bool:
    """``JARVIS_COHERENCE_OBSERVER_ENABLED`` (default ``true``
    post Slice 5 graduation 2026-05-01).

    Sub-gate for the periodic observer task. Master flag
    (``JARVIS_COHERENCE_AUDITOR_ENABLED``) must also be true for
    the observer to actually start. Operators may set this false
    to disable the periodic auditor while keeping the primitive
    APIs callable (e.g., on-demand audits without the schedule).
    Asymmetric env semantics."""
    raw = os.environ.get(
        "JARVIS_COHERENCE_OBSERVER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-01 (Priority #1 Slice 5)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Cadence env knobs — every numeric clamped
# ---------------------------------------------------------------------------


def _env_float_clamped(
    name: str, default: float, *, floor: float, ceiling: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def _env_int_clamped(
    name: str, default: int, *, floor: int, ceiling: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return min(ceiling, max(floor, v))
    except (TypeError, ValueError):
        return default


def cadence_hours_default() -> float:
    """``JARVIS_COHERENCE_CADENCE_HOURS_DEFAULT`` (default 6.0,
    floor 1.0, ceiling 48.0). Used for EXPLORE/CONSOLIDATE/None
    postures."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_CADENCE_HOURS_DEFAULT",
        6.0, floor=1.0, ceiling=48.0,
    )


def cadence_hours_harden() -> float:
    """``JARVIS_COHERENCE_CADENCE_HOURS_HARDEN`` (default 3.0,
    floor 1.0, ceiling 24.0). Tighter cadence in HARDEN posture."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_CADENCE_HOURS_HARDEN",
        3.0, floor=1.0, ceiling=24.0,
    )


def cadence_hours_maintain() -> float:
    """``JARVIS_COHERENCE_CADENCE_HOURS_MAINTAIN`` (default 12.0,
    floor 1.0, ceiling 48.0). Relaxed cadence in MAINTAIN
    posture."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_CADENCE_HOURS_MAINTAIN",
        12.0, floor=1.0, ceiling=48.0,
    )


def vigilance_multiplier() -> float:
    """``JARVIS_COHERENCE_VIGILANCE_MULTIPLIER`` (default 0.5,
    floor 0.1, ceiling 1.0). Cadence multiplier when drift was
    detected — tighter cycles for N subsequent ticks."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_VIGILANCE_MULTIPLIER",
        0.5, floor=0.1, ceiling=1.0,
    )


def vigilance_ticks() -> int:
    """``JARVIS_COHERENCE_VIGILANCE_TICKS`` (default 4, floor 1,
    ceiling 50). Number of subsequent cycles to maintain
    tightened cadence after drift."""
    return _env_int_clamped(
        "JARVIS_COHERENCE_VIGILANCE_TICKS",
        4, floor=1, ceiling=50,
    )


def dedup_window_size() -> int:
    """``JARVIS_COHERENCE_DEDUP_WINDOW_SIZE`` (default 16, floor
    1, ceiling 200). Size of the in-process drift signature
    dedup ring buffer. Same drift detected within window is
    suppressed from SSE emission."""
    return _env_int_clamped(
        "JARVIS_COHERENCE_DEDUP_WINDOW_SIZE",
        16, floor=1, ceiling=200,
    )


def backoff_ceiling_hours() -> float:
    """``JARVIS_COHERENCE_BACKOFF_CEILING_HOURS`` (default 24.0,
    floor 1.0, ceiling 168.0). Maximum cadence after consecutive
    failures."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_BACKOFF_CEILING_HOURS",
        24.0, floor=1.0, ceiling=168.0,
    )


def cadence_floor_seconds() -> float:
    """``JARVIS_COHERENCE_CADENCE_FLOOR_S`` (default 60.0, floor
    10.0, ceiling 3600.0). Hard floor on cadence regardless of
    multipliers — prevents pathological tight loops."""
    return _env_float_clamped(
        "JARVIS_COHERENCE_CADENCE_FLOOR_S",
        60.0, floor=10.0, ceiling=3600.0,
    )


# ---------------------------------------------------------------------------
# Posture multiplier mapping — env-driven, NO hardcoded posture math
# ---------------------------------------------------------------------------


def posture_cadence_hours(posture: Optional[str]) -> float:
    """Return the cadence in hours for the given posture string.
    Maps:
      HARDEN → ``cadence_hours_harden`` (tighter)
      MAINTAIN → ``cadence_hours_maintain`` (relaxed)
      EXPLORE/CONSOLIDATE/None/unknown → ``cadence_hours_default``"""
    if posture is None:
        return cadence_hours_default()
    p_norm = str(posture).strip().upper()
    if p_norm == "HARDEN":
        return cadence_hours_harden()
    if p_norm == "MAINTAIN":
        return cadence_hours_maintain()
    return cadence_hours_default()


# ---------------------------------------------------------------------------
# 5-value tick-outcome closed enum (J.A.R.M.A.T.R.I.X.)
# ---------------------------------------------------------------------------


class ObserverTickOutcome(str, enum.Enum):
    """5-value closed taxonomy. Every cycle returns exactly one.

    ``COHERENT_OK``  — cycle completed, no drift detected.
    ``DRIFT_EMITTED`` — novel drift detected and SSE published.
    ``DRIFT_DEDUPED`` — drift detected but signature seen within
                        dedup window; audit recorded but SSE
                        suppressed.
    ``INSUFFICIENT_DATA`` — first signature in window or
                            collector returned empty.
    ``FAILED``        — collector / signature compute / drift
                        compute / store write failed."""

    COHERENT_OK = "coherent_ok"
    DRIFT_EMITTED = "drift_emitted"
    DRIFT_DEDUPED = "drift_deduped"
    INSUFFICIENT_DATA = "insufficient_data"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Frozen tick-result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObserverTickResult:
    """Result of one observer cycle. Frozen for safe propagation
    across async boundaries."""

    outcome: ObserverTickOutcome
    signature: Optional[BehavioralSignature] = None
    verdict: Optional[BehavioralDriftVerdict] = None
    next_interval_s: float = 0.0
    failure_reason: Optional[str] = None
    schema_version: str = COHERENCE_OBSERVER_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "signature_id": (
                self.signature.signature_id()
                if self.signature is not None else None
            ),
            "verdict_outcome": (
                self.verdict.outcome.value
                if self.verdict is not None else None
            ),
            "drift_signature": (
                self.verdict.drift_signature
                if self.verdict is not None else ""
            ),
            "next_interval_s": self.next_interval_s,
            "failure_reason": self.failure_reason,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# WindowDataCollector Protocol — injectable, defensive
# ---------------------------------------------------------------------------


class WindowDataCollector(Protocol):
    """Read-only collector for window data. The default
    implementation reads from existing artifacts (posture history
    + module fingerprints + policy observations). Tests inject
    stubs for deterministic behavior."""

    def collect_window(
        self, *, now_ts: float, window_hours: int,
    ) -> WindowData:
        """Return the WindowData for the given window. NEVER
        raises (defensive contract)."""
        ...


# ---------------------------------------------------------------------------
# SSE event vocabulary + publisher (lazy import, master-gated)
# ---------------------------------------------------------------------------


EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED: str = (
    "behavioral_drift_detected"
)
"""SSE event fired on every novel non-DISABLED + non-COHERENT
verdict. Master-flag-gated by ``coherence_auditor_enabled``;
broker-missing / publish-error all return None silently. Mirrors
Move 4/5/6 lazy-import + best-effort discipline. NEVER raises."""


def publish_behavioral_drift(
    *,
    verdict: BehavioralDriftVerdict,
    op_id: str = "",
) -> Optional[str]:
    """Fire the ``EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED`` SSE
    event. NEVER raises.

    Master-flag-gated; no event for DISABLED / COHERENT outcomes
    (zero noise when feature off / no drift to report). Best-
    effort: broker-missing / publish-error all return None."""
    if not coherence_auditor_enabled():
        return None
    if verdict.outcome in (
        CoherenceOutcome.DISABLED,
        CoherenceOutcome.COHERENT,
    ):
        return None
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            get_default_broker,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        broker = get_default_broker()
        return broker.publish(
            event_type=EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED,
            op_id=str(op_id or ""),
            payload={
                "schema_version": (
                    COHERENCE_OBSERVER_SCHEMA_VERSION
                ),
                "outcome": verdict.outcome.value,
                "largest_severity": (
                    verdict.largest_severity.value
                ),
                "drift_signature": verdict.drift_signature,
                "finding_count": len(verdict.findings),
                "kinds": sorted({
                    f.kind.value for f in verdict.findings
                }),
                "detail": str(verdict.detail or "")[:200],
            },
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CoherenceObserver] SSE publish swallowed",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Default collector — reads posture history + (extension hooks)
# ---------------------------------------------------------------------------


def _safe_posture_reader() -> Optional[str]:
    """Read current posture string via Tier 1 #2 safe wrapper.
    Returns None when posture observer is degraded / unavailable.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.posture_observer import (  # noqa: E501
            get_default_observer as _get_obs,
            get_default_store as _get_store,
            observer_interval_s as _interval_s,
        )
        from backend.core.ouroboros.governance.posture_health import (  # noqa: E501
            safe_load_posture_value,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        try:
            obs = _get_obs()
        except Exception:  # noqa: BLE001 — defensive
            obs = None
        try:
            interval = _interval_s()
        except Exception:  # noqa: BLE001 — defensive
            interval = None
        return safe_load_posture_value(
            observer=obs, store=_get_store(),
            interval_s=interval,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None


def _collect_posture_records_from_store(
    *, now_ts: float, window_hours: int,
) -> Tuple[PostureRecord, ...]:
    """Read posture history from PostureStore and convert to
    PostureRecord tuples within the window. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.posture_observer import (  # noqa: E501
            get_default_store as _get_store,
        )
    except Exception:  # noqa: BLE001 — defensive
        return tuple()
    try:
        store = _get_store()
        readings = store.load_history()
    except Exception:  # noqa: BLE001 — defensive
        return tuple()
    cutoff = now_ts - (window_hours * 3600.0)
    out = []
    for r in readings:
        try:
            ts = float(getattr(r, "inferred_at", 0.0))
            if ts < cutoff:
                continue
            posture_obj = getattr(r, "posture", None)
            posture_str = (
                posture_obj.value if hasattr(posture_obj, "value")
                else str(posture_obj or "")
            )
            if not posture_str:
                continue
            out.append(PostureRecord(
                posture=str(posture_str), ts=ts,
            ))
        except Exception:  # noqa: BLE001 — defensive
            continue
    return tuple(out)


class _DefaultWindowDataCollector:
    """Default collector. Reads posture history (only). Other
    sources (op_records, recurrence, p99, apply_event_paths,
    module_fingerprints, policy_observations) are returned empty —
    Slice 3b will extend with phase_capture / summary.json /
    FlagRegistry readers. Empty defaults produce
    INSUFFICIENT_DATA verdicts on cold start, which is the
    semantically-correct outcome."""

    def collect_window(
        self, *, now_ts: float, window_hours: int,
    ) -> WindowData:
        return WindowData(
            window_start_ts=now_ts - (window_hours * 3600.0),
            window_end_ts=now_ts,
            posture_records=(
                _collect_posture_records_from_store(
                    now_ts=now_ts, window_hours=window_hours,
                )
            ),
        )


# ---------------------------------------------------------------------------
# Observer — async lifecycle, posture-aware cadence, adaptive vigilance
# ---------------------------------------------------------------------------


class CoherenceObserver:
    """Periodic Coherence Auditor. Mirrors Move 4
    InvariantDriftObserver lifecycle exactly: ``start()`` spawns
    the task; ``stop()`` cancels cooperatively; ``run_one_cycle()``
    is public for tests with no sleep. NEVER raises out of any
    public method.

    Injectable dependencies (production passes None and gets
    defaults; tests pass stubs for deterministic behavior):

      * ``collector``       — defaults to
                              ``_DefaultWindowDataCollector``
      * ``posture_reader``  — defaults to ``_safe_posture_reader``
      * ``budgets``         — defaults to ``DriftBudgets.from_env``"""

    def __init__(
        self,
        *,
        collector: Optional[WindowDataCollector] = None,
        posture_reader: Optional[
            Callable[[], Optional[str]]
        ] = None,
        budgets: Optional[DriftBudgets] = None,
        base_dir: Optional[Any] = None,  # passed through to store
    ) -> None:
        self._collector = (
            collector or _DefaultWindowDataCollector()
        )
        self._posture_reader = (
            posture_reader or _safe_posture_reader
        )
        self._budgets = budgets
        self._base_dir = base_dir
        self._task: Optional[asyncio.Task[Any]] = None
        self._stop_event = asyncio.Event()
        # Counters
        self._cycles_total = 0
        self._cycles_coherent = 0
        self._cycles_drift_emitted = 0
        self._cycles_drift_deduped = 0
        self._cycles_failed = 0
        self._cycles_insufficient = 0
        self._consecutive_failures = 0
        self._vigilance_ticks_remaining = 0
        # Drift signature dedup ring
        self._recent_signatures: Deque[str] = deque(
            maxlen=dedup_window_size(),
        )
        self._lock = threading.Lock()

    # ---- cadence computation ---------------------------------------------

    def compute_interval_s(self) -> float:
        """Return next-cycle sleep interval. Composes:
        cadence_hours(posture) × vigilance_factor (if active) ×
        backoff (linear in consecutive_failures, capped). Final
        result is clamped to [floor, backoff_ceiling]."""
        try:
            posture = self._posture_reader()
        except Exception:  # noqa: BLE001 — defensive
            posture = None
        hours = posture_cadence_hours(posture)
        seconds = hours * 3600.0
        # Vigilance — tighter cadence after drift
        with self._lock:
            if self._vigilance_ticks_remaining > 0:
                seconds *= vigilance_multiplier()
            # Failure backoff — linear in consecutive_failures
            if self._consecutive_failures > 0:
                seconds = seconds * (
                    1 + self._consecutive_failures
                )
        # Ceiling: never exceed backoff_ceiling
        ceiling = backoff_ceiling_hours() * 3600.0
        seconds = min(ceiling, seconds)
        # Floor: never below cadence_floor
        seconds = max(cadence_floor_seconds(), seconds)
        return float(seconds)

    # ---- lifecycle --------------------------------------------------------

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Spawn the observer task. NEVER raises. No-op when:
          * master flag off
          * sub-gate flag off
          * already running"""
        if not coherence_auditor_enabled():
            logger.info(
                "[CoherenceObserver] master flag off; not starting",
            )
            return
        if not observer_enabled():
            logger.info(
                "[CoherenceObserver] sub-gate off; not starting",
            )
            return
        if self.is_running():
            return
        self._stop_event.clear()
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            logger.warning(
                "[CoherenceObserver] no running loop; cannot start",
            )
            return
        self._task = loop.create_task(self._run_forever())
        logger.info(
            "[CoherenceObserver] started "
            "default_cadence=%.1fh vigilance_mult=%.2f "
            "vigilance_ticks=%d dedup=%d",
            cadence_hours_default(), vigilance_multiplier(),
            vigilance_ticks(), dedup_window_size(),
        )

    async def stop(self) -> None:
        """Cooperative shutdown. NEVER raises."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ---- one cycle (public for tests) ------------------------------------

    async def run_one_cycle(
        self, *, now_ts: Optional[float] = None,
    ) -> ObserverTickResult:
        """Execute one collect-compute-record-emit cycle. NEVER
        raises.

        Decision sequence:
          1. Collect WindowData. Failure → backoff + FAILED.
          2. Compute signature (Slice 1 — never raises).
          3. Record signature into ring buffer (Slice 2). Best-
             effort; failure swallowed.
          4. Read most-recent prior signature (Slice 2).
          5. Compute drift verdict (Slice 1 — never raises).
          6. Record verdict into audit log (Slice 2). Best-
             effort.
          7. Branch on verdict outcome:
             * DISABLED → COHERENT_OK (master off — should not
               normally happen if observer started, but defensive)
             * INSUFFICIENT_DATA → INSUFFICIENT_DATA, decay
               vigilance
             * COHERENT → COHERENT_OK, decay vigilance
             * DRIFT_DETECTED → check dedup ring → emit SSE if
               novel + escalate vigilance
             * FAILED → FAILED + increment failure counter"""
        import time as _time
        ts = float(now_ts) if now_ts is not None else _time.time()
        # Window hours — same env knob as the store reads
        whrs = window_hours_default()

        # 1. Collect (in thread to avoid blocking loop on file I/O)
        try:
            data = await asyncio.to_thread(
                self._collector.collect_window,
                now_ts=ts, window_hours=whrs,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            with self._lock:
                self._consecutive_failures += 1
                self._cycles_failed += 1
                self._cycles_total += 1
            logger.warning(
                "[CoherenceObserver] collect failed (#%d): %s",
                self._consecutive_failures, exc,
            )
            return ObserverTickResult(
                outcome=ObserverTickOutcome.FAILED,
                next_interval_s=self.compute_interval_s(),
                failure_reason=f"collect: {exc!r}",
            )

        # Reset failure counter on successful collect
        with self._lock:
            self._consecutive_failures = 0

        # 2. Compute signature
        sig = compute_behavioral_signature(data)

        # 3. Record signature (best-effort)
        try:
            await asyncio.to_thread(
                record_signature, sig, base_dir=self._base_dir,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[CoherenceObserver] record_signature swallowed",
                exc_info=True,
            )

        # 4. Read prior signature
        prior_sig: Optional[BehavioralSignature] = None
        try:
            r: WindowReadResult = await asyncio.to_thread(
                read_window,
                window_hours=whrs, base_dir=self._base_dir,
                now_ts=ts,
            )
            if (
                r.outcome is WindowOutcome.READ_OK
                and len(r.signatures) >= 2
            ):
                # Newest is the one we just wrote; second-newest
                # is the prior comparison point.
                prior_sig = r.signatures[-2]
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[CoherenceObserver] read_window swallowed",
                exc_info=True,
            )

        # 5. Compute drift
        budgets = (
            self._budgets if self._budgets is not None
            else DriftBudgets.from_env()
        )
        verdict = compute_behavioral_drift(
            prior_sig, sig,
            budgets=budgets,
            apply_event_paths=data.apply_event_paths,
            policy_observations=data.policy_observations,
            enabled_override=True,  # observer wouldn't start if off
        )

        # 6. Record audit (best-effort, only for non-DISABLED non-
        # COHERENT — coherent verdicts are noise in the audit log)
        if verdict.outcome not in (
            CoherenceOutcome.DISABLED,
            CoherenceOutcome.COHERENT,
        ):
            try:
                await asyncio.to_thread(
                    record_drift_audit, verdict,
                    base_dir=self._base_dir,
                )
            except Exception:  # noqa: BLE001 — defensive
                logger.debug(
                    "[CoherenceObserver] record_drift_audit "
                    "swallowed", exc_info=True,
                )

        # 7. Branch on outcome
        next_int = self.compute_interval_s()

        if verdict.outcome is CoherenceOutcome.DISABLED:
            # Should not happen in normal flow (master is
            # already checked at start()); defensive.
            with self._lock:
                self._cycles_total += 1
                self._cycles_coherent += 1
                if self._vigilance_ticks_remaining > 0:
                    self._vigilance_ticks_remaining -= 1
            return ObserverTickResult(
                outcome=ObserverTickOutcome.COHERENT_OK,
                signature=sig, verdict=verdict,
                next_interval_s=next_int,
            )

        if verdict.outcome is CoherenceOutcome.INSUFFICIENT_DATA:
            with self._lock:
                self._cycles_total += 1
                self._cycles_insufficient += 1
                if self._vigilance_ticks_remaining > 0:
                    self._vigilance_ticks_remaining -= 1
            return ObserverTickResult(
                outcome=ObserverTickOutcome.INSUFFICIENT_DATA,
                signature=sig, verdict=verdict,
                next_interval_s=next_int,
            )

        if verdict.outcome is CoherenceOutcome.FAILED:
            with self._lock:
                self._cycles_total += 1
                self._cycles_failed += 1
                self._consecutive_failures += 1
            return ObserverTickResult(
                outcome=ObserverTickOutcome.FAILED,
                signature=sig, verdict=verdict,
                next_interval_s=self.compute_interval_s(),
                failure_reason=verdict.detail,
            )

        if verdict.outcome is CoherenceOutcome.COHERENT:
            with self._lock:
                self._cycles_total += 1
                self._cycles_coherent += 1
                if self._vigilance_ticks_remaining > 0:
                    self._vigilance_ticks_remaining -= 1
            return ObserverTickResult(
                outcome=ObserverTickOutcome.COHERENT_OK,
                signature=sig, verdict=verdict,
                next_interval_s=next_int,
            )

        # DRIFT_DETECTED — check dedup ring
        with self._lock:
            self._cycles_total += 1
            sig_hash = verdict.drift_signature
            already_seen = (
                bool(sig_hash)
                and sig_hash in self._recent_signatures
            )
            if not already_seen:
                if sig_hash:
                    self._recent_signatures.append(sig_hash)
                self._vigilance_ticks_remaining = (
                    vigilance_ticks()
                )
                self._cycles_drift_emitted += 1
            else:
                self._cycles_drift_deduped += 1

        if already_seen:
            return ObserverTickResult(
                outcome=ObserverTickOutcome.DRIFT_DEDUPED,
                signature=sig, verdict=verdict,
                next_interval_s=self.compute_interval_s(),
            )

        # Novel drift — fire SSE (best-effort)
        publish_behavioral_drift(verdict=verdict)

        return ObserverTickResult(
            outcome=ObserverTickOutcome.DRIFT_EMITTED,
            signature=sig, verdict=verdict,
            next_interval_s=self.compute_interval_s(),
        )

    # ---- forever loop (private) ------------------------------------------

    async def _run_forever(self) -> None:
        """Periodic loop. NEVER raises out — every cycle catches
        + logs + continues. Stops cleanly on cancel / stop_event."""
        while not self._stop_event.is_set():
            try:
                result = await self.run_one_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "[CoherenceObserver] cycle raised: %s", exc,
                    exc_info=True,
                )
                with self._lock:
                    self._consecutive_failures += 1
                interval = self.compute_interval_s()
            else:
                interval = result.next_interval_s

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval,
                )
                # stop_event was set
                return
            except asyncio.TimeoutError:
                # Normal cadence tick — continue
                continue
            except asyncio.CancelledError:
                raise

    # ---- snapshot (for /coherence REPL + observability) ------------------

    def snapshot(self) -> Dict[str, Any]:
        """Return a counter snapshot. Slice 5 / future REPL
        consume this."""
        with self._lock:
            return {
                "cycles_total": self._cycles_total,
                "cycles_coherent": self._cycles_coherent,
                "cycles_drift_emitted": self._cycles_drift_emitted,
                "cycles_drift_deduped": self._cycles_drift_deduped,
                "cycles_insufficient": self._cycles_insufficient,
                "cycles_failed": self._cycles_failed,
                "consecutive_failures": (
                    self._consecutive_failures
                ),
                "vigilance_ticks_remaining": (
                    self._vigilance_ticks_remaining
                ),
                "dedup_ring_size": len(self._recent_signatures),
                "schema_version": (
                    COHERENCE_OBSERVER_SCHEMA_VERSION
                ),
            }


# ---------------------------------------------------------------------------
# Process-global default observer (singleton; consumers may inject)
# ---------------------------------------------------------------------------


_default_observer: Optional[CoherenceObserver] = None
_default_observer_lock = threading.Lock()


def get_default_observer() -> CoherenceObserver:
    """Process-global observer. Lazy-initialized + thread-safe."""
    global _default_observer  # noqa: PLW0603
    with _default_observer_lock:
        if _default_observer is None:
            _default_observer = CoherenceObserver()
        return _default_observer


def reset_default_observer_for_tests() -> None:
    """Reset the singleton. Test-only."""
    global _default_observer  # noqa: PLW0603
    with _default_observer_lock:
        _default_observer = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "COHERENCE_OBSERVER_SCHEMA_VERSION",
    "CoherenceObserver",
    "EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED",
    "ObserverTickOutcome",
    "ObserverTickResult",
    "WindowDataCollector",
    "backoff_ceiling_hours",
    "cadence_floor_seconds",
    "cadence_hours_default",
    "cadence_hours_harden",
    "cadence_hours_maintain",
    "dedup_window_size",
    "get_default_observer",
    "observer_enabled",
    "posture_cadence_hours",
    "publish_behavioral_drift",
    "vigilance_multiplier",
    "vigilance_ticks",
]
