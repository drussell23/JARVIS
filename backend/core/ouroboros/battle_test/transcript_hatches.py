"""Transcript escape hatches — the cockpit is honest about its scrollback.

Claiming the alternate screen made the canvas ring (~20k retained lines)
the ONLY history there is — and a ring you can page but not search is a
transcript in a locked box. These are CC's transcript-viewer keys mapped
onto the ring the cockpit already keeps:

  * ``[``  — write the WHOLE retained transcript to the terminal's
    PRIMARY scrollback (run_in_terminal leaves the alternate screen, so
    the print lands where Cmd+F / tmux copy-mode can search it);
  * ``v``  — the same transcript, in ``$EDITOR``;
  * ``{`` / ``}`` — jump the viewport between BLOCK MARKERS (⏺ action
    opens, ❯ operator lines) instead of paging blind;
  * ``Ctrl+L`` — force a full repaint (the garbled-screen recovery key a
    full-screen app taking async IPC owes its operator);
  * ``Ctrl+O`` — flip live narration between on ↔ verbose via the
    EXISTING ``/narrate`` verb through the normal input path (mirrored,
    history-synced — one code path with the typed form).

``[``/``v``/``{``/``}`` are printable characters, so they bind ONLY while
the viewport is scrolled back (PgUp is the doorway) — at the live tail
they type as themselves, and a hand-written ``{`` can never teleport the
view. All remappable via keybindings.json.

Also here: the tmux passthrough probe — the OSC-9 gate bell is silently
swallowed inside tmux without ``allow-passthrough on``, and a bell that
fires into a void is worse than none because the operator TRUSTS it.

NEVER raises into key dispatch or a repaint.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

TRANSCRIPT_HATCHES_SCHEMA_VERSION: str = "transcript_hatches.1"

#: Plain-text prefixes that open a block worth jumping to.
_BLOCK_MARKERS = ("⏺", "❯", "⚡", "◆")

_MARKUP_RE = re.compile(r"\[/?[a-zA-Z0-9 #_.,=-]*\]")


def _strip_markup(line: str) -> str:
    """Rich markup → plain text; regex fallback keeps this dependency-free
    on the hot path. NEVER raises."""
    try:
        from rich.text import Text
        return Text.from_markup(line).plain
    except Exception:  # noqa: BLE001
        try:
            return _MARKUP_RE.sub("", line)
        except Exception:  # noqa: BLE001
            return line


def _canvas() -> Any:
    try:
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            get_active_canvas,
        )
        return get_active_canvas()
    except Exception:  # noqa: BLE001
        return None


def transcript_lines() -> List[str]:
    """The retained transcript, oldest→newest, markup stripped. Empty when
    no cockpit canvas is live (fallback surface). NEVER raises."""
    try:
        mux = _canvas()
        if mux is None:
            return []
        snap = mux._buffer.snapshot()  # noqa: SLF001 — the ring IS the transcript
        return [_strip_markup(str(ln)) for ln in snap]
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] transcript read degraded", exc_info=True)
        return []


def _viewport_state() -> Optional[Tuple[Any, int, int]]:
    """(viewport, total, budget) for the live canvas, or None."""
    try:
        mux = _canvas()
        if mux is None:
            return None
        total, budget = mux.scroll_metrics()
        return mux._viewport, int(total), int(budget)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return None


def is_scrolled_back() -> bool:
    """True while the operator is reading history — the state in which the
    printable hatch keys are live. NEVER raises."""
    try:
        state = _viewport_state()
        return state is not None and not state[0].following
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# the hatches
# ---------------------------------------------------------------------------


def dump_to_scrollback(event: Any) -> None:
    """``[`` — the whole ring into the PRIMARY buffer, natively searchable.
    NEVER raises."""
    try:
        lines = transcript_lines()
        if not lines:
            return

        def _print() -> None:
            print(f"\n───── O+V transcript · {len(lines)} retained lines "
                  "(searchable here; the cockpit resumes below) ─────")
            for ln in lines:
                print(ln)
            print("───── end transcript ─────\n")

        from prompt_toolkit.application import run_in_terminal
        run_in_terminal(_print)
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] dump degraded", exc_info=True)


def open_in_editor(event: Any) -> None:
    """``v`` — the transcript in $EDITOR. NEVER raises."""
    try:
        import subprocess
        import tempfile

        lines = transcript_lines()
        if not lines:
            return
        editor = (os.environ.get("VISUAL") or os.environ.get("EDITOR")
                  or "vi")

        def _edit() -> None:
            try:
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".ov-transcript", delete=False,
                ) as fh:
                    fh.write("\n".join(lines) + "\n")
                    path = fh.name
                subprocess.run([*editor.split(), path], check=False)
                os.unlink(path)
            except Exception:  # noqa: BLE001
                pass

        from prompt_toolkit.application import run_in_terminal
        run_in_terminal(_edit)
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] editor degraded", exc_info=True)


def jump_block(event: Any, direction: int) -> None:
    """``{``/``}`` — move the viewport to the previous/next block marker.
    Paging finds a place; this finds a THING. NEVER raises."""
    try:
        state = _viewport_state()
        if state is None:
            return
        viewport, total, budget = state
        lines = transcript_lines()
        if not lines:
            return
        top = max(0, total - budget - viewport.offset)
        markers = [
            i for i, ln in enumerate(lines)
            if ln.lstrip().startswith(_BLOCK_MARKERS)
        ]
        if direction < 0:
            candidates = [i for i in markers if i < top]
            target = candidates[-1] if candidates else None
        else:
            candidates = [i for i in markers if i > top]
            target = candidates[0] if candidates else None
        if target is None:
            return
        new_offset = max(0, total - budget - target)
        # scroll() computes want = offset - lines → aim it exactly.
        viewport.scroll(
            viewport.offset - new_offset, total=total, budget=budget,
        )
        mux = _canvas()
        if mux is not None:
            mux._invalidate_now()  # noqa: SLF001 — the scroll keys' own move
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] jump degraded", exc_info=True)


def force_redraw(event: Any) -> None:
    """``Ctrl+L`` — recover a garbled screen. NEVER raises."""
    try:
        event.app.renderer.clear()
        event.app.invalidate()
    except Exception:  # noqa: BLE001
        pass


def install_transcript_hatches(kb: Any, ui: Any, client: Any) -> bool:
    """Mount the hatch actions (all keybindings.json-remappable). NEVER
    raises; returns True when ≥1 bound."""
    try:
        from prompt_toolkit.filters import Condition

        from backend.core.ouroboros.battle_test.keymap import bind_action

        scrolled = Condition(is_scrolled_back)
        bound = 0

        bound += bind_action(
            kb, "transcript:dump", ("[",), dump_to_scrollback,
            context="Transcript", filter=scrolled,
            description="write the transcript to native scrollback "
                        "(while scrolled)",
        )
        bound += bind_action(
            kb, "transcript:editor", ("v",), open_in_editor,
            context="Transcript", filter=scrolled,
            description="open the transcript in $EDITOR (while scrolled)",
        )
        bound += bind_action(
            kb, "transcript:prevBlock", ("{",),
            lambda e: jump_block(e, -1),
            context="Transcript", filter=scrolled,
            description="jump to the previous ⏺/❯ block (while scrolled)",
        )
        bound += bind_action(
            kb, "transcript:nextBlock", ("}",),
            lambda e: jump_block(e, +1),
            context="Transcript", filter=scrolled,
            description="jump to the next ⏺/❯ block (while scrolled)",
        )
        bound += bind_action(
            kb, "app:redraw", ("ctrl+l",), force_redraw,
            context="Global",
            description="force a full repaint (garbled-screen recovery)",
        )

        def _toggle_narrate(event: Any) -> None:
            try:
                verbose = not bool(getattr(ui, "_narrate_verbose", False))
                ui._narrate_verbose = verbose
                client.send_input(
                    "/narrate verbose" if verbose else "/narrate on",
                )
                ui.flash(
                    "🗣 narration: verbose" if verbose
                    else "🗣 narration: normal", seconds=2.0,
                )
            except Exception:  # noqa: BLE001
                pass

        bound += bind_action(
            kb, "narrate:toggle", ("ctrl+o",), _toggle_narrate,
            context="Global",
            description="toggle live narration normal ↔ verbose (/narrate)",
        )
        return bound > 0
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] install degraded", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# tmux — the bell's delivery path
# ---------------------------------------------------------------------------


def tmux_bell_warning(*, timeout_s: float = 1.0) -> str:
    """A one-line warning when tmux would swallow the gate bell, or "".

    Probed, not assumed: outside tmux, or with passthrough already on,
    silence is correct. NEVER raises."""
    try:
        if not os.environ.get("TMUX"):
            return ""
        if os.environ.get(
            "JARVIS_GATE_BELL_ENABLED", "true",
        ).strip().lower() in ("0", "false", "no", "off"):
            return ""
        import subprocess
        proc = subprocess.run(
            ["tmux", "show", "-gv", "allow-passthrough"],
            capture_output=True, text=True, timeout=timeout_s,
        )
        value = (proc.stdout or "").strip().lower()
        if value in ("on", "all"):
            return ""
        return ("⚠ tmux swallows the gate bell — add "
                "`set -g allow-passthrough on` to ~/.tmux.conf "
                "(then: tmux source-file ~/.tmux.conf)")
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "TRANSCRIPT_HATCHES_SCHEMA_VERSION",
    "dump_to_scrollback",
    "force_redraw",
    "install_transcript_hatches",
    "is_scrolled_back",
    "jump_block",
    "open_in_editor",
    "tmux_bell_warning",
    "transcript_lines",
]
