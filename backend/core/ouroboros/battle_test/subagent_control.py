"""Keyboard control over running subagents — Claude Code's `Ctrl+X Ctrl+K`.

The gap this closes
-------------------
ov dispatches L3 subagents into isolated worktrees and shows them in the
roster, and an operator watching that go wrong had no key to stop it. `Esc`
cancels the operator's OWN most recent op and is narrow on purpose — one
reflex that reached autonomous work could kill a soak — so there was nothing
between "cancel the thing I asked for" and "kill the process".

Claude Code binds exactly this: "Stop all running background subagents in this
session. Press twice within 3 seconds to confirm." The chord and its repeat
ARE the safety argument. A single key with this reach would be a mis-hit
waiting to happen; four deliberate presses cannot be.

One installer, two transports
-----------------------------
Both cockpits send the same LINE. On the attach client `send_input` crosses
the socket to the daemon; on the daemon `LocalCockpitClient` routes it
straight into `_dispatch_verb`. So the authority — `request_cancel_all` on the
governed loop — has exactly one caller reachable from exactly one verb, and
the two surfaces cannot drift into different ideas of what the chord does.

That asymmetry is not incidental. The attach client holds no governed loop and
can cancel nothing itself; a client-side implementation would have had to grow
its own opinion about what "all" means, on a process that cannot see the ops.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("Ouroboros.SubagentControl")

__all__ = ["STOP_ALL_ACTION", "STOP_ALL_KEYS", "STOP_ALL_VERB",
           "install_stop_all_binding"]

#: The remappable action id, so `/keys` lists it and keybindings.json can
#: move it. Named for what it does to the ORGANISM, not for the keys.
STOP_ALL_ACTION: str = "agents:stopAll"

#: Claude Code's own chord. A chord rather than a single key because the
#: reach is broad; `keymap.parse_keystroke` turns the space-separated form
#: into prompt_toolkit's two-step sequence.
STOP_ALL_KEYS = ("ctrl+x ctrl+k",)

#: The verb both surfaces send. The chord is a shortcut FOR this, never a
#: second path to the same authority.
STOP_ALL_VERB: str = "/stop-all"


def install_stop_all_binding(
    kb: Any,
    client: Any,
    notify: Optional[Callable[[str], None]] = None,
    running: Optional[Callable[[], int]] = None,
) -> bool:
    """Bind the stop-all chord into *kb*. NEVER raises; True when bound.

    ``client`` needs only ``send_input``. ``notify`` shows the arming
    message — a flash on the client, a console line on the daemon — and
    ``running`` reports how many agents are live so the prompt can say what
    is about to be stopped rather than asking the operator to confirm a
    number they cannot see.
    """
    try:
        from backend.core.ouroboros.battle_test.confirm_chord import (
            ConfirmLatch, confirm_window_s,
        )
        from backend.core.ouroboros.battle_test.keymap import bind_action

        if kb is None or client is None or not hasattr(client, "send_input"):
            return False

        latch = ConfirmLatch()

        def _say(message: str) -> None:
            if notify is None:
                return
            try:
                notify(message)
            except Exception:  # noqa: BLE001
                pass

        def _count() -> Optional[int]:
            if running is None:
                return None
            try:
                return int(running())
            except Exception:  # noqa: BLE001
                return None

        def _stop_all(event: Any = None) -> None:
            if not latch.press():
                # The ARMING press. It reports what the confirmation would
                # actually do, because "press again to confirm" on an idle
                # organism asks the operator to weigh a consequence that does
                # not exist — and they learn to confirm without reading.
                n = _count()
                if n == 0:
                    _say("nothing running")
                    latch.disarm()
                    return
                subject = (
                    f"{n} agent{'s' if n != 1 else ''}"
                    if n is not None else "all running work"
                )
                _say(
                    f"press again within {confirm_window_s():.0f}s to stop "
                    f"{subject}"
                )
                return
            # CONFIRMED. The verb carries it from here — on either surface.
            try:
                client.send_input(STOP_ALL_VERB)
                _say("stopping — each op halts at its next phase boundary")
            except Exception:  # noqa: BLE001
                logger.debug("[SubagentControl] stop-all send failed",
                             exc_info=True)
                _say("stop-all could not reach the organism")

        return bind_action(
            kb, STOP_ALL_ACTION, STOP_ALL_KEYS, _stop_all,
            context="Global",
            description="stop every running agent (press twice to confirm)",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[SubagentControl] stop-all binding unavailable",
                     exc_info=True)
        return False
