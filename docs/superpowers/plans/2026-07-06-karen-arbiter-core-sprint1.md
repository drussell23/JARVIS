# Karen Full-Duplex Voice — Sprint 1: Arbiter Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `VoiceDuplexArbiter` — the single async coordinator that owns the audio floor and arbitrates proactive (organism) vs. reactive (user) speech with barge-in, priority preemption, coalescing, and bounded shedding — fully unit-tested against a fake audio device (no mic/speaker).

**Architecture:** A pure-asyncio state machine (`LISTENING`/`USER_SPEAKING`/`KAREN_SPEAKING`/`THINKING`) drains a per-priority bounded queue and plays the highest-priority `SpeechRequest` through an injected `PlaybackHandle`. Barge-in and higher-priority requests preempt active playback via `preempt()`. Real STT/TTS engines are deferred to Sprint 3 — Sprint 1 depends only on the `PlaybackHandle` protocol (dependency inversion), so every behavior is testable with a `FakePlayback`.

**Tech Stack:** Python 3.9+, `asyncio`, `pytest` + `pytest-asyncio` (already configured). No new dependencies.

## Global Constraints

- Python 3.9+ (`from __future__ import annotations` in every file; no `asyncio.timeout`, use `asyncio.wait_for`).
- Pure asyncio — no blocking calls, no `time.sleep`/`sleep` loops in the arbiter.
- No new TTS/STT/network dependencies (Sprint 1 is engine-free; `PlaybackHandle` is a protocol).
- Fault isolation — any playback failure logs at DEBUG and returns; NEVER propagates to the caller/FSM.
- Kill switches default **false** during build: `JARVIS_KAREN_VOICE_ENABLED`, `JARVIS_KAREN_BARGE_IN_ENABLED`, `JARVIS_KAREN_PROACTIVE_ENABLED`.
- Priority ladder (verbatim): user barge-in > user-command response > proactive-critical (approval) > proactive-info (FYI).
- Bounded drop-oldest proactive queue; coalesce same-`coalesce_key` (keep latest). The organism NEVER blocks on audio.

---

## ⚠️ CORRECTION (applied after Task 3 — binds Tasks 4-7)

The `async def _drain(arb, fp)` helper shown in the task snippets below (a fixed
`2× await asyncio.sleep(0)`) is **superseded** — it is non-deterministic across
the arbiter's multi-hop resume chain and leaks a blocked play task into pytest
teardown (a hang). Task 3 replaced it in `tests/voice/duplex/test_arbiter.py`
with two helpers that Tasks 4-7 MUST use:

```python
async def _until(predicate, timeout: float = 2.0) -> None:
    """Wait until predicate() is true; AssertionError on timeout (fail fast)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"condition not met within {timeout}s")

async def _shutdown(arb, task) -> None:
    await arb.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
```

**For every Task 4-7 test:** replace `await _drain(arb, fp)` with
`await _until(lambda: <the condition the drain was waiting for>)`, wrap the body
in `try: ... finally: await _shutdown(arb, task)`, and NEVER assert on a fixed
number of `sleep(0)` yields. `arbiter.stop()` already cancels active playback.

## File Structure

- `backend/core/ouroboros/governance/comms/duplex/__init__.py` — package marker.
- `backend/core/ouroboros/governance/comms/duplex/protocols.py` — types + seams: `Priority`, `VoiceState`, `SpeechRequest`, `PlaybackHandle` protocol, `ArbiterConfig`.
- `backend/core/ouroboros/governance/comms/duplex/arbiter.py` — `VoiceDuplexArbiter`.
- `tests/voice/duplex/fakes.py` — `FakePlayback` (controllable `PlaybackHandle`).
- `tests/voice/duplex/test_protocols.py`, `tests/voice/duplex/test_arbiter.py`.

---

### Task 1: Protocols & types

**Files:**
- Create: `backend/core/ouroboros/governance/comms/duplex/__init__.py`
- Create: `backend/core/ouroboros/governance/comms/duplex/protocols.py`
- Test: `tests/voice/duplex/test_protocols.py`

**Interfaces:**
- Produces:
  - `class Priority(IntEnum)`: `PROACTIVE_INFO=1`, `PROACTIVE_CRITICAL=2`, `USER_RESPONSE=3`, `USER_BARGE_IN=4`
  - `class VoiceState(str, Enum)`: `LISTENING`, `USER_SPEAKING`, `KAREN_SPEAKING`, `THINKING`
  - `@dataclass(frozen=True) class SpeechRequest`: `text: str`, `priority: Priority`, `coalesce_key: str = ""`, `op_id: str = ""`
  - `class PlaybackHandle(Protocol)`: `async def play(self, text: str) -> None`, `def preempt(self) -> None`, `@property def is_active(self) -> bool`
  - `@dataclass(frozen=True) class ArbiterConfig`: `enabled: bool`, `barge_in_enabled: bool`, `proactive_enabled: bool`, `queue_max_per_priority: int = 8`; classmethod `from_env() -> ArbiterConfig`

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/duplex/test_protocols.py
from __future__ import annotations

from backend.core.ouroboros.governance.comms.duplex.protocols import (
    ArbiterConfig, Priority, SpeechRequest, VoiceState,
)


def test_priority_ordering_user_barge_in_is_highest():
    assert Priority.USER_BARGE_IN > Priority.USER_RESPONSE
    assert Priority.USER_RESPONSE > Priority.PROACTIVE_CRITICAL
    assert Priority.PROACTIVE_CRITICAL > Priority.PROACTIVE_INFO


def test_speech_request_defaults_and_frozen():
    r = SpeechRequest(text="hi", priority=Priority.PROACTIVE_INFO)
    assert r.coalesce_key == "" and r.op_id == ""
    try:
        r.text = "x"  # type: ignore[misc]
        assert False, "should be frozen"
    except AttributeError:
        pass


def test_voice_state_values():
    assert VoiceState.LISTENING.value == "listening"
    assert {s for s in VoiceState} >= {
        VoiceState.LISTENING, VoiceState.USER_SPEAKING,
        VoiceState.KAREN_SPEAKING, VoiceState.THINKING,
    }


def test_config_from_env_defaults_false(monkeypatch):
    for k in ("JARVIS_KAREN_VOICE_ENABLED", "JARVIS_KAREN_BARGE_IN_ENABLED",
              "JARVIS_KAREN_PROACTIVE_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    cfg = ArbiterConfig.from_env()
    assert cfg.enabled is False
    assert cfg.barge_in_enabled is False
    assert cfg.proactive_enabled is False
    assert cfg.queue_max_per_priority == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/voice/duplex/test_protocols.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named '...duplex'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/comms/duplex/__init__.py
"""Full-duplex Karen voice — arbiter core (Sprint 1).

Engine-free coordination layer: depends only on the PlaybackHandle protocol so
the concurrency/barge-in logic is unit-testable without a mic or speaker.
"""
from __future__ import annotations
```

```python
# backend/core/ouroboros/governance/comms/duplex/protocols.py
from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class Priority(enum.IntEnum):
    """Speech priority ladder. Higher preempts lower."""
    PROACTIVE_INFO = 1       # FYI narration
    PROACTIVE_CRITICAL = 2   # needs approval
    USER_RESPONSE = 3        # answer to a user command
    USER_BARGE_IN = 4        # user interrupting Karen


class VoiceState(str, enum.Enum):
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    KAREN_SPEAKING = "karen_speaking"
    THINKING = "thinking"


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    priority: Priority
    coalesce_key: str = ""   # same key → keep only the latest
    op_id: str = ""


@runtime_checkable
class PlaybackHandle(Protocol):
    """The audio floor. Sprint 3 wraps unified_voice_orchestrator; Sprint 1
    uses FakePlayback."""
    async def play(self, text: str) -> None: ...
    def preempt(self) -> None: ...
    @property
    def is_active(self) -> bool: ...


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class ArbiterConfig:
    enabled: bool = False
    barge_in_enabled: bool = False
    proactive_enabled: bool = False
    queue_max_per_priority: int = 8

    @classmethod
    def from_env(cls) -> "ArbiterConfig":
        return cls(
            enabled=_env_bool("JARVIS_KAREN_VOICE_ENABLED", False),
            barge_in_enabled=_env_bool("JARVIS_KAREN_BARGE_IN_ENABLED", False),
            proactive_enabled=_env_bool("JARVIS_KAREN_PROACTIVE_ENABLED", False),
            queue_max_per_priority=8,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/voice/duplex/test_protocols.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/duplex/__init__.py \
        backend/core/ouroboros/governance/comms/duplex/protocols.py \
        tests/voice/duplex/test_protocols.py
git commit -m "feat(karen): duplex voice protocols + types (arbiter Sprint 1)"
```

---

### Task 2: FakePlayback test double

**Files:**
- Create: `tests/voice/__init__.py` (empty) and `tests/voice/duplex/__init__.py` (empty) — so `from tests.voice.duplex.fakes import FakePlayback` resolves as a package import. (Verify first with `python3 -c "import tests" 2>&1`; if the repo already imports `tests.*` without `__init__.py` via namespace packages, these are harmless.)
- Create: `tests/voice/duplex/fakes.py`
- Test: `tests/voice/duplex/test_fakes.py`

**Interfaces:**
- Consumes: `PlaybackHandle` protocol (Task 1).
- Produces: `class FakePlayback` — controllable playback. `play(text)` blocks on an internal `asyncio.Event` (`release()` to finish, `preempt()` to cancel); records `played: list[str]`, `preempt_count: int`, `is_active`.

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/duplex/test_fakes.py
from __future__ import annotations

import asyncio
import pytest

from tests.voice.duplex.fakes import FakePlayback


@pytest.mark.asyncio
async def test_fake_play_completes_on_release():
    fp = FakePlayback()
    task = asyncio.create_task(fp.play("hello"))
    await asyncio.sleep(0)          # let play() start
    assert fp.is_active is True
    assert fp.played == ["hello"]
    fp.release()                    # simulate playback finishing
    await task
    assert fp.is_active is False


@pytest.mark.asyncio
async def test_fake_preempt_cancels_active_play():
    fp = FakePlayback()
    task = asyncio.create_task(fp.play("hello"))
    await asyncio.sleep(0)
    fp.preempt()                    # barge-in
    await task
    assert fp.is_active is False
    assert fp.preempt_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/voice/duplex/test_fakes.py -q`
Expected: FAIL — `ModuleNotFoundError: tests.voice.duplex.fakes`

- [ ] **Step 3: Write minimal implementation**

```python
# tests/voice/duplex/fakes.py
from __future__ import annotations

import asyncio
from typing import List


class FakePlayback:
    """Controllable PlaybackHandle for arbiter tests. play() awaits an internal
    Event so tests deterministically control when 'audio' finishes (release())
    or is cut off (preempt())."""

    def __init__(self) -> None:
        self.played: List[str] = []
        self.preempt_count = 0
        self._active = False
        self._gate: "asyncio.Event | None" = None

    @property
    def is_active(self) -> bool:
        return self._active

    async def play(self, text: str) -> None:
        self.played.append(text)
        self._active = True
        self._gate = asyncio.Event()
        try:
            await self._gate.wait()
        finally:
            self._active = False

    def preempt(self) -> None:
        self.preempt_count += 1
        self._active = False
        if self._gate is not None:
            self._gate.set()   # unblock play() early

    def release(self) -> None:
        """Test helper: simulate playback finishing naturally."""
        if self._gate is not None:
            self._gate.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/voice/duplex/test_fakes.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/voice/duplex/fakes.py tests/voice/duplex/test_fakes.py
git commit -m "test(karen): FakePlayback test double for the arbiter"
```

---

### Task 3: Arbiter skeleton — submit + drain highest priority

**Files:**
- Create: `backend/core/ouroboros/governance/comms/duplex/arbiter.py`
- Test: `tests/voice/duplex/test_arbiter.py`

**Interfaces:**
- Consumes: `PlaybackHandle` (Task 1), `FakePlayback` (Task 2), `Priority`/`SpeechRequest`/`VoiceState`/`ArbiterConfig` (Task 1).
- Produces:
  - `class VoiceDuplexArbiter`: `__init__(self, playback: PlaybackHandle, *, config: ArbiterConfig | None = None)`
  - `def submit(self, request: SpeechRequest) -> None` — non-blocking enqueue
  - `async def run(self) -> None` — arbitration loop (cancel to stop)
  - `async def stop(self) -> None`
  - `@property def state(self) -> VoiceState`
  - `def snapshot(self) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/voice/duplex/test_arbiter.py
from __future__ import annotations

import asyncio
import pytest

from backend.core.ouroboros.governance.comms.duplex.arbiter import VoiceDuplexArbiter
from backend.core.ouroboros.governance.comms.duplex.protocols import (
    ArbiterConfig, Priority, SpeechRequest, VoiceState,
)
from tests.voice.duplex.fakes import FakePlayback

_ON = ArbiterConfig(enabled=True, barge_in_enabled=True, proactive_enabled=True)


async def _drain(arb, fp):
    """Let the run loop pick up work and start playing."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_submit_then_play_highest_priority_first():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())

    arb.submit(SpeechRequest("info", Priority.PROACTIVE_INFO))
    arb.submit(SpeechRequest("urgent", Priority.PROACTIVE_CRITICAL))
    await _drain(arb, fp)

    # Higher priority (critical) plays first.
    assert fp.played[0] == "urgent"
    assert arb.state == VoiceState.KAREN_SPEAKING

    fp.release()                       # finish "urgent"
    await _drain(arb, fp)
    assert fp.played[1] == "info"      # then the info line

    fp.release()
    await _drain(arb, fp)
    assert arb.state == VoiceState.LISTENING

    await arb.stop()
    task.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/voice/duplex/test_arbiter.py::test_submit_then_play_highest_priority_first -q`
Expected: FAIL — `ModuleNotFoundError: ...arbiter`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/core/ouroboros/governance/comms/duplex/arbiter.py
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Deque, Dict, Optional

from .protocols import (
    ArbiterConfig, PlaybackHandle, Priority, SpeechRequest, VoiceState,
)

logger = logging.getLogger("Ouroboros.Karen.Arbiter")


class VoiceDuplexArbiter:
    """Single async owner of the audio floor (Sprint 1: engine-free)."""

    def __init__(
        self, playback: PlaybackHandle, *, config: Optional[ArbiterConfig] = None,
    ) -> None:
        self._playback = playback
        self._config = config or ArbiterConfig.from_env()
        self._state = VoiceState.LISTENING
        # Per-priority FIFO queues (drop-oldest bounded in a later task).
        self._queues: Dict[Priority, Deque[SpeechRequest]] = {
            p: deque() for p in Priority
        }
        self._wake = asyncio.Event()          # signalled when work is enqueued
        self._play_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def state(self) -> VoiceState:
        return self._state

    def submit(self, request: SpeechRequest) -> None:
        """Non-blocking enqueue. Never raises."""
        if not self._config.enabled:
            return
        try:
            self._queues[request.priority].append(request)
            self._wake.set()
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] submit failed", exc_info=True)

    def _pop_highest(self) -> Optional[SpeechRequest]:
        for p in sorted(Priority, reverse=True):
            q = self._queues[p]
            if q:
                return q.popleft()
        return None

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self._wake.wait()
            self._wake.clear()
            while self._state == VoiceState.LISTENING:
                req = self._pop_highest()
                if req is None:
                    break
                await self._speak(req)

    async def _speak(self, req: SpeechRequest) -> None:
        self._state = VoiceState.KAREN_SPEAKING
        self._play_task = asyncio.create_task(self._playback.play(req.text))
        try:
            await self._play_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] playback failed", exc_info=True)
        finally:
            self._play_task = None
            if self._state == VoiceState.KAREN_SPEAKING:
                self._state = VoiceState.LISTENING

    async def stop(self) -> None:
        self._running = False
        self._wake.set()

    def snapshot(self) -> dict:
        return {
            "state": self._state.value,
            "queued": {p.name: len(q) for p, q in self._queues.items()},
            "enabled": self._config.enabled,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/voice/duplex/test_arbiter.py::test_submit_then_play_highest_priority_first -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/duplex/arbiter.py tests/voice/duplex/test_arbiter.py
git commit -m "feat(karen): arbiter skeleton — submit + priority drain"
```

---

### Task 4: Barge-in — user speech preempts Karen

**Files:**
- Modify: `backend/core/ouroboros/governance/comms/duplex/arbiter.py`
- Test: `tests/voice/duplex/test_arbiter.py`

**Interfaces:**
- Produces (adds to `VoiceDuplexArbiter`):
  - `async def on_user_speech_start(self) -> None`
  - `async def on_user_speech_end(self) -> None`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/voice/duplex/test_arbiter.py
@pytest.mark.asyncio
async def test_user_speech_start_barges_in_and_flushes():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())

    arb.submit(SpeechRequest("a long narration", Priority.PROACTIVE_INFO))
    await _drain(arb, fp)
    assert arb.state == VoiceState.KAREN_SPEAKING

    await arb.on_user_speech_start()          # user interrupts
    assert fp.preempt_count == 1              # playback was killed
    assert arb.state == VoiceState.USER_SPEAKING

    await arb.on_user_speech_end()
    assert arb.state == VoiceState.LISTENING

    await arb.stop(); task.cancel()


@pytest.mark.asyncio
async def test_no_play_while_user_speaking():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())

    await arb.on_user_speech_start()
    arb.submit(SpeechRequest("proactive", Priority.PROACTIVE_CRITICAL))
    await _drain(arb, fp)
    assert fp.played == []                    # queued, not played
    assert arb.state == VoiceState.USER_SPEAKING

    await arb.on_user_speech_end()
    await _drain(arb, fp)
    assert fp.played == ["proactive"]         # drains after user done

    await arb.stop(); task.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/voice/duplex/test_arbiter.py -q -k barge or user`
Expected: FAIL — `AttributeError: 'VoiceDuplexArbiter' object has no attribute 'on_user_speech_start'`

- [ ] **Step 3: Write minimal implementation**

Add these methods to `VoiceDuplexArbiter` (after `_speak`):

```python
    async def on_user_speech_start(self) -> None:
        """Barge-in trigger. Preempts Karen and holds the floor. Never raises."""
        if not self._config.barge_in_enabled:
            return
        try:
            if self._state == VoiceState.KAREN_SPEAKING:
                self._playback.preempt()          # kill playback (idempotent)
                if self._play_task is not None:
                    self._play_task.cancel()
            self._state = VoiceState.USER_SPEAKING
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] on_user_speech_start failed", exc_info=True)

    async def on_user_speech_end(self) -> None:
        try:
            if self._state == VoiceState.USER_SPEAKING:
                self._state = VoiceState.LISTENING
                self._wake.set()                  # resume draining the queue
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] on_user_speech_end failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/voice/duplex/test_arbiter.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/duplex/arbiter.py tests/voice/duplex/test_arbiter.py
git commit -m "feat(karen): arbiter barge-in — user speech preempts + holds floor"
```

---

### Task 5: Priority preemption — a higher-priority request cuts a lower one

**Files:**
- Modify: `backend/core/ouroboros/governance/comms/duplex/arbiter.py`
- Test: `tests/voice/duplex/test_arbiter.py`

**Interfaces:**
- Modifies `submit()` to preempt an *active lower-priority* playback; adds `_active_priority`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/voice/duplex/test_arbiter.py
@pytest.mark.asyncio
async def test_critical_preempts_active_info_playback():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())

    arb.submit(SpeechRequest("info", Priority.PROACTIVE_INFO))
    await _drain(arb, fp)
    assert arb.state == VoiceState.KAREN_SPEAKING and fp.played == ["info"]

    arb.submit(SpeechRequest("APPROVAL", Priority.PROACTIVE_CRITICAL))
    await _drain(arb, fp)
    assert fp.preempt_count == 1               # info was cut
    assert fp.played[-1] == "APPROVAL"         # critical played immediately

    await arb.stop(); task.cancel()


@pytest.mark.asyncio
async def test_equal_or_lower_priority_does_not_preempt():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    task = asyncio.create_task(arb.run())

    arb.submit(SpeechRequest("crit1", Priority.PROACTIVE_CRITICAL))
    await _drain(arb, fp)
    arb.submit(SpeechRequest("info", Priority.PROACTIVE_INFO))
    await _drain(arb, fp)
    assert fp.preempt_count == 0               # info waits its turn
    assert fp.played == ["crit1"]

    await arb.stop(); task.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/voice/duplex/test_arbiter.py -q -k preempt`
Expected: FAIL — `crit1` not preempted assertion or `preempt_count` mismatch.

- [ ] **Step 3: Write minimal implementation**

In `__init__`, add `self._active_priority: Optional[Priority] = None`.
In `_speak`, set `self._active_priority = req.priority` at the top and `self._active_priority = None` in the `finally`.
Replace `submit()` with the preempting version:

```python
    def submit(self, request: SpeechRequest) -> None:
        """Non-blocking enqueue. A strictly-higher-priority request preempts an
        active lower-priority playback (but never interrupts the user). Never
        raises."""
        if not self._config.enabled:
            return
        try:
            self._queues[request.priority].append(request)
            if (
                self._state == VoiceState.KAREN_SPEAKING
                and self._active_priority is not None
                and request.priority > self._active_priority
            ):
                self._playback.preempt()
                if self._play_task is not None:
                    self._play_task.cancel()
                self._state = VoiceState.LISTENING     # let run() pick the winner
            self._wake.set()
        except Exception:  # noqa: BLE001
            logger.debug("[Arbiter] submit failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/voice/duplex/test_arbiter.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/duplex/arbiter.py tests/voice/duplex/test_arbiter.py
git commit -m "feat(karen): arbiter priority preemption of active playback"
```

---

### Task 6: Coalescing + bounded drop-oldest + telemetry

**Files:**
- Modify: `backend/core/ouroboros/governance/comms/duplex/arbiter.py`
- Test: `tests/voice/duplex/test_arbiter.py`

**Interfaces:**
- Modifies `submit()` (coalesce by `coalesce_key`, bound queue drop-oldest); adds `shed_count`, `coalesced_count` to `snapshot()`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/voice/duplex/test_arbiter.py
@pytest.mark.asyncio
async def test_coalesce_keeps_latest_same_key():
    fp = FakePlayback()
    arb = VoiceDuplexArbiter(fp, config=_ON)
    # Don't start run() — inspect the queue directly.
    arb.submit(SpeechRequest("hb v1", Priority.PROACTIVE_INFO, coalesce_key="hb"))
    arb.submit(SpeechRequest("hb v2", Priority.PROACTIVE_INFO, coalesce_key="hb"))
    q = arb._queues[Priority.PROACTIVE_INFO]
    assert [r.text for r in q] == ["hb v2"]        # only the latest survives
    assert arb.snapshot()["coalesced_count"] == 1


@pytest.mark.asyncio
async def test_bounded_queue_drops_oldest():
    fp = FakePlayback()
    cfg = ArbiterConfig(enabled=True, barge_in_enabled=True,
                        proactive_enabled=True, queue_max_per_priority=2)
    arb = VoiceDuplexArbiter(fp, config=cfg)
    for i in range(4):
        arb.submit(SpeechRequest(f"m{i}", Priority.PROACTIVE_INFO))
    q = arb._queues[Priority.PROACTIVE_INFO]
    assert [r.text for r in q] == ["m2", "m3"]     # oldest two shed
    assert arb.snapshot()["shed_count"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/voice/duplex/test_arbiter.py -q -k "coalesce or bounded"`
Expected: FAIL — `KeyError: 'coalesced_count'` / queue not bounded.

- [ ] **Step 3: Write minimal implementation**

In `__init__`, add `self.shed_count = 0` and `self.coalesced_count = 0`.
Insert coalescing + bounding into `submit()` right before `self._queues[request.priority].append(request)`:

```python
            q = self._queues[request.priority]
            if request.coalesce_key:
                before = len(q)
                q = deque(
                    r for r in q if r.coalesce_key != request.coalesce_key
                )
                self._queues[request.priority] = q
                self.coalesced_count += before - len(q)
            q.append(request)
            while len(q) > self._config.queue_max_per_priority:
                q.popleft()
                self.shed_count += 1
```

(Remove the old bare `self._queues[request.priority].append(request)` line — the block above now appends.)
Extend `snapshot()` return dict with `"shed_count": self.shed_count, "coalesced_count": self.coalesced_count`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/voice/duplex/test_arbiter.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/duplex/arbiter.py tests/voice/duplex/test_arbiter.py
git commit -m "feat(karen): arbiter coalescing + bounded drop-oldest + telemetry"
```

---

### Task 7: Kill switches + fault isolation

**Files:**
- Modify: `backend/core/ouroboros/governance/comms/duplex/arbiter.py`
- Test: `tests/voice/duplex/test_arbiter.py`

**Interfaces:** no new public API — hardens existing paths.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/voice/duplex/test_arbiter.py
class _BoomPlayback:
    """PlaybackHandle whose play() raises — must not crash the arbiter."""
    def __init__(self): self.preempt_count = 0
    @property
    def is_active(self): return False
    async def play(self, text): raise RuntimeError("audio device on fire")
    def preempt(self): self.preempt_count += 1


@pytest.mark.asyncio
async def test_disabled_arbiter_is_noop():
    fp = FakePlayback()
    off = ArbiterConfig(enabled=False, barge_in_enabled=False, proactive_enabled=False)
    arb = VoiceDuplexArbiter(fp, config=off)
    arb.submit(SpeechRequest("x", Priority.PROACTIVE_INFO))
    assert all(len(q) == 0 for q in arb._queues.values())  # nothing enqueued


@pytest.mark.asyncio
async def test_playback_exception_does_not_break_loop():
    arb = VoiceDuplexArbiter(_BoomPlayback(), config=_ON)
    task = asyncio.create_task(arb.run())
    arb.submit(SpeechRequest("boom", Priority.PROACTIVE_INFO))
    await _drain(arb, None)
    # Loop survived the playback exception and returned to LISTENING.
    assert arb.state == VoiceState.LISTENING
    await arb.stop(); task.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/voice/duplex/test_arbiter.py -q -k "disabled or exception"`
Expected: `test_disabled_arbiter_is_noop` PASSES (submit already gates on enabled); `test_playback_exception_does_not_break_loop` should already PASS if `_speak`'s try/except is correct. If either fails, fix the guard.

- [ ] **Step 3: Write minimal implementation**

These behaviors are already implemented (submit gates on `enabled`; `_speak` wraps playback in try/except and resets to `LISTENING` in `finally`). If Step 2 shows a failure, ensure `_speak`'s `finally` unconditionally clears `_active_priority` and resets state:

```python
        finally:
            self._play_task = None
            self._active_priority = None
            if self._state == VoiceState.KAREN_SPEAKING:
                self._state = VoiceState.LISTENING
            self._wake.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/voice/duplex/ -q`
Expected: PASS (entire Sprint 1 suite green)

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/duplex/arbiter.py tests/voice/duplex/test_arbiter.py
git commit -m "feat(karen): arbiter kill switches + fault isolation (Sprint 1 complete)"
```

---

## Sprint 1 Definition of Done

- `tests/voice/duplex/` fully green: protocols, fakes, arbiter (priority drain, barge-in, preemption, coalescing, bounded shedding, kill switches, fault isolation).
- Zero real audio touched; zero new dependencies.
- `VoiceDuplexArbiter` ready for Sprint 3 to inject a real `PlaybackHandle` wrapping `unified_voice_orchestrator._current_process`.

## Not in Sprint 1 (later plans)

- Sprint 2: `KarenSpeechSynthesizer` (LLM + persona + FSM context); retire `narrator_script.py`.
- Sprint 3: real `StreamingSTTEngine` + `UnifiedTTSEngine` + orchestrator `preempt()`/`flush()` wiring; always-listening capture loop.
- Sprint 4: voice→build intake + mid-flight steering.
