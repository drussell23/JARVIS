# Stage 2 — Brain Relocation (Distributed Body/Brain) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The governance Brain (FSM + 14 Body-independent sensors + observers) runs on the Stage-1 GCP Brain VM consuming Body signals streamed from the Mac over the Stage-0/1 mTLS WS bus; the Mac runs a thin Body-mode driver (Voice/Vision sensors + console census) whose control-plane starvation is ≈ 0.

**Architecture:** A `TrinityBusBridge` adapter mirrors selected topics between a local `TrinityEventBus` and a `StreamEventBroker` (which the proven Stage-0/1 `DistributedEventBus` already carries across the socket). The Brain organism hosts the mTLS WS server **in-process** on its own site (NOT on `EventChannelServer` — that surface is loopback-only by grep-enforced invariant; same intent, organism-owned server). Body sensors are unchanged: a `RemoteIntakeRouter` shim implements the same `ingest(envelope)` seam they already call, publishing envelopes onto the bus; a Brain-side `RemoteIntakeBridge` subscriber feeds them into the real `UnifiedIntakeRouter` (which already dedups by `envelope.dedup_key`).

**Tech Stack:** aiohttp WS (Stage-0 transport subpackage), `TrinityEventBus` (`backend/core/trinity_event_bus.py`), `IntentEnvelope` (`intake/intent_envelope.py`), Stage-1 discovery/mTLS (`brain_discovery.py`, `gen_brain_mtls.py`), pytest.

## Global Constraints

- `from __future__ import annotations` in every new file.
- Python 3.9: `asyncio.wait_for`, never `asyncio.timeout`.
- Async-first: no blocking calls on the event loop.
- Zero hardcoded endpoints/models: every knob env-resolved with defaults.
- Ships DARK: everything gated behind `JARVIS_DISTRIBUTED_BUS_ENABLED` (existing master, default `false`) — byte-identical behavior when off.
- Single-writer invariant: the Mac NEVER writes the FSM ledger; Body mode emits signals only.
- Loop safety at the Trinity layer: origin tagging + never re-forward a non-local-origin event (the Stage-1 reflection-storm class, one layer up).
- ASCII-only in generated shell/startup content.
- Commit per task, named files only (never `git add -A`).

---

### Task 1: `TrinityBusBridge` — TrinityEventBus ↔ StreamEventBroker adapter

**Files:**
- Create: `backend/core/ouroboros/governance/transport/trinity_bus_bridge.py`
- Test: `tests/governance/transport/test_trinity_bus_bridge.py`

**Interfaces:**
- Consumes: `TrinityEventBus.publish_raw(topic, data, ...) -> str` + `subscribe(pattern, handler) -> str` (`trinity_event_bus.py:1006/1060`, both async); `StreamEventBroker.publish(event_type, op_id, payload) -> Optional[str]` + `subscribe() -> _Subscriber` (queue-based).
- Produces: `class TrinityBusBridge` with `async def start(self) -> None`, `async def stop(self) -> None`, constructor `TrinityBusBridge(trinity_bus, broker, *, outbound_topics: List[str], source_id: str)`. Wire encoding: broker events of `event_type="task_started"` (a valid broker type) with `op_id=f"trinity:{topic}"` and `payload={"topic": topic, "data": <TrinityEvent.payload>, "origin": source_id}`. Constant `TRINITY_OP_PREFIX = "trinity:"`.

Semantics:
- **Outbound** (Trinity → broker): subscribe the trinity bus with each pattern in `outbound_topics`; on event, SKIP if `event.metadata.get("bridge_origin")` is set and ≠ `source_id` (never re-forward what we imported); else `broker.publish("task_started", "trinity:"+topic, {"topic":..., "data": event.payload, "origin": source_id})`.
- **Inbound** (broker → Trinity): a task drains a broker subscriber queue; for events whose `op_id.startswith("trinity:")` and `payload["origin"] != source_id`, republish into the trinity bus via `publish_raw(topic, data, ...)` **with `metadata={"bridge_origin": payload["origin"]}`** so outbound never bounces it back. (`publish_raw` has no metadata param — build a `TrinityEvent` directly with `metadata={"bridge_origin": ...}` and call `bus.publish(event)`.)
- TrinityEventBus fingerprint dedup (publish-side, `trinity_event_bus.py:970-982`) is defense-in-depth, not the primary loop guard.

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/transport/test_trinity_bus_bridge.py
# -*- coding: utf-8 -*-
"""TrinityBusBridge: topic-allowlisted mirroring with origin-tagged loop safety.

Two REAL TrinityEventBus instances linked through two REAL StreamEventBrokers
and an in-proc pump (the WS bridge's proven contract) -- publish on one side
is observed by subscribers on the other, exactly once, allowlist enforced.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from backend.core.ouroboros.governance.ide_observability_stream import (
    StreamEventBroker,
)
from backend.core.ouroboros.governance.transport.trinity_bus_bridge import (
    TRINITY_OP_PREFIX,
    TrinityBusBridge,
)
from backend.core.trinity_event_bus import TrinityEventBus, RepoType


async def _mk_bus() -> TrinityEventBus:
    return await TrinityEventBus.create(local_repo=RepoType.JARVIS)


async def _pump(src: StreamEventBroker, dst: StreamEventBroker, stop: asyncio.Event) -> None:
    """In-proc stand-in for the WS pair: mirror trinity-prefixed broker events
    once, by event id (the real wire dedups + suppresses reflections)."""
    sub = src.subscribe()
    seen: set = set()
    while not stop.is_set():
        try:
            ev = await asyncio.wait_for(sub.queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue
        if not ev.op_id.startswith(TRINITY_OP_PREFIX) or ev.event_id in seen:
            continue
        seen.add(ev.event_id)
        dst.publish(ev.event_type, ev.op_id, dict(ev.payload))


def test_publish_crosses_once_and_only_allowlisted_topics():
    async def scenario():
        mac_bus, brain_bus = await _mk_bus(), await _mk_bus()
        mac_broker, brain_broker = StreamEventBroker(), StreamEventBroker()
        stop = asyncio.Event()
        pumps = [asyncio.ensure_future(_pump(mac_broker, brain_broker, stop)),
                 asyncio.ensure_future(_pump(brain_broker, mac_broker, stop))]
        mac = TrinityBusBridge(mac_bus, mac_broker,
                               outbound_topics=["intake.remote_signal.*"],
                               source_id="mac")
        brain = TrinityBusBridge(brain_bus, brain_broker,
                                 outbound_topics=["actuation.*"],
                                 source_id="brain")
        await mac.start(); await brain.start()

        got: List[Dict[str, Any]] = []

        async def handler(ev):
            got.append({"topic": ev.topic, "payload": dict(ev.payload)})

        await brain_bus.subscribe("intake.remote_signal.*", handler)
        await mac_bus.publish_raw("intake.remote_signal.voice", {"k": 1})
        await mac_bus.publish_raw("fs.changed.src", {"k": 2})  # NOT allowlisted
        await asyncio.sleep(1.0)

        stop.set()
        for p in pumps:
            p.cancel()
        await mac.stop(); await brain.stop()
        return got

    got = asyncio.get_event_loop().run_until_complete(scenario())
    assert len(got) == 1, f"exactly the allowlisted topic, exactly once: {got!r}"
    assert got[0]["topic"] == "intake.remote_signal.voice"
    assert got[0]["payload"]["k"] == 1


def test_no_ping_pong_amplification_at_trinity_layer():
    """An imported event must NEVER be re-forwarded even when its topic matches
    the local outbound allowlist (the Stage-1 reflection-storm class)."""
    async def scenario():
        mac_bus, brain_bus = await _mk_bus(), await _mk_bus()
        mac_broker, brain_broker = StreamEventBroker(), StreamEventBroker()
        stop = asyncio.Event()
        pumps = [asyncio.ensure_future(_pump(mac_broker, brain_broker, stop)),
                 asyncio.ensure_future(_pump(brain_broker, mac_broker, stop))]
        # SAME topic allowlisted on BOTH sides -- the storm setup.
        mac = TrinityBusBridge(mac_bus, mac_broker,
                               outbound_topics=["shared.topic"], source_id="mac")
        brain = TrinityBusBridge(brain_bus, brain_broker,
                                 outbound_topics=["shared.topic"], source_id="brain")
        await mac.start(); await brain.start()

        await mac_bus.publish_raw("shared.topic", {"n": 1})
        await asyncio.sleep(1.5)  # long enough for any storm

        mac_n = len([e for e in mac_broker.recent_history(limit=500)
                     if e.op_id.startswith(TRINITY_OP_PREFIX)])
        brain_n = len([e for e in brain_broker.recent_history(limit=500)
                       if e.op_id.startswith(TRINITY_OP_PREFIX)])
        stop.set()
        for p in pumps:
            p.cancel()
        await mac.stop(); await brain.stop()
        return mac_n, brain_n

    mac_n, brain_n = asyncio.get_event_loop().run_until_complete(scenario())
    assert mac_n == 1, f"mac broker must hold exactly the original: {mac_n}"
    assert brain_n == 1, f"brain broker must hold exactly the mirror: {brain_n}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/transport/test_trinity_bus_bridge.py -x -q`
Expected: FAIL with `ModuleNotFoundError: ... trinity_bus_bridge`

- [ ] **Step 3: Implement `trinity_bus_bridge.py`**

```python
"""TrinityBusBridge -- the Stage-2 adapter the Stage-0 docstring promised.

Mirrors allowlisted TrinityEventBus topics onto a StreamEventBroker (which the
Stage-0/1 DistributedEventBus carries across the mTLS WS) and republishes
imported events into the local TrinityEventBus. Loop safety is ORIGIN-BASED:
imported events carry ``metadata.bridge_origin`` and are never re-forwarded --
the Stage-1 reflection-storm class, closed at this layer by construction.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TRINITY_OP_PREFIX = "trinity:"
_BROKER_EVENT_TYPE = "task_started"  # a valid StreamEventBroker type
_META_ORIGIN = "bridge_origin"


class TrinityBusBridge:
    def __init__(self, trinity_bus: Any, broker: Any, *,
                 outbound_topics: List[str], source_id: str) -> None:
        self._bus = trinity_bus
        self._broker = broker
        self._outbound_topics = list(outbound_topics)
        self._source_id = source_id
        self._sub_ids: List[str] = []
        self._drain_task: Optional[asyncio.Task] = None
        self._broker_sub = None

    async def start(self) -> None:
        for pattern in self._outbound_topics:
            sid = await self._bus.subscribe(pattern, self._on_outbound)
            self._sub_ids.append(sid)
        self._broker_sub = self._broker.subscribe()
        if self._broker_sub is None:
            raise RuntimeError("broker subscriber cap exceeded")
        self._drain_task = asyncio.ensure_future(self._drain_inbound())

    async def stop(self) -> None:
        for sid in self._sub_ids:
            try:
                await self._bus.unsubscribe(sid)
            except Exception:  # noqa: BLE001
                pass
        self._sub_ids.clear()
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._drain_task = None
        if self._broker_sub is not None:
            try:
                self._broker.unsubscribe(self._broker_sub)
            except Exception:  # noqa: BLE001
                pass
            self._broker_sub = None

    async def _on_outbound(self, event: Any) -> None:
        origin = (getattr(event, "metadata", None) or {}).get(_META_ORIGIN)
        if origin and origin != self._source_id:
            return  # imported -- NEVER re-forward (loop safety)
        try:
            self._broker.publish(
                _BROKER_EVENT_TYPE,
                TRINITY_OP_PREFIX + str(event.topic),
                {"topic": str(event.topic),
                 "data": dict(event.payload or {}),
                 "origin": self._source_id},
            )
        except Exception:  # noqa: BLE001
            logger.debug("[TrinityBusBridge] outbound publish failed", exc_info=True)

    async def _drain_inbound(self) -> None:
        from backend.core.trinity_event_bus import TrinityEvent  # noqa: PLC0415

        while True:
            ev = await self._broker_sub.queue.get()
            op_id = getattr(ev, "op_id", "") or ""
            if not op_id.startswith(TRINITY_OP_PREFIX):
                continue
            payload: Dict[str, Any] = dict(getattr(ev, "payload", None) or {})
            if payload.get("origin") == self._source_id:
                continue  # our own outbound reflected by the broker -- skip
            try:
                tev = TrinityEvent(
                    topic=str(payload.get("topic", "")),
                    payload=dict(payload.get("data", {}) or {}),
                    metadata={_META_ORIGIN: str(payload.get("origin", ""))},
                )
                await self._bus.publish(tev)
            except Exception:  # noqa: BLE001
                logger.debug("[TrinityBusBridge] inbound republish failed",
                             exc_info=True)
```

NOTE for the implementer: `TrinityEvent` is a dataclass with defaulted fields (`trinity_event_bus.py:261`) — verify the constructor kwargs above against the real field list (`event_id` auto? `source`/`priority` defaults) and adjust so construction type-checks; the load-bearing part is `metadata={"bridge_origin": ...}` surviving into subscribers.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/transport/test_trinity_bus_bridge.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/transport/trinity_bus_bridge.py tests/governance/transport/test_trinity_bus_bridge.py
git commit -m "feat(stage2): TrinityBusBridge -- origin-tagged Trinity<->broker adapter (Task 1)"
```

---

### Task 2: `OrganismBusHost` — in-process mTLS WS server for the Brain organism

**Files:**
- Create: `backend/core/ouroboros/governance/transport/organism_bus_host.py`
- Modify: `backend/core/ouroboros/governance/intake/intake_layer_service.py` (boot seam, beside the EventChannelServer ownership block ~line 1052)
- Modify: `scripts/brain_bus_echo_server.py` (early-exit when `JARVIS_BRAIN_BUS_SIDECAR_ENABLED=false` — the organism host replaces the sidecar on Stage-2 nodes; default `true` keeps Stage-1 behavior)
- Test: `tests/governance/transport/test_organism_bus_host.py`

**Interfaces:**
- Consumes: `TrinityBusBridge` (Task 1 — exact constructor `TrinityBusBridge(trinity_bus, broker, *, outbound_topics, source_id)`); `DistributedEventBus(broker, cfg, role="server").register_server_routes(app)`; `build_server_ssl_context(cfg)`; `TransportConfig.from_env(role="brain-server")`; `get_trinity_event_bus()` (`trinity_event_bus.py:1332`).
- Produces: `class OrganismBusHost` with `async def start(self) -> bool` (False when master flag off / port 0 — dark), `async def stop(self) -> None`, env knobs: `JARVIS_BRAIN_OUTBOUND_TOPICS` (comma list, default `"actuation.*,telemetry.posture.*"`), existing `JARVIS_BRAIN_WS_*` family for TLS/port, `JARVIS_DISTRIBUTED_BUS_ENABLED` master. Module fn `def bus_host_enabled() -> bool`.

Boot seam (in `IntakeLayerService`, same style as its EventChannelServer ownership): when `bus_host_enabled()`, instantiate + `await host.start()` during layer start; `await host.stop()` during layer stop. Master OFF ⇒ zero new imports on the hot path (lazy import inside the guard) — byte-identical.

- [ ] **Step 1: Write the failing tests** — (a) `start()` returns False and touches nothing when master flag off (dark test, no env); (b) with flag on + TLS disabled + an ephemeral port + a REAL `StreamEventBroker`/`TrinityEventBus`, a Stage-0 `BusBridgeClient` connects over localhost and a `publish_raw("actuation.click", {...})` on the organism bus is observed on the client's broker (use the exact `_Pair`-style scaffolding from `tests/governance/transport/test_bridge_reflection_and_cursor.py`, replacing the server side with `OrganismBusHost`); (c) sidecar early-exit: `brain_bus_echo_server.main()` returns 0 immediately when `JARVIS_BRAIN_BUS_SIDECAR_ENABLED=false`.
- [ ] **Step 2: Run to verify FAIL** — `python3 -m pytest tests/governance/transport/test_organism_bus_host.py -x -q` → `ModuleNotFoundError`.
- [ ] **Step 3: Implement** — `OrganismBusHost.start()`: resolve cfg; build ssl ctx (`tls_enabled` and ctx None ⇒ refuse, return False, log); own `web.Application` + `AppRunner` + `TCPSite(host=cfg.host or "0.0.0.0", port=cfg.port, ssl_context=ctx)`; `DistributedEventBus(broker, cfg, role="server").register_server_routes(app)`; `bridge = TrinityBusBridge(await get_trinity_event_bus(), broker, outbound_topics=<env>, source_id=cfg.source_id)`; `await bridge.start()`. `stop()` unwinds in reverse, never raises. Sidecar edit: first lines of `main()` — `if os.environ.get("JARVIS_BRAIN_BUS_SIDECAR_ENABLED", "true").strip().lower() in ("0","false","no","off"): return 0`.
- [ ] **Step 4: Run to verify PASS** — `python3 -m pytest tests/governance/transport/test_organism_bus_host.py tests/governance/transport/ -q` → all green (incl. Stage-0/1 suites unregressed).
- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/transport/organism_bus_host.py backend/core/ouroboros/governance/intake/intake_layer_service.py scripts/brain_bus_echo_server.py tests/governance/transport/test_organism_bus_host.py
git commit -m "feat(stage2): OrganismBusHost -- organism-owned mTLS WS bus + sidecar handoff (Task 2)"
```

---

### Task 3: Remote intake path — `RemoteIntakeRouter` shim (Body) + `RemoteIntakeBridge` (Brain)

**Files:**
- Create: `backend/core/ouroboros/governance/intake/remote_intake.py` (both classes — they are two halves of one wire contract and must stay in one file)
- Test: `tests/governance/intake/test_remote_intake.py`

**Interfaces:**
- Consumes: `IntentEnvelope.to_dict()/from_dict()` (`intent_envelope.py:249/270`); `UnifiedIntakeRouter.ingest(envelope) -> str` (`unified_intake_router.py:1096`); `TrinityEventBus.publish_raw/subscribe`.
- Produces:
  - `TOPIC_REMOTE_SIGNAL = "intake.remote_signal.body"`.
  - `class RemoteIntakeRouter` — duck-types the ONE method Body sensors call: `async def ingest(self, envelope) -> str` — publishes `publish_raw(TOPIC_REMOTE_SIGNAL, envelope.to_dict())` on the local (Mac) trinity bus and returns `"enqueued"` (fire-and-forget; the Brain's real router owns dedup/backpressure). Constructor: `RemoteIntakeRouter(trinity_bus)`.
  - `class RemoteIntakeBridge` — Brain side: `RemoteIntakeBridge(trinity_bus, router)`; `async def start(self)` subscribes `TOPIC_REMOTE_SIGNAL`, handler does `IntentEnvelope.from_dict(event.payload)` → `await router.ingest(envelope)`; malformed payloads log-and-drop (fail-soft, never crash the bus); `async def stop(self)` unsubscribes.

Boot seam for the bridge: inside the Task-2 `OrganismBusHost.start()` (the Brain host is exactly where remote signals should land) — construct with the organism's real router (injected; `IntakeLayerService` passes its router when building the host).

- [ ] **Step 1: Write the failing tests** — (a) round-trip: real Mac-side `TrinityEventBus` + shim; real Brain-side `TrinityEventBus` + a recording fake router (`async def ingest(env): calls.append(env); return "enqueued"`); link the two buses with Task-1 `TrinityBusBridge` pair + in-proc pump (reuse the `_pump` scaffolding from Task 1's test file verbatim); `make_envelope(...)` a real envelope, `await shim.ingest(env)`; assert the fake router received an `IntentEnvelope` whose `to_dict()` equals the original's. (b) replay-dedup honesty: deliver the SAME payload twice to `RemoteIntakeBridge` directly; assert router.ingest called twice with equal `dedup_key` (the REAL router's `_is_duplicate` owns dedup — this test documents the contract, not re-implements it). (c) malformed payload (`{"garbage": True}`) → no raise, router not called.
- [ ] **Step 2: Run to verify FAIL** — `python3 -m pytest tests/governance/intake/test_remote_intake.py -x -q` → `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `remote_intake.py` per the Produces block (≈70 lines; both classes fail-soft, `from __future__ import annotations`, no new deps). Wire the bridge into `OrganismBusHost` (router param, optional — None skips).
- [ ] **Step 4: Run to verify PASS** — `python3 -m pytest tests/governance/intake/test_remote_intake.py tests/governance/transport/ -q` → green.
- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/intake/remote_intake.py backend/core/ouroboros/governance/transport/organism_bus_host.py tests/governance/intake/test_remote_intake.py
git commit -m "feat(stage2): remote intake path -- Body shim + Brain bridge over the trinity bus (Task 3)"
```

---

### Task 4: Body-mode driver — `scripts/run_body_mode.py`

**Files:**
- Create: `scripts/run_body_mode.py`
- Test: `tests/infra/test_run_body_mode.py`

**Interfaces:**
- Consumes: `discover_brain_endpoint()` (`brain_discovery.py`); `DistributedEventBus(broker, cfg, role="client").start_client(url)` + `connected` gate (Stage-1 pattern from `ignite_brain_vm.py::_await_connected`); `TrinityBusBridge` (Task 1); `RemoteIntakeRouter` (Task 3); `VoiceCommandSensor(router=...)` (`intake_layer_service.py:563` construction pattern); `ControlPlaneWatchdog` (`control_plane_watchdog.py` — `get_default_watchdog()`).
- Produces: CLI `python3 scripts/run_body_mode.py [--inject-test-signal N] [--duration-s S] [--dry-run]`. Env: the `JARVIS_BRAIN_WS_*` client family + `JARVIS_BRAIN_MTLS_DIR` conventions from the Stage-1 ignition. Exit codes: 0 acceptance-clean, 2 discovery failed, 3 connect-gate timeout.

Behavior (composition only — every piece exists):
1. Discover the Brain WS URL (fail → exit 2, log "Brain offline").
2. Local `TrinityEventBus` + client `DistributedEventBus` + connect gate (30s, env `JARVIS_BRAIN_CONNECT_GATE_S`) → exit 3 on timeout.
3. `TrinityBusBridge(bus, broker, outbound_topics=["intake.remote_signal.*", "console.*"], source_id="mac-body")`.
4. `RemoteIntakeRouter(bus)` as the router seam; construct `VoiceCommandSensor(router=shim)` (real voice stays available); when `--inject-test-signal N`, build N deterministic `make_envelope(source="body_test", description="stage2 acceptance signal %d", ...)` and `await shim.ingest(...)` them (the deterministic acceptance path).
5. Arm `ControlPlaneWatchdog`; every 10s log `[BodyMode] lag_events=%d worst_ms=%.1f connected=%s` from `watchdog.lag_event_count` / `recent_lag_records()`.
6. `--duration-s` elapsed → clean stop → summary line `[BodyMode] SUMMARY lag_events=N worst_ms=X signals_sent=K` → exit 0.

- [ ] **Step 1: Write the failing tests** — pure-logic (module loads via `importlib.util.spec_from_file_location`, the established `tests/infra` pattern): (a) `--dry-run` prints the plan, touches no network (inject fake discover seam, assert not called); (b) injected-seam run: fake discover returns a URL, fake bus/bridge/shim record calls; `--inject-test-signal 3` → exactly 3 `shim.ingest` calls with distinct `dedup_key`s; (c) discovery-fail path exits 2 with the "Brain offline" log.
- [ ] **Step 2: Run to verify FAIL** — `python3 -m pytest tests/infra/test_run_body_mode.py -x -q`.
- [ ] **Step 3: Implement the driver** (injectable seams exactly like `BrainIgnitionDriver.__init__` — `discover_fn`, `bus_factory`, `sensor_factory` kwargs with live defaults resolved lazily).
- [ ] **Step 4: Run to verify PASS** — `python3 -m pytest tests/infra/test_run_body_mode.py -q`.
- [ ] **Step 5: Commit**

```bash
git add scripts/run_body_mode.py tests/infra/test_run_body_mode.py
git commit -m "feat(stage2): Body-mode driver -- discovery + bus client + sensor shim + starvation census (Task 4)"
```

---

### Task 5: Live-fire acceptance — Body signal reaches the Brain FSM (operator/agent-run)

**Files:**
- Modify: `scripts/ignite_brain_vm.py` (persistent-mode env additions only: fold `JARVIS_DISTRIBUTED_BUS_ENABLED=true` is already shipped; ADD `JARVIS_BRAIN_BUS_SIDECAR_ENABLED=false` + `JARVIS_BRAIN_OUTBOUND_TOPICS` to `_brain_env_values()`'s key list)
- No other code — this is the acceptance run itself.

**Interfaces:**
- Consumes: everything above + the Stage-1 ignition env (`GCP_ZONE=us-central1-a`, `JARVIS_GCP_PROJECT=jarvis-473803`, `JARVIS_BRAIN_MTLS_DIR=$PWD/.jarvis/brain_mtls`, client `JARVIS_BRAIN_WS_TLS_*`, `JARVIS_BRAIN_WS_TLS_SERVER_HOSTNAME=jarvis-brain`, `JARVIS_BRAIN_WS_PORT=8443`).
- Produces: the Stage-2 acceptance verdict.

- [ ] **Step 1:** Add the two env keys to `_brain_env_values()`'s fold list + a one-line test in `tests/infra/test_brain_tls_delivery.py` asserting they fold when set. Commit: `git add scripts/ignite_brain_vm.py tests/infra/test_brain_tls_delivery.py && git commit -m "feat(stage2): ignition folds bus-host env to the node (Task 5 prep)"`.
- [ ] **Step 2: Provision the Brain warm-standby** — `JARVIS_BRAIN_VM_PERSISTENT=true JARVIS_BRAIN_BUS_SIDECAR_ENABLED=false <Stage-1 env> python3 scripts/ignite_brain_vm.py` (persistent ⇒ VM survives; idle-shutdown timer still guards cost). Expected: `proven` may be skipped/False if the sidecar is off — the Stage-1 exchange needed the sidecar; acceptance here is Stage-2's own (next step). What must succeed: provision + discovery binding against the ORGANISM-hosted WS (proves Task 2 live).
- [ ] **Step 3: Run Body mode** — `python3 scripts/run_body_mode.py --inject-test-signal 3 --duration-s 300`. Expected: exit 0, `[BodyMode] SUMMARY lag_events=0` (≈0 tolerated: <3 events, worst <100ms).
- [ ] **Step 4: Verify on the Brain** — `gcloud compute ssh <node> --command "grep -E 'remote_signal|body_test' /opt/trinity/jarvis/.ouroboros/sessions/*/debug.log | head"` → the 3 envelopes appear as intake ingest lines (`enqueued`/`deduplicated`), and at least one reaches CLASSIFY (op created from a Mac-originated signal = the relocation proof).
- [ ] **Step 5: Cost close-out** — either leave the warm standby (idle timer armed) or `gcloud compute instances delete <node>`; verify with `gcloud compute instances list` per intended end-state. Ledger the verdict in `.superpowers/sdd/progress.md`.

---

## Self-Review (run before execution)

1. **Spec coverage:** Stage-2 line 93 = sensors/FSM on VM (already there via Stage 1 soak; Task 2 arms its bus), Mac = Vision/Voice + console (Task 4), streaming over WS (Tasks 1-3), live soak census (Tasks 4-5). Deviation from spec letter documented: organism hosts WS on its own site, not on loopback-only EventChannelServer. WAL/partition hardening is Stage 3 (spec line 94) — deliberately out.
2. **Placeholder scan:** Tasks 2-4 steps compress test/impl detail into contracts rather than full listings — each names exact files, seams, signatures, and reuses named existing scaffolding verbatim; no TBDs.
3. **Type consistency:** `TrinityBusBridge(trinity_bus, broker, *, outbound_topics, source_id)` used identically in Tasks 1/2/4; `RemoteIntakeRouter.ingest(envelope) -> str` matches the sensors' real call shape (`await self._router.ingest(envelope)`); `TOPIC_REMOTE_SIGNAL` matches the Task-4 allowlist pattern `intake.remote_signal.*`.
