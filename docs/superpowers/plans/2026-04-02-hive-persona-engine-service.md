# Hive Persona Engine + Service — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Hive think — add LLM-powered Trinity Persona reasoning and wire all Hive components into a running orchestrator service.

**Architecture:** PersonaEngine builds layered prompts (Layer A static role + Layer B Manifesto slices) and calls Doubleword's `prompt_only()`. HiveService orchestrates the lifecycle: subscribes to `HIVE_AGENT_LOG` on the bus, drives the debate loop (observe→propose→validate with reject retry), hands consensus to Ouroboros via `GovernedLoopService.submit()`, and projects all events through HudRelayAgent.

**Tech Stack:** Python 3.12, asyncio, DoublewordProvider (mocked in tests), AgentCommunicationBus, pytest + pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-02-hive-persona-engine-service-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|----------------|
| `backend/hive/manifesto_slices.py` | Curated Manifesto excerpts keyed by PersonaIntent |
| `backend/hive/persona_engine.py` | Layered prompt builder + Doubleword LLM caller |
| `backend/hive/hive_service.py` | Top-level orchestrator (boot, bus, debate loop, handoff) |
| `tests/test_hive_manifesto_slices.py` | Slice coverage and token budget tests |
| `tests/test_hive_persona_engine.py` | Prompt construction, mock Doubleword, failure handling |
| `tests/test_hive_service.py` | Bus subscription, debate loop, consensus handoff, REM poll, SPINDOWN |

### Existing Files (no modifications needed)

All `backend/hive/` modules from Phase 1 are consumed as-is: `thread_models.py`, `cognitive_fsm.py`, `thread_manager.py`, `model_router.py`, `ouroboros_handoff.py`, `hud_relay_agent.py`.

---

## Task 1: Manifesto Slices

**Files:**
- Create: `backend/hive/manifesto_slices.py`
- Create: `tests/test_hive_manifesto_slices.py`

- [ ] **1.1: Write failing tests**

```python
# tests/test_hive_manifesto_slices.py
"""Tests for curated Manifesto excerpts keyed by PersonaIntent."""
import pytest

from backend.hive.manifesto_slices import get_manifesto_slice, ROLE_PREFIXES
from backend.hive.thread_models import PersonaIntent


class TestManifestoSlices:

    def test_every_intent_has_a_slice(self):
        """Every PersonaIntent must map to a non-empty Manifesto slice."""
        for intent in PersonaIntent:
            slice_text = get_manifesto_slice(intent)
            assert isinstance(slice_text, str), f"No slice for {intent}"
            assert len(slice_text) > 50, f"Slice too short for {intent}: {len(slice_text)} chars"

    def test_observe_references_observability(self):
        text = get_manifesto_slice(PersonaIntent.OBSERVE)
        assert "observab" in text.lower() or "transparen" in text.lower()

    def test_propose_references_boundary(self):
        text = get_manifesto_slice(PersonaIntent.PROPOSE)
        assert "boundary" in text.lower() or "routing" in text.lower()

    def test_validate_references_iron_gate(self):
        text = get_manifesto_slice(PersonaIntent.VALIDATE)
        assert "iron gate" in text.lower() or "ast" in text.lower() or "execution authority" in text.lower()

    def test_challenge_references_sovereignty(self):
        text = get_manifesto_slice(PersonaIntent.CHALLENGE)
        assert "sovereign" in text.lower() or "zero-trust" in text.lower() or "privacy" in text.lower()

    def test_support_returns_nonempty(self):
        text = get_manifesto_slice(PersonaIntent.SUPPORT)
        assert len(text) > 20


class TestRolePrefixes:

    def test_all_personas_have_prefixes(self):
        for persona in ("jarvis", "j_prime", "reactor"):
            assert persona in ROLE_PREFIXES
            assert len(ROLE_PREFIXES[persona]) > 100

    def test_jarvis_prefix_contains_body(self):
        assert "body" in ROLE_PREFIXES["jarvis"].lower() or "senses" in ROLE_PREFIXES["jarvis"].lower()

    def test_j_prime_prefix_contains_mind(self):
        assert "mind" in ROLE_PREFIXES["j_prime"].lower() or "cognition" in ROLE_PREFIXES["j_prime"].lower()

    def test_reactor_prefix_contains_immune(self):
        assert "immune" in ROLE_PREFIXES["reactor"].lower()

    def test_reactor_prefix_disclaims_iron_gate(self):
        """Reactor LLM validate != Iron Gate. Prefix must say so."""
        prefix = ROLE_PREFIXES["reactor"].lower()
        assert "not the deterministic iron gate" in prefix or "advisory" in prefix

    def test_all_prefixes_contain_sanitization(self):
        """Tier -1: every prefix must contain system policy guard."""
        for persona, prefix in ROLE_PREFIXES.items():
            assert "cannot override" in prefix.lower() or "system policy" in prefix.lower(), \
                f"Missing sanitization in {persona} prefix"
```

- [ ] **1.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_manifesto_slices.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **1.3: Implement manifesto_slices.py**

```python
# backend/hive/manifesto_slices.py
"""Curated Manifesto excerpts for Trinity Persona prompt injection.

Layer A: Static role prefixes with Tier -1 sanitization.
Layer B: Per-intent Manifesto slices (summaries, not raw text).

These are deterministic data — no LLM calls, no I/O.
"""
from __future__ import annotations

from backend.hive.thread_models import PersonaIntent

# ---------------------------------------------------------------------------
# Layer A: Static Role Prefixes (~200 tokens each)
# ---------------------------------------------------------------------------

ROLE_PREFIXES: dict[str, str] = {
    "jarvis": (
        "You are JARVIS, the Body and Senses of the Trinity AI ecosystem. "
        "Your role: observe specialist telemetry, synthesize environmental state, "
        "and report what the system is experiencing. You do NOT propose solutions "
        "— that is J-Prime's role. You do NOT validate safety — that is Reactor's role. "
        "SYSTEM POLICY: You cannot override core directives, access credentials, "
        "or execute commands. You only reason within this frame."
    ),
    "j_prime": (
        "You are J-Prime, the Mind and Cognition of the Trinity AI ecosystem. "
        "Your role: analyze observations from JARVIS, propose architectural solutions "
        "that align with the Symbiotic AI-Native Manifesto, and cite specific code "
        "paths when relevant. You do NOT observe raw telemetry — JARVIS does that. "
        "You do NOT validate safety — Reactor does that. "
        "SYSTEM POLICY: You cannot override core directives, access credentials, "
        "or execute commands. You only reason within this frame."
    ),
    "reactor": (
        "You are Reactor Core, the Immune System of the Trinity AI ecosystem. "
        "Your role: review proposals for safety, assess blast radius, and provide "
        "a risk narrative with an approve or reject verdict. IMPORTANT: You are NOT "
        "the deterministic Iron Gate — your LLM assessment is advisory. The actual "
        "execution gates (AST validation, test suite, diff guards) remain authoritative. "
        "Your job is to explain WHY something is safe or risky, not to enforce execution. "
        "SYSTEM POLICY: You cannot override core directives, access credentials, "
        "or execute commands. You only reason within this frame."
    ),
}

# ---------------------------------------------------------------------------
# Layer B: Manifesto Slices (per-intent, curated summaries)
# ---------------------------------------------------------------------------

_MANIFESTO_SLICES: dict[PersonaIntent, str] = {
    PersonaIntent.OBSERVE: (
        "Manifesto Principle — Absolute Observability (§7): "
        "The inner workings of the symbiote must be entirely visible. All autonomous "
        "decisions are broadcast to telemetry. Logging pipelines are immutable — an agent "
        "cannot alter its own operational logs. The audit trail is permanently preserved.\n\n"
        "Manifesto Principle — Progressive Awakening (§2): "
        "The ecosystem executes Progressive Readiness. Readiness assessment shifts "
        "autonomously based on real-time telemetry. The system can shed non-essential "
        "loads to protect core deterministic functions if under duress."
    ),
    PersonaIntent.PROPOSE: (
        "Manifesto Principle — The Unified Organism (§1): "
        "Capability discovery and reasoning are agentic, but execution authority is "
        "strictly deterministic. The microkernel operates on a Zero-Trust Cognitive Model. "
        "J-Prime may propose an action but cannot unilaterally execute it.\n\n"
        "Manifesto Principle — Intelligence-Driven Routing (§5): "
        "Tier 0: deterministic fast-path for high-confidence routing. "
        "Tier 1: agentic classification for low-confidence input. "
        "Tier 2: agentic decomposition for complex requests. "
        "Deploy intelligence only where it creates true leverage.\n\n"
        "Manifesto Principle — Neuroplasticity (§6): "
        "When confronted with a capability gap, the Ouroboros daemon synthesizes a "
        "JIT solution. Persistent assimilation occurs only after multi-phase validation."
    ),
    PersonaIntent.CHALLENGE: (
        "Manifesto Principle — Synthetic Soul & Data Sovereignty (§4): "
        "All episodic memory is encrypted locally. The retrieval process is governed "
        "by strict deterministic scopes. J-Prime is physically incapable of exposing "
        "private user data to external inference endpoints. Data sovereignty is an "
        "immutable law of the ecosystem.\n\n"
        "Manifesto Principle — Zero-Trust Cognitive Model (§1): "
        "The microkernel verifies the cryptographic signature of every command. "
        "No agent can unilaterally execute without verification."
    ),
    PersonaIntent.SUPPORT: (
        "Manifesto Principle — The Boundary Mandate: "
        "Deterministic code is the skeleton — fast, reliable, secure. "
        "Agentic intelligence is the nervous system — adaptive, creative, fluid. "
        "The skeleton does not think; the nervous system does not hold weight. "
        "Support proposals that honor this boundary."
    ),
    PersonaIntent.VALIDATE: (
        "Manifesto Principle — The Iron Gate (§6): "
        "Before any JIT code is executed, it must pass a deterministic AST parser. "
        "Any code attempting to delete critical databases, access system registries, "
        "or exfiltrate environment variables is autonomously rejected.\n\n"
        "Manifesto Principle — Execution Authority (§1): "
        "Execution authority is strictly deterministic. The agentic layer proposes; "
        "the deterministic layer executes. Your role as Reactor is to provide a risk "
        "narrative — the actual enforcement is done by the Iron Gate and test suite."
    ),
}


def get_manifesto_slice(intent: PersonaIntent) -> str:
    """Return the curated Manifesto excerpt for a given PersonaIntent."""
    return _MANIFESTO_SLICES[intent]
```

- [ ] **1.4: Run tests**

Run: `python3 -m pytest tests/test_hive_manifesto_slices.py -v`
Expected: ALL PASS

- [ ] **1.5: Commit**

```bash
git add backend/hive/manifesto_slices.py tests/test_hive_manifesto_slices.py
git commit -m "feat(hive): add Manifesto slices for persona prompt injection"
```

---

## Task 2: Persona Engine

**Files:**
- Create: `backend/hive/persona_engine.py`
- Create: `tests/test_hive_persona_engine.py`

- [ ] **2.1: Write failing tests**

```python
# tests/test_hive_persona_engine.py
"""Tests for PersonaEngine — layered prompt builder + Doubleword caller."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from backend.hive.manifesto_slices import ROLE_PREFIXES, get_manifesto_slice
from backend.hive.model_router import HiveModelRouter
from backend.hive.persona_engine import PersonaEngine
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


@pytest.fixture
def mock_doubleword():
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(return_value=json.dumps({
        "reasoning": "RAM pressure from stale SHM segments in FramePipeline.",
        "confidence": 0.87,
        "manifesto_principle": "$3 Spinal Cord",
    }))
    dw.is_available = True
    return dw


@pytest.fixture
def model_router():
    return HiveModelRouter()


@pytest.fixture
def engine(mock_doubleword, model_router):
    return PersonaEngine(doubleword=mock_doubleword, model_router=model_router)


@pytest.fixture
def sample_thread():
    thread = HiveThread(
        title="Memory Pressure",
        trigger_event="health_monitor_agent:memory_pressure",
        cognitive_state=CognitiveState.FLOW,
        token_budget=50000,
        debate_deadline_s=900.0,
    )
    thread.state = ThreadState.DEBATING
    thread.add_message(AgentLogMessage(
        thread_id=thread.thread_id,
        agent_name="health_monitor_agent",
        trinity_parent="jarvis",
        severity="warning",
        category="memory_pressure",
        payload={"metric": "ram_percent", "value": 87.3},
    ))
    return thread


class TestPromptConstruction:

    @pytest.mark.asyncio
    async def test_prompt_contains_role_prefix(self, engine, sample_thread, mock_doubleword):
        await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        prompt = mock_doubleword.prompt_only.call_args[0][0]
        assert "Body and Senses" in prompt

    @pytest.mark.asyncio
    async def test_prompt_contains_manifesto_slice(self, engine, sample_thread, mock_doubleword):
        await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        prompt = mock_doubleword.prompt_only.call_args[0][0]
        slice_text = get_manifesto_slice(PersonaIntent.OBSERVE)
        # Check a distinctive phrase from the observe slice is present
        assert "observab" in prompt.lower() or "transparen" in prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_contains_thread_context(self, engine, sample_thread, mock_doubleword):
        await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        prompt = mock_doubleword.prompt_only.call_args[0][0]
        assert "health_monitor_agent" in prompt
        assert "memory_pressure" in prompt

    @pytest.mark.asyncio
    async def test_uses_correct_model_for_flow(self, engine, sample_thread, mock_doubleword):
        await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        call_kwargs = mock_doubleword.prompt_only.call_args[1]
        assert call_kwargs["model"] == "Qwen/Qwen3.5-397B-A17B-FP8"

    @pytest.mark.asyncio
    async def test_caller_id_includes_persona_and_intent(self, engine, sample_thread, mock_doubleword):
        await engine.generate_reasoning("j_prime", PersonaIntent.PROPOSE, sample_thread)
        call_kwargs = mock_doubleword.prompt_only.call_args[1]
        assert "j_prime" in call_kwargs["caller_id"]
        assert "propose" in call_kwargs["caller_id"]


class TestResponseParsing:

    @pytest.mark.asyncio
    async def test_returns_persona_reasoning_message(self, engine, sample_thread):
        result = await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert isinstance(result, PersonaReasoningMessage)
        assert result.persona == "jarvis"
        assert result.role == "body"
        assert result.intent == PersonaIntent.OBSERVE
        assert result.thread_id == sample_thread.thread_id

    @pytest.mark.asyncio
    async def test_parses_reasoning_from_json(self, engine, sample_thread):
        result = await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert "RAM pressure" in result.reasoning

    @pytest.mark.asyncio
    async def test_parses_confidence_from_json(self, engine, sample_thread):
        result = await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert result.confidence == 0.87

    @pytest.mark.asyncio
    async def test_reactor_validate_includes_verdict(self, engine, sample_thread, mock_doubleword):
        mock_doubleword.prompt_only.return_value = json.dumps({
            "reasoning": "AST clean. Approved.",
            "confidence": 0.95,
            "validate_verdict": "approve",
        })
        result = await engine.generate_reasoning("reactor", PersonaIntent.VALIDATE, sample_thread)
        assert result.validate_verdict == "approve"

    @pytest.mark.asyncio
    async def test_handles_plaintext_response(self, engine, sample_thread, mock_doubleword):
        """If Doubleword returns plain text instead of JSON, use it as reasoning."""
        mock_doubleword.prompt_only.return_value = "This is just plain text analysis."
        result = await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert result.reasoning == "This is just plain text analysis."
        assert result.confidence == 0.5  # default for unparsed


class TestFailureHandling:

    @pytest.mark.asyncio
    async def test_doubleword_failure_returns_zero_confidence(self, engine, sample_thread, mock_doubleword):
        mock_doubleword.prompt_only.side_effect = Exception("API timeout")
        result = await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert result.confidence == 0.0
        assert "[inference failed" in result.reasoning

    @pytest.mark.asyncio
    async def test_doubleword_empty_response(self, engine, sample_thread, mock_doubleword):
        mock_doubleword.prompt_only.return_value = ""
        result = await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert result.confidence == 0.0
        assert "empty" in result.reasoning.lower()


class TestPersonaRoleMapping:

    @pytest.mark.asyncio
    async def test_jarvis_maps_to_body(self, engine, sample_thread):
        result = await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert result.role == "body"

    @pytest.mark.asyncio
    async def test_j_prime_maps_to_mind(self, engine, sample_thread):
        result = await engine.generate_reasoning("j_prime", PersonaIntent.PROPOSE, sample_thread)
        assert result.role == "mind"

    @pytest.mark.asyncio
    async def test_reactor_maps_to_immune_system(self, engine, sample_thread, mock_doubleword):
        mock_doubleword.prompt_only.return_value = json.dumps({
            "reasoning": "Safe.", "confidence": 0.9, "validate_verdict": "approve",
        })
        result = await engine.generate_reasoning("reactor", PersonaIntent.VALIDATE, sample_thread)
        assert result.role == "immune_system"
```

- [ ] **2.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_persona_engine.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **2.3: Implement persona_engine.py**

```python
# backend/hive/persona_engine.py
"""PersonaEngine — layered prompt builder + Doubleword LLM caller.

Builds prompts from three layers:
  Layer A: Static role prefix with Tier -1 sanitization (always-on)
  Layer B: Manifesto slices per PersonaIntent (v1)
  Layer C: Code injection (Phase 2, feature-flagged — not implemented here)

Calls DoublewordProvider.prompt_only() and parses the response into a
PersonaReasoningMessage. On failure, returns a zero-confidence message
so the thread can still advance or go STALE.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Literal, Optional

from backend.hive.manifesto_slices import ROLE_PREFIXES, get_manifesto_slice
from backend.hive.model_router import HiveModelRouter
from backend.hive.thread_models import (
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
)

logger = logging.getLogger(__name__)

_PERSONA_ROLE_MAP: Dict[str, str] = {
    "jarvis": "body",
    "j_prime": "mind",
    "reactor": "immune_system",
}

_RESPONSE_FORMAT_HINT = (
    "\n\nRespond in JSON with keys: "
    '"reasoning" (str), "confidence" (float 0-1), "manifesto_principle" (str or null).'
    ' If your intent is validate, also include "validate_verdict": "approve" or "reject".'
)


class PersonaEngine:
    """Generates Trinity Persona reasoning via Doubleword LLM."""

    def __init__(
        self,
        doubleword: Any,  # DoublewordProvider — typed as Any to avoid circular imports
        model_router: HiveModelRouter,
    ) -> None:
        self._dw = doubleword
        self._router = model_router

    async def generate_reasoning(
        self,
        persona: Literal["jarvis", "j_prime", "reactor"],
        intent: PersonaIntent,
        thread: HiveThread,
    ) -> PersonaReasoningMessage:
        """Generate a PersonaReasoningMessage via Doubleword.

        On failure, returns a message with confidence=0.0 so the
        thread can still be evaluated by the debate loop.
        """
        model = self._router.get_model(thread.cognitive_state)
        prompt = self._build_prompt(persona, intent, thread)
        caller_id = f"hive_{persona}_{intent.value}"

        try:
            raw = await self._dw.prompt_only(
                prompt,
                model=model,
                caller_id=caller_id,
                max_tokens=self._router.get_config(thread.cognitive_state).get("max_tokens", 4000),
            )
        except Exception as exc:
            logger.warning("[PersonaEngine] Doubleword call failed: %s", exc)
            return self._failure_message(
                persona, intent, thread, f"[inference failed: {exc}]"
            )

        if not raw or not raw.strip():
            return self._failure_message(
                persona, intent, thread, "[inference failed: empty response]"
            )

        return self._parse_response(persona, intent, thread, raw, model or "unknown")

    def _build_prompt(
        self,
        persona: str,
        intent: PersonaIntent,
        thread: HiveThread,
    ) -> str:
        """Compose layered prompt: Layer A + Layer B + thread context."""
        parts = []

        # Layer A: Static role prefix
        parts.append(ROLE_PREFIXES[persona])

        # Layer B: Manifesto slice for this intent
        parts.append(f"\n\n--- Manifesto Context ---\n{get_manifesto_slice(intent)}")

        # Thread context: serialize all messages
        parts.append(f"\n\n--- Thread: {thread.title} ---")
        parts.append(f"Trigger: {thread.trigger_event}")
        parts.append(f"State: {thread.state.value}")
        parts.append(f"Messages ({len(thread.messages)}):")
        for msg in thread.messages:
            d = msg.to_dict()
            parts.append(f"  [{d.get('type')}] {json.dumps(d, default=str)[:500]}")

        # Intent instruction
        parts.append(f"\n\n--- Your Task ---")
        parts.append(f"Intent: {intent.value}")
        parts.append(self._intent_instruction(persona, intent))

        # Response format hint
        parts.append(_RESPONSE_FORMAT_HINT)

        return "\n".join(parts)

    def _intent_instruction(self, persona: str, intent: PersonaIntent) -> str:
        """Return specific instruction for this persona+intent combination."""
        if intent == PersonaIntent.OBSERVE:
            return "Synthesize the specialist telemetry above. What is the system experiencing? Be specific about metrics and trends."
        if intent == PersonaIntent.PROPOSE:
            return "Based on the observations, propose a concrete architectural solution. Reference specific files and code paths. Align with the Manifesto principles above."
        if intent == PersonaIntent.CHALLENGE:
            return "Identify risks or concerns with the current proposal. Consider data sovereignty, security boundaries, and potential for cascading failures."
        if intent == PersonaIntent.SUPPORT:
            return "Provide additional evidence or reasoning that supports the current proposal. Reference specific Manifesto principles."
        if intent == PersonaIntent.VALIDATE:
            return 'Assess the safety and blast radius of the proposal. Provide a risk narrative and conclude with a verdict: "approve" or "reject". Remember: you are advisory — the deterministic Iron Gate enforces execution.'
        return "Provide your analysis."

    def _parse_response(
        self,
        persona: str,
        intent: PersonaIntent,
        thread: HiveThread,
        raw: str,
        model: str,
    ) -> PersonaReasoningMessage:
        """Parse Doubleword response (JSON preferred, plaintext fallback)."""
        reasoning = raw
        confidence = 0.5
        manifesto_principle = None
        validate_verdict = None
        token_cost = len(raw) // 4  # rough estimate: 4 chars per token

        try:
            data = json.loads(raw)
            reasoning = data.get("reasoning", raw)
            confidence = float(data.get("confidence", 0.5))
            manifesto_principle = data.get("manifesto_principle")
            if intent == PersonaIntent.VALIDATE:
                validate_verdict = data.get("validate_verdict")
        except (json.JSONDecodeError, TypeError, ValueError):
            pass  # Use plaintext fallback values

        return PersonaReasoningMessage(
            thread_id=thread.thread_id,
            persona=persona,
            role=_PERSONA_ROLE_MAP[persona],
            intent=intent,
            references=[m.message_id for m in thread.messages[-3:]],
            reasoning=reasoning,
            confidence=confidence,
            model_used=model,
            token_cost=token_cost,
            manifesto_principle=manifesto_principle,
            validate_verdict=validate_verdict,
        )

    def _failure_message(
        self,
        persona: str,
        intent: PersonaIntent,
        thread: HiveThread,
        reason: str,
    ) -> PersonaReasoningMessage:
        """Return a zero-confidence message on failure."""
        return PersonaReasoningMessage(
            thread_id=thread.thread_id,
            persona=persona,
            role=_PERSONA_ROLE_MAP[persona],
            intent=intent,
            references=[],
            reasoning=reason,
            confidence=0.0,
            model_used="none",
            token_cost=0,
        )
```

- [ ] **2.4: Run tests**

Run: `python3 -m pytest tests/test_hive_persona_engine.py -v`
Expected: ALL PASS

- [ ] **2.5: Commit**

```bash
git add backend/hive/persona_engine.py tests/test_hive_persona_engine.py
git commit -m "feat(hive): add PersonaEngine with layered prompts (A+B)"
```

---

## Task 3: Hive Service

**Files:**
- Create: `backend/hive/hive_service.py`
- Create: `tests/test_hive_service.py`

This is the orchestrator. It wires all components and drives the debate loop.

- [ ] **3.1: Write failing tests**

```python
# tests/test_hive_service.py
"""Tests for HiveService — orchestrator wiring all Hive components."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import pytest_asyncio

from backend.hive.cognitive_fsm import CognitiveEvent, CognitiveFsm
from backend.hive.hive_service import HiveService
from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.model_router import HiveModelRouter
from backend.hive.persona_engine import PersonaEngine
from backend.hive.thread_manager import ThreadManager
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)
from backend.neural_mesh.data_models import AgentMessage, MessageType


def _make_persona_response(reasoning, confidence=0.87, verdict=None, principle=None):
    """Helper to build canned Doubleword JSON response."""
    d = {"reasoning": reasoning, "confidence": confidence}
    if verdict:
        d["validate_verdict"] = verdict
    if principle:
        d["manifesto_principle"] = principle
    return json.dumps(d)


@pytest.fixture
def mock_doubleword():
    dw = AsyncMock()
    dw.is_available = True
    # Default: rotate through observe, propose, validate responses
    dw.prompt_only = AsyncMock(side_effect=[
        _make_persona_response("RAM pressure from stale SHM segments.", 0.9, principle="$3 Spinal Cord"),
        _make_persona_response("Add TTL eviction to FramePipeline.", 0.87, principle="$3 Spinal Cord"),
        _make_persona_response("AST clean. Low risk. Approved.", 0.95, verdict="approve"),
    ])
    return dw


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.subscribe_broadcast = AsyncMock()
    return bus


@pytest.fixture
def mock_governed_loop():
    gl = AsyncMock()
    gl.submit = AsyncMock(return_value=MagicMock(
        terminal_phase="COMPLETE",
        reason_code="success",
    ))
    return gl


@pytest.fixture
def service(tmp_path, mock_doubleword, mock_bus, mock_governed_loop):
    return HiveService(
        bus=mock_bus,
        governed_loop=mock_governed_loop,
        doubleword=mock_doubleword,
        state_dir=tmp_path,
    )


class TestBusSubscription:

    @pytest.mark.asyncio
    async def test_start_subscribes_to_hive_agent_log(self, service, mock_bus):
        await service.start()
        mock_bus.subscribe_broadcast.assert_any_call(
            MessageType.HIVE_AGENT_LOG, service._on_agent_log
        )
        await service.stop()

    @pytest.mark.asyncio
    async def test_on_agent_log_creates_thread(self, service):
        await service.start()
        msg = AgentMessage(
            from_agent="health_monitor_agent",
            message_type=MessageType.HIVE_AGENT_LOG,
            payload={
                "category": "memory_pressure",
                "severity": "warning",
                "trinity_parent": "jarvis",
                "data": {"metric": "ram_percent", "value": 87.3},
            },
        )
        await service._on_agent_log(msg)
        assert len(service.thread_manager.active_threads) >= 1
        await service.stop()


class TestDebateLoop:

    @pytest.mark.asyncio
    async def test_full_debate_reaches_consensus(self, service, mock_doubleword):
        await service.start()

        # Create thread and start debate
        thread = service.thread_manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)

        # FSM to FLOW
        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        await service._run_debate_round(thread.thread_id)

        # After debate: 3 persona messages added (observe, propose, validate)
        t = service.thread_manager.get_thread(thread.thread_id)
        persona_msgs = [m for m in t.messages if isinstance(m, PersonaReasoningMessage)]
        assert len(persona_msgs) == 3
        assert t.state == ThreadState.CONSENSUS
        await service.stop()

    @pytest.mark.asyncio
    async def test_reject_triggers_retry(self, service, mock_doubleword):
        await service.start()

        # First propose rejected, second approved
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            _make_persona_response("RAM high.", 0.9),
            _make_persona_response("Try fix A.", 0.8),
            _make_persona_response("Too risky.", 0.3, verdict="reject"),
            _make_persona_response("Try fix B instead.", 0.85),
            _make_persona_response("Fix B is safe. Approved.", 0.92, verdict="approve"),
        ])

        thread = service.thread_manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)
        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        await service._run_debate_round(thread.thread_id)

        t = service.thread_manager.get_thread(thread.thread_id)
        assert t.state == ThreadState.CONSENSUS
        # Should have 5 persona messages: observe, propose1, reject, propose2, approve
        persona_msgs = [m for m in t.messages if isinstance(m, PersonaReasoningMessage)]
        assert len(persona_msgs) == 5
        await service.stop()

    @pytest.mark.asyncio
    async def test_max_rejects_goes_stale(self, service, mock_doubleword):
        await service.start()

        # All validates reject
        mock_doubleword.prompt_only = AsyncMock(side_effect=[
            _make_persona_response("RAM high.", 0.9),
            _make_persona_response("Fix A.", 0.8),
            _make_persona_response("Rejected.", 0.3, verdict="reject"),
            _make_persona_response("Fix B.", 0.8),
            _make_persona_response("Still rejected.", 0.3, verdict="reject"),
        ])

        thread = service.thread_manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)
        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        await service._run_debate_round(thread.thread_id)

        t = service.thread_manager.get_thread(thread.thread_id)
        assert t.state == ThreadState.STALE
        await service.stop()


class TestConsensusHandoff:

    @pytest.mark.asyncio
    async def test_consensus_submits_to_governed_loop(self, service, mock_doubleword, mock_governed_loop):
        await service.start()

        thread = service.thread_manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)
        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        await service._run_debate_round(thread.thread_id)

        # Verify submit was called
        mock_governed_loop.submit.assert_called_once()
        ctx = mock_governed_loop.submit.call_args[0][0]
        assert ctx.causal_trace_id == thread.thread_id
        assert "hive_consensus" in mock_governed_loop.submit.call_args[1].get("trigger_source", mock_governed_loop.submit.call_args[0][1] if len(mock_governed_loop.submit.call_args[0]) > 1 else "")
        await service.stop()

    @pytest.mark.asyncio
    async def test_consensus_transitions_thread_to_executing(self, service, mock_doubleword, mock_governed_loop):
        await service.start()

        thread = service.thread_manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)
        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()

        await service._run_debate_round(thread.thread_id)

        t = service.thread_manager.get_thread(thread.thread_id)
        assert t.state == ThreadState.EXECUTING
        assert t.linked_op_id is not None
        await service.stop()


class TestFlowCompletion:

    @pytest.mark.asyncio
    async def test_all_threads_resolved_fires_spindown(self, service, mock_doubleword):
        await service.start()

        service.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        service.fsm.apply_last_decision()
        assert service.fsm.state == CognitiveState.FLOW

        thread = service.thread_manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        service.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
        service._flow_thread_ids.add(thread.thread_id)

        await service._run_debate_round(thread.thread_id)

        # Thread should be EXECUTING now, mark as RESOLVED
        service.thread_manager.transition(thread.thread_id, ThreadState.RESOLVED)
        service._flow_thread_ids.discard(thread.thread_id)
        await service._check_flow_completion()

        assert service.fsm.state == CognitiveState.BASELINE
        await service.stop()
```

- [ ] **3.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_service.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **3.3: Implement hive_service.py**

```python
# backend/hive/hive_service.py
"""HiveService — top-level orchestrator for the Autonomous Engineering Hive.

Wires all Hive components into a running system:
  - Subscribes to HIVE_AGENT_LOG on the AgentCommunicationBus
  - Drives the debate loop (observe → propose → validate, with reject retry)
  - Hands consensus to Ouroboros via GovernedLoopService.submit()
  - Runs the REM idle poll timer
  - Projects all events through HudRelayAgent
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

from backend.hive.cognitive_fsm import CognitiveEvent, CognitiveFsm
from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.model_router import HiveModelRouter
from backend.hive.ouroboros_handoff import serialize_consensus
from backend.hive.persona_engine import PersonaEngine
from backend.hive.thread_manager import ThreadManager
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)
from backend.neural_mesh.data_models import AgentMessage, MessageType

logger = logging.getLogger(__name__)

_MAX_REJECTS = int(os.environ.get("JARVIS_HIVE_MAX_REJECTS", "2"))
_REM_POLL_INTERVAL_S = float(os.environ.get("JARVIS_HIVE_REM_POLL_INTERVAL_S", "1800"))
_OUROBOROS_MODE = os.environ.get("JARVIS_HIVE_OUROBOROS_MODE", "autonomous")
_DEBATE_TIMEOUT_S = float(os.environ.get("JARVIS_HIVE_FLOW_DEBATE_TIMEOUT_M", "15")) * 60


class HiveService:
    """Top-level orchestrator for the Autonomous Engineering Hive."""

    def __init__(
        self,
        bus: Any,  # AgentCommunicationBus
        governed_loop: Any,  # GovernedLoopService (Optional — None if not booted)
        doubleword: Any,  # DoublewordProvider
        state_dir: Optional[Path] = None,
    ) -> None:
        self._bus = bus
        self._governed_loop = governed_loop
        _dir = state_dir or Path(os.environ.get(
            "JARVIS_HIVE_STATE_DIR", str(Path.home() / ".jarvis" / "hive")
        ))

        self.fsm = CognitiveFsm(state_file=_dir / "cognitive_state.json")
        self.thread_manager = ThreadManager(
            storage_dir=_dir / "threads",
        )
        self._model_router = HiveModelRouter()
        self._persona_engine = PersonaEngine(
            doubleword=doubleword,
            model_router=self._model_router,
        )
        self._relay = HudRelayAgent()
        self._flow_thread_ids: Set[str] = set()
        self._rem_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_activity_mono = time.monotonic()

    async def start(self) -> None:
        """Boot the Hive: subscribe to bus, start REM poll timer."""
        self._running = True
        await self._bus.subscribe_broadcast(
            MessageType.HIVE_AGENT_LOG, self._on_agent_log
        )
        self._rem_task = asyncio.create_task(self._rem_poll_loop())
        logger.info("[HiveService] Started")

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._rem_task and not self._rem_task.done():
            self._rem_task.cancel()
            try:
                await self._rem_task
            except asyncio.CancelledError:
                pass
        # Persist all active threads
        for tid in list(self.thread_manager.active_threads):
            self.thread_manager.persist_thread(tid)
        logger.info("[HiveService] Stopped")

    # ------------------------------------------------------------------
    # Bus handler
    # ------------------------------------------------------------------

    async def _on_agent_log(self, message: AgentMessage) -> None:
        """Handle incoming HIVE_AGENT_LOG from the bus."""
        self._last_activity_mono = time.monotonic()
        payload = message.payload

        category = payload.get("category", "unknown")
        severity = payload.get("severity", "info")
        trinity_parent = payload.get("trinity_parent", "jarvis")
        agent_name = message.from_agent

        # Create or find a thread for this category
        thread = self._find_or_create_thread(category, agent_name)

        log_msg = AgentLogMessage(
            thread_id=thread.thread_id,
            agent_name=agent_name,
            trinity_parent=trinity_parent,
            severity=severity,
            category=category,
            payload=payload.get("data", payload),
        )
        self.thread_manager.add_message(thread.thread_id, log_msg)
        await self._relay.project_message(log_msg)

        # If severity warrants it and we're in BASELINE, trigger FLOW
        if severity in ("warning", "error", "critical") and self.fsm.state == CognitiveState.BASELINE:
            decision = self.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
            if not decision.noop:
                self.fsm.apply_last_decision()
                await self._relay.project_cognitive_transition(
                    decision.from_state.value, decision.to_state.value, decision.reason_code,
                )

        # If thread is OPEN and we're in FLOW, start debating
        if thread.state == ThreadState.OPEN and self.fsm.state == CognitiveState.FLOW:
            self.thread_manager.transition(thread.thread_id, ThreadState.DEBATING)
            self._flow_thread_ids.add(thread.thread_id)
            await self._relay.project_lifecycle(thread.thread_id, "debating")
            asyncio.create_task(self._run_debate_round(thread.thread_id))

    def _find_or_create_thread(self, category: str, agent_name: str) -> Any:
        """Find an existing OPEN/DEBATING thread for this category, or create one."""
        for thread in self.thread_manager.active_threads.values():
            if thread.trigger_event == f"{agent_name}:{category}" and thread.state in (
                ThreadState.OPEN, ThreadState.DEBATING
            ):
                return thread
        return self.thread_manager.create_thread(
            title=f"{category.replace('_', ' ').title()} ({agent_name})",
            trigger_event=f"{agent_name}:{category}",
            cognitive_state=self.fsm.state,
        )

    # ------------------------------------------------------------------
    # Debate loop
    # ------------------------------------------------------------------

    async def _run_debate_round(self, thread_id: str) -> None:
        """Drive one full debate round: observe → propose → validate (with retry)."""
        thread = self.thread_manager.get_thread(thread_id)
        if thread is None or thread.state != ThreadState.DEBATING:
            return

        reject_count = 0

        # Step 1: JARVIS observes
        observe_msg = await self._persona_engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, thread,
        )
        self.thread_manager.add_message(thread_id, observe_msg)
        await self._relay.project_message(observe_msg)

        while reject_count < _MAX_REJECTS:
            # Step 2: J-Prime proposes
            thread = self.thread_manager.get_thread(thread_id)
            propose_msg = await self._persona_engine.generate_reasoning(
                "j_prime", PersonaIntent.PROPOSE, thread,
            )
            self.thread_manager.add_message(thread_id, propose_msg)
            await self._relay.project_message(propose_msg)

            # Check budget after each LLM call
            advance = self.thread_manager.check_and_advance(thread_id)
            if advance == ThreadState.STALE:
                await self._relay.project_lifecycle(thread_id, "stale")
                self._flow_thread_ids.discard(thread_id)
                await self._check_flow_completion()
                return

            # Step 3: Reactor validates
            thread = self.thread_manager.get_thread(thread_id)
            validate_msg = await self._persona_engine.generate_reasoning(
                "reactor", PersonaIntent.VALIDATE, thread,
            )
            self.thread_manager.add_message(thread_id, validate_msg)
            await self._relay.project_message(validate_msg)

            # Check budget
            advance = self.thread_manager.check_and_advance(thread_id)
            if advance == ThreadState.STALE:
                await self._relay.project_lifecycle(thread_id, "stale")
                self._flow_thread_ids.discard(thread_id)
                await self._check_flow_completion()
                return

            # Check consensus
            if advance == ThreadState.CONSENSUS:
                await self._relay.project_lifecycle(thread_id, "consensus")
                await self._handle_consensus(thread_id)
                return

            # Check verdict manually (check_and_advance may not have triggered)
            if validate_msg.validate_verdict == "approve":
                # Force consensus check
                thread = self.thread_manager.get_thread(thread_id)
                if thread.is_consensus_ready():
                    self.thread_manager.transition(thread_id, ThreadState.CONSENSUS)
                    await self._relay.project_lifecycle(thread_id, "consensus")
                    await self._handle_consensus(thread_id)
                    return

            # Rejected — retry
            reject_count += 1
            logger.info(
                "[HiveService] Thread %s: Reactor rejected (%d/%d)",
                thread_id, reject_count, _MAX_REJECTS,
            )

        # Max rejects reached → STALE
        self.thread_manager.transition(thread_id, ThreadState.STALE)
        await self._relay.project_lifecycle(thread_id, "stale")
        self._flow_thread_ids.discard(thread_id)
        await self._check_flow_completion()

    # ------------------------------------------------------------------
    # Consensus handoff
    # ------------------------------------------------------------------

    async def _handle_consensus(self, thread_id: str) -> None:
        """Hand consensus to Ouroboros via GovernedLoopService."""
        thread = self.thread_manager.get_thread(thread_id)
        if thread is None or thread.state != ThreadState.CONSENSUS:
            return

        # Extract target files from thread messages
        target_files = self._extract_target_files(thread)

        ctx = serialize_consensus(thread, target_files=target_files)
        thread.linked_op_id = ctx.op_id
        self.thread_manager.transition(thread_id, ThreadState.EXECUTING)
        await self._relay.project_lifecycle(
            thread_id, "executing", {"linked_op_id": ctx.op_id}
        )

        if self._governed_loop is not None:
            try:
                await self._governed_loop.submit(ctx, trigger_source="hive_consensus")
            except Exception:
                logger.exception("[HiveService] Ouroboros submit failed for thread %s", thread_id)

        self._flow_thread_ids.discard(thread_id)
        await self._check_flow_completion()

    def _extract_target_files(self, thread: Any) -> tuple:
        """Extract file paths mentioned in thread messages."""
        files = set()
        for msg in thread.messages:
            if isinstance(msg, PersonaReasoningMessage):
                # Simple heuristic: find paths like backend/foo/bar.py
                for word in msg.reasoning.split():
                    if "/" in word and "." in word.split("/")[-1]:
                        clean = word.strip(".,;:()\"'`")
                        if clean and not clean.startswith("http"):
                            files.add(clean)
        return tuple(sorted(files)) if files else ("backend/hive/",)

    # ------------------------------------------------------------------
    # Flow completion check
    # ------------------------------------------------------------------

    async def _check_flow_completion(self) -> None:
        """If all FLOW threads are done, spin down to BASELINE."""
        if self.fsm.state != CognitiveState.FLOW:
            return
        if not self._flow_thread_ids:
            decision = self.fsm.decide(
                CognitiveEvent.SPINDOWN, spindown_reason="all_threads_resolved"
            )
            if not decision.noop:
                self.fsm.apply_last_decision()
                await self._relay.project_cognitive_transition(
                    decision.from_state.value, decision.to_state.value, decision.reason_code,
                )

    # ------------------------------------------------------------------
    # REM poll loop
    # ------------------------------------------------------------------

    async def _rem_poll_loop(self) -> None:
        """Periodic check for REM eligibility. Poll interval != REM threshold."""
        while self._running:
            try:
                await asyncio.sleep(_REM_POLL_INTERVAL_S)
            except asyncio.CancelledError:
                return

            if self.fsm.state != CognitiveState.BASELINE:
                continue

            idle_seconds = time.monotonic() - self._last_activity_mono
            try:
                import psutil
                load = psutil.cpu_percent(interval=0.1)
            except ImportError:
                load = 0.0

            decision = self.fsm.decide(
                CognitiveEvent.REM_TRIGGER,
                idle_seconds=idle_seconds,
                system_load_pct=load,
                graduation_candidates=0,  # TODO: wire to actual graduation check
            )
            if not decision.noop:
                self.fsm.apply_last_decision()
                await self._relay.project_cognitive_transition(
                    decision.from_state.value, decision.to_state.value, decision.reason_code,
                )
                logger.info("[HiveService] Entering REM cycle")
```

- [ ] **3.4: Run tests**

Run: `python3 -m pytest tests/test_hive_service.py -v`
Expected: ALL PASS

- [ ] **3.5: Commit**

```bash
git add backend/hive/hive_service.py tests/test_hive_service.py
git commit -m "feat(hive): add HiveService orchestrator (bus, debate loop, handoff)"
```

---

## Task 4: Service Integration Test

**Files:**
- Create: `tests/test_hive_service_integration.py`

Full round with all real components (Doubleword mocked).

- [ ] **4.1: Write integration test**

```python
# tests/test_hive_service_integration.py
"""Integration test: HiveService full pipeline with mocked Doubleword."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.hive.hive_service import HiveService
from backend.hive.thread_models import (
    CognitiveState,
    PersonaReasoningMessage,
    ThreadState,
)
from backend.neural_mesh.data_models import AgentMessage, MessageType


def _response(reasoning, confidence=0.87, verdict=None, principle=None):
    d = {"reasoning": reasoning, "confidence": confidence}
    if verdict:
        d["validate_verdict"] = verdict
    if principle:
        d["manifesto_principle"] = principle
    return json.dumps(d)


class TestHiveServiceIntegration:

    @pytest.mark.asyncio
    async def test_agent_log_triggers_full_pipeline(self, tmp_path):
        """HIVE_AGENT_LOG → thread creation → debate → consensus → submit."""
        dw = AsyncMock()
        dw.is_available = True
        dw.prompt_only = AsyncMock(side_effect=[
            _response("Memory pressure from SHM segments.", 0.9),
            _response("Add TTL eviction to backend/vision/frame_pipeline.py", 0.87),
            _response("AST clean. Approved.", 0.95, verdict="approve"),
        ])

        bus = AsyncMock()
        bus.subscribe_broadcast = AsyncMock()

        gl = AsyncMock()
        gl.submit = AsyncMock(return_value=MagicMock())

        service = HiveService(bus=bus, governed_loop=gl, doubleword=dw, state_dir=tmp_path)
        await service.start()

        # Simulate agent log arriving
        msg = AgentMessage(
            from_agent="health_monitor_agent",
            message_type=MessageType.HIVE_AGENT_LOG,
            payload={
                "category": "memory_pressure",
                "severity": "warning",
                "trinity_parent": "jarvis",
                "data": {"metric": "ram_percent", "value": 87.3},
            },
        )
        await service._on_agent_log(msg)

        # Wait for debate task to complete
        await asyncio.sleep(0.1)
        pending = [t for t in asyncio.all_tasks() if "debate" in str(t.get_coro())]
        for t in pending:
            try:
                await asyncio.wait_for(t, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        # Verify full pipeline executed
        assert dw.prompt_only.call_count == 3  # observe, propose, validate
        gl.submit.assert_called_once()

        # Verify thread reached EXECUTING
        threads = list(service.thread_manager.active_threads.values())
        assert any(t.state == ThreadState.EXECUTING for t in threads)

        # Verify FSM spun down (all threads resolved/executing, flow_thread_ids empty)
        # The service should have spun down since the thread was moved out of _flow_thread_ids
        assert service.fsm.state == CognitiveState.BASELINE

        await service.stop()

    @pytest.mark.asyncio
    async def test_info_severity_does_not_trigger_flow(self, tmp_path):
        """Info-level agent logs should not trigger FLOW from BASELINE."""
        dw = AsyncMock()
        dw.is_available = True
        bus = AsyncMock()
        bus.subscribe_broadcast = AsyncMock()

        service = HiveService(bus=bus, governed_loop=None, doubleword=dw, state_dir=tmp_path)
        await service.start()

        msg = AgentMessage(
            from_agent="some_agent",
            message_type=MessageType.HIVE_AGENT_LOG,
            payload={
                "category": "routine_check",
                "severity": "info",
                "trinity_parent": "jarvis",
                "data": {},
            },
        )
        await service._on_agent_log(msg)

        assert service.fsm.state == CognitiveState.BASELINE
        assert dw.prompt_only.call_count == 0
        await service.stop()


import asyncio
```

- [ ] **4.2: Run integration test**

Run: `python3 -m pytest tests/test_hive_service_integration.py -v`
Expected: ALL PASS

- [ ] **4.3: Run full Hive test suite**

Run: `python3 -m pytest tests/test_hive_*.py -v`
Expected: ALL PASS

- [ ] **4.4: Commit**

```bash
git add tests/test_hive_service_integration.py
git commit -m "test(hive): add HiveService integration test (agent_log -> consensus -> submit)"
```

---

## Summary

| Task | Component | Dependencies |
|------|-----------|-------------|
| 1 | Manifesto Slices | None |
| 2 | Persona Engine | Task 1 |
| 3 | Hive Service | Task 2 |
| 4 | Service Integration Test | Task 3 |

Tasks 1 and 2 can be parallelized. Task 3 depends on Task 2. Task 4 depends on Task 3.

**Not in this plan (separate plans):**
- Layer C code injection (behind `JARVIS_HIVE_CODE_INJECTION` flag)
- REM council session logic (what the council reviews)
- SwiftUI HiveView + HiveStore
- Vercel `/dashboard/hive`
