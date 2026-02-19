# Audio Pipeline Unification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the 7 disconnected conversation pipeline layers into a working end-to-end system by creating a bootstrap module, fixing component API mismatches, and correcting audio routing.

**Architecture:** Two-phase bootstrap module (`audio_pipeline_bootstrap.py`) extracts and fixes the existing buggy inline wiring code from `unified_supervisor.py:62645-62748`. Phase 1 (early) starts AudioBus before narrator. Phase 2 (late) wires StreamingSTT, TurnDetector, BargeIn, ConversationPipeline, and ModeDispatcher after Intelligence provides the LLM client.

**Tech Stack:** Python 3 async/await, sounddevice, faster-whisper, webrtcvad, speexdsp, Piper TTS, UnifiedModelServing (PRIME_API/PRIME_LOCAL/CLAUDE tier chain)

**Key Discovery:** The supervisor already has inline wiring at `unified_supervisor.py:62645-62748` but it has 3 bugs: ModeDispatcher constructor doesn't accept kwargs (line 62720), ModeDispatcher has no `start()` method (line 62722), and STT/BargeIn are never registered as AudioBus mic consumers.

---

### Task 1: Fix ModeDispatcher API to Accept Dependencies

The supervisor passes `conversation_pipeline=` to `ModeDispatcher()` but the constructor takes no args. Also calls `start()` which doesn't exist. Fix ModeDispatcher to accept deps in constructor and add `start()`/`stop()` lifecycle methods.

**Files:**
- Modify: `backend/audio/mode_dispatcher.py` (lines 57-68, add lines after 216)

**Step 1: Update ModeDispatcher constructor**

In `backend/audio/mode_dispatcher.py`, replace the `__init__` method (lines 63-76):

```python
def __init__(
    self,
    conversation_pipeline=None,
    speech_state=None,
):
    self._current_mode = VoiceMode.COMMAND
    self._previous_mode: Optional[VoiceMode] = None
    self._last_activity = time.time()
    self._mode_change_callbacks = []

    # Pipeline references (set via constructor or setters)
    self._conversation_pipeline = conversation_pipeline
    self._speech_state = speech_state

    # Conversation task management
    self._conversation_task: Optional[asyncio.Task] = None

    # Stats
    self._mode_switches = 0
    self._mode_history = []
```

**Step 2: Fix `_enter_mode` to start conversation loop**

Replace `_enter_mode` (lines 145-157):

```python
async def _enter_mode(self, mode: VoiceMode) -> None:
    """Set up when entering a mode."""
    if mode == VoiceMode.CONVERSATION:
        # Enable conversation mode (skip cooldown, AEC handles echo)
        if self._speech_state is not None:
            self._speech_state.set_conversation_mode(True)

        # Start conversation session AND run the loop
        if self._conversation_pipeline is not None:
            await self._conversation_pipeline.start_session()
            # Launch conversation loop as a background task
            self._conversation_task = asyncio.ensure_future(
                self._conversation_pipeline.run()
            )

    elif mode == VoiceMode.BIOMETRIC:
        pass  # Biometric mode handled by existing voice_unlock system
```

**Step 3: Fix `_leave_mode` to cancel conversation task**

Replace `_leave_mode` (lines 134-143):

```python
async def _leave_mode(self, mode: VoiceMode) -> None:
    """Clean up when leaving a mode."""
    if mode == VoiceMode.CONVERSATION:
        # Cancel conversation loop task
        if self._conversation_task is not None:
            self._conversation_task.cancel()
            try:
                await self._conversation_task
            except asyncio.CancelledError:
                pass
            self._conversation_task = None

        # Disable conversation mode in speech state
        if self._speech_state is not None:
            self._speech_state.set_conversation_mode(False)

        # End conversation session
        if self._conversation_pipeline is not None:
            await self._conversation_pipeline.end_session()
```

**Step 4: Add `start()`/`stop()` lifecycle methods**

Add after `get_status()` (after line 229):

```python
async def start(self) -> None:
    """Start the mode dispatcher. Currently a no-op — modes are event-driven."""
    logger.info("[ModeDispatcher] Started")

async def stop(self) -> None:
    """Stop the mode dispatcher and clean up any active mode."""
    if self._current_mode == VoiceMode.CONVERSATION:
        await self._leave_mode(VoiceMode.CONVERSATION)
    logger.info("[ModeDispatcher] Stopped")
```

**Step 5: Commit**

```bash
git add backend/audio/mode_dispatcher.py
git commit -m "fix: ModeDispatcher accepts constructor deps, adds lifecycle methods and conversation loop launch"
```

---

### Task 2: Fix ConversationPipeline TTS Routing Through AudioBus

`_speak_sentence()` calls `tts_engine.speak()` directly, bypassing AudioBus. TTS output must go through AudioBus so AEC has a reference signal and BargeIn can detect JARVIS speaking.

**Files:**
- Modify: `backend/audio/conversation_pipeline.py` (lines 472-487)

**Step 1: Update `_speak_sentence` to route through AudioBus**

Replace `_speak_sentence` (lines 472-487):

```python
async def _speak_sentence(
    self, sentence: str, cancel_event: asyncio.Event
) -> None:
    """Speak a single sentence through TTS, routing through AudioBus for AEC."""
    if cancel_event.is_set():
        return

    if self._tts_engine is None:
        return

    try:
        # Generate audio from TTS engine (get raw audio, don't play directly)
        if hasattr(self._tts_engine, 'synthesize'):
            # Preferred: get raw audio bytes and route through AudioBus
            audio_data = await self._tts_engine.synthesize(sentence)
            if audio_data is not None and self._audio_bus is not None:
                import numpy as np
                # Convert bytes to float32 if needed
                if isinstance(audio_data, bytes):
                    audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32767.0
                elif isinstance(audio_data, np.ndarray):
                    audio_np = audio_data
                else:
                    audio_np = None

                if audio_np is not None and not cancel_event.is_set():
                    sample_rate = getattr(self._tts_engine, 'sample_rate', 22050)
                    await self._audio_bus.play_audio(audio_np, sample_rate)
                    return

        # Fallback: direct TTS playback (legacy path, no AEC reference)
        if hasattr(self._tts_engine, 'speak_stream'):
            await self._tts_engine.speak_stream(sentence, play_audio=True)
        else:
            await self._tts_engine.speak(sentence, play_audio=True)
    except Exception as e:
        logger.debug(f"[ConvPipeline] TTS error: {e}")
```

**Step 2: Fix SentenceSplitter regex edge case**

Replace the `split` method's inner loop (lines 138-150) to handle token boundaries without trailing whitespace:

```python
async def split(
    self, token_stream: AsyncIterator[str]
) -> AsyncIterator[str]:
    """
    Consume an async stream of tokens and yield complete sentences.
    Handles cases where sentence-ending punctuation arrives without
    trailing whitespace (common with token-by-token LLM streaming).
    """
    buffer = ""

    async for token in token_stream:
        buffer += token

        # Check for sentence boundaries
        while True:
            match = _SENTENCE_DELIMITERS.search(buffer)
            if match and match.start() >= self._min_len:
                sentence = buffer[:match.end()].strip()
                buffer = buffer[match.end():]
                if sentence:
                    yield sentence
            else:
                # Also check for punctuation at end of buffer
                # (next token may not start with whitespace)
                if (len(buffer) >= self._min_len
                        and buffer.rstrip()
                        and buffer.rstrip()[-1] in '.!?'
                        and len(buffer) > len(buffer.rstrip())):
                    # Punctuation followed by trailing space
                    sentence = buffer.rstrip()
                    buffer = ""
                    if sentence:
                        yield sentence
                break

    # Flush remaining buffer
    buffer = buffer.strip()
    if buffer:
        yield buffer
```

**Step 3: Commit**

```bash
git add backend/audio/conversation_pipeline.py
git commit -m "fix: route TTS through AudioBus for AEC reference, fix SentenceSplitter edge case"
```

---

### Task 3: Create Audio Pipeline Bootstrap Module

Extract and fix the buggy inline wiring from `unified_supervisor.py:62645-62748`. The bootstrap handles all component creation, registration, and lifecycle.

**Files:**
- Create: `backend/audio/audio_pipeline_bootstrap.py`

**Step 1: Write the bootstrap module**

```python
"""
Audio Pipeline Bootstrap (Composition Layer)
=============================================

Two-phase factory that creates, wires, and manages the lifecycle of all
real-time voice conversation components.

Phase 1 (early): AudioBus — called before narrator, fast (~1s)
Phase 2 (late):  ConversationPipeline — called after Intelligence, heavier (~5s)

Usage in supervisor:
    # Phase 1 (early startup)
    audio_bus = await audio_pipeline_bootstrap.start_audio_bus()

    # Phase 2 (after Intelligence phase provides LLM client)
    handle = await audio_pipeline_bootstrap.wire_conversation_pipeline(
        audio_bus=audio_bus,
        llm_client=model_serving,
        speech_state=speech_state_manager,
    )

    # Shutdown
    await audio_pipeline_bootstrap.shutdown(handle)
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PipelineHandle:
    """Lifecycle handle returned by wire_conversation_pipeline()."""
    audio_bus: object = None
    streaming_stt: object = None
    turn_detector: object = None
    barge_in: object = None
    tts_engine: object = None
    conversation_pipeline: object = None
    mode_dispatcher: object = None
    health_task: Optional[asyncio.Task] = None

    async def get_status(self) -> dict:
        """Aggregate status from all components."""
        status = {}
        for name in [
            "audio_bus", "streaming_stt", "turn_detector",
            "barge_in", "conversation_pipeline", "mode_dispatcher",
        ]:
            comp = getattr(self, name, None)
            if comp is not None and hasattr(comp, "get_status"):
                try:
                    status[name] = comp.get_status()
                except Exception as e:
                    status[name] = {"error": str(e)}
            else:
                status[name] = None
        return status


async def start_audio_bus(timeout: float = 5.0):
    """
    Phase 1: Start AudioBus. Called early, before narrator.

    Creates AudioBus singleton + FullDuplexDevice + AEC.
    Returns the AudioBus instance (or None on failure).
    """
    try:
        from backend.audio.audio_bus import AudioBus
        bus = AudioBus.get_instance()
        await asyncio.wait_for(bus.start(), timeout=timeout)
        logger.info("[Bootstrap] AudioBus started (Phase 1)")
        return bus
    except asyncio.TimeoutError:
        logger.warning(f"[Bootstrap] AudioBus start timed out ({timeout}s)")
        return None
    except Exception as e:
        logger.warning(f"[Bootstrap] AudioBus start failed: {e}")
        return None


async def wire_conversation_pipeline(
    audio_bus,
    llm_client=None,
    speech_state=None,
    stt_timeout: float = 10.0,
    tts_timeout: float = 15.0,
) -> PipelineHandle:
    """
    Phase 2: Wire all conversation pipeline components.

    Called after Intelligence phase so llm_client (UnifiedModelServing) is
    available. Each sub-component is independently optional — partial
    wiring is OK (degraded mode).

    Returns a PipelineHandle for lifecycle management.
    """
    handle = PipelineHandle(audio_bus=audio_bus)
    loop = asyncio.get_running_loop()

    # 1. TTS singleton
    try:
        from backend.voice.engines.unified_tts_engine import get_tts_engine
        handle.tts_engine = await asyncio.wait_for(
            get_tts_engine(), timeout=tts_timeout,
        )
        logger.info("[Bootstrap] TTS singleton ready")
    except Exception as e:
        logger.debug(f"[Bootstrap] TTS init skipped: {e}")

    # 2. StreamingSTT — register as AudioBus mic consumer
    try:
        from backend.voice.streaming_stt import StreamingSTTEngine
        handle.streaming_stt = StreamingSTTEngine()
        await asyncio.wait_for(handle.streaming_stt.start(), timeout=stt_timeout)

        if audio_bus is not None:
            audio_bus.register_mic_consumer(handle.streaming_stt.on_audio_frame)
            logger.info("[Bootstrap] StreamingSTT registered on AudioBus")
        else:
            logger.info("[Bootstrap] StreamingSTT started (no AudioBus)")
    except Exception as e:
        logger.debug(f"[Bootstrap] StreamingSTT init skipped: {e}")
        handle.streaming_stt = None

    # 3. TurnDetector + BargeInController
    try:
        from backend.audio.turn_detector import TurnDetector
        from backend.audio.barge_in_controller import BargeInController

        handle.turn_detector = TurnDetector()

        handle.barge_in = BargeInController()
        handle.barge_in.set_loop(loop)

        if audio_bus is not None:
            handle.barge_in.set_audio_bus(audio_bus)
            # Register barge-in VAD callback as AudioBus mic consumer.
            # This creates a lightweight VAD wrapper that feeds results
            # to the BargeInController.
            _bargein_vad = _create_bargein_vad_consumer(handle.barge_in)
            audio_bus.register_mic_consumer(_bargein_vad)
            logger.info("[Bootstrap] BargeInController registered on AudioBus")

        if speech_state is not None:
            handle.barge_in.set_speech_state(speech_state)

    except Exception as e:
        logger.debug(f"[Bootstrap] TurnDetector/BargeIn skipped: {e}")

    # 4. ConversationPipeline
    try:
        from backend.audio.conversation_pipeline import ConversationPipeline

        handle.conversation_pipeline = ConversationPipeline(
            audio_bus=audio_bus,
            streaming_stt=handle.streaming_stt,
            turn_detector=handle.turn_detector,
            barge_in=handle.barge_in,
            tts_engine=handle.tts_engine,
            llm_client=llm_client,
        )
        logger.info("[Bootstrap] ConversationPipeline created")
    except Exception as e:
        logger.debug(f"[Bootstrap] ConversationPipeline init skipped: {e}")

    # 5. ModeDispatcher
    try:
        from backend.audio.mode_dispatcher import ModeDispatcher

        handle.mode_dispatcher = ModeDispatcher(
            conversation_pipeline=handle.conversation_pipeline,
            speech_state=speech_state,
        )
        await handle.mode_dispatcher.start()
        logger.info("[Bootstrap] ModeDispatcher started")
    except Exception as e:
        logger.debug(f"[Bootstrap] ModeDispatcher init skipped: {e}")

    return handle


def _create_bargein_vad_consumer(barge_in):
    """
    Create a mic consumer callback that runs VAD and feeds results
    to the BargeInController.

    The consumer runs in the audio thread — must be fast and non-blocking.
    Uses energy-based VAD (no imports needed) to detect speech.
    """
    import numpy as np
    _energy_threshold = float(os.getenv("JARVIS_BARGEIN_ENERGY_THRESHOLD", "0.01"))

    def _on_frame(frame: np.ndarray) -> None:
        energy = float(np.sqrt(np.mean(frame ** 2)))
        is_speech = energy > _energy_threshold
        barge_in.on_vad_speech_detected(is_speech)

    return _on_frame


async def shutdown(handle: PipelineHandle) -> None:
    """
    Shutdown all components in reverse order.
    Each step has a timeout to prevent shutdown stall.
    """
    _timeout = 5.0

    # 1. ModeDispatcher
    if handle.mode_dispatcher is not None:
        try:
            await asyncio.wait_for(handle.mode_dispatcher.stop(), timeout=_timeout)
        except Exception as e:
            logger.debug(f"[Bootstrap] ModeDispatcher stop error: {e}")

    # 2. ConversationPipeline
    if handle.conversation_pipeline is not None:
        try:
            await asyncio.wait_for(
                handle.conversation_pipeline.end_session(), timeout=_timeout,
            )
        except Exception as e:
            logger.debug(f"[Bootstrap] ConversationPipeline end error: {e}")

    # 3. StreamingSTT — unregister from AudioBus first
    if handle.streaming_stt is not None:
        if handle.audio_bus is not None:
            try:
                handle.audio_bus.unregister_mic_consumer(
                    handle.streaming_stt.on_audio_frame
                )
            except Exception:
                pass
        try:
            await asyncio.wait_for(handle.streaming_stt.stop(), timeout=_timeout)
        except Exception as e:
            logger.debug(f"[Bootstrap] StreamingSTT stop error: {e}")

    # 4. Cancel health task
    if handle.health_task is not None:
        handle.health_task.cancel()
        try:
            await handle.health_task
        except (asyncio.CancelledError, Exception):
            pass

    # Note: AudioBus is NOT stopped here — it has its own lifecycle
    # managed by the supervisor (started in Phase 1, stopped at shutdown).

    logger.info("[Bootstrap] Audio pipeline shutdown complete")
```

**Step 2: Export from `__init__.py`**

Add to `backend/audio/__init__.py` exports:

```python
from backend.audio.audio_pipeline_bootstrap import (
    start_audio_bus as bootstrap_start_audio_bus,
    wire_conversation_pipeline,
    shutdown as bootstrap_shutdown,
    PipelineHandle,
)
```

**Step 3: Commit**

```bash
git add backend/audio/audio_pipeline_bootstrap.py backend/audio/__init__.py
git commit -m "feat: create two-phase audio pipeline bootstrap module"
```

---

### Task 4: Replace Supervisor Inline Wiring With Bootstrap Calls

The supervisor already has buggy inline wiring at lines 62645-62748 and an AudioBus early init at 61241-61273. Replace the Phase 2 wiring with bootstrap calls. Keep the Phase 1 AudioBus init as-is (it already works).

**Files:**
- Modify: `unified_supervisor.py` (lines 62645-62748)

**Step 1: Replace inline wiring block**

Replace lines 62645-62748 (the entire `v238.0: CONVERSATION PIPELINE WIRING` block) with:

```python
            # =====================================================================
            # v238.1: CONVERSATION PIPELINE WIRING (via Bootstrap)
            # =====================================================================
            # Wire real-time voice conversation components AFTER Phase 4
            # (Intelligence) so the LLM client is available.
            # Uses audio_pipeline_bootstrap for clean composition.
            # =====================================================================
            if self._audio_bus_enabled and self._audio_bus is not None:
                try:
                    _wire_start = time.time()
                    from backend.audio.audio_pipeline_bootstrap import (
                        wire_conversation_pipeline,
                    )

                    # Get speech state for mode transitions
                    _speech_state = None
                    try:
                        from backend.core.unified_speech_state import (
                            get_speech_state_manager,
                        )
                        _speech_state = await get_speech_state_manager()
                    except Exception:
                        pass

                    self._audio_pipeline_handle = await wire_conversation_pipeline(
                        audio_bus=self._audio_bus,
                        llm_client=self._model_serving,
                        speech_state=_speech_state,
                    )

                    # Store references for shutdown and status
                    self._conversation_pipeline = self._audio_pipeline_handle.conversation_pipeline
                    self._mode_dispatcher = self._audio_pipeline_handle.mode_dispatcher

                    self._audio_infrastructure_initialized = True
                    _wire_ms = (time.time() - _wire_start) * 1000

                    self._component_status["audio_infrastructure"] = {
                        "status": "running",
                        "message": f"Pipeline wired via bootstrap in {_wire_ms:.0f}ms",
                    }
                    self.logger.info(
                        f"[Kernel] Audio pipeline wired (v238.1) in {_wire_ms:.0f}ms"
                    )

                except Exception as ap_err:
                    self.logger.warning(
                        f"[Kernel] Audio pipeline wiring failed: {ap_err}"
                    )
                    self._component_status["audio_infrastructure"] = {
                        "status": "degraded",
                        "message": f"Wiring failed: {ap_err}",
                    }
```

**Step 2: Update shutdown to use bootstrap**

Replace the audio infrastructure shutdown block (lines 73776-73823) with:

```python
            # =====================================================================
            # v238.1: AUDIO INFRASTRUCTURE SHUTDOWN (via Bootstrap)
            # =====================================================================
            if self._audio_infrastructure_initialized:
                try:
                    from backend.audio.audio_pipeline_bootstrap import shutdown as bootstrap_shutdown
                    if hasattr(self, '_audio_pipeline_handle') and self._audio_pipeline_handle is not None:
                        await asyncio.wait_for(
                            bootstrap_shutdown(self._audio_pipeline_handle),
                            timeout=10.0,
                        )
                except Exception as bs_err:
                    self.logger.debug(f"[Kernel] Bootstrap shutdown error: {bs_err}")

                # AudioBus has its own lifecycle
                if self._audio_bus is not None:
                    try:
                        await asyncio.wait_for(
                            self._audio_bus.stop(), timeout=5.0,
                        )
                        self.logger.info("[Kernel] AudioBus stopped")
                    except Exception as ab_err:
                        self.logger.debug(f"[Kernel] AudioBus stop error: {ab_err}")

                self._audio_infrastructure_initialized = False
```

**Step 3: Add `_audio_pipeline_handle` attribute init**

Near line 59010 where `self._audio_bus = None` is defined, add:

```python
self._audio_pipeline_handle = None  # v238.1: PipelineHandle from bootstrap
```

**Step 4: Commit**

```bash
git add unified_supervisor.py
git commit -m "refactor: replace inline audio wiring with bootstrap module calls"
```

---

### Task 5: Fix `speak_immediate()` to Use AudioBus

`speak_immediate()` in `realtime_voice_communicator.py` spawns `say` subprocess directly, bypassing AudioBus. Add AudioBus path when available.

**Files:**
- Modify: `backend/agi_os/realtime_voice_communicator.py` (around lines 1366-1400)

**Step 1: Add AudioBus-aware speech path**

Before the existing `say` subprocess call (around line 1366), add an AudioBus check:

```python
# Try AudioBus path first (provides AEC reference signal)
try:
    from backend.audio.audio_bus import get_audio_bus_safe
    _bus = get_audio_bus_safe()
    if _bus is not None and _bus.is_running:
        from backend.voice.engines.unified_tts_engine import get_tts_engine
        _tts = await get_tts_engine()
        if hasattr(_tts, 'synthesize'):
            import numpy as np
            audio_data = await _tts.synthesize(text)
            if audio_data is not None:
                if isinstance(audio_data, bytes):
                    audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32767.0
                elif isinstance(audio_data, np.ndarray):
                    audio_np = audio_data
                else:
                    audio_np = None
                if audio_np is not None:
                    sample_rate = getattr(_tts, 'sample_rate', 22050)
                    await _bus.play_audio(audio_np, sample_rate)
                    # Skip the legacy `say` path below
                    speech_duration_ms = (time.time() - speech_start_time) * 1000
                    self._is_speaking = False
                    return  # AudioBus path succeeded
except Exception as bus_err:
    logger.debug(f"[SpeakImmediate] AudioBus path failed, falling back to say: {bus_err}")

# Legacy path: direct `say` subprocess (no AEC reference)
cmd = [
    'say',
    ...
```

**Step 2: Make 300ms delay conditional**

Around line 1397, change:

```python
# Old: unconditional delay
# await asyncio.sleep(0.3)

# New: skip delay in conversation mode (AEC handles echo suppression)
try:
    from backend.core.unified_speech_state import get_speech_state_manager
    _ssm = await get_speech_state_manager()
    if not getattr(_ssm, '_conversation_mode', False):
        await asyncio.sleep(0.3)
except Exception:
    await asyncio.sleep(0.3)  # Fallback: always delay
```

**Step 3: Commit**

```bash
git add backend/agi_os/realtime_voice_communicator.py
git commit -m "fix: route speak_immediate() through AudioBus, conditional 300ms delay"
```

---

### Task 6: Migrate TTS Instantiation Sites to Singleton

7 call sites use `UnifiedTTSEngine()` directly instead of the async singleton `get_tts_engine()`. The singleton already exists at `backend/voice/engines/unified_tts_engine.py:51-85`.

Since most call sites are in `__init__` methods (sync context), and `get_tts_engine()` is async, the fix is to initialize with `None` and lazily acquire the singleton on first use.

**Files:**
- Modify: `backend/core/trinity_voice_coordinator.py` (line 1205)
- Modify: `backend/display/jarvis_computer_use_integration.py` (line 165)
- Modify: `backend/display/vision_ui_navigator.py` (line 804)
- Modify: `backend/ghost_hands/narration_engine.py` (line 620)
- Modify: `backend/voice/jarvis_voice.py` (line 1567)

**Step 1: For each file, apply the lazy singleton pattern**

At each instantiation site, replace:

```python
# Old
self._tts_engine = UnifiedTTSEngine()
```

With:

```python
# New: lazy singleton — acquired on first use
self._tts_engine = None  # Lazy: use _get_tts() to acquire
```

And add a helper method (or inline it at each usage site):

```python
async def _get_tts(self):
    """Get the TTS singleton (lazy init)."""
    if self._tts_engine is None:
        try:
            from backend.voice.engines.unified_tts_engine import get_tts_engine
            self._tts_engine = await get_tts_engine()
        except Exception as e:
            logger.debug(f"TTS singleton unavailable: {e}")
    return self._tts_engine
```

Then at each usage site, change `self._tts_engine.speak(...)` to:

```python
tts = await self._get_tts()
if tts is not None:
    await tts.speak(...)
```

**Step 2: Commit**

```bash
git add backend/core/trinity_voice_coordinator.py backend/display/jarvis_computer_use_integration.py backend/display/vision_ui_navigator.py backend/ghost_hands/narration_engine.py backend/voice/jarvis_voice.py
git commit -m "refactor: migrate 5 TTS instantiation sites to lazy singleton"
```

---

### Task 7: Add Conversation Mode Awareness to Hallucination Guard

`stt_hallucination_guard.py` has no concept of conversation mode. Add a flag so it can adjust thresholds for streaming partial transcripts.

**Files:**
- Modify: `backend/voice/stt_hallucination_guard.py`

**Step 1: Add conversation_mode parameter to verify_transcription**

Find the `verify_transcription` method signature and add a `conversation_mode` parameter:

```python
async def verify_transcription(
    self,
    transcription: str,
    confidence: float,
    audio_data: Optional[bytes] = None,
    engine_results: Optional[List[Dict[str, Any]]] = None,
    context: Optional[str] = "unlock_command",
    conversation_mode: bool = False,  # NEW: relaxed thresholds for streaming
) -> Tuple[VerificationResult, Optional[HallucinationDetection], str]:
```

**Step 2: Add early return for high-confidence conversation transcripts**

At the top of the method body, add:

```python
# In conversation mode, partial transcripts are expected to have
# lower confidence and AEC artifacts. Skip aggressive filtering
# for high-confidence conversation input.
if conversation_mode and confidence > 0.6:
    return (
        VerificationResult.VERIFIED,
        None,
        transcription,
    )
```

**Step 3: Commit**

```bash
git add backend/voice/stt_hallucination_guard.py
git commit -m "feat: add conversation_mode flag to hallucination guard for relaxed streaming thresholds"
```

---

### Task 8: Wire Wake Word to ModeDispatcher

The ModeDispatcher's `handle_transcript()` method is never called by the existing wake word / command processor. Connect it so "JARVIS, let's chat" triggers conversation mode.

**Files:**
- Modify: `backend/audio/audio_pipeline_bootstrap.py` (add to `wire_conversation_pipeline`)

**Step 1: Register transcript hook**

In `wire_conversation_pipeline()`, after creating the ModeDispatcher (step 5), add:

```python
    # 6. Register ModeDispatcher transcript hook on voice orchestrator
    try:
        from backend.agi_os.realtime_voice_communicator import (
            get_voice_communicator,
        )
        _communicator = get_voice_communicator()
        if _communicator is not None and handle.mode_dispatcher is not None:
            _communicator.register_transcript_hook(
                handle.mode_dispatcher.handle_transcript
            )
            logger.info("[Bootstrap] ModeDispatcher registered as transcript hook")
    except Exception as e:
        logger.debug(f"[Bootstrap] Transcript hook registration skipped: {e}")
```

Note: If `register_transcript_hook` doesn't exist on the voice communicator, this step requires adding it. The hook should be called with each final transcript text before command processing, allowing the ModeDispatcher to intercept mode-switching phrases.

**Step 2: Commit**

```bash
git add backend/audio/audio_pipeline_bootstrap.py
git commit -m "feat: wire ModeDispatcher transcript hook to voice communicator"
```

---

### Task 9: Update `__init__.py` Exports

Ensure the new bootstrap module is properly exported.

**Files:**
- Modify: `backend/audio/__init__.py`

**Step 1: Read current exports and add bootstrap**

Add the bootstrap exports to the existing `__init__.py`:

```python
from backend.audio.audio_pipeline_bootstrap import (
    start_audio_bus as bootstrap_start_audio_bus,
    wire_conversation_pipeline,
    shutdown as bootstrap_shutdown,
    PipelineHandle,
)
```

Add to `__all__`:

```python
"bootstrap_start_audio_bus",
"wire_conversation_pipeline",
"bootstrap_shutdown",
"PipelineHandle",
```

**Step 2: Commit**

```bash
git add backend/audio/__init__.py
git commit -m "chore: export audio pipeline bootstrap from __init__"
```

---

### Task 10: Integration Verification

Verify the full system works by checking imports and startup flow.

**Step 1: Verify all imports resolve**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -c "
from backend.audio.audio_pipeline_bootstrap import (
    start_audio_bus, wire_conversation_pipeline, shutdown, PipelineHandle
)
from backend.audio.mode_dispatcher import ModeDispatcher, VoiceMode
from backend.audio.conversation_pipeline import ConversationPipeline, SentenceSplitter
from backend.audio.barge_in_controller import BargeInController
from backend.audio.turn_detector import TurnDetector
print('All imports OK')
"
```

Expected: `All imports OK`

**Step 2: Verify ModeDispatcher accepts constructor args**

```bash
python3 -c "
from backend.audio.mode_dispatcher import ModeDispatcher
md = ModeDispatcher(conversation_pipeline=None, speech_state=None)
print(f'Mode: {md.current_mode.value}')
print('ModeDispatcher constructor OK')
"
```

Expected: `Mode: command` and `ModeDispatcher constructor OK`

**Step 3: Verify SentenceSplitter handles edge cases**

```bash
python3 -c "
import asyncio
from backend.audio.conversation_pipeline import SentenceSplitter

async def test():
    splitter = SentenceSplitter(min_sentence_len=5)

    # Test normal case
    async def tokens1():
        for t in ['Hello', ' world', '. ', 'How', ' are', ' you', '?']:
            yield t

    sentences = []
    async for s in splitter.split(tokens1()):
        sentences.append(s)
    print(f'Normal: {sentences}')
    assert len(sentences) >= 2, f'Expected 2+ sentences, got {len(sentences)}'

    # Test flush
    splitter2 = SentenceSplitter(min_sentence_len=5)
    async def tokens2():
        for t in ['Just', ' one', ' sentence']:
            yield t

    sentences2 = []
    async for s in splitter2.split(tokens2()):
        sentences2.append(s)
    print(f'Flush: {sentences2}')
    assert len(sentences2) == 1

    print('SentenceSplitter OK')

asyncio.run(test())
"
```

Expected: `SentenceSplitter OK`

**Step 4: Final commit with all verification passing**

```bash
git add -A
git status
# Review that only expected files are staged
git commit -m "feat: audio pipeline unification - 17 fixes for end-to-end conversation pipeline

- Create audio_pipeline_bootstrap.py with two-phase lifecycle
- Fix ModeDispatcher constructor, add start/stop, launch conversation loop
- Route TTS through AudioBus for AEC reference signal
- Register StreamingSTT and BargeIn as AudioBus mic consumers
- Replace supervisor inline wiring with bootstrap calls
- Add AudioBus path to speak_immediate()
- Migrate 5 TTS sites to lazy singleton
- Add conversation_mode to hallucination guard
- Fix SentenceSplitter regex edge case
- Conditional 300ms post-speech delay

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Execution Order Summary

| Order | Task | Priority | Est. Time |
|-------|------|----------|-----------|
| 1 | Fix ModeDispatcher API | P0 | 5 min |
| 2 | Fix ConversationPipeline TTS routing | P0 | 5 min |
| 3 | Create Bootstrap Module | P0/P1 | 10 min |
| 4 | Replace Supervisor Inline Wiring | P1 | 5 min |
| 5 | Fix speak_immediate() | P0/P2 | 5 min |
| 6 | Migrate TTS Singletons | P1 | 10 min |
| 7 | Hallucination Guard | P3 | 3 min |
| 8 | Wake Word Hook | P2 | 5 min |
| 9 | Update Exports | Chore | 2 min |
| 10 | Integration Verification | All | 5 min |

**Total estimated: ~55 minutes**
