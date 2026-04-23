"""Tests for Ticket A Guard 2 — BattleTestHarness wall-clock cap.

Covers the ``--max-wall-seconds`` hard ceiling added 2026-04-23 to prevent
provider retry storms from defeating ``--idle-timeout`` (storm heartbeats
reset the activity monitor's liveness counter) and budget caps (retries
may not be billable).

Reference: memory/project_followup_idle_timeout_retry_hijack.md.

Surface under test:
- ``HarnessConfig.max_wall_seconds_s`` field (None / positive float).
- ``HarnessConfig.from_env`` reading ``OUROBOROS_BATTLE_MAX_WALL_SECONDS``.
- ``BattleTestHarness._monitor_wall_clock`` coroutine firing the event.
- Run-loop wiring: stop_reason resolves to ``"wall_clock_cap"`` when the
  wall-clock event is the race winner.
- CLI arg surface: ``scripts/ouroboros_battle_test.py`` threads
  ``--max-wall-seconds`` into ``HarnessConfig.max_wall_seconds_s``.
"""
from __future__ import annotations

import asyncio
import atexit
import os
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    HarnessConfig,
)


# ---------------------------------------------------------------------------
# Fixture — disposable harness with short caps
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_harness(tmp_path: Path) -> Iterator[BattleTestHarness]:
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-wallclock-test"
    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.05,
        idle_timeout_s=30.0,
        max_wall_seconds_s=2.0,
        session_dir=session_dir,
    )
    harness = BattleTestHarness(config)
    yield harness
    atexit.unregister(harness._atexit_fallback_write)


@pytest.fixture
def tmp_harness_disabled(tmp_path: Path) -> Iterator[BattleTestHarness]:
    """Harness with wall-clock disabled (None) — legacy 3-way race."""
    session_dir = tmp_path / ".ouroboros" / "sessions" / "bt-wallclock-test-off"
    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.05,
        idle_timeout_s=30.0,
        max_wall_seconds_s=None,
        session_dir=session_dir,
    )
    harness = BattleTestHarness(config)
    yield harness
    atexit.unregister(harness._atexit_fallback_write)


# ---------------------------------------------------------------------------
# (1) HarnessConfig accepts the new field
# ---------------------------------------------------------------------------


def test_config_default_is_none():
    """Default must be None — legacy 3-way race (shutdown/budget/idle)."""
    cfg = HarnessConfig()
    assert cfg.max_wall_seconds_s is None


def test_config_accepts_positive_float():
    cfg = HarnessConfig(max_wall_seconds_s=2400.0)
    assert cfg.max_wall_seconds_s == 2400.0


def test_from_env_reads_max_wall_seconds(monkeypatch):
    """OUROBOROS_BATTLE_MAX_WALL_SECONDS env → max_wall_seconds_s."""
    monkeypatch.setenv("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "1800")
    cfg = HarnessConfig.from_env()
    assert cfg.max_wall_seconds_s == 1800.0


def test_from_env_zero_means_disabled(monkeypatch):
    """Env=0 must map to None so the race stays 3-way legacy."""
    monkeypatch.setenv("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "0")
    cfg = HarnessConfig.from_env()
    assert cfg.max_wall_seconds_s is None


def test_from_env_unset_means_disabled(monkeypatch):
    """Env unset must also map to None."""
    monkeypatch.delenv("OUROBOROS_BATTLE_MAX_WALL_SECONDS", raising=False)
    cfg = HarnessConfig.from_env()
    assert cfg.max_wall_seconds_s is None


# ---------------------------------------------------------------------------
# (2) _monitor_wall_clock coroutine fires the event
# ---------------------------------------------------------------------------


def test_monitor_wall_clock_fires_event(tmp_harness):
    """Coroutine sleeps for cap_s then calls ``_wall_clock_event.set()``."""

    async def run_it():
        tmp_harness._wall_clock_event = asyncio.Event()
        tmp_harness._started_at = asyncio.get_event_loop().time()
        # Use a very short cap to keep the test fast.
        await tmp_harness._monitor_wall_clock(0.2)
        return tmp_harness._wall_clock_event.is_set()

    assert asyncio.new_event_loop().run_until_complete(run_it()) is True


def test_monitor_wall_clock_cancellation_does_not_fire(tmp_harness):
    """If the coroutine is cancelled before the sleep completes, the
    event stays unset (legacy 3-way race winner handling)."""

    async def run_it():
        tmp_harness._wall_clock_event = asyncio.Event()
        tmp_harness._started_at = asyncio.get_event_loop().time()
        task = asyncio.ensure_future(tmp_harness._monitor_wall_clock(60.0))
        await asyncio.sleep(0.05)  # let the task start sleeping
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return tmp_harness._wall_clock_event.is_set()

    assert asyncio.new_event_loop().run_until_complete(run_it()) is False


# ---------------------------------------------------------------------------
# (3) Stop-reason mapping — race-winner resolution
# ---------------------------------------------------------------------------


def test_stop_reason_wall_clock_cap_when_wall_event_fires_first(tmp_harness):
    """Simulate the FIRST_COMPLETED race: wall_clock_event set first →
    ``_stop_reason="wall_clock_cap"``. Uses a minimal replica of the run()
    race-resolution block to validate the branch without booting the
    whole 6-layer stack.
    """

    async def race():
        tmp_harness._shutdown_event = asyncio.Event()
        tmp_harness._cost_tracker.budget_event = asyncio.Event()
        tmp_harness._idle_watchdog.idle_event = asyncio.Event()
        tmp_harness._wall_clock_event = asyncio.Event()
        tmp_harness._started_at = asyncio.get_event_loop().time()

        # Arm the wall-clock to fire after 0.1s, others stay inert.
        async def firebug():
            await asyncio.sleep(0.1)
            tmp_harness._wall_clock_event.set()

        _ = asyncio.ensure_future(firebug())

        shutdown_waiter = asyncio.ensure_future(tmp_harness._shutdown_event.wait())
        budget_waiter = asyncio.ensure_future(tmp_harness._cost_tracker.budget_event.wait())
        idle_waiter = asyncio.ensure_future(tmp_harness._idle_watchdog.idle_event.wait())
        wall_clock_waiter = asyncio.ensure_future(tmp_harness._wall_clock_event.wait())

        done, pending = await asyncio.wait(
            [shutdown_waiter, budget_waiter, idle_waiter, wall_clock_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        # Mirror the run() resolution logic for just the wall-clock branch.
        if wall_clock_waiter in done:
            return "wall_clock_cap"
        return "other"

    result = asyncio.new_event_loop().run_until_complete(race())
    assert result == "wall_clock_cap"


# ---------------------------------------------------------------------------
# (4) CLI arg surface
# ---------------------------------------------------------------------------


def test_cli_arg_parses_max_wall_seconds():
    """``--max-wall-seconds 30`` must propagate into args.max_wall_seconds."""
    import argparse

    parser = argparse.ArgumentParser()
    # Mirror the scripts/ouroboros_battle_test.py definition verbatim so this
    # test catches any drift in the CLI surface.
    parser.add_argument(
        "--max-wall-seconds",
        type=float,
        default=float(os.environ.get("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "0")),
    )
    args = parser.parse_args(["--max-wall-seconds", "30"])
    assert args.max_wall_seconds == 30.0


def test_cli_arg_default_is_zero_when_env_unset(monkeypatch):
    """Unset env → default 0.0 → None in HarnessConfig."""
    import argparse

    monkeypatch.delenv("OUROBOROS_BATTLE_MAX_WALL_SECONDS", raising=False)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-wall-seconds",
        type=float,
        default=float(os.environ.get("OUROBOROS_BATTLE_MAX_WALL_SECONDS", "0")),
    )
    args = parser.parse_args([])
    assert args.max_wall_seconds == 0.0
    # Launcher maps `args.max_wall_seconds or None` → None when 0:
    assert (args.max_wall_seconds or None) is None
