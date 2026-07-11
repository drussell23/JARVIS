"""Shared process-tree memory probe (P5 Arc C Slice 5a; footprint-aware 2026-07-11).

Single source of truth for "how much memory is THIS process tree
using". Extracted verbatim from ``BattleTestHarness.
_probe_process_tree_rss_mb`` (Arc B) so two consumers share ONE
implementation with zero duplication:

  * ``ProcessMemoryWatchdog`` (harness) — the hard-stop authority
    (terminate + partial summary). Behavior unchanged: it now calls
    this function instead of its own inlined copy.
  * ``MemoryPressureGate`` (Slice 5b) — the advisory fan-out clamp.
    It self-probes via this function (Amendment A: production
    correctness must NOT depend on the harness pushing RSS).

The scope flip vs. ``memory_pressure_gate``'s system-wide cascade
is the whole point: this is PROCESS-tree-scoped (self + all
descendants), because a leak lives in the tree (forked worktrees /
subprocess test runs), not just self.

Compression-aware metric (2026-07-11 OOM RCA)
---------------------------------------------
psutil ``rss`` counts RESIDENT pages only. On macOS the memory
compressor takes a leaking-but-idle process's cold pages OUT of the
resident set, so rss collapses while the process keeps owning the
memory: an orphaned Oracle spawn worker was caught live at a 33.9 GB
``phys_footprint`` with a 7 MB rss — invisible to this probe, to the
watchdog it feeds, and to ``ps``, while jetsam (and the user-facing
"out of application memory" dialog) charge the full footprint. The
authoritative darwin metric is ``ri_phys_footprint``
(``proc_pid_rusage`` RUSAGE_INFO_V4) — dirty + compressed + IOKit-
mapped, the same number Activity Monitor's "Memory" column and the
jetsam killer use. The probe therefore resolves its metric adaptively
(``JARVIS_MEMORY_PROBE_METRIC`` = ``auto`` | ``footprint`` | ``rss``,
default ``auto`` → footprint on darwin, rss elsewhere) and degrades
per-pid to rss whenever the footprint primitive is unavailable —
never returning less signal than the pre-upgrade probe.
"""
from __future__ import annotations

import ctypes # for darwin proc_pid_rusage 
import os
import sys
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Darwin phys_footprint primitive (proc_pid_rusage, RUSAGE_INFO_V4)
# ---------------------------------------------------------------------------

_RUSAGE_INFO_V4 = 4
# xnu bsd/sys/resource.h rusage_info layout: ri_uuid[16] then u64 fields
# ri_user_time, ri_system_time, ri_pkg_idle_wkups, ri_interrupt_wkups,
# ri_pageins, ri_wired_size, ri_resident_size, ri_phys_footprint, ...
_RI_PHYS_FOOTPRINT_INDEX = 7
# Over-allocate the out-struct: the kernel writes exactly sizeof(flavor
# struct) for the requested flavor; a generous buffer stays valid even
# if a future flavor constant is passed and protects against struct
# drift (an undersized buffer segfaults — proven in the RCA repro).
_RUSAGE_U64_SLOTS = 64

# Lazy tri-state cache for the libproc handle:
#   None  → not yet attempted;  False → attempted + unavailable;
#   CDLL  → ready.
_LIBPROC: Any = None


class _RusageInfo(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
        (f"ri_u64_{i}", ctypes.c_uint64) for i in range(_RUSAGE_U64_SLOTS)
    ]


def _get_libproc() -> Any:
    """Lazy-load /usr/lib/libproc.dylib once. False when unavailable
    (non-darwin, hardened runtime, dlopen failure) — callers then fall
    back to rss with zero per-call retry overhead."""
    global _LIBPROC
    if _LIBPROC is None:
        if sys.platform != "darwin":
            _LIBPROC = False
        else:
            try:
                lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
                lib.proc_pid_rusage.argtypes = [
                    ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                ]
                lib.proc_pid_rusage.restype = ctypes.c_int
                _LIBPROC = lib
            except Exception:  # noqa: BLE001 — probe is strictly fail-soft
                _LIBPROC = False
    return _LIBPROC


def _probe_pid_footprint_mb(pid: int) -> Optional[float]:
    """``ri_phys_footprint`` of *pid* in MB, or None when unprobeable
    (non-darwin, dead pid, permission denied). Kernel-truth only — no
    application state, per the Watchdog Isolation Invariant."""
    lib = _get_libproc()
    if not lib or pid < 0:
        return None
    try:
        info = _RusageInfo()
        rc = lib.proc_pid_rusage(pid, _RUSAGE_INFO_V4, ctypes.byref(info))
        if rc != 0:
            return None
        raw = getattr(info, f"ri_u64_{_RI_PHYS_FOOTPRINT_INDEX}")
        if raw <= 0:
            return None
        return raw / (1024.0 * 1024.0)
    except Exception:  # noqa: BLE001 — never raise into a watchdog tick
        return None


# ---------------------------------------------------------------------------
# Metric resolution — env-driven, platform-adaptive, no hardcoding
# ---------------------------------------------------------------------------


def _resolve_probe_metric() -> str:
    """``footprint`` | ``rss`` from ``JARVIS_MEMORY_PROBE_METRIC``.

    ``auto`` (default, and any unrecognized value) picks footprint on
    darwin — where rss is compression-blind — and rss elsewhere
    (Linux has no page compressor stealing resident pages; rss is the
    honest metric there).
    """
    raw = os.environ.get("JARVIS_MEMORY_PROBE_METRIC", "auto").strip().lower()
    if raw in ("rss", "footprint"):
        return raw
    return "footprint" if sys.platform == "darwin" else "rss"


# ---------------------------------------------------------------------------
# Tree probe
# ---------------------------------------------------------------------------


def probe_process_tree_memory_mb() -> Optional[float]:
    """Sum memory of THIS process + all descendants, in MB.

    Per-pid metric: phys_footprint on darwin (compression-aware — see
    module docstring), rss elsewhere or wherever the footprint
    primitive fails; forced via ``JARVIS_MEMORY_PROBE_METRIC``.

    Probe cascade (the spirit of memory_pressure_gate's
    psutil→stdlib fallback, but PROCESS-scoped):
      1. psutil tree walk: Σ (footprint | rss) per pid
      2. resource.getrusage(SELF).ru_maxrss self-only high-water
         (Darwin reports bytes, Linux KiB)
    Returns None only if every probe fails (treated as transient).
    """
    use_footprint = _resolve_probe_metric() == "footprint"
    try:
        import psutil
        me = psutil.Process()
        procs = [me]
        try:
            procs.extend(me.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        total = 0.0
        for proc in procs:
            try:
                mb: Optional[float] = None
                if use_footprint:
                    mb = _probe_pid_footprint_mb(proc.pid)
                if mb is None:
                    mb = proc.memory_info().rss / (1024.0 * 1024.0)
                total += mb
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total
    except Exception:  # noqa: BLE001 — fall through to stdlib
        pass
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return ru / (1024.0 * 1024.0)
        return ru / 1024.0
    except Exception:  # noqa: BLE001
        return None


# Back-compat surface: both consumers (ProcessMemoryWatchdog,
# MemoryPressureGate) and their test monkeypatch seams import this
# name. It now measures footprint on darwin — strictly MORE signal,
# same contract (MB or None).
probe_process_tree_rss_mb = probe_process_tree_memory_mb
