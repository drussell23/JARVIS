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


def viewport_top() -> Optional[int]:
    """Index of the transcript line currently at the top of the viewport.

    The single conversion between "where the operator is looking" and "which
    line that is" — every jump reads it and every jump writes through
    :func:`scroll_to_index`, so the two cannot disagree about which end
    ``offset`` counts from. NEVER raises.
    """
    try:
        state = _viewport_state()
        if state is None:
            return None
        viewport, total, budget = state
        return max(0, total - budget - viewport.offset)
    except Exception:  # noqa: BLE001
        return None


def scroll_to_index(index: int, *, context: int = 0) -> bool:
    """Put transcript line ``index`` at the top of the viewport.

    ``context`` lifts the target down the screen by that many rows, so a
    search hit lands with what came BEFORE it visible. A match at the very
    top of the screen is technically found and practically useless: the lines
    that explain it are the ones just above.

    Returns whether the viewport moved. NEVER raises.
    """
    try:
        state = _viewport_state()
        if state is None:
            return False
        viewport, total, budget = state
        target = max(0, int(index) - max(0, int(context)))
        new_offset = max(0, total - budget - target)
        # scroll() computes want = offset - lines → aim it exactly.
        moved = viewport.scroll(
            viewport.offset - new_offset, total=total, budget=budget,
        )
        mux = _canvas()
        if mux is not None:
            mux._invalidate_now()  # noqa: SLF001 — the scroll keys' own move
        return bool(moved)
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] scroll_to_index degraded", exc_info=True)
        return False


def jump_block(event: Any, direction: int) -> None:
    """``{``/``}`` — move the viewport to the previous/next block marker.
    Paging finds a place; this finds a THING. NEVER raises."""
    try:
        top = viewport_top()
        if top is None:
            return
        lines = transcript_lines()
        if not lines:
            return
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
        scroll_to_index(target)
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] jump degraded", exc_info=True)


#: `Ctrl+L` pressed twice CLEARS. One latch, module-level, because the two
#: presses are two separate key events and the state between them has to
#: outlive the first handler.
_CLEAR_LATCH: Any = None


def _clear_latch() -> Any:
    """The double-press latch, built once. NEVER raises; None if unavailable."""
    global _CLEAR_LATCH
    if _CLEAR_LATCH is None:
        try:
            from backend.core.ouroboros.battle_test.confirm_chord import (
                ConfirmLatch,
            )
            # CC's window for this gesture is TWO seconds, not the three its
            # `Ctrl+X Ctrl+K` uses — and the difference is the point. Killing
            # every agent deserves a longer think than redrawing a screen, so
            # the window is pinned here rather than inherited from the shared
            # default.
            _CLEAR_LATCH = ConfirmLatch(window_s=_clear_window_s())
        except Exception:  # noqa: BLE001
            return None
    return _CLEAR_LATCH


def _clear_window_s() -> float:
    """``JARVIS_CLEAR_DOUBLE_PRESS_S`` (default 2.0, CC's). NEVER raises."""
    try:
        return max(0.5, min(10.0, float(
            os.environ.get("JARVIS_CLEAR_DOUBLE_PRESS_S", "") or 2.0)))
    except (TypeError, ValueError):
        return 2.0


def force_redraw(event: Any) -> None:
    """``Ctrl+L`` — recover a garbled screen; twice in 2s clears. NEVER raises.

    Claude Code: "if you press `Ctrl+L` once, Claude Code redraws the screen
    and also shows a hint that pressing it again runs `/clear`. If you press
    it twice within two seconds, Claude Code runs `/clear`."

    The redraw happens on BOTH presses, and that ordering is the whole design:
    the first press must do its own job completely, because an operator
    reaching for Ctrl+L is usually recovering a garbled screen and has no
    intention of clearing anything. Arming is a side effect of a key that
    already worked, never a mode it puts them into.

    Reuses `ConfirmLatch` — the primitive built for `Ctrl+X Ctrl+K`, whose
    docstring already named this as CC's second use of the shape. A second
    timer here would be two answers to "did they press it twice".
    """
    try:
        event.app.renderer.clear()
        event.app.invalidate()
    except Exception:  # noqa: BLE001
        pass
    try:
        latch = _clear_latch()
        if latch is None:
            return
        if not latch.press():
            _flash_clear_hint(event)
            return
        _submit_clear(event)
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] clear latch degraded", exc_info=True)


def _flash_clear_hint(event: Any) -> None:
    """Say that a second press clears. NEVER raises.

    Without this the gesture is undiscoverable AND dangerous: an operator who
    happens to hit Ctrl+L twice while fixing a garbled screen would lose their
    transcript with no warning that it was about to happen.
    """
    try:
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            get_active_canvas,
        )
        canvas = get_active_canvas()
        if canvas is not None:
            # `push_raw`, not `emit`: `emit` renders through the typed
            # event registry, and an unregistered type falls to the null
            # renderer, which prints the TYPE NAME. This surfaced as a
            # transcript line reading `· line`.
            canvas.push_raw(
                f"  [dim]press ctrl+l again within "
                f"{_clear_window_s():.0f}s to clear the transcript[/dim]")
    except Exception:  # noqa: BLE001
        pass


def _submit_clear(event: Any) -> None:
    """Run `/clear` the way a typed line would. NEVER raises.

    Through the prompt buffer's own accept handler rather than by calling a
    clear function directly: `/clear` is a verb every surface already routes,
    and reaching past that router would mean this key clears on some surfaces
    and not others.
    """
    try:
        buf = event.app.current_buffer
        if buf is None:
            return
        buf.text = "/clear"
        buf.cursor_position = len("/clear")
        buf.validate_and_handle()
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] clear submit degraded", exc_info=True)


# ---------------------------------------------------------------------------
# `/` search — the hatch this module's own docstring called for
# ---------------------------------------------------------------------------
#
# "A ring you can page but not search is a transcript in a locked box." That
# sentence has opened this file since it shipped, above four hatches and no
# search. `TranscriptSearch` was written — smart case, wrapping `n`/`N`, Esc
# restoring your place, 21 tests — and never bound to a key.
#
# It mounts HERE rather than in a module of its own because this is already
# the transcript key cluster: `[`, `v`, `{`, `}` bind under the same
# scrolled-back doorway, read the same ring through the same accessor, and
# move the view through the same `scroll_to_index`. A second module would
# have to reimplement all four of those to have a `/`.


#: One session per process, lazily made. The search holds a cursor and a
#: restore point across keystrokes, so it cannot be rebuilt per press —
#: `n` means nothing without the search that preceded it.
_SEARCH: Any = None


def get_search() -> Any:
    """The live search session, bound to the canvas viewport. NEVER raises."""
    global _SEARCH
    try:
        from backend.core.ouroboros.battle_test.transcript_search import (
            TranscriptSearch,
        )
        state = _viewport_state()
        viewport = state[0] if state is not None else None
        if _SEARCH is None:
            _SEARCH = TranscriptSearch(viewport)
        elif viewport is not None and _SEARCH._viewport is not viewport:  # noqa: SLF001
            # The cockpit was rebuilt (detach → reattach, resize remount) and
            # this is a NEW viewport. Rebinding beats keeping a session that
            # would scroll a container nothing is drawing.
            _SEARCH = TranscriptSearch(viewport)
        return _SEARCH
    except Exception:  # noqa: BLE001
        return None


def reset_search_for_tests() -> None:
    global _SEARCH
    _SEARCH = None


def _ring_state() -> Tuple[int, int]:
    """``(push_count, retained)`` — the ring's monotonic counter and depth.

    Read together with the lines in one pass wherever both are needed, so a
    push landing between two reads cannot skew every ordinal by one. That
    skew only appears under load, which is exactly when a search is used.
    """
    try:
        mux = _canvas()
        if mux is None:
            return 0, 0
        buf = mux._buffer                      # noqa: SLF001 — the ring
        retained = int(buf.line_count)
        return int(getattr(buf, "push_count", retained)), retained
    except Exception:  # noqa: BLE001
        return 0, 0


def search_is_armed() -> bool:
    """True while the operator is typing a query. NEVER raises."""
    try:
        return bool(_ARMED[0])
    except Exception:  # noqa: BLE001
        return False


#: A one-slot latch rather than an attribute on the session: "the bar is
#: open" and "a query exists" are different states, and conflating them makes
#: `n` after closing the bar impossible to express.
_ARMED = [False]


def search_status() -> List[str]:
    """The search bar, or [] when it is closed.

    Returned as ROWS so it mounts through the same dynamic-rows container the
    agent view uses — one geometry primitive, not a bespoke widget per strip.
    """
    try:
        search = get_search()
        if search is None:
            return []
        if not search_is_armed() and not search.query:
            return []
        status = search.status() or f"/{search.query}"
        return [f"  {status}" + ("▏" if search_is_armed() else "")]
    except Exception:  # noqa: BLE001
        return []


def _reveal_current(search: Any) -> None:
    """Scroll to the cursor's match, honestly about eviction. NEVER raises."""
    try:
        push, retained = _ring_state()
        index = search.resolve(
            search.current, push_count=push, retained=retained,
        )
        if index is None:
            return
        state = _viewport_state()
        if state is None:
            return
        _viewport, total, budget = state
        search.reveal(index, total, budget)
        mux = _canvas()
        if mux is not None:
            mux._invalidate_now()              # noqa: SLF001
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] reveal degraded", exc_info=True)


def _run_search(search: Any) -> None:
    """Re-run the query against the CURRENT ring and reveal the first hit.

    Re-run on every keystroke rather than filtered from the previous result:
    the buffer grows while the operator types, and a filtered search would be
    confined to whatever the shorter prefix happened to match a second ago.
    """
    try:
        from backend.core.ouroboros.battle_test.transcript_search import (
            base_ordinal,
        )
        # Counter and lines in ONE pass, then the base derived from that pair
        # — reading them apart would let a push land between and skew every
        # ordinal by one.
        push, retained = _ring_state()
        lines = transcript_lines()
        search.search(lines, search.query,
                      base=base_ordinal(push, len(lines) or retained))
        _reveal_current(search)
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] search degraded", exc_info=True)


def install_transcript_search(kb: Any, ui: Any = None) -> int:
    """Bind `/` `n` `N` and the query capture. Returns the count bound.

    The grammar is CC's, and each key is filtered so it can only fire in the
    state where it is unambiguous:

      ``/``          arms the bar — while SCROLLED BACK, where `/` cannot be
                     the command palette because the operator is reading
                     history rather than issuing commands. No new mode: the
                     scroll state already distinguishes them.
      ``<any>``      the query, while armed. Printable characters only, so a
                     control sequence never lands in a search string.
      ``backspace``  edit it; on the last character the bar closes, because
                     an empty bar is a mode with nothing in it.
      ``enter``      keep the position and close — the operator found it.
      ``escape``     close AND restore the pre-search offset.
      ``n`` / ``N``  walk the matches after the bar is closed, which is where
                     CC puts them and the only place they are unambiguous:
                     while the bar is open, `n` is a letter.

    NEVER raises.
    """
    try:
        from prompt_toolkit.filters import Condition

        from backend.core.ouroboros.battle_test.keymap import bind_action
        from backend.core.ouroboros.battle_test.transcript_search import (
            search_enabled,
        )

        armed = Condition(search_is_armed)
        scrolled_idle = Condition(
            lambda: is_scrolled_back() and not search_is_armed()
                    and search_enabled()
        )

        def _flash(text: str) -> None:
            try:
                if ui is not None and hasattr(ui, "flash"):
                    ui.flash(text, seconds=2.0)
            except Exception:  # noqa: BLE001
                pass

        def _open(event: Any) -> None:
            search = get_search()
            if search is None:
                return
            search.reset()
            search.begin()                    # remember where we were
            _ARMED[0] = True
            _repaint()

        def _type(event: Any) -> None:
            search = get_search()
            if search is None:
                return
            ch = getattr(event, "data", "") or ""
            # Printable only. A stray escape sequence arriving as data would
            # otherwise be searched for, and the operator would see a query
            # they never typed.
            if len(ch) != 1 or not ch.isprintable():
                return
            search.query += ch
            _run_search(search)
            _repaint()

        def _backspace(event: Any) -> None:
            search = get_search()
            if search is None:
                return
            search.query = search.query[:-1]
            if not search.query:
                _close(restore=False)
                return
            _run_search(search)
            _repaint()

        def _accept(event: Any) -> None:
            _ARMED[0] = False                 # keep the query for n / N
            _repaint()

        def _cancel(event: Any) -> None:
            _close(restore=True)

        def _close(*, restore: bool) -> None:
            search = get_search()
            _ARMED[0] = False
            if search is None:
                return
            if restore:
                search.cancel()
            else:
                search.reset()
            _repaint()

        def _step(forward: bool):
            def _handler(event: Any) -> None:
                search = get_search()
                if search is None or not search.matches:
                    return
                push, retained = _ring_state()
                # Walk until a match that still EXISTS, at most one full lap.
                for _ in range(len(search.matches)):
                    ordinal = search.step(forward)
                    if search.resolve(
                        ordinal, push_count=push, retained=retained,
                    ) is not None:
                        _reveal_current(search)
                        _repaint()
                        return
                _flash("⚠ every match has scrolled out of the transcript")
            return _handler

        bound = 0
        bound += bind_action(
            kb, "transcript:search", ("/",), _open,
            context="Transcript", filter=scrolled_idle,
            description="search the transcript (while scrolled)",
        )
        # The query capture binds DIRECTLY, not through `bind_action`.
        #
        # `<any>` is a wildcard, not a key, and the registry is right to
        # refuse it: an action is something an operator can rebind, and
        # "every key on the keyboard" is not a chord anyone can express in
        # keybindings.json. Routing it through the catalog would either
        # require teaching the normaliser a fake key or advertise a
        # rebindable action that silently ignores the rebind.
        #
        # So the six real keys stay remappable and this one — the search bar
        # consuming its own input while open — is a plain binding.
        try:
            from prompt_toolkit.keys import Keys
            kb.add(Keys.Any, filter=armed)(_type)
            bound += 1
        except Exception:  # noqa: BLE001
            logger.debug("[Hatches] query capture degraded", exc_info=True)
        bound += bind_action(
            kb, "transcript:searchBack", ("backspace",), _backspace,
            context="Transcript", filter=armed,
            description="edit the search query",
        )
        bound += bind_action(
            kb, "transcript:searchAccept", ("enter",), _accept,
            context="Transcript", filter=armed, eager=True,
            description="keep the found position and close the search bar",
        )
        bound += bind_action(
            kb, "transcript:searchCancel", ("escape",), _cancel,
            context="Transcript", filter=armed, eager=True,
            description="close the search and go back where you were",
        )
        bound += bind_action(
            kb, "transcript:nextMatch", ("n",), _step(True),
            context="Transcript", filter=scrolled_idle,
            description="jump to the next match (while scrolled)",
        )
        bound += bind_action(
            kb, "transcript:prevMatch", ("N",), _step(False),
            context="Transcript", filter=scrolled_idle,
            description="jump to the previous match (while scrolled)",
        )
        return bound
    except Exception:  # noqa: BLE001
        logger.debug("[Hatches] search install degraded", exc_info=True)
        return 0


def _repaint() -> None:
    try:
        mux = _canvas()
        if mux is not None:
            mux._invalidate_now()              # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def install_transcript_hatches(kb: Any, ui: Any, client: Any) -> bool:
    """Mount the hatch actions (all keybindings.json-remappable). NEVER
    raises; returns True when ≥1 bound."""
    try:
        from prompt_toolkit.filters import Condition

        from backend.core.ouroboros.battle_test.keymap import bind_action

        # The viewer, OR a scrolled-back canvas. Widened rather than
        # replaced: scrolled-back was the ONLY way these keys ever became
        # live, and an operator with that habit must not lose it because a
        # mode arrived. `transcript_surface_active` is the one predicate both
        # states resolve through, so the two cannot drift.
        from backend.core.ouroboros.battle_test.transcript_mode import (
            transcript_surface_active,
        )
        scrolled = Condition(transcript_surface_active)
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
            # MOVED off Ctrl+O, which Claude Code owns for the transcript
            # viewer. Three actions claimed that one key here — this, the
            # deck's `lanes`, and the legacy TUI's expand — which is one more
            # than a key can serve. Narration loses the least by moving: it
            # is the only one of the three with a typed verb (`/narrate`)
            # that does exactly the same thing.
            kb, "narrate:toggle", ("ctrl+x ctrl+n",), _toggle_narrate,
            context="Global",
            description="toggle live narration normal ↔ verbose (/narrate)",
        )
        # The search this file has asked for since its first line.
        bound += install_transcript_search(kb, ui)
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
    "get_search",
    "install_transcript_search",
    "reset_search_for_tests",
    "scroll_to_index",
    "search_is_armed",
    "search_status",
    "viewport_top",
    "force_redraw",
    "install_transcript_hatches",
    "is_scrolled_back",
    "jump_block",
    "open_in_editor",
    "tmux_bell_warning",
    "transcript_lines",
]
