"""Sentinel hardening — Staged Payload Verification + Swarm Trace + AIMD yield.

Mandated bulletproof: a DW that PASSES the 5-token probe but FAILS the 150-token
synthetic workload must leave the Sentinel in WATCHING (probe_fn → False),
aborting the orchestrator handoff — the "fake recovery" edge case.

Plus: pass-1 failure short-circuits (never wastes the big workload); both-pass
hands off; the SwarmTracer writes a deterministic JSONL lifecycle with the
mandated fields; AIMD yields-on-fault (downscale) without collapsing to error.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.governance.dw_deep_probe import (
    make_staged_probe_fn,
    staged_health_check,
)
from backend.core.ouroboros.governance.dw_outage_forecaster import AIMDController
from backend.core.ouroboros.governance.swarm_trace import SwarmTracer


def _stream(gaps_and_tokens):
    seq = list(gaps_and_tokens)

    async def _dispatch(payload):
        it = iter(seq)

        async def _rl():
            try:
                gap, tok = next(it)
            except StopIteration:
                return b""
            if gap:
                await asyncio.sleep(gap)
            return f'data: {{"choices":[{{"delta":{{"content":"{tok}"}}}}]}}'.encode()

        return _rl

    return _dispatch


def _staged_mock(*, pass1_ok: bool = True, pass2_ok: bool = False):
    """A dispatch that behaves differently by payload size: the 5-token probe
    (max_tokens<=5) vs the 150-token synthetic workload."""
    async def _dispatch(payload):
        mt = payload.get("max_tokens", 5)
        if mt <= 5:
            gaps = ([(0.0, "a"), (0.01, "b"), (0.01, "c")] if pass1_ok
                    else [(0.0, "a"), (0.3, "b")])                     # rupture
        else:
            gaps = ([(0.0, "a"), (0.01, "b"), (0.01, "c")] if pass2_ok
                    else [(0.0, "a"), (0.12, "b"), (0.12, "c")])       # ITL creep
        return await _stream(gaps)(payload)

    return _dispatch


# ---------------------------------------------------------------------------
# Staged Payload Verification (the mandated "fake recovery" guard)
# ---------------------------------------------------------------------------


async def test_pass1_ok_pass2_fails_stays_watching() -> None:
    dispatch = _staged_mock(pass1_ok=True, pass2_ok=False)
    res = await staged_health_check(
        dispatch_fn=dispatch, ttft_bound_s=120.0, itl_hard_s=8.0,
        itl_safe_s=0.05, pass2_itl_safe_s=0.05,
    )
    # Pass 1 woke the lane; Pass 2 exposed sustained-ITL degradation.
    assert res.pass1.healthy is True
    assert res.pass2 is not None and res.pass2.healthy is False
    assert res.stage == "pass2_degraded"
    assert res.healthy is False

    # The Sentinel's probe_fn therefore reports NOT-ready → handoff aborted.
    probe_fn = make_staged_probe_fn(
        dispatch, ttft_bound_s=120.0, itl_hard_s=8.0,
        itl_safe_s=0.05, pass2_itl_safe_s=0.05,
    )
    assert await probe_fn() is False


async def test_pass1_failure_short_circuits_pass2() -> None:
    dispatch = _staged_mock(pass1_ok=False)
    res = await staged_health_check(
        dispatch_fn=dispatch, ttft_bound_s=120.0, itl_hard_s=0.05, itl_safe_s=2.0,
    )
    assert res.stage == "pass1_degraded"
    assert res.healthy is False
    assert res.pass2 is None            # never wasted the 150-token workload


async def test_both_passes_hands_off() -> None:
    dispatch = _staged_mock(pass1_ok=True, pass2_ok=True)
    res = await staged_health_check(
        dispatch_fn=dispatch, ttft_bound_s=120.0, itl_hard_s=8.0, itl_safe_s=0.05,
    )
    assert res.healthy is True
    assert res.stage == "both_passed"
    probe_fn = make_staged_probe_fn(
        dispatch, ttft_bound_s=120.0, itl_hard_s=8.0, itl_safe_s=0.05,
    )
    assert await probe_fn() is True


# ---------------------------------------------------------------------------
# Deterministic Swarm Trace (JSONL artifact)
# ---------------------------------------------------------------------------


async def test_swarm_tracer_writes_jsonl_lifecycle(tmp_path) -> None:
    p = tmp_path / "swarm_trace.jsonl"
    tr = SwarmTracer(str(p), op_id="op-42")
    tr.record_fan_out(
        sub_agent="alpha", symbol="SagaApplyStrategy._topological_sort",
        node_start_line=600, node_end_line=628, concurrency=1,
    )
    tr.record_token(sub_agent="alpha", ttft_s=0.53, itl_s=0.021)
    tr.record_aimd(event="additive_increase", concurrency=2)
    tr.record_fan_in(
        sub_agent="alpha", symbol="SagaApplyStrategy._topological_sort",
        converged=True, concurrency=2,
    )

    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    recs = [json.loads(ln) for ln in lines]     # every line is valid JSON

    fo = recs[0]
    assert fo["phase"] == "fan_out"
    assert fo["op_id"] == "op-42"
    assert fo["node_start_line"] == 600 and fo["node_end_line"] == 628
    assert fo["concurrency"] == 1
    assert "ts_mono" in fo and "ts_wall" in fo   # exact timestamps recorded
    assert recs[1]["ttft_s"] == 0.53 and recs[1]["itl_s"] == 0.021
    assert recs[2]["phase"] == "aimd" and recs[2]["concurrency"] == 2
    assert recs[3]["phase"] == "fan_in" and recs[3]["converged"] is True
    assert [r["seq"] for r in recs] == [1, 2, 3, 4]   # deterministic ordering


async def test_swarm_tracer_bus_subscribe_and_translate() -> None:
    class _Bus:
        def __init__(self):
            self.pattern = None

        async def subscribe(self, pattern, handler):
            self.pattern = pattern
            return "sub-1"

    tr = SwarmTracer("/dev/null")
    bus = _Bus()
    sid = await tr.attach_to_bus(bus)
    assert sid == "sub-1"
    assert bus.pattern == "swarm.#"

    from types import SimpleNamespace
    # A bus event translates to a trace line without raising (sink=/dev/null).
    await tr.on_event(SimpleNamespace(
        topic="swarm.fan_out", payload={"sub_agent": "beta", "concurrency": 1},
    ))


# ---------------------------------------------------------------------------
# AIMD Yield-on-Fault (downscale without failing the op)
# ---------------------------------------------------------------------------


async def test_aimd_yields_on_transient_fault_without_failing() -> None:
    aimd = AIMDController(max_limit=8, floor=1)
    for _ in range(5):
        aimd.on_success()
    assert aimd.limit == 6                       # ramped up under recovery

    # A [TransientAbsorb] DW degradation mid-scale-up → multiplicative downscale.
    assert aimd.on_transient_fault() == 3        # 6 // 2 — never raises, never 0
    assert aimd.limit >= aimd._floor

    # A severe degradation storm → hard yield straight to the floor.
    assert aimd.throttle_to_floor() == 1

    # And it can re-ramp afterward (slow-start again).
    assert aimd.on_success() == 2
