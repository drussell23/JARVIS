"""Headless recovery Sentinel — a read-only daemon with ONE SQLite mutation.

Post-#70033, the Sentinel is stripped of ALL git / filesystem-write / subprocess
privileges. Its entire contract:

  1. Deep-probe DW's inference lane at forecaster-predicted intervals (staged
     2-pass verification, injected as ``probe_fn``) — never spams, never
     generates side effects.
  2. On ``stability`` consecutive HEALTHY probes, write ONE row to the
     ``provider_state`` table (``DEGRADED`` → ``HEALTHY`` + timestamp) — its ONLY
     permitted mutation, the Single-Writer handoff.
  3. Self-terminate immediately (freeing the socket pool). The orchestrator —
     the exclusive owner of the git index + ``SagaApplyStrategy`` — polls the
     table and does the fixture-seed + AIMD slow-start launch itself.

Composes the #70030 forecaster (predicted intervals) + ``provider_state`` IPC
(#70021 DB). It imports NO git/os/subprocess machinery. Never raises.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from backend.core.ouroboros.governance.dw_outage_forecaster import (
    forecast_ttr,
    predict_sleep_s,
)
from backend.core.ouroboros.governance.provider_state import (
    mark_degraded,
    mark_healthy,
)

logger = logging.getLogger("Ouroboros.SentinelDaemon")

ProbeFn = Callable[[], Awaitable[bool]]
NowFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


@dataclass
class SentinelResult:
    recovered: bool
    probes: int
    reason: str


async def run_sentinel(
    state_conn,
    probe_fn: ProbeFn,
    *,
    provider: str = "doubleword",
    forecast_conn=None,
    now_fn: Optional[NowFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    terminate: Optional[Callable[[], None]] = None,
    max_wait_s: float = 10800.0,
    stability: int = 2,
    adaptive_hysteresis: bool = True,
    jitter_window_s: float = 1800.0,
    jitter_cap: int = 5,
    jitter_now_fn: Optional[NowFn] = None,
) -> SentinelResult:
    """The read-only watch loop. The ONLY writes it performs are the two
    ``provider_state`` upserts (start → DEGRADED, recovery → HEALTHY). It issues
    NO git / os.system / subprocess / file-write call — those belong solely to
    the orchestrator (Single-Writer). On stable HEALTHY it writes HEALTHY and
    calls ``terminate`` (the process then exits).

    Adaptive Hysteresis: the consecutive-pass requirement is recomputed each loop
    as ``stability + jitter_index`` (clamped to ``jitter_cap``) from the
    ``provider_jitter_events`` SQLite window — so a flapping (warm-up-jittering)
    provider must clear a HIGHER bar, and the requirement decays back to
    ``stability`` on its own as errors age out of the window. Never raises."""
    import asyncio as _asyncio

    now = now_fn or time.monotonic
    sleep = sleep_fn or _asyncio.sleep
    jnow = jitter_now_fn or time.time
    fconn = forecast_conn if forecast_conn is not None else state_conn

    def _required() -> int:
        if not adaptive_hysteresis:
            return stability
        try:
            from backend.core.ouroboros.governance.provider_jitter import (
                required_consecutive_passes,
            )
            return required_consecutive_passes(
                state_conn, provider, base=stability, cap=jitter_cap,
                window_s=jitter_window_s, now=jnow(),
            )
        except Exception:  # noqa: BLE001
            return stability

    t0 = now()
    mark_degraded(state_conn, provider, reason="sentinel_watch_start")
    logger.info(
        "[Sentinel] headless watch START provider=%s forecast_ttr≈%.0fs "
        "base_stability=%d adaptive=%s (read-only; SQLite IPC handoff)",
        provider, forecast_ttr(fconn), stability, adaptive_hysteresis,
    )
    healthy_streak = 0
    probes = 0
    while now() - t0 < max_wait_s:
        interval = predict_sleep_s(fconn, now() - t0)
        await sleep(interval)
        probes += 1
        try:
            healthy = bool(await probe_fn())
        except Exception:  # noqa: BLE001 — a failed probe is just "still down"
            healthy = False

        required = _required()   # Adaptive Hysteresis — recomputed each loop
        if healthy:
            healthy_streak += 1
            if healthy_streak >= required:
                mark_healthy(
                    state_conn, provider,
                    reason=f"staged_2pass_ok x{required} (jitter-adaptive, probe #{probes})",
                )
                logger.info(
                    "[Sentinel] %s HEALTHY written to provider_state after %d "
                    "consecutive passes (required=%d, jitter-adaptive) — "
                    "terminating (Single-Writer handoff)", provider, healthy_streak, required,
                )
                if terminate is not None:
                    terminate()
                return SentinelResult(recovered=True, probes=probes, reason="healthy_written")
            logger.info(
                "[Sentinel] %s pass %d/%d (jitter-adaptive stability window held back)",
                provider, healthy_streak, required,
            )
        else:
            healthy_streak = 0

    logger.info("[Sentinel] gave up after max_wait_s=%.0f (stayed DEGRADED)", max_wait_s)
    return SentinelResult(recovered=False, probes=probes, reason="max_wait_exceeded")


__all__ = ["SentinelResult", "run_sentinel"]
