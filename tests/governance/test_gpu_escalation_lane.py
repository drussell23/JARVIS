"""Elastic Multi-Tier Fleet -- the GPU Escalation Lane.

During a sustained outage the 7B CPU survival node is the baseline. When a
COMPLEX/IMMEDIATE (or token-overflowing) op arrives, the lane provisions the 32B
GPU node IN PARALLEL and routes that op to it. The 7B keeps serving BACKGROUND
ops. The instant the GPU in-flight count drains to zero the lane REAPS the GPU
node (stops billing) and falls back to the single 7B. Injected provision/reap
boundaries -> no real GCP in tests.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.failover_gpu_lane import (
    GpuEscalationLane,
    GpuLaneState,
)


def _lane(provisioned_ep="http://gpu:11434", outage=True, ready=True, calls=None):
    calls = calls if calls is not None else {}
    calls.setdefault("provision", 0)
    calls.setdefault("reap", 0)

    async def provision_fn():
        calls["provision"] += 1
        return provisioned_ep

    async def reap_fn():
        calls["reap"] += 1

    async def ready_fn(ep):
        return ready

    lane = GpuEscalationLane(
        provision_fn=provision_fn, reap_fn=reap_fn, ready_fn=ready_fn,
        outage_confirmed_fn=lambda: outage,
    )
    return lane, calls


@pytest.fixture(autouse=True)
def _quality_on(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    yield


async def test_background_op_never_escalates():
    lane, calls = _lane()
    ep = await lane.request("op1", urgency="background", complexity="simple")
    assert ep is None  # 7B handles it
    assert calls["provision"] == 0
    assert lane.is_gpu_active() is False


async def test_complex_op_provisions_gpu():
    lane, calls = _lane()
    ep = await lane.request("op1", urgency="standard", complexity="complex")
    assert ep == "http://gpu:11434"
    assert calls["provision"] == 1
    assert lane.gpu_inflight_count() == 1
    assert lane.is_gpu_active() is True


async def test_second_complex_op_reuses_one_gpu():
    lane, calls = _lane()
    await lane.request("op1", urgency="immediate", complexity="simple")
    await lane.request("op2", urgency="immediate", complexity="simple")
    assert calls["provision"] == 1          # ONE node for both
    assert lane.gpu_inflight_count() == 2


async def test_token_overflow_escalates():
    lane, calls = _lane()
    ep = await lane.request("op1", urgency="background", complexity="simple",
                            estimated_tokens=40_000)
    assert ep is not None and calls["provision"] == 1  # overflow forced the GPU


async def test_drain_to_zero_reaps_gpu():
    lane, calls = _lane()
    await lane.request("op1", urgency="immediate", complexity="complex")
    await lane.request("op2", urgency="immediate", complexity="complex")
    await lane.complete("op1")
    assert calls["reap"] == 0                # still one in-flight
    assert lane.is_gpu_active() is True
    await lane.complete("op2")
    assert calls["reap"] == 1                # drained -> reaped
    assert lane.is_gpu_active() is False
    assert lane.gpu_inflight_count() == 0


async def test_disabled_quality_never_provisions(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "false")
    lane, calls = _lane()
    ep = await lane.request("op1", urgency="immediate", complexity="complex")
    assert ep is None and calls["provision"] == 0


async def test_no_outage_does_not_escalate():
    lane, calls = _lane(outage=False)
    ep = await lane.request("op1", urgency="immediate", complexity="complex")
    assert ep is None and calls["provision"] == 0  # only escalate on sustained outage


async def test_provision_failure_failsoft():
    lane, calls = _lane(provisioned_ep=None)  # provision returns no endpoint
    ep = await lane.request("op1", urgency="immediate", complexity="complex")
    assert ep is None
    assert lane.is_gpu_active() is False
    assert lane.gpu_inflight_count() == 0     # no phantom in-flight
    assert lane._state == GpuLaneState.IDLE


async def test_not_ready_reaps_and_failsoft():
    lane, calls = _lane(ready=False)          # node comes up but never ready
    ep = await lane.request("op1", urgency="immediate", complexity="complex")
    assert ep is None
    assert calls["reap"] == 1                 # orphan node torn down
    assert lane._state == GpuLaneState.IDLE


async def test_concurrent_requests_single_provision():
    import asyncio
    lane, calls = _lane()
    results = await asyncio.gather(*[
        lane.request(f"op{i}", urgency="immediate", complexity="complex")
        for i in range(5)
    ])
    assert all(r == "http://gpu:11434" for r in results)
    assert calls["provision"] == 1            # no double-spend under concurrency
    assert lane.gpu_inflight_count() == 5
