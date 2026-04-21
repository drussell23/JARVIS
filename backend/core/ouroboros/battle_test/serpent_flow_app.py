"""
SerpentFlowApp — Slice 4 of the opt-in split layout arc.
==========================================================

Thin adapter composing :class:`LayoutController` + :class:`SplitLayout`
+ the existing flowing SerpentFlow renderer.

Responsibilities
----------------

* **Boot-time mode selection.** Parse ``--split`` / ``--layout=<mode>``
  from argv at boot; fall through to ``JARVIS_SERPENT_LAYOUT_DEFAULT``
  env default (``flow`` unless operator set it).
* **Event routing.** Callers publish pipeline events via
  :meth:`emit_stream`, :meth:`emit_dashboard`, :meth:`emit_diff`.
  In flow mode these are no-ops at the adapter layer — the existing
  SerpentFlow path already prints. In split/focus mode they push
  into the :class:`SplitLayout` region buffers.
* **Zero-change flow behavior.** When mode is flow, adapter
  constructs with no Rich dependency loaded and every adapter
  method delegates back to the existing SerpentFlow path. Pin
  tested at Slice 5 graduation.
* **Easy escape.** ``/layout flow`` via :mod:`layout_repl` always
  routes back to the flowing default.

Non-goals
---------

* Does NOT modify ``serpent_flow.py`` (1,900+ lines) — adapter sits
  alongside it as an opt-in composition.
* Does NOT take over the terminal by default — headless / sandbox /
  non-TTY runs stay on the existing flowing Console.print() path.
* Does NOT expose a model-callable interface — strictly operator-driven
  (§1 authority).
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from backend.core.ouroboros.battle_test.layout_controller import (
    LayoutController,
    LayoutTransition,
    MODE_FLOW,
    MODE_SPLIT,
    REGION_DASHBOARD,
    REGION_DIFF,
    REGION_STREAM,
    is_focus_mode,
    layout_default_from_env,
    parse_cli_layout_arg,
)
from backend.core.ouroboros.battle_test.split_layout import (
    SplitLayout,
)

logger = logging.getLogger("Ouroboros.SerpentFlowApp")


SERPENT_FLOW_APP_SCHEMA_VERSION: str = "serpent_flow_app.v1"


# ---------------------------------------------------------------------------
# Boot-time mode selection
# ---------------------------------------------------------------------------


def resolve_initial_mode(argv: Optional[Sequence[str]] = None) -> str:
    """Resolve the boot-time layout mode.

    Precedence: CLI arg > env var > ``flow`` default. Every lookup is
    validated — invalid values fall through to the next tier.
    """
    if argv is None:
        argv = sys.argv[1:]
    cli = parse_cli_layout_arg(argv)
    if cli is not None:
        return cli
    return layout_default_from_env()


# ---------------------------------------------------------------------------
# SerpentFlowApp
# ---------------------------------------------------------------------------


class SerpentFlowApp:
    """Operator-facing composition.

    Instantiate once at boot, thread through publish sites:

        app = SerpentFlowApp.from_argv(sys.argv[1:])
        app.emit_stream("CLASSIFY op-abc...")
        app.emit_dashboard("ops=3 cost=$0.12")
        app.emit_diff("--- a/x.py\\n+++ b/x.py\\n@@ ...")

    In flow mode (default), these route through the legacy
    SerpentFlow write path (``Console.print`` — injected via the
    ``stream_writer`` constructor arg for test isolation).

    In split / focus mode, writes route into the
    :class:`SplitLayout`'s bounded per-region buffers, which Rich
    Live re-renders.
    """

    def __init__(
        self,
        *,
        controller: Optional[LayoutController] = None,
        split_layout: Optional[SplitLayout] = None,
        stream_writer: Optional[Any] = None,
        output_stream: Any = None,
    ) -> None:
        self._output_stream = output_stream or sys.stdout
        self._controller = controller or LayoutController()
        self._split = split_layout or SplitLayout(
            controller=self._controller,
            output_stream=self._output_stream,
        )
        # Callback-style flow writer so tests can inject a capture
        # buffer without coupling to any specific Console
        # implementation. When None, we fall back to plain print().
        self._stream_writer = stream_writer
        self._active: bool = False
        self._unsub: Optional[Any] = None

    # --- factories ------------------------------------------------------

    @classmethod
    def from_argv(
        cls, argv: Sequence[str], **kwargs: Any,
    ) -> "SerpentFlowApp":
        """Construct an app with mode resolved from argv."""
        mode = resolve_initial_mode(argv)
        controller = LayoutController(initial_mode=mode)
        return cls(controller=controller, **kwargs)

    # --- introspection --------------------------------------------------

    @property
    def controller(self) -> LayoutController:
        return self._controller

    @property
    def split_layout(self) -> SplitLayout:
        return self._split

    def snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": SERPENT_FLOW_APP_SCHEMA_VERSION,
            "mode": self._controller.mode,
            "is_flow": self._controller.is_flow,
            "is_split": self._controller.is_split,
            "focused_region": self._controller.focused_region,
            "split_active": self._split.active,
        }

    # --- lifecycle ------------------------------------------------------

    def start(self) -> bool:
        """Activate whichever renderer matches the current mode.

        Returns True iff the split renderer came up; False when
        the mode is ``flow`` (flowing path needs no start), when
        not TTY, or when Rich is unavailable. Safe in headless
        environments — returns False without raising.
        """
        if self._active:
            return self._split.active
        if self._controller.is_flow:
            self._active = True
            self._unsub = self._controller.on_change(
                self._on_mode_change,
            )
            return False
        started = self._split.start()
        self._active = True
        self._unsub = self._controller.on_change(
            self._on_mode_change,
        )
        return started

    def stop(self) -> None:
        """Tear down any active renderer. Idempotent."""
        if not self._active:
            return
        self._active = False
        try:
            self._split.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[SerpentFlowApp] stop raise: %s", exc)
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:  # noqa: BLE001
                logger.debug("[SerpentFlowApp] controller unsub failed")
            self._unsub = None

    # --- emit — public publish surface ---------------------------------

    def emit_stream(self, text: str) -> None:
        """Publish a pipeline-stream line."""
        self._emit(REGION_STREAM, text)

    def emit_dashboard(self, text: str) -> None:
        """Publish a dashboard line (cost / ops / runtime counters)."""
        self._emit(REGION_DASHBOARD, text)

    def emit_diff(self, text: str) -> None:
        """Publish a diff-preview body (unified diff / Update block)."""
        self._emit(REGION_DIFF, text)

    def _emit(self, region: str, text: str) -> None:
        mode = self._controller.mode
        if mode == MODE_FLOW:
            # Flow mode — route to the existing Console writer.
            self._write_flow(text)
            return
        if mode == MODE_SPLIT or is_focus_mode(mode):
            # Split / focus mode — push into the bounded region buffer.
            self._split.push(region, text)
            # In focus mode, writes to a non-focused region stay
            # buffered so that when the operator returns to split
            # or switches focus, nothing was lost.
            return
        # Defensive: unknown mode (shouldn't happen post-validation).
        self._write_flow(text)

    def _write_flow(self, text: str) -> None:
        if self._stream_writer is not None:
            try:
                self._stream_writer(text)
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[SerpentFlowApp] stream_writer raise: %s", exc,
                )
        # Fallback — bare print. Matches the legacy flow path's
        # ultimate destination; callers with a real SerpentFlow
        # instance pass their Console.print as stream_writer.
        print(text, file=self._output_stream)

    def _on_mode_change(self, txn: LayoutTransition) -> None:
        """Controller transitioned — propagate to the split renderer.

        In flow → split, spin up Rich Live (if TTY). In split → flow,
        tear it down.
        """
        if txn.new_mode == MODE_FLOW:
            try:
                self._split.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[SerpentFlowApp] split.stop raise: %s", exc,
                )
            return
        # New mode is split or focus — ensure the renderer is up.
        if not self._split.active:
            try:
                self._split.start()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[SerpentFlowApp] split.start raise: %s", exc,
                )


__all__ = [
    "SERPENT_FLOW_APP_SCHEMA_VERSION",
    "SerpentFlowApp",
    "resolve_initial_mode",
]
