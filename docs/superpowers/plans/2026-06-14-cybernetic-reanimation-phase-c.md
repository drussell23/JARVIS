# Cybernetic Reanimation (Phase C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn the dead event-driven resilience infrastructure in `unified_supervisor.py` into a live, edge-triggered async reactive nervous system: a bus→activation dispatcher, typed pressure emitters, and 7 resilience organs registered to fire on events — all in a focused, unit-testable module, default-OFF.

**Architecture:** New module `backend/core/ouroboros/governance/resilience_reanimation.py` holds all reactive logic (importable + testable in isolation with mock bus/registry — `import unified_supervisor` is sandbox-blocked by split-brain-guard). The kernel gains one master-flag-guarded hook. Reuses `SupervisorEventBus`, `SystemServiceRegistry`, `ActivationContract` as-is.

**Tech Stack:** Python 3.11, asyncio, `ast`/`tokenize`, pytest.

**Spec:** `docs/superpowers/specs/2026-06-14-cybernetic-reanimation-design.md`

**Environment:** Work in worktree `/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/.claude/worktrees/reanimation` (branch `feature/cybernetic-reanimation`, OCA-owned → commits authorized). `import unified_supervisor` FAILS in sandbox (split-brain-guard) — the new module must NOT import `unified_supervisor` at module top level; it takes the bus/registry as injected constructor args (duck-typed), so its tests never import the kernel.

---

## File Structure

| File | Responsibility | Lifecycle |
|---|---|---|
| `backend/core/ouroboros/governance/resilience_reanimation.py` | Dispatcher + emitter + organ adapters + `ReanimationLayer` façade. No top-level `unified_supervisor` import. | Created C.1, extended C.2-C.4 |
| `tests/governance/test_resilience_reanimation.py` | Unit + integration tests (mock/fake bus & registry) | Created C.1, extended |
| `unified_supervisor.py` | `SupervisorEventType` += 3 values (C.2); kernel hook (C.1) | Modified |
| `docs/superpowers/specs/2026-06-14-cybernetic-reanimation-design.md` | Spec (committed with impl in final PR) | Present |

**Key design rule:** the module is provider-agnostic. The dispatcher accepts any object exposing `subscribe(handler)` (bus) and `activate_service(name)`/an event-driven-descriptor iterator (registry). This keeps it unit-testable with fakes.

---

## Task C.1: EventActivationDispatcher + module skeleton + kernel hook

**Files:**
- Create: `backend/core/ouroboros/governance/resilience_reanimation.py`
- Create: `tests/governance/test_resilience_reanimation.py`
- Modify: `unified_supervisor.py` (guarded kernel hook + `iter_event_driven()` accessor on `SystemServiceRegistry`)

- [ ] **Step 1: Write failing tests for the dispatcher**

Create `tests/governance/test_resilience_reanimation.py`:

```python
"""Unit tests for the Cybernetic Reanimation layer (Phase C).

These import ONLY the standalone module — never unified_supervisor (which is
sandbox-blocked by split_brain_guard). Bus + registry are fakes.
"""
import asyncio
import pytest

from backend.core.ouroboros.governance.resilience_reanimation import (
    EventActivationDispatcher,
)


class FakeBus:
    def __init__(self):
        self.handlers = []
    def subscribe(self, handler):
        self.handlers.append(handler)
    async def fire(self, event):
        for h in self.handlers:
            r = h(event)
            if asyncio.iscoroutine(r):
                await r


class FakeEvent:
    def __init__(self, type_value):
        self.event_type = type(self).Type(type_value)
    class Type:
        def __init__(self, value): self.value = value


class FakeDescriptor:
    def __init__(self, name, trigger_events):
        self.name = name
        self.activation_contract = type("C", (), {"trigger_events": trigger_events})()


class FakeRegistry:
    def __init__(self, descriptors):
        self._descs = descriptors
        self.activated = []
        self.fail_on = set()
    def iter_event_driven(self):
        return list(self._descs)
    async def activate_service(self, name):
        if name in self.fail_on:
            raise RuntimeError(f"boom:{name}")
        self.activated.append(name)
        return True


@pytest.mark.asyncio
async def test_dispatch_activates_matching_service():
    bus = FakeBus()
    reg = FakeRegistry([FakeDescriptor("grace", ["resource_pressure"])])
    d = EventActivationDispatcher(bus, reg)
    d.start()
    await bus.fire(FakeEvent("resource_pressure"))
    assert reg.activated == ["grace"]


@pytest.mark.asyncio
async def test_non_matching_event_does_not_activate():
    bus = FakeBus()
    reg = FakeRegistry([FakeDescriptor("grace", ["resource_pressure"])])
    EventActivationDispatcher(bus, reg).start()
    await bus.fire(FakeEvent("phase_start"))
    assert reg.activated == []


@pytest.mark.asyncio
async def test_one_failing_activation_does_not_block_others():
    bus = FakeBus()
    reg = FakeRegistry([
        FakeDescriptor("bad", ["resource_pressure"]),
        FakeDescriptor("good", ["resource_pressure"]),
    ])
    reg.fail_on = {"bad"}
    EventActivationDispatcher(bus, reg).start()
    await bus.fire(FakeEvent("resource_pressure"))
    assert reg.activated == ["good"]  # bad failed, good still ran
```

- [ ] **Step 2: Run tests — verify they FAIL (module missing)**

Run: `cd <worktree> && python3 -m pytest tests/governance/test_resilience_reanimation.py -q`
Expected: collection/import error — module `resilience_reanimation` does not exist yet.

- [ ] **Step 3: Implement the module + dispatcher (minimal)**

Create `backend/core/ouroboros/governance/resilience_reanimation.py`:

```python
"""Cybernetic Reanimation (Phase C) — bus→activation bridge + pressure emitters.

Standalone + injectable: never imports unified_supervisor at module scope, so it
is unit-testable in environments where the kernel import is blocked. The kernel
constructs the layer behind the JARVIS_RESILIENCE_REANIMATION_ENABLED flag and
passes its live SupervisorEventBus + SystemServiceRegistry.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger("resilience_reanimation")


class EventActivationDispatcher:
    """Subscribes to the supervisor event bus and activates registry services
    whose ActivationContract.trigger_events match the emitted event type.

    Adds NO new policy — the registry's gates (dependency/budget/backoff/rate)
    remain authoritative. This is the missing wire, nothing more.
    """

    def __init__(self, event_bus: Any, service_registry: Any) -> None:
        self._bus = event_bus
        self._registry = service_registry
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._bus.subscribe(self._on_event)
        self._started = True
        logger.info("[Reanimation] dispatcher subscribed to event bus")

    async def _on_event(self, event: Any) -> None:
        try:
            etype = event.event_type.value
        except Exception:  # noqa: BLE001 — malformed event, ignore
            return
        try:
            descriptors = list(self._registry.iter_event_driven())
        except Exception as err:  # noqa: BLE001 — fail-soft
            logger.warning("[Reanimation] registry iteration failed: %r", err)
            return
        activated = []
        for desc in descriptors:
            contract = getattr(desc, "activation_contract", None)
            triggers = getattr(contract, "trigger_events", None) or []
            if etype not in triggers:
                continue
            name = getattr(desc, "name", "")
            try:
                ok = await self._registry.activate_service(name)
                if ok:
                    activated.append(name)
            except Exception as err:  # noqa: BLE001 — isolate per service
                logger.warning(
                    "[Reanimation] activate_service(%s) failed: %r", name, err
                )
        if activated:
            logger.info(
                "[Reanimation] event=%s activated=%s", etype, activated
            )
```

- [ ] **Step 4: Run tests — verify PASS**

Run: `python3 -m pytest tests/governance/test_resilience_reanimation.py -q`
Expected: 3 passed.

- [ ] **Step 5: Add `iter_event_driven()` accessor to `SystemServiceRegistry`**

Read `unified_supervisor.py` around `class SystemServiceRegistry` (~line 13809) and its `register`/descriptor storage. Add a minimal **read-only** method that yields registered `ServiceDescriptor`s whose `activation_mode == "event_driven"` (or that carry an `activation_contract`). Pure-add; no behavior change to existing methods. Confirm `ServiceDescriptor` exposes `.name` and `.activation_contract` (read the dataclass; if the field is named differently, match it and update the test's `FakeDescriptor` to the real attribute name).

- [ ] **Step 6: Add the guarded kernel hook**

Read the `JarvisSystemKernel` boot sequence where `self._service_registry` is created and the event bus is available. Add, behind a flag helper:

```python
# near other env-flag helpers
def _reanimation_enabled() -> bool:
    import os
    return os.getenv("JARVIS_RESILIENCE_REANIMATION_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
```
and in boot (after registry + bus exist):
```python
if _reanimation_enabled():
    try:
        from backend.core.ouroboros.governance.resilience_reanimation import EventActivationDispatcher
        self._reanimation_dispatcher = EventActivationDispatcher(get_event_bus(), self._service_registry)
        self._reanimation_dispatcher.start()
    except Exception as _e:  # fail-soft — reanimation must never break boot
        self._logger.warning(f"[Reanimation] disabled (init failed): {_e!r}")
```
OFF path: the `if` is false → nothing constructed → byte-identical.

- [ ] **Step 7: Verify supervisor still parses + guard/governance still green**

Run:
```bash
python3 -c "import ast; ast.parse(open('unified_supervisor.py').read()); print('PARSE OK')"
python3 -m pytest tests/unit/core/test_no_shadowed_definitions.py -q
python3 -m pytest tests/governance/test_resilience_reanimation.py -q
```
Expected: PARSE OK; shadow guard 2 passed; reanimation tests pass.

- [ ] **Step 8: Commit**

```bash
git add backend/core/ouroboros/governance/resilience_reanimation.py tests/governance/test_resilience_reanimation.py unified_supervisor.py
git commit -m "feat(reanimation): C.1 — EventActivationDispatcher bridge + guarded kernel hook (default-off)"
```

---

## Task C.2: SupervisorEventType extension + PressureSignalEmitter

**Files:**
- Modify: `unified_supervisor.py` (`SupervisorEventType` += 3 values)
- Modify: `backend/core/ouroboros/governance/resilience_reanimation.py` (+ `PressureSignalEmitter`)
- Modify: `tests/governance/test_resilience_reanimation.py` (+ emitter tests)

- [ ] **Step 1: Add 3 enum values** to `SupervisorEventType` (additive):
```python
    RESOURCE_PRESSURE = "resource_pressure"
    ANOMALY_DETECTED = "anomaly_detected"
    COMPONENT_DEGRADED = "component_degraded"
```
Verify `ast.parse` OK.

- [ ] **Step 2: Write failing emitter tests** (in the test file). The emitter is injectable: it takes a `sampler` callable returning a dict `{signal: level}` and an `emit(event_type_value, payload)` callable, plus a thresholds dict. Edge-trigger semantics:

```python
from backend.core.ouroboros.governance.resilience_reanimation import PressureSignalEmitter

@pytest.mark.asyncio
async def test_emitter_edge_triggers_once_on_crossing():
    emitted = []
    levels = {"mem": [0.5, 0.95, 0.96, 0.4]}   # below, cross, stay, drop
    seq = iter(levels["mem"])
    def sampler(): return {"mem": next(seq)}
    em = PressureSignalEmitter(
        sampler=sampler,
        emit=lambda etype, payload: emitted.append((etype, payload)),
        thresholds={"mem": 0.9},
        signal_event={"mem": "resource_pressure"},
    )
    for _ in range(4):
        await em.tick()
    # crossing 0.5->0.95 emits once; 0.95->0.96 (stay above) no emit; drop resets
    assert [e[0] for e in emitted] == ["resource_pressure"]

@pytest.mark.asyncio
async def test_emitter_reemits_after_drop_then_recross():
    emitted = []
    seq = iter([0.95, 0.4, 0.95])
    em = PressureSignalEmitter(
        sampler=lambda: {"mem": next(seq)},
        emit=lambda etype, payload: emitted.append(etype),
        thresholds={"mem": 0.9},
        signal_event={"mem": "resource_pressure"},
    )
    for _ in range(3):
        await em.tick()
    assert emitted == ["resource_pressure", "resource_pressure"]

@pytest.mark.asyncio
async def test_emitter_failsoft_on_sampler_error():
    def sampler(): raise RuntimeError("probe down")
    em = PressureSignalEmitter(sampler=sampler, emit=lambda *a: None,
                               thresholds={"mem": 0.9}, signal_event={"mem": "resource_pressure"})
    await em.tick()  # must not raise
```

- [ ] **Step 3: Run — verify FAIL** (PressureSignalEmitter missing).

- [ ] **Step 4: Implement `PressureSignalEmitter`** in the module:

```python
class PressureSignalEmitter:
    """Edge-triggered pressure sampler. Emits a typed event only when a signal
    transitions from below to above its threshold (never every tick). Fail-soft.
    """

    def __init__(self, sampler, emit, thresholds, signal_event):
        self._sampler = sampler          # () -> {signal: level}
        self._emit = emit                # (event_type_value, payload) -> None
        self._thresholds = dict(thresholds)
        self._signal_event = dict(signal_event)
        self._above = {}                 # signal -> bool (last state)

    async def tick(self) -> None:
        try:
            sample = self._sampler() or {}
        except Exception as err:  # noqa: BLE001 — fail-soft
            logger.warning("[Reanimation] pressure sample failed: %r", err)
            return
        for signal, level in sample.items():
            thr = self._thresholds.get(signal)
            if thr is None:
                continue
            now_above = level >= thr
            was_above = self._above.get(signal, False)
            if now_above and not was_above:
                etype = self._signal_event.get(signal)
                if etype:
                    try:
                        self._emit(etype, {"signal": signal, "level": level})
                    except Exception as err:  # noqa: BLE001
                        logger.warning("[Reanimation] emit failed: %r", err)
            self._above[signal] = now_above
```

- [ ] **Step 5: Run — verify PASS** (all reanimation tests). **Step 6: Commit** `feat(reanimation): C.2 — typed pressure events + edge-triggered PressureSignalEmitter`.

---

## Task C.3: Register the 7 organs with ActivationContracts

**Files:** Modify `resilience_reanimation.py` (organ adapters + `ReanimationLayer` registration helper), `unified_supervisor.py` (wire emitter sampler to real `DynamicRAMMonitor`/`psutil` + register organs via registry), tests.

- [ ] **Step 1:** Read each organ's real API in `unified_supervisor.py`: `GracefulDegradationManager`, `LoadSheddingController` (`record_load`/`should_accept`), `AutoScalingController` (`record_metrics`/`evaluate`/`add_scale_callback`), `AnomalyDetector` (`record_observation`/`register_handler`), `ProcessHealthPredictor` (`record_metrics`), `SelfHealingOrchestrator` (`check_and_remediate`/`register_handler`), `AdvancedCircuitBreaker`. Note exact signatures.
- [ ] **Step 2:** Write adapter unit tests (mock organ objects; assert the adapter maps an event payload → the correct organ method call). One test per organ.
- [ ] **Step 3:** Implement adapters in the module (each adapter wraps an organ + exposes an `async on_event(payload)` calling the organ's API). Build a `ReanimationLayer` that registers each organ as a `ServiceDescriptor` with an `ActivationContract(trigger_events=[...])` via the injected registry, gated by `JARVIS_REANIMATE_<ORGAN>_ENABLED`.
- [ ] **Step 4:** Wire the real sampler in the kernel hook: sampler reads `DynamicRAMMonitor`/`psutil`; thresholds from `JARVIS_PRESSURE_*` env. Register the emitter as a `KernelBackgroundTaskRegistry` task (interval `JARVIS_PRESSURE_SAMPLE_INTERVAL_S`).
- [ ] **Step 5:** Verify (`ast.parse`, shadow guard, all reanimation tests). **Step 6: Commit** `feat(reanimation): C.3 — register 7 resilience organs with event-driven ActivationContracts`.

---

## Task C.4: Feedback emitters + integration proof + flags + finalize

**Files:** Modify module (feedback wiring), `unified_supervisor.py` (AnomalyDetector→`anomaly_detected`, breaker→`component_degraded`; FlagRegistry seeds), tests.

- [ ] **Step 1: Write the headline integration test** (module-level, real `SupervisorEventBus` if importable standalone — else a faithful FakeBus that delivers async): construct `ReanimationLayer` with a fake registry holding the 7 contracts; emit a synthetic `RESOURCE_PRESSURE`; assert `GracefulDegradationManager`/`LoadSheddingController`/`AutoScalingController` activate. Add an OFF test: layer factory with master flag false returns a no-op (no subscription, no registration).
- [ ] **Step 2:** Implement feedback: `AnomalyDetector` adapter, on anomaly, calls `emit("anomaly_detected", ...)`; `AdvancedCircuitBreaker` `on_state_change(OPEN)` calls `emit("component_degraded", ...)`. Wire `ProcessHealthPredictor`→`SelfHealingOrchestrator` from the existing `_health_monitor_loop` (reuse; pass health score; on low score emit `component_degraded`).
- [ ] **Step 3:** Register all flags in `FlagRegistry`: master + per-organ + thresholds + interval.
- [ ] **Step 4: Full verification:**
```bash
python3 -c "import ast; ast.parse(open('unified_supervisor.py').read()); print('PARSE OK')"
python3 -m pytest tests/unit/core/test_no_shadowed_definitions.py tests/governance/test_resilience_reanimation.py -q
python3 -m pytest tests/unit/backend/test_enterprise_organ_governance.py tests/unit/supervisor/test_higher_functions_protocol.py --collect-only -q
```
Expected: PARSE OK; reanimation + shadow-guard green; governance collection 0 errors.
- [ ] **Step 5: Commit spec + impl together** then push + PR:
```bash
git add -A
git commit -m "feat(reanimation): C.4 — closed-loop feedback + integration proof + flags; Phase C complete"
git push -u origin feature/cybernetic-reanimation
gh pr create --title "Cybernetic Reanimation (Phase C): event-driven resilience matrix" --body "<spec summary + test plan>"
```

---

## Self-Review
- **Spec coverage:** bridge → C.1; pressure emitters → C.2; muscle registration → C.3; feedback + integration proof + flags → C.4. All §4 components mapped.
- **Testability:** module never imports `unified_supervisor` at top level → all tests run in sandbox.
- **OFF byte-identical:** kernel hook flag-guarded; verified by inspection + OFF test.
- **No placeholders in C.1/C.2** (complete code). C.3/C.4 require reading real organ APIs first (instructed) — adapters are thin, signatures resolved at implementation.
- **No hardcoding:** all intervals/thresholds/flags via env + FlagRegistry.
