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
import time
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
# Deep Inference Probe (data-plane heartbeat) -- Run-#13 fix (2026-06-28)
# ---------------------------------------------------------------------------
#
# The legacy ``_default_dw_probe`` does ``GET /models`` -- a CONTROL-PLANE
# reachability check. In soak bt-2026-06-29-032526 DW's control plane answered
# 200 the whole session while the *data plane* (batch/generation) deadlocked at
# 180s per op -- so the probe stayed green and the early signal never fired.
# The Deep Inference Probe dispatches a deterministic ``max_tokens=1`` generation
# straight at the data plane and bounds it with a DYNAMICALLY-resolved
# ``asyncio.wait_for`` deadline. A wedged inference queue times the probe out in
# a fraction of a second and flips ``is_degrading()`` long before 180s.

def deep_probe_enabled() -> bool:
    """Use the deep INFERENCE probe (data plane) instead of the legacy
    GET /models reachability probe. DEFAULT-ON, but composes UNDER the
    heartbeat master gate (which is itself default-OFF) -- so unset env is
    byte-identical legacy. Rollback: ``JARVIS_DW_DEEP_PROBE_ENABLED=false``
    restores the control-plane probe."""
    return _enabled("JARVIS_DW_DEEP_PROBE_ENABLED", "true")


def _resolve_deep_probe_timeout(baseline_s: Optional[float] = None) -> float:
    """Resolve the deep-probe ``asyncio.wait_for`` deadline DYNAMICALLY.

    Resolution order (NEVER a hardcoded literal):
      1. Explicit operator pin ``JARVIS_DW_DEEP_PROBE_TIMEOUT_S`` (if > 0) wins.
      2. Otherwise a BASELINE-LATENCY metric: ``mult x baseline`` where
         *baseline* is the heartbeat's observed healthy-probe EWMA (passed in)
         or ``JARVIS_DW_DEEP_PROBE_BASELINE_S`` when no sample exists yet.
    Always clamped to ``[floor, ceil]`` so it is never 0 and never unbounded.
    A slower-but-healthy DW gets a proportionally larger deadline (no false
    degrade); a fast DW a tight one. Fail-soft: any parse error -> the metric
    path with defaults."""
    override = (os.environ.get("JARVIS_DW_DEEP_PROBE_TIMEOUT_S", "") or "").strip()
    if override:
        try:
            v = float(override)
            if v > 0:
                return v
        except (ValueError, TypeError):
            pass
    base = (
        baseline_s
        if (baseline_s is not None and baseline_s > 0)
        else _env_float("JARVIS_DW_DEEP_PROBE_BASELINE_S", 0.5)
    )
    mult = max(1.0, _env_float("JARVIS_DW_DEEP_PROBE_TIMEOUT_MULT", 4.0))
    floor = max(0.1, _env_float("JARVIS_DW_DEEP_PROBE_TIMEOUT_FLOOR_S", 1.0))
    ceil = max(floor, _env_float("JARVIS_DW_DEEP_PROBE_TIMEOUT_CEIL_S", 15.0))
    return max(floor, min(ceil, mult * base))


def _deep_probe_prompt() -> str:
    """The deterministic, ultra-low-latency probe prompt. Env-tunable so an
    operator can swap it for a model-specific minimal prompt; never raises."""
    return (
        os.environ.get("JARVIS_DW_DEEP_PROBE_PROMPT", "Return the digit 1")
        or "Return the digit 1"
    )


def _http_post_json(url: str, headers: dict, payload: bytes, timeout: float) -> str:
    """Injectable sync POST boundary (stdlib urllib). Returns the response body.
    Raises on transport/HTTP failure. Tests monkeypatch this -- ZERO real net."""
    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


async def _resolve_dw_probe_transport():
    """Gap 1 -- resolve the probe's ``(url, headers)`` from the SAME aegis bridge
    the real DW provider uses, so the probe impersonates real traffic at the
    protocol level (Aegis base URL + SESSION BEARER -- NOT the confiscated DW API
    key, which 401'd in soak bt-2026-06-29-055555).

    Fail-soft: an aegis base-URL resolve error falls back to the direct DW base.
    The session-header fetch is best-effort -- a failure yields no Authorization
    rather than crashing the probe. NEVER raises."""
    headers = {"Content-Type": "application/json"}
    try:
        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            aegis_provider_bridge as _apb,
        )
        base = _apb.dw_aegis_base_url()
    except Exception as exc:  # noqa: BLE001 -- fall back to direct DW
        logger.debug("[DWHeartbeat] aegis base resolve fail-soft err=%r", exc)
        return (_dw_base_url() + "/chat/completions", headers)
    url = base.rstrip("/") + "/chat/completions"
    try:
        from backend.core.ouroboros.governance import (  # noqa: PLC0415
            aegis_provider_bridge as _apb,
        )
        auth = await _apb.dw_session_auth_header()
        if isinstance(auth, dict):
            headers.update(auth)
    except Exception as exc:  # noqa: BLE001 -- best-effort bearer
        logger.debug("[DWHeartbeat] aegis session header fail-soft err=%r", exc)
    return (url, headers)


async def _default_dw_inference_dispatch() -> str:
    """Default deep-probe boundary: a real ``max_tokens=1`` generation against
    the DW data plane, routed through the SAME (Aegis-aware) transport + auth the
    real DW provider uses (Gap 1 -- faithful impersonation, no false 401).

    Returns the (possibly empty) content string. Raises on any transport/HTTP
    failure -- the caller treats BOTH an exception and an empty string as a
    degrade signal."""
    import json  # noqa: PLC0415

    url, headers = await _resolve_dw_probe_transport()
    model = (
        os.environ.get("JARVIS_DW_DEEP_PROBE_MODEL", "")
        or os.environ.get("DOUBLEWORD_MODEL", "")
        or "Qwen/Qwen3.5-397B-A17B-FP8"
    )
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": _deep_probe_prompt()}],
            "max_tokens": 1,
            "temperature": 0.0,
            "stream": False,
        }
    ).encode("utf-8")
    # Per-request timeout is a belt-and-braces backstop; the authoritative bound
    # is the caller's asyncio.wait_for (dynamic). Off-loaded so the blocking POST
    # never stalls the event loop.
    loop = asyncio.get_event_loop()
    body = await loop.run_in_executor(
        None, _http_post_json, url, headers, payload, _resolve_deep_probe_timeout(),
    )
    data = json.loads(body)
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


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
        inference_dispatch_fn: Optional[Callable[[], Any]] = None,
    ) -> None:
        # The deep-probe inference boundary (data plane). Injectable for tests;
        # default is a real max_tokens=1 DW generation.
        self._inference_dispatch_fn = (
            inference_dispatch_fn or _default_dw_inference_dispatch
        )
        # Baseline healthy-probe latency metric (EWMA) -- drives the DYNAMIC
        # deep-probe timeout. Seeded from env; updated on each healthy probe.
        self._baseline_latency_s: float = _env_float(
            "JARVIS_DW_DEEP_PROBE_BASELINE_S", 0.5
        )
        # Default probe selection: the deep INFERENCE probe (data plane) when
        # armed, else the legacy GET /models reachability probe (rollback).
        if probe_fn is not None:
            self._probe_fn: Callable[[], Any] = probe_fn
        elif deep_probe_enabled():
            self._probe_fn = self._deep_inference_probe
        else:
            self._probe_fn = _default_dw_probe
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
    # Deep Inference Probe (data plane) -- the testable core of the Run-#13 fix
    # ------------------------------------------------------------------

    def _record_baseline_latency(self, sample_s: float) -> None:
        """EWMA-update the healthy-probe baseline latency metric. Fail-soft."""
        try:
            if sample_s <= 0:
                return
            alpha = max(0.0, min(1.0, _env_float("JARVIS_DW_DEEP_PROBE_EWMA_ALPHA", 0.3)))
            cur = self._baseline_latency_s
            self._baseline_latency_s = (
                sample_s if cur <= 0 else alpha * sample_s + (1.0 - alpha) * cur
            )
        except Exception:  # noqa: BLE001
            pass

    async def _deep_inference_probe(self) -> bool:
        """Dispatch a deterministic ``max_tokens=1`` generation at the DW data
        plane, bounded by a DYNAMICALLY-resolved ``asyncio.wait_for`` deadline.

        Returns True iff a NON-EMPTY token comes back inside the deadline. A
        timeout (wedged inference queue), a transport error, OR an empty/blank
        result are all degrade signals (False). NEVER raises -- the caller
        (``beat``) records the verdict. A healthy probe updates the baseline
        latency metric so the deadline self-tunes to real DW speed."""
        timeout = _resolve_deep_probe_timeout(self._baseline_latency_s)

        async def _invoke() -> Any:
            res = self._inference_dispatch_fn()
            if asyncio.iscoroutine(res):
                res = await res
            return res

        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(_invoke(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug(
                "[DWHeartbeat] deep probe TIMEOUT after %.3fs (data-plane "
                "deadlock) -> degrade signal", timeout,
            )
            return False
        except Exception as exc:  # noqa: BLE001 -- a dispatch error IS a degrade
            logger.debug("[DWHeartbeat] deep probe dispatch err=%r -> degrade", exc)
            return False
        ok = bool(result) and bool(str(result).strip())
        if ok:
            self._record_baseline_latency(time.monotonic() - t0)
        return ok

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
    "deep_probe_enabled",
]
