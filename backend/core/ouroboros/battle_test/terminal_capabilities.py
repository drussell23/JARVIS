"""The process holding the paintbrush cannot see the canvas.

`ov` splits rendering from display: SerpentFlow, StreamRenderer, the diff
formatters and `print_fit` all run in the DAEMON, while the terminal that
shows their output belongs to an attached `ov` client on the other side of a
UDS. The daemon therefore formats tables, wraps diffs, and computes the
``⏺``/``⎿`` gutter with **no knowledge of how wide the receiving terminal
is** — a grep for ``width`` / ``COLUMNS`` / ``get_terminal_size`` / ``SIGWINCH``
across `cockpit_attach.py` and `attach_session.py` returns nothing.

Three symptoms, one cause
-------------------------
Width, theme and unicode-width were three separate complaints and are one
defect: the renderer is blind to the display. So this is one channel carrying
the display's self-description, not three patches.

  * **Width** — a 200-column diff wraps into mush at 80; an 80-column table
    wastes half a wide terminal.
  * **Theme** — `chrome_color()` reserves green for outcomes, a decision made
    daemon-side against an assumed dark background.
  * **Unicode width** — the daemon computes column counts for emoji and CJK.
    Get it wrong and every gutter below that line is misaligned. Invisible in
    an ASCII test suite; obvious the first time a filename carries an emoji.

The ambient problem, and why MINIMUM is the honest answer
---------------------------------------------------------
Output is either ADDRESSED (the cockpit that ran a verb — `session_scope`
carries its id) or AMBIENT (a Moltbook post, an autonomous op — everyone sees
it). For addressed output the answer is easy: use that subscriber's width.

For ambient output with two cockpits at different widths there is **no width
that is correct for both**. Rendering to the widest guarantees the narrow one
wraps; rendering to the narrowest leaves margin on the wide one. Margin is a
cosmetic loss; wrapping destroys the gutter alignment that makes the flow
readable at all. So ambient renders to the MINIMUM across live subscribers —
an asymmetric choice, made deliberately, for the same reason the visual-verify
battery clamps a pass to a fail: the two errors are not equally bad.

Everything here is a READ model over what subscribers declare. It stores no
terminal state of its own, opens nothing, and never blocks — a capability
lookup sits on the render path, and a render path that can stall is a cockpit
that can hang.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.TermCaps")

__all__ = [
    "TerminalCapabilities",
    "declare",
    "forget",
    "capabilities_for",
    "current_capabilities",
    "effective_width",
    "effective_theme",
    "supports_wide_glyphs",
    "snapshot",
]


def _env_int(name: str, fallback: int) -> int:
    """Bounds come from the environment, never from a literal in a branch."""
    try:
        return int(os.environ.get(name, "").strip() or fallback)
    except (TypeError, ValueError):
        return fallback


def _fallback_cols() -> int:
    """Width to assume when NOTHING has declared one.

    Not a hardcoded 80: it reads the daemon's own terminal when it has one
    (a locally-run SerpentFlow does), then `COLUMNS`, then the env knob. The
    literal is the last resort, not the first answer.
    """
    try:
        cols = int(os.environ.get("JARVIS_COCKPIT_FALLBACK_COLS", "").strip() or 0)
        if cols > 0:
            return cols
    except (TypeError, ValueError):
        pass
    try:
        import shutil
        size = shutil.get_terminal_size(fallback=(0, 0))
        if size.columns > 0:
            return int(size.columns)
    except Exception:  # noqa: BLE001
        pass
    return _env_int("COLUMNS", 80)


#: Clamps. A subscriber declaring 5 columns or 100_000 is either broken or
#: hostile; either way the renderer must not honour it literally.
def _min_cols() -> int:
    return max(1, _env_int("JARVIS_COCKPIT_MIN_COLS", 20))


def _max_cols() -> int:
    return max(_min_cols(), _env_int("JARVIS_COCKPIT_MAX_COLS", 400))


@dataclass(frozen=True)
class TerminalCapabilities:
    """One display's self-description. Frozen: a subscriber redeclaring
    produces a NEW record, so a half-applied resize can never be observed."""

    cols: int
    rows: int
    #: "dark" / "light" / "unknown" — never guessed. A terminal that does not
    #: answer the OSC 11 background query says so, and the renderer keeps its
    #: theme-agnostic path rather than betting on dark.
    theme: str = "unknown"
    #: True when the client's terminal renders emoji/CJK at DOUBLE width the
    #: way `wcwidth` predicts. Terminals disagree about this and the only
    #: honest source is the client.
    wide_glyphs: bool = True
    #: 1 / 8 / 256 / 16_777_216, or 0 for "did not say".
    color_depth: int = 0

    def clamped(self) -> "TerminalCapabilities":
        lo, hi = _min_cols(), _max_cols()
        return replace(
            self,
            cols=max(lo, min(hi, int(self.cols or 0) or _fallback_cols())),
            rows=max(1, int(self.rows or 0) or 24),
        )

    @classmethod
    def from_wire(cls, msg: Mapping[str, Any]) -> Optional["TerminalCapabilities"]:
        """Parse a ``{"type":"caps", ...}`` frame. Returns None on anything
        unusable rather than raising — a malformed frame from one cockpit must
        never disturb the others."""
        try:
            cols = int(msg.get("cols") or 0)
            rows = int(msg.get("rows") or 0)
            if cols <= 0 and rows <= 0:
                return None
            theme = str(msg.get("theme") or "unknown").strip().lower()
            if theme not in ("dark", "light", "unknown"):
                theme = "unknown"
            return cls(
                cols=cols or _fallback_cols(),
                rows=rows or 24,
                theme=theme,
                wide_glyphs=bool(msg.get("wide_glyphs", True)),
                color_depth=int(msg.get("color_depth") or 0),
            ).clamped()
        except Exception:  # noqa: BLE001
            return None


# A plain dict under a lock, not a registry class: the access pattern is
# read-mostly from render paths and written once per attach/resize. Same shape
# as `_DEFAULT_INTAKE_ROUTER` — the daemon installs, consumers ask.
_LOCK = threading.RLock()
_CAPS: Dict[str, TerminalCapabilities] = {}


def declare(session_id: Optional[str], caps: TerminalCapabilities) -> None:
    """Record (or replace) one subscriber's declaration. NEVER raises."""
    try:
        if not session_id or caps is None:
            return
        with _LOCK:
            _CAPS[str(session_id)] = caps.clamped()
        logger.debug(
            "[TermCaps] session=%s cols=%d rows=%d theme=%s wide=%s",
            session_id, caps.cols, caps.rows, caps.theme, caps.wide_glyphs,
        )
    except Exception:  # noqa: BLE001
        pass


def forget(session_id: Optional[str]) -> None:
    """Drop a departed subscriber. A stale narrow cockpit that has gone away
    must stop constraining the ambient minimum — otherwise one dead 40-column
    terminal squeezes every future render forever."""
    try:
        if not session_id:
            return
        with _LOCK:
            _CAPS.pop(str(session_id), None)
    except Exception:  # noqa: BLE001
        pass


def capabilities_for(session_id: Optional[str]) -> Optional[TerminalCapabilities]:
    try:
        if not session_id:
            return None
        with _LOCK:
            return _CAPS.get(str(session_id))
    except Exception:  # noqa: BLE001
        return None


def _ambient() -> Optional[TerminalCapabilities]:
    """The composite for output nobody addressed.

    MINIMUM width across live subscribers, because wrapping is a worse failure
    than margin. Theme collapses to "unknown" unless every subscriber agrees —
    a renderer must not colour for dark backgrounds because one of three
    cockpits is dark. `wide_glyphs` is the AND: if any terminal renders emoji
    narrow, the safe assumption for a shared line is narrow.
    """
    try:
        with _LOCK:
            live = list(_CAPS.values())
        if not live:
            return None
        cols = min(c.cols for c in live)
        rows = min(c.rows for c in live)
        themes = {c.theme for c in live}
        theme = themes.pop() if len(themes) == 1 else "unknown"
        return TerminalCapabilities(
            cols=cols, rows=rows, theme=theme,
            wide_glyphs=all(c.wide_glyphs for c in live),
            color_depth=min((c.color_depth for c in live), default=0),
        ).clamped()
    except Exception:  # noqa: BLE001
        return None


def current_capabilities() -> Optional[TerminalCapabilities]:
    """Capabilities for whoever this render is FOR. NEVER raises.

    Reads the ambient/addressed distinction the cockpit already maintains via
    `attach_session.current_session()` — the same ContextVar that decides
    whether output is broadcast or directed. No renderer has to thread a
    parameter, and no second notion of "who is this for" gets invented.
    """
    try:
        from backend.core.ouroboros.battle_test.attach_session import (
            current_session,
        )
        sid = current_session()
    except Exception:  # noqa: BLE001
        sid = None
    if sid:
        caps = capabilities_for(sid)
        if caps is not None:
            return caps
        # An addressed session that never declared: fall through to the
        # composite rather than to a literal. A cockpit on an old client
        # build still gets a width derived from its peers.
    return _ambient()


def effective_width(default: Optional[int] = None) -> int:
    """THE accessor a renderer calls. Always returns a usable positive width.

    Resolution order — measured, then composite, then the caller's hint, then
    the daemon's own terminal, then `COLUMNS`. Each step is a real observation
    before the next; the literal only appears when every observation failed.
    """
    caps = current_capabilities()
    if caps is not None and caps.cols > 0:
        return caps.cols
    if default and int(default) > 0:
        return int(default)
    return _fallback_cols()


def effective_theme() -> str:
    """"dark" / "light" / "unknown". Callers MUST treat unknown as
    "use the theme-agnostic path", never as a synonym for dark — guessing is
    how a light-terminal operator ends up reading grey on white."""
    caps = current_capabilities()
    return caps.theme if caps is not None else "unknown"


def supports_wide_glyphs() -> bool:
    """False → prefer ASCII-safe chrome for THIS render. Conservative when
    nothing is known: an aligned ASCII gutter beats a misaligned pretty one."""
    caps = current_capabilities()
    return bool(caps.wide_glyphs) if caps is not None else True


def snapshot() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(per-session, composite) — for `/system` and tests. NEVER raises."""
    try:
        with _LOCK:
            per = {
                sid: {
                    "cols": c.cols, "rows": c.rows, "theme": c.theme,
                    "wide_glyphs": c.wide_glyphs, "color_depth": c.color_depth,
                }
                for sid, c in _CAPS.items()
            }
        amb = _ambient()
        composite = (
            {"cols": amb.cols, "rows": amb.rows, "theme": amb.theme,
             "wide_glyphs": amb.wide_glyphs}
            if amb is not None else {}
        )
        return per, composite
    except Exception:  # noqa: BLE001
        return {}, {}
