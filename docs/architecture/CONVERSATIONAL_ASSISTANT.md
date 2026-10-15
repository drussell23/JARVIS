# JARVIS as a Conversational Assistant

**Status:** living document, started 2026-10-15. Update it when a roadmap item lands.
**Scope:** how JARVIS holds a conversation with its user: turn-taking, voice, memory, interruption and tool use. It compares JARVIS with how frontier assistants (Anthropic, OpenAI, Google DeepMind) do this, records what JARVIS does today and why, and lists what remains, in order.

This document does **not** cover Ouroboros + Venom (the self-development engine). See `OUROBOROS_VENOM_PRD.md` and the research paper for that.

---

## 1. How frontier assistants hold a conversation

This section draws only on what the companies have published about their products and APIs: OpenAI's Realtime API, Google's Gemini Live API, and the public behaviour of ChatGPT, Claude and Gemini. Their internal implementations are not public, and nothing here claims to describe them.

### 1.1 The model sees the whole conversation every turn

Text chat has no hidden dialogue state. On every turn the application sends the model its instructions (the system prompt), the entire conversation so far, and any tool results. Follow-ups ("which one is on Teams?"), pronouns ("it", "that one") and clarifying questions all come from the model reading that history. No rule says "if the user says *yes*, then…": the model sees what it offered one turn earlier.

### 1.2 Clarifying questions and offers come from training, not code

The models are trained, through instruction tuning and reinforcement learning from human feedback, to ask when a request is ambiguous and to offer a sensible next step. The product's instructions then shape that behaviour (for example, "offer to take the action when it would help"). No separate component decides when to offer something.

### 1.3 Voice is built for real-time conversation

- **Speech in, speech out.** OpenAI's Realtime API and Google's Gemini Live API use models that take audio in and produce audio out directly. That replaces the three-hop chain of speech-to-text, then a text model, then text-to-speech, which removes most of the delay, and the model hears tone and hesitation.
- **Streaming.** The reply starts playing within a few hundred milliseconds, while the rest of it is still being generated.
- **Turn detection.** The system decides when the user has finished speaking. It uses voice-activity detection (silence), and in newer versions also whether the sentence *sounds* finished (OpenAI calls this "semantic VAD"). No wake word is needed once a conversation is under way.
- **Echo cancellation.** Clients capture the microphone through WebRTC with the browser's echo canceller on (`getUserMedia({audio: {echoCancellation: true}})`). The assistant's own voice is subtracted from the mic signal, so it rarely hears itself.
- **Interruption ("barge-in").** When the user talks over the assistant, playback stops at once and the client tells the model how much of its reply the user actually heard (in the Realtime API, a truncate event that carries the audio position). The conversation record then holds the cut-off reply, so the model can continue naturally ("sure, Friday then…").

### 1.4 Tools run inside the conversation

Checking a calendar or opening an app is a tool the model calls in the middle of its turn. The tool's result goes back into the conversation, and the model decides what to say about it. Tool use is part of the same turn as speaking.

---

## 2. How JARVIS holds a conversation today

```
 browser (web app, JarvisVoice.js)                     backend (FastAPI + UnifiedCommandProcessor)
 ─────────────────────────────────                     ──────────────────────────────────────────
 Web Speech API recognition ──final transcript──▶  WS "command" ──▶ command_identity scopes:
   ▲    decideHeard(): echo / user / interrupt        │              request id · conversation · voice
   │    (utils/bargeIn.js, policy /audio/barge-in)    ▼
   │                                               IntentClassifier.classify_async  (the Mind + conversation)
   │                                                  │
   │                                               RuntimeTaskOrchestrator → agents / tools
   │                                               (google_workspace_agent: detector + Mind dates + conversation)
   │                                                  │
   │                                               composer (workspace data → spoken reply)
   │                                                  │
   │   ◀── voice_line {text, requestId} ──────── safe_say ──▶ command_voice sink (client)  or host speakers
   │                                                  │
   └── <audio> ◀── /audio/speak (render_speech: persona voice, engine order shared with the host)
```

### 2.1 A command and its context

| Concern | Where | What it does |
|---|---|---|
| Command identity | `backend/core/command_identity.py` (`command_scope`) | A command's id follows it from the WebSocket through every await, so a REST resend joins the run instead of repeating it. |
| Conversation | `UnifiedContext.record_turn` / `recent_turns` (`backend/api/unified_command_processor.py`); `conversation_scope`, `conversation_text`, `conversation_preamble` | Each exchange is recorded. The recent turns, back to the first silence longer than `JARVIS_CONVERSATION_IDLE_S`, travel with the command. |
| Who decides with the conversation | intent classifier, workspace detector, workspace composer | A follow-up is decided by the Mind *with* the exchange in front of it. |
| Where its voice is heard | `command_voice` sink, set by `UnifiedWebSocketManager.voice_sink` | A client that declared `voice_playback` hears every line said for its commands (reply, acknowledgement, descriptions). Otherwise the host's speakers say them. |

**Design note: no restatement layer.** An earlier version had the Mind rewrite a follow-up as a standalone request before classifying it. The local 30B dropped "tomorrow" and wrote today's date into unrelated email questions. A lossy rewrite is worse than giving each decider the raw exchange, so it was removed (2026-10-14).

### 2.2 Answers from the user's own data

- Gmail and Calendar go through `GoogleWorkspaceClient`. `verify_workspace` reads once through each service, and both the sign-in script and the pre-flight checklist use it.
- Each calendar event carries its meeting platform: `event_meeting()` reads the calendar's conference name, or the host of the join link for a pasted invite. The composer says "on Microsoft Teams" in words and never invents a platform.
- Dates are computed, not left to the model: `resolve_day`, `local_day_window`, and a table of upcoming dates in the detector's prompt. The composer gets local spoken times, one `when` span per event.

### 2.3 Voice

- **Rendering:** `TTSEngine.render` / `_render_file` in `backend/core/trinity_voice_coordinator.py`. Speaking is render, then play. A client is served the same render from `/audio/speak` (`render_speech`), with the same engines, order (`_ordered_engines`) and persona, so the browser hears the voice the host's speakers would.
- **One speech primitive:** `safe_say` (`backend/core/supervisor/unified_voice_orchestrator.py`). Mute outranks everything, then the command's client voice, then the host path.
- **Client playback:** `JarvisVoice.js` has a speech queue and a speech *generation* counter. Every stop advances the generation, so a fetch, playback callback, `play()` rejection or browser-voice fallback started under an older generation does nothing.

### 2.4 Turn-taking, barge-in and echo

| Piece | Where | Behaviour |
|---|---|---|
| Endpointing | `JarvisVoice.js` recognition handler | A command is the recogniser's **final** result: the user has stopped speaking. Interim results are never sent; they cut "what's 17 times 23" into three commands. |
| Barge-in policy | `backend/core/barge_in_policy.py`, `GET /audio/barge-in` | The operator's thresholds, read fresh per request. |
| The decision | `frontend/src/utils/bargeIn.js` (`decideHeard`) | **interrupt** (the user talks over JARVIS), **user** (just after JARVIS, within its echo window), **ignore** (JARVIS's echo, too little heard, or cooldown) or **pass** (normal handling). |
| Echo test | `echoOverlap` | A transcript is JARVIS's echo when most of its words are words JARVIS is saying. This is containment, not symmetric similarity, because an echo is a fragment of a long line. An interim transcript cut mid-word still matches. |
| Echo tail | `JARVIS_BARGE_IN_ECHO_TAIL_MS` | Over Remote Desktop a line comes back through the client machine's mic and is finalised 1–2 s *after* playback ends. Results inside that window are judged too. |
| Judged once | `bargeInStateRef.utteranceActive` | Once an utterance is the user's, its later interim results are not re-judged. Re-judging made the user interrupt their own sentence. |
| Interruption | `bargeIn()` | Playback stops, the rest of that command's lines are silenced by `requestId`, and the utterance becomes the next command. |

### 2.5 Speaking while writing

| Piece | Where | What it does |
|---|---|---|
| Splitter | `backend/core/speech_segments.py` (`SentenceSplitter`, `sentences`) | Tokens or finished text become sentences. One splitter, also used by the audio pipeline. |
| Voicing | `voice_while_writing(chunks, source=...)` | With a client voice, each finished sentence is said as soon as it is written and recorded with `note_voiced`. Without one (the host's multi-worker queue would not keep order), the text is only collected and said whole at the end. |
| No double speech | `voiced_scope` / `voiced_covers` (in `process_command`) | A reply already voiced in full is marked `spoken`, so the transport does not say it again. |
| One voice per command | `command_identity.in_command()` | Inside a user's command, the reply speaks (plus one acknowledgement when slow). Step narrators (the workspace agent's `_narrate`, the task orchestrator's "Done.") stay silent. |
| Frames | `UnifiedWebSocketManager.voice_sink` | A whole line reaches the page one sentence per `voice_line`. |
| Page | `frontend/src/utils/speechAudio.js` | `createSpeechAudioFetcher` fetches queued sentences' audio while the current one plays (`take` / `prefetch` / `abortAll`). There is one `sanitizeForSpeech`. A stop cancels every fetch in flight. |

**Measured (2026-10-15, real calendar).** For "Do I have any meetings tomorrow?", the first sentence went to the voice at 3.83 s and the last at 4.05 s; before, the whole reply reached the voice at ≈4.05 s and only then rendered (2.1 s for the whole text). A follow-up ("which one is on Google Meet?") voiced its first sentence at 1.89 s. Rendering one sentence takes ≈0.7 s warm (first audio bytes at 0.5 s; streaming the audio itself would save only ≈0.2 s, so it was not done).

### 2.6 The pre-flight checklist

Before JARVIS declares itself ready, `backend/core/flight_check.py` *exercises* each capability the user relies on:
- a real generation;
- a silent voice render;
- a real read of Gmail, Calendar and Contacts;
- the abilities usable on this host.

The supervised services count as items too. One report feeds the terminal, the loading page, the readiness tier and the spoken completion line.

---

## 3. Gap analysis

| Piece | Frontier assistants | JARVIS (2026-10-15) | Gap |
|---|---|---|---|
| Conversation memory | Full history every turn | Recent turns reach the classifier, the workspace detector and the composer | The general answer path does not receive the history yet. An interrupted reply is recorded whole. |
| Pipeline | Speech in, speech out | Browser speech recognition → text → local 30B → edge-tts | Three hops. No tone or prosody reaches the model. |
| Streaming | First audio in hundreds of ms | The reply is spoken a sentence at a time while it is written; the page fetches each sentence's audio while the one before plays | Rendering still costs ≈0.7–1.1 s per request (edge-tts round trip). |
| Turn detection | Voice activity + semantic end-of-turn | The recogniser's final result | No semantic end-of-turn, and the wake word is needed between turns. |
| Echo cancellation | WebRTC echo canceller on the mic | Text-level echo judgement plus echo tail | The browser's speech recognition API exposes no echo-cancellation control. |
| Interruption | Stop, and truncate the record to what was heard | Stop, and silence the rest of the command | The record is not truncated (roadmap 3). |
| No wake word mid-conversation | Yes | Only for approval prompts | Roadmap 4 (step 3). |
| Offers / clarifying questions | From the model | Not instructed | Roadmap 4 (step 4). |
| Long-term memory | Product memory features | Pieces exist elsewhere (O+V memory); not in conversation | Not planned yet. |

### Measured latency of one command

"Do I have any meetings tomorrow?", 2026-10-14 23:58:

| Stage | Time |
|---|---|
| Intent classification (incl. first-use init) | 1.9 s |
| Calendar fetch (agent + detector) | 1.0 s |
| Composer (incl. ≈0.5 s lost: the legacy client tried an absent local GGUF model and DoubleWord before J-Prime) | 1.0 s |
| Full-reply render (edge-tts, 349 chars; ≈1.1 s of it is fixed per request) | 2.1 s |
| **Silence before the first word** | **≈6 s** |

---

## 4. Is the local 30B capable of this?

The model is **Qwen3-Coder-30B-A3B**: a mixture-of-experts model with about 30 B parameters, of which about 3 B are active per token. It is served by J-Prime (llama.cpp, one RTX 5090, 32k context).

**It does well at:**
- intent decisions and tool choice (≈0.7–1.1 s each);
- structured JSON output;
- summarising the user's real data;
- short factual answers.

**It is weaker at:**
- nuanced conversation;
- careful multi-step reasoning;
- breadth of knowledge;
- following many rules at once.

Seen live:
- it dropped "tomorrow" when restating a question;
- it once attributed the wrong platform to a meeting;
- it had no native audio.

It is also a **coding** model, chosen for O+V, not for dialogue.

**Hardware coupling:**
- the 30B at 32k context needs about 21 GB of GPU memory;
- the vision model about 7.8 GB;
- the host's other GPU users about 8.5 GB.

That exceeds the 5090's 32 GB, so the models swap. Using vision evicts the Mind.

**Conclusion.** Most of what makes a frontier assistant *feel* conversational is engineering around the model: streaming, echo cancellation, turn-taking, history and interruption records. JARVIS can have all of it with this model. What the model limits is the depth and reliability of what is said. A dialogue-tuned model of the same class, such as an instruct variant of Qwen3-30B-A3B, would fit the same budget. Both 30Bs cannot be resident together, so the assistant and O+V would have to share or swap the slot; that is the operator's decision. The paid lanes (`backend/core/ouroboros/governance/paid_lanes.py`) exist for the hardest requests but are switched off.

---

## 5. Roadmap, in order

| # | Item | Status |
|---|---|---|
| 0a | Voice heard where the user is (client voice, one renderer) | Done, a586d50838 |
| 0b | Barge-in (policy, echo judgement, stop and silence) | Done, 937805cf14 |
| 0c | Endpointing, echo tail, utterance judged once, acknowledgement shown | Done, cdf9922266 |
| 0d | Pre-flight checklist; router status probe shares the request deadline | Done, 63523dfae1 |
| 1 | **Stream the reply sentence by sentence.** The composer writes through `PrimeRouter.generate_stream`; `speech_segments.voice_while_writing` says each finished sentence to the client voice; `command_identity.voiced_scope` records it and the command's end marks the reply spoken; the page prefetches queued sentences (`utils/speechAudio`). Step narrators stay silent inside a command (`in_command`). | Done (see §2.5) |
| 2 | **Mic through WebRTC with echo cancellation, speech-to-text on the host.** The fix for echo at its source, replacing the browser's speech recognition. | Planned |
| 3 | **Interruption recorded in the conversation.** The client reports how much of the reply was heard, and the turn is truncated. | Planned |
| 4 | **Conversation mode.** A listen window after every reply (setting, not constant), and the Mind instructed that it may offer the next step and act on "yes" from the exchange (wire `pending_payload` for email sends). | Planned |
| 5 | Dialogue-tuned model and memory across days. | Operator decision |

---

## 6. Settings

| Setting | Default | Meaning |
|---|---|---|
| `JARVIS_CONVERSATION_TURNS` | 12 | Turns kept in the exchange |
| `JARVIS_CONVERSATION_IDLE_S` | 600 | A pause longer than this starts a new conversation |
| `JARVIS_CONVERSATION_TURN_CHARS` | 600 | Per-turn characters passed to deciders |
| `JARVIS_WORKSPACE_DAYS_AHEAD` | 14 | Days in the detector's date table |
| `JARVIS_ACK_AFTER_S` | 2.5 | A command still running after this says a short acknowledgement |
| `JARVIS_BARGE_IN_ENABLED` | true | Barge-in master switch (shared with the host detector) |
| `JARVIS_BARGE_IN_MIN_WORDS` | 1 | Words heard before an interruption is judged |
| `JARVIS_BARGE_IN_ECHO_OVERLAP` | 0.6 | Share of heard words found in JARVIS's lines that makes it echo |
| `JARVIS_BARGEIN_COOLDOWN_MS` | 500 | Ignore further interruptions for this long after one |
| `JARVIS_BARGE_IN_ECHO_TAIL_MS` | 2500 | Echo window after JARVIS stops speaking |
| `JARVIS_TTS_GLOBAL_TIMEOUT_SECONDS` | 8 | `/audio/speak` render limit |
| `JARVIS_SPEECH_MIN_SENTENCE_CHARS` | 10 | Shortest piece said on its own |
| `JARVIS_PRIME_STATUS_PROBE_S` | 2 | J-Prime status probe limit when a request has no deadline |
| `JARVIS_FLIGHT_CHECK_*` | see `flight_check.py` | Pre-flight checklist |

---

## 7. Operating notes

- **Remote use (Mac → Windows over Remote Desktop):** in the Windows App, open the PC's **Configure** and turn on **Microphone**, then reconnect. Microphone redirection only applies to a new connection. The session then has a second "Remote Audio" endpoint (the mic).
- **Headphones** remove the echo at its source. Until roadmap 2 lands, they are the most effective single measure on a remote session.
- **Diagnosing voice:** the browser console's `✋ [Barge-in]` lines show interruptions, and `👂 [Barge-in]` lines show what was ignored and why (echo, undecided, cooldown) with the overlap measured.

## 8. Failure modes found in live runs (do not regress)

| Date | Symptom | Root cause | Fix |
|---|---|---|---|
| 10-14 | "Which one is on teams?" answered from the screen | The conversation history had no writer; deciders saw only the words | `record_turn` + `conversation_scope` |
| 10-14 | Browser voice ≠ host voice; silent on Windows | `/audio/speak` raced two macOS-`say` strategies with "Daniel" named in code | One renderer, `render_speech` |
| 10-15 | One sentence became three commands | Confident interim transcripts were sent as commands | Final results only |
| 10-15 | "let me check your account" ran as a command | JARVIS's acknowledgement echoed back and was finalised after playback | Echo tail, judged with `decideHeard` |
| 10-15 | A live 30B was routed as "down" during boot | The router's 2 s status probe timed out under load | The probe shares the request deadline |
| 10-15 | Local streaming never worked | `PrimeClient` posted to `/generate/stream` (no such route), read a `content` field no chunk carries, and took the httpx branch for an aiohttp session | One chat endpoint; OpenAI SSE `choices[0].delta.content` to `[DONE]`; error events raise |
| 10-15 | "Let me check your calendar.", "Done." and inbox statistics were heard with nothing on screen | Step narrators spoke inside the command, as a second voice over the reply | `in_command()`: the reply speaks |
| 10-15 | The composer lost ≈0.5 s per call | The legacy client tried an absent GGUF model and DoubleWord before J-Prime | The composer streams through the router every command uses |
