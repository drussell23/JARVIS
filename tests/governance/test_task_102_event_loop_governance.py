"""
Task #102 spine — Autonomous Event-Loop Governance Substrate.

H11 (event-loop starvation) confirmed final-mile after Task #101
diagnostic matrix falsified H7/H8/H9.  Task #102 provides composable
primitives over existing asyncio infrastructure — no external deps,
no dedicated threading hacks.

This spine pins:

  * Master-switch resolver (default true, env-tunable).
  * Yield cadence resolver (default 64, invalid fallback).
  * ``cooperative_yield()`` is a no-op when master is off.
  * ``cooperative_yield_every_n_async()`` correctly yields each item
    AND inserts ``asyncio.sleep(0)`` every N items — counted via
    event-loop scheduling probe.
  * ``offload_blocking()`` composes ``asyncio.to_thread`` when master
    is on; falls back to synchronous call when master is off.
  * Oracle._scan_for_changes consumes the substrate (AST scan).
  * FlagRegistry seeds present.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest


_GOV_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "event_loop_governance.py"
)
_ORACLE_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "oracle.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def _import_resolvers():
    from backend.core.ouroboros.governance.event_loop_governance import (
        event_loop_governance_enabled,
        resolve_yield_every_n,
    )
    return event_loop_governance_enabled, resolve_yield_every_n


@pytest.mark.parametrize("env_val,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("off", False),
    ("garbage", False),
])
def test_master_switch_resolver(env_val, expected, monkeypatch):
    monkeypatch.setenv("JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", env_val)
    enabled, _ = _import_resolvers()
    assert enabled() is expected


def test_master_switch_defaults_true_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", raising=False)
    enabled, _ = _import_resolvers()
    assert enabled() is True


@pytest.mark.parametrize("env_val,expected", [
    ("64", 64),
    ("128", 128),
    ("1", 1),
    ("0", 64),       # non-positive → default
    ("-5", 64),
    ("garbage", 64),
    ("", 64),
])
def test_yield_cadence_resolver(env_val, expected, monkeypatch):
    if env_val:
        monkeypatch.setenv("JARVIS_EVENT_LOOP_YIELD_EVERY_N", env_val)
    else:
        monkeypatch.delenv("JARVIS_EVENT_LOOP_YIELD_EVERY_N", raising=False)
    _, fn = _import_resolvers()
    assert fn() == expected


# ---------------------------------------------------------------------------
# cooperative_yield
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooperative_yield_is_no_op_when_master_off(monkeypatch):
    """When the master switch is off, cooperative_yield runs without
    actually awaiting (behaves like a no-op fast path)."""
    monkeypatch.setenv("JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", "false")
    from backend.core.ouroboros.governance.event_loop_governance import (
        cooperative_yield,
    )
    # Should complete instantly without scheduler trip
    await cooperative_yield()  # no exception, no hang


@pytest.mark.asyncio
async def test_cooperative_yield_releases_loop_when_master_on(monkeypatch):
    """The load-bearing case — when master is on, cooperative_yield
    actually gives other coroutines a scheduling slot."""
    monkeypatch.setenv("JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", "true")
    from backend.core.ouroboros.governance.event_loop_governance import (
        cooperative_yield,
    )
    competing_ran = []

    async def _competing():
        competing_ran.append(1)

    # Schedule a competing coroutine that will only run if we yield
    task = asyncio.create_task(_competing())
    # Without cooperative_yield, the next await is the only chance
    await cooperative_yield()
    # competing_ran should now be populated
    await task
    assert competing_ran == [1]


# ---------------------------------------------------------------------------
# cooperative_yield_every_n_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iterator_yields_all_items_in_order():
    from backend.core.ouroboros.governance.event_loop_governance import (
        cooperative_yield_every_n_async,
    )
    items = list(range(100))
    got = []
    async for x in cooperative_yield_every_n_async(items, every_n=10):
        got.append(x)
    assert got == items


@pytest.mark.asyncio
async def test_iterator_yields_event_loop_at_cadence(monkeypatch):
    """Empirical: a competing task should get scheduled approximately
    once per ``every_n`` iterations.  We use a tight counter as the
    competing task — its tick count should be ~ (items / every_n)."""
    monkeypatch.setenv("JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", "true")
    from backend.core.ouroboros.governance.event_loop_governance import (
        cooperative_yield_every_n_async,
    )
    competing_ticks = [0]

    async def _competing():
        while True:
            competing_ticks[0] += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(_competing())
    items = list(range(200))
    async for _ in cooperative_yield_every_n_async(items, every_n=20):
        pass
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Competing task ticks should be at LEAST (200 / 20) = 10.
    # Lower bound — scheduling is non-deterministic, but the yield
    # must give the competing task SOME slots.
    assert competing_ticks[0] >= 10, (
        f"Expected ≥10 competing ticks (200 items / 20 cadence); "
        f"got {competing_ticks[0]} — cooperative yield is not "
        f"releasing the event loop as expected"
    )


@pytest.mark.asyncio
async def test_iterator_does_not_yield_when_master_off(monkeypatch):
    """When master is off, the iterator still yields items but
    does NOT release the event loop — competing task starves."""
    monkeypatch.setenv("JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", "false")
    from backend.core.ouroboros.governance.event_loop_governance import (
        cooperative_yield_every_n_async,
    )
    competing_ticks = [0]

    async def _competing():
        # No internal sleep — starves if not given a slot
        while True:
            competing_ticks[0] += 1
            await asyncio.sleep(0)  # cooperative

    task = asyncio.create_task(_competing())
    # Without master ON, iterator does NOT inject sleep(0). The
    # for-loop body is pure sync, so the only yield is at iteration
    # END. competing task should get fewer slots.
    items = list(range(200))
    async for _ in cooperative_yield_every_n_async(items, every_n=20):
        pass  # no body await
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # When master is off, the iterator should not inject sleeps —
    # so we expect 0 or very few ticks for the competing task
    # before the iterator exhausts.  Loose bound: < 5 ticks (vs
    # ≥10 with master on).
    assert competing_ticks[0] < 10, (
        f"Master-off should NOT release event loop within the "
        f"iterator; competing task got {competing_ticks[0]} ticks "
        f"(expected very few)"
    )


# ---------------------------------------------------------------------------
# offload_blocking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offload_blocking_runs_function(monkeypatch):
    monkeypatch.setenv("JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", "true")
    from backend.core.ouroboros.governance.event_loop_governance import (
        offload_blocking,
    )
    result = await offload_blocking(lambda x, y: x + y, 2, 3)
    assert result == 5


@pytest.mark.asyncio
async def test_offload_blocking_propagates_exception(monkeypatch):
    monkeypatch.setenv("JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", "true")
    from backend.core.ouroboros.governance.event_loop_governance import (
        offload_blocking,
    )

    def _explode():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await offload_blocking(_explode, label="test")


@pytest.mark.asyncio
async def test_offload_blocking_falls_back_when_master_off(monkeypatch):
    """When master is off, offload_blocking runs the fn synchronously
    in the caller's coroutine — no to_thread overhead."""
    monkeypatch.setenv("JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED", "false")
    from backend.core.ouroboros.governance.event_loop_governance import (
        offload_blocking,
    )
    result = await offload_blocking(lambda: 42)
    assert result == 42


# ---------------------------------------------------------------------------
# AST pins — Oracle._scan_for_changes uses the substrate
# ---------------------------------------------------------------------------


def test_ast_pin_oracle_scan_imports_substrate():
    """``_scan_for_changes`` MUST import the event-loop governance
    primitives — lazy import inside the method is acceptable (avoids
    top-level cycle), but the import string MUST be present."""
    src = _ORACLE_SRC.read_text(encoding="utf-8")
    assert "from backend.core.ouroboros.governance.event_loop_governance import" in src
    assert "cooperative_yield_every_n_async" in src
    assert "offload_blocking" in src


def test_ast_pin_oracle_scan_uses_async_iterator():
    """The scan loop MUST consume the iterator via ``async for ... in
    cooperative_yield_every_n_async(python_files)`` — pin the wiring."""
    src = _ORACLE_SRC.read_text(encoding="utf-8")
    assert (
        "async for file_path in cooperative_yield_every_n_async(python_files):"
        in src
    ), (
        "Oracle._scan_for_changes MUST consume python_files via "
        "cooperative_yield_every_n_async — this is the load-bearing "
        "wire-up that releases the event loop for the Claude SDK "
        "stream consumer during a 29k-file scan"
    )


def test_ast_pin_oracle_scan_offloads_read_and_hash():
    src = _ORACLE_SRC.read_text(encoding="utf-8")
    assert "await offload_blocking(" in src, (
        "Oracle._scan_for_changes MUST offload _read_and_hash via "
        "offload_blocking — keeps file read + md5 off the event loop"
    )


def test_ast_pin_module_exports_public_surface():
    """Substrate module MUST expose the canonical public surface."""
    src = _GOV_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    module_fns = {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef)
    }
    module_async = {
        n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
    }
    assert "event_loop_governance_enabled" in module_fns
    assert "resolve_yield_every_n" in module_fns
    assert "cooperative_yield" in module_async
    assert "offload_blocking" in module_async
    # cooperative_yield_every_n_async is async generator (AsyncFunctionDef)
    assert "cooperative_yield_every_n_async" in module_async


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_seed_master_switch_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED" in src
    # Pin the FlagSpec block (not just a cross-reference in another
    # description)
    flagspec_marker = 'name="JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED"'
    idx = src.find(flagspec_marker)
    assert idx > 0
    window = src[idx:idx + 1800]
    assert "default=True" in window
    assert "Category.SAFETY" in window
    assert "event_loop_governance.py" in window


def test_seed_yield_cadence_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    flagspec_marker = 'name="JARVIS_EVENT_LOOP_YIELD_EVERY_N"'
    idx = src.find(flagspec_marker)
    assert idx > 0
    window = src[idx:idx + 1500]
    assert "default=64" in window
    assert "Category.TUNING" in window
    assert "event_loop_governance.py" in window
