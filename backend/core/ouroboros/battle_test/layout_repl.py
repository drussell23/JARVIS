"""
/layout REPL dispatcher — Slice 3 of the opt-in split layout arc.
==================================================================

Operator verbs for changing the SerpentFlow presentation mode at
runtime:

    /layout                  show current mode + valid targets
    /layout flow             switch to flowing (default) mode
    /layout split            switch to 3-pane split mode
    /layout focus <region>   focus one of stream / dashboard / diff
    /layout help             print the verb surface

Every verb is operator-driven (§1); the controller is a state
machine — no model path reaches here. Single-keystroke escape
back to flow is ``/layout flow`` regardless of current state.
"""
from __future__ import annotations

import shlex
import textwrap
from dataclasses import dataclass
from typing import Optional

from backend.core.ouroboros.battle_test.layout_controller import (
    LayoutController,
    LayoutError,
    MODE_FLOW,
    MODE_SPLIT,
    focus_region,
    get_default_layout_controller,
    is_focus_mode,
    valid_regions,
)


_COMMANDS = frozenset({"/layout"})

_HELP = textwrap.dedent(
    """
    SerpentFlow layout
    ------------------
      /layout                  — show the current mode
      /layout flow             — switch to flowing SerpentFlow (default)
      /layout split            — 3-pane split (stream | dashboard | diff)
      /layout focus <region>   — focus one region: stream | dashboard | diff
      /layout help             — this text

    Default is ``flow``. A split layout activates only when you ask.
    """
).strip()


@dataclass
class LayoutDispatchResult:
    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _COMMANDS


def dispatch_layout_command(
    line: str,
    *,
    controller: Optional[LayoutController] = None,
) -> LayoutDispatchResult:
    """Parse a ``/layout`` REPL line and apply it.

    Returns a :class:`LayoutDispatchResult` the caller's REPL prints.
    Never raises on operator-level malformed input — ``ok=False``
    with a usage-string carries the signal.
    """
    if not _matches(line):
        return LayoutDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return LayoutDispatchResult(
            ok=False, text=f"  /layout parse error: {exc}",
        )
    if not tokens:
        return LayoutDispatchResult(ok=False, text="", matched=False)
    c = controller or get_default_layout_controller()
    args = tokens[1:]
    if not args:
        return _layout_status(c)
    head = args[0].lower()
    if head in ("help", "?"):
        return LayoutDispatchResult(ok=True, text=_HELP)
    if head == "flow":
        return _switch(c, MODE_FLOW, reason="repl_flow")
    if head == "split":
        return _switch(c, MODE_SPLIT, reason="repl_split")
    if head == "focus":
        if len(args) < 2:
            return LayoutDispatchResult(
                ok=False,
                text=(
                    "  /layout focus <region>  "
                    "(stream | dashboard | diff)"
                ),
            )
        return _focus(c, args[1])
    return LayoutDispatchResult(
        ok=False, text=f"  /layout: unknown verb {head!r}",
    )


def _switch(
    controller: LayoutController, target: str, *, reason: str,
) -> LayoutDispatchResult:
    try:
        controller.set_mode(target, reason=reason)
    except LayoutError as exc:
        return LayoutDispatchResult(
            ok=False, text=f"  /layout: {exc}",
        )
    return LayoutDispatchResult(
        ok=True, text=f"  layout -> {target}",
    )


def _focus(
    controller: LayoutController, region: str,
) -> LayoutDispatchResult:
    # Accept both short form ("stream") and already-prefixed
    # ("focus:stream") so operators can paste either.
    if is_focus_mode(region):
        region_name = focus_region(region) or ""
    else:
        region_name = region.strip().lower()
    if region_name not in valid_regions():
        return LayoutDispatchResult(
            ok=False,
            text=(
                f"  /layout focus: unknown region {region!r} "
                f"(valid: {list(valid_regions())})"
            ),
        )
    try:
        controller.to_focus(region_name, reason="repl_focus")
    except LayoutError as exc:
        return LayoutDispatchResult(
            ok=False, text=f"  /layout focus: {exc}",
        )
    return LayoutDispatchResult(
        ok=True, text=f"  layout -> focus:{region_name}",
    )


def _layout_status(
    controller: LayoutController,
) -> LayoutDispatchResult:
    mode = controller.mode
    lines = [
        f"  current mode : {mode}",
        f"  valid targets: {', '.join(valid_regions())}",
        f"  verbs        : flow, split, focus <region>, help",
    ]
    return LayoutDispatchResult(ok=True, text="\n".join(lines))


__all__ = [
    "LayoutDispatchResult",
    "dispatch_layout_command",
]
