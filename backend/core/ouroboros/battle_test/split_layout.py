"""
SplitLayout — Rich Layout renderer for opt-in split mode.
===========================================================

Slice 2 of the SerpentFlow Opt-in Split Layout arc.

When :class:`~layout_controller.LayoutController` is in ``split`` or
``focus:<region>`` mode, this module provides the 3-region Rich
Layout rendering surface (stream / dashboard / diff). When the
controller is in ``flow`` mode, this module is inert — the
existing flowing SerpentFlow path is untouched.

Design contract
---------------

* **Lazy Rich import.** Python-level import of this module never
  imports ``rich``. The Rich stack is loaded on first
  :meth:`SplitLayout.start`. Tests that only exercise the
  buffer/push logic don't need Rich installed.
* **Headless / sandbox safe.** :meth:`SplitLayout.start` detects a
  non-interactive TTY and returns False; the renderer stays
  inert and buffers push calls silently, matching the
  ``stream_renderer`` / ``diff_preview`` convention from
  ``CLAUDE.md``.
* **Bounded buffers.** Every region has a max line count (env
  tunable). A slow consumer cannot OOM the renderer.
* **Push-only API.** Callers push region-tagged text; the renderer
  owns the draw schedule. The controller decides which regions
  are visible (all in ``split`` mode, one in ``focus:<r>``).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from backend.core.ouroboros.battle_test.layout_controller import (
    LayoutController,
    LayoutTransition,
    MODE_FLOW,
    MODE_SPLIT,
    REGION_DASHBOARD,
    REGION_DIFF,
    REGION_STREAM,
    focus_region,
    is_focus_mode,
    valid_regions,
)

logger = logging.getLogger("Ouroboros.SplitLayout")


SPLIT_LAYOUT_SCHEMA_VERSION: str = "serpent_split_layout.v1"


# ===========================================================================
# Env knobs
# ===========================================================================


def _region_max_lines() -> int:
    """Per-region buffer cap. Default 500 — wide enough for a long
    diff, tight enough that drifting operators can't OOM the
    renderer."""
    try:
        return max(
            10,
            int(os.environ.get("JARVIS_SPLIT_LAYOUT_MAX_LINES", "500")),
        )
    except (TypeError, ValueError):
        return 500


def _region_title() -> Dict[str, str]:
    """Human-readable region titles. Constants — operators see
    these in the split view header bar."""
    return {
        REGION_STREAM: "Pipeline Stream",
        REGION_DASHBOARD: "Live Dashboard",
        REGION_DIFF: "Diff Preview",
    }


# ===========================================================================
# RegionBuffer — bounded per-region text log
# ===========================================================================


@dataclass
class RegionBuffer:
    """Bounded append-only text buffer for one region."""
    name: str
    maxlen: int
    _lines: Deque[str] = field(default_factory=deque)
    _push_count: int = 0

    def push(self, line: str) -> None:
        # Bound maxlen once the buffer crosses threshold.
        self._lines.append(str(line))
        self._push_count += 1
        while len(self._lines) > self.maxlen:
            self._lines.popleft()

    def snapshot(self) -> Tuple[str, ...]:
        return tuple(self._lines)

    def as_text(self) -> str:
        return "\n".join(self._lines)

    def clear(self) -> None:
        self._lines.clear()

    @property
    def push_count(self) -> int:
        return self._push_count

    @property
    def line_count(self) -> int:
        return len(self._lines)


# ===========================================================================
# SplitLayout — the renderer
# ===========================================================================


class SplitLayout:
    """Owns 3 :class:`RegionBuffer` instances + optional Rich Live.

    Thread-safe. Safe to construct in headless environments — the
    Rich stack is only imported when :meth:`start` is called.
    """

    def __init__(
        self,
        *,
        controller: Optional[LayoutController] = None,
        region_max_lines: Optional[int] = None,
        output_stream: Any = None,
    ) -> None:
        self._controller = controller
        self._lock = threading.Lock()
        cap = region_max_lines or _region_max_lines()
        self._buffers: Dict[str, RegionBuffer] = {
            name: RegionBuffer(name=name, maxlen=cap)
            for name in valid_regions()
        }
        self._output_stream = output_stream or sys.stdout
        self._live: Any = None  # rich.Live instance when active
        self._layout: Any = None  # rich.layout.Layout when active
        self._active: bool = False
        self._unsub_controller: Optional[Callable[[], None]] = None

    # --- push / snapshot ----------------------------------------------

    def push(self, region: str, text: str) -> bool:
        """Append ``text`` to ``region``'s buffer.

        Returns False when the region name is unknown (fail-closed —
        silently dropped writes could mask wiring bugs).
        """
        if region not in self._buffers:
            logger.debug(
                "[SplitLayout] push rejected: unknown region %r", region,
            )
            return False
        with self._lock:
            self._buffers[region].push(text)
        if self._active:
            self._refresh_if_live()
        return True

    def clear(self, region: Optional[str] = None) -> None:
        """Clear one region (by name) or every region."""
        with self._lock:
            if region is None:
                for buf in self._buffers.values():
                    buf.clear()
            elif region in self._buffers:
                self._buffers[region].clear()

    def snapshot(self) -> Dict[str, Tuple[str, ...]]:
        """Snapshot all buffers. Used by tests + headless verification."""
        with self._lock:
            return {
                name: buf.snapshot() for name, buf in self._buffers.items()
            }

    def stats(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            return {
                name: {
                    "push_count": buf.push_count,
                    "line_count": buf.line_count,
                    "maxlen": buf.maxlen,
                }
                for name, buf in self._buffers.items()
            }

    # --- visibility -----------------------------------------------------

    def visible_regions(self) -> List[str]:
        """Which regions should render, given the controller mode.

        When no controller is attached, defaults to every region.
        """
        if self._controller is None:
            return list(valid_regions())
        mode = self._controller.mode
        if mode == MODE_FLOW:
            return []
        if mode == MODE_SPLIT:
            return list(valid_regions())
        if is_focus_mode(mode):
            region = focus_region(mode)
            return [region] if region else []
        return []

    # --- Rich integration — lazy, TTY-gated ---------------------------

    def is_tty(self) -> bool:
        stream = self._output_stream
        return hasattr(stream, "isatty") and stream.isatty()

    def start(self) -> bool:
        """Activate the live Rich renderer.

        Returns True on successful activation, False when the
        environment is not interactive (headless / CI / sandbox)
        or when Rich is unavailable. Idempotent — calling twice is
        a no-op.
        """
        if self._active:
            return True
        if not self.is_tty():
            logger.debug(
                "[SplitLayout] start: non-TTY — renderer stays inert",
            )
            return False
        try:
            from rich.layout import Layout
            from rich.live import Live
        except ImportError:
            logger.warning(
                "[SplitLayout] rich not available — renderer stays inert",
            )
            return False
        with self._lock:
            self._layout = self._build_layout(Layout)
            self._live = Live(
                self._layout,
                console=None,
                refresh_per_second=8,
                screen=False,
                transient=False,
            )
            self._live.start()
            self._active = True
        # Subscribe to controller transitions so layout switches
        # between split / focus modes on the fly.
        if self._controller is not None:
            self._unsub_controller = self._controller.on_change(
                self._on_mode_change,
            )
        logger.info("[SplitLayout] started")
        return True

    def stop(self) -> None:
        """Deactivate the Rich renderer. Safe to call repeatedly."""
        with self._lock:
            if not self._active:
                return
            try:
                if self._live is not None:
                    self._live.stop()
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[SplitLayout] stop: Live.stop raised: %s", exc,
                )
            self._live = None
            self._layout = None
            self._active = False
        if self._unsub_controller is not None:
            try:
                self._unsub_controller()
            except Exception:  # noqa: BLE001
                logger.debug("[SplitLayout] controller unsub failed")
            self._unsub_controller = None
        logger.info("[SplitLayout] stopped")

    @property
    def active(self) -> bool:
        return self._active

    # --- Rich helpers ---------------------------------------------------

    def _build_layout(self, layout_cls: Any) -> Any:
        """Compose a :class:`rich.layout.Layout` from the currently
        visible regions."""
        from rich.panel import Panel  # local — Rich optional
        from rich.text import Text

        visible = self.visible_regions()
        if not visible:
            # Controller in flow mode but SplitLayout.start was called
            # anyway — render a stub so the Live handle is non-empty.
            layout = layout_cls(name="empty")
            layout.update(Panel(
                Text(
                    "SerpentFlow in flow mode. "
                    "Use /layout split or /layout focus <region> to activate.",
                ),
                title="serpent / layout",
            ))
            return layout
        # Focus mode: single region, full frame.
        if len(visible) == 1:
            layout = layout_cls(name=visible[0])
            region = visible[0]
            buf = self._buffers[region]
            layout.update(Panel(
                Text(buf.as_text()),
                title=_region_title().get(region, region),
            ))
            return layout
        # Split mode: every region in its own panel, stacked rows.
        layout = layout_cls(name="root")
        children = [layout_cls(name=r) for r in visible]
        layout.split_column(*children)
        titles = _region_title()
        for child in children:
            region = child.name or ""
            if region in self._buffers:
                child.update(Panel(
                    Text(self._buffers[region].as_text()),
                    title=titles.get(region, region),
                ))
        return layout

    def _refresh_if_live(self) -> None:
        """Rebuild the layout against the current buffer state.

        Best-effort: any Rich-level exception is swallowed — the push
        path must never raise from an observer.
        """
        if not self._active or self._layout is None:
            return
        try:
            from rich.layout import Layout as _Layout  # noqa: F401
            new_layout = self._build_layout(_Layout)
            # Rich's Layout.update only replaces the content of one
            # layout; for multi-region we rebuild root.
            self._live.update(new_layout)
            self._layout = new_layout
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[SplitLayout] refresh exception: %s", exc,
            )

    def _on_mode_change(self, _txn: LayoutTransition) -> None:
        """Controller transition → rebuild layout."""
        self._refresh_if_live()


# ===========================================================================
# Module singleton (test + production convenience)
# ===========================================================================


_default_split: Optional[SplitLayout] = None
_singleton_lock = threading.Lock()


def get_default_split_layout() -> SplitLayout:
    """Return the process-wide :class:`SplitLayout` singleton.

    Lazily constructs one wired to the default
    :class:`LayoutController`.
    """
    global _default_split
    with _singleton_lock:
        if _default_split is None:
            from backend.core.ouroboros.battle_test.layout_controller import (
                get_default_layout_controller,
            )
            _default_split = SplitLayout(
                controller=get_default_layout_controller(),
            )
        return _default_split


def set_default_split_layout(split: SplitLayout) -> None:
    """Test hook."""
    global _default_split
    with _singleton_lock:
        _default_split = split


def reset_default_split_layout() -> None:
    """Test helper."""
    global _default_split
    with _singleton_lock:
        if _default_split is not None:
            try:
                _default_split.stop()
            except Exception:  # noqa: BLE001
                pass
        _default_split = None


__all__ = [
    "RegionBuffer",
    "SPLIT_LAYOUT_SCHEMA_VERSION",
    "SplitLayout",
    "get_default_split_layout",
    "reset_default_split_layout",
    "set_default_split_layout",
]
