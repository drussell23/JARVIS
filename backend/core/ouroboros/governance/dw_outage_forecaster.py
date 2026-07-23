"""Predictive DW Outage Forecaster — ML-driven recovery watch, no naive polling.

The naive answer to "wait for DW to recover" is a ``while True: ping; sleep(5)``
loop — which spams the endpoint (IP-ban / wasted compute risk) and learns
nothing. This replaces it with a lightweight predictive controller that gets
smarter every outage:

  * **SQLite outage memory (DRY on #70021's DB).** Every DW outage's
    ``(outage_start, recovery, time-to-recovery)`` is recorded into a
    ``dw_outage_history`` table in the SAME ``.jarvis/chunk_strategy.db`` the
    StrategyOutcomeLogger uses — one durable telemetry substrate, two concerns.

  * **EMA + Bayesian-shrinkage forecaster (native math, NO ML libs).** The
    predicted TTR is an exponential moving average of historical TTRs, shrunk
    toward a prior mean during the cold-start (low-data) phase via a
    ``conf = n/(n+k)`` Bayesian weight — a single weird sample can never
    dominate, and with zero history it returns the prior (never NaN / never 0).

  * **Dynamic bounded backoff.** The watch sleep is driven by the forecast:
    ``clamp(remaining_predicted_TTR * 0.5, floor, ceiling)``. As the outage runs
    longer the predicted-remaining shrinks, so the watcher probes more often as
    expected recovery nears — bounded ALWAYS in ``[floor=30s, ceiling=600s]`` so
    a cold-start over-estimate can never cause an indefinite sleep.

  * **TCP-style AIMD slow-start (thundering-herd protection).** On predicted
    recovery the swarm is NOT unleashed at max concurrency. ``AIMDController``
    starts the semaphore limit at 1 (a single probe agent); each success
    additively increases it (+1), each failure multiplicatively decreases it
    (//2), bounded by the swarm's own ``swarm_concurrency()`` ceiling — so a
    recovering DW server is ramped onto, never slammed.

Pure + deterministic (injectable clock / sleep / probe). Composes
``provider_heartbeat.DWHeartbeat`` (the signal, injected as ``probe_fn``),
``chunk_swarm.swarm_concurrency`` (the AIMD ceiling), and the #70021 SQLite
layer. Never raises on the telemetry path.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger("Ouroboros.DWOutageForecaster")

_OUTAGE_TABLE = "dw_outage_history"

# --- env knobs (all bounded, no hardcoded magic in the hot path) -----------
_PRIOR_ENV = "JARVIS_DW_FORECAST_PRIOR_TTR_S"
_DEFAULT_PRIOR_TTR_S = 120.0
_ALPHA_ENV = "JARVIS_DW_FORECAST_EMA_ALPHA"
_DEFAULT_ALPHA = 0.4
_WARMUP_ENV = "JARVIS_DW_FORECAST_WARMUP_K"
_DEFAULT_WARMUP_K = 3
_FLOOR_ENV = "JARVIS_DW_FORECAST_SLEEP_FLOOR_S"
_DEFAULT_FLOOR_S = 30.0
_CEILING_ENV = "JARVIS_DW_FORECAST_SLEEP_CEILING_S"
_DEFAULT_CEILING_S = 600.0   # 10 minutes


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def default_forecast_db_path() -> str:
    """Same durable substrate as Wire #2's StrategyOutcomeLogger (#70021)."""
    return str(Path(".jarvis") / "chunk_strategy.db")


def open_forecast_db(path: Optional[str] = None) -> Optional[sqlite3.Connection]:
    """Open (creating parent dir + table) the outage-history DB. Returns None on
    any failure — the forecaster then rides the cold-start prior. Never raises."""
    try:
        p = Path(path or default_forecast_db_path())
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), check_same_thread=False)
        _ensure_outage_table(conn)
        return conn
    except sqlite3.Error:
        return None


def _ensure_outage_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_OUTAGE_TABLE} ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "outage_start_ts REAL NOT NULL, "
        "recovery_ts REAL, "
        "ttr_s REAL, "
        "surface TEXT DEFAULT 'direct_streaming')"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Outage lifecycle recording
# ---------------------------------------------------------------------------


def record_outage_start(
    conn: Optional[sqlite3.Connection], start_ts: float,
    *, surface: str = "direct_streaming",
) -> None:
    """Open an outage row (recovery_ts NULL). Never raises."""
    if conn is None:
        return
    try:
        _ensure_outage_table(conn)
        conn.execute(
            f"INSERT INTO {_OUTAGE_TABLE} (outage_start_ts, surface) VALUES (?, ?)",
            (float(start_ts), surface),
        )
        conn.commit()
    except sqlite3.Error:
        logger.debug("[DWForecaster] record_outage_start failed", exc_info=True)


def record_recovery(
    conn: Optional[sqlite3.Connection], start_ts: float, recovery_ts: float,
    *, surface: str = "direct_streaming",
) -> Optional[float]:
    """Close the open outage row for ``start_ts`` and compute its TTR. Returns
    the TTR seconds, or None. Never raises."""
    if conn is None:
        return None
    ttr = max(0.0, float(recovery_ts) - float(start_ts))
    try:
        _ensure_outage_table(conn)
        cur = conn.execute(
            f"UPDATE {_OUTAGE_TABLE} SET recovery_ts=?, ttr_s=? "
            f"WHERE recovery_ts IS NULL AND outage_start_ts=?",
            (float(recovery_ts), ttr, float(start_ts)),
        )
        if cur.rowcount == 0:
            # No matching open row (e.g. restart) — insert a completed one.
            conn.execute(
                f"INSERT INTO {_OUTAGE_TABLE} "
                f"(outage_start_ts, recovery_ts, ttr_s, surface) VALUES (?, ?, ?, ?)",
                (float(start_ts), float(recovery_ts), ttr, surface),
            )
        conn.commit()
        return ttr
    except sqlite3.Error:
        logger.debug("[DWForecaster] record_recovery failed", exc_info=True)
        return None


def _completed_ttrs(conn: Optional[sqlite3.Connection]) -> List[float]:
    """Historical TTRs, oldest → newest. Empty on any trouble."""
    if conn is None:
        return []
    try:
        _ensure_outage_table(conn)
        rows = conn.execute(
            f"SELECT ttr_s FROM {_OUTAGE_TABLE} "
            f"WHERE ttr_s IS NOT NULL ORDER BY outage_start_ts ASC"
        ).fetchall()
        return [float(r[0]) for r in rows if r[0] is not None]
    except sqlite3.Error:
        return []


# ---------------------------------------------------------------------------
# The forecaster (EMA + Bayesian shrinkage)
# ---------------------------------------------------------------------------


def forecast_ttr(
    conn: Optional[sqlite3.Connection],
    *,
    prior_mean: Optional[float] = None,
    alpha: Optional[float] = None,
    warmup_k: Optional[int] = None,
) -> float:
    """Predict the current outage's total duration (seconds).

    EMA of historical TTRs, Bayesian-shrunk toward ``prior_mean`` by a
    ``conf = n/(n+k)`` weight so the cold-start (small n) leans on the prior and
    a lone anomalous sample can never dominate. Zero history → the prior. Always
    a finite positive number (never NaN, never 0)."""
    prior = prior_mean if prior_mean is not None else _env_float(_PRIOR_ENV, _DEFAULT_PRIOR_TTR_S)
    a = alpha if alpha is not None else _env_float(_ALPHA_ENV, _DEFAULT_ALPHA)
    a = min(1.0, max(0.01, a))
    k = warmup_k if warmup_k is not None else _env_int(_WARMUP_ENV, _DEFAULT_WARMUP_K)
    k = max(0, k)

    ttrs = _completed_ttrs(conn)
    if not ttrs:
        return max(1.0, prior)   # cold start → prior

    ema = ttrs[0]
    for t in ttrs[1:]:
        ema = a * t + (1.0 - a) * ema

    n = len(ttrs)
    conf = n / (n + k) if (n + k) > 0 else 1.0    # Bayesian shrinkage weight
    predicted = conf * ema + (1.0 - conf) * prior
    return max(1.0, predicted)


def predict_sleep_s(
    conn: Optional[sqlite3.Connection],
    elapsed_outage_s: float,
    *,
    floor_s: Optional[float] = None,
    ceiling_s: Optional[float] = None,
) -> float:
    """The dynamically-adjusting, BOUNDED watch interval.

    Sleeps for roughly half the predicted-remaining TTR so the watcher wakes and
    probes as expected recovery nears; as ``elapsed`` grows the interval shrinks
    toward the floor. STRICTLY bounded ``[floor, ceiling]`` — a cold-start
    over-estimate can never cause an indefinite (or sub-floor spammy) sleep."""
    fl = floor_s if floor_s is not None else _env_float(_FLOOR_ENV, _DEFAULT_FLOOR_S)
    ce = ceiling_s if ceiling_s is not None else _env_float(_CEILING_ENV, _DEFAULT_CEILING_S)
    if ce < fl:
        ce = fl
    ttr = forecast_ttr(conn)
    remaining = ttr - max(0.0, float(elapsed_outage_s))
    if remaining <= fl:
        return fl   # near/past expected recovery → probe at floor cadence
    return max(fl, min(ce, remaining * 0.5))


# ---------------------------------------------------------------------------
# TCP-style AIMD slow-start controller
# ---------------------------------------------------------------------------


class AIMDController:
    """Additive-Increase / Multiplicative-Decrease concurrency governor.

    Slow-start: the limit BEGINS at ``floor`` (1 — a single probe agent). Each
    success additively increases it (+``ai_step``); each failure multiplicatively
    decreases it (``// md_factor``). Bounded ``[floor, max_limit]`` where
    ``max_limit`` defaults to the swarm's own concurrency ceiling. This is how a
    recovering DW server is ramped onto instead of slammed by a thundering herd."""

    def __init__(
        self,
        *,
        max_limit: Optional[int] = None,
        floor: int = 1,
        ai_step: int = 1,
        md_factor: int = 2,
    ) -> None:
        if max_limit is None:
            try:
                from backend.core.ouroboros.governance.chunk_swarm import swarm_concurrency
                max_limit = swarm_concurrency()
            except Exception:  # noqa: BLE001
                max_limit = 4
        self._floor = max(1, int(floor))
        self._max = max(self._floor, int(max_limit or self._floor))
        self._ai = max(1, int(ai_step))
        self._md = max(2, int(md_factor))
        self._limit = self._floor   # slow-start: begin at the floor (1)

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def max_limit(self) -> int:
        return self._max

    def on_success(self) -> int:
        """Additive increase — one more slot, capped at the ceiling."""
        self._limit = min(self._max, self._limit + self._ai)
        return self._limit

    def on_failure(self) -> int:
        """Multiplicative decrease — halve, never below the floor."""
        self._limit = max(self._floor, self._limit // self._md)
        return self._limit

    def on_transient_fault(self) -> int:
        """Yield-on-Fault: a ``[TransientAbsorb]`` DW degradation DURING scale-up
        multiplicatively downscales concurrency (same as ``on_failure``) so the
        recovering server is backed off — WITHOUT failing the global op. The
        absorbed round retries at the lower limit; new sub-agent dispatch gates
        on ``self.limit``, so in-flight agents drain and the herd shrinks."""
        return self.on_failure()

    def throttle_to_floor(self) -> int:
        """Hard yield — collapse straight to the floor (1) on a SEVERE
        degradation (repeated absorbs / a rupture storm), the fastest safe
        retreat before a full re-ramp."""
        self._limit = self._floor
        return self._limit

    def reset(self) -> int:
        self._limit = self._floor
        return self._limit


# ---------------------------------------------------------------------------
# The predictive watch loop
# ---------------------------------------------------------------------------


@dataclass
class RecoveryOutcome:
    recovered: bool
    ttr_s: Optional[float]
    probes: int
    aimd: AIMDController


ProbeFn = Callable[[], Awaitable[bool]]     # a lightweight DW health ping → bool
NowFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


async def watch_for_recovery(
    conn: Optional[sqlite3.Connection],
    probe_fn: ProbeFn,
    *,
    now_fn: Optional[NowFn] = None,
    sleep_fn: Optional[SleepFn] = None,
    max_probes: int = 0,
    surface: str = "direct_streaming",
) -> RecoveryOutcome:
    """Predictively wait out a DW outage, then confirm recovery with a single
    slow-start probe. Records the outage lifecycle to SQLite (feeding future
    forecasts). Returns a :class:`RecoveryOutcome` carrying the ramped AIMD
    controller the relaunch should seed its ``max_concurrency`` from.

    ``probe_fn`` is injected (production: the DWHeartbeat deep-probe; tests: a
    mock) so this loop never itself hammers the endpoint — it probes exactly
    once per predicted interval. Never raises."""
    import asyncio as _asyncio

    now = now_fn or time.monotonic
    sleep = sleep_fn or _asyncio.sleep
    aimd = AIMDController()

    start_ts = now()
    record_outage_start(conn, start_ts, surface=surface)
    probes = 0

    while True:
        elapsed = now() - start_ts
        interval = predict_sleep_s(conn, elapsed)
        await sleep(interval)
        probes += 1
        try:
            healthy = bool(await probe_fn())
        except Exception:  # noqa: BLE001 — a failed probe is just "still down"
            healthy = False

        if healthy:
            # TCP slow-start: a SINGLE probe agent confirmed the surface. The
            # AIMD limit is already 1; the relaunch ramps it up per round.
            aimd.on_success()   # 1 -> 2 available for the first real round
            ttr = record_recovery(conn, start_ts, now(), surface=surface)
            logger.info(
                "[DWForecaster] DW RECOVERED after %.0fs (%d probes) — slow-start "
                "at limit=%d/%d", (ttr or elapsed), probes, aimd.limit, aimd.max_limit,
            )
            return RecoveryOutcome(recovered=True, ttr_s=ttr, probes=probes, aimd=aimd)

        aimd.on_failure()   # still down → keep the ramp conservative
        logger.debug(
            "[DWForecaster] DW still down (probe %d, elapsed %.0fs, next in %.0fs)",
            probes, elapsed, interval,
        )
        if max_probes and probes >= max_probes:
            return RecoveryOutcome(recovered=False, ttr_s=None, probes=probes, aimd=aimd)


__all__ = [
    "AIMDController",
    "RecoveryOutcome",
    "default_forecast_db_path",
    "forecast_ttr",
    "open_forecast_db",
    "predict_sleep_s",
    "record_outage_start",
    "record_recovery",
    "watch_for_recovery",
]
