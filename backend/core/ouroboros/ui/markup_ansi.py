"""Resolve Rich markup for surfaces that cannot parse it.

Two consumers render the operator's lines, and neither speaks Rich markup:

* the **cockpit attach bridge**, whose ``line`` frames the client prints
  verbatim — markup sent there arrives as literal ``[cyan]…[/cyan]``;
* **prompt_toolkit**, which draws the bottom toolbar and has its own
  formatting types (``ANSI`` / ``HTML`` / fragment lists). A plain string
  handed to it is printed exactly as given.

Both were being fed strings composed with ``[{_SEM['neural']}]…[/…]``, and both
showed the tags. Measured from a live cockpit:

    [cyan]qwen3-coder-ov:30b[/cyan] · voice: off ('wake') · 'detach' to leave
    [dim]· Iterating… (4s · ↓ 902 tokens)[/dim]

The composition layers are right to emit markup — it is the design language,
and rewriting them to emit ANSI would push terminal concerns up into every
producer. What was missing is a translator at the boundary, and there was one
copy of it living inside the harness. This is that copy, extracted, so the
daemon and the client cannot drift in how they render the same line.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("Ouroboros.MarkupAnsi")

__all__ = ["markup_to_ansi", "markup_to_plain", "toolbar_fragments"]


def markup_to_ansi(
    msg: Any,
    *,
    console: Any = None,
    width: Optional[int] = None,
) -> str:
    """Rich markup → a string of ANSI escapes. NEVER raises.

    Capabilities are MIRRORED from *console* rather than declared, so the
    rendered line matches the surface it is bound for. ``ABSENT`` and ``None``
    are different answers: ``color_system=None`` is a console SAYING it has no
    colour, while a missing attribute is no console to ask — collapsing them
    forces escape codes into a dumb terminal.

    ``width=None`` with soft wrapping means the RECEIVING terminal decides
    where lines break. Imposing the composing side's width is how a daemon
    with no TTY (Rich defaults to 80 columns) pins a 140-column cockpit to 80.
    """
    try:
        from io import StringIO

        from rich.console import Console

        if console is None:
            force_terminal, color_system = True, "truecolor"
            eff_width = width
        else:
            force_terminal = bool(getattr(console, "is_terminal", True))
            color_system = getattr(console, "color_system", "truecolor")
            eff_width = width if width is not None else (
                int(getattr(console, "width", 0) or 0) or None
            )
        buf = StringIO()
        out = Console(
            file=buf,
            force_terminal=force_terminal,
            color_system=color_system,
            width=eff_width,
            markup=True,
            highlight=False,
            soft_wrap=True,
            legacy_windows=False,
        )
        # A scratch Console has no theme, so a design-language TOKEN
        # (`muted`, `accent`, …) resolved to nothing and the styling vanished
        # silently. `spooled_console` hit the same trap; the theme's own
        # idempotent helper is the fix there and here.
        from backend.core.ouroboros.ui.theme import ensure_theme
        ensure_theme(out)
        out.print(msg, end="")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — styling is never worth losing the line
        return markup_to_plain(msg)


def markup_to_plain(msg: Any) -> str:
    """Rich markup → its visible characters, styling discarded. NEVER raises.

    Uses Rich's own parser rather than a regex: markup nesting, escaped
    brackets and malformed tags all have defined behaviour there, and a
    hand-rolled stripper gets one of them wrong on the line that matters.
    """
    try:
        from rich.markup import render

        return render(str(msg)).plain
    except Exception:  # noqa: BLE001
        return str(msg)


def toolbar_fragments(msg: Any, *, console: Any = None) -> Any:
    """Rich markup → something prompt_toolkit will RENDER rather than print.

    prompt_toolkit accepts a plain string, and a plain string is exactly what
    it printed the markup as. ``ANSI`` is its own documented type for
    pre-escaped text, so the translation lands in the type system rather than
    in a convention someone has to remember.

    Degrades to PLAIN text — never to raw markup — when prompt_toolkit is
    unavailable or the escape wrapper fails. A toolbar without colour is
    legible; a toolbar full of tags is the bug this exists to remove.
    """
    try:
        from prompt_toolkit.formatted_text import ANSI

        # No width: the toolbar is drawn by prompt_toolkit, which owns the
        # terminal geometry and will clip or pad to the real column count.
        return ANSI(markup_to_ansi(msg, console=console, width=None))
    except Exception:  # noqa: BLE001
        logger.debug("[MarkupAnsi] toolbar fragment degraded", exc_info=True)
        return markup_to_plain(msg)
