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


class LocalRewindClient:
    """The daemon terminal's half of the Transactional Viewport Lock.

    `RewindController` needs exactly three things from a "client":
    ``send_autonomy("pause")``, ``send_rewind_request()`` and
    ``send_autonomy("resume")``. Nothing about the lock is bridge-specific
    — the bridge existed because the cockpit is a SEPARATE PROCESS and had
    to ask the daemon to pause. At the daemon's own terminal there is no
    second process, so the same three calls are local.

    So the lock is not reimplemented here. This is the second entrance to
    one implementation — the pattern `_on_autonomy` already states in its
    own comment ("the SAME intake pause the pause/resume verbs flip: one
    implementation, two entrances").

    The snapshot arrives SYNCHRONOUSLY, which the bridge path cannot do.
    `deliver()` is therefore called from inside `send_rewind_request()`,
    before it returns — the controller tolerates this because it only ever
    required delivery to happen once, not later.

    NEVER raises: a rewind menu that can break the REPL it opens in is
    worse than no menu.
    """

    __slots__ = ("_pause", "_resume", "_provider", "_deliver", "_limit",
                 "_held")

    def __init__(
        self,
        *,
        pause: Callable[[], Any],
        resume: Callable[[], Any],
        provider: Callable[[int], Any],
        deliver: Optional[Callable[[Any], Any]] = None,
        limit: int = 10,
    ) -> None:
        self._pause = pause
        self._resume = resume
        self._provider = provider
        self._deliver = deliver
        self._limit = max(1, int(limit))
        #: Whether WE acquired the hold. A resume we did not pause for
        #: would release someone else's lock — the refcount upstream is
        #: what makes concurrent holders safe, and lying to it defeats it.
        self._held = False

    def bind_deliver(self, deliver: Callable[[Any], Any]) -> None:
        self._deliver = deliver

    def send_autonomy(self, action: str) -> bool:
        try:
            if str(action) == "pause":
                self._pause()
                self._held = True
                return True
            if str(action) == "resume":
                if not self._held:
                    return True      # nothing of ours to release
                self._resume()
                self._held = False
                return True
            return False
        except Exception:  # noqa: BLE001
            logger.debug("[Rewind] local autonomy %s failed", action,
                         exc_info=True)
            return False

    def send_rewind_request(self) -> bool:
        """Fetch and deliver the snapshot in one step. NEVER raises.

        Returns False when the planner yields nothing — the controller
        treats that as an empty list and releases the hold, which is the
        correct outcome: an empty menu must not leave intake paused.
        """
        try:
            rows = list(self._provider(self._limit) or ())
        except Exception:  # noqa: BLE001
            logger.debug("[Rewind] local provider failed", exc_info=True)
            return False
        if self._deliver is None:
            return False
        try:
            self._deliver({"candidates": rows})
            return True
        except Exception:  # noqa: BLE001
            logger.debug("[Rewind] local deliver failed", exc_info=True)
            return False


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


def render_restore_points(
    candidates: Any, *, width: Optional[int] = None,
) -> List[str]:
    """The restore points as plain rows. Pure. NEVER raises.

    A plain list rather than a Float, because the DAEMON's own REPL is a
    `PromptSession` and has no palette overlay — the Float belongs to the
    cockpit's `FloatContainer`. Rendering rows means the same snapshot is
    usable on a surface that cannot host a menu, instead of the daemon
    terminal having no rewind at all.

    Numbered to match `/undo N` exactly, so the reading and the command
    cannot disagree — and NOT auto-executed: a rollback should never be
    one accidental keystroke, which is the same reason the cockpit menu
    only INSERTS the command.
    """
    try:
        rows = [c for c in (candidates or ()) if isinstance(c, dict)]
        if not rows:
            return ["  (no restore points — nothing committed to undo)"]
        cols = int(width) if width and int(width) > 0 else 80
        # The HEADER is clipped too. Clipping only the data rows left the
        # one line that names the command running past a narrow terminal —
        # and that line is the affordance.
        out = [
            "  ⏪ restore points — `/undo N` to revert (Enter confirms)"[:cols]
        ]
        for i, c in enumerate(rows, start=1):
            sha = str(c.get("sha") or c.get("commit") or "")[:9]
            subject = " ".join(str(
                c.get("subject") or c.get("summary") or "").split())
            when = str(c.get("age") or c.get("when") or "")
            line = f"    {i:>2}. {sha}  {subject}"
            if when:
                line = f"{line}  ({when})"
            out.append(line[:cols])
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[Rewind] render degraded", exc_info=True)
        return ["  (restore points unavailable)"]


def local_rewind_rows(
    *,
    pause: Callable[[], Any],
    resume: Callable[[], Any],
    provider: Callable[[int], Any],
    limit: int = 10,
    width: Optional[int] = None,
) -> List[str]:
    """Take the lock, snapshot, render, RELEASE. NEVER raises.

    The whole daemon-side sequence in one call, through the same
    controller the cockpit uses. The release is in a `finally`: a snapshot
    that leaves intake paused because rendering raised would be strictly
    worse than no rewind — the organism would sit idle and nothing would
    say why.
    """
    client = LocalRewindClient(
        pause=pause, resume=resume, provider=provider, limit=limit,
    )
    captured: List[Any] = []
    client.bind_deliver(lambda payload: captured.append(payload))
    controller = RewindController(client)
    try:
        controller.open()
        rows = (captured[-1].get("candidates") if captured else ()) or ()
        return render_restore_points(rows, width=width)
    except Exception:  # noqa: BLE001
        logger.debug("[Rewind] local sequence degraded", exc_info=True)
        return ["  (rewind unavailable)"]
    finally:
        try:
            controller.close()
        except Exception:  # noqa: BLE001
            # Belt and braces — the controller releases exactly once, but a
            # paused organism with no explanation is the one outcome worth
            # a second guard.
            client.send_autonomy("resume")


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
                if app is None or app.current_buffer.text:
                    return False
                # ...and nothing dismissable is on screen.
                #
                # `overlay_arbiter` binds a single `Escape` whose eagerness is a
                # per-keystroke FILTER, active only while an overlay is up. This
                # is the COMPLEMENT of that filter, and the pair is what removes
                # the collision rather than arbitrating it: with a panic showing,
                # the first Escape closes it eagerly and this sequence must not
                # also be live, or two presses would dismiss AND rewind. With the
                # cockpit clear the eager binding is inactive, so the input
                # processor buffers `esc esc` naturally — no timer, no custom
                # processor, no polling.
                #
                # Asked here rather than in the arbiter because this filter
                # already owns "when does a double-Esc mean rewind", and that
                # question has exactly one right place to be answered.
                from backend.core.ouroboros.battle_test.overlay_arbiter import (
                    overlay_active,
                )
                return not overlay_active()
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
    "LocalRewindClient",
    "local_rewind_rows",
    "render_restore_points",
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
