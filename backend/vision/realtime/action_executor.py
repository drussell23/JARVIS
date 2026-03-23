"""
Vision action executor — click / type / scroll via pyautogui.

Design goals
------------
- All pyautogui calls are dispatched through ``asyncio.to_thread()`` so they
  never block the event loop.
- Every executed action is recorded in ``committed_actions`` (a set of
  action_ids) for idempotency: callers can detect duplicate dispatches.
- All exceptions are caught and surfaced as ``ActionResult(success=False)``;
  nothing propagates to the caller.
- Zero hardcoding: any tunable (e.g. typewrite interval) is sourced from
  environment variables.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Set, Tuple

import pyautogui

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven tunables — no hardcoding
# ---------------------------------------------------------------------------
_TYPEWRITE_INTERVAL_S = float(os.environ.get("VISION_TYPEWRITE_INTERVAL_S", "0.05"))
"""Seconds between each keystroke when typing. Configurable for speed/stability."""

_CLICK_PAUSE_S = float(os.environ.get("VISION_CLICK_PAUSE_S", "0.0"))
"""Optional pause injected before a click action (useful for slow UIs)."""


# ---------------------------------------------------------------------------
# Public enumerations and dataclasses
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    """Supported action primitives."""

    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"


@dataclass
class ActionRequest:
    """Describes a single UI action to execute.

    Parameters
    ----------
    action_id:
        Caller-assigned unique identifier. Used for idempotency tracking.
    action_type:
        One of :class:`ActionType` — CLICK, TYPE, or SCROLL.
    coords:
        ``(x, y)`` screen coordinates for CLICK actions.
    text:
        String to type for TYPE actions.
    scroll_amount:
        Number of scroll clicks for SCROLL actions (negative = down).
    """

    action_id: str
    action_type: ActionType
    coords: Optional[Tuple[int, int]] = None
    text: Optional[str] = None
    scroll_amount: Optional[int] = None


@dataclass
class ActionResult:
    """Outcome of a single :class:`ActionRequest` execution.

    Parameters
    ----------
    success:
        ``True`` if pyautogui completed without raising.
    action_id:
        Echoed from the originating :class:`ActionRequest`.
    action_type:
        Echoed from the originating :class:`ActionRequest`.
    latency_ms:
        Wall-clock time from ``execute()`` entry to return, in milliseconds.
    error:
        Human-readable exception message when ``success`` is ``False``.
    """

    success: bool
    action_id: str
    action_type: ActionType
    latency_ms: float
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# ActionExecutor
# ---------------------------------------------------------------------------

class ActionExecutor:
    """Async wrapper around pyautogui with action-id idempotency tracking.

    Usage
    -----
    ::

        executor = ActionExecutor()
        req = ActionRequest(
            action_id="act-001",
            action_type=ActionType.CLICK,
            coords=(523, 187),
        )
        result = await executor.execute(req)

    All pyautogui operations run in a thread pool via ``asyncio.to_thread()``
    to avoid blocking the event loop during GUI interaction.
    """

    def __init__(self) -> None:
        self.committed_actions: Set[str] = set()
        """Set of action_ids that have been successfully executed."""

    async def execute(self, request: ActionRequest) -> ActionResult:
        """Execute *request* and return an :class:`ActionResult`.

        The action runs off the event loop via ``asyncio.to_thread()``.
        On success the ``action_id`` is recorded in :attr:`committed_actions`.
        Any exception is caught and surfaces in the returned result.

        Parameters
        ----------
        request:
            The action to execute.

        Returns
        -------
        ActionResult
        """
        t0 = time.monotonic()
        try:
            await self._dispatch(request)
            latency_ms = (time.monotonic() - t0) * 1000
            self.committed_actions.add(request.action_id)
            logger.debug(
                "Action %s (%s) succeeded in %.1f ms",
                request.action_id,
                request.action_type,
                latency_ms,
            )
            return ActionResult(
                success=True,
                action_id=request.action_id,
                action_type=request.action_type,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            logger.warning(
                "Action %s (%s) failed after %.1f ms: %s",
                request.action_id,
                request.action_type,
                latency_ms,
                exc,
            )
            return ActionResult(
                success=False,
                action_id=request.action_id,
                action_type=request.action_type,
                latency_ms=latency_ms,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Internal dispatch — prefer Ghost Hands, fallback to pyautogui
    # ------------------------------------------------------------------

    async def _dispatch(self, request: ActionRequest) -> None:
        """Route *request* to Ghost Hands BackgroundActuator (preferred) or pyautogui.

        v305.0: Ghost Hands provides focus-preserving execution via
        Playwright (browsers), AppleScript (native apps), and CGEvent
        (low-level). Falls back to pyautogui only if Ghost Hands is
        unavailable. Zero hardcoding — backend selection is automatic.
        """
        # Try Ghost Hands first (focus-preserving, multi-backend)
        actuator = await self._get_ghost_hands_actuator()
        if actuator is not None:
            await self._dispatch_via_ghost_hands(actuator, request)
            return

        # Fallback to pyautogui (steals focus, single backend)
        logger.debug(
            "Ghost Hands unavailable for %s, falling back to pyautogui",
            request.action_id,
        )
        await self._dispatch_via_pyautogui(request)

    async def _get_ghost_hands_actuator(self):
        """Get Ghost Hands BackgroundActuator singleton (cached)."""
        if not hasattr(self, '_actuator_cache'):
            self._actuator_cache = None
            self._actuator_checked = False

        if self._actuator_checked:
            return self._actuator_cache

        self._actuator_checked = True
        try:
            from backend.ghost_hands.background_actuator import get_background_actuator
            self._actuator_cache = await asyncio.wait_for(
                get_background_actuator(), timeout=2.0,
            )
            logger.info("[ActionExecutor] Ghost Hands BackgroundActuator connected")
        except Exception as exc:
            logger.debug("[ActionExecutor] Ghost Hands not available: %s", exc)
            self._actuator_cache = None

        return self._actuator_cache

    async def _dispatch_via_ghost_hands(self, actuator, request: ActionRequest) -> None:
        """Execute action via Ghost Hands BackgroundActuator."""
        action_type = request.action_type

        if action_type == ActionType.CLICK:
            if request.coords is None:
                raise ValueError(f"CLICK action {request.action_id!r} requires coords")
            report = await actuator.click(coordinates=request.coords)
            if hasattr(report, 'result') and str(report.result) == 'FAILED':
                raise RuntimeError(f"Ghost Hands click failed: {getattr(report, 'error', 'unknown')}")

        elif action_type == ActionType.TYPE:
            if request.text is None:
                raise ValueError(f"TYPE action {request.action_id!r} requires text")
            report = await actuator.type_text(request.text)
            if hasattr(report, 'result') and str(report.result) == 'FAILED':
                raise RuntimeError(f"Ghost Hands type failed: {getattr(report, 'error', 'unknown')}")

        elif action_type == ActionType.SCROLL:
            if request.scroll_amount is None:
                raise ValueError(f"SCROLL action {request.action_id!r} requires scroll_amount")
            # Ghost Hands may not have a direct scroll — fall back to pyautogui
            await asyncio.to_thread(self._do_scroll, request.scroll_amount)

        else:
            raise ValueError(f"Unsupported action_type {action_type!r}")

    async def _dispatch_via_pyautogui(self, request: ActionRequest) -> None:
        """Fallback: execute action via pyautogui (steals focus)."""
        action_type = request.action_type

        if action_type == ActionType.CLICK:
            if request.coords is None:
                raise ValueError(f"CLICK action {request.action_id!r} requires coords")
            x, y = request.coords
            await asyncio.to_thread(self._do_click, x, y)

        elif action_type == ActionType.TYPE:
            if request.text is None:
                raise ValueError(f"TYPE action {request.action_id!r} requires text")
            await asyncio.to_thread(self._do_type, request.text)

        elif action_type == ActionType.SCROLL:
            if request.scroll_amount is None:
                raise ValueError(f"SCROLL action {request.action_id!r} requires scroll_amount")
            await asyncio.to_thread(self._do_scroll, request.scroll_amount)

        else:
            raise ValueError(f"Unsupported action_type {action_type!r}")

    # ------------------------------------------------------------------
    # Synchronous pyautogui wrappers — run inside to_thread (fallback)
    # ------------------------------------------------------------------

    @staticmethod
    def _do_click(x: int, y: int) -> None:
        if _CLICK_PAUSE_S > 0:
            time.sleep(_CLICK_PAUSE_S)
        pyautogui.click(x, y)

    @staticmethod
    def _do_type(text: str) -> None:
        pyautogui.typewrite(text, interval=_TYPEWRITE_INTERVAL_S)

    @staticmethod
    def _do_scroll(amount: int) -> None:
        pyautogui.scroll(amount)
