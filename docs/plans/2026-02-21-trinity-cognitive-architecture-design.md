# JARVIS Trinity Cognitive Architecture Design

**Date:** 2026-02-21
**Status:** Approved
**Scope:** Cross-repo (JARVIS Body, J-Prime, Reactor Core)

---

## 1. Executive Summary

**The Disease:** JARVIS Body contains ~11,000 lines of keyword-based classification
waterfall across `unified_command_processor.py` (6,747 lines),
`tiered_command_router.py` (1,356 lines), `vision_command_handler.py` (~1,500 lines
of classification), and `jarvis_voice_api.py` (~250 lines of pre-classification).
The Body acts as a brain, making cognitive decisions that belong to J-Prime. This
causes misclassification, maintenance nightmares, and prevents J-Prime from fulfilling
its role as the single cognitive authority.

**The Cure:** The Spinal Reflex Arc -- a biologically-inspired architecture where
the Body senses and acts, the Mind thinks and decides, and the Nerves learn and adapt.

### Three Interlocking Design Decisions

| Decision | Pattern | Key Principle |
|----------|---------|---------------|
| **Query Routing** | Spinal Reflex Arc | Body never classifies. ~15 reflexes execute locally; everything else goes to J-Prime. |
| **Mind-Body Contract** | Dual-Model Phi Classifier | Phi-3.5-mini (2.1GB, permanently resident) classifies via JSON grammar; specialist models generate content. |
| **Body Refactoring** | Interface Preservation (Liskov) | `process_command()` signature unchanged; 5,000 lines of classification replaced with ~200 lines. |

**Net impact:** ~10,800 lines of classification removed. Zero new files. Zero caller
changes. One brain, one authority, one learning loop.

---

## 2. Architecture Overview

### 2.1 The Trinity (Body / Mind / Nerves)

```
USER (text / voice / image)
        |
        v
+-----------------------------------------------------------------------+
|  JARVIS BODY (:8010/:3000)                                            |
|                                                                       |
|  React UI --- Voice Pipeline --- Vision Pipeline                      |
|       \            |              /                                    |
|        +-----------|-------------+                                    |
|                    |                                                   |
|           process_command()                                            |
|                    |                                                   |
|        +-----------+-----------+                                      |
|        |           |           |                                      |
|        v           v           v                                      |
|  Protected    Reflex       J-Prime Call                               |
|  Local Ops    Manifest     (via existing                              |
|  (voice       Check        JarvisPrimeClient                         |
|   unlock)         |        with circuit breakers)                     |
|               Execute +         |                                     |
|               async notify      v                                     |
|                    |    Action Executor                                |
|                    |    (switch on intent)                             |
|                    +-----------|                                      |
|                                |                                      |
|                       Return to caller                                |
+-----------------------------------------------------------------------+
        |          Network (single round trip)
        v
+-----------------------------------------------------------------------+
|  J-PRIME (:8000/:8001) -- GCP Golden Image VM                         |
|                                                                       |
|  Phi-3.5-mini (3.8B, ~2.1GB) -- PERMANENTLY RESIDENT                 |
|  INPUT: Raw query + context metadata                                  |
|  OUTPUT: Classification JSON (grammar-constrained)                    |
|          {intent, domain, complexity, confidence, ...}                |
|                    |                                                   |
|  GCPModelSwapCoordinator                                              |
|  domain="math" -> load Qwen-Math-7B                                  |
|  domain="code" -> load Qwen-Coder-7B                                 |
|  domain="conversation" -> Phi handles (no swap needed)                |
|                    |                                                   |
|  Specialist Model (7B-9B) -- Swappable                                |
|  Generates free-text content (no grammar constraint)                  |
|                    |                                                   |
|  Return response with x_jarvis_routing metadata                       |
+-----------------------------------------------------------------------+
        |          Telemetry flows async
        v
+-----------------------------------------------------------------------+
|  REACTOR CORE (:8090)                                                 |
|                                                                       |
|  Ingests every classification + response as training signal           |
|  DPO pairs from escalation decisions                                  |
|  Fine-tunes Phi classifier over time                                  |
|  Trains specialist models on escalated query types                    |
|  Goal: reduce Claude escalation toward zero                           |
+-----------------------------------------------------------------------+
```

### 2.2 Query Categories (3 Types)

| Category | Where Processed | Latency | Examples |
|----------|----------------|---------|----------|
| **Protected Local Ops** | Body only (never leaves machine) | <100ms | Voice unlock (ECAPA-TDNN + LangGraph pipeline) |
| **Reflexes** | Body executes + async notify J-Prime | <5ms | Lock screen, volume up, mute, brightness |
| **Brain-First** | J-Prime classifies + generates | 200-2000ms | Questions, commands, vision, agentic tasks |

---

## 3. Spinal Reflex Arc -- Query Routing

### 3.1 Reflex Manifest

Published by J-Prime at startup, consumed by Body. Lives at
`~/.jarvis/trinity/reflex_manifest.json`.

**Seed manifest** ships in the `jarvis-prime` repo as a default config. J-Prime
publishes it on first boot and evolves it dynamically via Reactor Core learning.

**Properties:**

- ~10-15 entries maximum -- conservative, unambiguous phrases only
- Exact/near-exact matching (no regex, no NLP)
- Versioned, signed by J-Prime (integrity verification)
- Body checks `~/.jarvis/trinity/reflex_inhibition.json` before executing
  (descending inhibition when J-Prime has an active agentic plan)

### 3.2 Reflex Inhibition

J-Prime publishes a temporary inhibition signal during agentic plan execution:

```json
{
  "inhibit_reflexes": ["lock_screen"],
  "reason": "agentic_plan_active",
  "ttl_seconds": 300,
  "published_at": "2026-02-21T10:00:00Z"
}
```

Body checks TTL before firing any reflex. Expired inhibitions are ignored.

### 3.3 Brain Vacuum Fallback (J-Prime Unreachable)

During the 60-600 second GCP VM boot window, the existing `JarvisPrimeClient`
fallback chain activates: `PRIME_LOCAL -> CLAUDE_API`. Claude receives the same
system prompt contract and returns a structured classification. Since Claude
reliably follows instructions, the system prompt + parser approach is acceptable
for this degraded mode.

---

## 4. Dual-Model Phi Classifier -- Mind-Body Contract

### 4.1 The Classification Schema (v1)

```python
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "intent": {
            "type": "string",
            "enum": [
                "answer",            # Question -> generate answer
                "action",            # Single system action -> Body executes
                "multi_step_action", # Multi-step plan -> step-by-step execution
                "vision_needed",     # Needs to see screen/image
                "clarify",           # Ambiguous -> ask user to clarify
                "conversation"       # Casual/greeting -> brief response
            ]
        },
        "domain": {
            "type": "string",
            "enum": [
                "math", "code", "reasoning", "creative", "general",
                "system", "vision", "agentic", "translation",
                "conversation", "surveillance"
            ]
        },
        "complexity": {
            "type": "string",
            "enum": ["trivial", "simple", "moderate", "complex", "expert"]
        },
        "requires_vision": {"type": "boolean"},
        "requires_action": {"type": "boolean"},
        "escalate_to_claude": {"type": "boolean"},
        "escalation_reason": {"type": "string"},
        "confidence": {"type": "number"},
        "suggested_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "target": {"type": "string"},
                    "parameters": {"type": "object"}
                }
            }
        }
    },
    "required": [
        "schema_version", "intent", "domain", "complexity",
        "confidence", "requires_vision", "requires_action",
        "escalate_to_claude"
    ]
}
```

### 4.2 Phi Classifier System Prompt

Includes:

- Classification instructions
- The full action registry (dynamically generated from the same source as the
  reflex manifest)
- Domain definitions with examples
- Escalation criteria: escalate if not confident local models can handle it, if
  the query requires computer use planning, or if vision is needed and LLaVA is
  unavailable

### 4.3 Dual-Model Flow Inside J-Prime

```
Request arrives
    |
    +-- Phi-3.5-mini classifies (JSON grammar, ~100-150ms)
    |   Output: classification JSON
    |
    +-- Is intent "conversation" or "action" (simple)?
    |   YES: Phi generates response directly (~200ms total, no specialist)
    |   NO: Continue to specialist
    |
    +-- GCPModelSwapCoordinator: domain -> specialist model
    |   Swap if needed (5-10s cold, 0ms if already loaded)
    |
    +-- Specialist generates content (free text, ~500-2000ms)
    |
    +-- Assemble response with x_jarvis_routing metadata
```

### 4.4 Escalation to Claude

Triggered by `escalate_to_claude: true` from Phi. Reasons:

- Computer-use / agentic planning (beyond 7B capability)
- Vision queries when LLaVA unavailable
- Low confidence from Phi (<0.5)
- Query exceeds local model context window
- Safety-critical decisions

Escalation threshold starts conservative (escalate more) and Reactor Core
progressively tightens it as local models improve.

### 4.5 PRIME_LOCAL on Mac (16GB RAM)

Cannot run dual-model (no room for Phi + specialist). Fallback strategy:

- Single model with system prompt classification (acceptable for degraded mode)
- OR skip classification, use default model for everything
- This path only activates when GCP AND Claude are unreachable -- deep fallback

### 4.6 x_jarvis_routing Response Metadata

Every response from J-Prime includes:

```json
{
  "id": "chatcmpl-abc123",
  "choices": [{"message": {"role": "assistant", "content": "..."}}],
  "x_jarvis_routing": {
    "schema_version": 1,
    "intent": "answer",
    "domain": "math",
    "classifier_model": "phi-3.5-mini-q4km",
    "generator_model": "qwen2.5-math-7b-q4km",
    "confidence": 0.94,
    "escalated": false,
    "classification_ms": 142,
    "generation_ms": 1230,
    "suggested_actions": []
  }
}
```

Body reads `x_jarvis_routing` to decide how to render/execute the response.
Unknown intents/domains fall back to treating as `general`/`answer`.

---

## 5. Interface Preservation -- Body Refactoring

### 5.1 What Changes

**`unified_command_processor.py`** -- From 6,747 lines to ~800 lines:

```
class UnifiedCommandProcessor:
    # --- PRESERVED (unchanged) ---
    process_command() signature and return shape
    Self-voice suppression check
    Voice unlock (protected local op) detection
    Command execution handlers (MacOSController, IntelligentCommandHandler, etc.)
    Telemetry capture
    Pattern learning / stats

    # --- NEW (replaces 5,000 lines of classification) ---
    _check_reflex_manifest()      # ~30 lines
    _call_jprime()                # ~50 lines (extends existing JarvisPrimeClient)
    _execute_action()             # ~100 lines (switch on x_jarvis_routing.intent)
    _brain_vacuum_fallback()      # ~30 lines (Claude API with system prompt)
```

**`jarvis_voice_api.py`** -- From 5,450 to ~3,200 lines. Removes:

- Surveillance regex detection
- Math equation guard
- Workspace intent guard
- Vision handler routing pre-classification

**`main.py`** -- Removes lock-screen regex fast-path (replaced by reflex check).

### 5.2 What Gets Deleted

| File | Lines | Action |
|------|-------|--------|
| `tiered_command_router.py` | 1,356 | Delete entirely |
| `tiered_vbia_adapter.py` | ~350 | Delete entirely |
| `unified_command_processor_pure.py` | ~500 | Delete entirely |
| `vision_command_handler.py` classification | ~1,500 | Extract execution, delete classification |
| UCP classification internals | ~5,000 | Replaced with ~200 lines |
| Voice API pre-classification | ~250 | Deleted |
| main.py lock-screen fast-path | ~70 | Replaced by reflex |

### 5.3 What Gets Extended (Zero New Files)

| Existing File | Extension |
|---------------|-----------|
| `backend/core/jarvis_prime_client.py` | Add `classify_and_complete()` method. Parse `x_jarvis_routing`. Brain vacuum fallback logic. |
| `backend/api/unified_command_processor.py` | Reflex router + action executor as internal methods. Hollowed-out UCP becomes the canonical thin router. |

### 5.4 Caller Impact

**Zero.** All 5 hot-path callers continue importing
`from api.unified_command_processor import get_unified_processor` and calling
`processor.process_command(text)`. The return shape
`{"success": bool, "response": str, "command_type": str}` is preserved.

---

## 6. Cross-Repo Integration Points

### 6.1 File Changes by Repo

| Repo | Files Modified | Files Deleted | Net Lines |
|------|---------------|---------------|-----------|
| **JARVIS Body** | `unified_command_processor.py`, `jarvis_prime_client.py`, `jarvis_voice_api.py`, `main.py` | `tiered_command_router.py`, `tiered_vbia_adapter.py`, `unified_command_processor_pure.py` | **-10,800** |
| **J-Prime** | `run_server.py`, `server.py`, `llama_cpp_executor.py` | None | **+400** |
| **Reactor Core** | `ingestion/telemetry_ingestor.py` | None | **+50** |

### 6.2 Shared State

| Path | Writer | Reader | Purpose |
|------|--------|--------|---------|
| `~/.jarvis/trinity/reflex_manifest.json` | J-Prime | Body | Reflex definitions |
| `~/.jarvis/trinity/reflex_inhibition.json` | J-Prime | Body | Temporary reflex suppression |
| `~/.jarvis/trinity/classification_schema.json` | J-Prime | Body | Schema version for forward compat |
| `~/.jarvis/telemetry/*.jsonl` | Body | Reactor Core | Query + classification telemetry |

---

## 7. Gap Resolution Matrix

| # | Gap | Resolution | Where |
|---|-----|-----------|-------|
| 1 | Voice Unlock | Protected local op, checked before reflex/J-Prime | UCP internal |
| 2 | Structured Response | Phi JSON grammar constrained decoding | J-Prime |
| 3 | Keyword Classification | Phi LLM replaces all keyword matching | J-Prime |
| 4 | Brain Vacuum | JarvisPrimeClient fallback chain -> Claude API | JarvisPrimeClient |
| 5 | Manifest Bootstrap | Seed manifest in jarvis-prime repo | J-Prime |
| 6 | Reflex Inhibition | `reflex_inhibition.json` with TTL | Shared state |
| 7 | Two-Phase Vision | Always attach minimal screen metadata; J-Prime requests full screenshot via `vision_needed` intent | UCP + J-Prime |
| 8 | Conversation State | Cloud SQL persistence (existing infra) | J-Prime |
| 9 | Agentic Feedback Loop | Action executor reports step results back to J-Prime between steps | UCP action executor |
| 10 | Manifest is Hardcoding | Pragmatic optimization; Reactor Core trains toward replacing it | Reactor Core roadmap |
| 11 | Rapid-Fire Race | Reflexes with physical consequences can have ~100ms inhibition window | UCP internal |
| 12 | Manifest Tampering | J-Prime signs manifest; Body verifies | Shared state |
| 13 | Surgical Deletion | Extract execution, delete classification. Execution handlers preserved. | UCP |
| 14 | Ambiguous Phrases | Conservative manifest: only unambiguous phrases qualify | J-Prime manifest |
| 15 | Cost Tracking | `x_jarvis_routing` includes model info + escalation flag -> Helicone | J-Prime response |

---

## 8. Implementation Order

```
STEP 1: J-Prime -- Phi Classifier (jarvis-prime repo)
  +-- Add grammar-constrained classification to llama_cpp_executor.py
  +-- Add Phi-as-permanent-classifier logic to run_server.py
  +-- Define classification schema + system prompt with action registry
  +-- Add x_jarvis_routing metadata to response assembly
  +-- Test: classify 50 diverse queries, verify JSON structure
  Deliverable: J-Prime returns structured routing with every response

STEP 2: J-Prime -- Reflex Manifest Publishing (jarvis-prime repo)
  +-- Create seed manifest as config in jarvis-prime
  +-- Publish to ~/.jarvis/trinity/ on startup
  +-- Add inhibition signal support
  Deliverable: Body can read reflex manifest on boot

STEP 3: Body -- Extend JarvisPrimeClient (JARVIS repo)
  +-- Add classify_and_complete() method
  +-- Parse x_jarvis_routing from response
  +-- Add brain vacuum fallback (Claude API with classification prompt)
  Deliverable: Body can get structured decisions from J-Prime

STEP 4: Body -- Hollow Out UCP (THE BIG SURGERY) (JARVIS repo)
  +-- Keep process_command() signature
  +-- Add reflex manifest check (internal method)
  +-- Replace classification with J-Prime call (internal method)
  +-- Add action executor (internal method, switch on intent)
  +-- Preserve execution handlers, self-voice suppression, telemetry
  +-- Golden test: run 50-query corpus, verify identical behavior
  Deliverable: UCP is ~800 lines, callers unchanged

STEP 5: Body -- Clean Up Callers (JARVIS repo)
  +-- Remove jarvis_voice_api.py pre-classification (~250 lines)
  +-- Remove main.py lock-screen regex fast-path (~70 lines)
  Deliverable: Callers are clean pass-throughs

STEP 6: Body -- Delete Dead Files (JARVIS repo)
  +-- tiered_command_router.py
  +-- tiered_vbia_adapter.py
  +-- unified_command_processor_pure.py
  +-- vision_command_handler.py classification (extract execution first)
  Deliverable: ~3,200 lines of dead code removed

STEP 7: Reactor Core -- Telemetry Integration (reactor-core repo)
  +-- Update telemetry ingestor to parse new x_jarvis_routing format
  +-- Generate DPO pairs from escalation decisions
  +-- Create training pipeline for Phi classifier improvement
  Deliverable: Learning loop closed
```

---

## 9. Verification Criteria

The surgery is complete when:

1. `python3 unified_supervisor.py` boots all three repos as one system
2. Every query type (question, command, vision, agentic, surveillance,
   conversation, voice unlock) produces correct behavior
3. `unified_command_processor.py` is <1,000 lines
4. `tiered_command_router.py` no longer exists
5. No keyword-based classification exists in the Body
6. All classification decisions flow through J-Prime's Phi classifier
7. `x_jarvis_routing` metadata present on every J-Prime response
8. Brain vacuum (J-Prime unreachable) degrades gracefully to Claude API
9. Reflexes fire in <5ms and are logged to telemetry
10. Golden test corpus of 50 queries passes with expected behavior
