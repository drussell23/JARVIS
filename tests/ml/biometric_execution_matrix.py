"""BiometricExecutionMatrix & Zero-Trust OOM Circuit Breaker (Slice 250.2b
Phase 3 — "The ECAPA Execution Matrix").

A SYNCHRONOUS, fail-secure speaker-authentication front-end for the ECAPA
embedder. Its single load-bearing guarantee:

    Under ANY failure — circuit open, unified-memory / Metal OOM, or a generic
    extractor error — the verdict is REJECTED (locked). The matrix NEVER returns
    ACCEPTED on an error path.

Why a circuit breaker, and why fail-secure
------------------------------------------
On Apple-Silicon unified memory, an ECAPA forward pass can raise ``MemoryError``
under RAM pressure. A naive auth front-end might (a) treat an exception as
"inconclusive" and fall through to ACCEPT, or (b) keep hammering the OOMing
extractor. Both are unacceptable for a biometric gate. So:

  * OOM (and designated RAM-pressure errors) → record a breaker failure and
    return the secure-lock verdict SYNCHRONOUSLY. Telemetry (RESOURCE_PRESSURE
    event + Slice-254 diagnostic swarm dispatch) fires FIRE-AND-FORGET so it can
    never delay or alter the verdict.
  * Sustained OOMs trip the breaker; while open, ``authenticate`` short-circuits
    to ``circuit_open_locked`` WITHOUT ever invoking the embedder — protecting
    the pipeline from a thundering OOM herd.

Structural injection (NO backend imports — split-brain-guard safe)
------------------------------------------------------------------
Everything the kernel provides is consumed by SHAPE, never by import:

  * ``CircuitBreakerLike`` (Protocol) — structurally aligned to the supervisor's
    ``AdvancedCircuitBreaker``. Production injects the real one; this module
    ships a standalone ``LocalOOMCircuitBreaker`` default so the matrix works
    without the kernel.
  * ``Embedder = Callable[[np.ndarray], np.ndarray]`` — the ECAPA extractor. In
    production pass ``ecapa_facade.extract_embedding`` (a thin sync adapter over
    the async facade) or any callable; in tests pass a deterministic stand-in or
    a failing one. ``try_default_ecapa_embedder`` LAZILY probes the facade and
    returns ``None`` if unavailable (no module-scope import).
  * ``EventSink = Callable[[str, dict], None]`` — structurally the
    ``SupervisorEventBus`` emit ``(event_type, payload)``. Production wraps
    ``get_event_bus().emit(...)``.
  * ``SwarmTrigger = Callable[[dict], Any]`` — structurally the Slice-254
    ``DiagnosticSubAgent`` swarm dispatch.

Pure numpy. No torch, no scipy.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, Protocol, runtime_checkable

import numpy as np

# --------------------------------------------------------------------------- #
# Injection contracts (duck-typed — never imported from the kernel)
# --------------------------------------------------------------------------- #
Embedder = Callable[[np.ndarray], np.ndarray]
EventSink = Callable[[str, dict], Any]
SwarmTrigger = Callable[[dict], Any]


@runtime_checkable
class CircuitBreakerLike(Protocol):
    """Structural alignment with the supervisor's ``AdvancedCircuitBreaker``.

    Only these three methods are consumed. ``@runtime_checkable`` so either a
    duck-typed fake or the real breaker satisfies ``isinstance``.
    """

    def can_execute(self) -> bool:
        ...

    def record_success(self) -> None:
        ...

    def record_failure(self, error: Optional[BaseException] = None) -> None:
        ...


# Error classes treated as RAM-pressure (fire RESOURCE_PRESSURE telemetry).
# Tuple so production can extend via the env knob below without code change here.
_RAM_PRESSURE_ERRORS: tuple[type[BaseException], ...] = (MemoryError,)


# --------------------------------------------------------------------------- #
# Standalone default breaker (importable, kernel-free)
# --------------------------------------------------------------------------- #
class LocalOOMCircuitBreaker:
    """Minimal failure-threshold + reset-timeout breaker.

    Structurally a ``CircuitBreakerLike``. Trips OPEN after ``failure_threshold``
    consecutive failures; after ``reset_timeout_s`` elapses it goes HALF-OPEN
    (``can_execute`` returns True again to permit one trial). A success resets
    the failure count to zero. Env-tunable defaults so production can inject the
    real ``AdvancedCircuitBreaker`` or rely on this without code edits.
    """

    def __init__(
        self,
        *,
        failure_threshold: Optional[int] = None,
        reset_timeout_s: Optional[float] = None,
    ) -> None:
        self.failure_threshold = int(
            failure_threshold
            if failure_threshold is not None
            else os.environ.get("JARVIS_ECAPA_BREAKER_FAILURE_THRESHOLD", 5)
        )
        self.reset_timeout_s = float(
            reset_timeout_s
            if reset_timeout_s is not None
            else os.environ.get("JARVIS_ECAPA_BREAKER_RESET_TIMEOUT_S", 30.0)
        )
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    def can_execute(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            # Open: allow a half-open trial once the reset window has elapsed.
            if (time.monotonic() - self._opened_at) >= self.reset_timeout_s:
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self, error: Optional[BaseException] = None) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()


# --------------------------------------------------------------------------- #
# Lazy ECAPA probe — NO module-scope import of the facade.
# --------------------------------------------------------------------------- #
def try_default_ecapa_embedder() -> Optional[Embedder]:
    """Lazily probe ``backend.core.ecapa_facade.extract_embedding``.

    Returns a single-arg ``Embedder`` if the facade is importable and callable,
    else ``None`` (sandbox / no torch / no model). Imported INSIDE the function
    so this module never imports the kernel at module scope.
    """
    try:
        from backend.core import ecapa_facade  # type: ignore
    except Exception:
        return None
    extractor = getattr(ecapa_facade, "extract_embedding", None)
    if not callable(extractor):
        return None

    def _embed(x: np.ndarray) -> np.ndarray:
        return np.asarray(extractor(x), dtype=np.float64)

    return _embed


# --------------------------------------------------------------------------- #
# Verdict + result
# --------------------------------------------------------------------------- #
class Verdict(str, Enum):
    ACCEPTED = "verdict_accepted"
    REJECTED = "verdict_rejected"


@dataclass(frozen=True)
class AuthResult:
    verdict: Verdict
    score: float
    reason: str


def _cosine_similarity(u: np.ndarray, v: np.ndarray) -> float:
    """L2-cosine similarity; 0.0 for a zero vector (no NaN)."""
    u = np.asarray(u, dtype=np.float64).reshape(-1)
    v = np.asarray(v, dtype=np.float64).reshape(-1)
    nu = float(np.linalg.norm(u))
    nv = float(np.linalg.norm(v))
    if nu == 0.0 or nv == 0.0:
        return 0.0
    return float(np.dot(u, v) / (nu * nv))


# --------------------------------------------------------------------------- #
# The matrix
# --------------------------------------------------------------------------- #
class BiometricExecutionMatrix:
    """Synchronous, fail-secure ECAPA speaker-authentication front-end."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        baseline_embedding: np.ndarray,
        accept_threshold: float,
        breaker: Optional[CircuitBreakerLike] = None,
        event_sink: Optional[EventSink] = None,
        swarm_trigger: Optional[SwarmTrigger] = None,
    ) -> None:
        self._embedder = embedder
        self._baseline = np.asarray(baseline_embedding, dtype=np.float64).reshape(-1)
        self._accept_threshold = float(accept_threshold)
        self.breaker: CircuitBreakerLike = (
            breaker if breaker is not None else LocalOOMCircuitBreaker()
        )
        self._event_sink = event_sink
        self._swarm_trigger = swarm_trigger
        # Strong refs to fire-and-forget telemetry tasks so they are not GC'd
        # mid-flight (asyncio holds only weak refs to bare tasks).
        self._pending_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # The headline: synchronous, fail-secure authentication.
    # ------------------------------------------------------------------ #
    def authenticate(self, uniform_tensor: np.ndarray) -> AuthResult:
        # 1. Breaker open -> stay locked. Embedder is NEVER called.
        if not self.breaker.can_execute():
            return AuthResult(Verdict.REJECTED, 0.0, "circuit_open_locked")

        # 2. Try the extractor.
        try:
            emb = self._embedder(uniform_tensor)
            self.breaker.record_success()
            score = _cosine_similarity(emb, self._baseline)
            verdict = (
                Verdict.ACCEPTED
                if score >= self._accept_threshold
                else Verdict.REJECTED
            )
            return AuthResult(verdict, score, "evaluated")

        # 3. RAM-pressure failure -> FAIL SECURE + async telemetry, sync return.
        except _RAM_PRESSURE_ERRORS as err:
            self.breaker.record_failure(err)
            # Fire telemetry/swarm fire-and-forget BEFORE returning, but it must
            # never block or alter the secure-lock verdict below.
            self._emit_pressure_async(
                {
                    "component": "ecapa",
                    "error_class": type(err).__name__,
                    "error_message": str(err),
                    "rss_hint": self._rss_hint(),
                    "verdict": Verdict.REJECTED.value,
                    "reason": "oom_fail_secure",
                }
            )
            return AuthResult(Verdict.REJECTED, 0.0, "oom_fail_secure")

        # 4. Any other error -> also fail secure (no telemetry — not RAM class).
        except Exception as err:  # noqa: BLE001 — fail-secure is the contract.
            try:
                self.breaker.record_failure(err)
            except Exception:
                pass
            return AuthResult(Verdict.REJECTED, 0.0, "error_fail_secure")

    # ------------------------------------------------------------------ #
    # Fire-and-forget telemetry — NEVER blocks, NEVER raises, NEVER delays.
    # ------------------------------------------------------------------ #
    def _emit_pressure_async(self, payload: dict) -> None:
        """Dispatch RESOURCE_PRESSURE telemetry + swarm trigger out-of-band.

        Guarantees:
          * Returns immediately (does not await the sinks).
          * Any failure is swallowed (telemetry never affects the verdict).
          * If a running loop exists, schedules a task (strong-ref'd, no-GC).
          * If no running loop, fires best-effort on a daemon thread.
        """
        try:
            if self._event_sink is None and self._swarm_trigger is None:
                return

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None:
                task = loop.create_task(self._dispatch_async(payload))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
            else:
                thread = threading.Thread(
                    target=self._dispatch_sync,
                    args=(payload,),
                    name="ecapa-resource-pressure",
                    daemon=True,
                )
                thread.start()
        except Exception:
            # Telemetry dispatch must never propagate.
            pass

    async def _dispatch_async(self, payload: dict) -> None:
        """Fire both sinks CONCURRENTLY; each guarded independently so a slow or
        failing event sink can never stall (or starve) the swarm dispatch."""
        await asyncio.gather(
            self._call_sink_maybe_async(
                self._event_sink, "resource_pressure", payload
            ),
            self._call_trigger_maybe_async(self._swarm_trigger, payload),
        )

    def _dispatch_sync(self, payload: dict) -> None:
        """Best-effort synchronous dispatch on a daemon thread (no loop)."""
        if self._event_sink is not None:
            try:
                res = self._event_sink("resource_pressure", payload)
                self._drain_if_coro(res)
            except Exception:
                pass
        if self._swarm_trigger is not None:
            try:
                res = self._swarm_trigger(payload)
                self._drain_if_coro(res)
            except Exception:
                pass

    @staticmethod
    def _drain_if_coro(res: Any) -> None:
        """If a sink returned a coroutine in a sync context, run it to completion
        on a throwaway loop so it doesn't leak — best-effort, errors swallowed."""
        if asyncio.iscoroutine(res):
            try:
                asyncio.run(res)
            except Exception:
                pass

    @staticmethod
    async def _call_sink_maybe_async(
        sink: Optional[EventSink], event_type: str, payload: dict
    ) -> None:
        if sink is None:
            return
        try:
            res = sink(event_type, payload)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass

    @staticmethod
    async def _call_trigger_maybe_async(
        trigger: Optional[SwarmTrigger], payload: dict
    ) -> None:
        if trigger is None:
            return
        try:
            res = trigger(payload)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass

    @staticmethod
    def _rss_hint() -> Optional[float]:
        """Best-effort resident-set-size hint (MiB). None if unavailable.

        stdlib-only probe (``resource``); never raises. Documents the pressure
        snapshot at the moment of the OOM for the diagnostic swarm.
        """
        try:
            import resource  # POSIX-only; lazy + guarded.

            ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux reports KiB, macOS reports bytes; normalize roughly to MiB.
            import sys

            divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
            return float(ru) / divisor
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Optional async surface (awaits a coroutine embedder if provided).
    # ------------------------------------------------------------------ #
    async def authenticate_async(self, uniform_tensor: np.ndarray) -> AuthResult:
        """Async variant: awaits the embedder if it is a coroutine function.

        Same fail-secure contract as the sync path. The headline remains the
        synchronous ``authenticate``; this is a convenience for an async ECAPA
        facade.
        """
        if not self.breaker.can_execute():
            return AuthResult(Verdict.REJECTED, 0.0, "circuit_open_locked")
        try:
            res = self._embedder(uniform_tensor)
            emb = await res if asyncio.iscoroutine(res) else res
            self.breaker.record_success()
            score = _cosine_similarity(emb, self._baseline)
            verdict = (
                Verdict.ACCEPTED
                if score >= self._accept_threshold
                else Verdict.REJECTED
            )
            return AuthResult(verdict, score, "evaluated")
        except _RAM_PRESSURE_ERRORS as err:
            self.breaker.record_failure(err)
            self._emit_pressure_async(
                {
                    "component": "ecapa",
                    "error_class": type(err).__name__,
                    "error_message": str(err),
                    "rss_hint": self._rss_hint(),
                    "verdict": Verdict.REJECTED.value,
                    "reason": "oom_fail_secure",
                }
            )
            return AuthResult(Verdict.REJECTED, 0.0, "oom_fail_secure")
        except Exception as err:  # noqa: BLE001
            try:
                self.breaker.record_failure(err)
            except Exception:
                pass
            return AuthResult(Verdict.REJECTED, 0.0, "error_fail_secure")
