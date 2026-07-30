"""Bipartite Async Layout — the CC-style two-zone TUI for the proactive organism.

The root cause of shattered terminal UIs is synchronous blocking I/O interleaving
background telemetry with the user's keystrokes. This decouples the two into a
strictly zoned, prompt_toolkit-driven surface:

  * **Zone 1 — the Proactive Canvas** (top): a rounded ``rich.panel.Panel`` that
    every ``StreamEventBroker`` background event (Sentinel logs, AWE triggers, DLQ
    checkpoints, …) auto-scrolls into. Bounded (a ``RegionBuffer`` ring) so it can
    be streamed into aggressively without unbounded growth.
  * **Zone 2 — the Command Deck** (bottom): a permanently anchored ``> `` prompt.

The two are composed with ``rich.layout.Layout`` (Zone 1 = a Panel region, Zone 2
= the deck region) and driven by a full-screen ``prompt_toolkit.Application``. The
Application's render loop redraws the whole screen atomically on every
``invalidate()`` — which is the **async-input-handling equivalent of
``patch_stdout``**, and strictly stronger: because prompt_toolkit owns the screen,
a background task streaming 50 events into Zone 1 can NEVER overwrite, stutter, or
corrupt the keystrokes the user is typing into Zone 2. (The legacy non-fullscreen
REPL already uses ``patch_stdout(raw=True)`` for its print-above-prompt model; the
full-screen Application supersedes it for the framed layout.)

DRY: the telemetry is formatted by the SAME unified event router primitives the
``/breadcrumbs`` feed uses — ``EventBreadcrumbRegistry.describe`` (glyph/color) +
``.render`` (severity/text). This module only redirects the SINK of those events
into Zone 1; it never re-formats them. The bounded Zone-1 buffer reuses
``split_layout.RegionBuffer``. Fable is never referenced. Never raises on the
hot path.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, List, Optional, Tuple

from backend.core.ouroboros.ui.semantic_tokens import (  # noqa: E402
    role_palette as _role_palette,
)

#: Semantic colour roles — the SAME name and access pattern as every
#: other module. One vocabulary, one spelling, one owner.
_SEM = _role_palette()

logger = logging.getLogger("Ouroboros.BipartiteLayout")

from backend.core.ouroboros.battle_test.input_continuation import (
    prompt_height,
)
from backend.core.ouroboros.battle_test.canvas_viewport import (
    CanvasViewport, canvas_history_lines, install_scroll_bindings,
    scrollback_enabled,
)

_DEFAULT_MAX_LINES = 500


def bipartite_enabled() -> bool:
    """The framed cockpit is now the DEFAULT entry point (opt-in flag removed).
    A dedicated KILL-SWITCH remains for safety — ``JARVIS_BIPARTITE_LAYOUT_DISABLED``
    (or ``JARVIS_BIPARTITE_LAYOUT_ENABLED=0``) instantly reverts to the legacy
    flowing loop, since the full-screen app can't be compile-tested headless.
    Enabling still requires a real TTY (see :func:`should_run_bipartite`)."""
    if os.environ.get("JARVIS_BIPARTITE_LAYOUT_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return False
    return os.environ.get(
        "JARVIS_BIPARTITE_LAYOUT_ENABLED", "true",   # default ON — the cockpit
    ).strip().lower() in ("1", "true", "yes", "on")


def mouse_enabled() -> bool:
    """Should the cockpit capture mouse events?

    Follows the alt-screen decision — without it the deck is not a scrollable
    viewport and there is nothing for the wheel to move.
    ``JARVIS_DISABLE_MOUSE=1`` opts out on its own, for anyone who relies on
    native click-and-drag selection.
    """
    if os.environ.get("JARVIS_DISABLE_MOUSE", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return False
    return fullscreen_enabled()


def _real_tty() -> bool:
    """A real interactive terminal, via the canonical helper.

    ``sys.stdout.isatty()`` is False under the active ``patch_stdout`` proxy,
    which is why `real_stdout_isatty` (reading ``sys.__stdout__``) exists. One
    definition, used by both the launch gate and the alt-screen decision, so
    they cannot disagree about what a terminal is.
    """
    try:
        from backend.core.ouroboros.battle_test.presentation_restraint import (
            real_stdout_isatty,
        )
        return real_stdout_isatty()
    except Exception:  # noqa: BLE001
        return False


def fullscreen_enabled() -> bool:
    """Does the cockpit claim the terminal's ALTERNATE SCREEN?

    `full_screen=True` issues smcup, which gives a fixed viewport and — as a
    direct consequence — **disables the terminal's native scrollback**. An
    operator scrolling up to re-read what the organism did an hour ago finds
    nothing there, because the alternate buffer has no history and the primary
    buffer stopped receiving output the moment the cockpit mounted.

    That made the cockpit's own canvas load-bearing, and it was not up to the
    job: Zone 1 existed to replace the scrollback the alt-screen had taken
    away, but it rendered only ``snap[-budget:]`` — the last screenful, with
    no way to reach anything above it. Claiming the screen meant deleting the
    session's history, so #70171 defaulted this OFF and let the terminal keep
    what it was better at keeping.

    **Default TRUE as of the scrollback viewport.** `canvas_viewport` turned
    Zone 1 from a tail into a window over ~20k retained lines, with PgUp/PgDn,
    Home/End and a view that holds still while the organism appends. The
    canvas can now hold a session, so the objection that kept the cockpit out
    of the alternate screen no longer applies — and full-screen is what makes
    `ov` an instrument you are inside of rather than a command that scrolled
    past. Exiting issues rmcup, so the shell comes back untouched, with the
    scrollback that was there before the cockpit mounted.

    ``JARVIS_BIPARTITE_FULLSCREEN=0`` returns to the inline cockpit for anyone
    who would rather keep native scrollback (or is debugging with the shell's
    output interleaved).
    """
    raw = os.environ.get("JARVIS_BIPARTITE_FULLSCREEN", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    # Unset → on, but only where an alternate screen exists at all. A pipe or
    # a dumb terminal has no smcup to issue, and claiming one there produces
    # escape soup in a log file rather than a cockpit.
    return _real_tty()


#: Where an overlay float sits, and how wide. ONE answer for every overlay.
#:
#: `top=1` rather than `ycursor=True`: an overlay is not attached to where the
#: caret happens to be, and one that moves while the operator reads a traceback is
#: hostile.
#:
#: `left=0, right=0` — FULL WIDTH — and that is a fix, not a preference. Inset at
#: `left=2` the float began at column 2, so the deck's GLYPH GUTTER (columns 0-2,
#: where `⏺`/`💭`/`●` live) stayed visible beside it. A FATAL traceback then read
#: as interleaved with the transcript underneath: deck bullets running down the
#: left margin of a crash report. Proven by rendering a float over a filled canvas
#: and finding `CAFATAL LINE A` — two canvas columns, then the overlay.
#:
#: An overlay that does not occlude is not an overlay. Blank lines WITHIN a
#: full-width float do paint over what is beneath them (verified), so opacity
#: comes free once the inset is gone.
_OVERLAY_FLOAT_POSITION = {"top": 1, "left": 0, "right": 0}


def _overlay_style(role: str) -> str:
    """Semantic role -> prompt_toolkit style, WITH a background. NEVER raises.

    Extracted from the panic overlay's own `_panic_style`, which had the whole
    translation argument right and only ever needed to be said once: Rich spells a
    colour `bright_yellow`, prompt_toolkit spells it `ansibrightyellow`, and both
    accept `#RRGGBB`. Assuming either dialect is how `class:panic` silently
    rendered as the default in the first place.

    The background is the half the panic version lacked. A foreground-only style
    leaves prompt_toolkit painting the float's cells with the DEFAULT background,
    which on a themed terminal is not necessarily the deck's — so the overlay read
    as text floating on the transcript rather than as a surface on top of it.
    """
    try:
        from backend.core.ouroboros.ui.semantic_tokens import style_for
        raw = (style_for(role) or "").strip()
        fg = ""
        if raw.startswith("#"):
            fg = raw
        elif raw:
            fg = "ansi" + raw.replace("_", "")
        # `bg:default` is deliberately NOT used: it means "whatever was there",
        # which is exactly the transparency being fixed.
        return f"bold bg:#0b0b0b fg:{fg}" if fg else "bold bg:#0b0b0b"
    except Exception:  # noqa: BLE001
        return "bold bg:#0b0b0b"



def _line_to_text(line: str) -> Any:
    """One transcript line -> a Rich ``Text``, decoded by its OWN encoding.

    The ring is a MARKUP ring: every producer pushes Rich markup and this seam
    called `Text.from_markup` on all of it. That held while markup was the only
    thing anyone pushed.

    Then the masthead arrived. `render_cockpit_header` returns an ANSI string — it
    was built for a prompt_toolkit ANSI window — and pushing it through a markup
    decoder shredded it: `from_markup` consumed the ESC, then printed the numeric
    payload of every truecolor SGR as literal text. On screen that is

        2;192;144;166;48;2;199;150;170m   ;156;66;48;2;47;43;17m   [0m

    smeared across the emblem. Two incompatible encodings meeting at one decoder.

    So the decoder SNIFFS instead of assuming. A line carrying ESC is ANSI and goes
    through `Text.from_ansi`, which is Rich's own decoder for exactly this; anything
    else is markup and is unchanged. That fixes the CLASS rather than the masthead:
    any producer may now push either encoding — a mirrored daemon line, a captured
    subprocess tail, a syntax-highlighted diff — and the ring stops caring.

    ANSI wins when a line somehow contains both, because a half-decoded escape is
    unreadable garbage while a literal `[bold]` is merely ugly. Mixing the two in
    one line is a producer bug either way, and this makes it look like one.

    NEVER raises: an undecodable line renders as plain text rather than taking the
    frame down with it.
    """
    try:
        from rich.text import Text
        if "\x1b" in line:
            return Text.from_ansi(line)
        return Text.from_markup(line)
    except Exception:  # noqa: BLE001
        try:
            from rich.text import Text
            return Text(str(line))
        except Exception:  # noqa: BLE001
            return str(line)


def _canvas_dimension(mux: Any = None) -> Any:
    """How much room the live canvas takes — CONTENT-SIZED, recomputed per frame.

    Returns a CALLABLE, because a `Dimension` computed once at build time cannot
    follow content that grows. prompt_toolkit accepts `AnyDimension`, so a callable
    is its own supported idiom, and it is the one `build_dynamic_rows` already uses
    for every other variable-height strip. The canvas was the last region still
    sized by a constant.

    ONE shape for both modes
    -----------------------
        Dimension(min=0, max=want, preferred=want)

    Which is EXACTLY what the inline branch already used — it simply had `want`
    hardcoded to 8 rather than derived. So this is the existing shape, given a real
    number, rather than a new mechanism.

    Every part of that triple is load-bearing, and two plausible alternatives are
    both wrong in ways only a render reveals:

    * ``Dimension(weight=1)`` (the old fullscreen branch) makes the canvas the
      greedy flex child. It is handed every leftover row, and `_anchor` then pads
      the TOP to push a short deck down against the prompt — which is precisely how
      a 12-row crest, thirty blank rows and four transcript lines ended up on one
      screen.
    * ``Dimension.exact(want)`` looks tidier and BREAKS THE COCKPIT. Verified: an
      exact 30 rows inside 10 available renders `Window too small...` and nothing
      else — HSplit refuses rather than clamps. ``min=0`` is what makes the region
      shrinkable, so a deck longer than the terminal degrades to the tail instead of
      to an error.
    * ``max=want`` is what stops the void coming back. Without a ceiling the child
      absorbs slack by weight — the trap `build_dynamic_rows` documents about an
      eight-row black slab nailed open above the prompt.

    Outside the alternate screen the derived height is additionally capped by
    ``JARVIS_BIPARTITE_LIVE_ROWS``: there the app does not own the screen, and a
    canvas that grows with its content would reserve that height on every repaint,
    turning the scrollback we just restored into a wall of blank rows.

    ``mux=None`` keeps the old signature working and falls back to the previous
    constant behaviour, so a caller that has no multiplexer to measure is not
    forced to invent one.
    """
    from prompt_toolkit.layout.dimension import Dimension

    try:
        rows = max(0, int(os.environ.get("JARVIS_BIPARTITE_LIVE_ROWS", "8")))
    except (TypeError, ValueError):
        rows = 8

    if mux is None:
        # No content to measure: preserve exactly what this function did before.
        if fullscreen_enabled():
            return Dimension(weight=1)
        return Dimension(min=0, max=rows, preferred=rows)

    def _dimension() -> Any:
        try:
            # IN THE ALTERNATE SCREEN THE CANVAS IS GREEDY, and that is Claude
            # Code's geometry: the input is pinned to the BOTTOM and the transcript
            # flows UPWARD into it, so the newest line is always the one directly
            # above where you type and the eye never travels.
            #
            # Safe as of the two-pass cache invalidation (#70288). Before it, a
            # greedy region lost its tail at small terminal heights because the
            # measurement pass's text was reused by the draw pass — content
            # rendered for a budget the window did not have, clipped from the
            # bottom. Content-sizing hid that by making the two passes agree; it
            # also collapsed the whole stack to the top of the screen, which is the
            # thing an operator actually complained about.
            #
            # The blank band content-sizing was reaching for was never the canvas's
            # fault — it came from mounting the crest as a FIXED region. That now
            # lives in the transcript (`seed_masthead`).
            if fullscreen_enabled():
                return Dimension(weight=1)
            # Inline, the app does not own the screen: a greedy canvas would reserve
            # its height every repaint and turn the restored scrollback into a wall
            # of blanks. There it stays a content-sized bounded region.
            want = max(1, min(int(mux.content_height()), rows))
            return Dimension(min=0, max=want, preferred=want)
        except Exception:  # noqa: BLE001
            # Degrade to the greedy region rather than to nothing: a canvas that
            # cannot size itself must still be able to show the transcript.
            return Dimension(weight=1)

    return _dimension


def bottom_anchor_enabled() -> bool:
    """Does the transcript grow UPWARD from the prompt?

    Claude Code's geometry: the input box is fixed at the bottom and the
    transcript fills toward it, so the newest line is always the one
    directly above where you type and your eye never travels. A
    top-aligned canvas puts the conversation at row 0 and leaves a void
    between it and the prompt — which is what an operator reads as "it
    does not flow like Claude".

    Only meaningful in the alternate screen, where the canvas owns every
    remaining row. Inline, the canvas is a bounded live region that must
    still collapse to nothing when idle, so padding it would nail an
    empty block open. ``JARVIS_BIPARTITE_BOTTOM_ANCHOR=0`` restores the
    top-aligned canvas."""
    raw = os.environ.get("JARVIS_BIPARTITE_BOTTOM_ANCHOR", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return fullscreen_enabled()


def toolbar_above_prompt() -> bool:
    """Does the pulse row sit above the ``❯`` input, or beneath it?

    Above, by default. The toolbar carries the organism's liveness — spinner,
    elapsed, tokens — and Claude Code puts that immediately above the input
    box, adjacent to the transcript line it continues. prompt_toolkit's
    ``bottom_toolbar`` idiom put it underneath, which is where a shell puts
    ITS chrome, and the difference is whether the operator reads the pulse as
    the organism working or as furniture.

    ``JARVIS_BIPARTITE_TOOLBAR_BELOW=1`` restores the footer position for a
    surface that genuinely wants a footer (persistent key hints rather than a
    pulse). NEVER raises.
    """
    raw = os.environ.get("JARVIS_BIPARTITE_TOOLBAR_BELOW", "").strip().lower()
    return raw not in ("1", "true", "yes", "on")


def _canvas_max_lines() -> int:
    try:
        return canvas_history_lines()
    except (TypeError, ValueError):
        return _DEFAULT_MAX_LINES


def _terminal_size() -> "tuple":
    """``(columns, lines)`` from the live terminal. NEVER raises.

    The fallback is only for a genuinely sizeless stdout (a pipe, a CI
    runner). It is deliberately NOT the old 100x24 default: a value that
    silently stands in for a real measurement is how a hardcoded 24 survived
    long enough to clip the deck.
    """
    try:
        import shutil
        sz = shutil.get_terminal_size(fallback=(100, 24))
        return (int(sz.columns), int(sz.lines))
    except Exception:  # noqa: BLE001
        return (100, 24)


class BipartiteLayout:
    """The TUI multiplexer: a bounded Zone-1 telemetry ring + Rich composition of
    the two zones + resize recompute. Pure/headless-testable — the live
    prompt_toolkit Application is built separately (:func:`build_bipartite_application`)
    and only on a real TTY."""

    def __init__(
        self,
        *,
        registry: Any = None,
        max_lines: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        invalidate: Optional[Callable[[], None]] = None,
        title: str = "◇ O+V · proactive canvas",
    ) -> None:
        # Reuse the bounded buffer primitive (DRY) — never the Live renderer.
        from backend.core.ouroboros.battle_test.split_layout import RegionBuffer

        # In the alternate screen this ring is the ONLY history that exists,
        # so it is sized for a session rather than for a tail sitting beneath
        # a terminal that still had its own scrollback.
        self._buffer = RegionBuffer(
            name="canvas", maxlen=max_lines or _canvas_max_lines(),
        )
        self._viewport = CanvasViewport()
        self._registry = registry
        # Seeded from the LIVE terminal, not from a constant. `height=24`
        # was a hardcoded dimension every caller that omitted it inherited
        # forever: `_line_budget` deducts chrome from `self._height`, so a
        # frozen 24 clipped the deck identically at LINES=30 and LINES=120,
        # and `scroll_metrics` handed the scroll keys that same stale budget
        # — the deck could neither show its tail nor be scrolled to it.
        #
        # SIGWINCH keeps it true afterwards (see `handle_sigwinch`, wired at
        # application build). Seeding matters independently: SIGWINCH fires
        # on CHANGE, so a cockpit booted into an 80x50 terminal that is never
        # resized would otherwise run its whole life believing it had 24 rows.
        seed_w, seed_h = _terminal_size()
        self._width = max(10, int(seed_w if width is None else width))
        self._height = max(3, int(seed_h if height is None else height))
        self._invalidate = invalidate
        self._title = title
        self._resize_count = 0
        self._sprite: Any = None            # the DORMANT/WAKING hero animation
        # The seed above is an ESTIMATE: the terminal's size stands in for the
        # canvas's own allotment because nothing has measured the canvas yet.
        # It is a fair guess and it is the best available before the first frame
        # — a cockpit still has to draw something. `observe_allotment` replaces
        # it with the framework's real number on that first render and flips
        # this, so no reader ever mistakes the stand-in for the measurement.
        self._allotment_measured = False
        # Masthead claims, and the lock that makes the claim atomic with the push.
        # A plain bool plus a later `if not seeded` is check-then-act: two boot
        # threads both read False before either writes and both seed.
        self._masthead_lock = threading.RLock()
        self._masthead_seeded: set = set()

    # -- the Ouroboros hero animation -----------------------------------

    def attach_sprite(self, sprite: Any) -> None:
        """Attach the Async Sprite Engine and hang its frame advance on THIS
        canvas's invalidate (DRY — the same hook the ReactiveTheme uses; one
        rendering pipeline). The animated logo is shown while the feed is idle."""
        self._sprite = sprite
        try:
            if sprite is not None and self._invalidate is not None:
                sprite.set_invalidate(self._invalidate)
        except Exception:  # noqa: BLE001
            pass

    def _hero_active(self) -> bool:
        """Show the animated logo when nothing has happened yet — the DORMANT /
        WAKING centrepiece — and hand the canvas to the feed the instant real
        telemetry arrives."""
        if self._sprite is None:
            return False
        try:
            if self.line_count() > 0:
                return False
            from backend.core.ouroboros.ui.theme import get_reactive_theme, UIState
            return get_reactive_theme().state in (UIState.DORMANT, UIState.HEALTHY)
        except Exception:  # noqa: BLE001
            return self.line_count() == 0

    # -- registry (lazy, DRY) -------------------------------------------

    def _reg(self):
        if self._registry is None:
            try:
                from backend.core.ouroboros.governance.event_breadcrumb_registry import (
                    build_default_registry,
                )
                self._registry = build_default_registry()
            except Exception:  # noqa: BLE001
                self._registry = _NullRegistry()
        return self._registry

    # -- the sink (Zone 1) — same formatting as /breadcrumbs ------------

    def emit(self, event_type: str, payload: dict) -> None:
        """Format ONE event exactly as the unified router does (describe → glyph/
        color, render → severity/text) and push it into the Zone-1 ring. Triggers
        a re-render. Never raises."""
        try:
            reg = self._reg()
            desc = reg.describe(event_type)
            _sev, text = reg.render(event_type, dict(payload or {}))
            glyph = getattr(desc, "glyph", "·")
            color = getattr(desc, "color", "white")
            self._buffer.push(f"[{color}]{glyph} {text}[/{color}]")
        except Exception:  # noqa: BLE001 — a bad event never breaks the canvas
            logger.debug("[Bipartite] emit failed", exc_info=True)
        # Feed the Reactive Theme Singleton — a state-mapped event mutates the
        # canvas border accent in place (DORMANT/ARMED/SOAKING/DEGRADED/HEALTHY).
        try:
            from backend.core.ouroboros.ui.theme import get_reactive_theme
            get_reactive_theme().on_event(event_type, dict(payload or {}))
        except Exception:  # noqa: BLE001
            pass
        self._invalidate_now()

    async def aemit(self, event_type: str, payload: dict) -> None:
        """Async emit — yields to the event loop FIRST (proving it never blocks the
        input reader), then appends. Never raises."""
        import asyncio
        await asyncio.sleep(0)   # cooperative yield — the decoupling proof
        self.emit(event_type, payload)

    def set_streaming_tail(self, text: object) -> None:
        """The line currently being WRITTEN, rendered inside the deck.

        Not a push: this is a view of something still happening, and a
        pending line that entered the ring would be evicted by maxlen
        mid-sentence and survive as history if the stream died. `as_text`
        composes it last on every frame, so it lands at the end of the
        transcript exactly where it will come to rest — which is CC's
        actual mechanic, rather than a strip below the deck approximating
        it. NEVER raises.
        """
        try:
            self._buffer.set_pending(text)
        except Exception:  # noqa: BLE001
            return
        self._invalidate_now()

    def push_raw(self, markup_line: str) -> None:
        """Push a pre-formatted Rich-markup line (e.g. piped Sentinel stdout).
        Never raises."""
        try:
            self._buffer.push(str(markup_line))
        except Exception:  # noqa: BLE001
            pass
        self._invalidate_now()

    # -- rendering ------------------------------------------------------

    def observe_allotment(self, width: Optional[int],
                          height: Optional[int]) -> bool:
        """Record the rows and columns this canvas was ACTUALLY given.

        The whole clipping defect in one sentence: the canvas was sizing itself
        against the TERMINAL when what bounds it is the REGION its parent
        `HSplit` hands it, and those are the same number only in a cockpit with
        no header, no toolbar, no status row, no search bar, no pending strip,
        no turn row and no agent view.

        Measured, at 200x60 with a 12-row crest mounted: the mux believed it had
        39 rows and produced 39 lines into a window that was given 8. Thirty-one
        lines were dropped — and because a `Window` draws a content block from
        its TOP, the rows it kept were the OLDEST of them. The operator was left
        watching a window parked thirty-one lines behind the live tail, which is
        the precise reason the last beats of a script "never rendered".

        Predicting the answer is what failed, and every version of predicting
        fails the same way. `_canvas_fragments` reserved exactly one row for
        chrome (`size.rows - 1`), which was true when the cockpit was a canvas
        and a prompt; each strip added since made it wronger, silently, because
        nothing compared the prediction to reality. Deducting the strips instead
        would mean this class re-deriving its parent's layout arithmetic — two
        implementations of one truth, drifting the next time a row is added.

        So it is OBSERVED, at the seam the framework already provides for it:
        `UIControl.create_content(width, height)` is prompt_toolkit telling a
        control exactly what it has. That is authoritative rather than inferred,
        it needs no knowledge of what the siblings cost, and it stays correct
        through a resize, a palette opening, a strip appearing mid-session, and
        any row some future arc adds — none of which this method has to know
        about.

        Returns whether the allotment changed. NEVER raises.

        A zero or ``None`` height is REFUSED rather than recorded. Those are
        real states — a `ConditionalContainer` collapsed, a control asked to
        measure rather than draw — and treating them as a budget would render an
        empty deck and then invalidate on the emptiness. The last good
        measurement is a better answer than a degenerate fresh one.
        """
        try:
            changed = False
            if width is not None and int(width) > 0:
                new_width = max(10, int(width))
                if new_width != self._width:
                    self._width, changed = new_width, True
            if height is not None and int(height) > 0:
                new_height = max(3, int(height))
                if new_height != self._height:
                    self._height, changed = new_height, True
                # Provenance is set even when the number is unchanged: an
                # estimate that happens to equal the measurement is still an
                # estimate until something measures it, and `allotment_measured`
                # is what tells a reader which one they are looking at. Same
                # rule the advisor follows about a cap that coincides with a
                # real blast radius.
                self._allotment_measured = True
            return changed
        except (TypeError, ValueError):
            return False

    @property
    def allotment_measured(self) -> bool:
        """Has a real render told us our size, or are we still guessing?

        Exposed because "38 rows, measured" and "38 rows, assumed" are different
        facts, and a surface that cannot tell them apart is how a hardcoded 24
        survived long enough to be found by accident. The estimate is a
        legitimate fallback before the first frame; it must simply never wear a
        measurement's authority.
        """
        return bool(getattr(self, "_allotment_measured", False))

    def seed_masthead(self, render: Any, *, key: str = "masthead") -> int:
        """Push the identity block into the TRANSCRIPT, exactly once. NEVER raises.

        Claude Code's banner is not a fixed region — it is the top of the
        scrollback. It sits directly above the first thing that happens and scrolls
        away as work arrives, because a masthead is only interesting until there is
        something better in its place.

        Mounting it as `header`/`header_height` instead is what stranded the emblem
        at row 0 while a bottom-anchored deck hugged the prompt, with a band between
        that belonged to neither. As transcript content it needs no reserved rows,
        cannot strand itself, and participates in the scrollback the operator pages
        back through.

        IDEMPOTENT, and that is the load-bearing part
        --------------------------------------------
        Boot is exactly when a terminal emits a flurry of SIGWINCH: the app mounts,
        the alternate screen is claimed, the crest warms off-thread, and any of
        those can drive a layout rebuild. A masthead pushed per rebuild would stack
        three emblems into the ring, and the ring is append-only — nothing would
        take them back out.

        Guarded by a claim under the SAME lock that guards the push, not by a
        check-then-act: two threads that both read "not yet seeded" before either
        writes would both proceed, which is the classic double-seed. The flag is
        keyed, so a surface with a genuinely different banner can seed its own
        without either being able to suppress the other.

        Returns the number of lines seeded — 0 when it was already done, which lets
        a caller tell "I seeded" from "someone beat me to it" without inspecting the
        ring.
        """
        try:
            with self._masthead_lock:
                if key in self._masthead_seeded:
                    return 0
                # Claimed BEFORE rendering. Rendering the crest is slow enough to
                # be preempted, and a claim taken afterwards would leave the whole
                # render inside the race it exists to close.
                self._masthead_seeded.add(key)
                text = render() if callable(render) else render
                lines = str(text or "").split("\n")
                # Trailing blanks from a renderer that ends with a newline would
                # open a gap between the emblem and the first event — the very
                # thing this exists to close.
                while lines and not lines[-1].strip():
                    lines.pop()
                if not lines:
                    # Nothing to show: release the claim so a later call with a
                    # warmed renderer can still seed. An empty masthead is not a
                    # seeded masthead, and holding the claim would make a cold
                    # boot permanently emblem-less.
                    self._masthead_seeded.discard(key)
                    return 0
                for line in lines:
                    self._buffer.push(line)
                # One blank BETWEEN the masthead and the feed — the deck's only
                # grouping cue, the same separator `compose_live_script` puts
                # before each action.
                self._buffer.push("")
            self._invalidate_now()
            return len(lines) + 1
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] masthead seed degraded", exc_info=True)
            return 0

    def masthead_seeded(self, key: str = "masthead") -> bool:
        """Has this banner already been laid down? For tests and for a caller
        that wants to skip an expensive render it does not need."""
        with self._masthead_lock:
            return key in self._masthead_seeded

    def content_height(self) -> int:
        """Rows the canvas WANTS — its content plus its own chrome. NEVER raises.

        This is what makes the crest sit directly above the deck. The canvas used
        to be `Dimension(weight=1)`, the greedy flex child, so it was handed every
        leftover row and `_anchor` padded the TOP to push its few lines down
        against the prompt. With a 12-row crest mounted the result was an emblem,
        thirty blank rows, and four lines of transcript: two islands with a void
        between them.

        Derived from the RING, never from `_line_budget()`, and that is the whole
        reason this is a separate method rather than a call to the budget. The
        budget is `allotment - chrome`, and the allotment is now what this returns,
        so asking the budget would close a loop: 4 lines → allot 4 → budget 3 →
        show 3 → allot 3 → budget 2 … a collapse spiral, one row per frame.
        Reading `len(snapshot())` breaks it, because the ring's size does not depend
        on how tall the canvas is.

        Clamped by the TERMINAL, not by ``self._height``, for the same reason: the
        observed allotment is this function's own output, and clamping a value by
        itself is the loop again wearing a hat. The terminal is an external fact.

        An IDLE canvas keeps room for its resting hero. Collapsing to one row when
        nothing has happened would replace the animated emblem — the DORMANT
        centrepiece — with a single line of idle text, which is a regression
        disguised as tightness.
        """
        try:
            chrome = 4 if os.environ.get(
                "JARVIS_BIPARTITE_BORDER", "",
            ).strip().lower() in ("1", "true", "yes", "on") else 1
            _cols, term_rows = _terminal_size()
            # Leave the surrounding chrome (prompt, toolbar, strips, a header) room
            # to exist. Without a ceiling a long deck would claim the whole screen
            # and prompt_toolkit would shrink the prompt to nothing.
            ceiling = max(3, int(term_rows) - 2)
            if self._hero_active():
                hero = getattr(self._sprite, "rows", None)
                want = int(hero) if isinstance(hero, int) and hero > 0 else 12
                return max(3, min(want + chrome, ceiling))
            lines = len(self._buffer.snapshot())
            if lines <= 0:
                # Not zero: the idle line still needs somewhere to be drawn.
                return min(1 + chrome, ceiling)
            return max(1, min(lines + chrome, ceiling))
        except Exception:  # noqa: BLE001
            return max(3, self._height)

    def _line_budget(self) -> int:
        """Rows the canvas may draw into, frame chrome deducted.

        ``self._height`` is the canvas's OWN allotment — observed from the
        framework once a frame has rendered (see :meth:`observe_allotment`),
        estimated from the terminal before that. The chrome deduction is
        unchanged and still means what it always meant: rows this canvas spends
        on its own panel border and title. It was never wrong; it was being
        subtracted from the wrong number.
        """
        chrome = 4 if os.environ.get(
            "JARVIS_BIPARTITE_BORDER", "",
        ).strip().lower() in ("1", "true", "yes", "on") else 1
        return max(1, self._height - chrome)

    def scroll_metrics(self) -> tuple:
        """``(total, budget)`` — what the scroll keys clamp against."""
        try:
            return len(self._buffer.snapshot()), self._line_budget()
        except Exception:  # noqa: BLE001
            return 0, 1

    def _visible_lines(self) -> List[str]:
        """The screenful the operator is LOOKING AT — the live tail while
        following, an older window once they scroll back.

        The status row replaces the topmost visible line rather than being
        overlaid: losing one row of telemetry is a fair price for always
        knowing whether you are watching "now", and it costs nothing while
        following, when there is no status to show.
        """
        try:
            budget = self._line_budget()
            snap = self._buffer.snapshot()
            if not scrollback_enabled():
                return self._anchor(list(snap[-budget:]))
            visible, above, below = self._viewport.window(
                # push_count, not len(): once the ring saturates its length
                # stops changing while the content keeps moving.
                snap, budget, appended=self._buffer.push_count,
            )
            status = self._viewport.status(above, below)
            if status:
                return self._anchor(list(visible[1:]) + [
                    f"[reverse dim] {status} [/reverse dim]",
                ])
            # At the LIVE TAIL, say that history exists — once.
            #
            # `status` is the counterpart and only ever renders while
            # SCROLLED: it tells a reader how to get back, and nothing told
            # them they could leave. The alternate screen took the terminal's
            # own scrollback, so an operator who does not know the key has no
            # way to reach 20,000 retained lines. It costs the same one row
            # the status does, and only until they scroll once.
            hint = self._viewport.tail_hint(above)
            if hint:
                return self._anchor(list(visible[1:]) + [
                    f"[{_SEM['verbose']}] {hint} [/]",
                ])
            return self._anchor(list(visible))
        except Exception:  # noqa: BLE001
            return []

    def _anchor(self, lines: List[str]) -> List[str]:
        """Pad the TOP so the newest line lands against the prompt.

        Padding rather than a Rich alignment because the canvas body is a
        Group of independently-styled lines with no shared container to
        align — and because blank leading rows are exactly what the
        alternate screen shows above a short conversation anyway. NEVER
        raises: an unpadded canvas is merely top-aligned, not broken."""
        try:
            if not lines or not bottom_anchor_enabled():
                # An EMPTY canvas keeps its idle message and its resting
                # hero: padding zero lines to a screenful would replace
                # "the organism rests" with a wall of blanks.
                return lines
            budget = self._line_budget()
            missing = budget - len(lines)
            return ([""] * missing) + lines if missing > 0 else lines
        except Exception:  # noqa: BLE001
            return lines

    def render_canvas(self) -> Any:
        """Zone 1 — a rounded ``rich.panel.Panel`` of the tail telemetry. Never
        raises (returns an empty panel on any failure)."""
        try:
            from rich.console import Group
            from rich.panel import Panel
            from rich.text import Text
            from rich.box import ROUNDED

            lines = self._visible_lines()
            # DORMANT/WAKING hero: the animated Ouroboros chase is the centrepiece
            # until real telemetry arrives, then the feed takes over.
            if self._hero_active():
                try:
                    frame = self._sprite.current_frame()
                    body = Group(frame, Text("\n  the organism rests — awaiting intent",
                                             style="bright_black", justify="center"))
                except Exception:  # noqa: BLE001
                    body = Text("  O + V", style="bold #5EE06A", justify="center")
            elif lines:
                body = Group(*[_line_to_text(ln) for ln in lines])
            else:
                body = Text("  idle — waiting for the organism to act", style="bright_black")
            n = self._buffer.line_count if hasattr(self._buffer, "line_count") else len(lines)
            # CC-style default (operator mandate 2026-07-23): BORDERLESS — the
            # canvas is open flowing content; structure comes from typography,
            # and the reactive state accent lives in the header's status dot.
            # JARVIS_BIPARTITE_BORDER=1 restores the framed Panel (whose border
            # then carries the reactive accent as before).
            if os.environ.get("JARVIS_BIPARTITE_BORDER", "").strip().lower() in (
                "1", "true", "yes", "on",
            ):
                border = "cyan"
                try:
                    from backend.core.ouroboros.ui.theme import get_reactive_theme
                    border = get_reactive_theme().active_border_style() or "cyan"
                except Exception:  # noqa: BLE001
                    border = "cyan"
                return Panel(
                    body, title=self._title, title_align="left",
                    subtitle=f"[{_SEM['verbose']}]{n} events[/]", subtitle_align="right",
                    box=ROUNDED, border_style=border, padding=(0, 1),
                    height=max(3, self._height - 3),
                )
            return body
        except Exception:  # noqa: BLE001
            try:
                from rich.panel import Panel
                return Panel("", title=self._title)
            except Exception:  # noqa: BLE001
                return None

    def render_deck(self) -> Any:
        """Zone 2 — the visual Command Deck row (the live editable prompt is
        prompt_toolkit's; this is the Rich-side representation for composition +
        headless preview). Never raises."""
        try:
            from rich.text import Text
            t = Text()
            t.append("› ", style="cyan bold")
            t.append("type a verb or plain text", style="bright_black")
            t.append("   ·   ", style="bright_black")
            t.append("⌃C", style="cyan")
            t.append(" detach · ", style="bright_black")
            t.append("wake", style="cyan")
            t.append(" for voice", style="bright_black")
            return t
        except Exception:  # noqa: BLE001
            return "› "

    def render_layout(self) -> Any:
        """The strictly-zoned ``rich.layout.Layout``: Zone 1 (canvas, fills) over
        Zone 2 (deck, one row). Never raises."""
        try:
            from rich.layout import Layout

            root = Layout(name="root")
            root.split_column(
                Layout(self.render_canvas(), name="canvas", ratio=1),
                Layout(self.render_deck(), name="deck", size=1),
            )
            return root
        except Exception:  # noqa: BLE001
            return self.render_canvas()

    def _render_to_ansi(self, renderable: Any) -> str:
        """Render any Rich renderable to an ANSI string at the current terminal
        dims — headless (a StringIO console), so tests + the prompt_toolkit ANSI
        window share ONE render path. Never raises."""
        try:
            from io import StringIO
            from rich.console import Console

            buf = StringIO()
            Console(
                file=buf, force_terminal=True, color_system="truecolor",
                width=self._width, height=self._height, highlight=False, emoji=True,
            ).print(renderable)
            return buf.getvalue()
        except Exception:  # noqa: BLE001
            return ""

    def render_layout_ansi(self) -> str:
        return self._render_to_ansi(self.render_layout())

    def render_canvas_ansi(self) -> str:
        return self._render_to_ansi(self.render_canvas())

    # -- resize (SIGWINCH) ----------------------------------------------

    def on_resize(self, width: int, height: int) -> None:
        """Recompute the zone boundaries for a new terminal size. Clamped so a
        degenerate (1×1) resize never crashes the render. Never raises."""
        try:
            self._width = max(10, int(width))
            self._height = max(3, int(height))
            self._resize_count += 1
        except (TypeError, ValueError):
            pass
        self._invalidate_now()

    def handle_sigwinch(self, *_args: Any) -> None:
        """OS SIGWINCH adapter — read the live terminal size and recompute. Safe to
        register as a signal handler. Never raises."""
        try:
            import shutil
            sz = shutil.get_terminal_size(fallback=(self._width, self._height))
            self.on_resize(sz.columns, sz.lines)
        except Exception:  # noqa: BLE001
            pass

    # -- accessors ------------------------------------------------------

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def resize_count(self) -> int:
        return self._resize_count

    def line_count(self) -> int:
        try:
            return int(self._buffer.line_count)
        except Exception:  # noqa: BLE001
            return 0

    def set_invalidate(self, fn: Optional[Callable[[], None]]) -> None:
        self._invalidate = fn

    def _invalidate_now(self) -> None:
        if self._invalidate is not None:
            try:
                self._invalidate()
            except Exception:  # noqa: BLE001
                pass


class _NullRegistry:
    """Fallback when the real registry can't import — keeps emit total."""

    class _D:
        glyph = "·"
        color = "white"
        severity = 1

    def describe(self, _et: str):
        return self._D()

    def render(self, et: str, _payload: dict) -> Tuple[int, str]:
        return 1, str(et)


# ---------------------------------------------------------------------------
# Active-canvas registry — the DRY seam the event router redirects into
# ---------------------------------------------------------------------------


_active_canvas: Optional[BipartiteLayout] = None


def set_active_canvas(mux: Optional[BipartiteLayout]) -> None:
    """Register (or clear) the live Zone-1 sink. When set, the unified event
    router pushes into it instead of the flowing console (the sink redirect)."""
    global _active_canvas
    _active_canvas = mux


def get_active_canvas() -> Optional[BipartiteLayout]:
    """The live Zone-1 sink, or ``None`` (legacy flowing-console mode)."""
    return _active_canvas


# ---------------------------------------------------------------------------
# The live prompt_toolkit full-screen Application (TTY only)
# ---------------------------------------------------------------------------


def _palette_height() -> int:
    """Rows the `/` menu may occupy. Bounded so a 76-verb palette cannot
    swallow the canvas — it scrolls instead."""
    try:
        return max(3, min(24, int(
            os.environ.get("JARVIS_PALETTE_HEIGHT", "12") or 12,
        )))
    except (TypeError, ValueError):
        return 12


def _palette_multicolumn() -> bool:
    """Multi-column menu (``JARVIS_PALETTE_MULTICOLUMN``, default off).

    Single-column is the default deliberately: it is the layout that shows
    ``display_meta`` beside each verb, and a 60-verb table whose names are
    mostly self-explanatory is worth far less than one that says what each
    verb DOES. Multi-column fits more names and drops the descriptions."""
    return (os.environ.get("JARVIS_PALETTE_MULTICOLUMN", "0")
            .strip().lower() in ("1", "true", "yes", "on"))



def _mount_region_layout(rows: "list") -> Any:
    """Derive the root from the arbiter, or fall back to the plain HSplit.

    NEVER raises. This is the root container of the operator's daily surface;
    a status-quo fallback is always available and is strictly better than a
    cockpit that will not boot.

    `rows[0]` is the canvas — the deck region. The prompt and toolbar rows are
    NOT regions: they are chrome that must survive every arbitration, so they
    stay in the outer HSplit rather than becoming columns the arbiter could
    decide to hide at 40 columns.
    """
    from prompt_toolkit.layout import HSplit
    try:
        from backend.core.ouroboros.battle_test.region_layout import (
            RegionSources, dynamic_region_container, region_layout_enabled,
        )
        from backend.core.ouroboros.battle_test.viewport_arbiter import (
            ViewportArbiter, arbiter_enabled,
        )
        if not (region_layout_enabled() and arbiter_enabled()) or not rows:
            return HSplit(rows)

        body, chrome = rows[0], list(rows[1:])
        # Only `deck` has a source today. `lanes` and `transcript` return None,
        # and `build_region_tree` DROPS a region whose factory yields None —
        # so an unsupplied region costs nothing rather than drawing an empty
        # panel that looks like a bug.
        sources = RegionSources(deck=lambda: body)
        derived = dynamic_region_container(ViewportArbiter(), sources)
        return HSplit([derived] + chrome)
    except Exception:  # noqa: BLE001
        logger.debug("[Bipartite] region layout mount degraded", exc_info=True)
        return HSplit(rows)

#: The cockpit Application's repaint period.
#:
#: Named because more than the Application depends on it: `serpent_rule`
#: advances whole cells per FRAME, so it has to be told the frame period or
#: its motion beats against the real one. A literal in two places is how
#: those two silently disagree.
_REFRESH_INTERVAL_S: float = 0.1


def build_dynamic_rows(rows: Callable[[], Any]) -> Any:
    """A strip that is EXACTLY as tall as whatever it currently holds.

    ONE geometry primitive for every variable-height strip the cockpit grows
    — the agent view, the search bar, and whatever comes next. Each of those
    is the same two problems (how tall am I, do I exist at all) and solving
    them per strip is how a layout ends up with three subtly different answers
    to "collapse when empty".

    ``Dimension.exact``, computed per repaint, for the reason the prompt
    learned the hard way: ``HSplit`` hands each child its preferred size and
    distributes the leftover by weight, so a child whose ``max`` exceeds its
    ``preferred`` absorbs the slack. A range here would nail an eight-row
    black slab open above the prompt on an idle cockpit.

    Paired with a ``ConditionalContainer`` so zero agents costs zero rows —
    an idle cockpit must be exactly as tall as it was before the agent view
    existed, and a Window rendering an empty string still occupies a line.

    ``rows`` is a callable returning the CURRENT roster lines, so the source
    (a local singleton in-process, a heartbeat snapshot remotely) is the
    caller's concern and this container never learns which one it is drawing.
    NEVER raises — returns None, and the layout omits the row.
    """
    try:
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.layout import ConditionalContainer, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension

        def _current() -> list:
            try:
                return [str(x) for x in (rows() or ())]
            except Exception:  # noqa: BLE001
                return []

        def _fragments() -> Any:
            try:
                lines = _current()
                if not lines:
                    return []
                from prompt_toolkit.formatted_text import ANSI
                return ANSI("\n".join(lines))
            except Exception:  # noqa: BLE001
                return []

        def _height() -> Any:
            try:
                return Dimension.exact(max(1, len(_current())))
            except Exception:  # noqa: BLE001
                return Dimension.exact(1)

        return ConditionalContainer(
            content=Window(
                content=FormattedTextControl(_fragments, focusable=False),
                height=_height, wrap_lines=False,
            ),
            filter=Condition(lambda: bool(_current())),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Bipartite] dynamic rows unavailable", exc_info=True)
        return None


#: The agent view was the first caller and named the primitive; the name is
#: kept so its regression spine keeps pointing at the thing it pins.
build_agent_row = build_dynamic_rows


def build_bipartite_application(
    mux: BipartiteLayout,
    *,
    on_accept: Callable[[str], Any],
    extra_key_bindings: Any = None,
    toolbar: Optional[Callable[[], str]] = None,
    header: Optional[Callable[[], str]] = None,
    header_height: int = 0,
    completer: Any = None,
    history: Any = None,
    auto_suggest: Any = None,
    turn_spinner: Any = None,
    agent_rows: Optional[Callable[[], Any]] = None,
    search_rows: Optional[Callable[[], Any]] = None,
    status_rows: Optional[Callable[[], Any]] = None,
    pending_rows: Optional[Callable[[], Any]] = None,
    stream_rows: Optional[Callable[[], Any]] = None,
    queue_rows: Optional[Callable[[], Any]] = None,
    panic_rows: Optional[Callable[[], Any]] = None,
    #: The archived-diff overlay (`diff_overlay.DiffOverlayController.rows`).
    #: Rows arrive PRE-COLOURED by Rich, so this float renders them as ANSI
    #: rather than under a single prompt_toolkit style.
    diff_rows: Optional[Callable[[], Any]] = None,
    # NOTE: no `on_mux` here, deliberately. It belongs to `run_bipartite_repl`,
    # which CONSTRUCTS the multiplexer and therefore has something to hand
    # back. A caller of this function already holds the mux — it passes it in
    # as the first argument — so an `on_mux` callback here could only ever
    # return the caller's own object to it.
    #
    # It was in this signature, read by nothing, passed by nobody: dead API
    # surface that invited exactly one mistake, and duly caused it. Planning
    # this work, I recommended "pass `on_mux` in the demo so in-transcript
    # streaming demos its real mechanic" — which would have silently done
    # nothing, because `ov demo live` calls THIS function directly. Removed
    # rather than documented; a parameter that cannot be correct to pass should
    # not be offered.
    serpent_active: Optional[Callable[[], bool]] = None,
) -> Any:
    """Construct the full-screen ``prompt_toolkit.Application``: Zone 1 an ANSI
    window fed from ``mux.render_canvas_ansi()`` (re-rendered each frame, so
    SIGWINCH auto-syncs the dims), Zone 2 the anchored ``> `` prompt. The app's
    atomic render loop is the ``patch_stdout``-equivalent — background emits into
    Zone 1 never corrupt the prompt. Caller must gate on a real TTY
    (``real_stdout_isatty``). Returns the Application. Never raises out."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import Layout as PTLayout, HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.widgets import TextArea
    from prompt_toolkit.key_binding import KeyBindings

    def _canvas_fragments():
        # NO terminal probe here any more, and no `size.rows - 1`.
        #
        # This used to re-sync the mux to `app.output.get_size()` minus one row
        # "for the deck", which is a PREDICTION of what the surrounding HSplit
        # would leave over. It was right while the cockpit was a canvas and a
        # prompt, and every strip added since made it wronger by a row or twelve
        # — silently, because nothing ever compared it to what the canvas was
        # actually given. `_MeasuredCanvasControl` below now supplies the real
        # number before this callable runs, so by here the budget is already
        # correct for THIS frame.
        return ANSI(mux.render_canvas_ansi())

    class _MeasuredCanvasControl(FormattedTextControl):
        """A canvas control that tells the mux how big it really is.

        `create_content(width, height)` is prompt_toolkit handing a control its
        exact allotment for the frame about to be drawn. Recording it here — and
        crucially, BEFORE delegating to ``super()``, which is what invokes the
        text callable — means the very frame being measured is also the frame
        that renders at the corrected size. There is no lag and no first-frame
        flash of clipped content.

        The alternative was to read ``window.render_info`` after a render, which
        is the same measurement one frame late; every resize would then show a
        single wrong frame, and a resize is exactly when an operator is looking.

        A resize is published through the mux's existing invalidate so any
        surface hanging off it repaints — the same hook `attach_sprite` and the
        ReactiveTheme already use. Only on CHANGE: invalidating every frame from
        inside a render is a repaint loop, and `observe_allotment` returning a
        changed-flag is what keeps that honest.
        """

        def create_content(self, width, height=None):  # noqa: ANN001
            try:
                if mux.observe_allotment(width, height):
                    # The text this control produces DEPENDS on the allotment we
                    # just learned, so the cached text is now stale. Clearing it is
                    # the whole reason a changed allotment is worth detecting.
                    #
                    # prompt_toolkit renders a frame in TWO passes: a measurement
                    # pass with `height=None`, then the draw pass with the real
                    # height. `create_content` pulls its text through
                    # `_get_formatted_text_cached`, so without this the draw pass
                    # REUSES the measurement pass's text — computed against
                    # whatever budget was in effect before anything was measured.
                    #
                    # Measured, greedy canvas, 400 lines in a 30-row terminal:
                    #
                    #   asked_h=None  mux.h=24 (stale seed)  ->  24 lines
                    #   asked_h=12    mux.h=12 (correct)     ->  24 lines  <-- stale
                    #
                    # Twenty-four lines painted into twelve rows, clipped from the
                    # BOTTOM: the newest output lost. Recording the right height was
                    # never enough — #70280's claim of a "zero-lag, same-frame"
                    # observation held only while the two passes happened to agree,
                    # which is why content-sizing hid this and greedy exposed it.
                    for attr in ("_fragment_cache", "_content_cache"):
                        cache = getattr(self, attr, None)
                        clear = getattr(cache, "clear", None)
                        if callable(clear):
                            clear()
                    mux._invalidate_now()
            except Exception:  # noqa: BLE001 — never break a frame to measure it
                logger.debug("[Bipartite] allotment observation degraded",
                             exc_info=True)
            return super().create_content(width, height)

    _canvas_control = _MeasuredCanvasControl(_canvas_fragments,
                                             focusable=False)
    # Click-to-expand. `mouse_support` was already on — the wheel scrolled —
    # but there were no MouseEventType handlers anywhere, so every click fell
    # on the floor. Installed on THIS control because it is the one that draws
    # the transcript; the rows it resolves against are the rendered ANSI, so
    # the panel border and the anchor padding need no arithmetic.
    #
    # A click SUBMITS `/expand <ref>` through `on_accept` — the same callable
    # a typed line goes through — so every surface routes a click exactly the
    # way it already routes typing, and the whole `/expand` ref family works
    # without any of it being reimplemented for the mouse.
    try:
        from backend.core.ouroboros.battle_test.canvas_mouse import (
            install_canvas_mouse,
        )
        install_canvas_mouse(
            _canvas_control,
            lambda: mux.render_canvas_ansi().splitlines(),
            lambda line: (on_accept(line) if callable(on_accept) else None),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Bipartite] canvas mouse unavailable", exc_info=True)

    canvas = Window(
        content=_canvas_control,
        wrap_lines=False,
        # Content-sized, so a short deck sits directly under the header
        # instead of being padded down against the prompt.
        height=_canvas_dimension(mux),
    )
    # Ctrl+R history search rides the SAME completion menu the palette
    # renders — a gated completer that yields nothing until the
    # (remappable) history:search action arms it. Merged BEFORE the
    # TextArea exists because the buffer's completer is fixed at
    # construction.
    _hist_controller = None
    try:
        from backend.core.ouroboros.battle_test.history_search import (
            build_history_search,
            merge_history_completer,
        )
        _hist_controller, _hist_completer = build_history_search(history)
        completer = merge_history_completer(completer, _hist_completer)
    except Exception:  # noqa: BLE001 — search is a bonus, typing is not
        _hist_controller = None

    # The `/` palette. Passing a completer here is only HALF the wiring —
    # prompt_toolkit computes completions into the buffer, but draws the menu
    # as a Float, and this layout had no FloatContainer to draw it on. That is
    # why the completer built in D5 yielded 76 correct verbs and the operator
    # saw nothing: the menu had nowhere to exist.
    prompt = TextArea(
        # Multi-line, with the CONDITION applied to the buffer just below.
        #
        # The old `multiline=False` did not only stop the operator typing a
        # second line — it made a PASTED block lose its newlines, silently
        # collapsing a stack trace into one line. That is data loss.
        #
        # Passing the condition HERE does not work, despite the parameter
        # being annotated `FilterOrBool`: TextArea branches on the raw
        # truthiness (`if multiline:`) before any `to_filter`, and a Filter
        # raises ValueError on `__bool__`. So the literal True selects the
        # growable branch — the falsy one hard-clamps `height = D.exact(1)`,
        # which would put the caret below the fold on line two — and the
        # buffer gets the real rule afterwards.
        # Height is set to the CONTENT's size just below, once the buffer
        # this reads from exists. A range here (`min=1, max=8`) reads as
        # "one row, grow if needed" and does the opposite: HSplit hands out
        # preferred sizes and then distributes leftover rows by weight, so a
        # child whose max exceeds its preferred absorbs the slack — an empty
        # prompt rendered as an eight-row black slab under the deck.
        multiline=True,
        prompt=[("fg:#5ee06a bold", "❯ ")],
        wrap_lines=False, style="class:command-deck",
        completer=completer,
        complete_while_typing=bool(completer is not None),
        # Persistent recall on THE surface the operator actually types
        # into. Without an explicit history the TextArea's buffer gets a
        # fresh InMemoryHistory — Up-arrow forgot everything at detach,
        # while the fallback PromptSession remembered. `auto_up` on the
        # buffer already walks history when the caret is on the first
        # line, so a History object is the ONLY missing piece.
        **({"history": history} if history is not None else {}),
        # History ghost-text (grey fish/CC-style suggestion). None when
        # the operator disabled it — TextArea treats None as "no
        # suggestions" natively.
        **({"auto_suggest": auto_suggest} if auto_suggest is not None
           else {}),
    )

    def _accept(buff) -> bool:
        text = buff.text
        try:
            on_accept(text)
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] on_accept raised", exc_info=True)
        return False  # clear the buffer after accept

    prompt.buffer.accept_handler = _accept
    # Enter submits unless the text is visibly unfinished. `Buffer.multiline`
    # is the library's OWN seam — it stores a Filter and `is_multiline` calls
    # it per keystroke — so this needs no custom Enter binding to fight with.
    try:
        from backend.core.ouroboros.battle_test.input_continuation import (
            continuation_filter,
        )
        prompt.buffer.multiline = continuation_filter(lambda: prompt.buffer.text)
        # Exactly as tall as what is typed — nothing left for HSplit to
        # inflate. Same module as the continuation rule: how the prompt
        # behaves while composing lives in one place.
        prompt.window.height = prompt_height(lambda: prompt.buffer.text)
    except Exception:  # noqa: BLE001 — plain multiline still beats one line
        logger.debug("[Bipartite] continuation rule degraded", exc_info=True)

    kb = KeyBindings()
    # Arm/cycle history search (default Ctrl+R) + the auto-disarm watch.
    if _hist_controller is not None:
        try:
            from backend.core.ouroboros.battle_test.history_search import (
                install_history_search,
            )
            _hist_controller.watch(prompt.buffer)
            install_history_search(kb, _hist_controller)
        except Exception:  # noqa: BLE001
            pass
    # Shift+Tab raises the risk floor for this session. It composes
    # into risk_tier_floor's strictest-wins resolution rather than
    # overriding it, so the keystroke can only ever ADD friction —
    # it cannot make the organism more permissive than the config
    # already allows, in any cycle position.
    try:
        from backend.core.ouroboros.governance.session_risk_floor import (
            cycle_session_floor,
        )
        kb.add("s-tab")(lambda event: cycle_session_floor())
    except Exception:  # noqa: BLE001
        pass
    # Ctrl+V pastes a SCREENSHOT. The most common way an operator has
    # an image is Cmd+Shift+Ctrl+4 — on the clipboard, never written
    # to disk — and a terminal pastes text, so it produced nothing.
    # Spilled to a file and handed to the EXISTING /attach verb, so
    # validation, the size cap and the multi-modal path are the ones
    # a dragged file already uses. Text pastes fall through unchanged.
    try:
        from backend.core.ouroboros.battle_test.clipboard_image import (
            install_image_paste_binding,
        )
        install_image_paste_binding(
            kb, lambda text: (prompt.buffer.insert_text(text)
                              if prompt.buffer is not None else None),
        )
    except Exception:  # noqa: BLE001
        pass
    # Ctrl+T collapses the plan checklist. A four-item plan is
    # orientation while work runs and clutter while reading a diff,
    # and which of those it is changes minute to minute — so it is a
    # keystroke, not a setting.
    try:
        from backend.core.ouroboros.battle_test.plan_checklist import (
            toggle_checklist,
        )
        kb.add("c-t")(lambda event: toggle_checklist())
    except Exception:  # noqa: BLE001
        pass
    # Ctrl+S parks a half-written goal so the operator can check something
    # and come back to it — the prompt accepts paragraphs now, which makes it
    # worth interrupting.
    try:
        from backend.core.ouroboros.battle_test.draft_stash import (
            install_stash_binding,
        )
        install_stash_binding(kb, lambda: prompt.buffer)
    except Exception:  # noqa: BLE001
        pass
    # Ctrl+Z — suspend to the shell, `fg` to come back. CC binds it and this
    # cockpit did not, which is a gap the alternate screen CREATES rather than
    # inherits: a normal terminal turns Ctrl+Z into SIGTSTP itself, but a
    # full-screen app holds the terminal in raw mode, so the keystroke arrives
    # as an ordinary key and the shell never sees it. The operator's habit
    # silently stopped working the day the cockpit went full-screen.
    #
    # `suspend_to_background` is prompt_toolkit's own: it leaves the alternate
    # screen, restores cooked mode, raises SIGTSTP, and repaints on SIGCONT.
    # Doing this by hand means owning terminal-state restoration across a
    # signal, and getting it wrong leaves the operator at a shell with no echo.
    #
    # Bound HERE so every surface that builds this Application inherits it —
    # the daemon cockpit and the attach client alike — which is the same
    # reasoning the SIGWINCH handler below is registered on.
    try:
        import signal as _sig
        if hasattr(_sig, "SIGTSTP"):        # Unix only, as CC documents
            from backend.core.ouroboros.battle_test.keymap import bind_action

            def _suspend(event: Any) -> None:
                try:
                    event.app.suspend_to_background()
                except Exception:  # noqa: BLE001
                    logger.debug("[Bipartite] suspend degraded", exc_info=True)

            bind_action(
                kb, "app:suspend", ("ctrl+z",), _suspend, context="Global",
                description="suspend to the shell (`fg` to resume)",
            )
    except Exception:  # noqa: BLE001
        logger.debug("[Bipartite] suspend binding unavailable", exc_info=True)
    # Scrollback keys. In the alternate screen the terminal no longer offers
    # its own, so these ARE the scrollback — not a convenience layered on it.
    # SIGWINCH → the layout. `handle_sigwinch` existed, read the live
    # terminal, and had NO CALLER: a resize never reached `_line_budget`, so
    # the deck kept clipping to whatever height the constructor was given.
    # Registered here rather than in each caller so every surface that builds
    # this Application inherits it — the demo and the daemon cockpit alike.
    #
    # Best-effort by design: signal handlers may only be installed on the
    # main thread, and a cockpit hosted on a worker must still render. It
    # degrades to the constructor seed, which is now the live size anyway.
    try:
        import signal as _signal
        _signal.signal(_signal.SIGWINCH, mux.handle_sigwinch)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        install_scroll_bindings(
            kb, mux._viewport, mux.scroll_metrics, mux._invalidate_now,
        )
    except Exception:  # noqa: BLE001
        pass
    # Alt+Enter, from the same module that decides when Enter continues — so
    # the rule and its escape hatch can never be wired on different surfaces.
    try:
        from backend.core.ouroboros.battle_test.input_continuation import (
            install_newline_binding,
        )
        install_newline_binding(kb)
    except Exception:  # noqa: BLE001
        pass

    # Detach/exit through the remappable keymap (defaults unchanged:
    # Ctrl+C / Ctrl+D). A mount rather than a literal `@kb.add("c-c")`
    # so keybindings.json can move them and `/keys` can show them; the
    # except-branch keeps the legacy hardcoded pair when the keymap
    # module is unavailable — a cockpit the operator cannot LEAVE is
    # the one regression this seam must never introduce.
    def _exit(event) -> None:
        event.app.exit()

    try:
        from backend.core.ouroboros.battle_test.keymap import KeymapMount
        _mount = KeymapMount("cockpit")
        _mount.action(
            "app:detach", ("ctrl+c",), context="Global",
            description="leave the cockpit; the daemon keeps running",
        )(_exit)
        _mount.action(
            "app:exit", ("ctrl+d",), context="Global",
            description="leave the cockpit",
        )(_exit)
        _mount_kb = _mount.key_bindings()
        if _mount_kb is None:
            raise RuntimeError("keymap mount unavailable")
        kb = _merge_key_bindings(kb, _mount_kb)
    except Exception:  # noqa: BLE001
        kb.add("c-c")(_exit)
        kb.add("c-d")(_exit)

    if extra_key_bindings is not None:
        try:
            kb = _merge_key_bindings(kb, extra_key_bindings)
        except Exception:  # noqa: BLE001
            pass

    rows = []
    if header is not None and header_height > 0:
        # The CC-style identity header (mini animated crest + version + path).
        # Stateless render — the callable derives its animation phase from the
        # clock, so the app's refresh_interval animates it with zero tasks.
        def _header_fragments():
            try:
                return ANSI(str(header() or ""))
            except Exception:  # noqa: BLE001
                return ANSI("")

        rows.append(Window(
            content=FormattedTextControl(_header_fragments, focusable=False),
            height=header_height, wrap_lines=False,
        ))
    # The CC input framing: a dim hairline above AND below the ❯ row.
    def _rule(row: int = 0) -> Any:
        """A venom-purple hairline — with the serpent running it while the
        organism works.

        The boot crest already tells this story: a snake travelling a closed
        path after a `+`, catching it, going round again. Two hairlines ARE a
        closed path (left→right along the top, right→left along the bottom),
        so the same creature lives here on the same laws — `serpent_rule`
        takes the crest's own prey palette.

        Falls back to the plain `char="─"` Window when no activity source was
        supplied: a border that moves forever is a distraction that teaches
        the operator to stop seeing the border, and framing the caret is the
        one thing this line has to do.
        """
        if serpent_active is None:
            return Window(height=1, char="─", style="fg:#a371f7")
        try:
            from prompt_toolkit.layout.controls import FormattedTextControl

            def _fragments(_row: int = row) -> Any:
                try:
                    import time as _t
                    from backend.core.ouroboros.ui.serpent_rule import (
                        rule_fragments,
                    )
                    width = 80
                    app = _APP_REF.get("app")
                    if app is not None and app.output is not None:
                        width = max(1, int(app.output.get_size().columns))
                    try:
                        live = bool(serpent_active())
                    except Exception:  # noqa: BLE001
                        live = False
                    return rule_fragments(
                        _row, width, _t.monotonic(), active=live,
                        # The Application's OWN repaint period, so the head
                        # advances a whole number of cells per frame. A speed
                        # picked independently of the frame rate produces a
                        # beat — 1.8 cells/frame renders as 1,2,2,2,2,1,… and
                        # that stray 1 is what an eye reads as stutter.
                        interval=_REFRESH_INTERVAL_S,
                    )
                except Exception:  # noqa: BLE001
                    return [("fg:#a371f7", "─" * 80)]

            return Window(
                content=FormattedTextControl(_fragments, focusable=False),
                height=1, wrap_lines=False,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] serpent rule unavailable", exc_info=True)
            return Window(height=1, char="─", style="fg:#a371f7")

    # The palette sits ABOVE the input, full width, and pushes the prompt down
    # as it opens — the same relationship Claude Code has. A Float would
    # overlay the canvas at the widget's own width; a row participates in the
    # layout, so it wraps to the terminal and the prompt stays anchored under
    # it. `dont_extend_height` keeps it at zero rows when nothing is matching.
    _palette = None
    if completer is not None:
        try:
            from backend.core.ouroboros.battle_test.palette_render import (
                build_palette_window,
            )
            _palette = build_palette_window()
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] palette row unavailable", exc_info=True)

    rows += [canvas]
    # The TURN row — the live spinner bound to the question the operator
    # just asked, between the canvas and the prompt exactly where Claude
    # Code puts it. A ConditionalContainer, so an idle cockpit is EXACTLY
    # as tall as it was before this existed (Style Guide §07: one
    # in-place spinner, never six stacked lines).
    if turn_spinner is not None:
        try:
            from backend.core.ouroboros.battle_test.turn_spinner import (
                build_turn_row,
            )
            _turn_row = build_turn_row(turn_spinner)
            if _turn_row is not None:
                rows += [_turn_row]
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] turn row unavailable", exc_info=True)
    # AMBIENT state goes BELOW the prompt; ACTIVE surfaces go above.
    #
    # This is Claude Code's geometry and it is not arbitrary. Above the input
    # box sits what is happening to the turn you just took — the working
    # spinner, the search you are typing into. Below it sits the standing
    # state: what mode you are in, what is running, what it has cost. The
    # split is by whether the row is about the NEXT keystroke or about the
    # session, and an operator reads downward from the thing they just did.
    #
    # These three were all mounted above the prompt as they were built, one
    # per slice, each reasonable in isolation. Together they pushed the caret
    # steadily down the screen behind a stack of state the operator was not
    # asking about — the roster's header alone costs three rows every time an
    # agent runs. `_below_prompt` collects them; the search bar stays above
    # because while it is open it IS the next keystroke.
    _below_prompt: list = []
    for label, provider in (("agent", agent_rows), ("status", status_rows)):
        if provider is None:
            continue
        try:
            row = build_dynamic_rows(provider)
            if row is not None:
                _below_prompt.append(row)
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] %s row unavailable", label,
                         exc_info=True)
    # The sentence being WRITTEN, directly under the deck it is about to
    # become part of. The deck is bottom-anchored, so its newest entry sits
    # immediately above this — the in-flight text appears exactly where it
    # will land, which is what makes it read as inline rather than as a
    # separate widget.
    # The operator's own backlog, nearest the caret — it is about what
    # THEY did, so it belongs closest to where they are typing.
    if queue_rows is not None:
        try:
            _q_row = build_dynamic_rows(queue_rows)
            if _q_row is not None:
                rows += [_q_row]
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] queue row unavailable", exc_info=True)
    if stream_rows is not None:
        try:
            _stream_row = build_dynamic_rows(stream_rows)
            if _stream_row is not None:
                rows += [_stream_row]
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] stream row unavailable", exc_info=True)
    # The rejection window, above the search bar and the caret. It is the
    # most time-critical row the cockpit ever draws — the operator has
    # seconds — so it sits where the eye already is.
    if pending_rows is not None:
        try:
            _pending_row = build_dynamic_rows(pending_rows)
            if _pending_row is not None:
                rows += [_pending_row]
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] pending row unavailable", exc_info=True)
    # The search bar sits DIRECTLY above the prompt: while it is open it IS the
    # next keystroke, which is the rule `_below_prompt` states for what goes
    # above the caret versus what goes below it.
    #
    # This mount is a REPAIR. The comment above survived an edit that lost the
    # code, leaving `search_rows` accepted by the signature, forwarded here by
    # `run_bipartite_repl`, resolved by `ov.py::_transcript_search_rows` — and
    # read by nothing. Transcript search was dark on the SHIPPING client, not
    # merely in a demo, and the test guarding it asserted `"search_rows=" in
    # src` so it passed for the entire period the feature did nothing.
    #
    # `build_dynamic_rows`' own docstring names this row — "the agent view, the
    # search bar, and whatever comes next" — so the geometry primitive was
    # built for it and then never handed it. Found by
    # `ui/capability_handoff.py`, which exists because of this defect.
    if search_rows is not None:
        try:
            _search_row = build_dynamic_rows(search_rows)
            if _search_row is not None:
                rows += [_search_row]
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] search row unavailable", exc_info=True)
    # The palette is NOT a row. See the FloatContainer below: as an HSplit row
    # it shares the ambient grid with the canvas, so every asynchronous Deck or
    # Lane frame arriving underneath forces the palette's geometry to be
    # recomputed along with everything else.
    _toolbar_row = None
    if toolbar is not None:
        # A one-row morphing footer (e.g. the attach client's AttachUI.toolbar —
        # audio state, detach hint). Re-evaluated each repaint; a failing
        # callable renders empty rather than crashing the frame.
        def _toolbar_fragments():
            # ``toolbar()`` returns EITHER a plain string (key hints) or a
            # formatted-text fragment list (#70140, the palette on the
            # PromptSession surface). This wrapped it in str() unconditionally,
            # so a fragment list rendered as its Python repr —
            # `[('class:completion-menu.completion', '  /anticipate ...` —
            # printed across the bottom of the cockpit.
            #
            # Producer/consumer contract drift, the same shape as the mock
            # drift that cost a day: one side widened its return type and the
            # other kept assuming the old one.
            try:
                value = toolbar()
                if isinstance(value, list):
                    return value          # already fragments; pass through
                return [("class:bottom-toolbar", str(value or ""))]
            except Exception:  # noqa: BLE001
                return [("", "")]

        _toolbar_row = Window(
            content=FormattedTextControl(_toolbar_fragments, focusable=False),
            height=1, wrap_lines=False,
        )

    # The pulse belongs ABOVE the input, which is where Claude Code puts it and
    # where the eye already is: the operator is reading the newest deck line,
    # and "still working, 42s, 33k tokens" is the continuation of that line,
    # not a footer. Below the prompt it is separated from what it describes by
    # the whole input frame, and it reads as terminal chrome — something the
    # shell put there — rather than as the organism still breathing.
    #
    # It also keeps the geometry honest: the canvas bottom-anchors so the
    # newest event lands against the prompt, and a row wedged underneath meant
    # the LAST thing before the input was a status bar the deck did not own.
    if _toolbar_row is not None and toolbar_above_prompt():
        rows += [_toolbar_row, _rule(0), prompt, _rule(1)]
    else:
        rows += [_rule(0), prompt, _rule(1)]
        if _toolbar_row is not None:
            rows.append(_toolbar_row)
    # The standing state, under the box the operator types into.
    rows += _below_prompt
    # ── Region layout mount (PR #70213's seam, finally consumed) ──────
    #
    # `viewport_arbiter` has decided placements since #70187 and
    # `region_layout.dynamic_region_container` has been able to build them
    # since #70213 — with nothing reading either. `JARVIS_REGION_LAYOUT_ENABLED`
    # read DARK on the progress board, which is exactly what that state is for.
    #
    # Mounted CONSERVATIVELY on purpose. The arbiter arbitrates over
    # ("deck", "lanes", "transcript"), and only `deck` has a widget source
    # today — the nested task tree, collapse/expand and transcript mode are the
    # features that will supply the other two. So the derived tree is TODAY'S
    # tree, and the observable surface does not move.
    #
    # That is the point, not a limitation. A layout change and a feature change
    # landing together is unbisectable when the cockpit misbehaves, and this
    # cockpit is the thing the operator stares at all day. The seam goes live,
    # `DynamicContainer` starts re-deriving per frame, and the next slice hangs
    # regions off a surface that has already been watched.
    root: Any = _mount_region_layout(rows)
    if _palette is not None:
        # Z-INDEX OVERLAY, not a row.
        #
        # This cockpit takes asynchronous IPC continuously — Deck entries, Lane
        # frames, the heartbeat. A palette that participates in the HSplit
        # shares the ambient grid with all of it, so each of those frames
        # recomputes the palette's geometry too, and the reflow lands on the
        # keystroke that opened the menu.
        #
        # A Float is measured independently of the grid: the canvas beneath can
        # repaint at whatever rate the daemon pushes without the overlay taking
        # part. Background updates stop being able to disturb the menu.
        #
        # The float carries OUR page-style palette, not prompt_toolkit's
        # CompletionsMenu widget. That widget is a bounded dropdown sized to
        # its longest entry — the narrow grey control #70123 replaced and
        # #70140 stripped — and reintroducing it as a float would trade the
        # layout back for the tearing fix. There is no need to choose:
        #
        #   left=0, right=0  -> spans the terminal, so descriptions wrap into
        #                       a real column instead of the widget's width;
        #   ycursor=True     -> tracks the caret. The prompt here is a
        #                       multi-line block (pulse + deck + caret) whose
        #                       height changes as those regions fill, so any
        #                       fixed `bottom=` offset would drift the moment
        #                       the live region grew a line.
        try:
            from prompt_toolkit.layout import Float, FloatContainer
            root = FloatContainer(
                content=root,
                floats=[Float(
                    content=_palette,
                    left=0, right=0,          # full terminal width
                    ycursor=True,             # follow the caret, not a constant
                )],
            )
        except Exception:  # noqa: BLE001 — a cockpit without a menu still types
            logger.debug("[Bipartite] palette overlay unavailable",
                         exc_info=True)
    elif completer is not None:
        # No page palette available (import failure). Fall back to
        # prompt_toolkit's own widget rather than leaving completions
        # invisible: a bounded dropdown beats no menu at all.
        try:
            from prompt_toolkit.layout import Float, FloatContainer
            from prompt_toolkit.layout.menus import (
                CompletionsMenu, MultiColumnCompletionsMenu,
            )
            _menu = (
                MultiColumnCompletionsMenu(show_meta=True)
                if _palette_multicolumn() else
                CompletionsMenu(max_height=_palette_height(), scroll_offset=1)
            )
            root = FloatContainer(
                content=root,
                floats=[Float(xcursor=True, ycursor=True, content=_menu)],
            )
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] completion menu unavailable", exc_info=True)
    # Adaptive color depth (root cause of the quantized/muddy logo): pt 3.0.x's
    # default depth reads TERM only — COLORTERM is ignored, so a truecolor
    # terminal gets its 24-bit palette QUANTIZED to the 256 cube. Detect
    # truecolor honestly; JARVIS_BIPARTITE_COLOR_DEPTH={1,4,8,24} overrides.
    _depth = None
    try:
        from prompt_toolkit.output.color_depth import ColorDepth
        _env = os.environ.get("JARVIS_BIPARTITE_COLOR_DEPTH", "").strip()
        _map = {"1": ColorDepth.DEPTH_1_BIT, "4": ColorDepth.DEPTH_4_BIT,
                "8": ColorDepth.DEPTH_8_BIT, "24": ColorDepth.DEPTH_24_BIT}
        if _env in _map:
            _depth = _map[_env]
        elif os.environ.get("COLORTERM", "").strip().lower() in ("truecolor", "24bit"):
            _depth = ColorDepth.DEPTH_24_BIT
    except Exception:  # noqa: BLE001
        _depth = None
    # Brand styling for the completion menu + footer. Sourced from the ONE
    # palette in ui.theme, so a brand change moves the cockpit with it, and
    # tier-aware so a 16-colour terminal is not handed truecolor hexes to
    # quantize into mud. Without it prompt_toolkit paints its default: a
    # filled light-grey listbox and a reverse-video toolbar bar — the two
    # loudest things on an otherwise dark screen.
    _style = None
    try:
        from backend.core.ouroboros.ui.theme import cockpit_prompt_style
        _style = cockpit_prompt_style()
    except Exception:  # noqa: BLE001 — an unstyled cockpit still works
        _style = None

    # Vim editing mode, from the ONE reader every surface consults
    # (JARVIS_EDITOR_MODE=vim). None = prompt_toolkit's emacs default,
    # byte-identical to every cockpit that ever shipped.
    _editing_mode = None
    try:
        from backend.core.ouroboros.battle_test.keymap import editing_mode
        _editing_mode = editing_mode()
    except Exception:  # noqa: BLE001
        _editing_mode = None

    # ── FATAL_PANIC overlay ────────────────────────────────────────────
    # The SAME FloatContainer architecture the `/` palette established —
    # one Z-index story, not a second overlay mechanism. Mounted last, so
    # it draws ABOVE the palette: a completion menu must never occlude the
    # notice that the organism has died in the background.
    #
    # `top=1` rather than `ycursor=True`: a crash is not attached to where
    # the caret happens to be, and an overlay that moves while the
    # operator reads a traceback is hostile.
    # ONE float host, established before any overlay wants it.
    #
    # This used to be implicit: whichever overlay block ran first found `root`
    # was not a `FloatContainer` and wrapped it, and later blocks appended to the
    # container the first one happened to create. With a single overlay that is
    # invisible. With two it is a silent drop — the diff float was added under an
    # `isinstance(root, FloatContainer)` guard that was False, because the block
    # that creates the container runs AFTER it, so the overlay mounted nothing
    # and reported nothing.
    #
    # Hoisting the host makes every overlay block a pure append, in source order,
    # which is also the draw order. No block has to know whether it is first, and
    # the next overlay added cannot reintroduce this by being placed anywhere in
    # particular.
    if diff_rows is not None or panic_rows is not None:
        try:
            from prompt_toolkit.layout import FloatContainer
            if not isinstance(root, FloatContainer):
                # Only when an overlay actually exists: an empty FloatContainer
                # renders identically, but wrapping unconditionally would change
                # the container tree for every cockpit that has no overlays and
                # buys nothing.
                root = FloatContainer(content=root, floats=[])
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] float host unavailable", exc_info=True)

    # The diff preview goes on FIRST, and the order is load-bearing.
    #
    # prompt_toolkit draws `root.floats` in list order, so a float appended later
    # renders ON TOP. The panic overlay must win that contest: a crash outranks a
    # diff the operator opened for themselves, and an overlay stack whose z-order
    # depends on which block happens to run first is the kind of thing that
    # silently inverts the next time this function is reorganised.
    #
    # It matches `overlay_arbiter`'s Z constants deliberately — Z_DIFF_PREVIEW <
    # Z_PANIC — so the surface an operator SEES on top is the same one `Escape`
    # closes first. Two orderings that disagree would mean pressing Escape
    # dismissing something other than what they are looking at.
    if diff_rows is not None:
        try:
            from prompt_toolkit.filters import Condition
            from prompt_toolkit.layout import (
                ConditionalContainer, Float, FloatContainer, Window,
            )
            from prompt_toolkit.layout.controls import FormattedTextControl
            from prompt_toolkit.formatted_text import ANSI

            def _diff_visible() -> bool:
                try:
                    return bool(diff_rows())
                except Exception:  # noqa: BLE001
                    return False

            # ANSI, not a style tuple: the rows arrive already coloured by Rich
            # (the syntax highlighting is the whole point of the overlay), and
            # wrapping pre-escaped text in a single prompt_toolkit style would
            # print the escapes and flatten the diff to one colour.
            _diff_win = ConditionalContainer(
                Window(
                    FormattedTextControl(
                        lambda: ANSI("\n".join(diff_rows())),
                    ),
                    wrap_lines=False,
                    # Opaque, so the deck does not show through a diff. The rows
                    # carry their own Rich colours, so this contributes only the
                    # background the float needs to occlude with.
                    style="bg:#0b0b0b",
                ),
                filter=Condition(_diff_visible),
            )
            if isinstance(root, FloatContainer):
                root.floats.append(Float(
                    content=_diff_win, **_OVERLAY_FLOAT_POSITION,
                ))
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] diff overlay unavailable", exc_info=True)

    if panic_rows is not None:
        try:
            from prompt_toolkit.filters import Condition
            from prompt_toolkit.layout import (
                ConditionalContainer, Float, FloatContainer, Window,
            )
            from prompt_toolkit.layout.controls import FormattedTextControl

            def _panic_visible() -> bool:
                try:
                    return bool(panic_rows())
                except Exception:  # noqa: BLE001
                    return False

            # `class:panic` was never registered in any prompt_toolkit
            # Style, so the overlay inherited the default and a FATAL
            # notice rendered in the same green as a success. The style
            # comes from the semantic layer instead — `alert` is the role
            # that exists precisely for "wants the operator's eye NOW" —
            # resolved to a prompt_toolkit style string.
            def _panic_style() -> str:
                """Rich style -> prompt_toolkit style. NEVER raises.

                The two engines spell colours differently: Rich says
                `bright_yellow`, prompt_toolkit says `ansibrightyellow`,
                and both accept `#RRGGBB`. Translating is the ONLY honest
                option — assuming either dialect is how `class:panic`
                silently rendered as the default in the first place.
                """
                try:
                    from backend.core.ouroboros.ui.semantic_tokens import (
                        style_for,
                    )
                    raw = (style_for("alert") or "").strip()
                    if not raw:
                        return "bold"
                    if raw.startswith("#"):
                        return f"bold {raw}"
                    return "bold fg:ansi" + raw.replace("_", "")
                except Exception:  # noqa: BLE001
                    return "bold"

            _p_style = _overlay_style("alert")
            _panic_win = ConditionalContainer(
                Window(
                    FormattedTextControl(
                        lambda: [(_p_style, "\n".join(panic_rows()))],
                    ),
                    wrap_lines=False, style=_p_style,
                ),
                filter=Condition(_panic_visible),
            )
            # Append-only. The host is hoisted above, so this block no longer
            # has to be the one that creates it — and the diff float appended
            # before it stays underneath, which is the z-order both this and
            # `overlay_arbiter` agree on.
            if isinstance(root, FloatContainer):
                root.floats.append(Float(
                    content=_panic_win, **_OVERLAY_FLOAT_POSITION,
                ))
        except Exception:  # noqa: BLE001
            # A cockpit that cannot draw the overlay must still RUN — the
            # panic is already logged and on the telemetry lane.
            logger.debug("[Bipartite] panic overlay unavailable", exc_info=True)

    app = Application(
        layout=PTLayout(root, focused_element=prompt),
        **({"editing_mode": _editing_mode}
           if _editing_mode is not None else {}),
        # Full-screen on a real terminal — the canvas holds the history the
        # alternate screen takes away (see `fullscreen_enabled`).
        key_bindings=kb, full_screen=fullscreen_enabled(),
        # The wheel scrolls the deck. Capturing the mouse is a real trade:
        # the terminal's own click-and-drag selection stops working while an
        # application owns mouse events, which is the single most common
        # friction point of a full-screen TUI. So it follows the alt-screen
        # decision (there is nothing to scroll without it) and can be turned
        # off on its own by an operator who selects text more than they
        # scroll — keeping the flicker-free rendering either way.
        mouse_support=mouse_enabled(),
        refresh_interval=_REFRESH_INTERVAL_S,
        **({"style": _style} if _style is not None else {}),
        **({"color_depth": _depth} if _depth is not None else {}),
    )
    _APP_REF["app"] = app
    mux.set_invalidate(app.invalidate)
    # The diff overlay's repaint, attached now that an Application exists.
    #
    # The controller is a process singleton built on first touch — a verb can open
    # a diff before any cockpit has mounted — so it cannot be handed an
    # `invalidate` at construction. Without this the off-thread render completes,
    # the rows land correctly, and NOTHING redraws until the next unrelated frame:
    # the overlay appears to take seconds to open, which is indistinguishable from
    # the blocking render it was built to avoid.
    #
    # Bound unconditionally rather than only when `diff_rows` was passed: the
    # binding is what makes an already-open overlay repaint, and a surface that
    # mounts the float later in the session would otherwise inherit a dead hook.
    if diff_rows is not None:
        try:
            from backend.core.ouroboros.battle_test.diff_overlay import (
                bind_invalidate,
            )
            bind_invalidate(app.invalidate)
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] diff repaint hook unavailable",
                         exc_info=True)
    # Register the app's invalidate with the Reactive Theme so a state transition
    # (DORMANT→ARMED→SOAKING→DEGRADED→HEALTHY) repaints the border IN PLACE — no
    # Application teardown/rebuild (zero-flicker mandate).
    try:
        from backend.core.ouroboros.ui.theme import get_reactive_theme
        get_reactive_theme().register_invalidate(app.invalidate)
    except Exception:  # noqa: BLE001
        pass
    return app


_APP_REF: dict = {}


def _merge_key_bindings(a: Any, b: Any) -> Any:
    from prompt_toolkit.key_binding import merge_key_bindings
    return merge_key_bindings([a, b])


def should_run_bipartite() -> bool:
    """Launch gate: the master flag AND a real interactive TTY (checked via the
    canonical ``real_stdout_isatty`` — a plain ``sys.stdout.isatty`` is False under
    the active ``patch_stdout`` proxy). Headless / piped / CI never enters the
    full-screen mode. Never raises."""
    return bipartite_enabled() and _real_tty()


async def _alive_watcher(
    app_exit: Callable[[], Any],
    watch_alive: Callable[[], bool],
    *,
    interval_s: float = 0.25,
    sleep_fn=None,
) -> None:
    """Poll ``watch_alive`` and call ``app_exit`` the moment it goes False — the
    daemon-died-mid-typing guard, generic + injectable (headless-testable).
    Never raises out."""
    import asyncio
    sleep = sleep_fn or asyncio.sleep
    try:
        while True:
            try:
                if not watch_alive():
                    try:
                        app_exit()
                    except Exception:  # noqa: BLE001
                        pass
                    return
            except Exception:  # noqa: BLE001 — a probe error reads as "gone"
                try:
                    app_exit()
                except Exception:  # noqa: BLE001
                    pass
                return
            await sleep(interval_s)
    except asyncio.CancelledError:
        raise


async def run_bipartite_repl(
    *,
    on_accept: Callable[[str], Any],
    title: str = "◇ O+V · proactive canvas",
    extra_key_bindings: Any = None,
    toolbar: Optional[Callable[[], str]] = None,
    watch_alive: Optional[Callable[[], bool]] = None,
    seed: Optional[List[str]] = None,
    header: Optional[Callable[[], str]] = None,
    header_height: int = 0,
    completer: Any = None,
    history: Any = None,
    auto_suggest: Any = None,
    turn_spinner: Any = None,
    agent_rows: Optional[Callable[[], Any]] = None,
    search_rows: Optional[Callable[[], Any]] = None,
    status_rows: Optional[Callable[[], Any]] = None,
    pending_rows: Optional[Callable[[], Any]] = None,
    stream_rows: Optional[Callable[[], Any]] = None,
    queue_rows: Optional[Callable[[], Any]] = None,
    panic_rows: Optional[Callable[[], Any]] = None,
    diff_rows: Optional[Callable[[], Any]] = None,
    on_mux: Optional[Callable[[Any], None]] = None,
    serpent_active: Optional[Callable[[], bool]] = None,
) -> None:
    """Launch the full-screen Bipartite REPL: build the multiplexer, register it as
    the live Zone-1 sink (so any producer — the daemon's event router OR the
    attach client's bridge stream — redirects into it), run the Application to
    completion, and clear the sink on exit. ``toolbar`` adds a one-row morphing
    footer; ``watch_alive`` exits the app the moment it returns False (daemon
    death never hangs the prompt). The atomic render loop is the
    ``patch_stdout``-equivalent — background telemetry into Zone 1 never corrupts
    Zone 2 keystrokes. Caller gates on :func:`should_run_bipartite`. Never
    raises out."""
    import asyncio
    import shutil

    size = shutil.get_terminal_size(fallback=(100, 30))
    mux = BipartiteLayout(
        width=size.columns,
        height=max(6, size.lines - max(0, header_height) - 2),
        title=title,
    )
    # Hand the multiplexer to the caller. Without it the client holds no
    # reference to the deck, so a seam like `set_streaming_tail` is
    # reachable in principle and inert in practice — the trap this
    # codebase has produced five times, most recently inside the fix for
    # it.
    if on_mux is not None:
        try:
            on_mux(mux)
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] on_mux hook failed", exc_info=True)
    set_active_canvas(mux)
    for ln in (seed or []):
        mux.push_raw(ln)
    watcher = None
    try:
        app = build_bipartite_application(
            mux, on_accept=on_accept, extra_key_bindings=extra_key_bindings,
            toolbar=toolbar, header=header, header_height=header_height,
            completer=completer, history=history, auto_suggest=auto_suggest,
            turn_spinner=turn_spinner, agent_rows=agent_rows,
            search_rows=search_rows, status_rows=status_rows,
            pending_rows=pending_rows, stream_rows=stream_rows,
            queue_rows=queue_rows, panic_rows=panic_rows,
            diff_rows=diff_rows,
            serpent_active=serpent_active,
        )
        if watch_alive is not None:
            watcher = asyncio.ensure_future(
                _alive_watcher(app.exit, watch_alive)
            )
        await app.run_async()
    except Exception:  # noqa: BLE001 — a TUI failure must not crash the organism
        logger.debug("[Bipartite] run_bipartite_repl exited on error", exc_info=True)
    finally:
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        set_active_canvas(None)


__all__ = [
    "BipartiteLayout",
    "bipartite_enabled",
    "bottom_anchor_enabled",
    "build_bipartite_application",
    "get_active_canvas",
    "build_dynamic_rows",
    "run_bipartite_repl",
    "set_active_canvas",
    "should_run_bipartite",
]
