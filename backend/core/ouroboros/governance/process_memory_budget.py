"""Process-tree memory budget -- the ONE formula for a soak's memory cap.

Extracted verbatim from ``BattleTestHarness._resolve_process_memory_thresholds``
so two consumers share a single definition:

  * the harness's ``ProcessMemoryWatchdog`` -- which ENFORCES the cap on the
    soak's own process tree (it delegates here, behaviour unchanged);
  * ``test_execution_lock`` -- which must RESERVE that same cap for a live
    soak before granting memory to a concurrent test run. It evaluates the
    formula against the SOAK's environment (read from ``/proc/<pid>/environ``)
    rather than its own, so an operator override on the soak is honoured.

A deliberate leaf: stdlib only at import time, ``psutil`` lazily.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional, Tuple

__all__ = ["resolve_process_memory_thresholds"]


def _env_positive_float(environ: Mapping[str, str], name: str) -> Optional[float]:
    raw = (environ.get(name, "") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_process_memory_thresholds(
    environ: Optional[Mapping[str, str]] = None,
    *,
    total_mb: Optional[float] = None,
) -> Tuple[float, Optional[float], float]:
    """Resolve ``(warn_mb, cap_mb, interval_s)`` from *environ*, adaptively.

    Never hardcodes a byte count. ``JARVIS_PROCESS_MEMORY_CAP_MB`` is an
    absolute override; absent it the cap is a fraction of total system RAM
    (``JARVIS_PROCESS_MEMORY_CAP_FRACTION``, default 0.75) so the same code
    protects a 16GB laptop and a 256GB box without edits. ``cap_mb=None``
    means DISABLED (master switch off, or total RAM unknowable with no
    explicit cap).

    *environ* defaults to this process's environment; *total_mb* defaults to
    ``psutil``'s total RAM. NEVER raises.
    """
    env = os.environ if environ is None else environ
    if (env.get("JARVIS_PROCESS_MEMORY_WATCHDOG_ENABLED", "true") or "").strip().lower() == "false":
        return (0.0, None, 0.0)

    # Interval -- floor 2s (no busy-probe), ceiling 120s (bound the
    # detection lag on a fast leak).
    interval_s = _env_positive_float(env, "JARVIS_PROCESS_MEMORY_WATCHDOG_INTERVAL_S") or 15.0
    interval_s = max(2.0, min(120.0, interval_s))

    cap_mb = _env_positive_float(env, "JARVIS_PROCESS_MEMORY_CAP_MB")
    if cap_mb is None:
        try:
            frac_raw = _env_positive_float(env, "JARVIS_PROCESS_MEMORY_CAP_FRACTION")
            frac = frac_raw if frac_raw is not None else 0.75
            frac = max(0.10, min(0.95, frac))
            if total_mb is None:
                import psutil  # lazy -- already a project dependency
                total_mb = psutil.virtual_memory().total / (1024.0 * 1024.0)
            cap_mb = total_mb * frac
        except Exception:  # noqa: BLE001 -- psutil missing / probe failed
            # No host-relative cap derivable and no override -> stay
            # DISABLED rather than invent a number (no hardcoding).
            return (0.0, None, interval_s)

    warn_mb = _env_positive_float(env, "JARVIS_PROCESS_MEMORY_WARN_MB")
    if warn_mb is None:
        warn_mb = cap_mb * 0.85
    # Keep warn strictly below cap so the WARN checkpoint always precedes
    # the CAP stop.
    warn_mb = min(warn_mb, cap_mb * 0.98)
    return (warn_mb, cap_mb, interval_s)
