"""Ask the audio hardware whether it is already suffering.

WHY THIS EXISTS
---------------
``local_model_admission``'s docstring names the hazard exactly:

    the audio tap runs on a real-time thread, `HALC_ProxyIOContext ::
    skipping cycle due to overload` is already in the boot log at idle, and
    a swap storm during model load is exactly the condition that turns a
    dropped buffer into a severed sentence.

That sentence was PROSE. Nothing read it. The guard inferred audio risk
from a memory number, which is a proxy for the thing we actually care
about — and a proxy that says "22.8% free" while `coreaudiod` is, at that
same moment, logging a real overload with a safety violation.

macOS publishes the ground truth. `coreaudiod` emits an audioanalytics IO
error per overload, and it carries the causal chain in one record:

    overload_type: Overload          safety_violation: 1
    multi_cycle_io_page_faults_duration: 3141689      <- PAGING, mid-cycle
    lateness: 271   deadline: 2151   scheduler_latency: 31083

`multi_cycle_io_page_faults_duration` is the mechanism the admission
docstring describes, measured by the OS rather than guessed by us: the
real-time thread took page faults inside its IO deadline. That is a swap
storm severing a sentence, in one field.

COST, MEASURED
--------------
`/usr/bin/log show` costs ~1.4-2.7s and the cost is dominated by process
startup, NOT by window length (30s window: 2.70s; 120s: 1.46s). Two
consequences, both structural:

  * it can never run on the event loop, and never per token — it is
    TTL-cached and single-flight;
  * the window may be generous for free, so it is, which makes the signal
    less spiky than a tight window would.

UNKNOWN IS NOT CONTENTION
-------------------------
When the probe cannot answer — binary missing, timeout, breaker open, a
future macOS that renames the subsystem — it returns ``ok=False`` and
callers fall back to the memory-only decision. Reading "could not measure"
as "audio is suffering" would refuse local inference forever the first
time Apple changes a log predicate, which is how a safety guard becomes
the reason a tier is permanently dark. The memory gate still guards; this
sharpens it when it can.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("Jarvis.AudioContention")

AUDIO_CONTENTION_SCHEMA_VERSION: str = "audio_contention.1"

#: The subsystem coreaudiod reports IO errors through.
_PREDICATE = (
    'subsystem == "com.apple.audioanalytics" '
    'AND eventMessage CONTAINS "overload_type"'
)

_INT_FIELD = r'"{field}":\s*Optional\((-?\d+)\)'

__all__ = [
    "AUDIO_CONTENTION_SCHEMA_VERSION",
    "AudioContention",
    "probe",
    "probe_async",
    "reset_for_tests",
    "window_s",
]


def enabled() -> bool:
    """``JARVIS_AUDIO_CONTENTION_PROBE_ENABLED`` (default true)."""
    raw = (os.environ.get("JARVIS_AUDIO_CONTENTION_PROBE_ENABLED", "1") or "").strip()
    return raw.lower() not in ("0", "false", "no", "off")


def _f(name: str, default: float, lo: float, hi: float) -> float:
    """Env float, clamped. NEVER raises — a typo in a guard's timing must
    not be able to turn the guard into the outage."""
    try:
        raw = (os.environ.get(name) or "").strip()
        return max(lo, min(hi, float(raw))) if raw else default
    except Exception:  # noqa: BLE001
        return default


def window_s() -> float:
    """How far back to look. Generous because it is nearly free (see COST)."""
    return _f("JARVIS_AUDIO_CONTENTION_WINDOW_S", 120.0, 10.0, 3600.0)


def ttl_s() -> float:
    """Cache lifetime. Must exceed the probe's own ~1.4-2.7s cost by a wide
    margin or the probe becomes the contention it measures."""
    return _f("JARVIS_AUDIO_CONTENTION_TTL_S", 45.0, 5.0, 900.0)


def timeout_s() -> float:
    return _f("JARVIS_AUDIO_CONTENTION_TIMEOUT_S", 8.0, 1.0, 60.0)


@dataclass(frozen=True)
class AudioContention:
    """One reading. ``ok=False`` means UNMEASURED, never "healthy"."""

    ok: bool
    overloads: int = 0
    safety_violations: int = 0
    worst_lateness: int = 0
    page_fault_ns: int = 0
    window_s: float = 0.0
    age_s: float = 0.0
    error: Optional[str] = None

    @property
    def contended(self) -> bool:
        """Did the audio graph actually miss its deadline in the window?

        A single overload counts. There is no "acceptable rate" of severed
        audio to tune here — the caller decides what to DO about it, and
        the caller has cheaper options than refusing (slow down first).
        """
        return self.ok and self.overloads > 0

    @property
    def paging_implicated(self) -> bool:
        """True when the overloads took page faults inside the IO cycle.

        This is the distinction that makes the signal actionable for a
        MEMORY guard: audio can overload for reasons a local model cannot
        cause (a USB interface, a hostile plugin). Page faults mid-cycle
        are memory pressure, which is exactly what loading weights makes
        worse — so this is the field that should move an ADMISSION
        decision, while bare `contended` should only move a SPEED one.
        """
        return self.contended and self.page_fault_ns > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": AUDIO_CONTENTION_SCHEMA_VERSION,
            "ok": self.ok, "overloads": self.overloads,
            "safety_violations": self.safety_violations,
            "worst_lateness": self.worst_lateness,
            "page_fault_ns": self.page_fault_ns,
            "contended": self.contended,
            "paging_implicated": self.paging_implicated,
            "window_s": self.window_s, "age_s": round(self.age_s, 1),
            "error": self.error,
        }


_lock = threading.Lock()
_cached: Optional[AudioContention] = None
_cached_at: float = 0.0
_inflight = threading.Lock()


def _parse(out: str, window: float) -> AudioContention:
    """Count overloads and extract the worst numbers. NEVER raises."""
    try:
        lines = [ln for ln in out.splitlines() if "overload_type" in ln]
        if not lines:
            return AudioContention(ok=True, overloads=0, window_s=window)

        def _max_field(field: str) -> int:
            best = 0
            for m in re.finditer(_INT_FIELD.format(field=field), out):
                try:
                    best = max(best, int(m.group(1)))
                except ValueError:
                    continue
            return best

        # safety_violation is 0/1 per record — count records, not the max.
        violations = sum(
            1 for m in re.finditer(_INT_FIELD.format(field="safety_violation"), out)
            if m.group(1) not in ("0",)
        )
        return AudioContention(
            ok=True,
            overloads=len(lines),
            safety_violations=violations,
            worst_lateness=_max_field("lateness"),
            page_fault_ns=_max_field("multi_cycle_io_page_faults_duration"),
            window_s=window,
        )
    except Exception as exc:  # noqa: BLE001
        return AudioContention(ok=False, window_s=window,
                               error=f"parse:{type(exc).__name__}")


def probe(force: bool = False) -> AudioContention:
    """A cached reading. SYNCHRONOUS and ~1.4-2.7s on a miss — callers on
    the event loop MUST use :func:`probe_async`. NEVER raises.
    """
    global _cached, _cached_at  # noqa: PLW0603

    if not enabled():
        return AudioContention(ok=False, error="disabled")

    now = time.monotonic()
    with _lock:
        if _cached is not None and not force and (now - _cached_at) < ttl_s():
            age = now - _cached_at
            return AudioContention(**{**_cached.__dict__, "age_s": age})

    # Single-flight: a burst of callers must not each spawn `log show`.
    # Non-blocking acquire — a caller that loses the race takes the stale
    # reading rather than queueing behind a 2s subprocess.
    if not _inflight.acquire(blocking=False):
        with _lock:
            if _cached is not None:
                return AudioContention(
                    **{**_cached.__dict__, "age_s": now - _cached_at})
        return AudioContention(ok=False, error="probe_in_flight")

    try:
        window = window_s()
        try:
            from backend.core.bounded_subprocess import run_bounded
        except Exception:  # noqa: BLE001
            return AudioContention(ok=False, window_s=window,
                                   error="bounded_subprocess_unavailable")

        completed = run_bounded(
            ["/usr/bin/log", "show", "--last", f"{int(window)}s",
             "--predicate", _PREDICATE, "--style", "compact"],
            timeout=timeout_s(), text=True,
        )
        if completed is None:
            reading = AudioContention(ok=False, window_s=window,
                                      error="unanswered")
        elif completed.returncode != 0:
            reading = AudioContention(ok=False, window_s=window,
                                      error=f"rc={completed.returncode}")
        else:
            reading = _parse(completed.stdout or "", window)

        with _lock:
            _cached = reading
            _cached_at = time.monotonic()
        return reading
    finally:
        _inflight.release()


async def probe_async(force: bool = False) -> AudioContention:
    """:func:`probe` off the event loop. NEVER raises.

    Uses the house offload primitive, whose pool is separate from the
    default executor precisely so a 2s subprocess cannot contend with the
    200+ ``to_thread`` sites in this process.
    """
    try:
        from backend.core.async_offload import call_off_loop
        result = await call_off_loop(probe, force)
        if result is None:                      # offload failed, fail-open
            return AudioContention(ok=False, error="offload_failed")
        return result
    except Exception:  # noqa: BLE001
        return AudioContention(ok=False, error="offload_error")


def reset_for_tests() -> None:
    """Test-only."""
    global _cached, _cached_at  # noqa: PLW0603
    with _lock:
        _cached = None
        _cached_at = 0.0
