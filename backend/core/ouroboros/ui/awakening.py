"""backend/core/ouroboros/ui/awakening.py -- the ov boot ceremony conductor.

:class:`AwakeningConductor` owns a Rich ``Live`` region that plays the boot
ceremony: the procedural crest (``ui.crest``) draws itself tail-to-head while
the *real* boot-phase readiness renders beneath it via a MOUNTED
:class:`~backend.core.ouroboros.ui.wake_sequence.WakeSequenceRenderer` --
this module never reimplements phase tracking (Mandate 3 / DRY). When the
crest finishes drawing it fires an ``on_ignition`` callback exactly once,
then holds/breathes until boot is genuinely "live" (never faked), cools down
to a one-line header, and supports Esc/Enter skip while buffering any other
typed keys so they are handed off to the REPL rather than swallowed.

Dependency rule (leaf UI layer): stdlib + Rich + ``.theme`` + ``.crest`` +
``.wake_sequence`` only. No ``governance``/``battle_test`` imports -- the
``on_ignition``/``context_provider`` callbacks are injected by the caller so
governance concerns stay out of the presentation layer.

NEVER raises: :meth:`AwakeningConductor.run` wraps the entire ceremony and
falls back to the plain (no ``Live``) path on any exception, logging DEBUG
to ``logging.getLogger("Ouroboros.UI.Awakening")``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import select
import sys
import time
from typing import Callable, Optional

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from .crest import CrestFrame, generate_crest, style_for_cell
from .theme import (
    ColorTier,
    Token,
    detect_tier,
    ensure_theme,
    style_for,
    supports_unicode,
)
from .wake_sequence import WakeSequenceRenderer

logger = logging.getLogger("Ouroboros.UI.Awakening")

#: Kill switch -- default ON. Flip to a falsy value to skip the ceremony
#: entirely and go straight to the plain (no ``Live``) boot path.
_ENABLED_ENV_VAR = "JARVIS_OV_AWAKENING_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Bytes that request a skip -- Esc, CR, LF. Every other byte is buffered
#: into ``typed_prefix`` for REPL handoff (never swallowed, never a skip).
_SKIP_CHARS = frozenset({"\x1b", "\r", "\n"})

#: Wall-clock (or injected-clock) ceiling on the pre-ignition reveal and the
#: post-ignition hold -- honesty guard so a stuck sensor can never wedge the
#: ceremony forever (spec §3.2 item 4).
_HOLD_GUARD_S = 20.0

#: Animation cadence -- mirrors the 16ms batched-refresh pattern used by
#: ``battle_test/stream_renderer.py`` (~60fps).
_ANIMATION_TICK_S = 0.016

#: Cool-down sub-frame hold, real wall-clock -- ~0.4s total across 2 frames.
_COOLDOWN_SUBFRAME_S = 0.2


def _awakening_enabled() -> bool:
    """Env gate read fresh on every call (tests monkeypatch the env var)."""
    return os.environ.get(_ENABLED_ENV_VAR, "true").strip().lower() in _TRUTHY


class _StdinKeyReader:
    """Non-blocking raw-stdin reader for skip detection (production only).

    Only actually engages when BOTH ``sys.__stdout__`` and ``sys.stdin`` are
    real ttys -- checked via ``sys.__stdout__`` (not ``sys.stdout``, which
    can be wrapped by prompt_toolkit's ``patch_stdout`` and report ``False``
    even on a real terminal). Under any other condition (tests, headless,
    CI, piped output) this degrades to an inert no-op reader: ``read()``
    always returns ``b""``. Restores termios settings in ``__exit__``, and
    NEVER raises -- every termios call is wrapped defensively.
    """

    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._old_settings = None
        self._active = False

    def __enter__(self) -> "_StdinKeyReader":
        try:
            stdout = sys.__stdout__
            if stdout is None or not stdout.isatty():
                return self
            stdin = sys.stdin
            if stdin is None or not stdin.isatty():
                return self
            import termios
            import tty

            self._fd = stdin.fileno()
            self._old_settings = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self._active = True
        except Exception:  # noqa: BLE001 -- terminal setup must never be fatal
            logger.debug("[awakening] cbreak setup failed", exc_info=True)
            self._active = False
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._active and self._fd is not None and self._old_settings is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except Exception:  # noqa: BLE001
                logger.debug("[awakening] termios restore failed", exc_info=True)
        self._active = False

    def read(self) -> bytes:
        if not self._active or self._fd is None:
            return b""
        try:
            ready, _, _ = select.select([self._fd], [], [], 0)
            if not ready:
                return b""
            return os.read(self._fd, 64)
        except Exception:  # noqa: BLE001
            logger.debug("[awakening] stdin read failed", exc_info=True)
            return b""


class AwakeningConductor:
    """Plays the boot ceremony: crest reveal + mounted wake sequence.

    Consumes (never rebuilds) :class:`~.wake_sequence.WakeSequenceRenderer`
    for real phase readiness, and :func:`~.crest.generate_crest` for the
    procedural crest geometry. See module docstring for the full contract.
    """

    def __init__(
        self,
        console: Console,
        *,
        timer: object = None,
        on_ignition: Optional[Callable[[], None]] = None,
        context_provider: Optional[Callable[[], str]] = None,
        clock: Callable[[], float] = time.monotonic,
        key_source: Optional[Callable[[], bytes]] = None,
    ) -> None:
        self._console = console
        ensure_theme(console)
        self._wake = WakeSequenceRenderer(console)
        if timer is not None:
            self._wake.attach(timer)  # existing observer API -- MOUNTED, not rebuilt

        self._on_ignition = on_ignition
        self._context_provider = context_provider
        self._clock = clock
        self._key_source = key_source

        self.typed_prefix: str = ""
        self.used_plain_path: bool = False

        self._skip_requested = False
        self._ignited = False
        self._header_printed = False

        #: Resize proof (Mandate 4 / SIGWINCH): count of in-flight crest
        #: regenerations triggered by a mid-trace console size change, and
        #: the last size measured by the animated tick loop. ``None`` until
        #: the first tick establishes a baseline.
        self.regenerations: int = 0
        self._last_size: Optional[tuple] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_skip(self) -> None:
        """Request an immediate jump to cool-down. Idempotent."""
        self._skip_requested = True

    async def run(self) -> None:
        """Play the whole ceremony. NEVER raises -- falls back to the plain
        path on any exception encountered anywhere in the animated path."""
        try:
            frame = self._generate_frame()
            if self._should_go_plain(frame):
                await self._run_plain()
                return
            await self._run_animated(frame)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[awakening] animated ceremony failed; falling back to plain",
                exc_info=True,
            )
            try:
                await self._run_plain()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[awakening] plain fallback also failed", exc_info=True,
                )

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _generate_frame(self) -> CrestFrame:
        try:
            size = self._console.size
            tier = detect_tier(self._console)
            unicode_ok = supports_unicode()
            return generate_crest(
                size.width, size.height, tier=tier, unicode_ok=unicode_ok,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[awakening] frame generation failed", exc_info=True)
            return CrestFrame(0, 0, (), 0.0, "generation error")

    def _should_go_plain(self, frame: CrestFrame) -> bool:
        """True when any guard forces the plain (no ``Live``) path.

        Guards: the master env kill switch, a non-interactive console (via
        ``console.is_terminal`` -- honors ``force_terminal`` for injected
        test consoles the same way Rich's own renderers do), or an
        unavailable crest frame (unsupported unicode/color/size).
        """
        if not _awakening_enabled():
            return True
        try:
            if not getattr(self._console, "is_terminal", False):
                return True
        except Exception:  # noqa: BLE001
            return True
        if frame.unavailable_reason is not None:
            return True
        return False

    def _check_resize(self, frame: CrestFrame) -> CrestFrame:
        """Per-tick SIGWINCH proof (Mandate 4): if the measured console size
        changed since the last tick, regenerate the crest at the new size.

        Crest cell ``delay_s`` values are derived purely from arc-angle
        (tail=0 -> head=1), the SAME fractional schedule regardless of
        width -- so the same ``elapsed`` reveals the same arc fraction on
        the regenerated frame and the reveal stays monotonic by
        construction; no remapping is needed, only adoption of the new
        frame. If the console shrank below the crest minimum, the new
        frame comes back ``unavailable_reason``-set -- request a skip so
        the ceremony degrades gracefully to cool-down instead of trying to
        render a frame with no cells. NEVER raises: any failure to measure
        or regenerate just keeps the current frame for this tick.
        """
        try:
            size = self._console.size
            current = (size.width, size.height)
        except Exception:  # noqa: BLE001
            logger.debug("[awakening] resize measurement failed", exc_info=True)
            return frame
        if current == self._last_size:
            return frame
        self._last_size = current
        try:
            new_frame = self._generate_frame()
        except Exception:  # noqa: BLE001
            logger.debug("[awakening] resize regeneration failed", exc_info=True)
            return frame
        if new_frame.unavailable_reason is None:
            self.regenerations += 1
            return new_frame
        # Too small now -- degrade gracefully to cool-down rather than
        # crashing or spinning on an empty frame.
        self.request_skip()
        return frame

    # ------------------------------------------------------------------
    # Animated path
    # ------------------------------------------------------------------

    async def _run_animated(self, frame: CrestFrame) -> None:
        tier = detect_tier(self._console)
        start = self._clock()
        ignite_elapsed: Optional[float] = None

        try:
            size0 = self._console.size
            self._last_size = (size0.width, size0.height)
        except Exception:  # noqa: BLE001
            self._last_size = None

        reader_cm: Optional[_StdinKeyReader] = None
        poll = self._key_source
        if poll is None:
            reader_cm = _StdinKeyReader()
            reader_cm.__enter__()
            poll = reader_cm.read

        try:
            with Live(console=self._console, auto_refresh=False, transient=True) as live:
                while True:
                    now = self._clock()
                    elapsed = now - start

                    self._poll_keys(poll)
                    frame = self._check_resize(frame)

                    # Deliberately NOT wrapped in try/except: a rendering
                    # failure must propagate out to run()'s outer handler
                    # so the WHOLE ceremony falls back to the plain path
                    # (spec §3.2 item 7) rather than spinning forever on a
                    # tick that can never succeed.
                    crest_text = self._render_crest_text(frame, elapsed, tier)
                    live.update(
                        Group(crest_text, self._wake.render_frame()), refresh=True,
                    )

                    if not self._ignited and elapsed >= frame.max_delay_s:
                        self._ignited = True
                        ignite_elapsed = elapsed
                        if self._on_ignition is not None:
                            try:
                                self._on_ignition()
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "[awakening] on_ignition callback failed",
                                    exc_info=True,
                                )

                    if self._skip_requested:
                        break
                    if self._ignited:
                        anchor = ignite_elapsed if ignite_elapsed is not None else elapsed
                        holding_for = elapsed - anchor
                        if self._wake.model.is_live or holding_for >= _HOLD_GUARD_S:
                            break

                    await asyncio.sleep(_ANIMATION_TICK_S)

                await self._cool_down(frame, tier, live)
        finally:
            if reader_cm is not None:
                reader_cm.__exit__(None, None, None)

        self._print_cooled_header()

    async def _cool_down(self, frame: CrestFrame, tier: ColorTier, live: Live) -> None:
        """A couple of tint sub-frames (~0.4s) before the ``Live`` region
        closes (``transient=True`` erases it -- the cooled header below is
        the only thing left on screen)."""
        accent_style = style_for(Token.ACCENT, tier) or "accent"

        # Sub-frame 1: hold the fully-revealed gradient crest.
        full = self._render_crest_text(frame, frame.max_delay_s + 1.0, tier)
        live.update(Group(full, self._wake.render_frame()), refresh=True)
        await asyncio.sleep(_COOLDOWN_SUBFRAME_S)

        # Sub-frame 2: collapse to a single accent-tinted mark.
        tint = Text("ov · ouroboros", style=accent_style)
        live.update(Group(tint), refresh=True)
        await asyncio.sleep(_COOLDOWN_SUBFRAME_S)

    def _render_crest_text(
        self, frame: CrestFrame, elapsed: float, tier: ColorTier,
    ) -> RenderableType:
        """Render the crest as revealed so far -- cells whose ``delay_s``
        has elapsed draw solid; everything else is blank space. Draws
        tail-to-head purely as a function of the (test-injectable) clock."""
        if not frame.cells or frame.cols <= 0 or frame.rows <= 0:
            return Text("")
        grid = {(c.x, c.y): c for c in frame.cells if c.delay_s <= elapsed}
        text = Text()
        for y in range(frame.rows):
            for x in range(frame.cols):
                cell = grid.get((x, y))
                if cell is None:
                    text.append(" ")
                else:
                    text.append(cell.glyph, style=style_for_cell(cell, tier))
            if y < frame.rows - 1:
                text.append("\n")
        return text

    def _poll_keys(self, poll: Callable[[], bytes]) -> None:
        """Read whatever is available this tick. Esc/CR/LF request a skip;
        every other decoded char is appended to ``typed_prefix`` -- NEVER
        swallowed (load-bearing anti-corruption rule for REPL handoff)."""
        try:
            data = poll()
        except Exception:  # noqa: BLE001
            logger.debug("[awakening] key_source failed", exc_info=True)
            return
        if not data:
            return
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""
        for ch in text:
            if ch in _SKIP_CHARS:
                self.request_skip()
                return
            self.typed_prefix += ch

    # ------------------------------------------------------------------
    # Plain path (no Live) -- attaches to the SAME mounted renderer
    # ------------------------------------------------------------------

    async def _run_plain(self) -> None:
        self.used_plain_path = True
        start = self._clock()
        while True:
            now = self._clock()
            self._wake.flush(now)
            if self._skip_requested:
                break
            if self._wake.model.is_live:
                break
            if (now - start) >= _HOLD_GUARD_S:
                break
            if self._key_source is not None:
                self._poll_keys(self._key_source)
                if self._skip_requested:
                    break
            await asyncio.sleep(_ANIMATION_TICK_S)
        self._print_cooled_header()

    # ------------------------------------------------------------------
    # Cooled header -- printed exactly once, whichever path completes
    # ------------------------------------------------------------------

    def _print_cooled_header(self) -> None:
        if self._header_printed:
            return
        self._header_printed = True
        try:
            header = Text()
            header.append("ov", style="accent")
            header.append(" · ", style="muted")
            header.append("ouroboros", style="heading")
            header.append("   ")
            header.append("live", style="success")
            self._console.print(header)
            if self._context_provider is not None:
                try:
                    ctx = self._context_provider()
                except Exception:  # noqa: BLE001
                    ctx = ""
                if ctx:
                    self._console.print(Text(str(ctx), style="muted"))
        except Exception:  # noqa: BLE001
            logger.debug("[awakening] cooled header print failed", exc_info=True)


async def run_awakening(console: Optional[Console] = None, **kwargs: object) -> "AwakeningConductor":
    """Build a console (if not given) and play the ceremony end-to-end.

    NEVER raises -- ``AwakeningConductor.run`` already guards its whole
    body, and any failure constructing the console falls back to a bare
    :class:`~rich.console.Console`.
    """
    if console is None:
        try:
            from .theme import build_console

            console = build_console()
        except Exception:  # noqa: BLE001
            logger.debug("[awakening] build_console failed; bare Console", exc_info=True)
            console = Console()

    conductor = AwakeningConductor(console, **kwargs)  # type: ignore[arg-type]
    try:
        await conductor.run()
    except Exception:  # noqa: BLE001 -- conductor.run() already never raises; stay defensive
        logger.debug("[awakening] run_awakening failed unexpectedly", exc_info=True)
    return conductor


__all__ = ["AwakeningConductor", "run_awakening"]
