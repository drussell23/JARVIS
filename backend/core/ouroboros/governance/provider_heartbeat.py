"""provider_heartbeat.py -- Gap 1 of the Sovereign Failover Mesh (2026-06-24).

A lightweight ASYNC background loop that PROBES DoubleWord (DW) health
PROACTIVELY -- a cheap, bounded-timeout health request to the DW endpoint on a
fixed interval -- and records each verdict into the EXISTING
:class:`SurfaceHealthLedger` (reuse; no new ledger). This gives the failover
lifecycle an EARLY warning of degradation *before* an op formally times out
(~53s) or the quarantine gradient declares a full ``is_global_outage`` (which
needs a FULL window of 5 zero-success sweeps).

Why a separate heartbeat instead of riding the op-driven gradient?
------------------------------------------------------------------
The quarantine ``ProviderHealthGradient`` only learns from *live op dispatches*.
On a quiet system (or one whose ops are already wedged), it can take a long time
to fill the window. The heartbeat is a CHEAP, INDEPENDENT, time-driven signal:
it surfaces ``is_degrading()`` the moment ``consecutive_failures`` crosses the
degrade streak (default 2 -- strictly LESS than the outage window of 5). The
lifecycle can then PRE-WARM J-Prime so the node is warm by the time DW formally
collapses and the op drops into the Cryo-DLQ.

Degradation taxonomy (reuse SurfaceVerdict)
-------------------------------------------
  * probe OK              -> SurfaceVerdict.HEALTHY (streak resets to 0)
  * probe failed / errored -> SurfaceVerdict.TRANSPORT_DEGRADED (streak +1)
A probe *error* is itself a degrade signal -- the loop NEVER crashes on it.
``is_degrading()`` == the recorded ``consecutive_failures`` for the
DIRECT_STREAMING surface (DW's SSE generation lane) >= the degrade streak.

Env gates
---------
JARVIS_DW_HEARTBEAT_ENABLED    default "false" (default-OFF byte-identical)
    OFF -> beat()/run() are inert no-ops; is_degrading() is always False; no
    background task; today's behavior exactly.
JARVIS_DW_HEARTBEAT_INTERVAL_S default 10.0 (probe cadence in run())
JARVIS_DW_DEGRADE_STREAK       default 2 (< the outage window of 5)
JARVIS_DW_HEARTBEAT_PROBE_TIMEOUT_S default 3.0 (bounded per-probe deadline)
DOUBLEWORD_BASE_URL            default https://api.doubleword.ai/v1 (probe target)

Fail-soft / bounded throughout. Lazy-imports heavy deps so the module imports
clean in tests. The default probe uses stdlib urllib so tests can inject a fake
boundary with zero real network.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Optional

from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceKind,
    SurfaceVerdict,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers (fail-soft, never raise)
# ---------------------------------------------------------------------------

def _enabled(name: str, default: str) -> bool:
    val = (os.environ.get(name, default) or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def heartbeat_enabled() -> bool:
    """Master gate. DEFAULT-OFF (byte-identical legacy when unset).

    The operator arms this at soak time via
    ``export JARVIS_DW_HEARTBEAT_ENABLED=true`` alongside
    ``JARVIS_FAILOVER_LIFECYCLE_ENABLED``.
    """
    return _enabled("JARVIS_DW_HEARTBEAT_ENABLED", "false")


def _interval_s() -> float:
    return max(0.001, _env_float("JARVIS_DW_HEARTBEAT_INTERVAL_S", 10.0))


def _degrade_streak() -> int:
    # Clamp to >= 1 and (defensively) below the quarantine outage window so the
    # heartbeat is always the EARLIER signal. The outage window default is 5.
    return max(1, _env_int("JARVIS_DW_DEGRADE_STREAK", 2))


def _probe_timeout_s() -> float:
    return max(0.1, _env_float("JARVIS_DW_HEARTBEAT_PROBE_TIMEOUT_S", 3.0))


def _dw_base_url() -> str:
    return (
        os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
        or "https://api.doubleword.ai/v1"
    ).rstrip("/")


# ---------------------------------------------------------------------------
# Default probe boundary (stdlib urllib -- injectable for tests)
# ---------------------------------------------------------------------------

def _default_dw_probe() -> bool:
    """Cheap DW reachability probe -- a bounded GET to ``{base}/models``.

    Returns True iff the endpoint answers with a non-5xx status within the
    bounded timeout. NEVER raises -- any error (timeout, refused, DNS) is a
    failed probe (returns False), which the heartbeat treats as a degrade
    signal. This is a *reachability* check, NOT a generation -- it is cheap
    and side-effect-free.
    """
    url = _dw_base_url() + "/models"
    try:
        import urllib.request  # noqa: PLC0415

        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=_probe_timeout_s()
        ) as resp:
            status = getattr(resp, "status", 200)
            return 200 <= int(status) < 500
    except Exception as exc:  # noqa: BLE001 -- a probe error IS a degrade signal
        logger.debug("[DWHeartbeat] default probe fail-soft err=%r", exc)
        return False


# ---------------------------------------------------------------------------
# DWHeartbeat
# ---------------------------------------------------------------------------

class DWHeartbeat:
    """Preemptive DW health heartbeat. Records verdicts into the shared
    SurfaceHealthLedger; exposes an EARLY ``is_degrading()`` signal.

    All boundaries are injectable for testability -- a fake probe + an
    in-process ledger mean NO real network is touched.
    """

    # The DW surface this heartbeat tracks: the SSE generation lane (the one
    # that actually carries O+V's generation traffic).
    _SURFACE = SurfaceKind.DIRECT_STREAMING

    def __init__(
        self,
        *,
        probe_fn: Optional[Callable[[], Any]] = None,
        ledger: Optional[SurfaceHealthLedger] = None,
    ) -> None:
        self._probe_fn = probe_fn or _default_dw_probe
        # Reuse the shared ledger by default (the same on-disk file the rest of
        # the surface-health stack reads). Tests inject an in-memory ledger.
        self._ledger = ledger if ledger is not None else SurfaceHealthLedger()
        self._stopped = False
        # Cache of the last recorded verdict for a cheap read surface (the
        # ledger is the source of truth; this avoids a lock round-trip on the
        # hot is_degrading() path).
        self._last_verdict: Optional[SurfaceVerdict] = None
        self._last_streak: int = 0

    # ------------------------------------------------------------------
    # Public read surface (consumed by the failover lifecycle pre-warm gate)
    # ------------------------------------------------------------------

    def is_degrading(self) -> bool:
        """True iff DW is DEGRADED but NOT yet a full outage.

        Reads the live ``consecutive_failures`` streak for the DIRECT_STREAMING
        surface from the shared ledger and compares against the degrade streak
        (default 2 < the outage window of 5). OFF -> always False (inert).
        Fail-soft -> False on any error (conservative: no false pre-warm).
        """
        if not heartbeat_enabled():
            return False
        try:
            rec = self._ledger.verdict_for(self._SURFACE)
            if rec is None:
                return False
            return int(rec.consecutive_failures) >= _degrade_streak()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DWHeartbeat] is_degrading fail-soft err=%r", exc)
            return False

    def latest_verdict(self) -> Optional[SurfaceVerdict]:
        """The most recently recorded SurfaceVerdict, or None if no beat yet.
        OFF -> None (inert)."""
        if not heartbeat_enabled():
            return None
        try:
            rec = self._ledger.verdict_for(self._SURFACE)
            return rec.verdict if rec is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DWHeartbeat] latest_verdict fail-soft err=%r", exc)
            return None

    def consecutive_failures(self) -> int:
        """Current degrade streak for the DW SSE surface (0 when healthy)."""
        if not heartbeat_enabled():
            return 0
        try:
            rec = self._ledger.verdict_for(self._SURFACE)
            return int(rec.consecutive_failures) if rec is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------
    # One probe + record (the testable core)
    # ------------------------------------------------------------------

    async def beat(self) -> None:
        """Fire one probe and record the verdict into the ledger. Fail-soft.

        OFF -> inert no-op (no probe, no record): byte-identical legacy.
        A probe error is itself a degrade signal -- the verdict recorded is
        TRANSPORT_DEGRADED (never propagates the exception).
        """
        if not heartbeat_enabled():
            return
        ok = False
        try:
            result = self._probe_fn()
            if asyncio.iscoroutine(result):
                result = await result
            ok = bool(result)
        except Exception as exc:  # noqa: BLE001 -- a probe error IS a degrade
            logger.debug("[DWHeartbeat] probe raised (degrade signal) err=%r", exc)
            ok = False

        verdict = SurfaceVerdict.HEALTHY if ok else SurfaceVerdict.TRANSPORT_DEGRADED
        try:
            rec = self._ledger.record(
                self._SURFACE,
                verdict,
                diagnostic="dw_heartbeat",
            )
            self._last_verdict = rec.verdict
            self._last_streak = int(rec.consecutive_failures)
            if not ok and self._last_streak >= _degrade_streak():
                logger.warning(
                    "[DWHeartbeat] DW DEGRADING: streak=%d (>=%d) surface=%s "
                    "-- EARLY pre-warm signal armed (outage window not yet full)",
                    self._last_streak, _degrade_streak(), self._SURFACE.value,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DWHeartbeat] record fail-soft err=%r", exc)

    # ------------------------------------------------------------------
    # Async run() driver (bounded, cancellable -- no event-loop starvation)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Beat on a fixed interval. Gated -- OFF returns immediately (inert).

        Cadence = JARVIS_DW_HEARTBEAT_INTERVAL_S (asyncio.sleep, Python 3.9+
        safe -- NOT asyncio.timeout). Fail-soft: a beat error never breaks the
        loop. Cancellable via stop() or task cancellation.
        """
        if not heartbeat_enabled():
            logger.debug("[DWHeartbeat] run(): disabled -- inert")
            return
        self._stopped = False
        logger.info(
            "[DWHeartbeat] run() loop started interval=%.1fs degrade_streak=%d",
            _interval_s(), _degrade_streak(),
        )
        while not self._stopped:
            try:
                await self.beat()
            except Exception as exc:  # noqa: BLE001 -- belt-and-braces
                logger.warning("[DWHeartbeat] run beat fail-soft err=%r", exc)
            try:
                await asyncio.sleep(_interval_s())
            except asyncio.CancelledError:
                break

    def stop(self) -> None:
        self._stopped = True


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: Optional[DWHeartbeat] = None


def get_dw_heartbeat() -> DWHeartbeat:
    """Return (or lazily create) the process-wide DW heartbeat singleton."""
    global _singleton  # noqa: PLW0603
    if _singleton is None:
        _singleton = DWHeartbeat()
    return _singleton


def _reset_singleton_for_tests() -> None:
    """Test hook: drop the singleton so a fresh heartbeat is created."""
    global _singleton  # noqa: PLW0603
    _singleton = None


__all__ = [
    "DWHeartbeat",
    "get_dw_heartbeat",
    "heartbeat_enabled",
]
