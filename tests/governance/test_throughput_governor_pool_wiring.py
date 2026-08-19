"""The governor is CONSULTED by the pool — and composes with the lock.

Two failure modes this guards, both of which have precedent in this codebase:

  1. **Wired but inert.** A consult whose fallback touches something the class
     does not own raises, gets swallowed by a broad `except`, and returns "no
     opinion" forever. The capability ships, the tests pass, nothing happens.
  2. **A second authority.** Routing a throughput number through
     `set_target_pool_size` would either be silently rejected by the
     Immutability Lock (a no-op exactly where a serving mesh exists) or would
     have to outrank hardware truth. Lane COUNT and lane DRIVABILITY are
     different axes and must compose, not compete.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import tempfile
import textwrap
from types import SimpleNamespace
from typing import Any, cast

import pytest

from backend.core.ouroboros.governance import local_inference_director as lid
from backend.core.ouroboros.governance import throughput_governor as tg
from backend.core.ouroboros.governance.background_agent_pool import (
    BackgroundAgentPool,
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("JARVIS_LATENCY_LEDGER_PATH", os.path.join(d, "l.json"))
    monkeypatch.delenv("JARVIS_THROUGHPUT_GOVERNOR_ENABLED", raising=False)
    tg.reset_for_tests()
    yield
    tg.reset_for_tests()


def _pool(**kw) -> BackgroundAgentPool:
    return BackgroundAgentPool(
        orchestrator=cast(Any, SimpleNamespace()), pool_size=3, queue_size=4, **kw)


class TestTheConsultIsActuallyLive:
    def test_it_returns_a_real_ceiling_not_none(self):
        """The anti-inertness pin. `_throughput_lane_ceiling` reaches for a
        route budget; an earlier draft reached it via `self._config`, which
        this class does not own — every call raised, the broad `except`
        swallowed it, and the consult was dead while looking wired."""
        assert isinstance(_pool()._throughput_lane_ceiling(), int)

    def test_it_returns_none_when_the_governor_is_off(self, monkeypatch):
        """None means 'no opinion' — the caller must leave lanes untouched.
        It must be distinguishable from a real ceiling of 1."""
        monkeypatch.setenv("JARVIS_THROUGHPUT_GOVERNOR_ENABLED", "0")
        tg.reset_for_tests()
        assert _pool()._throughput_lane_ceiling() is None

    def test_the_ceiling_tracks_measured_physics(self):
        cfg = lid.LocalConfig.from_env()
        prof = lid.LatencyProfiler(cfg, ledger_key=lid.physics_key(cfg))
        for _ in range(8):
            prof.record(ttft_ms=50.0, total_ms=900.0, output_tokens=400)
        fast = _pool()._throughput_lane_ceiling()

        tg.reset_for_tests()
        prof2 = lid.LatencyProfiler(cfg, ledger_key=lid.physics_key(cfg))
        for _ in range(8):
            prof2.record(ttft_ms=50.0, total_ms=90_000.0, output_tokens=400)
        slow = _pool()._throughput_lane_ceiling()

        assert fast is not None and slow is not None
        assert fast > slow


class TestItComposesWithTheImmutabilityLock:
    def test_the_consult_never_calls_the_locked_setter(self):
        """Structural, via AST rather than substring: a comment mentioning the
        setter must not be able to fail (or pass) this test."""
        src = inspect.getsource(BackgroundAgentPool._throughput_lane_ceiling)
        tree = ast.parse(textwrap.dedent(src))
        called = {
            n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "set_target_pool_size" not in called

    @pytest.mark.asyncio
    async def test_the_ceiling_still_applies_under_a_held_lock(self):
        """The whole reason this is a separate axis. Under a topology lock,
        `set_target_pool_size` rejects every other source — so a throughput
        number pushed through it would vanish. Consulted directly, it lives."""
        pool = _pool()
        assert pool.set_target_pool_size(3, source="fleet_topology", lock=True)
        assert pool.set_target_pool_size(1, source="config") is False  # rejected
        assert pool._throughput_lane_ceiling() is not None  # survives the lock


class TestHoldIsReversibleAndRetireIsNot:
    def _throughput_branch(self) -> ast.If:
        """The `if` in `_worker_loop` guarding on the throughput ceiling."""
        src = textwrap.dedent(
            inspect.getsource(BackgroundAgentPool._worker_loop))
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.If):
                continue
            names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            if "_thr" in names:
                return node
        raise AssertionError("throughput gate not found in _worker_loop")

    def test_the_throughput_gate_continues_it_does_not_break(self):
        """A topology retire is permanent and CORRECT — a node either exists
        or it does not. A throughput ceiling comes from a reading with a ~30s
        TTL, so breaking on it would make a transient measurement (a cold
        model load, a memory spike) an irreversible decision."""
        body = self._throughput_branch().body
        kinds = {type(n) for n in ast.walk(ast.Module(body=body, type_ignores=[]))}
        assert ast.Continue in kinds
        assert ast.Break not in kinds

    def test_the_gate_waits_so_stop_stays_responsive(self):
        """A bare `continue` would spin the worker hot."""
        node = self._throughput_branch()
        assert any(isinstance(n, ast.Await) for n in ast.walk(node))

    def test_worker_zero_can_never_be_held(self):
        """The governor floors its verdict at 1 lane, so `worker_id >= ceiling`
        is false for worker 0. A zero-lane pool is a STALLED queue."""
        for total_ms in (900.0, 90_000.0, 600_000.0):
            tg.reset_for_tests()
            cfg = lid.LocalConfig.from_env()
            p = lid.LatencyProfiler(cfg, ledger_key=lid.physics_key(cfg))
            for _ in range(8):
                p.record(ttft_ms=50.0, total_ms=total_ms, output_tokens=400)
            ceiling = _pool()._throughput_lane_ceiling()
            assert ceiling is not None and ceiling >= 1


class TestNamingDoesNotCollide:
    def test_the_hold_is_not_called_park(self):
        """`ParkRequested` / `ParkedOpStore` already mean OP-level
        suspend-and-resume in this module. Reusing the word for worker-level
        lane throttling would make two mechanisms indistinguishable by name."""
        src = inspect.getsource(BackgroundAgentPool._worker_loop)
        tree = ast.parse(textwrap.dedent(src))
        attrs = {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        assert not any("park" in a.lower() for a in attrs
                       if "throughput" in a.lower())
