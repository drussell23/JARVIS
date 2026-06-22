"""tests/governance/test_transport_breaker_wiring.py
-- Task A3: TransportCircuitBreaker wired into DW dispatch.

Tests are intentionally thin and fast: they exercise the new
_breaker_select_transport() seam in candidate_generator, not the full
dispatch loop (which requires a heavy async context).

Isolation strategy: we patch ``get_transport_breaker`` to return a lightweight
stub that implements only ``select_lane`` -- avoiding the enum-identity
breakage that occurs when ``importlib.reload`` creates a new BreakerState enum
class while the existing breaker instance still holds references to the old one.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import transport_circuit_breaker as tcb
from backend.core.ouroboros.governance import candidate_generator as cg


class _StubBreaker:
    """Minimal stub with a fixed select_lane result for each lane."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping  # preferred -> result

    def select_lane(self, preferred: str, *, now: float) -> str:  # noqa: ARG002
        return self._mapping.get(preferred, preferred)

    def record(self, lane: str, *, ok: bool, failure_mode: str | None, now: float) -> None:
        pass  # no-op for these tests


# ---------------------------------------------------------------------------
# A3-T1: _breaker_select_transport selects realtime when batch is OPEN
# ---------------------------------------------------------------------------

def test_open_batch_forces_realtime(monkeypatch):
    """When the batch lane is OPEN and the breaker is enabled,
    _breaker_select_transport('batch') must return 'realtime'."""
    # Stub returns 'realtime' for 'batch' -- mimics an OPEN batch lane.
    stub = _StubBreaker({"batch": "realtime", "realtime": "realtime"})
    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED", "true")
    monkeypatch.setattr(tcb, "get_transport_breaker", lambda: stub)

    assert cg._breaker_select_transport("batch") == "realtime"


# ---------------------------------------------------------------------------
# A3-T2: _breaker_select_transport is OFF byte-identical when disabled
# ---------------------------------------------------------------------------

def test_disabled_returns_preferred(monkeypatch):
    """When the breaker is disabled, _breaker_select_transport must return
    preferred unchanged -- OFF byte-identical (the stub is never consulted)."""
    stub = _StubBreaker({"batch": "realtime"})  # would rotate if called
    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED", "false")
    monkeypatch.setattr(tcb, "get_transport_breaker", lambda: stub)

    assert cg._breaker_select_transport("batch") == "batch"


# ---------------------------------------------------------------------------
# A3-T3: closed batch passes through unchanged
# ---------------------------------------------------------------------------

def test_closed_batch_passes_through(monkeypatch):
    """CLOSED batch -> preferred returned unchanged (no rotation)."""
    stub = _StubBreaker({"batch": "batch"})  # CLOSED: returns preferred
    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED", "true")
    monkeypatch.setattr(tcb, "get_transport_breaker", lambda: stub)

    assert cg._breaker_select_transport("batch") == "batch"


# ---------------------------------------------------------------------------
# A3-T4: realtime passes through (no sibling rotation when preferred=realtime)
# ---------------------------------------------------------------------------

def test_realtime_passes_through_when_closed(monkeypatch):
    """CLOSED realtime -> preferred returned unchanged."""
    stub = _StubBreaker({"realtime": "realtime"})
    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED", "true")
    monkeypatch.setattr(tcb, "get_transport_breaker", lambda: stub)

    assert cg._breaker_select_transport("realtime") == "realtime"


# ---------------------------------------------------------------------------
# A3-T5: breaker exception in select_lane is fail-soft (returns preferred)
# ---------------------------------------------------------------------------

def test_breaker_error_is_failsoft(monkeypatch):
    """If get_transport_breaker().select_lane raises, _breaker_select_transport
    must swallow it and return preferred unchanged."""
    class _BadBreaker:
        def select_lane(self, preferred: str, *, now: float) -> str:
            raise RuntimeError("simulated breaker failure")

    monkeypatch.setenv("JARVIS_TRANSPORT_BREAKER_ENABLED", "true")
    monkeypatch.setattr(tcb, "get_transport_breaker", _BadBreaker)

    # Must not raise; must return preferred.
    assert cg._breaker_select_transport("batch") == "batch"
