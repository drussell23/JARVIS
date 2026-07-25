"""Terminal capability probing — does this terminal deliver key RELEASE events?

Lives OUTSIDE ``ui/`` deliberately: this is terminal *protocol I/O*, not
rendering. The ``ui/`` package is guarded against literal ANSI/styling
(``tests/ui/test_theme_guard.py``) so the theme stays the single source of
truth for appearance, and the kitty handshake below necessarily emits a raw
CSI sequence. Widening that guard's exemption list — which holds exactly one
entry, the token source-of-truth — to accommodate a non-UI concern would have
been the wrong trade.

The fragmentation problem
-------------------------
A standard TTY sends keypresses only, so hold-to-talk is impossible. Terminals
implementing the **kitty keyboard protocol** (kitty, WezTerm, foot, recent
iTerm2, Ghostty) can report key *release* when asked, which makes true
hold-to-talk exact. Hardcoding either assumption breaks half the hosts, so the
input paradigm is chosen by asking the terminal rather than by guessing.

The probe
---------
The kitty protocol defines a query: emit ``CSI ? u`` and a supporting terminal
replies ``CSI ? <flags> u``. A terminal without support replies nothing.

Doing this safely on a live cockpit is the whole difficulty:

* it needs raw mode (an un-raw terminal would echo the escape sequence into the
  operator's screen), restored under ``finally`` on every path;
* it must never block — a non-responding terminal has to time out fast, so the
  read is ``select``-bounded;
* it must fail CLOSED. An ambiguous or absent reply means "no release events",
  because degrading to toggle is harmless while wrongly assuming release
  support would leave the mic latched open with no closing edge;
* it must be inert when there is no real TTY (CI, pipes, headless soaks) — the
  probe is skipped entirely rather than reading a nonexistent terminal.

``TERM``/``TERM_PROGRAM`` are used only as a *hint* for telemetry, never as the
verdict: terminal identification strings are notoriously spoofed and inherited
through multiplexers, so the live handshake is the only trustworthy authority.
"""

from __future__ import annotations

import enum
import logging
import os
import sys
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: kitty keyboard-protocol query and the prefix of a conforming reply.
_QUERY = "\x1b[?u"
_REPLY_PREFIX = "\x1b[?"

#: Terminals KNOWN to implement the protocol. Hint only — see module docstring.
_LIKELY_SUPPORT = ("kitty", "wezterm", "foot", "ghostty")


class KeyReleaseSupport(str, enum.Enum):
    """Closed verdict taxonomy."""

    SUPPORTED = "supported"          # handshake confirmed release capability
    UNSUPPORTED = "unsupported"      # handshake completed, no support
    NO_TTY = "no_tty"                # nothing to probe (pipe / CI / headless)
    TIMEOUT = "timeout"              # terminal never answered
    FORCED_ON = "forced_on"          # operator override
    FORCED_OFF = "forced_off"        # operator override
    ERROR = "error"                  # probe faulted — fail closed

    @property
    def has_release(self) -> bool:
        """Only two verdicts grant release-driven hold-to-talk. Everything else
        — including TIMEOUT and ERROR — degrades. Fail-closed by construction."""
        return self in (KeyReleaseSupport.SUPPORTED, KeyReleaseSupport.FORCED_ON)


def _probe_timeout_s() -> float:
    try:
        return max(0.01, float(os.environ.get("JARVIS_PTT_PROBE_TIMEOUT_S", "0.12")))
    except (TypeError, ValueError):
        return 0.12


def _forced() -> Optional[KeyReleaseSupport]:
    """Operator override, checked before any I/O so a probe can be bypassed on
    a terminal where even the handshake misbehaves."""
    raw = os.environ.get("JARVIS_PTT_KEY_RELEASE_SUPPORTED", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return KeyReleaseSupport.FORCED_ON
    if raw in ("0", "false", "no", "off"):
        return KeyReleaseSupport.FORCED_OFF
    return None


def terminal_hint() -> str:
    """Best-guess terminal identity, for telemetry only."""
    for var in ("TERM_PROGRAM", "TERM"):
        val = (os.environ.get(var, "") or "").strip().lower()
        if val:
            for name in _LIKELY_SUPPORT:
                if name in val:
                    return name
            return val
    return "unknown"


def _is_real_tty() -> bool:
    """A genuine interactive terminal on BOTH directions. ``sys.__stdin__`` is
    used rather than ``sys.stdin`` because the cockpit runs under
    ``patch_stdout``, which replaces the module-level handle — the same trap
    that broke presentation-restraint TTY detection."""
    try:
        stdin = getattr(sys, "__stdin__", None)
        stdout = getattr(sys, "__stdout__", None)
        return bool(
            stdin is not None and stdout is not None
            and stdin.isatty() and stdout.isatty()
        )
    except Exception:  # noqa: BLE001
        return False


def probe_key_release_support(
    *, timeout_s: Optional[float] = None,
) -> Tuple[KeyReleaseSupport, dict]:
    """Ask the terminal whether it reports key releases.

    Returns ``(verdict, telemetry)``. NEVER raises, NEVER leaves the terminal in
    raw mode, and never blocks longer than ``timeout_s``."""
    hint = terminal_hint()
    forced = _forced()
    if forced is not None:
        return (forced, {"reason": "env_override", "terminal": hint})

    if not _is_real_tty():
        return (KeyReleaseSupport.NO_TTY, {"reason": "not_a_tty", "terminal": hint})

    to = timeout_s if timeout_s is not None else _probe_timeout_s()
    try:
        import select
        import termios
        import tty
    except Exception:  # noqa: BLE001 — non-POSIX host
        return (KeyReleaseSupport.ERROR, {"reason": "no_termios", "terminal": hint})

    fd = None
    saved = None
    try:
        fd = sys.__stdin__.fileno()
        saved = termios.tcgetattr(fd)
        # cbreak, not full raw: leaves signal handling intact so Ctrl-C still
        # works if the probe somehow stalls.
        tty.setcbreak(fd)
        try:
            sys.__stdout__.write(_QUERY)
            sys.__stdout__.flush()
        except Exception:  # noqa: BLE001
            return (KeyReleaseSupport.ERROR, {"reason": "write_failed", "terminal": hint})

        chunks = []
        deadline_budget = to
        while deadline_budget > 0:
            ready, _, _ = select.select([fd], [], [], deadline_budget)
            if not ready:
                break
            try:
                data = os.read(fd, 32)
            except Exception:  # noqa: BLE001
                break
            if not data:
                break
            chunks.append(data.decode("utf-8", "replace"))
            joined = "".join(chunks)
            # A conforming reply terminates with 'u'.
            if _REPLY_PREFIX in joined and joined.rstrip().endswith("u"):
                break
            deadline_budget *= 0.5   # bounded drain; never re-arm the full budget

        reply = "".join(chunks)
        if not reply:
            return (KeyReleaseSupport.TIMEOUT, {
                "reason": "no_reply", "terminal": hint,
            })
        if _REPLY_PREFIX in reply and reply.rstrip().endswith("u"):
            return (KeyReleaseSupport.SUPPORTED, {
                "reason": "handshake_ok", "terminal": hint,
                "reply": reply.encode("unicode_escape").decode()[:40],
            })
        # Something answered, but not the protocol. Ambiguity fails CLOSED.
        return (KeyReleaseSupport.UNSUPPORTED, {
            "reason": "non_protocol_reply", "terminal": hint,
            "reply": reply.encode("unicode_escape").decode()[:40],
        })
    except Exception as exc:  # noqa: BLE001
        return (KeyReleaseSupport.ERROR, {
            "reason": type(exc).__name__, "terminal": hint,
        })
    finally:
        # Restoring the terminal is non-negotiable: leaking cbreak would leave
        # the operator's shell unusable after exit.
        if fd is not None and saved is not None:
            try:
                import termios as _t
                _t.tcsetattr(fd, _t.TCSADRAIN, saved)
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "KeyReleaseSupport",
    "probe_key_release_support",
    "terminal_hint",
]
