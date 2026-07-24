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
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.BipartiteLayout")

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


def _canvas_max_lines() -> int:
    try:
        return max(16, int(os.environ.get("JARVIS_BIPARTITE_CANVAS_MAX_LINES", _DEFAULT_MAX_LINES)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_LINES


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
        width: int = 100,
        height: int = 24,
        invalidate: Optional[Callable[[], None]] = None,
        title: str = "◇ O+V · proactive canvas",
    ) -> None:
        # Reuse the bounded buffer primitive (DRY) — never the Live renderer.
        from backend.core.ouroboros.battle_test.split_layout import RegionBuffer

        self._buffer = RegionBuffer(name="canvas", maxlen=max_lines or _canvas_max_lines())
        self._registry = registry
        self._width = max(10, int(width))
        self._height = max(3, int(height))
        self._invalidate = invalidate
        self._title = title
        self._resize_count = 0
        self._sprite: Any = None            # the DORMANT/WAKING hero animation

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

    def push_raw(self, markup_line: str) -> None:
        """Push a pre-formatted Rich-markup line (e.g. piped Sentinel stdout).
        Never raises."""
        try:
            self._buffer.push(str(markup_line))
        except Exception:  # noqa: BLE001
            pass
        self._invalidate_now()

    # -- rendering ------------------------------------------------------

    def _visible_lines(self) -> List[str]:
        # Auto-scroll: keep only the last (height - frame chrome) lines.
        try:
            budget = max(1, self._height - 4)   # panel top/bottom border + title + deck
            snap = self._buffer.snapshot()
            return list(snap[-budget:])
        except Exception:  # noqa: BLE001
            return []

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
                body = Group(*[Text.from_markup(ln) for ln in lines])
            else:
                body = Text("  idle — waiting for the organism to act", style="bright_black")
            n = self._buffer.line_count if hasattr(self._buffer, "line_count") else len(lines)
            # State-reactive border (Style Guide §06): the accent reflects the
            # organism's meta-state, mutated in place by the Reactive Theme.
            border = "cyan"
            try:
                from backend.core.ouroboros.ui.theme import get_reactive_theme
                border = get_reactive_theme().active_border_style() or "cyan"
            except Exception:  # noqa: BLE001
                border = "cyan"
            return Panel(
                body, title=self._title, title_align="left",
                subtitle=f"[bright_black]{n} events[/bright_black]", subtitle_align="right",
                box=ROUNDED, border_style=border, padding=(0, 1),
                height=max(3, self._height - 3),
            )
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


def build_bipartite_application(
    mux: BipartiteLayout,
    *,
    on_accept: Callable[[str], Any],
    extra_key_bindings: Any = None,
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
        # Re-sync dims to the live terminal each render → SIGWINCH handled for free.
        try:
            app = _APP_REF.get("app")
            if app is not None and app.output is not None:
                size = app.output.get_size()
                if (size.columns, size.rows) != (mux.width, mux.height):
                    mux.on_resize(size.columns, max(4, size.rows - 1))  # reserve deck row
        except Exception:  # noqa: BLE001
            pass
        return ANSI(mux.render_canvas_ansi())

    canvas = Window(
        content=FormattedTextControl(_canvas_fragments, focusable=False),
        wrap_lines=False, height=Dimension(weight=1),
    )
    prompt = TextArea(
        height=1, prompt="› ", multiline=False, wrap_lines=False,
        style="class:command-deck",
    )

    def _accept(buff) -> bool:
        text = buff.text
        try:
            on_accept(text)
        except Exception:  # noqa: BLE001
            logger.debug("[Bipartite] on_accept raised", exc_info=True)
        return False  # clear the buffer after accept

    prompt.buffer.accept_handler = _accept

    kb = KeyBindings()

    @kb.add("c-c")
    @kb.add("c-d")
    def _exit(event) -> None:
        event.app.exit()

    if extra_key_bindings is not None:
        try:
            kb = _merge_key_bindings(kb, extra_key_bindings)
        except Exception:  # noqa: BLE001
            pass

    root = HSplit([canvas, prompt])
    app = Application(
        layout=PTLayout(root, focused_element=prompt),
        key_bindings=kb, full_screen=True, mouse_support=False,
        refresh_interval=0.2,
    )
    _APP_REF["app"] = app
    mux.set_invalidate(app.invalidate)
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
    if not bipartite_enabled():
        return False
    try:
        from backend.core.ouroboros.battle_test.presentation_restraint import (
            real_stdout_isatty,
        )
        return real_stdout_isatty()
    except Exception:  # noqa: BLE001
        return False


async def run_bipartite_repl(
    *,
    on_accept: Callable[[str], Any],
    title: str = "◇ O+V · proactive canvas",
    extra_key_bindings: Any = None,
) -> None:
    """Launch the full-screen Bipartite REPL: build the multiplexer, register it as
    the live Zone-1 sink (so the event router redirects into it), run the
    Application to completion, and clear the sink on exit. The atomic render loop
    is the ``patch_stdout``-equivalent — background telemetry into Zone 1 never
    corrupts Zone 2 keystrokes. Caller gates on :func:`should_run_bipartite`.
    Never raises out."""
    import shutil

    size = shutil.get_terminal_size(fallback=(100, 30))
    mux = BipartiteLayout(width=size.columns, height=size.lines, title=title)
    set_active_canvas(mux)
    try:
        app = build_bipartite_application(
            mux, on_accept=on_accept, extra_key_bindings=extra_key_bindings,
        )
        await app.run_async()
    except Exception:  # noqa: BLE001 — a TUI failure must not crash the organism
        logger.debug("[Bipartite] run_bipartite_repl exited on error", exc_info=True)
    finally:
        set_active_canvas(None)


__all__ = [
    "BipartiteLayout",
    "bipartite_enabled",
    "build_bipartite_application",
    "get_active_canvas",
    "run_bipartite_repl",
    "set_active_canvas",
    "should_run_bipartite",
]
