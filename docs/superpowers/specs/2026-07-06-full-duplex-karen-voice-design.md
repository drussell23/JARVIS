# Spec: Full-Duplex Karen — Conversational Voice of a Living Organism

**Date:** 2026-07-06
**Author:** Derek J. Russell (design conducted with Claude)
**Status:** Design approved — Sprint 1 plan next
**Scope tag:** Product Program · Karen Voice (Option C: full duplex)

---

## 1. Context

Karen is O+V's spoken persona. Today she is a **one-way narrator** driven by 13
static mail-merge templates in `governance/comms/narrator_script.py` — the same
sentences every time ("Derek, I'm analyzing {file}. Goal: {goal}."). The goal
is to turn her into a **full-duplex conversational colleague**: she narrates the
FSM proactively *and* the operator can talk to her, interrupt her mid-sentence,
and direct builds by voice — ChatGPT/Gemini-voice-grade interaction wrapped
around a proactive autonomous organism.

### The stack already exists (this is wiring, not greenfield)

Confirmed in `backend/voice/`:

- **Neural TTS** — `UnifiedTTSEngine` (`engines/unified_tts_engine.py`): Piper
  (neural, default) + Coqui (voice cloning). Not just macOS `say`.
- **Streaming STT** — `streaming_stt.py` `StreamingSTTEngine`: `webrtcvad` +
  Whisper (local/GCP ensemble), emits `StreamingTranscriptEvent` (partial +
  final) on an `asyncio.Queue`, tracks `_speech_start_ms`, from **AEC-cleaned**
  frames (Karen won't trigger on her own voice).
- **Preemptable playback** — `unified_voice_orchestrator` tracks
  `self._current_process` with `.terminate()`/`.kill()` and a **global playback
  mutex**. Barge-in = kill + flush.
- **Wake word** — Picovoice primitives present (unused in always-listening mode
  but retained as an optional gate).
- **Persona/context** — `jarvis_personality_adapter.py`,
  `contextual_message_generator.py`.
- **Brain** — existing DW/Claude providers. **DW/Claude are the brain (text);
  the voice stack is the ears+mouth. No new voice API is added.**

### Persona (locked)

Australian neural voice. Tone: **concise, dryly witty, highly technical, zero
fluff, maximal signal-to-noise.** She talks like a senior engineer who
respects your time.

### Interaction model (locked)

**Always-listening open mic** + conversational barge-in (no wake word to start a
turn; barge-in any time).

---

## 2. Architecture — one arbiter, two speakers, one ear

```
   ┌─────────────────────── VoiceDuplexArbiter ───────────────────────┐
   │  owns audio device (global mutex) · async state machine          │
   │  states: LISTENING · USER_SPEAKING · KAREN_SPEAKING · THINKING   │
   └───────▲───────────────────▲──────────────────────┬──────────────┘
           │ speech-start/final │ proactive emit        │ play/preempt/flush
   ┌───────┴────────┐   ┌───────┴─────────┐    ┌────────▼─────────────┐
   │ StreamingSTT   │   │ CommProtocol    │    │ UnifiedTTSEngine     │
   │ (webrtcvad +   │   │ FSM events ─►   │    │ (Piper/Coqui) ─►     │
   │  Whisper, AEC) │   │ KarenSpeech     │    │ orchestrator afplay  │
   └───────┬────────┘   │ Synthesizer     │    │ (_current_process)   │
     user speech        └─────────────────┘    └──────────────────────┘
           │
   final ──┴──► IntentRouter ──► (A) conversational Q → LLM → speak
                              └─► (B) build command → O+V intake → build
```

Two speech sources (reactive user-response + proactive organism narration), one
mouth, arbitrated by a single coordinator.

---

## 3. The VoiceDuplexArbiter (the heart — mandate #4)

A single async coordinator owning the audio device and resolving every
collision via **priority + state**.

**States:** `LISTENING`, `USER_SPEAKING`, `KAREN_SPEAKING`, `THINKING`.

**Collision matrix:**

| Event during… | LISTENING | KAREN_SPEAKING | USER_SPEAKING |
|---|---|---|---|
| **User speech-start** | → USER_SPEAKING | **barge-in**: kill+flush → USER_SPEAKING | (continue) |
| **Proactive emit** | speak | preempt if *critical* (approval), else **queue** | **queue** (never cut off user) |
| **2nd proactive emit** | priority-queue + coalesce same-topic | " | " |

**Priority ladder:** user barge-in > user-command response > proactive-critical
(needs approval) > proactive-info (FYI narration).

**Invariants (bulletproof):**
- **Single device owner** (reuse the global playback mutex) → no buffer
  collisions.
- **FSM never blocks on audio** — proactive emits are fire-and-forget onto a
  **bounded drop-oldest** queue (the `KarenPreambleVoice` shed pattern). If audio
  is busy, the organism keeps running; Karen coalesces/catches up. (Same
  principle as the v41 watchdog: the guarded system never blocks on the guard.)
- **Idempotent barge-in** — `kill()` on a finished process is a no-op.
- **Pure asyncio** — all transitions via `asyncio.Event`/`Condition`/`Queue`.
  No blocking audio reads, no `sleep` loops.
- **Ducking** optional (gain attenuation when the engine supports it);
  queue/preempt is the guaranteed primary.
- **Fault isolation** — any audio failure logs at DEBUG and returns; never
  propagates into the FSM.

---

## 4. Dynamic speech (mandate #1)

New `KarenSpeechSynthesizer` replaces the template dict:
- **Context in:** live FSM phase + op-ledger (target files, risk tier, provider,
  diff summary) via `CommProtocol` events + persona + time-of-day/prefs
  (`jarvis_personality_adapter`).
- **Persona system prompt:** the locked Australian/dry-witty/technical/high-SNR
  voice.
- **LLM out (streamed):** DW primary (cheap/fast) → sentence-chunked to TTS as
  sentences complete → low latency. Claude for high-stakes lines.
- **Zero-latency fillers:** tiny generated acks ("on it") cover LLM first-token
  latency — NOT the retired narration templates.
- **RETIRED:** `narrator_script.py` `SCRIPTS` + `_REQUIRED_KEYS` template tables.

---

## 5. Integration with the organism (voice → build)

- User build command → existing intake (`voice_command_sensor` /
  `UnifiedIntakeRouter`) → O+V governance → Karen narrates via `CommProtocol`.
- Mid-flight steering ("use a token bucket") → barge-in → injected as a
  `conversation_bridge` signal / clarification into the running op.

---

## 6. Sprint breakdown (each landed + verified independently)

| Sprint | Deliverable | Real audio? |
|---|---|---|
| **1 — Arbiter core** | `VoiceDuplexArbiter` state machine + concurrency model, TDD against a **fake audio device** (injected STT events + mock playback process). The hardest part, de-risked first. | ❌ (mocked) |
| **2 — Dynamic speech** | `KarenSpeechSynthesizer` (LLM + persona + FSM context); retire `narrator_script.py` templates. | ❌ |
| **3 — Real-engine wiring** | Wire `StreamingSTTEngine` + `UnifiedTTSEngine` + orchestrator `preempt()`/`flush()` hook into the arbiter; always-listening loop. | ✅ (local interactive) |
| **4 — Voice → build loop** | IntentRouter → intake; mid-flight steering via conversation-bridge. | ✅ |

This spec's plan covers **Sprint 1 only**. Sprints 2–4 get their own plans.

---

## 7. Sprint 1 scope (arbiter core) — precise boundary

**In:** the `VoiceDuplexArbiter` class — state machine, priority ladder,
collision resolution (barge-in / queue / duck), bounded drop-oldest proactive
queue, device-mutex ownership, all pure-async — plus a **fake audio device**
seam (`AudioSink`/`AudioSource` protocols) so every behavior is unit-testable
with zero mic/speaker. Kill switches. Telemetry counters.

**Out (later sprints):** real STT/TTS engines, LLM speech synthesis, voice→build
intake wiring, the always-listening capture loop.

**Seams (dependency inversion for testability):**
- `PlaybackHandle` protocol — `.play(audio)`, `.preempt()`, `.is_active` —
  real impl wraps `unified_voice_orchestrator._current_process`; fake impl is a
  controllable stub.
- `SpeechSource` protocol — yields `SpeechRequest(priority, text/audio,
  coalesce_key)` — real impls are the synthesizer + user-response path; fake
  emits scripted requests.
- `BargeSignal` protocol — an async event stream of speech-start — real impl
  taps `StreamingSTTEngine._speech_start_ms`; fake fires on command.

---

## 8. Bulletproofing summary

| Failure mode | Structural protection |
|---|---|
| Proactive emit collides with user command | arbiter queues proactive (user priority) — never a buffer race |
| Barge-in mid-playback | `.kill()` + flush, idempotent |
| Audio device contention | single-owner global mutex |
| FSM stalls waiting on audio | fire-and-forget bounded drop-oldest queue |
| Karen hears herself | AEC-cleaned STT frames (existing) |
| Engine/import failure | fault-isolated (DEBUG + return), never reaches FSM |
| Runaway proactive spam | coalesce same-topic + min-gap (existing `KarenPreambleVoice` clock) |

---

## 9. Kill switches (independently gateable)

`JARVIS_KAREN_VOICE_ENABLED` (master) · `JARVIS_KAREN_CONVERSATION_ENABLED`
(reactive/duplex) · `JARVIS_KAREN_BARGE_IN_ENABLED` · `JARVIS_KAREN_PROACTIVE_ENABLED`.
All default **false** during build; graduate per sprint.

---

## 10. Open questions (resolved)

1. Persona — **RESOLVED**: Australian, concise/dry-witty/technical, high SNR.
2. Mic — **RESOLVED**: always-listening open mic + barge-in.
3. Voice engine — Piper (local/free) default; Coqui-clone or premium deferred.
