"""Operator-yield bridge tests (spec §5.4, LR-B / Task 8).

The bridge wires operator-presence events into the existing cooperative
park/resume machinery:

* ``operator.active``  → set a module suspend flag so the next park-decision
  point (``should_park_for_route(operator_suspended=True)``) parks the op at
  its next SAFE checkpoint, freeing the worker.
* ``operator.idle``    → clear the flag + resume parked ops via
  ``BackgroundAgentPool.submit_for_resume``.

All tests use fakes — no real bus, no real event loop scheduling beyond the
coroutines under test. Master flag ``JARVIS_OPERATOR_YIELD_ENABLED`` gates the
whole surface; off → byte-identical no-op.
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Tuple

import pytest

from backend.core.ouroboros.governance import op_park_store
from backend.core.ouroboros.governance import operator_yield_bridge as bridge


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_flag():
    """Ensure the module suspend flag starts cleared for every test."""
    bridge.set_operator_idle()
    yield
    bridge.set_operator_idle()


class _FakeCtx:
    def __init__(self, op_id: str, route: str = "background") -> None:
        self.op_id = op_id
        self.provider_route = route


class _FakeBgOp:
    def __init__(self, op_id: str, status: str, context: Any) -> None:
        self.op_id = op_id
        self.status = status
        self.context = context
        self.park_attempt_seq = 1


class _FakePool:
    """Minimal stand-in exposing the surface the bridge needs."""

    def __init__(self, parked: List[_FakeBgOp]) -> None:
        self._parked = parked
        self._resumed_ops: dict = {}
        self.submitted: List[Tuple[str, int]] = []

    def list_all(self):
        return list(self._parked)

    def is_resumed_dispatch(self, ctx_op_id: str) -> bool:
        return ctx_op_id in self._resumed_ops

    async def submit_for_resume(self, ctx, *, attempt_seq: int) -> str:
        op_id = str(getattr(ctx, "op_id", "") or "")
        self.submitted.append((op_id, attempt_seq))
        self._resumed_ops[op_id] = attempt_seq
        return f"pool-{op_id}"


# ---------------------------------------------------------------------------
# (a) should_park_for_route gains operator_suspended
# ---------------------------------------------------------------------------


def test_should_park_operator_suspended_true_parks_supported_route(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")
    # background is in the park-supported set; operator_suspended forces park
    # even with no queue pressure (the worker must be freed for the operator).
    assert op_park_store.should_park_for_route(
        "background", queue_pressure=False, operator_suspended=True
    ) is True
    assert op_park_store.should_park_for_route(
        "complex", queue_pressure=False, operator_suspended=True
    ) is True
    assert op_park_store.should_park_for_route(
        "standard", queue_pressure=False, operator_suspended=True
    ) is True


def test_should_park_operator_suspended_false_preserves_existing(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")
    # operator_suspended=False (default) → identical to pre-Task-8 behaviour:
    # no queue pressure + no batch payload → no park.
    assert op_park_store.should_park_for_route(
        "background", queue_pressure=False, operator_suspended=False
    ) is False
    assert op_park_store.should_park_for_route(
        "background", queue_pressure=False
    ) is False  # default
    # With queue pressure the existing route-eligibility still parks.
    assert op_park_store.should_park_for_route(
        "background", queue_pressure=True, operator_suspended=False
    ) is True


def test_should_park_operator_suspended_respects_master_flag_off(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("JARVIS_BG_PARK_ENABLED", raising=False)
    # Master park flag off → no park even when operator_suspended.
    assert op_park_store.should_park_for_route(
        "background", queue_pressure=False, operator_suspended=True
    ) is False


def test_should_park_operator_suspended_unsupported_route_no_park(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")
    # IMMEDIATE / SPECULATIVE are not park-supported routes — even under
    # operator suspend they must not park (no resume continuation support).
    assert op_park_store.should_park_for_route(
        "immediate", queue_pressure=False, operator_suspended=True
    ) is False
    assert op_park_store.should_park_for_route(
        "speculative", queue_pressure=False, operator_suspended=True
    ) is False


def test_should_park_resumed_dispatch_never_parks_even_when_suspended(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")
    assert op_park_store.should_park_for_route(
        "background",
        queue_pressure=True,
        is_resumed=True,
        operator_suspended=True,
    ) is False


# ---------------------------------------------------------------------------
# (b) bridge flag + handlers
# ---------------------------------------------------------------------------


def test_operator_suspended_false_when_yield_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)
    bridge.set_operator_active()  # set the raw flag
    # Flag is set, but yield disabled → operator_suspended() reports False.
    assert bridge.operator_suspended() is False


def test_operator_suspended_reflects_flag_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    assert bridge.operator_suspended() is False
    bridge.set_operator_active()
    assert bridge.operator_suspended() is True
    bridge.set_operator_idle()
    assert bridge.operator_suspended() is False


def test_set_active_set_idle_toggle_the_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    bridge.set_operator_active()
    assert bridge.operator_suspended() is True
    bridge.set_operator_idle()
    assert bridge.operator_suspended() is False


def test_on_operator_active_sets_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    asyncio.run(bridge.on_operator_active(None, pool=_FakePool([])))
    assert bridge.operator_suspended() is True


def test_on_operator_idle_clears_flag_and_resumes_parked_ops(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    bridge.set_operator_active()

    parked = [
        _FakeBgOp("op-a", "parked", _FakeCtx("op-a")),
        _FakeBgOp("op-b", "running", _FakeCtx("op-b")),  # not parked → skip
        _FakeBgOp("op-c", "parked", _FakeCtx("op-c")),
    ]
    pool = _FakePool(parked)

    asyncio.run(bridge.on_operator_idle(None, pool=pool))

    # Flag cleared
    assert bridge.operator_suspended() is False
    # Only the two parked ops were resumed
    submitted_ids = sorted(op_id for op_id, _ in pool.submitted)
    assert submitted_ids == ["op-a", "op-c"]


def test_on_operator_idle_skips_already_resumed_ops(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    parked = [_FakeBgOp("op-a", "parked", _FakeCtx("op-a"))]
    pool = _FakePool(parked)
    pool._resumed_ops["op-a"] = 1  # already in a resume dispatch
    asyncio.run(bridge.on_operator_idle(None, pool=pool))
    assert pool.submitted == []  # no double-resume


# ---------------------------------------------------------------------------
# Master-flag-off → byte-identical no-op
# ---------------------------------------------------------------------------


def test_bridge_handlers_noop_when_yield_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)
    pool = _FakePool([_FakeBgOp("op-a", "parked", _FakeCtx("op-a"))])

    asyncio.run(bridge.on_operator_active(None, pool=pool))
    assert bridge.operator_suspended() is False  # disabled → reports False

    asyncio.run(bridge.on_operator_idle(None, pool=pool))
    assert pool.submitted == []  # no resume attempted when disabled


def test_attach_noop_when_yield_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JARVIS_OPERATOR_YIELD_ENABLED", raising=False)

    class _FakeBus:
        def __init__(self) -> None:
            self.subscribed: List[str] = []

        async def subscribe(self, pattern, handler, **kw):
            self.subscribed.append(pattern)
            return "sub-id"

    bus = _FakeBus()
    asyncio.run(bridge.attach(bus=bus, pool=_FakePool([])))
    assert bus.subscribed == []  # nothing subscribed when disabled


def test_attach_subscribes_both_topics_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")
    from backend.core.ouroboros.governance.operator_presence import (
        EVENT_OPERATOR_ACTIVE,
        EVENT_OPERATOR_IDLE,
    )

    class _FakeBus:
        def __init__(self) -> None:
            self.subscribed: List[str] = []

        async def subscribe(self, pattern, handler, **kw):
            self.subscribed.append(pattern)
            return f"sub-{len(self.subscribed)}"

    bus = _FakeBus()
    asyncio.run(bridge.attach(bus=bus, pool=_FakePool([])))
    assert EVENT_OPERATOR_ACTIVE in bus.subscribed
    assert EVENT_OPERATOR_IDLE in bus.subscribed


def test_attach_fail_soft_on_bad_bus(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_OPERATOR_YIELD_ENABLED", "true")

    class _BadBus:
        async def subscribe(self, *a, **k):
            raise RuntimeError("boom")

    # Must not raise — fail-soft.
    asyncio.run(bridge.attach(bus=_BadBus(), pool=_FakePool([])))
