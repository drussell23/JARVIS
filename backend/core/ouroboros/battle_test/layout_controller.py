"""
LayoutController — deterministic operator-owned presentation state.
===================================================================

Slice 1 of the SerpentFlow Opt-in Split Layout arc. Closes the CC-parity
gap "No multi-monitor layout for SerpentFlow" under the design
constraint from ``feedback_tui_design.md``:

* **Default stays flowing.** The TUI is a single scrolling panel by
  default — matching Claude Code's actual feel, not the
  three-pane misconception.
* **Operator authority only.** The model never selects a layout;
  the operator does via env flag, CLI arg, or ``/layout`` REPL
  command. Manifesto §1.
* **Easy escape.** A single verb (``/layout flow``) returns to the
  flowing default regardless of current state.

Modes
-----

* ``flow``  (default)  — no layout chrome; existing SerpentFlow path.
* ``split``            — 3 named regions: stream / dashboard / diff.
* ``focus:<region>``   — one region full-frame; the others hidden.

This module is pure state + regex. Rich is not imported here
(:mod:`split_layout` owns the renderer). A headless / sandbox /
CI runtime can construct a controller and change modes without
ever touching a terminal.

Authority boundary
------------------

* §1 deterministic — no LLM calls, no tool use, no authority
  surface; the controller is a plain state machine.
* §7 fail-closed — unknown modes raise :class:`LayoutError`; the
  controller is never left in an ambiguous state.
* §8 observable — every transition fires listeners with the
  old-mode / new-mode pair for downstream observability.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.LayoutController")


LAYOUT_CONTROLLER_SCHEMA_VERSION: str = "serpent_layout.v1"


# ===========================================================================
# Mode vocabulary — frozen string constants so clients can hard-code
# ===========================================================================


MODE_FLOW: str = "flow"
MODE_SPLIT: str = "split"
# Focus is a family: MODE_FOCUS_PREFIX + region name.
MODE_FOCUS_PREFIX: str = "focus:"

REGION_STREAM: str = "stream"
REGION_DASHBOARD: str = "dashboard"
REGION_DIFF: str = "diff"

_VALID_REGIONS: Tuple[str, ...] = (
    REGION_STREAM, REGION_DASHBOARD, REGION_DIFF,
)

# Regex: alphabetic only, bounded length. Prevents injection of
# control chars / ANSI / path-like strings into a mode string.
_MODE_RX = re.compile(r"^[a-z][a-z0-9_:\-]{0,31}$")


# ===========================================================================
# Exceptions
# ===========================================================================


class LayoutError(Exception):
    """Invalid layout mode / region / transition."""


# ===========================================================================
# Immutable transition payload — emitted to listeners
# ===========================================================================


@dataclass(frozen=True)
class LayoutTransition:
    """Frozen record of one mode change."""
    old_mode: str
    new_mode: str
    reason: str = ""  # free-form operator note (e.g. "cli_arg_split")

    def project(self) -> Dict[str, Any]:
        return {
            "schema_version": LAYOUT_CONTROLLER_SCHEMA_VERSION,
            "old_mode": self.old_mode,
            "new_mode": self.new_mode,
            "reason": self.reason,
        }


# ===========================================================================
# Public helpers — mode parsing / validation
# ===========================================================================


def is_focus_mode(mode: str) -> bool:
    return isinstance(mode, str) and mode.startswith(MODE_FOCUS_PREFIX)


def focus_region(mode: str) -> Optional[str]:
    """Return the region id for a focus mode, or ``None`` otherwise.

    ``focus_region("focus:stream") -> "stream"``; any other input
    returns ``None``.
    """
    if not is_focus_mode(mode):
        return None
    region = mode[len(MODE_FOCUS_PREFIX):]
    return region if region in _VALID_REGIONS else None


def valid_regions() -> Tuple[str, ...]:
    return _VALID_REGIONS


def is_valid_mode(mode: str) -> bool:
    """Strict mode-string validator."""
    if not isinstance(mode, str) or not _MODE_RX.match(mode):
        return False
    if mode == MODE_FLOW or mode == MODE_SPLIT:
        return True
    if is_focus_mode(mode):
        return focus_region(mode) is not None
    return False


# ===========================================================================
# Env + CLI helpers
# ===========================================================================


def layout_default_from_env() -> str:
    """Read ``JARVIS_SERPENT_LAYOUT_DEFAULT``.

    Returns one of the validated mode strings. Falls back to
    :data:`MODE_FLOW` when the env var is unset OR carries an
    invalid value (fail-safe: bad operator input never wedges the
    controller into an unknown state).
    """
    raw = os.environ.get("JARVIS_SERPENT_LAYOUT_DEFAULT", "").strip().lower()
    if not raw:
        return MODE_FLOW
    if is_valid_mode(raw):
        return raw
    logger.warning(
        "[LayoutController] ignoring JARVIS_SERPENT_LAYOUT_DEFAULT=%r "
        "— not a valid mode; defaulting to 'flow'", raw,
    )
    return MODE_FLOW


def parse_cli_layout_arg(argv: Sequence[str]) -> Optional[str]:
    """Parse ``--split`` / ``--layout=<mode>`` out of an argv list.

    Returns the requested mode or ``None`` if no layout arg was
    supplied. Never raises — unknown args bubble up to the caller's
    main arg parser.
    """
    for i, arg in enumerate(argv):
        if arg == "--split":
            return MODE_SPLIT
        if arg == "--flow":
            return MODE_FLOW
        if arg.startswith("--layout="):
            candidate = arg[len("--layout="):].strip().lower()
            if is_valid_mode(candidate):
                return candidate
            logger.warning(
                "[LayoutController] ignoring invalid --layout value %r",
                candidate,
            )
            return None
        if arg == "--layout" and i + 1 < len(argv):
            candidate = argv[i + 1].strip().lower()
            if is_valid_mode(candidate):
                return candidate
            logger.warning(
                "[LayoutController] ignoring invalid --layout value %r",
                candidate,
            )
            return None
    return None


# ===========================================================================
# LayoutController — the state machine
# ===========================================================================


class LayoutController:
    """Operator-facing state machine.

    Thread-safe: transitions happen under a lock so listeners always
    see a consistent old→new pair.
    """

    def __init__(
        self, *, initial_mode: Optional[str] = None,
    ) -> None:
        if initial_mode is None:
            initial_mode = layout_default_from_env()
        if not is_valid_mode(initial_mode):
            raise LayoutError(
                f"invalid initial layout mode: {initial_mode!r}"
            )
        self._mode: str = initial_mode
        self._lock = threading.Lock()
        self._listeners: List[Callable[[LayoutTransition], None]] = []

    # --- state introspection ------------------------------------------

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @property
    def is_flow(self) -> bool:
        return self.mode == MODE_FLOW

    @property
    def is_split(self) -> bool:
        return self.mode == MODE_SPLIT

    @property
    def focused_region(self) -> Optional[str]:
        return focus_region(self.mode)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": LAYOUT_CONTROLLER_SCHEMA_VERSION,
            "mode": self.mode,
            "is_flow": self.is_flow,
            "is_split": self.is_split,
            "focused_region": self.focused_region,
            "valid_regions": list(_VALID_REGIONS),
        }

    # --- transitions --------------------------------------------------

    def set_mode(self, mode: str, *, reason: str = "") -> LayoutTransition:
        """Validate + apply a mode change. Fires listeners even when
        ``mode == current mode`` (idempotent "nudge" useful for
        re-triggering a re-render)."""
        if not is_valid_mode(mode):
            raise LayoutError(f"invalid layout mode: {mode!r}")
        with self._lock:
            old = self._mode
            self._mode = mode
            txn = LayoutTransition(
                old_mode=old, new_mode=mode, reason=reason,
            )
            listeners = list(self._listeners)
        self._emit(listeners, txn)
        logger.info(
            "[LayoutController] transition %s -> %s (reason=%r)",
            txn.old_mode, txn.new_mode, txn.reason,
        )
        return txn

    def to_flow(self, *, reason: str = "") -> LayoutTransition:
        return self.set_mode(MODE_FLOW, reason=reason)

    def to_split(self, *, reason: str = "") -> LayoutTransition:
        return self.set_mode(MODE_SPLIT, reason=reason)

    def to_focus(
        self, region: str, *, reason: str = "",
    ) -> LayoutTransition:
        if region not in _VALID_REGIONS:
            raise LayoutError(
                f"unknown focus region: {region!r} "
                f"(valid: {list(_VALID_REGIONS)})"
            )
        return self.set_mode(
            MODE_FOCUS_PREFIX + region, reason=reason,
        )

    # --- listener hook ------------------------------------------------

    def on_change(
        self, listener: Callable[[LayoutTransition], None],
    ) -> Callable[[], None]:
        """Subscribe to mode transitions. Best-effort: listener
        exceptions are logged and swallowed."""
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return _unsub

    def _emit(
        self,
        listeners: List[Callable[[LayoutTransition], None]],
        txn: LayoutTransition,
    ) -> None:
        for l in listeners:
            try:
                l(txn)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[LayoutController] listener raise: %s", exc,
                )


# ===========================================================================
# Module singleton
# ===========================================================================


_default_controller: Optional[LayoutController] = None
_singleton_lock = threading.Lock()


def get_default_layout_controller() -> LayoutController:
    global _default_controller
    with _singleton_lock:
        if _default_controller is None:
            _default_controller = LayoutController()
        return _default_controller


def set_default_layout_controller(controller: LayoutController) -> None:
    """Test hook — caller-built controller replaces the singleton.

    Production code never calls this; the CLI boot path is the only
    legitimate injection site, which routes through
    :func:`reset_default_layout_controller` + init.
    """
    global _default_controller
    with _singleton_lock:
        _default_controller = controller


def reset_default_layout_controller() -> None:
    """Test helper — drop the singleton so the next getter re-reads env."""
    global _default_controller
    with _singleton_lock:
        _default_controller = None


__all__ = [
    "LAYOUT_CONTROLLER_SCHEMA_VERSION",
    "LayoutController",
    "LayoutError",
    "LayoutTransition",
    "MODE_FLOW",
    "MODE_FOCUS_PREFIX",
    "MODE_SPLIT",
    "REGION_DASHBOARD",
    "REGION_DIFF",
    "REGION_STREAM",
    "focus_region",
    "get_default_layout_controller",
    "is_focus_mode",
    "is_valid_mode",
    "layout_default_from_env",
    "parse_cli_layout_arg",
    "reset_default_layout_controller",
    "set_default_layout_controller",
    "valid_regions",
]
