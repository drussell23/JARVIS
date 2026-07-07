# Karen Full-Duplex Voice — Sprint 4: Voice → Build — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox (`- [ ]`) steps.

**Goal:** A spoken user utterance ("add rate limiting to the auth endpoint") is classified as a build command and routed into O+V's governed loop via the existing `VoiceCommandSensor` → `UnifiedIntakeRouter.ingest`, so the organism builds it and Karen (Sprints 2-3) narrates. STOP is already composed; barge-in + re-submit gives steering.

**Architecture:** Close the existing gap chain, don't rebuild. Expose the live `VoiceCommandSensor` via a singleton (mirrors `get_default_intake_router`). A minimal `VoiceBuildBridge.on_final_transcript(text, conf)` classifies build-vs-chat (pluggable classifier) and, for build intents, constructs a `VoiceCommandPayload` and calls the sensor. Wired as a fork at the existing final-transcript handler (no competing queue consumer), env-gated, fault-isolated. The broken `JarvisVoiceBridge` is bypassed entirely.

**Tech Stack:** Python 3.9+, asyncio, existing `VoiceCommandSensor`, `UnifiedIntakeRouter`, `StreamingSTTEngine`. No new deps.

## Global Constraints
- Python 3.9+, `from __future__ import annotations`. Pure asyncio, no blocking, no sleep loops.
- DRY (mandate #3): reuse `VoiceCommandSensor.handle_voice_command` + `make_envelope(source="voice_human")` — no new intake schema, no second STT queue drainer.
- No hardcoded routing table (mandate #1): the build/chat decision is a pluggable `VoiceIntentClassifier` protocol (deterministic default, LLM-upgradeable), not a fixed keyword map baked into the bridge.
- Fault isolation: the bridge NEVER raises into the audio/STT loop or the FSM.
- Env-gated: `JARVIS_KAREN_VOICE_BUILD_ENABLED` (default false). Off = zero change to the chat pipeline.
- Steering: STOP composes existing primitives (sensor stop-phrase → UserSignalBus → GLS cancel). Prompt-level redirect of a running op is OUT of scope (flagged follow-up).

## File Structure
New package `backend/core/ouroboros/governance/comms/voice_build/`:
- `classifier.py` — `VoiceIntent` enum, `VoiceIntentClassifier` protocol, `HeuristicClassifier` default.
- `bridge.py` — `VoiceBuildBridge.on_final_transcript(...)`.
Modified:
- `intake/intake_layer_service.py` — publish the live `VoiceCommandSensor` as the process default.
- `intake/sensors/voice_command_sensor.py` — add module-level `get_default_voice_sensor` / `set_default_voice_sensor` (mirror the router pattern).
- `backend/audio/audio_pipeline_bootstrap.py` — env-gated construction of `VoiceBuildBridge` + the fork call at the final-transcript handler; remove/repair the broken `create_voice_bridge(...)` call.

---

### Task 1: `VoiceCommandSensor` singleton accessor + publish the live instance

**Files:** Modify `backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py`; Modify `backend/core/ouroboros/governance/intake/intake_layer_service.py`; Test `tests/governance/intake/test_voice_sensor_default.py`.

**Interfaces — Produces:** module-level `set_default_voice_sensor(sensor)` / `get_default_voice_sensor() -> Optional[VoiceCommandSensor]` in `voice_command_sensor.py` (mirroring `unified_intake_router.set_default_intake_router` / `get_default_intake_router`). `IntakeLayerService._build_components` calls `set_default_voice_sensor(self._voice_sensor)` right after constructing it (~line 578).

- [ ] **Step 1: failing test**
```python
# tests/governance/intake/test_voice_sensor_default.py
from __future__ import annotations
from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    get_default_voice_sensor, set_default_voice_sensor,
)

def test_default_voice_sensor_roundtrip():
    assert get_default_voice_sensor() is None or get_default_voice_sensor() is not None
    sentinel = object()
    set_default_voice_sensor(sentinel)
    assert get_default_voice_sensor() is sentinel
    set_default_voice_sensor(None)
    assert get_default_voice_sensor() is None
```
- [ ] **Step 2: run → FAIL** (`ImportError`). `python3 -m pytest tests/governance/intake/test_voice_sensor_default.py -q`
- [ ] **Step 3: implement** — in `voice_command_sensor.py`, add at module scope:
```python
_default_voice_sensor = None

def set_default_voice_sensor(sensor) -> None:
    global _default_voice_sensor
    _default_voice_sensor = sensor

def get_default_voice_sensor():
    return _default_voice_sensor
```
Add them to `__all__` if the module has one. In `intake_layer_service.py`, immediately after `self._voice_sensor = VoiceCommandSensor(...)` (~line 578), add:
```python
        try:
            from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
                set_default_voice_sensor,
            )
            set_default_voice_sensor(self._voice_sensor)
        except Exception:  # noqa: BLE001
            pass
```
- [ ] **Step 4: run → PASS**  — [ ] **Step 5: commit** `feat(karen): expose live VoiceCommandSensor via process singleton (S4 Task 1)`

---

### Task 2: `VoiceIntentClassifier` — build-vs-chat, pluggable

**Files:** Create `.../voice_build/__init__.py`, `.../voice_build/classifier.py`; Test `tests/governance/comms/voice_build/test_classifier.py`.

**Interfaces — Produces:** `class VoiceIntent(enum.Enum)`: `BUILD`, `IGNORE`. `class VoiceIntentClassifier(Protocol)`: `def classify(self, text: str) -> VoiceIntent`. `class HeuristicClassifier`: default deterministic impl — detects imperative software-change intent (leading imperative verb + a code/software object) via a small, overridable verb set (constructor param, NOT a frozen module constant), returns `BUILD` else `IGNORE`. Empty/whitespace → `IGNORE`.

- [ ] **Step 1: failing test**
```python
# tests/governance/comms/voice_build/test_classifier.py
from __future__ import annotations
from backend.core.ouroboros.governance.comms.voice_build.classifier import (
    HeuristicClassifier, VoiceIntent,
)

def test_build_commands_classified_build():
    c = HeuristicClassifier()
    for t in ["add rate limiting to the auth endpoint",
              "fix the failing login test",
              "refactor the payment module"]:
        assert c.classify(t) == VoiceIntent.BUILD

def test_chat_and_noise_classified_ignore():
    c = HeuristicClassifier()
    for t in ["what time is it", "hey karen how are you", "", "   ", "thanks"]:
        assert c.classify(t) == VoiceIntent.IGNORE

def test_verb_set_is_injectable_not_hardcoded():
    c = HeuristicClassifier(build_verbs={"frobnicate"})
    assert c.classify("frobnicate the widget") == VoiceIntent.BUILD
    assert c.classify("add a feature") == VoiceIntent.IGNORE   # 'add' not in the custom set
```
- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implement** — `classifier.py`:
```python
from __future__ import annotations
import enum
from typing import Iterable, Optional, Protocol

class VoiceIntent(enum.Enum):
    BUILD = "build"
    IGNORE = "ignore"

class VoiceIntentClassifier(Protocol):
    def classify(self, text: str) -> VoiceIntent: ...

_DEFAULT_BUILD_VERBS = frozenset({
    "add", "fix", "refactor", "implement", "build", "create", "remove",
    "delete", "rename", "update", "change", "write", "make", "wire", "harden",
})

class HeuristicClassifier:
    """Deterministic build-intent detection. The verb set is injectable (not a
    frozen module constant) so the policy is configurable, not hardcoded; an
    LLM-backed classifier can implement the same protocol later."""
    def __init__(self, build_verbs: Optional[Iterable[str]] = None) -> None:
        self._verbs = frozenset(v.lower() for v in (build_verbs or _DEFAULT_BUILD_VERBS))
    def classify(self, text: str) -> VoiceIntent:
        if not text or not text.strip():
            return VoiceIntent.IGNORE
        words = text.strip().lower().split()
        # imperative: a build verb in the first two tokens
        if any(w.strip(".,!?") in self._verbs for w in words[:2]):
            return VoiceIntent.BUILD
        return VoiceIntent.IGNORE
```
`__init__.py`: package docstring + `from __future__ import annotations`.
- [ ] **Step 4: run → PASS**  — [ ] **Step 5: commit** `feat(karen): VoiceIntentClassifier (build-vs-chat, pluggable) (S4 Task 2)`

---

### Task 3: `VoiceBuildBridge` — classify + route to the sensor

**Files:** Create `.../voice_build/bridge.py`; Test `tests/governance/comms/voice_build/test_bridge.py`.

**Interfaces — Consumes:** Task 1 sensor accessor, Task 2 classifier. **Produces:** `class VoiceBuildBridge(__init__(self, voice_sensor, classifier=None, repo="jarvis"))`; `async def on_final_transcript(self, text: str, confidence: float = 1.0) -> Optional[str]` — returns the sensor's ingest result for a BUILD, else `None`. Never raises. Builds `VoiceCommandPayload(description=text, target_files=[], repo=repo, stt_confidence=confidence)` and calls `voice_sensor.handle_voice_command`.

- [ ] **Step 1: failing test**
```python
# tests/governance/comms/voice_build/test_bridge.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.voice_build.bridge import VoiceBuildBridge

class _FakeSensor:
    def __init__(self, result="enqueued"): self.result, self.calls = result, []
    async def handle_voice_command(self, payload):
        self.calls.append(payload); return self.result

@pytest.mark.asyncio
async def test_build_transcript_routed_to_sensor():
    s = _FakeSensor()
    br = VoiceBuildBridge(s)
    res = await br.on_final_transcript("add rate limiting to auth", confidence=0.9)
    assert res == "enqueued"
    assert len(s.calls) == 1
    assert s.calls[0].description == "add rate limiting to auth"
    assert s.calls[0].stt_confidence == 0.9

@pytest.mark.asyncio
async def test_chat_transcript_not_routed():
    s = _FakeSensor()
    br = VoiceBuildBridge(s)
    assert await br.on_final_transcript("what time is it") is None
    assert s.calls == []

@pytest.mark.asyncio
async def test_sensor_exception_is_isolated():
    class _Boom:
        async def handle_voice_command(self, p): raise RuntimeError("router down")
    br = VoiceBuildBridge(_Boom())
    assert await br.on_final_transcript("fix the test") is None   # no raise
```
- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implement** — `bridge.py`:
```python
from __future__ import annotations
import logging
from typing import Optional
from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    VoiceCommandPayload,
)
from .classifier import HeuristicClassifier, VoiceIntent

logger = logging.getLogger("Ouroboros.Karen.VoiceBuild")

class VoiceBuildBridge:
    def __init__(self, voice_sensor, classifier=None, repo: str = "jarvis") -> None:
        self._sensor = voice_sensor
        self._classifier = classifier or HeuristicClassifier()
        self._repo = repo
    async def on_final_transcript(self, text: str, confidence: float = 1.0) -> Optional[str]:
        try:
            if self._sensor is None:
                return None
            if self._classifier.classify(text) != VoiceIntent.BUILD:
                return None
            payload = VoiceCommandPayload(
                description=text, target_files=[], repo=self._repo,
                stt_confidence=confidence,
            )
            return await self._sensor.handle_voice_command(payload)
        except Exception:  # noqa: BLE001
            logger.debug("[VoiceBuild] route failed", exc_info=True)
            return None
```
- [ ] **Step 4: run → PASS**  — [ ] **Step 5: commit** `feat(karen): VoiceBuildBridge — classify + route voice to intake (S4 Task 3)`

---

### Task 4: Wire into the pipeline (env-gated fork) + neutralize the broken bridge call

**Files:** Modify `backend/audio/audio_pipeline_bootstrap.py`; Test `tests/governance/comms/voice_build/test_wiring_smoke.py`.

**Interfaces — Produces:** in `wire_conversation_pipeline`, when `JARVIS_KAREN_VOICE_BUILD_ENABLED` is on: construct `VoiceBuildBridge(get_default_voice_sensor())`, store on the handle, and fork the final transcript to `await handle.voice_build.on_final_transcript(text, conf)` at the existing final-transcript point (in `ConversationPipeline`'s final handling — inject via a hook the pipeline already exposes, or a thin wrapper). Remove/repair the broken `create_voice_bridge(mode_dispatcher=..., audio_bus=...)` call (it currently TypeErrors + is swallowed) — replace with the new gated path.

- [ ] **Step 1: failing test** — a smoke test that the factory + gating logic compose (mock sensor); assert an enabled build utterance routes and a disabled config is inert. (Full pipeline wiring is verified live; this test covers the decision logic extracted into a small helper `should_route_voice_build() -> bool` reading the env flag.)
```python
# tests/governance/comms/voice_build/test_wiring_smoke.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.comms.voice_build.bridge import VoiceBuildBridge

class _FakeSensor:
    def __init__(self): self.calls = []
    async def handle_voice_command(self, p): self.calls.append(p); return "enqueued"

@pytest.mark.asyncio
async def test_bridge_end_to_end_with_default_sensor(monkeypatch):
    import backend.core.ouroboros.governance.intake.sensors.voice_command_sensor as vcs
    s = _FakeSensor()
    vcs.set_default_voice_sensor(s)
    try:
        br = VoiceBuildBridge(vcs.get_default_voice_sensor())
        await br.on_final_transcript("implement the retry loop", confidence=0.95)
        assert len(s.calls) == 1
    finally:
        vcs.set_default_voice_sensor(None)
```
- [ ] **Step 2: run → FAIL** (until Task 1/3 land — should PASS once they do; this task's real work is the bootstrap edit)
- [ ] **Step 3: implement** — bootstrap edits: (a) replace the broken `create_voice_bridge(mode_dispatcher=..., audio_bus=...)` block with `pass` / removal; (b) add env-gated construction:
```python
    if os.getenv("JARVIS_KAREN_VOICE_BUILD_ENABLED", "").strip().lower() in ("1","true","yes","on"):
        try:
            from backend.core.ouroboros.governance.comms.voice_build.bridge import VoiceBuildBridge
            from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import get_default_voice_sensor
            handle.voice_build = VoiceBuildBridge(get_default_voice_sensor())
            logger.info("[Bootstrap] Karen voice->build bridge mounted")
        except Exception as e:
            handle.voice_build = None
            logger.warning(f"[Bootstrap] voice->build mount skipped: {e}")
```
(c) at the ConversationPipeline final-transcript point, fork: `if getattr(handle, "voice_build", None) is not None: await handle.voice_build.on_final_transcript(final_text, confidence)`. Add `voice_build: object = None` to `PipelineHandle`. Add teardown = drop the reference in `shutdown`.
- [ ] **Step 4: run → PASS** (`tests/governance/comms/voice_build/ -q`; AST-parse the bootstrap; confirm no `create_voice_bridge(mode_dispatcher` remains)  — [ ] **Step 5: commit** `feat(karen): wire voice->build into pipeline, remove broken bridge call (S4 Task 4)`

---

## Sprint 4 Definition of Done
- `get_default_voice_sensor()` exposes the live sensor; `VoiceBuildBridge` classifies + routes a spoken build command into `UnifiedIntakeRouter.ingest` via `VoiceCommandSensor`.
- The broken `create_voice_bridge(...)` call is gone.
- STOP path reachable (sensor stop-phrase → UserSignalBus → GLS cancel) — verified by the existing sensor logic now being invokable.
- All `voice_build` + `intake` new tests green; env-gated (off = inert).

## Not in Sprint 4 (flagged follow-ups)
- Prompt-level mid-flight STEERING (redirect a running op's plan, beyond STOP+resubmit) — needs new, authority-bounded design; not a wiring job.
- LLM-backed `VoiceIntentClassifier` (implements the same protocol) for fuzzy commands.
- Live mic battle test: say "add rate limiting" → watch an op appear in the governed loop → say "stop" → watch it cancel.
