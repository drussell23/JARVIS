"""Park a half-written goal, do something else, get it back.

The prompt accepts paragraphs now. That makes it worth interrupting: an
operator two hundred characters into describing a repair notices they need to
check `/posture` first, and their only options are to lose the draft or to
finish it blind. Both are bad enough that people stop writing long goals,
which quietly undoes multi-line input.

`Ctrl+S` swaps the buffer with a held draft. Press it with text and the
buffer empties, holding what was there; press it again with an empty buffer
and the draft comes back, cursor where it was.

A SWAP, not a stack
-------------------
The obvious design is a stack — stash, stash again, pop twice. It is wrong
here for a reason that only shows up in use: a stack has no visible depth in
a one-line prompt, so the operator cannot tell whether the next press returns
the thing they want or something from ten minutes ago. A single slot is a
thing you can hold in your head.

The cost is that a second stash would overwrite the first, and silently
destroying a draft is exactly what this exists to prevent. So it does not:
stashing while the slot is occupied SWAPS them. Nothing is ever lost, and the
operator gets the other draft rather than an error.

Cursor position travels with the text
-------------------------------------
Restoring a paragraph with the caret at position 0 means hunting for where
you were. The offset is part of the draft, clamped on restore because the
buffer it returns to may not be the one it left.

Not persisted, deliberately
---------------------------
A stash that survived a restart would be a second history with different
rules — and history already exists, is durable, and is what "I want this
back tomorrow" means. This is for the next ninety seconds.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger("Ouroboros.DraftStash")

__all__ = ["DraftStash", "stash_enabled", "install_stash_binding"]


def stash_enabled() -> bool:
    """Default ON. Off, Ctrl+S is left to the terminal."""
    return os.environ.get(
        "JARVIS_DRAFT_STASH_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


class DraftStash:
    """One held draft, swapped in and out of a buffer."""

    def __init__(self) -> None:
        self._text: Optional[str] = None
        self._cursor: int = 0
        self.swaps = 0

    @property
    def holding(self) -> bool:
        return self._text is not None

    @property
    def preview(self) -> str:
        """First line of what is held, for a toolbar hint."""
        if not self._text:
            return ""
        first = self._text.strip().splitlines()[0] if self._text.strip() else ""
        return first[:40] + ("…" if len(first) > 40 else "")

    def toggle(self, text: str, cursor: int = 0) -> Tuple[str, int]:
        """Swap the buffer with the slot. Returns ``(new_text, new_cursor)``.

        Four cases, and all of them have to be non-destructive:

        * text, empty slot   → park it, buffer clears
        * empty, full slot   → restore it, slot clears
        * text, full slot    → SWAP. Never overwrite: silently destroying a
                               draft is what this exists to prevent
        * empty, empty slot  → nothing to do, and no error either
        """
        try:
            incoming = str(text or "")
            held, held_cursor = self._text, self._cursor
            if incoming.strip():
                self._text, self._cursor = incoming, max(0, int(cursor))
                self.swaps += 1
                if held is None:
                    return "", 0
                return held, min(held_cursor, len(held))
            if held is None:
                return incoming, max(0, int(cursor))
            self._text, self._cursor = None, 0
            self.swaps += 1
            # Clamped: the buffer it returns to may not be the one it left.
            return held, min(held_cursor, len(held))
        except Exception:  # noqa: BLE001 — a stash must never eat a draft
            logger.debug("[DraftStash] toggle degraded", exc_info=True)
            return str(text or ""), max(0, int(cursor or 0))

    def clear(self) -> None:
        self._text, self._cursor = None, 0


def install_stash_binding(kb: Any, get_buffer: Any,
                          stash: Optional[DraftStash] = None,
                          notify: Optional[Any] = None) -> Optional[DraftStash]:
    """Bind Ctrl+S to the swap. Returns the stash, or None. NEVER raises.

    Ctrl+S is XOFF under a cooked terminal and would freeze output rather
    than reach the application — but prompt_toolkit puts the input in raw
    mode with `IXON` cleared, so the key arrives. Verified against the
    library rather than assumed, because the failure mode is a terminal that
    appears to hang.
    """
    try:
        if kb is None or not stash_enabled():
            return None
        slot = stash or DraftStash()

        @kb.add("c-s")
        def _swap(event: Any) -> None:
            try:
                buf = get_buffer()
                if buf is None:
                    return
                text, cursor = slot.toggle(buf.text, buf.cursor_position)
                buf.text = text
                buf.cursor_position = min(cursor, len(text))
                if notify is not None:
                    notify(slot)
            except Exception:  # noqa: BLE001
                logger.debug("[DraftStash] swap degraded", exc_info=True)

        return slot
    except Exception:  # noqa: BLE001
        logger.debug("[DraftStash] install degraded", exc_info=True)
        return None
