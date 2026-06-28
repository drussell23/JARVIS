"""Shared Iron Gate exploration floor — single source of truth.

Used by:
  * ``orchestrator.py`` — Iron Gate (currently inline; should import from here,
    tracked as I1 follow-up for the next PR so orchestrator.py is not in PR D scope).
  * ``tool_executor.py`` — Epistemic Feedback Loop (PR D I1).

Both consumers MUST read the same env var (``JARVIS_MIN_EXPLORATION_CALLS``) with the
same complexity-scaled defaults so the ContextVar floor and the Iron Gate floor are
always in sync.  Having this logic in two places caused the floor mismatch described
in PR D review finding I1: moderate/complex ops saw floor=1 in the loop but floor=2
at the gate, so the loop's re-prompt fired AFTER the gate already approved.

Pure module — no I/O, no orchestrator imports.  NEVER raises.
"""
from __future__ import annotations

import os
from typing import FrozenSet

# ---------------------------------------------------------------------------
# BROAD-6 canonical exploration tool set
# ---------------------------------------------------------------------------
# Mirrors orchestrator.py's _EXPLORATION_TOOLS (lines 5993-5996).  When the
# Iron Gate set changes, update this constant and both consumers get the fix
# automatically.

IRON_GATE_EXPLORATION_TOOLS: FrozenSet[str] = frozenset({
    "read_file",
    "search_code",
    "get_callers",
    "list_symbols",
    "glob_files",
    "list_dir",
})


# ---------------------------------------------------------------------------
# Complexity-scaled floor helper
# ---------------------------------------------------------------------------

def iron_gate_exploration_floor(task_complexity: str = "") -> int:
    """Complexity-scaled Iron Gate exploration floor.

    Reads ``JARVIS_MIN_EXPLORATION_CALLS`` — the SAME env var that
    ``orchestrator.py`` reads — so operators get a single knob.  Falls back to
    ``JARVIS_TOOL_LOOP_MIN_EXPLORATION`` (the old per-loop knob) for backward
    compat with callers that pre-date this helper.

    Complexity-scaled defaults (mirrors orchestrator.py inline logic exactly):

      * ``simple``              → 1  (single-file mechanical ops: one read IS
                                       reading the codebase)
      * anything else (default) → 2  (moderate / complex / unknown — conservative;
                                       the safe value when complexity is unavailable)

    Args:
        task_complexity: Value of ``ctx.task_complexity`` (e.g. ``"simple"``,
            ``"moderate"``, ``"complex"``).  Pass ``""`` when not available;
            defaults to 2.

    Returns:
        Minimum number of BROAD-6 exploration calls required before a patch.
        Always >= 0.  NEVER raises.
    """
    # Primary: JARVIS_MIN_EXPLORATION_CALLS (matches the Iron Gate)
    _env = os.environ.get("JARVIS_MIN_EXPLORATION_CALLS", "").strip()
    if not _env:
        # Fallback: old per-loop knob (backward compat)
        _env = os.environ.get("JARVIS_TOOL_LOOP_MIN_EXPLORATION", "").strip()
    if _env:
        try:
            return max(0, int(_env))
        except (TypeError, ValueError):
            pass

    # Complexity-scaled default — mirrors orchestrator.py lines 5999-6002
    if (task_complexity or "").strip().lower() == "simple":
        return 1
    # moderate / complex / unknown → 2 (conservative)
    return 2
