"""A cockpit that attaches before its daemon hydrates says so.

`ov` paints in milliseconds; a cold-booting daemon takes seconds. In that gap
the deck has nothing to show, and a blank deck is indistinguishable from three
very different situations: a healthy idle organism with nothing to say, a
wedged one, and a socket that never connected.

One line removes the whole question. The transition out of it is driven by the
UDS callbacks that already exist — a timer asking "has anything arrived yet"
would be a second source of truth for something the stream itself answers.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import time
from typing import Any

import pytest

from backend.core.ouroboros.battle_test.cockpit_fsm import (
    MODE_FLOW,
    MODE_IGNITION,
)


def _ui() -> Any:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import AttachUI
        return AttachUI()


# --------------------------------------------------------------------------
# 1. the skeleton
# --------------------------------------------------------------------------

def test_a_freshly_attached_cockpit_is_in_ignition() -> None:
    assert _ui().ignition_state == MODE_IGNITION


def test_the_skeleton_renders_instead_of_a_void() -> None:
    """MANDATE 4(1): silent stream, so the operator sees waiting — not
    nothing."""
    assert "awaiting daemon telemetry" in _ui().prompt()


def test_the_skeleton_is_one_line() -> None:
    """It occupies the deck's space without becoming a splash screen."""
    ui = _ui()
    lines = [ln for ln in ui.prompt().splitlines() if "awaiting" in ln]
    assert len(lines) == 1


# --------------------------------------------------------------------------
# 2. the handoff
# --------------------------------------------------------------------------

async def test_a_heartbeat_transitions_to_flow() -> None:
    """MANDATE 4(2). The payload arrives on the bridge callback; the FSM
    moves on that edge, not on a clock."""
    ui = _ui()
    assert ui.ignition_state == MODE_IGNITION
    ui.on_telemetry({"type": "heartbeat", "phase": "GENERATE"})
    await asyncio.sleep(0)
    assert ui.ignition_state == MODE_FLOW
    assert "awaiting daemon telemetry" not in ui.prompt()


@pytest.mark.parametrize("deliver", [
    lambda ui: ui.on_ambient("DW provider failover"),
    lambda ui: ui.on_telemetry({"phase": "APPLY"}),
    lambda ui: ui.on_lane_history({"lane": "unit/7", "lines": []}),
])
def test_any_payload_ends_ignition(deliver) -> None:
    """A heartbeat, a lane registration and an ambient line all prove the
    same thing — the far end is alive. Which arrives first is a race, so no
    single one may be privileged."""
    ui = _ui()
    deliver(ui)
    assert ui.ignition_state != MODE_IGNITION


def test_the_transition_is_idempotent() -> None:
    """It reports the EDGE, so later payloads cannot re-arm the skeleton."""
    ui = _ui()
    assert ui.note_upstream_activity() is True
    assert ui.note_upstream_activity() is False
    ui.on_ambient("more")
    assert ui.ignition_state == MODE_FLOW


def test_real_deck_content_survives_the_transition() -> None:
    """The payload that ends ignition must not be swallowed by the handoff."""
    ui = _ui()
    ui.on_ambient("DW provider failover")
    assert "failover" in ui.prompt()
    assert "awaiting" not in ui.prompt()


# --------------------------------------------------------------------------
# 3. the timeout
# --------------------------------------------------------------------------

def test_prolonged_silence_is_named_not_implied() -> None:
    """Continuing to show 'awaiting…' forever implies progress that is not
    happening. After the window it says what it now suspects."""
    ui = _ui()
    ui.prompt()                                   # anchors the clock
    ui._ignition_started = time.monotonic() - 30
    line = ui._ignition_line() or ""
    assert "unreachable" in line
    assert "detach" in line, "no way out is offered"


def test_the_window_is_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_IGNITION_TIMEOUT_S", "2")
    ui = _ui()
    ui.prompt()
    ui._ignition_started = time.monotonic() - 3
    assert "unreachable" in (ui._ignition_line() or "")


def test_a_degenerate_window_cannot_fire_instantly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero would flag every cold boot unreachable before a daemon could
    physically answer."""
    monkeypatch.setenv("JARVIS_IGNITION_TIMEOUT_S", "0")
    assert _ui()._ignition_deadline() >= 1.0


def test_late_telemetry_still_clears_the_warning() -> None:
    """A slow daemon is not a dead one — the warning is a suspicion, and it
    must be retractable."""
    ui = _ui()
    ui.prompt()
    ui._ignition_started = time.monotonic() - 30
    assert "unreachable" in (ui._ignition_line() or "")
    ui.on_ambient("late but alive")
    assert ui._ignition_line() is None
    assert "unreachable" not in ui.prompt()


def test_the_clock_starts_at_first_render_not_construction() -> None:
    """What matters is how long the OPERATOR has been looking at an empty
    deck, not how long the object has existed."""
    ui = _ui()
    assert ui._ignition_started == 0.0
    ui.prompt()
    assert ui._ignition_started > 0.0


# --------------------------------------------------------------------------
# 4. no polling was introduced
# --------------------------------------------------------------------------

def test_no_second_source_of_truth_was_added() -> None:
    """DRY, structural: the transition rides the existing UDS callbacks."""
    import inspect

    from backend.core.ouroboros.cli import ov

    for name in ("on_ambient", "on_telemetry", "on_lane_history"):
        body = inspect.getsource(getattr(ov.AttachUI, name))
        assert "note_upstream_activity" in body, f"{name} does not hand off"
    src = inspect.getsource(ov.AttachUI)
    assert "asyncio.create_task" not in src.split("note_upstream_activity")[1][:400]
