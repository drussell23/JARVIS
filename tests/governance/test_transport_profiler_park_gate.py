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
