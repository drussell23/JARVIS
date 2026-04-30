"""
Ouroboros Serpent Animation — Visual indicator when the governance pipeline is active.

The Ouroboros (serpent eating its own tail) is the symbol of self-modification.
When the governance pipeline is running, a living ASCII serpent animation
appears in the terminal, rotating through frames to show the organism is
thinking, evolving, and working.

The animation runs in a background asyncio task and is automatically
started/stopped by the orchestrator at pipeline entry/exit.

Boundary Principle:
  Pure visual feedback. No model inference, no side effects.
  The animation is deterministic (frame sequence), non-blocking
  (asyncio task), and self-cleaning (restores terminal state on stop).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get(
    "JARVIS_OUROBOROS_ANIMATION", "true"
).lower() in ("true", "1", "yes")

# Suppression hook retained for callers that want to silence the
# serpent animation in non-interactive contexts (CI, headless soaks).
# Historically called by the now-retired LiveDashboard at boot.
_SUPPRESSED = False


def suppress() -> None:
    """Suppress the serpent animation. Used by non-interactive callers."""
    global _SUPPRESSED
    _SUPPRESSED = True

# ═══════════════════════════════════════════════════════════════════════════
# Ouroboros serpent frames — the snake eating its own tail
# ═══════════════════════════════════════════════════════════════════════════

_SERPENT_FRAMES = [
    # Frame 0: Head at top, eating tail
    r"""
       ___
      /   \
     | O>  |
      \   /
     --\-/--
    |  OUROBOROS  |
     --/---\--
      / >>> \
     | >>>>> |
      \_____/
    """,
    # Frame 1: Rotation
    r"""
       ___
      / > \
     | O   |
      \ > /
     --\-/--
    | >OUROBOROS> |
     --/---\--
      /  >> \
     |  >>> |
      \_____/
    """,
    # Frame 2: Digesting
    r"""
       ___
      />> \
     | O   |
      \>> /
     --\-/--
    |>>OUROBOROS>>|
     --/---\--
      / >>  \
     | >>>  |
      \_____/
    """,
    # Frame 3: Full cycle
    r"""
       ___
      />>>\
     | O>  |
      \>>>/
     --\-/--
    |>OUROBOROS>>|
     --/---\--
      />>>  \
     |>>>>  |
      \_____/
    """,
]

# Compact single-line spinner for non-TTY environments
_SPINNER_FRAMES = [
    "\U0001F40D [ OUROBOROS ] ~~~~~~~~~~~~>  ",
    "\U0001F40D [ OUROBOROS ] ~~~~~~~~~~~~~> ",
    "\U0001F40D [ OUROBOROS ] ~~~~~~~~~~~~~~>",
    "\U0001F40D [ OUROBOROS ] >~~~~~~~~~~~~~~",
    "\U0001F40D [ OUROBOROS ] ~>~~~~~~~~~~~~~",
    "\U0001F40D [ OUROBOROS ] ~~>~~~~~~~~~~~~",
    "\U0001F40D [ OUROBOROS ] ~~~>~~~~~~~~~~~",
    "\U0001F40D [ OUROBOROS ] ~~~~>~~~~~~~~~~",
]

# Minimal phase indicators with snake emoji
_PHASE_ICONS = {
    "CLASSIFY":          "\U0001F40D [ OUROBOROS ] CLASSIFY  >>>>>>>>>>>>>>>",
    "ROUTE":             "\U0001F40D [ OUROBOROS ] ROUTE     >>>>>>>>>>>>>  ",
    "CONTEXT_EXPANSION": "\U0001F40D [ OUROBOROS ] EXPAND    >>>>>>>>>>>    ",
    "GENERATE":          "\U0001F40D [ OUROBOROS ] GENERATE  >>>>>>>>>      ",
    "VALIDATE":          "\U0001F40D [ OUROBOROS ] VALIDATE  >>>>>>>        ",
    "GATE":              "\U0001F40D [ OUROBOROS ] GATE      >>>>>          ",
    "APPROVE":           "\U0001F40D [ OUROBOROS ] APPROVE   >>>            ",
    "APPLY":             "\U0001F40D [ OUROBOROS ] APPLY     >              ",
    "VERIFY":            "\U0001F40D [ OUROBOROS ] VERIFY    >>             ",
    "COMPLETE":          "\U0001F40D [ OUROBOROS ] COMPLETE  >>>>           ",
}


class OuroborosSerpent:
    """Living terminal animation showing the Ouroboros pipeline is active.

    Usage:
        serpent = OuroborosSerpent()
        await serpent.start("GENERATE")    # Start spinning
        serpent.update_phase("VALIDATE")   # Update phase label
        await serpent.stop()               # Clean up
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._phase = ""
        self._frame_idx = 0
        self._start_time = 0.0
        self._use_compact = not (sys.stderr.isatty() and _ENABLED)

    async def start(self, phase: str = "CLASSIFY") -> None:
        """Start the serpent animation in a background task."""
        if not _ENABLED or _SUPPRESSED:
            return
        self._phase = phase
        self._running = True
        self._start_time = time.monotonic()
        self._frame_idx = 0
        self._task = asyncio.create_task(
            self._animate(), name="ouroboros_serpent"
        )

    def update_phase(self, phase: str) -> None:
        """Update the current phase label."""
        self._phase = phase

    async def stop(self, success: bool = True) -> None:
        """Stop the animation and show final status."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if not _ENABLED:
            return

        elapsed = time.monotonic() - self._start_time
        icon = "\U0001F40D COMPLETE \u2705" if success else "\U0001F40D FAILED \u274C"
        _clear_line()
        sys.stderr.write(
            f"\r{icon} [ OUROBOROS ] in {elapsed:.1f}s\n"
        )
        sys.stderr.flush()

    async def _animate(self) -> None:
        """Background animation loop."""
        while self._running:
            try:
                if self._use_compact:
                    self._render_compact()
                else:
                    self._render_compact()  # Use compact for now (full art later)
                self._frame_idx = (self._frame_idx + 1) % len(_SPINNER_FRAMES)
                await asyncio.sleep(0.15)
            except asyncio.CancelledError:
                break
            except Exception:
                break

    def _render_compact(self) -> None:
        """Render single-line spinner with phase."""
        elapsed = time.monotonic() - self._start_time
        phase_line = _PHASE_ICONS.get(self._phase, _SPINNER_FRAMES[self._frame_idx])
        _clear_line()
        sys.stderr.write(f"\r{phase_line} ({elapsed:.0f}s)")
        sys.stderr.flush()


def _clear_line() -> None:
    """Clear the current terminal line."""
    sys.stderr.write("\r\033[K")


# ═══════════════════════════════════════════════════════════════════════════
# Global singleton for the orchestrator to use
# ═══════════════════════════════════════════════════════════════════════════

_global_serpent: Optional[OuroborosSerpent] = None


def get_serpent() -> OuroborosSerpent:
    """Get or create the global Ouroboros serpent."""
    global _global_serpent
    if _global_serpent is None:
        _global_serpent = OuroborosSerpent()
    return _global_serpent
