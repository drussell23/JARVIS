"""Hive Aggregator — read-only fan-in multiplexer (Phase 12, Hive Step 1)."""
from __future__ import annotations

import asyncio

import pytest

from backend.api.hive_aggregator import HiveAggregator
from backend.api.hive_envelope import (
    HiveTelemetryEnvelope, from_ide_sse_event, from_trinity_event,
)


# ---------------------------------------------------------------------------
# mocks mirroring the REAL source contracts (native subscribe methods)
# ---------------------------------------------------------------------------

class _TrinityEvent:
    def __init__(self, topic, payload, event_id):
        self.topic = topic; self.payload = payload; self.event_id = event_id


class _MockTrinityBus:
    """Mirrors TrinityEventBus.subscribe(pattern, handler) + wildcard routing."""
    def __init__(self):
        self._subs = {}; self._n = 0; self.unsubscribed = []

    async def subscribe(self, pattern, handler, **kw):
        self._n += 1; sid = f"t-{self._n}"; self._subs[sid] = (pattern, handler); return sid

    async def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id); self._subs.pop(sub_id, None); return True

    @staticmethod
    def _matches(pattern, topic):
        if pattern.endswith(".#"):
            base = pattern[:-2]
            return topic == base or topic.startswith(base + ".")
        return pattern == topic

    async def fire(self, topic, payload, event_id):
        ev = _TrinityEvent(topic, payload, event_id)
        for pat, h in list(self._subs.values()):
            if self._matches(pat, topic):
                await h(ev)


class _SseEvent:
    def __init__(self, event_type, op_id, payload, event_id):
        self.event_type = event_type; self.op_id = op_id
        self.payload = payload; self.event_id = event_id


class _MockSseSub:
    def __init__(self): self.queue = asyncio.Queue(); self._closed = False


class _MockSseBroker:
    """Mirrors StreamEventBroker.subscribe / stream_iter / unsubscribe."""
    def __init__(self): self.subs = []

    def subscribe(self, op_id_filter=None, last_event_id=None):
        s = _MockSseSub(); self.subs.append(s); return s

    def unsubscribe(self, sub):
        sub._closed = True

    async def stream_iter(self, sub, heartbeat_s=0):
        while not sub._closed:
            try:
                ev = await asyncio.wait_for(sub.queue.get(), timeout=0.5)
                yield ev
            except asyncio.TimeoutError:
                if sub._closed:
                    return
                continue

    async def publish(self, ev):
        for s in self.subs:
            await s.queue.put(ev)


# ---------------------------------------------------------------------------
# MANDATE 4 — 5 Trinity + 5 SSE, published simultaneously → all 10 captured,
# cast to envelopes, yielded in perfect chronological order, zero drops.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiplexer_merges_both_streams_in_chronological_order():
    bus, broker = _MockTrinityBus(), _MockSseBroker()
    agg = HiveAggregator(bus=bus, sse_broker=broker, sort_window_s=0.05)
    await agg.start()
    await asyncio.sleep(0.01)   # let subscriptions settle

    # Interleave the two fabrics with strictly increasing source timestamps.
    async def blast():
        # ts 1,3,5,7,9 → Trinity ; ts 2,4,6,8,10 → SSE
        for i in range(5):
            await bus.fire("autonomy.work_unit_state_changed",
                           {"ts": (2 * i + 1), "state": "running", "op_id": f"op{i}"},
                           event_id=f"t{i}")
            await broker.publish(_SseEvent(
                "gate_evaluated", f"op{i}",
                {"ts": (2 * i + 2), "narration_text": f"gate {i} PASS"}, f"s{i}"))

    await blast()
    await asyncio.sleep(0.2)     # > sort window: everything coalesces into one sorted batch

    got = await agg.drain_available()

    # 1. All 10 captured — nothing dropped.
    assert len(got) == 10, f"expected 10, got {len(got)}: {[e.ts for e in got]}"
    assert agg.stats["dropped_raw"] == 0 and agg.stats["dropped_out"] == 0
    # 2. Every item is a HiveTelemetryEnvelope.
    assert all(isinstance(e, HiveTelemetryEnvelope) for e in got)
    # 3. Perfect chronological order by source timestamp.
    ts_order = [e.ts for e in got]
    assert ts_order == sorted(ts_order) == [float(i) for i in range(1, 11)], ts_order
    # 4. Both fabrics are represented + correctly typed.
    fabrics = {e.source_fabric for e in got}
    assert fabrics == {"trinity", "ide_sse"}
    assert any(e.subsystem == "swarm" for e in got)        # autonomy.* → swarm
    assert any(e.subsystem == "governance" for e in got)   # gate_evaluated → governance

    await agg.stop()
    assert len(bus.unsubscribed) == len(_TrinityPatterns := [
        "training.#", "tier.#", "autonomy.#", "workflow.#", "gap.#",
        "fs.#", "command.#", "intake.#", "reactor.#", "degradation.#"])


@pytest.mark.asyncio
async def test_read_only_never_publishes_back_to_sources():
    """Mandate 1: the aggregator must be a pure listener — it must not call any
    publish method on the source bus/broker."""
    bus, broker = _MockTrinityBus(), _MockSseBroker()
    # Trip-wire: if the aggregator ever tries to publish, fail loudly.
    bus.publish = lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote to bus!"))
    agg = HiveAggregator(bus=bus, sse_broker=broker, sort_window_s=0.02)
    await agg.start()
    await bus.fire("gap.detected", {"ts": 1.0}, "g1")
    await asyncio.sleep(0.1)
    got = await agg.drain_available()
    assert len(got) == 1 and got[0].subsystem == "sensor"
    await agg.stop()


@pytest.mark.asyncio
async def test_neither_stream_blocks_the_other_under_load():
    """Fan-in stays live even if one source floods: blast 200 Trinity events;
    a lone SSE event must still make it through the merge."""
    bus, broker = _MockTrinityBus(), _MockSseBroker()
    agg = HiveAggregator(bus=bus, sse_broker=broker, sort_window_s=0.02, raw_max=64, out_max=4096)
    await agg.start()
    await asyncio.sleep(0.01)
    for i in range(200):
        await bus.fire("fs.changed.modified", {"ts": float(i)}, f"f{i}")
    await broker.publish(_SseEvent("tool_confidence", "opX", {"ts": 999.0}, "sX"))
    await asyncio.sleep(0.2)
    got = await agg.drain_available()
    # The SSE event survived despite the Trinity flood (proves independent fan-in).
    assert any(e.source_fabric == "ide_sse" and e.ts == 999.0 for e in got)
    await agg.stop()


# ---------------------------------------------------------------------------
# envelope adapters cast correctly + stay bridge-compatible (mandate 3)
# ---------------------------------------------------------------------------

def test_envelope_to_bus_payload_is_governance_bridge_compatible():
    env = from_ide_sse_event(event_type="gate_evaluated", op_id="op7",
                             payload={"narration_text": "gate PASS", "phase": "GATE"})
    p = env.to_bus_payload()
    # the exact keys governance_sse_bridge._render reads:
    for k in ("type", "narration_text", "source_brain", "narration_priority"):
        assert k in p
    assert p["narration_text"] == "gate PASS"
    assert p["type"] == "governance"


def test_trinity_swarm_lifecycle_maps_to_swarm_envelope():
    env = from_trinity_event(topic="autonomy.work_unit_state_changed",
                             payload={"state": "disposed", "op_id": "op1", "ts": 5.0})
    assert env.subsystem == "swarm"
    assert env.trace_id == "op1"
    assert env.ts == 5.0


# ---------------------------------------------------------------------------
# start_hive_relay — the ONE shared host wiring (converged organism + harness).
# Both hosts must call it on their live path (the wired-but-inert checklist).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_hive_relay_publishes_hive_tagged_frames():
    """The relay helper attaches read-only + republishes every envelope to the
    cockpit publish surface tagged ``hive: True`` (what ov_hive_panel folds)."""
    from backend.api.hive_aggregator import start_hive_relay

    bus, broker = _MockTrinityBus(), _MockSseBroker()
    published = []
    pair = await start_hive_relay(published.append, bus=bus, sse_broker=broker)
    assert pair is not None
    agg, task = pair
    await asyncio.sleep(0.01)
    await bus.fire("intake.signal_accepted", {"sensor": "test_failure", "ts": 1.0}, "e1")
    await broker.publish(_SseEvent("gate_evaluated", "op9",
                                   {"narration_text": "gate PASS", "ts": 2.0}, "e2"))
    await asyncio.sleep(0.2)
    assert len(published) == 2
    assert all(f.get("hive") is True for f in published)
    # bridge-compat keys survive the relay (governance_sse_bridge._render reads these)
    assert all("narration_text" in f and "source_brain" in f for f in published)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    await agg.stop()


@pytest.mark.asyncio
async def test_start_hive_relay_degrades_to_none_not_raise():
    """A publish surface that explodes at start must yield None, never raise."""
    from backend.api import hive_aggregator as mod

    class _Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("boom")

    orig = mod.HiveAggregator
    mod.HiveAggregator = _Boom
    try:
        pair = await mod.start_hive_relay(lambda f: None, bus=None, sse_broker=None)
        assert pair is None
    finally:
        mod.HiveAggregator = orig


def _calls_in_function(path, func_name):
    """All function/attr names called inside ``func_name`` of the module at path."""
    import ast as _ast
    tree = _ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Call):
                    f = sub.func
                    if isinstance(f, _ast.Name):
                        names.add(f.id)
                    elif isinstance(f, _ast.Attribute):
                        names.add(f.attr)
    return names


def test_both_pipeline_hosts_wire_the_hive_relay():
    """AST wiring pin: the converged --headless organism AND the battle-test
    harness (the process where the 16 sensors + GovernedLoop actually run)
    both call start_hive_relay on their cockpit-boot path. Kills the
    wired-but-inert class: an aggregator in a process with no pipeline
    traffic proves nothing."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    assert "start_hive_relay" in _calls_in_function(
        root / "backend" / "api" / "converged_headless.py", "_start_control_plane")
    assert "start_hive_relay" in _calls_in_function(
        root / "backend" / "core" / "ouroboros" / "battle_test" / "harness.py",
        "_start_cockpit_attach_bridge")


@pytest.mark.asyncio
async def test_trinity_bus_late_materialization_reattaches(monkeypatch):
    """trinity_subs=0 residual fix: when the TrinityEventBus singleton doesn't
    exist at cockpit boot, the aggregator polls the injected resolver and
    attaches the MOMENT the bus materializes — that fabric's frames then flow."""
    monkeypatch.setenv("JARVIS_HIVE_BUS_REATTACH_MIN_S", "0.01")
    monkeypatch.setenv("JARVIS_HIVE_BUS_REATTACH_MAX_S", "0.02")
    holder = {"bus": None}
    agg = HiveAggregator(bus=None, sse_broker=None,
                         bus_resolver=lambda: holder["bus"], sort_window_s=0.0)
    await agg.start()
    await asyncio.sleep(0.05)          # loop is polling; bus still absent
    assert agg._sub_ids == []
    holder["bus"] = bus = _MockTrinityBus()   # the singleton materializes late
    await asyncio.sleep(0.1)
    assert len(agg._sub_ids) > 0        # re-attached
    await bus.fire("intake.signal_accepted", {"ts": 1.0}, "late-1")
    await asyncio.sleep(0.05)
    got = await agg.drain_available()
    assert any(e.source_fabric == "trinity" for e in got)
    await agg.stop()


@pytest.mark.asyncio
async def test_no_resolver_means_no_reattach_loop():
    """Explicit bus=None WITHOUT a resolver (unit-test/injection posture) must
    not spin any background polling."""
    agg = HiveAggregator(bus=None, sse_broker=None, sort_window_s=0.0)
    await agg.start()
    # only the drainer task exists — no re-attach loop
    assert len(agg._tasks) == 1
    await agg.stop()


# ---------------------------------------------------------------------------
# Step 2: the HiveEmitter edge is the aggregator's THIRD fabric
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_three_fabric_merge_includes_actor_edge():
    """Trinity + IDE-SSE + the actor edge all fold into ONE chronological feed."""
    from backend.api.hive_emitter import HiveEmitter

    bus, broker = _MockTrinityBus(), _MockSseBroker()
    em = HiveEmitter()
    agg = HiveAggregator(bus=bus, sse_broker=broker, emitter=em,
                         sort_window_s=0.03)
    await agg.start()
    await asyncio.sleep(0.01)
    await bus.fire("intake.signal_accepted", {"ts": 1.0}, "t1")
    await broker.publish(_SseEvent("gate_evaluated", "op1", {"ts": 2.0}, "s1"))
    em.emit(actor_id="mcp.gmail_send", subsystem="mcp", intent="tool_call",
            summary="mcp_gmail_send success 812ms 2048B", trace_id="op1")
    await asyncio.sleep(0.2)
    got = await agg.drain_available()
    fabrics = {e.source_fabric for e in got}
    assert fabrics == {"trinity", "ide_sse", "actor_edge"}
    # chronological: actor edge stamped now-time sorts last
    assert [e.source_fabric for e in sorted(got, key=lambda e: e.ts)][:2] == \
        ["trinity", "ide_sse"]
    await agg.stop()


# ---------------------------------------------------------------------------
# Step 2 wiring pins: every silent-actor shim is ON its live path
# ---------------------------------------------------------------------------

def _module_calls(path):
    import ast as _ast
    tree = _ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            f = node.func
            if isinstance(f, _ast.Name):
                names.add(f.id)
            elif isinstance(f, _ast.Attribute):
                names.add(f.attr)
    return names


def test_all_silent_actor_shims_are_wired():
    """AST wiring pin (the wired-but-inert checklist): each mapped seam calls
    hive_emit on its live path."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    sites = [
        "backend/core/ouroboros/governance/tool_executor.py",     # MCP/web
        "backend/ghost_hands/orchestrator.py",                    # actuation
        "backend/core_contexts/facade.py",                        # 5 contexts
        "backend/voice/jarvis_agent_voice.py",                    # wake
        "backend/voice/streaming_stt.py",                         # STT
        "backend/voice/barge_in_detector.py",                     # TTS
        "backend/vision/realtime/frame_pipeline.py",              # perception
        "backend/core/ouroboros/consciousness/memory_engine.py",  # memory
    ]
    missing = [s for s in sites if "hive_emit" not in _module_calls(root / s)]
    assert not missing, f"hive_emit shim missing from: {missing}"


def test_ghost_hands_flushes_its_coalescing_window():
    """Sequence-completion contract: the task-complete path flushes the
    per-action window (otherwise burst envelopes lag by a full window)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    calls = _module_calls(root / "backend" / "ghost_hands" / "orchestrator.py")
    assert "hive_flush" in calls


def test_flag_registry_delegate_reaches_emitter():
    """hive_flags (in the walked governance package) must forward to the ONE
    source of truth in backend.api.hive_emitter."""
    from backend.core.ouroboros.governance.hive_flags import register_flags

    class _Reg:
        def __init__(self):
            self.specs = []

        def register(self, spec):
            self.specs.append(spec)

    reg = _Reg()
    n = register_flags(reg)
    assert n == 5 and len(reg.specs) == 5
    names = {s.name for s in reg.specs}
    assert "JARVIS_HIVE_EMITTERS_ENABLED" in names
