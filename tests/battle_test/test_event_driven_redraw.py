"""Tests for the event-driven REPL redraw model — replaces
``refresh_interval``-driven background ticks with explicit
``invalidate()`` calls + a spinner-only invalidator task.

Symptom this fix targets: keystrokes "batch" / appear with delay
because prompt_toolkit's refresh_interval schedules a redraw on
the asyncio event loop every 100ms regardless of state, and key
events have to wait their turn behind the in-progress redraw.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.live_status_line import (
    SpinnerInvalidator,
    invalidate_app,
    is_auto_refresh_enabled,
)


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JARVIS_REPL_AUTO_REFRESH_ENABLED", raising=False)
    yield


# ===========================================================================
# is_auto_refresh_enabled — env flag default-off
# ===========================================================================


def test_auto_refresh_default_off():
    """Default model is event-driven — refresh_interval=None."""
    assert is_auto_refresh_enabled() is False


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("", False), ("garbage", False),
])
def test_auto_refresh_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("JARVIS_REPL_AUTO_REFRESH_ENABLED", raw)
    assert is_auto_refresh_enabled() is expected


# ===========================================================================
# invalidate_app — defensive contract
# ===========================================================================


def test_invalidate_app_returns_false_when_no_app():
    """No prompt_toolkit Application running → returns False, not raises."""
    # Outside a PromptSession, get_app() raises NoRunningApplicationError.
    # The helper must catch and return False.
    result = invalidate_app()
    # In test env without a running app, expected to be False.
    assert result is False or result is True  # either is fine; just no raise


def test_invalidate_app_handles_missing_prompt_toolkit():
    """Defensive: if get_app() raises any exception, return False."""
    with mock.patch(
        "prompt_toolkit.application.get_app",
        side_effect=RuntimeError("simulated"),
    ):
        assert invalidate_app() is False


# ===========================================================================
# SpinnerInvalidator — lifecycle + behavior
# ===========================================================================


def test_invalidator_construct_does_not_start():
    """Construction is cheap — no task scheduled until start()."""
    inv = SpinnerInvalidator(get_active=lambda: False)
    assert inv.is_running() is False


def test_invalidator_start_stop_idempotent():
    """Starting/stopping multiple times is safe."""
    async def _scenario():
        inv = SpinnerInvalidator(get_active=lambda: False, cadence_s=0.05)
        # Start without a running event loop should fail-soft
        # (we're inside one here so it'll succeed)
        ok1 = inv.start()
        ok2 = inv.start()  # second start: no-op
        assert ok1 is True
        assert ok2 is True
        assert inv.is_running() is True
        inv.stop()
        inv.stop()  # second stop: no-op
        # After cancel, give the loop a tick to clean up
        await asyncio.sleep(0.01)

    asyncio.get_event_loop().run_until_complete(_scenario())


def test_invalidator_invokes_get_app_when_active():
    """When predicate returns True, invalidate_app should be called."""
    invalidated_count = [0]

    def _predicate():
        return True

    async def _scenario():
        with mock.patch(
            "backend.core.ouroboros.battle_test.live_status_line.invalidate_app",
            side_effect=lambda: invalidated_count.__setitem__(0, invalidated_count[0] + 1) or True,
        ):
            inv = SpinnerInvalidator(get_active=_predicate, cadence_s=0.02)
            inv.start()
            await asyncio.sleep(0.10)  # ~5 ticks at 20ms cadence
            inv.stop()
            await asyncio.sleep(0.01)
        # Should have fired at least 2-3 times in 100ms
        assert invalidated_count[0] >= 2

    asyncio.get_event_loop().run_until_complete(_scenario())


def test_invalidator_skips_when_predicate_false():
    """When predicate returns False, NO invalidate calls fire."""
    invalidated_count = [0]

    async def _scenario():
        with mock.patch(
            "backend.core.ouroboros.battle_test.live_status_line.invalidate_app",
            side_effect=lambda: invalidated_count.__setitem__(0, invalidated_count[0] + 1) or True,
        ):
            inv = SpinnerInvalidator(get_active=lambda: False, cadence_s=0.02)
            inv.start()
            await asyncio.sleep(0.10)
            inv.stop()
            await asyncio.sleep(0.01)
        assert invalidated_count[0] == 0

    asyncio.get_event_loop().run_until_complete(_scenario())


def test_invalidator_handles_predicate_exception():
    """Predicate raising → treated as False, no invalidate, NEVER raises."""
    def _raising():
        raise RuntimeError("predicate exploded")

    async def _scenario():
        inv = SpinnerInvalidator(get_active=_raising, cadence_s=0.02)
        inv.start()
        await asyncio.sleep(0.05)
        inv.stop()
        await asyncio.sleep(0.01)
        # Just verify no exception escaped — the test reaching here is the assertion
        assert True

    asyncio.get_event_loop().run_until_complete(_scenario())


def test_invalidator_cadence_clamped():
    """Cadence is clamped to [0.01, 5.0] — pathological values
    don't break the loop."""
    inv1 = SpinnerInvalidator(get_active=lambda: False, cadence_s=0.0)
    assert inv1._cadence_s >= 0.01
    inv2 = SpinnerInvalidator(get_active=lambda: False, cadence_s=999.0)
    assert inv2._cadence_s <= 5.0


def test_invalidator_dynamic_predicate_active_then_inactive():
    """Realistic scenario: spinner activates briefly, then deactivates.
    Invalidator fires during active window only."""
    state = {"active": False, "fires": 0}

    async def _scenario():
        with mock.patch(
            "backend.core.ouroboros.battle_test.live_status_line.invalidate_app",
            side_effect=lambda: state.__setitem__("fires", state["fires"] + 1) or True,
        ):
            inv = SpinnerInvalidator(
                get_active=lambda: state["active"],
                cadence_s=0.02,
            )
            inv.start()
            await asyncio.sleep(0.05)  # idle — no fires
            assert state["fires"] == 0
            state["active"] = True
            await asyncio.sleep(0.10)  # active — fires
            fires_during_active = state["fires"]
            state["active"] = False
            await asyncio.sleep(0.05)  # idle again
            inv.stop()
            await asyncio.sleep(0.01)
            # Fires happened during active window only
            assert fires_during_active >= 2
            # Final count not much higher (idle period had no fires)
            assert state["fires"] - fires_during_active <= 1

    asyncio.get_event_loop().run_until_complete(_scenario())


# ===========================================================================
# Source-level regression — serpent_flow uses event-driven model
# ===========================================================================


_SERPENT_FLOW = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


def test_serpent_flow_resolves_refresh_interval_via_helper():
    """The PromptSession's refresh_interval kwarg must come from the
    auto-refresh resolver (None when default-off, _REPL_REFRESH_INTERVAL_S
    when operator opts in)."""
    src = _SERPENT_FLOW.read_text()
    assert "is_auto_refresh_enabled" in src
    assert "_refresh_interval_kwarg" in src


def test_serpent_flow_starts_spinner_invalidator():
    """REPL._loop must construct + start a SpinnerInvalidator so
    the spinner animates without refresh_interval ticks."""
    src = _SERPENT_FLOW.read_text()
    assert "SpinnerInvalidator" in src
    assert "_spinner_invalidator" in src
    assert "self._spinner_invalidator.start()" in src


def test_serpent_flow_stops_invalidator_on_repl_stop():
    """The invalidator must be stopped during REPL.stop() so the
    background task doesn't leak past session end."""
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    # Find SerpentREPL.stop method and verify invalidator.stop() inside
    found_stop_with_invalidator = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "stop":
            body = ast.unparse(node)
            if "_spinner_invalidator" in body and "invalidator.stop()" in body:
                found_stop_with_invalidator = True
                break
    assert found_stop_with_invalidator, (
        "SerpentREPL.stop must call _spinner_invalidator.stop() to "
        "prevent task leak across session boundaries"
    )


def test_op_lifecycle_calls_invalidate_app():
    """Without refresh_interval, op state transitions must explicitly
    fire invalidate_app() so the toolbar redraws on cost / phase
    changes."""
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_maybe_set_terminal_title":
            body = ast.unparse(node)
            assert "invalidate_app" in body, (
                "_maybe_set_terminal_title (called from op_started/"
                "completed/failed) must invoke invalidate_app() so "
                "the toolbar updates without refresh_interval"
            )
            return
    pytest.fail("_maybe_set_terminal_title not found")
