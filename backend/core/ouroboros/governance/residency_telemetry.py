"""Residency Telemetry — the organism instruments its OWN loop.

Pre-soak hardening (operator authorization 2026-07-19) for the 24/7
Continuous Residency Validation. Mandate 1: no bash, no cron, no
psutil scraping — the async loop measures ITSELF.

Every ``JARVIS_RESIDENCY_SNAPSHOT_S`` (300s) a non-blocking task
appends one JSON line:

  * ``rss_mb``       — stdlib ``resource.getrusage`` (native, no
    psutil; macOS bytes / Linux KiB normalized).
  * ``uds_conns``    — live attach/audio subscriber count (pulled from
    the injected bridge — the organism's own connection ledgers).
  * ``loop_lag_ms``  — event-loop scheduling latency: the round-trip
    of a ``call_soon`` future (the honest "is the loop starving?"
    probe — a zero-work callback that should return in microseconds;
    a fat number means the loop is congested).
  * deltas vs the first snapshot → a leak shows as monotone rss growth.

DRY (mandate 3): the bounded ``.jsonl`` rides the EXISTING
``headless_telemetry`` RotatingFileHandler (same
``JARVIS_TELEMETRY_LOG_MAX_BYTES`` rotation the FSM log janitor uses).
The task registers on the Orchestrator lifecycle (GovernedLoop) — one
coroutine, cancelled cleanly at teardown.

NEVER raises on the sampling path — a telemetry fault must never
perturb the system it measures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.ResidencyTelemetry")


def _snapshot_interval_s() -> float:
    try:
        return max(5.0, min(3600.0, float(os.environ.get(
            "JARVIS_RESIDENCY_SNAPSHOT_S", "300",
        ))))
    except (TypeError, ValueError):
        return 300.0


def _log_path() -> Path:
    return Path(os.environ.get(
        "JARVIS_RESIDENCY_LOG",
        ".jarvis/logs/residency_telemetry.jsonl",
    ))


def rss_mb() -> float:
    """Resident set size in MiB via stdlib ``resource`` — NO psutil.
    ru_maxrss is bytes on macOS, KiB on Linux. NEVER raises."""
    try:
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Normalize: Darwin reports bytes, Linux KiB.
        if sys.platform == "darwin":
            return round(raw / (1024.0 * 1024.0), 2)
        return round(raw / 1024.0, 2)
    except Exception:  # noqa: BLE001
        return 0.0


async def loop_lag_ms(loop: Optional[asyncio.AbstractEventLoop] = None) -> float:
    """Event-loop scheduling latency: the round-trip of a zero-work
    ``call_soon`` callback. A healthy loop returns in µs; congestion
    (a blocking call hogging the loop) inflates it — the honest
    starvation probe. NEVER raises."""
    try:
        loop = loop or asyncio.get_running_loop()
        fut: "asyncio.Future" = loop.create_future()
        t0 = loop.time()
        loop.call_soon(lambda: fut.done() or fut.set_result(loop.time()))
        t1 = await fut
        return round((t1 - t0) * 1000.0, 3)
    except Exception:  # noqa: BLE001
        return -1.0


class ResidencyTelemetry:
    """Owns the bounded JSONL sink + the snapshot loop. ``conn_source``
    returns the live UDS subscriber count (injected — the bridges'
    own client ledgers); ``clock`` injected for the fast-forward
    test."""

    def __init__(
        self,
        *,
        conn_source: Optional[Callable[[], int]] = None,
        log_path: Optional[Path] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._conns = conn_source or (lambda: 0)
        self._path = log_path or _log_path()
        self._clock = clock
        self._handler: Optional[logging.handlers.RotatingFileHandler] = None
        self._task: Optional[asyncio.Task] = None
        self._first_rss: Optional[float] = None
        self._samples = 0
        self.last: Dict[str, Any] = {}

    def _get_handler(self) -> Optional[logging.handlers.RotatingFileHandler]:
        if self._handler is not None:
            return self._handler
        try:
            # DRY: the SAME rotation knobs as the FSM log janitor.
            from backend.core.ouroboros.governance.headless_telemetry import (  # noqa: E501
                _DEFAULT_BACKUPS,
                _DEFAULT_MAX_BYTES,
                _FLAG_BACKUPS,
                _FLAG_MAX_BYTES,
                _env_int,
            )
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handler = logging.handlers.RotatingFileHandler(
                str(self._path),
                maxBytes=_env_int(_FLAG_MAX_BYTES, _DEFAULT_MAX_BYTES),
                backupCount=_env_int(_FLAG_BACKUPS, _DEFAULT_BACKUPS),
                encoding="utf-8",
            )
            self._handler.setFormatter(logging.Formatter("%(message)s"))
            return self._handler
        except Exception:  # noqa: BLE001
            return None

    async def snapshot(self) -> Dict[str, Any]:
        """One measurement → one bounded JSONL line. Returns the row.
        NEVER raises; NEVER blocks the loop."""
        try:
            r = rss_mb()
            if self._first_rss is None:
                self._first_rss = r
            lag = await loop_lag_ms()
            # Loop-Lag Watchdog (DRY: routes through SovereignGovernor's
            # degradation discipline). A spike throttles THIS ring's
            # verbosity to protect terminal interaction.
            try:
                from backend.core.ouroboros.governance.comms.duplex.sovereign_governor import (  # noqa: E501
                    LoopLagWatchdog, loop_lag_degraded,
                )
                if not hasattr(self, "_lag_watchdog"):
                    self._lag_watchdog = LoopLagWatchdog()
                self._lag_watchdog.observe_lag_ms(lag)
                _throttled = loop_lag_degraded()
            except Exception:  # noqa: BLE001
                _throttled = False
            row = {
                "ts": time.time(),
                "rss_mb": r,
                "rss_delta_mb": round(r - (self._first_rss or r), 2),
                "loop_lag_ms": lag,
                "sample": self._samples,
            }
            if not _throttled:
                # Full verbosity when the loop is healthy; throttled
                # rows shed the non-essential fields under congestion.
                row["uds_conns"] = self._safe_conns()
            else:
                row["throttled"] = True
            self._samples += 1
            self.last = row
            h = self._get_handler()
            if h is not None:
                # Emit through the rotating handler (bounded on disk).
                rec = logging.LogRecord(
                    "residency", logging.INFO, __file__, 0,
                    json.dumps(row, separators=(",", ":")), (), None,
                )
                h.emit(rec)
            return row
        except Exception:  # noqa: BLE001
            logger.debug("[Residency] snapshot degraded", exc_info=True)
            return {}

    def _safe_conns(self) -> int:
        try:
            return int(self._conns())
        except Exception:  # noqa: BLE001
            return -1

    def start(self) -> bool:
        """Register the snapshot loop on the running loop. Gated
        ``JARVIS_RESIDENCY_TELEMETRY_ENABLED`` (default ON — cheap and
        the soak needs it). NEVER raises."""
        try:
            if os.environ.get(
                "JARVIS_RESIDENCY_TELEMETRY_ENABLED", "1",
            ).strip().lower() not in ("1", "true", "yes", "on"):
                return False
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._loop())
            logger.info(
                "[Residency] telemetry armed (every %.0fs → %s)",
                _snapshot_interval_s(), self._path,
            )
            return True
        except RuntimeError:
            return False
        except Exception:  # noqa: BLE001
            return False

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_snapshot_interval_s())
                await self.snapshot()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[Residency] loop degraded", exc_info=True)

    async def stop(self) -> None:
        """Cancel the loop + flush the handler. NEVER raises."""
        try:
            task = self._task
            self._task = None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if self._handler is not None:
                try:
                    self._handler.close()
                except Exception:  # noqa: BLE001
                    pass
                self._handler = None
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "ResidencyTelemetry",
    "loop_lag_ms",
    "rss_mb",
]
