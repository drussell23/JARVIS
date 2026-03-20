"""Tests for GoalDecomposer — goal-based improvement decomposition."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.goal_decomposer import (
    GoalDecomposer,
    GoalDecompositionResult,
    SubTask,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeOracle:
    """Minimal Oracle stub with semantic_search."""

    def __init__(self, results=None):
        self._results = results or []

    async def semantic_search(self, query: str, k: int = 5):
        return self._results


class FakeRouter:
    """Minimal intake router stub."""

    def __init__(self, status: str = "enqueued"):
        self._status = status
        self.ingested = []

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return self._status


class FakeChain:
    """Minimal reasoning chain stub."""

    def __init__(self, expanded=None, active=True):
        self._config = MagicMock()
        self._config.is_active.return_value = active
        self._expanded = expanded

    async def process(self, command, context, trace_id, deadline):
        if self._expanded is None:
            return None
        result = MagicMock()
        result.handled = True
        result.expanded_intents = self._expanded
        return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGoalDecomposerBasic:
    """Basic decomposition without reasoning chain."""

    @pytest.mark.asyncio
    async def test_single_intent_no_chain(self):
        """Without reasoning chain, goal becomes a single sub-task."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter()
        decomposer = GoalDecomposer(oracle=oracle, intake_router=router)

        result = await decomposer.decompose("improve auth security")

        assert result.total >= 1
        assert result.submitted_count >= 1
        assert result.correlation_id.startswith("op-")
        assert len(router.ingested) >= 1

    @pytest.mark.asyncio
    async def test_no_oracle_falls_back_to_root(self):
        """Without Oracle, target_files defaults to ('.',)."""
        router = FakeRouter()
        decomposer = GoalDecomposer(oracle=None, intake_router=router)

        result = await decomposer.decompose("fix tests")

        assert result.total == 1
        assert result.sub_tasks[0].target_files == (".",)
        assert result.submitted_count == 1

    @pytest.mark.asyncio
    async def test_oracle_no_results_falls_back(self):
        """When Oracle returns empty results, falls back to root."""
        oracle = FakeOracle([])
        router = FakeRouter()
        decomposer = GoalDecomposer(oracle=oracle, intake_router=router)

        result = await decomposer.decompose("improve something")

        assert result.total == 1
        assert result.sub_tasks[0].target_files == (".",)

    @pytest.mark.asyncio
    async def test_oracle_filters_low_confidence(self):
        """Files below min_confidence are excluded."""
        oracle = FakeOracle([
            ("jarvis:backend/auth.py", 0.8),
            ("jarvis:backend/old.py", 0.1),  # below threshold
        ])
        router = FakeRouter()
        decomposer = GoalDecomposer(oracle=oracle, intake_router=router)

        result = await decomposer.decompose("improve auth")

        # Only auth.py should be included (0.8 > 0.3 threshold)
        assert result.sub_tasks[0].target_files == ("backend/auth.py",)


class TestGoalDecomposerWithChain:
    """Decomposition with reasoning chain expansion."""

    @pytest.mark.asyncio
    async def test_chain_expands_intents(self):
        """Reasoning chain expands goal into multiple sub-intents."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter()
        chain = FakeChain(expanded=["check passwords", "add 2FA", "audit logs"])

        decomposer = GoalDecomposer(
            oracle=oracle, intake_router=router, reasoning_chain=chain
        )
        result = await decomposer.decompose("improve security")

        assert result.total == 3
        assert result.submitted_count == 3
        assert len(router.ingested) == 3

    @pytest.mark.asyncio
    async def test_chain_inactive_falls_back(self):
        """When chain is inactive, goal is treated as single intent."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter()
        chain = FakeChain(active=False)

        decomposer = GoalDecomposer(
            oracle=oracle, intake_router=router, reasoning_chain=chain
        )
        result = await decomposer.decompose("improve security")

        assert result.total == 1

    @pytest.mark.asyncio
    async def test_chain_failure_falls_back(self):
        """When chain raises, goal is treated as single intent."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter()
        chain = FakeChain()
        chain.process = AsyncMock(side_effect=RuntimeError("boom"))

        decomposer = GoalDecomposer(
            oracle=oracle, intake_router=router, reasoning_chain=chain
        )
        result = await decomposer.decompose("improve security")

        assert result.total == 1
        assert result.submitted_count == 1

    @pytest.mark.asyncio
    async def test_max_subtasks_honored(self):
        """Expansion is capped at JARVIS_GOAL_MAX_SUBTASKS."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter()
        chain = FakeChain(expanded=[f"task_{i}" for i in range(20)])

        decomposer = GoalDecomposer(
            oracle=oracle, intake_router=router, reasoning_chain=chain
        )
        # Default max is 8
        result = await decomposer.decompose("big goal")

        assert result.total <= 8


class TestGoalDecomposerSubmission:
    """Tests for envelope submission behavior."""

    @pytest.mark.asyncio
    async def test_envelopes_use_ai_miner_source(self):
        """Envelopes use 'ai_miner' source (valid in IntentEnvelope)."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter()
        decomposer = GoalDecomposer(oracle=oracle, intake_router=router)

        await decomposer.decompose("fix auth")

        envelope = router.ingested[0]
        assert envelope.source == "ai_miner"

    @pytest.mark.asyncio
    async def test_envelopes_require_human_ack_by_default(self):
        """Goal-decomposed envelopes default to requires_human_ack=True."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter()
        decomposer = GoalDecomposer(oracle=oracle, intake_router=router)

        await decomposer.decompose("fix auth")

        envelope = router.ingested[0]
        assert envelope.requires_human_ack is True

    @pytest.mark.asyncio
    async def test_shared_correlation_id(self):
        """All envelopes from same goal share a correlation_id via causal_id."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter()
        chain = FakeChain(expanded=["task1", "task2"])

        decomposer = GoalDecomposer(
            oracle=oracle, intake_router=router, reasoning_chain=chain
        )
        result = await decomposer.decompose("multi-task goal")

        # All envelopes share the same causal_id (correlation)
        causal_ids = {e.causal_id for e in router.ingested}
        assert len(causal_ids) == 1
        assert causal_ids.pop() == result.correlation_id

    @pytest.mark.asyncio
    async def test_router_failure_counted_as_skipped(self):
        """Router ingest errors are counted as skipped, not crashes."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter()
        router.ingest = AsyncMock(side_effect=RuntimeError("router down"))

        decomposer = GoalDecomposer(oracle=oracle, intake_router=router)
        result = await decomposer.decompose("fix auth")

        assert result.skipped_count >= 1
        assert len(result.errors) >= 1

    @pytest.mark.asyncio
    async def test_deduplicated_status_counted_as_skipped(self):
        """Router returning 'deduplicated' counts as skipped."""
        oracle = FakeOracle([("jarvis:backend/auth.py", 0.8)])
        router = FakeRouter(status="deduplicated")

        decomposer = GoalDecomposer(oracle=oracle, intake_router=router)
        result = await decomposer.decompose("fix auth")

        assert result.skipped_count == 1
        assert result.submitted_count == 0


class TestGoalDecompositionResult:
    """Tests for the result dataclass."""

    def test_total_property(self):
        result = GoalDecompositionResult(
            original_goal="test",
            sub_tasks=[
                SubTask(intent="a", target_files=("f1",), confidence=0.8),
                SubTask(intent="b", target_files=("f2",), confidence=0.7),
            ],
            correlation_id="op-test",
        )
        assert result.total == 2

    def test_empty_errors(self):
        result = GoalDecompositionResult(
            original_goal="test",
            sub_tasks=[],
            correlation_id="op-test",
        )
        assert result.errors == []
