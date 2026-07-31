"""What a real terminal actually sends — keys, mouse, and window size.

`PtySession` in `test_headless_tui_integration.py` supplies a genuine TTY and
can already type text. Driving the COCKPIT needs more than text, because the
things thirty commits of UI work changed are not characters:

    Ctrl+O          is  b"\\x0f"
    Ctrl+Z          is  b"\\x1a"
    a click at 4,7  is  b"\\x1b[<0;5;8M" then b"\\x1b[<0;5;8m"

This module is that vocabulary. It encodes what a terminal emits, so the
production key bindings and mouse handlers are exercised on their own terms —
the same discipline the PTY harness already states: supply a real terminal
rather than defeat the guards. A test that called `handler(event)` directly
would prove the handler works in a world where prompt_toolkit's key parser,
the mouse-capture mode string and the SGR encoding are all assumed correct.
Those assumptions are exactly what has never been checked.

Why SGR and not X10
-------------------
prompt_toolkit enables SGR mouse reporting (`\\x1b[?1006h`) alongside the
button-event mode, because the older X10 encoding packs coordinates into
single bytes and breaks past column 223. Emitting X10 here would work in a
narrow terminal and silently mis-address a click in a wide one — which is
precisely the class of defect this harness exists to catch rather than
reproduce.

Rows and columns are 1-BASED on the wire and 0-based everywhere in the
application. The conversion happens HERE, once, so no caller has to remember
it — an off-by-one in a click coordinate is invisible in a passing test and
reported by an operator as "it expanded the wrong thing".
"""
from __future__ import annotations

import fcntl
import struct
import termios
from typing import Iterable, Tuple

__all__ = [
    "CTRL",
    "SPECIAL",
    "key",
    "keys",
    "mouse_click",
    "mouse_drag",
    "mouse_press",
    "mouse_release",
    "mouse_scroll",
    "set_winsize",
]

#: Control chords. `ctrl+<letter>` is the letter's ordinal minus 64 — the
#: ASCII control range — which is why Ctrl+I is indistinguishable from Tab
#: and Ctrl+M from Enter at the byte level. Callers get told rather than
#: silently surprised: see `key`.
CTRL = {c: bytes([ord(c.upper()) - 64]) for c in "abcdefghijklmnopqrstuvwxyz"}
CTRL["_"] = b"\x1f"          # chat:undo
CTRL["["] = b"\x1b"          # == Escape

#: Keys with no printable form. Arrow/Home/End use the CSI forms a terminal
#: in "application cursor" mode does not send — prompt_toolkit accepts both,
#: and the normal forms are what a plain terminal emits.
SPECIAL = {
    "escape": b"\x1b",
    "enter": b"\r",
    "tab": b"\t",
    "backspace": b"\x7f",
    "space": b" ",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
}

#: Aliases a terminal cannot distinguish, named so a test that relies on the
#: collision says so out loud instead of appearing to test something else.
AMBIGUOUS = {"ctrl+i": "tab", "ctrl+m": "enter", "ctrl+[": "escape"}


def key(name: str) -> bytes:
    """``"ctrl+o"`` -> ``b"\\x0f"``. Raises on an unknown key.

    Raising rather than returning empty bytes is deliberate: a typo that sent
    NOTHING would make the assertion fail for a reason that has nothing to do
    with the behaviour under test, and the operator-visible symptom of that is
    an afternoon spent debugging a working feature.
    """
    raw = str(name or "").strip()
    if len(raw) == 1:
        # A single character IS itself, case included — `G` and `g` are
        # different bindings in the transcript viewer and lowercasing here
        # would silently send the wrong one.
        return raw.encode()
    token = raw.lower()
    if not token:
        raise ValueError("empty key")
    if token in SPECIAL:
        return SPECIAL[token]
    if token.startswith("ctrl+"):
        rest = token[5:]
        if rest not in CTRL:
            raise ValueError(f"no control encoding for {name!r}")
        return CTRL[rest]
    if len(token) == 1:
        return token.encode()
    if len(name) == 1:                     # preserve case for `G` vs `g`
        return name.encode()
    raise ValueError(f"unknown key {name!r}")


def keys(*names: str) -> bytes:
    """Several keystrokes as one write — a chord, or a typed word."""
    out = b""
    for n in names:
        out += key(n) if len(n) != 1 else n.encode()
    return out


# ---------------------------------------------------------------------------
# mouse — SGR (1006) encoding
# ---------------------------------------------------------------------------

_BTN = {"left": 0, "middle": 1, "right": 2, "scroll_up": 64, "scroll_down": 65}
_MOD = {"shift": 4, "alt": 8, "ctrl": 16}


def _sgr(button: int, col: int, row: int, *, press: bool,
         modifiers: Iterable[str] = ()) -> bytes:
    """``ESC [ < Cb ; Cx ; Cy (M|m)`` — press is ``M``, release is ``m``.

    Coordinates arrive 0-based (the application's frame) and go out 1-based
    (the wire's). One conversion, one place.
    """
    code = int(button)
    for m in modifiers or ():
        code |= _MOD[str(m).lower()]
    tail = "M" if press else "m"
    return f"\x1b[<{code};{int(col) + 1};{int(row) + 1}{tail}".encode()


def mouse_press(col: int, row: int, *, button: str = "left",
                modifiers: Iterable[str] = ()) -> bytes:
    return _sgr(_BTN[button], col, row, press=True, modifiers=modifiers)


def mouse_release(col: int, row: int, *, button: str = "left",
                  modifiers: Iterable[str] = ()) -> bytes:
    return _sgr(_BTN[button], col, row, press=False, modifiers=modifiers)


def mouse_click(col: int, row: int, *, button: str = "left",
                modifiers: Iterable[str] = ()) -> bytes:
    """A press AND a release in the same cell — which is what a click IS.

    Sending only the press is the single easiest way to write a mouse test
    that proves nothing: the cockpit deliberately acts on MOUSE_UP, because
    acting on the press fires while the operator is still deciding.
    """
    return (mouse_press(col, row, button=button, modifiers=modifiers)
            + mouse_release(col, row, button=button, modifiers=modifiers))


def mouse_drag(start: Tuple[int, int], end: Tuple[int, int], *,
               steps: int = 3) -> bytes:
    """Press, MOVE while held, release — a real drag.

    The intermediate moves carry bit 32 (motion) on top of the button, which
    is how a terminal reports "still held, now here". Without them the
    application sees a press and a release in two places and no drag at all,
    which is exactly what a selection model must distinguish from a click.
    """
    (c0, r0), (c1, r1) = start, end
    out = mouse_press(c0, r0)
    steps = max(1, int(steps))
    for i in range(1, steps + 1):
        c = c0 + (c1 - c0) * i // steps
        r = r0 + (r1 - r0) * i // steps
        out += _sgr(_BTN["left"] | 32, c, r, press=True)
    out += mouse_release(c1, r1)
    return out


def mouse_scroll(col: int, row: int, *, up: bool = True, times: int = 1) -> bytes:
    """Wheel notches. Reported as button 64/65 with a PRESS and no release."""
    btn = _BTN["scroll_up" if up else "scroll_down"]
    return b"".join(_sgr(btn, col, row, press=True)
                    for _ in range(max(1, int(times))))


# ---------------------------------------------------------------------------
# window size
# ---------------------------------------------------------------------------


def set_winsize(fd: int, cols: int, rows: int) -> bool:
    """Tell the child how big its terminal is. NEVER raises; True on success.

    Without this a pty defaults to 0x0 on some platforms, and a TUI handed
    zero rows either draws nothing or divides by zero deep in a layout — a
    failure that looks like the feature being broken rather than the harness
    never having said how big the screen is.

    Also delivers SIGWINCH to the child, which is the same path a real resize
    takes, so the cockpit's own resize handling is exercised rather than
    bypassed.
    """
    try:
        packed = struct.pack("HHHH", int(rows), int(cols), 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
        return True
    except Exception:  # noqa: BLE001
        return False
