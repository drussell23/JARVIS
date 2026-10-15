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
import sys
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Tuple

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
    "expanded": ("▾", "\033[2m"),
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


# ---------------------------------------------------------------------------
# Folds: long text shows its head; the rest is one key away (Claude Code's
# "… +N lines"). The parked text lives in the cockpit's BoundedBodyStore --
# the substrate the ov cockpit's /expand serves -- under this view's own ref
# family; the session log always has the whole text.
# ---------------------------------------------------------------------------

#: Tones whose text folds when long ("command" is what the user typed).
_FOLDED_TONES = frozenset({"reply", "detail", "failed", "done", "note"})


def _env_int(name: str, default: int, floor: int = 0) -> int:
    try:
        return max(floor, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def fold_lines() -> int:
    """Lines of a long text shown before its fold (``JARVIS_CONSOLE_FOLD_LINES``;
    0 shows everything)."""
    return _env_int("JARVIS_CONSOLE_FOLD_LINES", 4)


_store = None
_store_lock = threading.Lock()
_expanded: set = set()


def fold_store():
    """This terminal's parked texts: a BoundedBodyStore with its own ref
    family (``JARVIS_CONSOLE_FOLD_PREFIX``) and capacity
    (``JARVIS_CONSOLE_FOLD_STORE_SIZE``), outside the cockpit's spine."""
    global _store
    with _store_lock:
        if _store is None:
            from backend.core.ouroboros.battle_test.tool_render_store import BoundedBodyStore
            _store = BoundedBodyStore(
                capacity=_env_int("JARVIS_CONSOLE_FOLD_STORE_SIZE", 50, floor=1),
                ref_prefix=os.environ.get("JARVIS_CONSOLE_FOLD_PREFIX", "").strip() or "r-",
                spine_kind=None)
        return _store


def _park(title: str, body: str) -> str:
    return fold_store().store(op_id="console", round_index=0, tool_name=title,
                              body=body, summary=title).ref


# ---------------------------------------------------------------------------
# Withheld lines: what the view leaves out, kept for a while so a finished
# boot phase can carry the lines logged while it ran, folded under its ✓.
# ---------------------------------------------------------------------------

_withheld: Deque[Tuple[float, str]] = deque(
    maxlen=_env_int("JARVIS_CONSOLE_WITHHELD_LINES", 5000, floor=1))


def withhold(plain: str, at: Optional[float] = None) -> None:
    """A line the view leaves out of the terminal: into the session log, and
    into the window a phase's fold is cut from."""
    from backend.core.session_log import log_only
    log_only(plain)
    _withheld.append((time.time() if at is None else at, plain))


def withheld_between(start: float, end: float) -> List[str]:
    """Withheld lines logged from *start* to *end* (wall-clock seconds)."""
    return [line for at, line in list(_withheld) if start <= at <= end]


class WithheldRecorder(logging.Handler):
    """Keeps the records the view leaves out of the terminal, for the phase
    folds (the session log has them through its own handler)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if tone_of(record) is None and not shows(record.levelno):
                _withheld.append((record.created, self.format(record)))
        except Exception:  # noqa: BLE001 -- logging never raises into its caller
            self.handleError(record)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(text: str, tone: str, *, color: bool = True, suffix: str = "",
           body: str = "") -> str:
    """A narrative line as the terminal shows it. A long *text* folds after
    ``fold_lines()`` lines and a *body* (a phase's log lines) folds whole;
    either leaves one "… +N lines" line naming its ref."""
    glyph, style = _TONES.get(tone, _TONES["note"])
    lines = text.splitlines() or [text]
    hidden, ref = 0, ""
    limit = fold_lines()
    if tone in _FOLDED_TONES and limit and len(lines) > limit:
        hidden += len(lines) - limit
        ref = _park(lines[0], text + (("\n\n" + body) if body else ""))
        lines = lines[:limit]
    elif body:
        ref = _park(lines[0], body)
    if body:
        hidden += len(body.splitlines())
    indent = " " * (len(glyph) + 1)
    # The suffix (how long it took) belongs to the first line, where the
    # eye reads what happened; the rest of the text continues under it.
    head, rest = lines[0], lines[1:]
    if suffix:
        head += f"  {_DIM}{suffix}{_RESET}" if color else f"  {suffix}"
    shown = ("\n" + indent).join([head] + rest)
    if not color:
        line = f"{glyph} {shown}"
    else:
        line = (f"{style}{glyph} {shown}{_RESET}" if tone in ("detail", "note", "expanded")
                else f"{style}{glyph}{_RESET} {shown}")
    if ref:
        key = expand_key()
        marker = f"… +{hidden} lines ({key} to expand · {ref})" if key else f"… +{hidden} lines ({ref})"
        line += "\n" + indent + (f"{_DIM}{marker}{_RESET}" if color else marker)
    return line


def render_record(record: logging.LogRecord, *, color: bool = True) -> Optional[str]:
    """A narrative record rendered for the terminal; None for ordinary ones."""
    tone = tone_of(record)
    if tone is None:
        return None
    return render(record.getMessage(), tone, color=color,
                  suffix=str(getattr(record, "console_suffix", "") or ""),
                  body=str(getattr(record, "console_body", "") or ""))


def narrate(text: str, tone: str = "note", *, suffix: str = "", body: str = "",
            console_only: bool = False) -> None:
    """Put a line in the terminal's narrative -- and, like any record, in the
    session log and the IPC stream, unless *console_only* (a view action,
    such as an expansion, re-shows what the log already has). *body* is
    folded under the line. Never raises."""
    text = (text or "").strip()
    if not text:
        return
    try:
        logging.getLogger(NARRATIVE_LOGGER).info(
            text, extra={"console_tone": tone if tone in _TONES else "note",
                         "console_suffix": suffix, "console_body": body,
                         "console_only": console_only})
    except Exception:  # noqa: BLE001 -- narration never breaks what it narrates
        pass


def is_console_only(record: logging.LogRecord) -> bool:
    return bool(getattr(record, "console_only", False))


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def view_key() -> str:
    """The key that changes the view (``JARVIS_CONSOLE_VIEW_KEY``; empty: none)."""
    return os.environ.get("JARVIS_CONSOLE_VIEW_KEY", "v").strip()[:1]


def expand_key() -> str:
    """The key that opens a fold (``JARVIS_CONSOLE_EXPAND_KEY``; empty: none)."""
    return os.environ.get("JARVIS_CONSOLE_EXPAND_KEY", "e").strip()[:1]


def cycle_view() -> None:
    """Move the terminal to the next view and say so."""
    narrate(f"Terminal view: {VIEW_NAMES[cycle_verbosity()]}", "note", console_only=True)


def expand_latest() -> Optional[str]:
    """Open the newest fold not opened yet -- each press goes one further
    back. Returns its ref, or None when every fold is open or evicted."""
    store = fold_store()
    for ref in reversed(store.all_refs()):
        stored = store.lookup(ref)
        if ref in _expanded or stored is None:
            continue
        _expanded.add(ref)
        # The text heads itself when it is the line that folded (a reply);
        # a phase's lines are headed by the phase they belong to.
        header = ref if stored.body.startswith(stored.summary) else f"{stored.summary} · {ref}"
        narrate(f"{header}\n{stored.body}", "expanded", console_only=True)
        return ref
    narrate("Nothing folded is left to expand.", "note", console_only=True)
    return None


def key_bindings() -> Dict[str, Callable[[], object]]:
    """The keys this terminal answers (configured ones only)."""
    return {key: action for key, action in ((view_key(), cycle_view),
                                            (expand_key(), expand_latest)) if key}


def key_hint() -> str:
    """The keys, as the status region's footer names them."""
    names = {view_key(): f"view: {VIEW_NAMES[verbosity()]}", expand_key(): "expand"}
    return " · ".join(f"{key} {names[key]}" for key in key_bindings())


class ConsoleKeys:
    """Answers single keys from an interactive terminal: ``msvcrt`` on
    Windows; cbreak mode on POSIX, which keeps Ctrl+C a signal and is put
    back however the listener ends. Keys without a binding are ignored."""

    def __init__(self, bindings: Optional[Dict[str, Callable[[], object]]] = None,
                 poll_s: float = 0.25) -> None:
        self._bindings = {k.lower(): a for k, a in (bindings or key_bindings()).items()}
        self._poll_s = poll_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        stdin = sys.stdin
        try:
            interactive = bool(self._bindings) and stdin is not None and stdin.isatty()
        except (ValueError, OSError):
            interactive = False
        if not interactive:
            return False
        self._thread = threading.Thread(target=self._run, name="console_keys", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, 2 * self._poll_s))
        self._thread = None

    def handle(self, char: str) -> None:
        action = self._bindings.get((char or "").lower())
        if action is not None:
            try:
                action()
            except Exception:  # noqa: BLE001 -- a key never breaks the terminal
                pass

    def _run(self) -> None:
        if os.name == "nt":
            import msvcrt
            while not self._stop.is_set():
                if msvcrt.kbhit():
                    self.handle(msvcrt.getwch())
                else:
                    self._stop.wait(self._poll_s)
            return
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], self._poll_s)
                if ready:
                    self.handle(os.read(fd, 1).decode(errors="ignore"))
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


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
    "ConsoleFilter", "ConsoleKeys", "NARRATIVE_LOGGER", "VERBOSITIES", "VIEW_NAMES",
    "WithheldRecorder", "cycle_verbosity", "cycle_view", "expand_key", "expand_latest",
    "fold_lines", "fold_store", "is_console_only", "key_bindings", "key_hint", "narrate",
    "phase_title", "render", "render_record", "seconds", "set_verbosity", "shows", "threshold",
    "tone_of", "verbosity", "view_key", "withhold", "withheld_between",
]
