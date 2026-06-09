"""Slice 187 — precision TTFT telemetry (don't measure speed with a broken stopwatch).

The routing decisions (latency governor, Slice 185) rely on DW's measured RT TTFT. But the raw
measurement conflates TWO things: (1) TRUE network TTFT — HTTP request-sent → first-byte-back —
and (2) LOCAL ASYNC LAG — how long the starved event loop took to even *process* that byte. A
loop stuttering at 1190ms (the ControlPlaneStarvation) inflates apparent vendor latency and
corrupts the empirical data.

This module draws the mathematical separation:
  * ``network_ttft_ms`` — the pure vendor latency via ``time.perf_counter()``.
  * ``measure_loop_lag_ms`` — the event-loop scheduling lag during the window.
  * ``ttft_sample_is_clean`` — a sample is recorded ONLY if the loop wasn't starved; a
    contaminated reading is EXCLUDED so the routing math never trains on a broken stopwatch.
"""
from __future__ import annotations

import asyncio
import os
import time


def now_perf() -> float:
    """Monotonic high-resolution timestamp for pure-latency math (NOT wall clock). NEVER raises."""
    return time.perf_counter()


def network_ttft_ms(request_sent_perf: float, first_byte_perf: float) -> float:
    """PURE network TTFT in ms: HTTP request-sent → first-byte-returned, via perf_counter — the
    vendor's latency, independent of any FSM/processing time BEFORE the request was sent. Clamped
    to >= 0 (perf_counter is monotonic, so out-of-order only on caller error). NEVER raises."""
    try:
        return max(0.0, (float(first_byte_perf) - float(request_sent_perf)) * 1000.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        v = float(raw) if raw else default
        return v if v > 0 else default
    except Exception:  # noqa: BLE001
        return default


def ttft_sample_is_clean(loop_lag_ms: float, *, max_loop_lag_ms: float | None = None) -> bool:
    """A TTFT sample is CLEAN only when the event loop was NOT starved during measurement — a
    stuttering loop inflates apparent TTFT. Contaminated samples (lag above the threshold) must
    be EXCLUDED from the latency tracker so routing never trains on corrupted vendor data.
    NEVER raises."""
    try:
        thr = max_loop_lag_ms if max_loop_lag_ms is not None else _env_float(
            "JARVIS_DW_TTFT_MAX_LOOP_LAG_MS", 200.0,
        )
        return float(loop_lag_ms) <= thr
    except Exception:  # noqa: BLE001
        return True  # fail-open: don't discard data on a measurement-of-the-measurement error


async def measure_loop_lag_ms() -> float:
    """Measure the event loop's current scheduling lag: schedule a zero-delay yield and measure
    the overshoot. A frictionless loop returns ~0; a starved one returns the stall in ms. This is
    the 'is my stopwatch broken right now?' probe. NEVER raises (returns 0.0 on error)."""
    try:
        t0 = time.perf_counter()
        await asyncio.sleep(0)
        return max(0.0, (time.perf_counter() - t0) * 1000.0)
    except Exception:  # noqa: BLE001
        return 0.0
