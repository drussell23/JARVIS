# Audio Pipeline Unification & Enterprise Hardening Design

**Date:** 2026-02-18
**Status:** Approved
**Scope:** 17 fixes across conversation pipeline, supervisor integration, and cross-repo coordination

## Problem Statement

The real-time conversation pipeline has 7 well-implemented layer components (Layer -1 through 6+) that are individually correct but **completely disconnected**. No wiring/bootstrap code exists. No supervisor lifecycle integration exists. The pipeline has never run end-to-end.

Additionally, 6 cross-cutting issues (TTS per-message instantiation, speech path bypassing AudioBus, unconditional delays, etc.) degrade the system even in non-conversation modes.

## Architecture Decision

**Approach 1: Single Bootstrap Module** — `backend/audio/audio_pipeline_bootstrap.py`

Two-phase initialization:
- **Phase 1 (Early):** AudioBus starts before narrator. Fast (~1s).
- **Phase 2 (Late):** ConversationPipeline wired after Intelligence phase provides LLM client. Heavier (~5s).

Follows existing codebase factory patterns (`get_model_serving()`, `get_gcp_vm_manager()`).

## Bootstrap Module API

```python
class AudioPipelineBootstrap:
    async def start_audio_bus(config: DeviceConfig) -> AudioBus:
        """Phase 1: Called early, before narrator."""
        # Creates AudioBus + FullDuplexDevice + AEC
        # Returns immediately after device opens

    async def wire_conversation_pipeline(
        audio_bus: AudioBus,
        llm_client: UnifiedModelServing,
        speech_state: UnifiedSpeechStateManager,
    ) -> PipelineHandle:
        """Phase 2: Called after Intelligence phase."""
        # Creates StreamingSTT -> registers on AudioBus
        # Creates TurnDetector, BargeInController -> registers on AudioBus
        # Gets TTS singleton via get_tts_engine()
        # Creates ConversationPipeline with all refs
        # Creates ModeDispatcher
        # Returns handle with start/stop/health

    async def shutdown(handle: PipelineHandle):
        """Reverse order: Dispatcher -> Pipeline -> STT -> AudioBus."""
```

## Consolidated Fix List (17 Items)

### P0: Critical Path (Pipeline Non-Functional Without These)

| # | Issue | File(s) | Fix |
|---|-------|---------|-----|
| 1 | No wiring/bootstrap code | NEW: `audio_pipeline_bootstrap.py` | Create two-phase bootstrap module |
| 2 | TTS bypasses AudioBus in pipeline | `conversation_pipeline.py` | `_speak_sentence()` routes through `AudioBus.play_audio()` |
| 3 | ModeDispatcher doesn't call `run()` | `mode_dispatcher.py` | `_enter_mode(CONVERSATION)` creates task for `pipeline.run()` |
| 4 | `speak_stream()` has no barge-in cancel | `conversation_pipeline.py` | Use `AudioBus.play_stream(cancel=event)` |
| 5 | `speak_immediate()` bypasses AudioBus | `realtime_voice_communicator.py` | Add AudioBus path when available |

### P1: Degraded Without These

| # | Issue | File(s) | Fix |
|---|-------|---------|-----|
| 6 | BargeInController disconnected | `audio_pipeline_bootstrap.py` | Register as mic consumer, `set_loop()` |
| 7 | StreamingSTT disconnected | `audio_pipeline_bootstrap.py` | Register `on_audio_frame` on AudioBus |
| 8 | `_listen_for_turn()` TurnDetector wiring | `conversation_pipeline.py` | Already correct in current code |
| 9 | No supervisor lifecycle integration | `unified_supervisor.py` | Add Phase 1 (early) + Phase 2 (late) calls |
| 10 | TTS instantiation sites ignore singleton | 7 call sites | Migrate to `get_tts_engine()` |

### P2: Production Bugs

| # | Issue | File(s) | Fix |
|---|-------|---------|-----|
| 11 | 300ms unconditional delay | `realtime_voice_communicator.py:1397` | Conditional on `not conversation_mode` |
| 12 | Wake word -> ModeDispatcher unconnected | `audio_pipeline_bootstrap.py` | Register transcript hook |
| 13 | SentenceSplitter regex edge case | `conversation_pipeline.py` | Handle token boundary without trailing space |

### P3: Hardening

| # | Issue | File(s) | Fix |
|---|-------|---------|-----|
| 14 | Hallucination guard no conversation awareness | `stt_hallucination_guard.py` | Add conversation_mode flag |
| 15 | Duplicate `register_with_websocket_service()` | TBD | Remove duplicate |
| 16 | WebSocket endpoint incomplete | TBD | Wire ConversationPipeline |

## Data Flow (End-to-End After Fix)

```
User speaks
  -> FullDuplexDevice (Layer -1, 48kHz)
  -> AudioBus (Layer 0): downsample 48->16kHz, AEC
  -> Registered consumers:
     1. StreamingSTTEngine.on_audio_frame()
        -> VAD -> buffer -> faster-whisper
        -> StreamingTranscriptEvent (partial + final)
     2. BargeInController.on_vad_speech_detected()
        -> If JARVIS speaking: cancel_event.set() + flush playback
  -> ConversationPipeline._listen_for_turn()
     -> Reads from StreamingSTT.get_transcripts()
     -> TurnDetector.on_vad_result() -> "turn_end"
  -> ConversationPipeline._generate_and_speak_response()
     -> session.get_context_for_llm() -> messages array
     -> ModelRequest(messages, system_prompt, stream=True)
     -> self._llm_client.generate_stream(request)
        -> UnifiedModelServing tier chain:
           1. PRIME_API (jarvis-prime on GCP, /v1/chat/completions, SSE)
           2. PRIME_LOCAL (local GGUF via llama-cpp-python)
           3. CLAUDE (Anthropic API fallback)
     -> SentenceSplitter: tokens -> complete sentences
     -> TTS engine (Piper via singleton): sentence -> audio chunks
     -> AudioBus.play_audio(): chunks -> PlaybackRingBuffer -> FullDuplexDevice
        (AEC gets reference signal from playback buffer)
  -> User hears JARVIS response

Barge-in:
  User speaks during JARVIS response
  -> AEC strips JARVIS voice from mic input
  -> BargeInController detects speech on cleaned signal
  -> cancel_event.set() -> SentenceSplitter stops
  -> AudioBus.flush_playback() -> silence within one frame
  -> Pipeline switches back to _listen_for_turn()
```

## Cross-Repo Integration (Already Working)

- **TrinityOrchestrator** already manages jarvis-prime and reactor-core as subprocesses
- **UnifiedModelServing** already routes through PRIME_API -> PRIME_LOCAL -> CLAUDE
- **jarvis-prime** serves `/v1/chat/completions` with SSE streaming
- **No additional cross-repo wiring needed** - the conversation pipeline connects via `self._model_serving.generate_stream()` which is the existing tier chain

## Files Modified

| File | Action | Lines Changed (est.) |
|------|--------|---------------------|
| `backend/audio/audio_pipeline_bootstrap.py` | **CREATE** | ~250 |
| `backend/audio/conversation_pipeline.py` | EDIT | ~30 |
| `backend/audio/mode_dispatcher.py` | EDIT | ~15 |
| `backend/agi_os/realtime_voice_communicator.py` | EDIT | ~20 |
| `unified_supervisor.py` | EDIT | ~30 |
| `backend/voice/engines/unified_tts_engine.py` | EDIT (verify singleton) | ~5 |
| `backend/core/trinity_voice_coordinator.py` | EDIT | ~3 |
| `backend/display/jarvis_computer_use_integration.py` | EDIT | ~3 |
| `backend/display/vision_ui_navigator.py` | EDIT | ~3 |
| `backend/ghost_hands/narration_engine.py` | EDIT | ~3 |
| `backend/voice/jarvis_voice.py` | EDIT | ~3 |
| `backend/voice/stt_hallucination_guard.py` | EDIT | ~15 |

## Verification Criteria

1. `python3 unified_supervisor.py` boots with AudioBus starting before narrator
2. ConversationPipeline wires after Intelligence phase
3. "JARVIS, let's chat" enters conversation mode
4. Spoken words are transcribed via StreamingSTT
5. LLM response streams through SentenceSplitter -> TTS -> AudioBus
6. Barge-in cancels TTS mid-sentence
7. "goodbye" exits conversation mode
8. All TTS output routes through AudioBus (AEC has reference signal)
9. `speak_immediate()` uses AudioBus when available
10. No per-message TTS engine instantiation
