"""Saying what a surface did NOT receive.

Two bounded buffers feed an attached cockpit and both drop under load:

  * :class:`~battle_test.spooled_console.ConsoleSpooler` drops whole LINES
    when a cockpit stops reading;
  * :class:`~battle_test.stream_mirror.StreamMirror` drops CHARACTERS from
    the front of the token buffer when frames outrun the socket.

Both were counting. Neither was telling. A bounded queue that silently
discards is correct engineering and dishonest reporting: the operator reads a
continuous transcript with holes in it and has no way to know a hole is
there.

The rule is the same for both, so it lives once here rather than twice in
them. It is also the same rule the SSE broker already applies — one
``stream_lag`` per window, not one per dropped frame — so all three
backpressure surfaces say the same thing the same way.

Coalesced, not per-drop
-----------------------
Under sustained load a per-drop notice becomes the thing crowding the queue:
the surface spends its remaining capacity announcing that it has no capacity.
So a caller reports the DELTA since it last spoke, and only when that delta
is positive.

Pure and total. No I/O, no clock, no state of its own — the caller owns the
watermark, because the caller owns the buffer. NEVER raises: a notice that
can fail is a notice that turns a dropped line into a dropped stream.
"""
from __future__ import annotations

from typing import Optional, Tuple

__all__ = ["coalesced_drop_notice", "DROP_GLYPH"]

#: The op-chrome continuation glyph. A gap is something that happened to the
#: work, so it renders as work chrome rather than as prose — the same
#: ``⎿`` an operator already reads as "and then this".
DROP_GLYPH = "⎿"


def coalesced_drop_notice(
    dropped_total: int,
    reported_total: int,
    *,
    unit: str = "line",
    detail: str = "",
) -> Tuple[Optional[str], int]:
    """One notice for everything lost since the caller last spoke.

    Returns ``(notice_or_None, new_reported_total)``. The caller stores the
    second value and passes it back next time; the watermark lives with the
    buffer that owns the drops.

    ``unit`` names what was lost in the surface's own terms — a console loses
    lines, a token mirror loses characters. Reporting "12 lines" for
    characters would be precise about a quantity and wrong about the thing,
    which is the failure mode this whole surface exists to prevent.

    ``detail`` appends a surface-specific clause (where the full record
    lives, what to do). Empty is fine; the notice stands without it.
    """
    try:
        total = int(dropped_total)
        seen = int(reported_total)
    except (TypeError, ValueError):
        return None, reported_total

    if total <= seen:
        return None, seen              # nothing new to confess

    missed = total - seen
    noun = str(unit or "item")
    plural = noun if missed == 1 else f"{noun}s"
    tail = f" — {detail}" if detail else ""
    return (
        f"[dim]{DROP_GLYPH} {missed} {plural} dropped{tail}[/dim]",
        total,
    )
