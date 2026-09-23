"""Growth-triggered allocation attribution -- free until memory actually grows.

Why
---

bt-2026-09-22-201845's daemon grew 2.2 GB -> 3.9 GB in 3.7 hours, near
linearly, 99% anonymous heap: Python objects accumulating somewhere. The
ProcessMemoryWatchdog saw every byte of it and could say nothing about WHERE,
because the only instrument that can -- ``tracemalloc`` -- costs memory and
time on every allocation, so nothing ran it. A leak you cannot attribute gets
guessed at, and a guessed fix is not a fix.

How
---

The watchdog already ticks. Each tick this records the daemon's OWN resident
size (``worker_lifeline._probe_self_memory_mb`` -- the tree the watchdog
guards includes pytest children and pool workers, whose comings and goings
are noise here, and tracemalloc can only see this process anyway) into a
rolling window and fits a line. Nothing else happens -- no tracemalloc --
until the window shows growth that is all three of:

* **sustained** -- the window spans ``JARVIS_MEMTRACE_WINDOW_S`` and the
  process has been up at least that long (warm-up caches are not a leak);
* **linear** -- r^2 >= ``JARVIS_MEMTRACE_MIN_R2``;
* **significant** -- growth across the window exceeds
  ``JARVIS_MEMTRACE_SIGNIFICANCE`` times the fit's own residual noise AND
  ``JARVIS_MEMTRACE_MIN_GROWTH_FRACTION`` of resident size.

The threshold is therefore the process's own noise, not a number: a quiet
daemon trips on a small steady leak, a noisy one needs a bigger one.

Then tracemalloc starts (frames bounded by ``JARVIS_MEMTRACE_FRAMES``) and,
every ``JARVIS_MEMTRACE_CAPTURE_S`` (default: a third of the window),
successive snapshots are diffed by ``file:line``. The top growing sites go to
the log at WARNING and to ``summary.json`` (``memory_growth``).

Containment (the diagnostic must not become the incident)
---------------------------------------------------------

* Snapshots are taken and diffed on the thread pool (``offload``), never on
  the event loop; one capture at a time (a tick during a capture skips).
* Before every snapshot tracemalloc's OWN memory is checked against
  ``JARVIS_MEMTRACE_OVERHEAD_FRACTION`` of resident size; over it, tracing
  stops and says so. It will not start, or continue, when resident size is
  within that same margin of the watchdog's warn line.
* At most one previous snapshot is held; ``JARVIS_MEMTRACE_REPORTS`` reports
  and it stops, releasing everything (``tracemalloc.stop``).
* Its own frames and tracemalloc's are filtered from what it reports.
* If tracemalloc was already running (``PYTHONTRACEMALLOC``), it is used but
  never stopped: it is not ours.
* NEVER raises into the watchdog.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
import tracemalloc
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.MemoryGrowth")

_MB = 1024.0 * 1024.0


def _env_float(name: str, default: float) -> float:
    try:
        raw = (os.environ.get(name, "") or "").strip()
        value = float(raw) if raw else default
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return (
        os.environ.get("JARVIS_MEMTRACE_ENABLED", "true").strip().lower()
        not in ("0", "false", "no", "off")
    )


def window_s() -> float:
    return _env_float("JARVIS_MEMTRACE_WINDOW_S", 1800.0)


def capture_s() -> float:
    return _env_float("JARVIS_MEMTRACE_CAPTURE_S", window_s() / 3.0)


def min_r2() -> float:
    return min(1.0, _env_float("JARVIS_MEMTRACE_MIN_R2", 0.8))


def significance() -> float:
    return _env_float("JARVIS_MEMTRACE_SIGNIFICANCE", 3.0)


def min_growth_fraction() -> float:
    return _env_float("JARVIS_MEMTRACE_MIN_GROWTH_FRACTION", 0.05)


def frames() -> int:
    # Bounded both ways: 1 frame is file:line; more costs memory per trace.
    return max(1, min(25, int(_env_float("JARVIS_MEMTRACE_FRAMES", 1.0))))


def top_n() -> int:
    return max(1, int(_env_float("JARVIS_MEMTRACE_TOP_N", 10.0)))


def max_reports() -> int:
    return max(1, int(_env_float("JARVIS_MEMTRACE_REPORTS", 3.0)))


def overhead_fraction() -> float:
    return min(0.5, _env_float("JARVIS_MEMTRACE_OVERHEAD_FRACTION", 0.05))


# ---------------------------------------------------------------------------
# The trigger: pure, so it is testable without a leak
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GrowthFit:
    slope_mb_per_h: float
    r2: float
    growth_mb: float
    noise_mb: float
    span_s: float
    samples: int
    start_mb: float
    end_mb: float


def fit_growth(samples: List[Tuple[float, float]]) -> Optional[GrowthFit]:
    """Least-squares line through ``(t, mb)``. ``None`` when underdetermined."""
    n = len(samples)
    if n < 3:
        return None
    ts = [t for t, _ in samples]
    ys = [y for _, y in samples]
    t_mean = sum(ts) / n
    y_mean = sum(ys) / n
    sxx = sum((t - t_mean) ** 2 for t in ts)
    if sxx <= 0:
        return None
    sxy = sum((t - t_mean) * (y - y_mean) for t, y in samples)
    slope = sxy / sxx
    intercept = y_mean - slope * t_mean
    resid = [y - (intercept + slope * t) for t, y in samples]
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((y - y_mean) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    span = ts[-1] - ts[0]
    return GrowthFit(
        slope_mb_per_h=slope * 3600.0,
        r2=r2,
        growth_mb=slope * span,
        noise_mb=math.sqrt(ss_res / max(1, n - 2)),
        span_s=span,
        samples=n,
        start_mb=intercept + slope * ts[0],
        end_mb=intercept + slope * ts[-1],
    )


def growth_verdict(fit: Optional[GrowthFit], *, window: float) -> Tuple[bool, str]:
    """Whether *fit* is a sustained, linear, significant leak -- and why not."""
    if fit is None:
        return False, "not enough samples"
    if fit.span_s < window:
        return False, f"window {fit.span_s:.0f}s < {window:.0f}s"
    if fit.growth_mb <= 0:
        return False, "not growing"
    if fit.r2 < min_r2():
        return False, f"r2 {fit.r2:.2f} < {min_r2():.2f} (noisy, not a trend)"
    if fit.growth_mb < significance() * fit.noise_mb:
        return False, (
            f"growth {fit.growth_mb:.0f}MB < {significance():.1f} x noise "
            f"{fit.noise_mb:.0f}MB"
        )
    floor = min_growth_fraction() * max(fit.start_mb, 1.0)
    if fit.growth_mb < floor:
        return False, f"growth {fit.growth_mb:.0f}MB < {floor:.0f}MB floor"
    return True, "sustained linear growth"


# ---------------------------------------------------------------------------
# The tracer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GrowthSite:
    site: str
    size_diff_kb: float
    count_diff: int
    size_kb: float


class MemoryGrowthTracer:
    """Watches own RSS; attributes growth with tracemalloc only once it is real."""

    def __init__(self, *, clock=time.monotonic, started_at: Optional[float] = None) -> None:
        self._clock = clock
        self._started = started_at if started_at is not None else clock()
        self._samples: Deque[Tuple[float, float]] = deque()
        self._lock = threading.Lock()
        self._capturing = False
        self._tracing = False
        self._owns_tracemalloc = False
        self._prev: Any = None
        self._last_capture = 0.0
        self._trigger: Optional[GrowthFit] = None
        self._reports: List[Dict[str, Any]] = []
        self._stopped = ""
        self._module_file = os.path.abspath(__file__)

    # -- observation ---------------------------------------------------------

    def observe(self, rss_mb: float, now: Optional[float] = None) -> Optional[GrowthFit]:
        """Record one sample; return the fit when it just crossed the trigger."""
        now = self._clock() if now is None else now
        win = window_s()
        with self._lock:
            self._samples.append((now, float(rss_mb)))
            while self._samples and now - self._samples[0][0] > win:
                self._samples.popleft()
            if self._tracing or self._trigger is not None:
                return None
            if now - self._started < win:
                return None  # warm-up caches are growth, not a leak
            fit = fit_growth(list(self._samples))
        # Coverage: the window must actually be full, not just old enough.
        ok, _why = growth_verdict(fit, window=win * _coverage())
        return fit if ok else None

    async def tick(self, *, warn_mb: Optional[float] = None) -> None:
        """One watchdog tick. NEVER raises."""
        if not enabled():
            return
        try:
            from backend.core.ouroboros.governance.cooperative_fs_io import (  # noqa: PLC0415
                is_offload_error, offload,
            )
            from backend.core.ouroboros.governance.worker_lifeline import (  # noqa: PLC0415
                _probe_self_memory_mb,
            )
            rss = await offload(_probe_self_memory_mb, cpu_bound=False)
            if rss is None or is_offload_error(rss):
                return
            fit = self.observe(rss)
            if fit is not None:
                self._start(fit, rss, warn_mb)
            if self._tracing and not self._capturing:
                if self._clock() - self._last_capture >= capture_s():
                    self._capturing = True
                    try:
                        await offload(self._capture, rss, warn_mb, cpu_bound=False)
                    finally:
                        self._capturing = False
        except Exception:  # noqa: BLE001 — a diagnostic never breaks the watchdog
            logger.debug("[MemoryGrowth] tick degraded", exc_info=True)

    # -- tracing ---------------------------------------------------------------

    def _headroom_ok(self, rss_mb: float, warn_mb: Optional[float]) -> bool:
        if not warn_mb:
            return True
        return rss_mb < warn_mb * (1.0 - overhead_fraction())

    def _start(self, fit: GrowthFit, rss_mb: float, warn_mb: Optional[float]) -> None:
        with self._lock:
            self._trigger = fit
        if not self._headroom_ok(rss_mb, warn_mb):
            self._stop(f"not started: {rss_mb:.0f}MB is within the tracing margin of warn {warn_mb:.0f}MB")
            return
        self._owns_tracemalloc = not tracemalloc.is_tracing()
        if self._owns_tracemalloc:
            tracemalloc.start(frames())
        self._tracing = True
        self._last_capture = self._clock()  # first snapshot one capture period in
        logger.warning(
            "[MemoryGrowth] sustained growth %.0f MB/h (r2=%.2f, +%.0fMB over %.0fs, "
            "noise %.0fMB) — tracing allocations (%d frame(s)) to attribute it",
            fit.slope_mb_per_h, fit.r2, fit.growth_mb, fit.span_s, fit.noise_mb, frames(),
        )

    def _stop(self, reason: str) -> None:
        self._tracing = False
        self._prev = None
        if self._owns_tracemalloc and tracemalloc.is_tracing():
            tracemalloc.stop()
        self._owns_tracemalloc = False
        with self._lock:
            self._stopped = reason
        logger.warning("[MemoryGrowth] tracing stopped — %s", reason)

    def _capture(self, rss_mb: float, warn_mb: Optional[float]) -> None:
        """On a worker thread. Snapshot, diff against the previous, report."""
        self._last_capture = self._clock()
        if not tracemalloc.is_tracing():
            self._stop("tracemalloc was stopped externally")
            return
        overhead_mb = tracemalloc.get_tracemalloc_memory() / _MB
        budget_mb = overhead_fraction() * rss_mb
        if overhead_mb > budget_mb:
            self._stop(f"tracing overhead {overhead_mb:.0f}MB exceeds budget {budget_mb:.0f}MB")
            return
        if not self._headroom_ok(rss_mb, warn_mb):
            self._stop(f"{rss_mb:.0f}MB reached the tracing margin of warn {warn_mb:.0f}MB")
            return
        snap = tracemalloc.take_snapshot().filter_traces((
            tracemalloc.Filter(False, tracemalloc.__file__),
            tracemalloc.Filter(False, self._module_file),
            tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
            tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
        ))
        prev, self._prev = self._prev, snap
        if prev is None:
            return  # baseline taken; growth is measured from here
        sites = [
            GrowthSite(
                site=f"{s.traceback[0].filename}:{s.traceback[0].lineno}",
                size_diff_kb=round(s.size_diff / 1024.0, 1),
                count_diff=s.count_diff,
                size_kb=round(s.size / 1024.0, 1),
            )
            for s in snap.compare_to(prev, "lineno")
            if s.size_diff > 0
        ][: top_n()]
        del prev
        report = {
            "at_rss_mb": round(rss_mb, 1),
            "tracing_overhead_mb": round(overhead_mb, 1),
            "interval_s": round(capture_s(), 1),
            "top_growth": [asdict(s) for s in sites],
        }
        with self._lock:
            self._reports.append(report)
            done = len(self._reports) >= max_reports()
        logger.warning(
            "[MemoryGrowth] top growing allocation sites over the last %.0fs "
            "(rss %.0fMB, tracing overhead %.0fMB):\n%s",
            capture_s(), rss_mb, overhead_mb,
            "\n".join(
                f"  +{s.size_diff_kb:>10.1f} KB  {s.count_diff:>+8d} objs  {s.site}"
                for s in sites
            ) or "  (nothing grew)",
        )
        if done:
            self._stop(f"attribution complete after {max_reports()} report(s)")

    # -- reporting -------------------------------------------------------------

    def report(self) -> Dict[str, Any]:
        """For ``summary.json``: empty when growth never triggered."""
        with self._lock:
            if self._trigger is None:
                return {}
            return {
                "trigger": asdict(self._trigger),
                "tracing": self._tracing,
                "stopped": self._stopped,
                "reports": list(self._reports),
            }


def _coverage() -> float:
    """Fraction of the window the samples must actually span (sparse ticks
    after a stall must not fit a 'trend' through two points)."""
    return min(1.0, _env_float("JARVIS_MEMTRACE_WINDOW_COVERAGE", 0.9))


_default: Optional[MemoryGrowthTracer] = None


def default_tracer() -> MemoryGrowthTracer:
    global _default  # noqa: PLW0603
    if _default is None:
        _default = MemoryGrowthTracer()
    return _default


__all__ = [
    "GrowthFit",
    "MemoryGrowthTracer",
    "default_tracer",
    "fit_growth",
    "growth_verdict",
]
