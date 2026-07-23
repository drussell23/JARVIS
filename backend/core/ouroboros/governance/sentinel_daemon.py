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
) -> SentinelResult:
    """The read-only watch loop. The ONLY writes it performs are the two
    ``provider_state`` upserts (start → DEGRADED, recovery → HEALTHY). It issues
    NO git / os.system / subprocess / file-write call — those belong solely to
    the orchestrator (Single-Writer). On stable HEALTHY it writes HEALTHY and
    calls ``terminate`` (the process then exits). Never raises."""
    import asyncio as _asyncio

    now = now_fn or time.monotonic
    sleep = sleep_fn or _asyncio.sleep
    fconn = forecast_conn if forecast_conn is not None else state_conn

    t0 = now()
    mark_degraded(state_conn, provider, reason="sentinel_watch_start")
    logger.info(
        "[Sentinel] headless watch START provider=%s forecast_ttr≈%.0fs "
        "stability=%d (read-only; SQLite IPC handoff)",
        provider, forecast_ttr(fconn), stability,
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

        if healthy:
            healthy_streak += 1
            if healthy_streak >= stability:
                mark_healthy(
                    state_conn, provider,
                    reason=f"staged_2pass_ok x{stability} (probe #{probes})",
                )
                logger.info(
                    "[Sentinel] %s HEALTHY written to provider_state — "
                    "terminating (Single-Writer handoff to orchestrator)", provider,
                )
                if terminate is not None:
                    terminate()
                return SentinelResult(recovered=True, probes=probes, reason="healthy_written")
        else:
            healthy_streak = 0

    logger.info("[Sentinel] gave up after max_wait_s=%.0f (stayed DEGRADED)", max_wait_s)
    return SentinelResult(recovered=False, probes=probes, reason="max_wait_exceeded")


__all__ = ["SentinelResult", "run_sentinel"]
