"""Loop starvation is not model latency, and the local budget must not charge
the model for it — nor teach the profiler that it should.

Measured, bt-2026-09-09-024244::

    [CandidateGenerator] Primary plan() failed (LocalLatencyLockup:
        local_inference timeout: budget=4000ms warm=True), trying fallback
    ...
    [ControlPlaneStarvation] lag_ms=1739.6 ... — main asyncio loop is starved

Two seconds apart, on the same loop: roughly half the 4s budget was never
available to the model. `asyncio.wait_for` measures WALL time, so the starvation
was charged to inference — and then charged a second time, because the handler
fed `record_timeout_penalty` and inflated every later budget with it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import local_inference_director as LID


class _Profiler:
    def __init__(self, budget_ms: float = 4000.0):
        self._budget_ms = budget_ms
        self.penalties = []

    def adaptive_timeout_ms(self, **_kw):
        return self._budget_ms

    def record_timeout_penalty(self, ms):
        self.penalties.append(ms)

    def is_warm(self):
        return True


class _Client:
    """Only the seam under test — the guarded-complete budget arithmetic."""

    def __init__(self, profiler, sleep_s):
        self.profiler = profiler
        self._sleep_s = sleep_s
        self._cfg = type("C", (), {"num_ctx": 0})()

    async def complete(self, **_kw):
        await asyncio.sleep(self._sleep_s)
        return "done"

    complete_guarded = LID.LocalPrimeClient.complete_guarded


def _run(client, **kw):
    return asyncio.run(client.complete_guarded(
        system="s", user="u", prompt_tokens=10, max_tokens=64,
        temperature=0.2, sampling=None, response_format=None, on_token=None,
        **kw,
    ))


@pytest.fixture(autouse=True)
def _lag_compensation_on(monkeypatch):
    monkeypatch.setenv("JARVIS_STREAM_LAG_COMPENSATION_ENABLED", "true")
    yield


def test_credited_starvation_is_not_recorded_as_a_model_penalty(monkeypatch):
    """THE defect: a starved loop taught the profiler the model was slow."""
    monkeypatch.setattr(LID, "_recent_lag_ms", lambda *a, **k: 1739.6)
    prof = _Profiler(budget_ms=50.0)
    client = _Client(prof, sleep_s=30.0)

    with pytest.raises(LID.LocalLatencyLockup) as exc:
        _run(client)

    assert prof.penalties == [], (
        "starvation-explained timeout was charged to the model's latency profile"
    )
    assert "lag_credit=" in str(exc.value)


def test_a_genuine_model_timeout_is_still_penalised(monkeypatch):
    """The other half: with a healthy loop nothing is credited, and a real
    overrun must still teach the profiler."""
    monkeypatch.setattr(LID, "_recent_lag_ms", lambda *a, **k: 0.0)
    prof = _Profiler(budget_ms=50.0)
    client = _Client(prof, sleep_s=30.0)

    with pytest.raises(LID.LocalLatencyLockup):
        _run(client)

    assert prof.penalties == [50.0]


def test_the_credit_widens_the_budget(monkeypatch):
    """A call that finishes inside budget+credit must survive."""
    monkeypatch.setattr(LID, "_recent_lag_ms", lambda *a, **k: 400.0)
    prof = _Profiler(budget_ms=100.0)
    client = _Client(prof, sleep_s=0.25)   # > 100ms, < 100ms + 400ms credit

    assert _run(client) == "done"
    assert prof.penalties == []


def test_the_credit_is_capped_not_unbounded(monkeypatch):
    """An uncapped credit would turn a wedged model into an infinite wait."""
    monkeypatch.setenv("JARVIS_STREAM_LAG_CREDIT_CAP_S", "0.2")
    monkeypatch.setattr(LID, "_recent_lag_ms", lambda *a, **k: 600_000.0)
    prof = _Profiler(budget_ms=50.0)
    client = _Client(prof, sleep_s=30.0)

    with pytest.raises(LID.LocalLatencyLockup):
        asyncio.run(asyncio.wait_for(
            client.complete_guarded(
                system="s", user="u", prompt_tokens=10, max_tokens=64,
                temperature=0.2, sampling=None, response_format=None,
                on_token=None,
            ),
            timeout=5.0,
        ))


def test_the_credit_reuses_the_stream_primitive():
    """One credit policy, one cap, one flag — a second would be a second thing
    to keep in agreement."""
    import inspect

    src = inspect.getsource(LID.LocalPrimeClient.complete_guarded)
    assert "_lag_compensated_timeout_s" in src
