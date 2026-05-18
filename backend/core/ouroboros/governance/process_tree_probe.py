"""Shared process-tree RSS probe (P5 Arc C, Slice 5a).

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
"""
from __future__ import annotations

import sys
from typing import Optional


def probe_process_tree_rss_mb() -> Optional[float]:
    """Sum RSS of THIS process + all descendants, in MB.

    Probe cascade (the spirit of memory_pressure_gate's
    psutil→stdlib fallback, but PROCESS-scoped):
      1. psutil: self.rss + Σ children(recursive).rss
      2. resource.getrusage(SELF).ru_maxrss self-only high-water
         (Darwin reports bytes, Linux KiB)
    Returns None only if every probe fails (treated as transient).
    """
    try:
        import psutil
        me = psutil.Process()
        total = me.memory_info().rss
        for child in me.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total / (1024.0 * 1024.0)
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
