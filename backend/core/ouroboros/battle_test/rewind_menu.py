"""Esc-Esc rewind — a locked list of restore points, in the palette.

CC's rewind menu meets O+V's reality: the daemon keeps WORKING while a
menu is open, so by the time the operator picks a restore point the world
it described may be gone. Refreshing the menu faster is a workaround
wearing a fix's clothes — the root cause is active background autonomy
during a user-intervention event. So opening the menu takes the
**Transactional Viewport Lock**:

  1. Esc-Esc (empty prompt) → this client sends ``autonomy: pause`` and a
     ``rewind_list`` request over the bridge;
  2. the daemon suspends intake (refcounted across panes, TTL'd against
     wedged holders, released on disconnect) and answers — addressed to
     this cockpit alone — with a STATIC snapshot of the /undo planner's
     restore points;
  3. the menu renders through the SAME gated-completer palette Float that
     history search uses — no second overlay system;
  4. selecting a point inserts ``/undo N`` into the prompt (an explicit
     Enter confirms a git revert — a rollback should never be one
     accidental keystroke), and CLOSING the menu — accept or dismiss —
     releases the hold. The daemon's TTL and disconnect-release back this
     up, and a daemon that never answers (pre-rewind build) trips a local
     failsafe that releases the hold and says so.

Env: ``JARVIS_REWIND_MENU_ENABLED`` (default true),
``JARVIS_REWIND_REPLY_TIMEOUT_S`` (failsafe, default 5).

NEVER raises into key dispatch, the read loop, or a repaint.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

REWIND_MENU_SCHEMA_VERSION: str = "rewind_menu.1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_REWIND_MENU_ENABLED"
REPLY_TIMEOUT_ENV_VAR: str = "JARVIS_REWIND_REPLY_TIMEOUT_S"

REWIND_ACTION: str = "app:rewind"
REWIND_DEFAULT_KEYS = ("esc esc",)


def is_rewind_menu_enabled() -> bool:
    """Master flag — default true. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _reply_timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get(REPLY_TIMEOUT_ENV_VAR, "5")))
    except (TypeError, ValueError):
        return 5.0


class RewindController:
    """The lock's client half + the menu's state machine.

    Lifecycle: ``open()`` acquires the hold and requests the snapshot;
    ``deliver()`` (wired to the client's ``on_rewind_list``) renders it;
    ``close()`` releases the hold exactly once, whatever path led there —
    accept, Esc, empty list, reply timeout."""

    def __init__(
        self,
        client: Any,
        *,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._client = client
        self._notify = notify or (lambda _m: None)
        self.armed = False
        self.candidates: List[Dict[str, Any]] = []
        self._menu_opened = False
        self._hold_released = True
        self._watched_buffers: set = set()
        self._failsafe: Any = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> bool:
        """Acquire the viewport lock and request the snapshot. NEVER
        raises; False when disabled/detached (no hold is taken)."""
        try:
            if not is_rewind_menu_enabled() or self.armed:
                return False
            if not self._client.send_autonomy("pause"):
                return False
            self._hold_released = False
            self.armed = True
            self._menu_opened = False
            self.candidates = []
            if not self._client.send_rewind_request():
                self.close(reason="request failed")
                return False
            self._notify("⏸ autonomy held — fetching restore points…")
            self._arm_failsafe()
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[Rewind] open degraded", exc_info=True)
            self.close(reason="open degraded")
            return False

    def deliver(self, frame: Dict[str, Any]) -> None:
        """The daemon's addressed snapshot arrived — render it. NEVER
        raises."""
        try:
            if not self.armed:
                return
            self._cancel_failsafe()
            raw = frame.get("candidates")
            self.candidates = [c for c in (raw or []) if isinstance(c, dict)]
            if not self.candidates:
                self._notify("nothing to rewind — no reverted-able commits")
                self.close(reason="empty")
                return
            from prompt_toolkit.application.current import get_app_or_none
            app = get_app_or_none()
            if app is None:
                self.close(reason="no app")
                return
            buf = app.current_buffer
            self._watch(buf)
            self._menu_opened = True
            buf.start_completion(select_first=False)
            app.invalidate()
        except Exception:  # noqa: BLE001
            logger.debug("[Rewind] deliver degraded", exc_info=True)
            self.close(reason="deliver degraded")

    def close(self, *, reason: str = "") -> None:
        """Release the hold EXACTLY ONCE. Safe from any path. NEVER
        raises."""
        try:
            self.armed = False
            self._menu_opened = False
            self._cancel_failsafe()
            if not self._hold_released:
                self._hold_released = True
                try:
                    self._client.send_autonomy("resume")
                except Exception:  # noqa: BLE001
                    pass
                self._notify("▶ autonomy released"
                             + (f" ({reason})" if reason else ""))
        except Exception:  # noqa: BLE001
            logger.debug("[Rewind] close degraded", exc_info=True)

    # -- plumbing ----------------------------------------------------------

    def _watch(self, buffer: Any) -> None:
        """Auto-release on menu close — the buffer's own event, so accept
        and Esc and clear all release without enumeration."""
        try:
            if id(buffer) in self._watched_buffers:
                return
            self._watched_buffers.add(id(buffer))

            def _on_change(buf: Any) -> None:
                try:
                    if (
                        self.armed and self._menu_opened
                        and buf.complete_state is None
                    ):
                        self.close()
                except Exception:  # noqa: BLE001
                    self.close()

            buffer.on_completions_changed += _on_change
        except Exception:  # noqa: BLE001
            logger.debug("[Rewind] watch degraded", exc_info=True)

    def _arm_failsafe(self) -> None:
        """A daemon that never answers (pre-rewind build, wedged loop)
        must not leave autonomy frozen on a hold nobody can see."""
        try:
            loop = asyncio.get_event_loop()

            def _fire() -> None:
                if self.armed and not self._menu_opened:
                    self._notify("rewind unavailable on this daemon")
                    self.close(reason="no reply")

            self._failsafe = loop.call_later(_reply_timeout_s(), _fire)
        except Exception:  # noqa: BLE001
            self._failsafe = None

    def _cancel_failsafe(self) -> None:
        try:
            if self._failsafe is not None:
                self._failsafe.cancel()
        except Exception:  # noqa: BLE001
            pass
        self._failsafe = None


def _completer_base() -> Any:
    """prompt_toolkit's ``Completer``, or ``object`` when unavailable.

    INHERITING is load-bearing, not tidy (same lesson history_search
    learned live): prompt_toolkit consumes completers through
    ``get_completions_async``, which only the base class supplies by
    wrapping the sync method. A duck-typed completer passes every direct
    test and raises AttributeError on the first real keystroke."""
    try:
        from prompt_toolkit.completion import Completer
        return Completer
    except Exception:  # noqa: BLE001
        return object


class RewindCompleter(_completer_base()):  # type: ignore[misc]
    """Feeds the locked restore points into the palette while armed.

    Selecting one INSERTS ``/undo N`` — the same typed verb the daemon
    already knows how to execute transactionally — so the keystroke path
    and the typed path cannot drift, and a git revert always takes an
    explicit Enter."""

    def __init__(self, controller: RewindController) -> None:
        self._controller = controller

    def get_completions(self, document: Any, _event: Any = None):
        ctl = self._controller
        if not ctl.armed or not ctl.candidates:
            return
        try:
            from prompt_toolkit.completion import Completion
        except ImportError:
            return
        try:
            replace = -len(document.text_before_cursor or "")
            for cand in ctl.candidates:
                n = cand.get("n")
                label = str(cand.get("label", ""))
                ins = cand.get("insertions", 0)
                dels = cand.get("deletions", 0)
                yield Completion(
                    text=f"/undo {n}",
                    start_position=replace,
                    display=f"{n}. {label}",
                    display_meta=f"⏪ +{ins} −{dels}",
                )
        except Exception:  # noqa: BLE001
            logger.debug("[Rewind] completion degraded", exc_info=True)
            return


def merge_rewind_completer(
    base_completer: Any, controller: Optional[RewindController],
) -> Any:
    """Compose the gated rewind source ahead of a surface's completer —
    same shape as merge_history_completer. NEVER raises."""
    if controller is None:
        return base_completer
    rc = RewindCompleter(controller)
    if base_completer is None:
        return rc
    try:
        from prompt_toolkit.completion import merge_completers
        return merge_completers([rc, base_completer])
    except Exception:  # noqa: BLE001
        return base_completer


def install_rewind_binding(kb: Any, controller: RewindController) -> bool:
    """Bind the remappable ``app:rewind`` action (default Esc Esc, fires
    only on an EMPTY prompt — a draft's double-Esc still means "clear").
    NEVER raises."""
    try:
        from prompt_toolkit.filters import Condition

        @Condition
        def _empty_prompt() -> bool:
            try:
                from prompt_toolkit.application.current import (
                    get_app_or_none,
                )
                app = get_app_or_none()
                return app is not None and not app.current_buffer.text
            except Exception:  # noqa: BLE001
                return False

        def _open(event: Any) -> None:
            controller.open()

        from backend.core.ouroboros.battle_test.keymap import bind_action
        return bind_action(
            kb, REWIND_ACTION, REWIND_DEFAULT_KEYS, _open,
            context="Chat", filter=_empty_prompt,
            description="open the rewind menu (pauses autonomy while open)",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[Rewind] install degraded", exc_info=True)
        return False


__all__ = [
    "MASTER_FLAG_ENV_VAR",
    "REWIND_ACTION",
    "REWIND_DEFAULT_KEYS",
    "REWIND_MENU_SCHEMA_VERSION",
    "RewindCompleter",
    "RewindController",
    "install_rewind_binding",
    "is_rewind_menu_enabled",
    "merge_rewind_completer",
]
