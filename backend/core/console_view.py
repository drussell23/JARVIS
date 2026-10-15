"""
console_view - what the supervisor's terminal shows, decided in one place.

The terminal used to show every INFO record of a hundred modules (1,756 lines
in the 18:30 boot of 2026-10-14) above the status region, so what was going
on -- which phase, what JARVIS did with a command -- scrolled past inside
noise. The session log keeps all of it (``session_log.SessionLogHandler``);
the terminal shows a VIEW of it:

* **narrative lines**: what a person follows -- each boot phase as it
  finishes ("✓ Preflight  15.7s"), each command and what JARVIS did with it.
  They are log records of :data:`NARRATIVE_LOGGER` carrying a ``console_tone``,
  so the session log, the IPC viewer and the terminal all receive the same
  record;
* **records at or above the view's threshold**, set by the existing
  ``JARVIS_UI_VERBOSITY`` (``--verbosity``; ``--verbose`` means ``debug``):
  ``summary`` shows errors, ``ops`` (the default) warnings and errors,
  ``debug`` everything the loggers emit -- today's full stream.

The view can be changed while JARVIS runs (:func:`cycle_verbosity`, bound to
a key by the status region), so the full stream is one key away.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Optional, Tuple

#: Ordered quietest first; ``cycle_verbosity`` walks this order.
VERBOSITIES: Tuple[str, ...] = ("summary", "ops", "debug")
_DEFAULT = "ops"

#: The lowest record level each view shows (narrative lines always show).
_THRESHOLDS: Dict[str, int] = {
    "summary": logging.ERROR,
    "ops": logging.WARNING,
    "debug": logging.NOTSET,
}

#: What each view is called where it is shown to the user.
VIEW_NAMES: Dict[str, str] = {"summary": "quiet", "ops": "normal", "debug": "full log"}

#: The logger narrative lines are recorded under.
NARRATIVE_LOGGER = "jarvis.console"

#: tone -> (glyph, ANSI style). The glyphs follow Claude Code's transcript:
#: "›" what the user asked, "⏺" what JARVIS answered, "⎿" what it did.
_TONES: Dict[str, Tuple[str, str]] = {
    "done": ("✓", "\033[92m"),
    "failed": ("✗", "\033[91m"),
    "command": ("›", "\033[1m"),
    "reply": ("⏺", "\033[96m"),
    "detail": ("  ⎿", "\033[2m"),
    "note": ("·", "\033[2m"),
}
_RESET = "\033[0m"
_DIM = "\033[2m"

_lock = threading.Lock()
_override: Optional[str] = None


def _normalized(value: str) -> Optional[str]:
    value = (value or "").strip().lower()
    return value if value in VERBOSITIES else None


def verbosity() -> str:
    """The view in force: the one chosen at runtime, else the configured one."""
    with _lock:
        if _override is not None:
            return _override
    return _normalized(os.environ.get("JARVIS_UI_VERBOSITY", "")) or _DEFAULT


def set_verbosity(value: str) -> str:
    """Choose the view for the rest of this run; returns the one in force."""
    global _override
    chosen = _normalized(value)
    if chosen is not None:
        with _lock:
            _override = chosen
    return verbosity()


def cycle_verbosity() -> str:
    """Move to the next more detailed view (wrapping to the quietest)."""
    current = verbosity()
    return set_verbosity(VERBOSITIES[(VERBOSITIES.index(current) + 1) % len(VERBOSITIES)])


def threshold() -> int:
    return _THRESHOLDS[verbosity()]


def tone_of(record: logging.LogRecord) -> Optional[str]:
    """The narrative tone a record carries, or None for an ordinary record."""
    tone = getattr(record, "console_tone", None)
    return tone if tone in _TONES else None


def shows(levelno: int, tone: Optional[str] = None) -> bool:
    """Whether the terminal shows a record of *levelno* (or narrative *tone*)."""
    return tone is not None or levelno >= threshold()


class ConsoleFilter(logging.Filter):
    """The view, applied to a terminal handler. Reads the view per record, so
    a change made while JARVIS runs takes effect on the next line."""

    def filter(self, record: logging.LogRecord) -> bool:
        return shows(record.levelno, tone_of(record))


def render(text: str, tone: str, *, color: bool = True, suffix: str = "") -> str:
    """A narrative line as the terminal shows it."""
    glyph, style = _TONES.get(tone, _TONES["note"])
    # A multi-line text (a description of the screen) stays under its glyph.
    text = text.replace("\n", "\n" + " " * (len(glyph) + 1))
    if not color:
        return f"{glyph} {text}" + (f"  {suffix}" if suffix else "")
    body = f"{style}{glyph}{_RESET} {text}"
    if tone in ("detail", "note"):
        body = f"{style}{glyph} {text}{_RESET}"
    return body + (f"  {_DIM}{suffix}{_RESET}" if suffix else "")


def render_record(record: logging.LogRecord, *, color: bool = True) -> Optional[str]:
    """A narrative record rendered for the terminal; None for ordinary ones."""
    tone = tone_of(record)
    if tone is None:
        return None
    return render(record.getMessage(), tone, color=color,
                  suffix=str(getattr(record, "console_suffix", "") or ""))


def narrate(text: str, tone: str = "note", *, suffix: str = "") -> None:
    """Put a line in the terminal's narrative (and, like any record, in the
    session log and the IPC stream). Never raises."""
    text = (text or "").strip()
    if not text:
        return
    try:
        logging.getLogger(NARRATIVE_LOGGER).info(
            text, extra={"console_tone": tone if tone in _TONES else "note",
                         "console_suffix": suffix})
    except Exception:  # noqa: BLE001 -- narration never breaks what it narrates
        pass


def seconds(value: float) -> str:
    """A duration as the narrative shows it."""
    if value < 10:
        return f"{value:.1f}s"
    if value < 90:
        return f"{value:.0f}s"
    return f"{value / 60:.1f}m"


def phase_title(phase: str) -> str:
    """A phase identifier as a person reads it ("clean_slate" -> "Clean slate")."""
    words = (phase or "").replace("-", " ").replace("_", " ").split()
    return " ".join(words).capitalize() if words else "Phase"


__all__ = [
    "ConsoleFilter", "NARRATIVE_LOGGER", "VERBOSITIES", "VIEW_NAMES", "cycle_verbosity",
    "narrate", "phase_title", "render", "render_record", "seconds", "set_verbosity",
    "shows", "threshold", "tone_of", "verbosity",
]
