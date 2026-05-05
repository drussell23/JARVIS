"""NarrativeRenderer — Rich rendering of NarrativeFrame with strict
visual hierarchy.
========================================================================

Slice 3 of the **Gap #6 closure arc**.

User-facing contract — three constraints
-----------------------------------------

The operator's brief for this slice was explicit:

  1. **Strict Visual Hierarchy** — model voice (💭 frames) MUST be
     visually distinct from system actions (`⏺` tool headers,
     phase markers). Use dim, italicized text with a subtle gray-
     blue tint so the operator's eye separates "what the model is
     saying" from "what O+V is doing".

  2. **Tool Transparency** — every tool call MUST narrate its WHY.
     When the model omits a preamble, Slice 2's
     :func:`tool_preamble_synthesizer.synthesize_preamble` produces
     a deterministic fallback. This module renders both kinds
     identically (same glyph, same style) so operators can't tell
     which were synthesized.

  3. **No Clutter** — streaming prose must flow cleanly:
     * Indent matches the active op-block side-rail (``  │  ``)
     * Wrapped lines respect the indent (no orphaned column-1 text)
     * Coexists with ``patch_stdout`` for background sensor prints
     * Never breaks the op-block borders

Architectural reuse
-------------------

* :class:`NarrativeFrame` (Slice 1) — input record
* :class:`NarrativeKind` (Slice 1) — drives glyph + tint dispatch
* :func:`tool_preamble_synthesizer.synthesize_preamble` (Slice 2)
  — fallback narration source
* House style: pure substrate (no Console import top-level), Rich
  style strings emitted as plain text the caller wraps. Mirrors the
  Gap #2 ``ToolRenderRegistry`` → ``ToolRenderView`` separation.

Authority boundary
------------------

* §1 deterministic — pure formatting; the prose IS model output but
  we only style it
* §7 fail-closed — every formatter degrades to a generic dim italic
  line on pathological input; NEVER raises into the render path
* §8 observable — the closed glyph/tint dispatch table is the source
  of truth (Slice 5 AST pin verifies it covers every NarrativeKind)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from backend.core.ouroboros.battle_test.narrative_channel import (
    NarrativeFrame,
    NarrativeKind,
)

logger = logging.getLogger("Ouroboros.NarrativeRenderer")


# ===========================================================================
# Schema
# ===========================================================================


NARRATIVE_RENDERER_SCHEMA_VERSION: str = "narrative_renderer.v1"


# ===========================================================================
# Visual hierarchy table — closed enum dispatch (Constraint 1)
# ===========================================================================
#
# Each NarrativeKind maps to ONE glyph + ONE tint. Closed table —
# adding a new NarrativeKind requires updating this table AND the
# AST pin in Slice 5 catches missed entries. Operators can rely on
# glyphs as a stable visual vocabulary.


@dataclass(frozen=True)
class FrameStyle:
    """Per-kind visual style.

    Fields
    ------
    * ``glyph`` — the leading visual marker (``💭``, ``🗣``, ``🤔``,
      ``🔧``, ``💀``).
    * ``tint`` — Rich color name. Slice 3 uses ``"bright_black"``
      family (gray-blue) for model voice — distinct from the cyan
      used for system actions in ``serpent_flow.py:_C['neural']``.
    * ``italic`` — emphasizes "this is the model speaking", not a
      system event.
    """

    glyph: str
    tint: str
    italic: bool = True
    schema_version: str = NARRATIVE_RENDERER_SCHEMA_VERSION


_KIND_STYLES: Mapping[NarrativeKind, FrameStyle] = {
    NarrativeKind.INTENT: FrameStyle(
        glyph="💭", tint="bright_blue",
    ),
    NarrativeKind.PLAN_PROSE: FrameStyle(
        glyph="💭", tint="bright_blue",
    ),
    NarrativeKind.TOOL_PREAMBLE: FrameStyle(
        glyph="🗣", tint="bright_black",  # gray — quieter than INTENT
    ),
    NarrativeKind.THINKING: FrameStyle(
        glyph="🤔", tint="bright_black",
    ),
    NarrativeKind.L2_REPAIR_PROSE: FrameStyle(
        glyph="🔧", tint="yellow",  # repair narrative gets a different
                                    # tint — operator should notice repair
    ),
    NarrativeKind.POSTMORTEM_PROSE: FrameStyle(
        glyph="💀", tint="red",     # dim italic still applies via global
                                    # ``italic=True``; red signals failure
    ),
}


_FALLBACK_STYLE: FrameStyle = FrameStyle(
    glyph="💭", tint="bright_black",
)


def get_style(kind: object) -> FrameStyle:
    """Resolve a :class:`FrameStyle` for a kind. Falls back gracefully
    on unknowns (forward-compat with future kinds added without
    updating this table)."""
    if isinstance(kind, NarrativeKind):
        return _KIND_STYLES.get(kind, _FALLBACK_STYLE)
    return _FALLBACK_STYLE


# ===========================================================================
# Frozen render output — caller emits via Console.print
# ===========================================================================


@dataclass(frozen=True)
class RenderedFrame:
    """Structured render output. The caller hands ``markup`` to
    ``Console.print(..., highlight=False)``.

    Fields
    ------
    * ``markup`` — Rich markup string (one or more lines, joined with
      newlines). Indented and styled.
    * ``glyph`` — for tests / observability.
    * ``style_str`` — the Rich style applied to the prose body.
    """

    markup: str
    glyph: str
    style_str: str
    schema_version: str = NARRATIVE_RENDERER_SCHEMA_VERSION


# ===========================================================================
# Helpers
# ===========================================================================


def _safe_str(raw: object) -> str:
    if raw is None:
        return ""
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return ""


def _escape_rich(text: str) -> str:
    """Escape ``[`` so Rich treats raw text as content not markup. The
    caller's markup brackets are added in :func:`compose` AFTER escape."""
    return text.replace("[", "\\[")


def _build_indent(*, op_active: bool) -> str:
    """Indent helper enforcing **Constraint 3 (No Clutter)**:

      * Inside an active op block → match the side-rail: ``  │  ``
      * Outside (system-level narrative) → flush left with 2-space pad

    Mirrors :meth:`SerpentFlow._op_line` exactly so the rendered line
    visually nests under the active op without breaking the border.
    """
    if op_active:
        return "  │  "
    return "  "


# ===========================================================================
# Compose — the load-bearing function
# ===========================================================================


def compose(
    frame: NarrativeFrame,
    *,
    op_active: bool = True,
    max_chars_per_line: int = 0,
) -> RenderedFrame:
    """Render a :class:`NarrativeFrame` to Rich-markup string ready for
    ``Console.print``.

    Parameters
    ----------
    frame :
        The narrative frame. Lookup the visual style via
        :func:`get_style`.
    op_active :
        ``True`` when the frame's ``op_id`` is currently active in the
        operator's view (drives indent — Constraint 3).
    max_chars_per_line :
        ``0`` (default) means single-line emission. When > 0, the
        prose is wrapped into multiple lines respecting the indent
        — useful for streaming long PLAN_PROSE frames.

    NEVER raises. Pathological frames degrade to the fallback style
    with the raw prose text.
    """
    if not isinstance(frame, NarrativeFrame):
        return RenderedFrame(
            markup="", glyph="", style_str="",
        )

    style = get_style(frame.kind)
    indent = _build_indent(op_active=op_active)

    # Build the Rich style string. Closed-enum dispatch keeps the style
    # composition deterministic — operators see the same shape every time.
    style_parts = [style.tint]
    if style.italic:
        style_parts.append("italic")
    style_str = " ".join(style_parts)

    prose_safe = _safe_str(frame.prose).strip()
    if not prose_safe:
        # DISCARDED / empty BUFFERING frames render nothing — operator
        # sees no clutter for failed model emissions.
        return RenderedFrame(
            markup="", glyph=style.glyph, style_str=style_str,
        )

    escaped = _escape_rich(prose_safe)

    # Line wrapping (only when caller asks). The opening glyph is on
    # the FIRST line; wrapped continuations indent under it via a
    # zero-width space-equivalent so the prose visually flows.
    if max_chars_per_line > 0:
        lines = _wrap_prose(escaped, max_chars_per_line)
    else:
        lines = [escaped]

    out_lines = []
    for i, line in enumerate(lines):
        if i == 0:
            out_lines.append(
                f"{indent}[{style_str}]{style.glyph} {line}[/{style_str}]"
            )
        else:
            # Continuation lines: indent past the glyph (2 chars +
            # space) so prose stays aligned visually.
            cont_indent = f"{indent}   "
            out_lines.append(
                f"{cont_indent}[{style_str}]{line}[/{style_str}]"
            )

    return RenderedFrame(
        markup="\n".join(out_lines),
        glyph=style.glyph,
        style_str=style_str,
    )


def _wrap_prose(text: str, max_chars: int) -> list:
    """Word-wrap ``text`` to lines no longer than ``max_chars``.

    Pure-stdlib wrapping — no ``textwrap`` (its hyphen handling is
    surprising for code-flavored text). Splits on whitespace; long
    individual tokens are emitted on their own line.
    """
    if max_chars <= 0:
        return [text]
    out: list = []
    current = ""
    for word in text.split():
        if not current:
            current = word
            continue
        if len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            out.append(current)
            current = word
    if current:
        out.append(current)
    return out or [text]


# ===========================================================================
# Convenience: emit directly to a Rich Console
# ===========================================================================


def render_to_console(
    frame: NarrativeFrame,
    console: object,
    *,
    op_active: bool = True,
    max_chars_per_line: int = 0,
) -> bool:
    """Compose + emit. Returns ``True`` on success, ``False`` if the
    frame produced no markup or the console raised.

    The caller passes a Rich :class:`Console` instance; we
    feature-detect ``.print`` rather than importing Rich at module
    top so headless / no-Rich contexts can still import this module.

    NEVER raises.
    """
    rendered = compose(
        frame,
        op_active=op_active,
        max_chars_per_line=max_chars_per_line,
    )
    if not rendered.markup:
        return False
    print_fn = getattr(console, "print", None)
    if not callable(print_fn):
        return False
    try:
        print_fn(rendered.markup, highlight=False)
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[NarrativeRenderer] console.print raised; swallowing",
            exc_info=True,
        )
        return False


__all__ = [
    "FrameStyle",
    "NARRATIVE_RENDERER_SCHEMA_VERSION",
    "RenderedFrame",
    "compose",
    "get_style",
    "render_to_console",
]
