"""`say -v ?` — asked once per process, bounded, never on the loop twice.

TWO SUBSYSTEMS WERE ASKING THE SAME IMMUTABLE QUESTION
------------------------------------------------------
`realtime_voice_communicator._discover_voices` and
`trinity_voice_coordinator._detect_best_voice` each ran
`subprocess.run(['say', '-v', '?'])` — with 15s and 5s timeouts
respectively — from their constructors. StallSampler caught BOTH on the
wedged main loop; after the screen-context fixes landed they were the only
two frames left.

The installed voice list does not change during a process's life. Two
subsystems shelling out for the same static answer is duplicated work whose
cost is paid in event-loop latency, so the answer is fetched once and shared.

WHY A RAW STRING AND NOT A PARSED MODEL
----------------------------------------
The two callers want different things from it: one builds a catalogue keyed
by name, the other picks a single best voice. Parsing here would force both
into a shape neither asked for and would put this module in the business of
deciding what a "voice" is. It caches the bytes `say` produced; meaning stays
with the caller.

FAILURE IS A DISTINCT ANSWER FROM "NO VOICES"
----------------------------------------------
`None` means the query could not be answered — timed out, refused by the
breaker, or `say` is absent (a Linux node). An empty string would say "this
machine has no voices", which is a different claim and one that would make a
caller fall back to a wrong default rather than to its own.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("Jarvis.SystemVoices")

SYSTEM_VOICES_SCHEMA_VERSION: str = "system_voices.1"

__all__ = [
    "SYSTEM_VOICES_SCHEMA_VERSION",
    "reset_for_tests",
    "voice_catalog_raw",
]

_lock = threading.Lock()
_cached: Optional[str] = None
_attempted = False


def _timeout_s() -> float:
    """``JARVIS_VOICE_CATALOG_TIMEOUT_S`` — default 3s.

    Deliberately tighter than the 15s and 5s the two call sites carried.
    `say -v ?` reads a local catalogue and returns in milliseconds; a
    fifteen-second budget does not make a slow machine succeed, it makes a
    wedged one expensive. The bound that matters is the reap, and
    `run_bounded` owns that.
    """
    try:
        return max(0.5, min(60.0, float(
            (os.environ.get("JARVIS_VOICE_CATALOG_TIMEOUT_S") or "").strip()
            or 3.0)))
    except Exception:  # noqa: BLE001
        return 3.0


def voice_catalog_raw(force: bool = False) -> Optional[str]:
    """The raw stdout of ``say -v ?``, cached per process. NEVER raises.

    Returns None when the query could not be answered. A failure is cached
    too: a machine without `say` will not grow one, and retrying per
    constructor is how a missing binary turns into a recurring stall.
    ``force=True`` re-asks (tests, and an operator who just installed a
    voice).
    """
    global _cached, _attempted  # noqa: PLW0603
    with _lock:
        if _attempted and not force:
            return _cached

    result = None
    try:
        from backend.core.bounded_subprocess import run_bounded
        completed = run_bounded(["say", "-v", "?"], timeout=_timeout_s(),
                                text=True)
        if completed is not None and completed.returncode == 0:
            result = completed.stdout or ""
        elif completed is None:
            logger.debug("[SystemVoices] `say -v ?` unanswered "
                         "(timeout, breaker, or absent)")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SystemVoices] catalog query failed: %s", exc,
                     exc_info=True)

    with _lock:
        _cached = result
        _attempted = True
    return result


def reset_for_tests() -> None:
    """Test-only."""
    global _cached, _attempted  # noqa: PLW0603
    with _lock:
        _cached = None
        _attempted = False
