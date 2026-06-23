"""Sovereign Temporal Breaker — tests.

A DW batch can return ``200 OK`` with ``status="in_progress"`` FOREVER (DW
accepts the batch but never finalizes it). The existing lane escalation is
ERROR-BOUND: it only fires when a poll *raises* ``DoublewordInfraError("Batch
retrieval failed")``. A perpetual ``in_progress`` never raises -> the FSM waits
forever (the node stalls). The Sovereign Temporal Breaker enforces our OWN
temporal boundary: if a batch stays ``in_progress`` past
``JARVIS_DW_BATCH_TIMEOUT_S`` it forcefully raises ``SovereignBatchTimeoutError``
(a ``DoublewordInfraError`` whose message carries "Batch retrieval failed") so
the lane-escalation predicate catches it and rotates the op off the wedged batch
lane.

Coverage:
  (a) ``SovereignBatchTimeoutError`` IS a ``DoublewordInfraError`` AND
      ``is_batch_lane_retrieval_timeout(SovereignBatchTimeoutError(), lane=
      "batch")`` is True (the escalation trigger fires).
  (b) the in_progress-past-deadline path raises it AND the raise is RE-RAISED
      (not swallowed by the loop's ``except Exception``).
  (c) ``JARVIS_DW_TEMPORAL_BREAKER_ENABLED=false`` -> no raise (legacy).
  (d) under the deadline -> no raise.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.doubleword_provider import (
    DoublewordInfraError,
    DoublewordProvider,
    SovereignBatchTimeoutError,
    _batch_temporal_deadline_s,
    _dw_temporal_breaker_enabled,
)
from backend.core.ouroboros.governance import dw_fault_taxonomy


# ---------------------------------------------------------------------------
# (a) Identity + taxonomy predicate
# ---------------------------------------------------------------------------
def test_sovereign_batch_timeout_is_doubleword_infra_error():
    err = SovereignBatchTimeoutError()
    assert isinstance(err, DoublewordInfraError)
    # Message MUST carry the Shape-1 marker so the predicate matches.
    assert "batch retrieval failed" in str(err).lower()
    # status_code 0 -> non-HTTP (our-side temporal breaker).
    assert err.status_code == 0


def test_sovereign_batch_timeout_satisfies_lane_escalation_predicate():
    err = SovereignBatchTimeoutError()
    assert dw_fault_taxonomy.is_batch_lane_retrieval_timeout(err, lane="batch") is True
    # A realtime-lane attempt must NEVER feed a batch-lane trip.
    assert dw_fault_taxonomy.is_batch_lane_retrieval_timeout(err, lane="realtime") is False


# ---------------------------------------------------------------------------
# (b) propagation: the raise is RE-RAISED, not swallowed
# ---------------------------------------------------------------------------
def test_propagation_reraise_clause_exists_in_source():
    """Structural proof: an ``except SovereignBatchTimeoutError: raise`` guard
    exists in ``_adaptive_poll_batch`` so the temporal-breaker raise escapes the
    swallowing ``except Exception`` that continues the poll loop."""
    src = inspect.getsource(DoublewordProvider._adaptive_poll_batch)
    assert "except SovereignBatchTimeoutError:" in src
    # The CancelledError re-raise must still be present and the new guard must
    # sit alongside it (both re-raise rather than continue the loop).
    assert "except asyncio.CancelledError:" in src
    # The breaker raise itself.
    assert "raise SovereignBatchTimeoutError" in src


class _FakeResp:
    def __init__(self, status: int, payload: Dict[str, Any]):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Always returns 200 + ``in_progress`` -> the perpetual-stall scenario."""

    def __init__(self):
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        return _FakeResp(200, {"status": "in_progress"})


def _make_provider() -> DoublewordProvider:
    prov = DoublewordProvider.__new__(DoublewordProvider)
    # Minimal attribute surface used by _adaptive_poll_batch.
    prov._base_url = "https://example.invalid/v1"
    prov._rate_limiter = None
    fake = _FakeSession()
    prov._fake_session = fake

    async def _get_session():
        return fake

    prov._get_session = _get_session  # type: ignore[assignment]
    prov._request_timeout = lambda: 1.0  # type: ignore[assignment]
    prov._next_poll_interval = lambda attempt: 0.0  # type: ignore[assignment]
    return prov


def _run(coro):
    return asyncio.run(coro)


def test_in_progress_past_deadline_raises_and_propagates(monkeypatch):
    """Drive a fake poll that returns ``in_progress`` forever; with a mocked
    clock past the temporal deadline, ``_adaptive_poll_batch`` MUST raise
    ``SovereignBatchTimeoutError`` (propagates out, not swallowed)."""
    monkeypatch.setenv("JARVIS_DW_TEMPORAL_BREAKER_ENABLED", "true")
    # Deadline of 0s -> any positive elapsed in the in_progress branch trips the
    # breaker on the FIRST in_progress poll. Real wall clock, no clock mocking
    # (the outer _DW_MAX_WAIT_S stays at its 3600s default, untouched, so the
    # ONLY exit is the breaker raise -> proves it propagates, not the legacy
    # timeout fall-through).
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "0")

    import backend.core.ouroboros.governance.doubleword_provider as dwp

    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(dwp.asyncio, "sleep", _nosleep)

    prov = _make_provider()
    with pytest.raises(SovereignBatchTimeoutError):
        _run(prov._adaptive_poll_batch("batch-perpetual"))


# ---------------------------------------------------------------------------
# (c) gate OFF -> legacy (no raise)
# ---------------------------------------------------------------------------
def test_disabled_no_raise_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_TEMPORAL_BREAKER_ENABLED", "false")
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "5")
    assert _dw_temporal_breaker_enabled() is False

    import backend.core.ouroboros.governance.doubleword_provider as dwp

    # Outer _DW_MAX_WAIT_S deadline shrunk so the legacy loop terminates with
    # None quickly instead of spinning to 3600s. We patch the module constant.
    monkeypatch.setattr(dwp, "_DW_MAX_WAIT_S", 0.05)

    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(dwp.asyncio, "sleep", _nosleep)

    prov = _make_provider()
    # Legacy: polls to _DW_MAX_WAIT_S and returns None (NO raise).
    result = _run(prov._adaptive_poll_batch("batch-legacy"))
    assert result is None


# ---------------------------------------------------------------------------
# (d) under the deadline -> no raise
# ---------------------------------------------------------------------------
def test_under_deadline_completes_no_raise(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_TEMPORAL_BREAKER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "300")

    import backend.core.ouroboros.governance.doubleword_provider as dwp

    async def _nosleep(*_a, **_k):
        return None

    monkeypatch.setattr(dwp.asyncio, "sleep", _nosleep)

    # A session that reports in_progress ONCE then completes — well under 300s.
    class _CompletingSession:
        def __init__(self):
            self.calls = 0

        def get(self, *a, **k):
            self.calls += 1
            if self.calls >= 2:
                return _FakeResp(200, {"status": "completed", "output_file_id": "out-123"})
            return _FakeResp(200, {"status": "in_progress"})

    prov = DoublewordProvider.__new__(DoublewordProvider)
    prov._base_url = "https://example.invalid/v1"
    prov._rate_limiter = None
    sess = _CompletingSession()

    async def _get_session():
        return sess

    prov._get_session = _get_session  # type: ignore[assignment]
    prov._request_timeout = lambda: 1.0  # type: ignore[assignment]
    prov._next_poll_interval = lambda attempt: 0.0  # type: ignore[assignment]

    result = _run(prov._adaptive_poll_batch("batch-under"))
    assert result == "out-123"


# ---------------------------------------------------------------------------
# helpers: env resolution
# ---------------------------------------------------------------------------
def test_temporal_deadline_reads_env(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "123")
    assert _batch_temporal_deadline_s() == pytest.approx(123.0)


def test_temporal_deadline_default(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_BATCH_TIMEOUT_S", raising=False)
    assert _batch_temporal_deadline_s() == pytest.approx(300.0)


def test_temporal_breaker_enabled_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_TEMPORAL_BREAKER_ENABLED", raising=False)
    assert _dw_temporal_breaker_enabled() is True


def test_temporal_deadline_failsoft_on_bad_env(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_BATCH_TIMEOUT_S", "not-a-number")
    # Fail-soft: a bad env -> default, never raises.
    assert _batch_temporal_deadline_s() == pytest.approx(300.0)
