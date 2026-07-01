"""Absolute Route Sealing -- kill the cascade leak.

The hybrid-execution-mesh soak revealed a routing leak: when the awakened 32B
timed out mid-generation, the Phase 3c sentinel seam caught the failure and
CASCADED to the DoubleWord lane -- which, under the synthetic adversary, is the
``adversary-stub-model``. The stub then retry-stalled and the watchdog killed
the run.

When the router has COMMITTED to the sovereign J-Prime provider (a Cryo-DLQ pin
``provider_override=gcp-jprime``, or the hybrid-mesh flag is armed), a 32B
failure/timeout must be TERMINAL -- raise ``sovereign_route_sealed`` and halt the
cognitive loop, NEVER cascade to the local/stub lane.

Default OFF (no override, no flag) -> byte-identical legacy fail-soft cascade.
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import pytest

import backend.core.ouroboros.governance.candidate_generator as cg


def _deadline(seconds: float = 60.0):
    return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=seconds)


# --- sealing predicate ------------------------------------------------------

def test_sealing_true_on_override():
    ctx = SimpleNamespace(provider_override="gcp-jprime", op_id="op-1")
    assert cg._absolute_route_sealing(ctx) is True


def test_sealing_true_on_env_flag(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_ABSOLUTE_ROUTE_SEALING", "true")
    ctx = SimpleNamespace(provider_override="", op_id="op-2")
    assert cg._absolute_route_sealing(ctx) is True


def test_sealing_false_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_FAILOVER_ABSOLUTE_ROUTE_SEALING", raising=False)
    ctx = SimpleNamespace(provider_override="", op_id="op-3")
    assert cg._absolute_route_sealing(ctx) is False


def test_sealing_failsoft_on_bad_ctx(monkeypatch):
    monkeypatch.delenv("JARVIS_FAILOVER_ABSOLUTE_ROUTE_SEALING", raising=False)
    assert cg._absolute_route_sealing(object()) is False  # no attr -> False, no raise


# --- the sentinel seam SEALS instead of cascading (when committed) ----------

class _StubGen:
    """Minimal stub carrying only the two seam collaborators. If the seal fires,
    the DW code below the seam is never reached (so no other attrs are needed)."""

    def __init__(self, *, endpoint, dispatch):
        self._endpoint = endpoint
        self._dispatch = dispatch

    async def _discover_jprime_endpoint(self):
        return self._endpoint

    async def _failover_local_dispatch(self, context, deadline, endpoint):
        return await self._dispatch(context, deadline, endpoint)


async def test_sentinel_seals_on_32b_exception_under_sealing(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_ABSOLUTE_ROUTE_SEALING", "true")

    async def _raise(*_a):
        raise RuntimeError("LocalLatencyLockup: local_inference timeout budget=150000ms")

    stub = _StubGen(endpoint="http://10.0.0.5:11434", dispatch=_raise)
    ctx = SimpleNamespace(provider_override="", op_id="op-seal-1")
    with pytest.raises(RuntimeError) as ei:
        await cg.CandidateGenerator._dispatch_via_sentinel(stub, ctx, _deadline(), "standard")
    assert "sovereign_route_sealed" in str(ei.value)


async def test_sentinel_seals_on_empty_result_under_sealing(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_ABSOLUTE_ROUTE_SEALING", "true")

    async def _empty(*_a):
        return None  # discovered + dispatched but yielded nothing

    stub = _StubGen(endpoint="http://10.0.0.5:11434", dispatch=_empty)
    ctx = SimpleNamespace(provider_override="gcp-jprime", op_id="op-seal-2")
    with pytest.raises(RuntimeError, match="sovereign_route_sealed"):
        await cg.CandidateGenerator._dispatch_via_sentinel(stub, ctx, _deadline(), "standard")


async def test_sentinel_does_not_seal_when_off(monkeypatch):
    """Sealing OFF -> legacy fail-soft: the seam does NOT raise sovereign_route_sealed;
    it falls through to the DW path (which errors on the stub, proving fall-through)."""
    monkeypatch.delenv("JARVIS_FAILOVER_ABSOLUTE_ROUTE_SEALING", raising=False)

    async def _empty(*_a):
        return None

    stub = _StubGen(endpoint="http://10.0.0.5:11434", dispatch=_empty)
    ctx = SimpleNamespace(provider_override="", op_id="op-nofell")
    with pytest.raises(Exception) as ei:
        await cg.CandidateGenerator._dispatch_via_sentinel(stub, ctx, _deadline(), "standard")
    # It fell through to the (stub-incompatible) DW path -> NOT a seal.
    assert "sovereign_route_sealed" not in str(ei.value)


async def test_sentinel_no_seal_when_not_committed(monkeypatch):
    """No endpoint discovered = we never committed to J-Prime -> not a '32B failure
    during generation'. Even with sealing ON, fall through to legacy (do not seal
    on a pre-dispatch miss)."""
    monkeypatch.setenv("JARVIS_FAILOVER_ABSOLUTE_ROUTE_SEALING", "true")

    async def _never_called(*_a):
        raise AssertionError("dispatch must not be called when no endpoint")

    stub = _StubGen(endpoint=None, dispatch=_never_called)
    ctx = SimpleNamespace(provider_override="", op_id="op-nocommit")
    with pytest.raises(Exception) as ei:
        await cg.CandidateGenerator._dispatch_via_sentinel(stub, ctx, _deadline(), "standard")
    assert "sovereign_route_sealed" not in str(ei.value)


# --- the terminal reason is classified non-retryable (halt, no GENERATE_RETRY) -

def test_sovereign_route_sealed_is_nonretryable():
    from backend.core.ouroboros.governance.orchestrator import _is_nonretryable_terminal
    assert _is_nonretryable_terminal("sovereign_route_sealed") is True
    assert _is_nonretryable_terminal("sovereign_route_sealed:gcp-jprime:LocalLatencyLockup") is True
    # existing exact-match codes unaffected
    assert _is_nonretryable_terminal("advisor_blocked") is True
    assert _is_nonretryable_terminal("some_retryable_thing") is False
