# tests/governance/test_local_prime_chaos_hardware.py
"""Skip-guarded hardware-integration wrappers for the Phase 3 / 3.3 local-prime smoke checks.

These tests require a live local Ollama instance on 127.0.0.1:11434 with qwen2.5-coder:3b
pulled.  They skip cleanly in CI or on any machine without the local engine present.
"""
from __future__ import annotations

import pytest

from tests.governance.chaos_simulation_fabric import (
    ollama_available,
    run_local_prime_warm_standby_check,
    run_in_flight_exhaustion_handoff_check,
)

_NEEDS_OLLAMA = pytest.mark.skipif(
    not ollama_available(),
    reason="requires a live local Ollama (:11434) + qwen2.5-coder:3b",
)


@_NEEDS_OLLAMA
@pytest.mark.asyncio
async def test_local_prime_warm_standby_latency() -> None:
    r = await run_local_prime_warm_standby_check()
    assert r["healthy"] is True
    assert r["warm_faster"] is True               # warm-standby keeps weights resident
    assert r["memory_guard_refused"] is True       # CRITICAL evict+refuse fires


@_NEEDS_OLLAMA
@pytest.mark.asyncio
async def test_in_flight_exhaustion_handoff() -> None:
    r = await run_in_flight_exhaustion_handoff_check()
    assert r["absorbed"] is True
    assert r["kept_central"] is True and r["pruned_peripheral"] is True
    assert r["beacon_count"] >= 1
    assert r["reraised_on_unhealthy"] is True
