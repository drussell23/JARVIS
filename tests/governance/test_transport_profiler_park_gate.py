"""Sovereign Transport Profiler — active-detachment park gate (2026-06-20).

An ASYNC_BATCH_PAYLOAD op must park (detach the worker) regardless of queue
pressure, on any batch-capable route — while leaving the legacy queue-pressure
park policy byte-identical for non-batch ops."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.op_park_store import should_park_for_route


@pytest.fixture(autouse=True)
def _park_on(monkeypatch):
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")
    monkeypatch.delenv("JARVIS_BG_PARK_ROUTES", raising=False)


def test_async_batch_parks_standard_route_without_queue_pressure():
    # The live wedge: standard-route diff-codegen on a batch-only model.
    assert should_park_for_route(
        "standard", queue_pressure=False, async_batch_payload=True,
    ) is True


def test_async_batch_parks_complex_and_background():
    for route in ("complex", "background"):
        assert should_park_for_route(
            route, queue_pressure=False, async_batch_payload=True,
        ) is True


def test_async_batch_does_not_park_immediate_or_speculative():
    # These never force-batch (Slice 36 gate); defense-in-depth.
    for route in ("immediate", "speculative"):
        assert should_park_for_route(
            route, queue_pressure=False, async_batch_payload=True,
        ) is False


def test_legacy_non_batch_unchanged_no_pressure():
    # Non-batch op, no queue pressure → no park (byte-identical legacy).
    assert should_park_for_route(
        "complex", queue_pressure=False, async_batch_payload=False,
    ) is False


def test_legacy_non_batch_parks_eligible_route_under_pressure():
    assert should_park_for_route(
        "complex", queue_pressure=True, async_batch_payload=False,
    ) is True


def test_master_off_never_parks_even_async_batch(monkeypatch):
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "false")
    assert should_park_for_route(
        "standard", queue_pressure=False, async_batch_payload=True,
    ) is False


def test_resumed_never_reparks_even_async_batch():
    assert should_park_for_route(
        "standard", queue_pressure=False, is_resumed=True,
        async_batch_payload=True,
    ) is False


def test_resolve_async_batch_payload_reads_profile_not_frozen_ctx(monkeypatch):
    """OperationContext is frozen — the park gate must resolve batch-only directly
    from the immortal profile + live topology, NOT a ctx tag."""
    from backend.core.ouroboros.governance import generate_park_wrapper as GPW
    from backend.core.ouroboros.governance import dw_transport_profile as TP

    monkeypatch.setenv("JARVIS_DW_TRANSPORT_PROFILE_ENABLED", "true")
    prof = TP.get_transport_profile()
    prof.record_batch_only("Qwen/Qwen3.5-397B-A17B-FP8-dottxt")

    class _Topo:
        def dw_models_for_route(self, route):
            return ("Qwen/Qwen3.5-397B-A17B-FP8-dottxt", "openai/gpt-oss-20b")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.provider_topology.get_topology",
        lambda: _Topo(),
    )

    class Ctx:
        provider_route = "standard"

    # standard route + a batch-only model in the ranked list → detach.
    assert GPW._resolve_async_batch_payload(Ctx(), "standard") is True
    # immediate route is never batch-capable → no detach.
    assert GPW._resolve_async_batch_payload(Ctx(), "immediate") is False
    prof.clear("Qwen/Qwen3.5-397B-A17B-FP8-dottxt")


def test_batch_sla_horizon_default_override_clamp(monkeypatch):
    from backend.core.ouroboros.governance.candidate_generator import (
        batch_sla_horizon_s,
    )
    monkeypatch.delenv("JARVIS_DW_BATCH_SLA_HORIZON_S", raising=False)
    monkeypatch.delenv("DOUBLEWORD_MAX_WAIT_S", raising=False)
    assert batch_sla_horizon_s() == 3600.0          # mirrors poll horizon
    monkeypatch.setenv("JARVIS_DW_BATCH_SLA_HORIZON_S", "1800")
    assert batch_sla_horizon_s() == 1800.0
    monkeypatch.setenv("JARVIS_DW_BATCH_SLA_HORIZON_S", "999999")
    assert batch_sla_horizon_s() == 24 * 3600.0     # clamp 24h
    monkeypatch.setenv("JARVIS_DW_BATCH_SLA_HORIZON_S", "10")
    assert batch_sla_horizon_s() == 300.0           # floor


def test_parked_batch_horizon_contextvar_isolated():
    """The horizon ContextVar must default off and be task-local — an in-pool
    dispatch must NEVER inherit the parked continuation's long budget."""
    from backend.core.ouroboros.governance.candidate_generator import (
        set_parked_batch_horizon,
        reset_parked_batch_horizon,
        _parked_batch_horizon_active,
    )
    assert _parked_batch_horizon_active() is False
    tok = set_parked_batch_horizon(True)
    assert _parked_batch_horizon_active() is True
    reset_parked_batch_horizon(tok)
    assert _parked_batch_horizon_active() is False


def test_resolve_async_batch_payload_false_when_no_batch_only(monkeypatch):
    from backend.core.ouroboros.governance import generate_park_wrapper as GPW

    class _Topo:
        def dw_models_for_route(self, route):
            return ("openai/gpt-oss-20b",)  # not batch-only

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.provider_topology.get_topology",
        lambda: _Topo(),
    )

    class Ctx:
        provider_route = "complex"

    assert GPW._resolve_async_batch_payload(Ctx(), "complex") is False
