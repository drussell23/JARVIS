"""Synthetic probe trace-context isolation (ov doctor --live, Slice C)."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.doctor_probe import (
    SYNTHETIC_OP_PREFIX, TRACE_CLASS_SYNTHETIC, is_synthetic_trace,
    run_synthetic_tool_probe,
)


class _FakeResult:
    status = "success"
    output = "probe body"
    error = None


class _FakeToolBackend:
    """Mirrors the REAL AsyncProcessToolBackend.execute_async signature."""

    def __init__(self):
        self.calls = []

    async def execute_async(self, call, policy_ctx, deadline):
        self.calls.append((call, policy_ctx, deadline))
        return _FakeResult()


class _RecordingBus:
    def __init__(self):
        self.published = []

    async def publish_raw(self, topic, data, **kw):
        self.published.append((topic, data))
        return "evt-1"


def test_is_synthetic_trace_predicate():
    assert is_synthetic_trace(trace_class=TRACE_CLASS_SYNTHETIC)
    assert is_synthetic_trace(op_id=SYNTHETIC_OP_PREFIX + "abc")
    assert not is_synthetic_trace(op_id="op-019f-real", trace_class="")


@pytest.mark.asyncio
async def test_synthetic_probe_broadcasts_edges_but_never_mutates_state(
    monkeypatch,
):
    """THE mandate test: the synthetic injection traverses BOTH observability
    fabrics (TrinityEventBus broadcast + the hive actor_edge envelope), while
    MemoryEngine's write path catches the trace id and skips every write."""
    import backend.core.ouroboros.governance.doctor_probe as probe_mod
    from backend.api.hive_emitter import HiveEmitter

    # -- fabric capture: a recording bus + a fresh emitter as the default --
    bus = _RecordingBus()
    monkeypatch.setattr(
        "backend.core.trinity_event_bus.get_event_bus_if_exists",
        lambda: bus)
    em = HiveEmitter()
    em.bind_loop()
    monkeypatch.setattr(
        "backend.api.hive_emitter.get_default_emitter", lambda: em)

    backend_fake = _FakeToolBackend()
    verdict = await run_synthetic_tool_probe(tool_backend=backend_fake)

    # the REAL backend contract was exercised with the synthetic op ctx
    assert verdict["ok"] is True
    assert verdict["op_id"].startswith(SYNTHETIC_OP_PREFIX)
    call, policy_ctx, _deadline = backend_fake.calls[0]
    assert call.name == "web_search"
    assert policy_ctx.op_id == verdict["op_id"]

    # fabric 1 — TrinityEventBus broadcast, trace-tagged
    assert len(bus.published) == 1
    topic, data = bus.published[0]
    assert topic == "command.doctor_probe_completed"
    assert data["trace_class"] == TRACE_CLASS_SYNTHETIC

    # fabric 2 — the hive actor_edge envelope, trace-tagged
    await asyncio.sleep(0.05)
    env = em.out_queue.get_nowait()
    assert env.subsystem == "web"
    assert env.detail.get("trace_class") == TRACE_CLASS_SYNTHETIC
    assert "[synthetic probe]" in env.action_summary

    # -- state protection: MemoryEngine write path short-circuits --
    from backend.core.ouroboros.governance.consciousness_bridge import (
        ConsciousnessBridge,
    )

    ingested = []

    class _Memory:
        async def ingest_outcome(self, op_id):
            ingested.append(op_id)

    class _Consciousness:
        _memory = _Memory()

    bridge = ConsciousnessBridge(consciousness=_Consciousness())
    await bridge.record_operation_outcome(
        op_id=verdict["op_id"], files_changed=[], success=True,
        failure_reason=None)
    assert ingested == []          # the trace was caught; NO state write

    # and a REAL op still ingests (the guard never overblocks)
    await bridge.record_operation_outcome(
        op_id="op-real-123", files_changed=[], success=True,
        failure_reason=None)
    assert ingested == ["op-real-123"]


@pytest.mark.asyncio
async def test_probe_never_raises_without_backend(monkeypatch):
    """Backend construction failure → structured verdict, no exception."""
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.tool_executor."
        "AsyncProcessToolBackend",
        None, raising=False)

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no backend")

    import backend.core.ouroboros.governance.tool_executor as te
    monkeypatch.setattr(te, "AsyncProcessToolBackend", _Boom)
    verdict = await run_synthetic_tool_probe()
    assert verdict["ok"] is False
    assert verdict["status"] in ("backend_unavailable", "exec_error")
