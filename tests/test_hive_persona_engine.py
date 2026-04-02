"""Tests for backend.hive.persona_engine — PersonaEngine layered prompts & inference."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from backend.hive.manifesto_slices import ROLE_PREFIXES, get_manifesto_slice
from backend.hive.model_router import HiveModelRouter
from backend.hive.persona_engine import PERSONA_ROLE_MAP, PersonaEngine
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
)


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def mock_doubleword() -> AsyncMock:
    """Return a mock Doubleword client with a default JSON response."""
    dw = AsyncMock()
    dw.prompt_only = AsyncMock(
        return_value=json.dumps(
            {
                "reasoning": "Telemetry shows stable metrics across all zones.",
                "confidence": 0.87,
                "manifesto_principle": "Absolute Observability",
            }
        )
    )
    return dw


@pytest.fixture
def router() -> HiveModelRouter:
    """Return a model router with default config."""
    return HiveModelRouter()


@pytest.fixture
def engine(mock_doubleword: AsyncMock, router: HiveModelRouter) -> PersonaEngine:
    """Return a PersonaEngine wired to mock dependencies."""
    return PersonaEngine(doubleword=mock_doubleword, model_router=router)


@pytest.fixture
def sample_thread() -> HiveThread:
    """Return a sample thread in FLOW state with agent log messages."""
    thread = HiveThread(
        title="Test thread: build regression",
        trigger_event="test_failure",
        cognitive_state=CognitiveState.FLOW,
        token_budget=20000,
        debate_deadline_s=120.0,
    )
    thread.add_message(
        AgentLogMessage(
            thread_id=thread.thread_id,
            agent_name="build_monitor",
            trinity_parent="jarvis",
            severity="error",
            category="build",
            payload={"exit_code": 1, "module": "backend.core"},
        )
    )
    thread.add_message(
        AgentLogMessage(
            thread_id=thread.thread_id,
            agent_name="test_runner",
            trinity_parent="jarvis",
            severity="warning",
            category="test",
            payload={"failed": 3, "passed": 147},
        )
    )
    return thread


# ============================================================================
# PROMPT CONSTRUCTION — LAYER A (Role Prefix)
# ============================================================================


class TestPromptRolePrefix:
    """Verify Layer A: role prefix appears in the prompt."""

    @pytest.mark.asyncio
    async def test_jarvis_prompt_contains_role_prefix(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        prompt = engine._build_prompt("jarvis", PersonaIntent.OBSERVE, sample_thread)
        # The ROLE_PREFIXES for jarvis mentions "Body and Senses"
        assert "Body and Senses" in prompt

    @pytest.mark.asyncio
    async def test_j_prime_prompt_contains_role_prefix(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        prompt = engine._build_prompt("j_prime", PersonaIntent.PROPOSE, sample_thread)
        assert "Mind" in prompt

    @pytest.mark.asyncio
    async def test_reactor_prompt_contains_role_prefix(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        prompt = engine._build_prompt("reactor", PersonaIntent.VALIDATE, sample_thread)
        assert "Immune System" in prompt


# ============================================================================
# PROMPT CONSTRUCTION — LAYER B (Manifesto Slice)
# ============================================================================


class TestPromptManifestoSlice:
    """Verify Layer B: manifesto slice content appears in the prompt."""

    @pytest.mark.asyncio
    async def test_observe_prompt_contains_manifesto_slice(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        prompt = engine._build_prompt("jarvis", PersonaIntent.OBSERVE, sample_thread)
        slice_content = get_manifesto_slice(PersonaIntent.OBSERVE)
        # At least the first 50 chars of the slice should appear
        assert slice_content[:50] in prompt

    @pytest.mark.asyncio
    async def test_validate_prompt_contains_manifesto_slice(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        prompt = engine._build_prompt("reactor", PersonaIntent.VALIDATE, sample_thread)
        slice_content = get_manifesto_slice(PersonaIntent.VALIDATE)
        assert slice_content[:50] in prompt


# ============================================================================
# PROMPT CONSTRUCTION — THREAD CONTEXT
# ============================================================================


class TestPromptThreadContext:
    """Verify thread context (agent names, categories) appears in prompt."""

    @pytest.mark.asyncio
    async def test_prompt_contains_agent_names(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        prompt = engine._build_prompt("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert "build_monitor" in prompt
        assert "test_runner" in prompt

    @pytest.mark.asyncio
    async def test_prompt_contains_categories(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        prompt = engine._build_prompt("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert "build" in prompt
        assert "test" in prompt

    @pytest.mark.asyncio
    async def test_prompt_contains_severity(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        prompt = engine._build_prompt("jarvis", PersonaIntent.OBSERVE, sample_thread)
        assert "ERROR" in prompt
        assert "WARNING" in prompt


# ============================================================================
# MODEL ROUTING
# ============================================================================


class TestModelRouting:
    """Verify correct model selection based on cognitive state."""

    @pytest.mark.asyncio
    async def test_flow_state_uses_397b_model(
        self,
        engine: PersonaEngine,
        mock_doubleword: AsyncMock,
        sample_thread: HiveThread,
    ) -> None:
        """FLOW state should dispatch to the 397B model."""
        await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        call_kwargs = mock_doubleword.prompt_only.call_args
        assert "397B" in call_kwargs.kwargs.get("model", call_kwargs[1].get("model", ""))

    @pytest.mark.asyncio
    async def test_flow_state_max_tokens(
        self,
        engine: PersonaEngine,
        mock_doubleword: AsyncMock,
        sample_thread: HiveThread,
    ) -> None:
        """FLOW state should use 10000 max_tokens."""
        await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        call_kwargs = mock_doubleword.prompt_only.call_args
        max_tokens = call_kwargs.kwargs.get(
            "max_tokens", call_kwargs[1].get("max_tokens", 0)
        )
        assert max_tokens == 10000

    @pytest.mark.asyncio
    async def test_rem_state_uses_35b_model(
        self,
        engine: PersonaEngine,
        mock_doubleword: AsyncMock,
    ) -> None:
        """REM state should dispatch to the 35B model."""
        thread = HiveThread(
            title="REM thread",
            trigger_event="minor_issue",
            cognitive_state=CognitiveState.REM,
            token_budget=8000,
            debate_deadline_s=60.0,
        )
        await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, thread)
        call_kwargs = mock_doubleword.prompt_only.call_args
        assert "35B" in call_kwargs.kwargs.get("model", call_kwargs[1].get("model", ""))


# ============================================================================
# CALLER ID
# ============================================================================


class TestCallerId:
    """Verify caller_id includes persona and intent."""

    @pytest.mark.asyncio
    async def test_caller_id_format(
        self,
        engine: PersonaEngine,
        mock_doubleword: AsyncMock,
        sample_thread: HiveThread,
    ) -> None:
        await engine.generate_reasoning("jarvis", PersonaIntent.OBSERVE, sample_thread)
        call_kwargs = mock_doubleword.prompt_only.call_args
        caller_id = call_kwargs.kwargs.get(
            "caller_id", call_kwargs[1].get("caller_id", "")
        )
        assert "jarvis" in caller_id
        assert "observe" in caller_id

    @pytest.mark.asyncio
    async def test_caller_id_j_prime_propose(
        self,
        engine: PersonaEngine,
        mock_doubleword: AsyncMock,
        sample_thread: HiveThread,
    ) -> None:
        await engine.generate_reasoning("j_prime", PersonaIntent.PROPOSE, sample_thread)
        call_kwargs = mock_doubleword.prompt_only.call_args
        caller_id = call_kwargs.kwargs.get(
            "caller_id", call_kwargs[1].get("caller_id", "")
        )
        assert "j_prime" in caller_id
        assert "propose" in caller_id


# ============================================================================
# RESPONSE — CORRECT MESSAGE SHAPE
# ============================================================================


class TestResponseShape:
    """Verify returned PersonaReasoningMessage has correct fields."""

    @pytest.mark.asyncio
    async def test_returns_correct_persona(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        msg = await engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.persona == "jarvis"

    @pytest.mark.asyncio
    async def test_returns_correct_role(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        msg = await engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.role == "body"

    @pytest.mark.asyncio
    async def test_returns_correct_intent(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        msg = await engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.intent == PersonaIntent.OBSERVE

    @pytest.mark.asyncio
    async def test_returns_correct_thread_id(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        msg = await engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.thread_id == sample_thread.thread_id

    @pytest.mark.asyncio
    async def test_j_prime_role_is_mind(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        msg = await engine.generate_reasoning(
            "j_prime", PersonaIntent.PROPOSE, sample_thread
        )
        assert msg.role == "mind"

    @pytest.mark.asyncio
    async def test_reactor_role_is_immune_system(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        msg = await engine.generate_reasoning(
            "reactor", PersonaIntent.VALIDATE, sample_thread
        )
        assert msg.role == "immune_system"


# ============================================================================
# JSON PARSING
# ============================================================================


class TestJsonParsing:
    """Verify reasoning and confidence are extracted from JSON responses."""

    @pytest.mark.asyncio
    async def test_extracts_reasoning(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        msg = await engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert "stable metrics" in msg.reasoning

    @pytest.mark.asyncio
    async def test_extracts_confidence(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        msg = await engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.confidence == pytest.approx(0.87, abs=0.01)

    @pytest.mark.asyncio
    async def test_extracts_manifesto_principle(
        self, engine: PersonaEngine, sample_thread: HiveThread
    ) -> None:
        msg = await engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.manifesto_principle == "Absolute Observability"

    @pytest.mark.asyncio
    async def test_token_cost_from_json(
        self,
        engine: PersonaEngine,
        mock_doubleword: AsyncMock,
        sample_thread: HiveThread,
    ) -> None:
        raw = mock_doubleword.prompt_only.return_value
        msg = await engine.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.token_cost == len(raw) // 4


# ============================================================================
# VALIDATE INTENT — VERDICT EXTRACTION
# ============================================================================


class TestValidateVerdict:
    """Verify reactor validate messages extract validate_verdict."""

    @pytest.mark.asyncio
    async def test_validate_verdict_approve(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        mock_doubleword.prompt_only.return_value = json.dumps(
            {
                "reasoning": "Proposal is safe with minimal blast radius.",
                "confidence": 0.92,
                "manifesto_principle": "Iron Gate",
                "validate_verdict": "approve",
            }
        )
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "reactor", PersonaIntent.VALIDATE, sample_thread
        )
        assert msg.validate_verdict == "approve"
        assert msg.confidence == pytest.approx(0.92, abs=0.01)

    @pytest.mark.asyncio
    async def test_validate_verdict_reject(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        mock_doubleword.prompt_only.return_value = json.dumps(
            {
                "reasoning": "Missing rollback strategy.",
                "confidence": 0.78,
                "manifesto_principle": "Iron Gate",
                "validate_verdict": "reject",
            }
        )
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "reactor", PersonaIntent.VALIDATE, sample_thread
        )
        assert msg.validate_verdict == "reject"


# ============================================================================
# PLAINTEXT FALLBACK
# ============================================================================


class TestPlaintextFallback:
    """Verify plaintext responses are handled with confidence=0.5."""

    @pytest.mark.asyncio
    async def test_plaintext_response_confidence(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        mock_doubleword.prompt_only.return_value = (
            "The build is failing due to a missing import in backend.core."
        )
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.confidence == 0.5

    @pytest.mark.asyncio
    async def test_plaintext_response_reasoning(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        raw_text = "The build is failing due to a missing import in backend.core."
        mock_doubleword.prompt_only.return_value = raw_text
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.reasoning == raw_text

    @pytest.mark.asyncio
    async def test_plaintext_token_cost(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        raw_text = "Some plaintext reasoning about the issue at hand."
        mock_doubleword.prompt_only.return_value = raw_text
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.token_cost == len(raw_text) // 4


# ============================================================================
# FAILURE HANDLING
# ============================================================================


class TestFailureHandling:
    """Verify inference failures produce confidence=0.0 messages."""

    @pytest.mark.asyncio
    async def test_doubleword_exception_returns_zero_confidence(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        mock_doubleword.prompt_only.side_effect = RuntimeError("connection refused")
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.confidence == 0.0
        assert "[inference failed" in msg.reasoning
        assert "connection refused" in msg.reasoning

    @pytest.mark.asyncio
    async def test_empty_response_returns_zero_confidence(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        mock_doubleword.prompt_only.return_value = ""
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.confidence == 0.0

    @pytest.mark.asyncio
    async def test_whitespace_only_response_returns_zero_confidence(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        mock_doubleword.prompt_only.return_value = "   \n\t  "
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.confidence == 0.0

    @pytest.mark.asyncio
    async def test_failure_message_has_correct_persona_and_intent(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        mock_doubleword.prompt_only.side_effect = TimeoutError("timed out")
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "reactor", PersonaIntent.VALIDATE, sample_thread
        )
        assert msg.persona == "reactor"
        assert msg.role == "immune_system"
        assert msg.intent == PersonaIntent.VALIDATE
        assert msg.thread_id == sample_thread.thread_id

    @pytest.mark.asyncio
    async def test_failure_token_cost_is_zero(
        self,
        mock_doubleword: AsyncMock,
        router: HiveModelRouter,
        sample_thread: HiveThread,
    ) -> None:
        mock_doubleword.prompt_only.side_effect = RuntimeError("boom")
        eng = PersonaEngine(doubleword=mock_doubleword, model_router=router)
        msg = await eng.generate_reasoning(
            "jarvis", PersonaIntent.OBSERVE, sample_thread
        )
        assert msg.token_cost == 0


# ============================================================================
# PERSONA-ROLE MAPPING
# ============================================================================


class TestPersonaRoleMap:
    """Verify the persona-to-role mapping is correct."""

    def test_jarvis_is_body(self) -> None:
        assert PERSONA_ROLE_MAP["jarvis"] == "body"

    def test_j_prime_is_mind(self) -> None:
        assert PERSONA_ROLE_MAP["j_prime"] == "mind"

    def test_reactor_is_immune_system(self) -> None:
        assert PERSONA_ROLE_MAP["reactor"] == "immune_system"
