"""
Task #104 spine — Autonomous Quiescence Protocol (Core Isolation).

The B1 falsification campaign (Task #103) proved Oracle's boot index
was the dominant event-loop suffocator (disabling it flipped Claude
stream first_raw_event 0→24) AND that ~97 residual not-done tasks
still delayed first event 94-333s.  Task #104 deploys deterministic
containment: a global asyncio.Event gate that the core CLEARS for
the lifetime of every Claude stream; background loops park at 0% CPU
until released.

This spine pins:

  * Master-switch + max-pause resolvers (env-tunable, invalid
    fallback).
  * Default gate state = set (background allowed).
  * quiescence_core_active CLEARS on first concurrent entrant,
    SETS on last exit (refcounted).
  * await_quiescence_clearance: immediate when set, parks when
    cleared, returns True/False (normal vs safety-valve).
  * Anti-starvation: max-pause timeout → proceed degraded.
  * Master-off → both surfaces no-op (byte-identical).
  * Concurrent core entrants (BG pool) compose via refcount.
  * cooperative_yield_every_n_async (Task #102) composes the gate.
  * AST pins: providers.py wraps stream in quiescence_core_active;
    oracle.py _index_repository awaits the gate.
  * FlagRegistry seeds present.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest


_QUIESCENCE_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "quiescence.py"
)
_PROVIDERS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)
_ORACLE_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "oracle.py"
)
_ELG_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "event_loop_governance.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "flag_registry_seed.py"
)


@pytest.fixture(autouse=True)
def _reset_quiescence():
    from backend.core.ouroboros.governance.quiescence import reset_for_tests
    reset_for_tests()
    yield
    reset_for_tests()


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_val,expected", [
    ("true", True), ("True", True), ("1", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("garbage", False),
])
def test_master_switch(env_val, expected, monkeypatch):
    monkeypatch.setenv("JARVIS_QUIESCENCE_PROTOCOL_ENABLED", env_val)
    from backend.core.ouroboros.governance.quiescence import (
        quiescence_protocol_enabled,
    )
    assert quiescence_protocol_enabled() is expected


def test_master_switch_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_QUIESCENCE_PROTOCOL_ENABLED", raising=False)
    from backend.core.ouroboros.governance.quiescence import (
        quiescence_protocol_enabled,
    )
    assert quiescence_protocol_enabled() is True


@pytest.mark.parametrize("env_val,expected", [
    ("420.0", 420.0), ("60", 60.0), ("0", 420.0),
    ("-5", 420.0), ("garbage", 420.0),
])
def test_max_pause_resolver(env_val, expected, monkeypatch):
    monkeypatch.setenv("JARVIS_QUIESCENCE_MAX_PAUSE_S", env_val)
    from backend.core.ouroboros.governance.quiescence import (
        resolve_max_pause_s,
    )
    assert resolve_max_pause_s() == pytest.approx(expected, abs=0.01)


# ---------------------------------------------------------------------------
# Gate behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_gate_allows_background(monkeypatch):
    """Default state: gate is set → await_quiescence_clearance returns
    immediately (background allowed)."""
    monkeypatch.setenv("JARVIS_QUIESCENCE_PROTOCOL_ENABLED", "true")
    from backend.core.ouroboros.governance.quiescence import (
        await_quiescence_clearance, is_core_active,
    )
    assert is_core_active() is False
    result = await asyncio.wait_for(
        await_quiescence_clearance(label="t"), timeout=1.0,
    )
    assert result is True


@pytest.mark.asyncio
async def test_core_active_pauses_background(monkeypatch):
    """The load-bearing case: when the core holds the gate, a
    background loop's await_quiescence_clearance BLOCKS until
    release."""
    monkeypatch.setenv("JARVIS_QUIESCENCE_PROTOCOL_ENABLED", "true")
    from backend.core.ouroboros.governance.quiescence import (
        await_quiescence_clearance, quiescence_core_active, is_core_active,
    )

    bg_proceeded = []

    async def _background():
        await await_quiescence_clearance(label="bg")
        bg_proceeded.append(time_marker())

    def time_marker():
        return asyncio.get_event_loop().time()

    async with quiescence_core_active(label="core"):
        assert is_core_active() is True
        # Start a background task that will block on the gate
        bg_task = asyncio.create_task(_background())
        # Give it a chance to run + block
        await asyncio.sleep(0.05)
        # It MUST still be blocked (gate cleared by core)
        assert not bg_proceeded, (
            "Background loop proceeded while core was active — "
            "quiescence containment FAILED"
        )
    # Core released — gate set — background should now proceed
    await asyncio.wait_for(bg_task, timeout=1.0)
    assert bg_proceeded, "Background did not resume after core release"
    assert is_core_active() is False


@pytest.mark.asyncio
async def test_refcount_concurrent_core_entrants(monkeypatch):
    """BG pool runs concurrent workers — gate must only release when
    the LAST concurrent core exits (refcount)."""
    monkeypatch.setenv("JARVIS_QUIESCENCE_PROTOCOL_ENABLED", "true")
    from backend.core.ouroboros.governance.quiescence import (
        quiescence_core_active, is_core_active,
    )

    async with quiescence_core_active(label="core-1"):
        assert is_core_active() is True
        async with quiescence_core_active(label="core-2"):
            assert is_core_active() is True
        # core-2 exited but core-1 still active → gate stays cleared
        assert is_core_active() is True, (
            "Gate released while a concurrent core entrant was still "
            "active — refcount broken"
        )
    # both exited → released
    assert is_core_active() is False


@pytest.mark.asyncio
async def test_anti_starvation_safety_valve(monkeypatch):
    """If the core holds the gate longer than max-pause, the
    background loop proceeds DEGRADED (returns False), never starves
    forever."""
    monkeypatch.setenv("JARVIS_QUIESCENCE_PROTOCOL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_QUIESCENCE_MAX_PAUSE_S", "0.1")  # 100ms
    from backend.core.ouroboros.governance.quiescence import (
        await_quiescence_clearance, quiescence_core_active,
    )

    async with quiescence_core_active(label="hung-core"):
        # Core holds the gate; background loop must time out and
        # proceed (degraded) within ~max_pause, NOT hang.
        result = await asyncio.wait_for(
            await_quiescence_clearance(label="bg"), timeout=2.0,
        )
        assert result is False, (
            "Expected safety-valve degrade (False) when core held "
            "the gate past max-pause; got True"
        )


@pytest.mark.asyncio
async def test_master_off_is_noop(monkeypatch):
    """Master off → core_active is pass-through, clearance is
    immediate True. Byte-identical legacy behavior."""
    monkeypatch.setenv("JARVIS_QUIESCENCE_PROTOCOL_ENABLED", "false")
    from backend.core.ouroboros.governance.quiescence import (
        await_quiescence_clearance, quiescence_core_active, is_core_active,
    )
    async with quiescence_core_active(label="core"):
        # Master off — gate never cleared
        assert is_core_active() is False
        r = await asyncio.wait_for(
            await_quiescence_clearance(label="bg"), timeout=1.0,
        )
        assert r is True


@pytest.mark.asyncio
async def test_core_exception_releases_gate(monkeypatch):
    """If the wrapped core body raises, the gate MUST still be
    released (finally) — a crashed stream cannot freeze the
    organism."""
    monkeypatch.setenv("JARVIS_QUIESCENCE_PROTOCOL_ENABLED", "true")
    from backend.core.ouroboros.governance.quiescence import (
        quiescence_core_active, is_core_active,
    )
    with pytest.raises(ValueError, match="boom"):
        async with quiescence_core_active(label="crash"):
            assert is_core_active() is True
            raise ValueError("boom")
    # Gate MUST be released despite the exception
    assert is_core_active() is False, (
        "Gate left cleared after core body raised — organism would "
        "freeze; finally-release FAILED"
    )


# ---------------------------------------------------------------------------
# AST pins — wiring
# ---------------------------------------------------------------------------


def test_ast_pin_providers_wraps_stream():
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    assert "from backend.core.ouroboros.governance.quiescence import" in src
    assert "quiescence_core_active" in src
    assert "_quiescence_core_active(label=\"claude_stream\")" in src, (
        "providers.py MUST wrap the messages.stream() in "
        "quiescence_core_active(label='claude_stream')"
    )


def test_ast_pin_oracle_index_repository_awaits_gate():
    src = _ORACLE_SRC.read_text(encoding="utf-8")
    assert "await_quiescence_clearance" in src
    assert 'label="oracle_index_repository"' in src, (
        "oracle.py _index_repository batch loop MUST await the "
        "quiescence gate (the proven B1 dominant offender)"
    )


def test_ast_pin_cooperative_yield_composes_quiescence():
    """Task #102's primitive MUST compose the gate so all its
    consumers get containment for free (no duplication)."""
    src = _ELG_SRC.read_text(encoding="utf-8")
    assert "await_quiescence_clearance" in src, (
        "cooperative_yield_every_n_async MUST await the quiescence "
        "gate at its yield point — composition over per-call-site "
        "duplication"
    )


def test_ast_pin_quiescence_public_surface():
    src = _QUIESCENCE_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    async_fns = {
        n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
    }
    assert "quiescence_protocol_enabled" in fns
    assert "resolve_max_pause_s" in fns
    assert "is_core_active" in fns
    assert "reset_for_tests" in fns
    assert "await_quiescence_clearance" in async_fns
    # quiescence_core_active is an @asynccontextmanager-decorated
    # async generator → AsyncFunctionDef
    assert "quiescence_core_active" in async_fns


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_seed_master_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    idx = src.find('name="JARVIS_QUIESCENCE_PROTOCOL_ENABLED"')
    assert idx > 0
    window = src[idx:idx + 1600]
    assert "default=True" in window
    assert "Category.SAFETY" in window
    assert "quiescence.py" in window


def test_seed_max_pause_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    idx = src.find('name="JARVIS_QUIESCENCE_MAX_PAUSE_S"')
    assert idx > 0
    window = src[idx:idx + 1500]
    assert "default=420.0" in window
    assert "Category.TUNING" in window
    assert "quiescence.py" in window
