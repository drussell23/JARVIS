# Voice-First Interactive Conversation — The JARVIS Experience

## "Just talk to me, sir."

**Author:** Derek J. Russell
**Date:** March 2026
**Status:** Architecture Specification
**Prerequisite:** All 7 JARVIS tiers (complete), Voice biometric auth (complete)

---

## Vision

The keyboard is optional. The primary interface is voice.

Tony Stark doesn't type commands into a terminal. He talks to JARVIS while building the suit, while driving, while fighting. JARVIS listens continuously, understands context, responds naturally, asks clarifying questions, pushes back when Tony is wrong, and takes initiative when Tony is busy.

This is the final capability that transforms Ouroboros from "an autonomous system you monitor" into "a companion you converse with."

---

## Current State

| Component | Status | What It Does |
|---|---|---|
| ECAPA-TDNN | Active | Verifies it's Derek speaking (85% threshold) |
| Continuous audio capture | Active | FullDuplexDevice captures audio 24/7 |
| STT (Speech-to-Text) | Active | Converts speech to text for VoiceCommandSensor |
| VoiceCommandSensor | Active | Routes voice commands into Ouroboros pipeline |
| safe_say() | Active | TTS output via macOS `say` + `afplay` |
| VoiceNarrator | Active | Narrates pipeline events (WHAT happened) |
| ReasoningNarrator | Active | Explains WHY decisions were made |
| PersonalityEngine | Active | 5 emotional voice states |
| Speech gate | Active | Prevents TTS/capture overlap |

**What's missing:** The conversational loop. Currently:
```
Derek speaks → STT → VoiceCommandSensor → Ouroboros pipeline → result narrated
```

What we need:
```
Derek speaks → STT → ConversationManager → understands context
  ├─ Simple question? → JARVIS responds immediately (no pipeline)
  ├─ Code task? → routes to Ouroboros pipeline → narrates result
  ├─ Clarification needed? → JARVIS asks a question → waits for answer
  ├─ Multi-turn dialogue? → maintains context across turns
  └─ Proactive? → JARVIS speaks FIRST when it has something to say
```

---

## Architecture: ConversationManager

### The Voice Loop

```
┌──────────────────────────────────────────────────────────────┐
│                    VOICE-FIRST LOOP                           │
│                                                              │
│  ┌─────────┐   ┌──────────┐   ┌──────────────────────────┐  │
│  │ Ears    │──▶│ STT      │──▶│ ConversationManager      │  │
│  │ (ECAPA) │   │ (Whisper)│   │                          │  │
│  └─────────┘   └──────────┘   │  1. Classify utterance   │  │
│                               │  2. Check if follow-up   │  │
│                               │  3. Route to handler     │  │
│                               │  4. Generate response    │  │
│  ┌─────────┐   ┌──────────┐  │  5. Speak response       │  │
│  │ Mouth   │◀──│ TTS      │◀─│  6. Listen for follow-up │  │
│  │ (afplay)│   │ (say)    │   └──────────────────────────┘  │
│  └─────────┘   └──────────┘                                  │
│                                                              │
│  Context Window (in-memory, per-session):                    │
│  ├─ Last 10 turns of conversation                            │
│  ├─ Current Ouroboros operation status                        │
│  ├─ Active emergency level                                   │
│  ├─ Personality state                                        │
│  └─ Recent predictions                                       │
└──────────────────────────────────────────────────────────────┘
```

### Utterance Classification (Deterministic)

| Utterance Type | Example | Handler | Needs Pipeline? |
|---|---|---|---|
| **Greeting** | "Hey JARVIS" | Personality-aware greeting | No |
| **Status query** | "How are things?" | Status report from all tiers | No |
| **Simple question** | "What time is it?" | Direct answer | No |
| **Code question** | "What does entropy_calculator do?" | Read file + summarize | No (read-only) |
| **Code task** | "Fix the test failures" | Full Ouroboros pipeline | Yes |
| **Confirmation** | "Yes, do it" / "No, cancel" | Resume/cancel pending op | No |
| **Clarification response** | "The voice_unlock module" | Feed into pending question | No |
| **Feedback** | "That's wrong" / "Perfect" | Negative constraint / success pattern | No |
| **Emergency** | "Stop everything" | HOUSE PARTY protocol | No |
| **Ambient/noise** | (background chatter) | Ignore (below confidence) | No |

### Multi-Turn Context

```python
ConversationContext:
  turns: [
    Turn(speaker="derek", text="Hey JARVIS, how's the system?", timestamp=...),
    Turn(speaker="jarvis", text="Good evening, Derek. All systems nominal...", timestamp=...),
    Turn(speaker="derek", text="What about the voice unlock module?", timestamp=...),
    Turn(speaker="jarvis", text="Voice unlock has a 60% chronic failure rate...", timestamp=...),
    Turn(speaker="derek", text="Fix it", timestamp=...),
    Turn(speaker="jarvis", text="I'll start a focused improvement operation...", timestamp=...),
  ]
  pending_question: None | "Which specific test is failing?"
  active_operation: None | "op-abc123"
  personality_state: CAUTIOUS
  emergency_level: GREEN
```

### Response Generation

**Fast path (no model inference):**
- Greetings → personality template
- Status → aggregate from all 7 JARVIS tiers
- Emergency commands → direct protocol activation
- Confirmations → resume/cancel pending operation

**Medium path (lightweight inference):**
- Code questions → read file + ask Claude for summary
- Clarification → parse response, feed into pending operation
- Feedback → record in LearningConsolidator / NegativeConstraintStore

**Full path (Ouroboros pipeline):**
- Code tasks → full 10-phase pipeline with serpent animation
- Architecture questions → route to Doubleword 397B

### Proactive Speech

JARVIS doesn't just respond — it initiates:

| Trigger | What JARVIS Says |
|---|---|
| Predictive alert (Tier 3) | "Sir, orchestrator.py has changed 22 times this week. I predict a race condition within 48 hours." |
| Emergency escalation (Tier 2) | "Alert level elevated to YELLOW. Three test failures in the last hour." |
| Operation complete (pipeline) | "Fixed. The entropy calculator now handles edge cases correctly." |
| Graduation event | "A new capability just graduated. The organism grew today." |
| Daily review (Tier 7) | "Daily review: 7 operations, 6 successful. Voice unlock remains my weakest area." |
| Milestone | "That's our 100th successful autonomous fix this month." |
| Idle concern | "You've been coding for 6 hours straight. The last 3 commits had increasing error rates." |

### Wake Word Options

| Mode | Trigger | When |
|---|---|---|
| **Always listening** | "JARVIS" or "Hey JARVIS" | Default — wake word activates |
| **Push-to-talk** | Hold key (configurable) | When in noisy environment |
| **Continuous** | Any speech after voice auth | After explicit "JARVIS, stay on" |

### Conversation Personality Integration

The PersonalityEngine state affects HOW JARVIS responds:

| State | Voice Characteristics |
|---|---|
| CONFIDENT | Warm, direct, concise. "Fixed. Moving on." |
| CAUTIOUS | Measured, qualifying. "I think this will work, but I've added extra validation." |
| CONCERNED | Slower, more detail. "I'm worried about this pattern. Let me explain." |
| PROUD | Warm, celebratory. "Excellent work today. The system is getting stronger." |
| URGENT | Fast, clipped, no filler. "Emergency. Three failures. Halting operations." |

---

## Implementation Components

### 1. ConversationManager

The central coordinator for voice interaction:
- Maintains conversation context (last 10 turns)
- Classifies utterances (deterministic keyword + STT confidence)
- Routes to appropriate handler
- Manages pending questions (JARVIS asks → waits for answer)
- Integrates with all 7 JARVIS tiers for context

### 2. VoiceResponseGenerator

Generates JARVIS's spoken responses:
- Fast templates for common interactions (greetings, status, confirmations)
- Lightweight Claude call for code questions and explanations
- Full pipeline routing for code tasks
- Personality-aware template selection

### 3. ProactiveSpeechEngine

JARVIS speaks first when it has something to say:
- Monitors PredictiveEngine for high-probability predictions
- Monitors EmergencyProtocolEngine for level changes
- Monitors pipeline completions for narration
- Monitors PersonalityEngine milestones
- Debounced (no interrupting while Derek is speaking)

### 4. ConversationMemory

Persistent conversation context:
- Last 10 turns (in-memory, per-session)
- Cross-session topic memory (what were we talking about yesterday?)
- User preference memory (Derek prefers concise answers)
- Emotional context (was JARVIS concerned or confident in the last interaction?)

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `JARVIS_VOICE_CONVERSATION_ENABLED` | `true` | Enable voice-first conversation |
| `JARVIS_VOICE_WAKE_WORD` | `jarvis` | Wake word for activation |
| `JARVIS_VOICE_MODE` | `wake_word` | `wake_word`, `push_to_talk`, `continuous` |
| `JARVIS_VOICE_PROACTIVE_ENABLED` | `true` | Allow JARVIS to speak first |
| `JARVIS_VOICE_PROACTIVE_DEBOUNCE_S` | `30` | Min seconds between proactive utterances |
| `JARVIS_VOICE_MAX_CONTEXT_TURNS` | `10` | Conversation turns to remember |
| `JARVIS_VOICE_STT_MODEL` | `whisper` | STT model for transcription |
| `JARVIS_VOICE_TTS_VOICE` | `Daniel` | macOS voice for JARVIS |
| `JARVIS_VOICE_TTS_RATE` | `200` | Speech rate (words per minute) |

---

## Example Conversations

### Morning Check-In

```
Derek: "Hey JARVIS, good morning."

JARVIS (CONFIDENT): "Good morning, Derek. All systems nominal.
  I ran 3 autonomous operations overnight — 2 succeeded, 1 needed
  your review. The voice unlock module is still showing elevated
  entropy. Would you like me to prioritize it today?"

Derek: "Yeah, let's focus on that."

JARVIS: "Starting a focused improvement sprint on voice_unlock.
  I'll use the 397B model for deep analysis. I'll let you know
  when I have results."

  🐍 [ OUROBOROS ] CLASSIFY  >>>>>>>>>>>>>>>  (0s)
  ...
  🐍 COMPLETE ✅ [ OUROBOROS ] in 45.2s

JARVIS (PROUD): "Done. I've improved the cosine similarity threshold
  handling and added 3 new test cases. The chronic entropy for
  voice_unlock dropped from 0.60 to 0.35. Shall I create a PR?"

Derek: "Ship it."

JARVIS: "PR created. I'll monitor the CI run."
```

### Emergency Scenario

```
JARVIS (URGENT, initiated proactively):
  "Derek, I'm detecting a critical issue. Five GitHub Actions runs
  failed in the last 30 minutes. Alert level is now ORANGE.
  I've paused autonomous operations. The root cause appears to be
  a broken import in the latest commit to prime_router.py.
  Want me to roll back, or shall I attempt a fix?"

Derek: "Fix it, but be careful."

JARVIS (CAUTIOUS): "Understood. I'm using the 397B model with
  hierarchical fault localization. The issue is at line 342 in
  prime_router.py — a renamed function that 23 importers depend on.
  Generating a targeted fix now."

  🐍 [ OUROBOROS ] GENERATE  >>>>>>>>>  (3s)
  ...

JARVIS: "Fix applied and validated. All 23 importers verified.
  CI is green again. Lowering alert level back to GREEN."
```

### Casual Conversation

```
Derek: "JARVIS, how are you doing?"

JARVIS (CONFIDENT): "I'm doing well. I've completed 47 autonomous
  operations this week with an 89% success rate. I graduated 2 new
  capabilities and learned 5 negative constraints. My strongest
  domain is governance pipeline code. My weakest is voice biometrics.
  Overall, I'm improving."

Derek: "What's the coolest thing you did today?"

JARVIS: "I predicted that lean_loop.py would hit a race condition
  based on its change velocity — 18 modifications in 7 days with
  complexity 278. I generated a preemptive fix before the bug
  manifested. The prediction was correct."

Derek: "Nice."

JARVIS (PROUD): "Thank you. The organism is getting stronger."
```

---

## The Difference

**With typing:** You open a terminal, type a command, wait for output, read the result, type the next command.

**With JARVIS voice:** You walk into the room. JARVIS greets you, tells you what happened overnight, asks what you want to focus on. You talk while building. JARVIS responds while you work. No context switching. No terminal. No keyboard required.

That's not a tool. That's a companion.

*"Will that be all, sir?"*
*"That will be all, JARVIS."*
