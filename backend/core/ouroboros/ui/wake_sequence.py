"""backend/core/ouroboros/ui/wake_sequence.py -- the ov boot cockpit's
wake-sequence renderer.

Unlike a scripted boot animation, this renders *true* system readiness: it
subscribes to :class:`BootTimer` phase transitions (via ``add_observer``) and
draws each phase as active -> done from the real ``PhaseRecord`` stream. The
organism is genuinely waking; the terminal shows exactly that (spec §5).

Two collaborators, split for testability:

* :class:`WakeModel` -- pure state. Coalesces repeated records per phase,
  preserves first-seen order, exposes an ``is_live`` flag (all phases done).
* :class:`WakeSequenceRenderer` -- binds the model to a themed console with a
  debounce gate so a fast Venom spin-up coalesces into one render per refresh
  window instead of flooding the terminal (bulletproof mandate #4).

Dependency rule: this module imports only ``ui.theme`` + Rich + the
``PhaseRecord`` dataclass. It performs no I/O beyond the console it is given.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from rich.console import Console, Group, RenderableType
from rich.text import Text

from . import theme

logger = logging.getLogger("Ouroboros.UI.Wake")


# ===========================================================================
# Pure state model
# ===========================================================================


class WakeModel:
    """Coalesced view of boot phases: name -> is_active, in first-seen order.

    A phase seen multiple times (in-flight then completed) collapses to one
    entry whose state is the latest observation. NEVER raises on ``observe``.
    """

    def __init__(self) -> None:
        self._order: List[str] = []
        self._active: dict = {}  # name -> bool (True == in flight)

    def observe(self, record: object) -> None:
        """Fold one :class:`PhaseRecord` into the model. NEVER raises."""
        try:
            name = getattr(record, "name", "") or ""
            if not name:
                return
            in_flight = bool(getattr(record, "is_in_flight", False))
            if name not in self._active:
                self._order.append(name)
            self._active[name] = in_flight
        except Exception:  # noqa: BLE001
            logger.debug("[wake] observe failed", exc_info=True)

    def phases(self) -> List[Tuple[str, bool]]:
        """(name, is_active) pairs in first-seen order."""
        return [(n, bool(self._active.get(n, False))) for n in self._order]

    @property
    def is_live(self) -> bool:
        """True once there is at least one phase and none remain active."""
        if not self._order:
            return False
        return not any(self._active.get(n, False) for n in self._order)


# ===========================================================================
# Renderer + debounce gate
# ===========================================================================


class WakeSequenceRenderer:
    """Binds a :class:`WakeModel` to a themed console with a debounce gate.

    Usage::

        renderer = WakeSequenceRenderer(build_console())
        renderer.attach(get_default_timer())   # subscribe to real boot phases
        # a driver loop calls renderer.flush(time.monotonic()) each tick

    The debounce gate (``should_flush`` / ``flush``) guarantees at most one
    render per ``min_interval_s`` window regardless of how fast phase events
    arrive -- rapid observations coalesce in the model and surface as a single
    frame. ``render_count`` is exposed for tests + telemetry.
    """

    def __init__(self, console: Console, *, min_interval_s: float = 0.016) -> None:
        self._console = console
        self._model = WakeModel()
        self._min_interval_s = max(0.0, float(min_interval_s))
        self._dirty = False
        self._last_flush_at: Optional[float] = None
        self.render_count = 0

    @property
    def model(self) -> WakeModel:
        return self._model

    # ---- input --------------------------------------------------------

    def observe(self, record: object) -> None:
        """Fold a phase record in and mark the frame dirty. NEVER raises."""
        self._model.observe(record)
        self._dirty = True

    def _on_phase(self, record: object) -> None:
        """BootTimer observer callback. Isolated -- never raises into boot."""
        try:
            self.observe(record)
        except Exception:  # noqa: BLE001
            logger.debug("[wake] _on_phase failed", exc_info=True)

    def attach(self, timer: object) -> None:
        """Subscribe to a :class:`BootTimer`'s live phase transitions."""
        try:
            add = getattr(timer, "add_observer", None)
            if callable(add):
                add(self._on_phase)
        except Exception:  # noqa: BLE001
            logger.debug("[wake] attach failed", exc_info=True)

    # ---- debounce -----------------------------------------------------

    def should_flush(self, now: float) -> bool:
        """True when there is new state AND the refresh window has elapsed."""
        if not self._dirty:
            return False
        if self._last_flush_at is None:
            return True
        return (now - self._last_flush_at) >= self._min_interval_s

    def flush(self, now: float) -> bool:
        """Render the current frame if the debounce gate permits.

        Returns ``True`` when a render happened. NEVER raises.
        """
        if not self.should_flush(now):
            return False
        try:
            self._console.print(self.render_frame())
        except Exception:  # noqa: BLE001
            logger.debug("[wake] flush render failed", exc_info=True)
        self._dirty = False
        self._last_flush_at = now
        self.render_count += 1
        return True

    # ---- rendering ----------------------------------------------------

    def render_frame(self) -> RenderableType:
        """Build the themed wake frame from current model state.

        Uses semantic theme tokens by name (resolved against the console's
        pushed theme), so it degrades with the tier and leaks no escapes at
        the NONE tier. NEVER raises -- returns a bare Text on failure.
        """
        try:
            parts: List[RenderableType] = []

            header = Text()
            header.append("ov", style="accent")
            header.append(" · ouroboros", style="heading")
            parts.append(header)
            parts.append(Text("self-evolving engineering organism", style="muted"))
            parts.append(Text(""))

            check = theme.mark("check")
            dot = theme.mark("dot")
            for name, active in self._model.phases():
                line = Text()
                if active:
                    line.append(f"{dot} ", style="accent")
                    line.append(name, style="body")
                else:
                    line.append(f"{check} ", style="muted")
                    line.append(name, style="muted")
                parts.append(line)

            if self._model.is_live:
                parts.append(Text("live", style="success"))

            return Group(*parts)
        except Exception:  # noqa: BLE001
            logger.debug("[wake] render_frame failed", exc_info=True)
            return Text("ov · ouroboros")


__all__ = ["WakeModel", "WakeSequenceRenderer"]
