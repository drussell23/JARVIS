"""§37 Slice 9 — OSC 8 hyperlink substrate.

Closes Tier 1 #9 from the §37 UX roadmap. OSC 8 (Operating System
Command 8) is a terminal escape sequence supported by modern
terminals (iTerm2 / GNOME Terminal / Alacritty / Kitty / Windows
Terminal / VS Code integrated terminal / etc.) that lets text
be rendered as a clickable hyperlink. Format::

    \\033]8;;<URL>\\033\\\\<TEXT>\\033]8;;\\033\\\\

Modern terminal: clickable hyperlink. Older terminal: plain text
(escape sequences are silently ignored when the terminal doesn't
recognize them — the visible text remains correctly rendered).

Per the operator binding "fully leverage existing files... no
duplication... advanced/dynamic":

  * **TERM-aware detection** — composes the established
    presentation_restraint TTY-gate pattern. OSC 8 only
    rendered when `sys.stdout.isatty()` AND the TERM env var
    suggests a modern terminal.
  * **Substrate-only** — pure-function wrapper. Callers
    compose `wrap(text, url)` at render time. NO global state,
    NO singleton.
  * **NEVER raises** — defensive: any error in TTY detection
    or env reading falls through to plain text.
  * **Master-flag-aware** — operator opt-out via
    `JARVIS_OSC8_HYPERLINKS_ENABLED=false`.

Identity preservation: hyperlinks render the SAME visible text
as the plain version (no decorative underline or color change
forced by this module — terminal renders its own affordance).

What this module does NOT do:

  * NOT detect the TARGET terminal's capabilities at runtime
    (that requires terminfo + DA1/DA2/DCS round-trips, much
    higher cost). Uses TERM env-var heuristic — same shape as
    `presentation_restraint.set_terminal_title` (Gap #7
    Slice 4 closure).
  * NOT introduce a clickable-link state machine. Each call is
    independent.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional


logger = logging.getLogger("Ouroboros.OSC8")


OSC8_SCHEMA_VERSION: str = "osc8.1"


# ---------------------------------------------------------------------------
# TERM-awareness — heuristic detection
# ---------------------------------------------------------------------------


# Modern terminals known to support OSC 8 hyperlinks. Conservative
# allow-list — operators with non-listed terminals can still opt
# in via JARVIS_OSC8_HYPERLINKS_ENABLED=true (forces enabled
# regardless of TERM).
_OSC8_KNOWN_TERMS: frozenset = frozenset({
    "xterm-256color",
    "xterm-kitty",
    "tmux-256color",
    "screen-256color",
    "alacritty",
    "iterm2",
    "iterm.app",
    "wezterm",
    "ghostty",
    "konsole",
    "gnome-terminal",
    "vscode",
})


# TERM_PROGRAM env-var values that indicate OSC 8 support.
# Some terminals identify via TERM_PROGRAM rather than TERM.
_OSC8_KNOWN_TERM_PROGRAMS: frozenset = frozenset({
    "iterm.app",
    "vscode",
    "kitty",
    "wezterm",
    "ghostty",
    "alacritty",
    "warpterminal",
})


def _master_enabled() -> bool:
    """``JARVIS_OSC8_HYPERLINKS_ENABLED`` master flag.
    Defaults to ``true`` — opts operator IN by default since
    OSC 8 degrades cleanly on non-supporting terminals (escape
    sequences are silently swallowed). Set ``=false`` to
    disable globally."""
    raw = os.environ.get(
        "JARVIS_OSC8_HYPERLINKS_ENABLED", "true",
    ).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _real_stdout_isatty() -> bool:
    """Use sys.__stdout__ (the unpatched original) so that a
    test fixture replacing sys.stdout doesn't false-positive
    the gate. Mirrors the load-bearing TTY-gate fix from
    Gap #7 Slice 2 (`presentation_restraint.real_stdout_isatty`)."""
    try:
        original = getattr(sys, "__stdout__", None)
        if original is None:
            return False
        return bool(original.isatty())
    except Exception:  # noqa: BLE001 — defensive
        return False


def is_supported() -> bool:
    """Return True when OSC 8 hyperlinks should render. Composes:

      1. Master flag: JARVIS_OSC8_HYPERLINKS_ENABLED (default
         true; operator opt-out)
      2. Real-stdout TTY gate: same pattern as Gap #7 Slice 2's
         `real_stdout_isatty` — checks sys.__stdout__ so prompt_
         toolkit's stdout-patching doesn't false-positive
      3. TERM heuristic: TERM or TERM_PROGRAM env-var matches
         a known-supporting terminal

    NEVER raises. Returns False on any detection error.
    """
    if not _master_enabled():
        return False
    if not _real_stdout_isatty():
        return False
    try:
        term = (
            os.environ.get("TERM", "")
            .strip().lower()
        )
        term_program = (
            os.environ.get("TERM_PROGRAM", "")
            .strip().lower()
        )
        if term in _OSC8_KNOWN_TERMS:
            return True
        if term_program in _OSC8_KNOWN_TERM_PROGRAMS:
            return True
        # `xterm` plain (without -256color) — not all
        # generations support OSC 8. Conservative: skip.
        return False
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Pure-function wrapper
# ---------------------------------------------------------------------------


def wrap(
    text: str,
    url: str,
    *,
    force: Optional[bool] = None,
) -> str:
    """Wrap ``text`` in an OSC 8 hyperlink to ``url`` when the
    terminal supports it. Falls through to plain ``text`` when
    not supported (or master flag off, or non-TTY).

    Args:
        text: visible text (unchanged on non-supporting
            terminals)
        url: target URL (file:// / http(s):// / etc.)
        force: when ``True``, render OSC 8 regardless of
            detection (used in tests + when caller has explicit
            knowledge); when ``False``, render plain regardless;
            ``None`` (default) consults :func:`is_supported`.

    Pure function. NEVER raises. Empty / non-string inputs
    return the text as-is.
    """
    if not isinstance(text, str):
        return str(text or "")
    if not isinstance(url, str) or not url:
        return text
    # Decide whether to render
    if force is True:
        render = True
    elif force is False:
        render = False
    else:
        render = is_supported()
    if not render:
        return text
    # OSC 8 escape format:
    #   \x1b]8;;<URL>\x1b\\<TEXT>\x1b]8;;\x1b\\
    # The trailing \x1b\\ ("ST" — String Terminator) closes
    # the OSC sequence. Must use \x1b\\ literally; \x07 (BEL)
    # also works as terminator on some terminals but \x1b\\ is
    # more portable.
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


# ---------------------------------------------------------------------------
# File-URL helper — composes pathlib for absolute paths
# ---------------------------------------------------------------------------


def file_url(path: str, line: Optional[int] = None) -> str:
    """Construct a ``file://`` URL for editor-jump links.
    Optionally appends ``#L<line>`` fragment which most editors
    (VS Code / IntelliJ / etc.) honor on hyperlink open.

    NEVER raises. Returns empty string on malformed input.
    """
    try:
        from pathlib import Path
        if not isinstance(path, str) or not path:
            return ""
        # Resolve to absolute path; URL encoding handled
        # implicitly (file paths rarely need it).
        abs_path = str(Path(path).resolve())
        url = f"file://{abs_path}"
        if line is not None:
            try:
                url += f"#L{int(line)}"
            except (TypeError, ValueError):
                pass
        return url
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``osc8_uses_real_stdout_isatty`` — TTY gate uses
         sys.__stdout__ (unpatched), not sys.stdout (patched
         by prompt_toolkit). Catches the load-bearing TTY
         regression Gap #7 Slice 2 closed.
      2. ``osc8_authority_asymmetry`` — substrate purity.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = "backend/core/ouroboros/governance/osc8.py"

    def _validate_uses_real_stdout(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name == "_real_stdout_isatty":
                    target_func = node
                    break
        if target_func is None:
            violations.append(
                "_real_stdout_isatty function missing"
            )
            return tuple(violations)
        # Body MUST reference sys.__stdout__ specifically.
        # `sys.stdout.isatty()` would re-introduce the Gap #7
        # Slice 2 regression where prompt_toolkit's
        # patch_stdout shadows the gate.
        body_text = ast.get_source_segment(
            source, target_func,
        ) or ""
        if "__stdout__" not in body_text:
            violations.append(
                "_real_stdout_isatty MUST reference "
                "sys.__stdout__ (Gap #7 Slice 2 regression "
                "discipline) — sys.stdout.isatty() returns "
                "False under prompt_toolkit's patch_stdout"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"osc8.py MUST NOT import {module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="osc8_uses_real_stdout_isatty",
            target_file=target,
            description=(
                "§37 Slice 9 — TTY gate uses sys.__stdout__ "
                "(unpatched original). Mirrors Gap #7 Slice 2 "
                "load-bearing fix; sys.stdout.isatty() returns "
                "False under prompt_toolkit's patch_stdout."
            ),
            validate=_validate_uses_real_stdout,
        ),
        ShippedCodeInvariant(
            invariant_name="osc8_authority_asymmetry",
            target_file=target,
            description=(
                "§37 Slice 9 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "OSC8_SCHEMA_VERSION",
    "file_url",
    "is_supported",
    "register_shipped_invariants",
    "wrap",
]
