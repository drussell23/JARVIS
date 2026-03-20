"""Integration test: command processor -> reasoning chain orchestrator."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.reasoning_chain_orchestrator import (
    ChainConfig,
    ChainPhase,
    ChainResult,
)


class TestChainConfigActivation:
    @pytest.mark.asyncio
    async def test_chain_disabled_returns_inactive(self):
        env = {
            "JARVIS_REASONING_CHAIN_SHADOW": "false",
            "JARVIS_REASONING_CHAIN_ENABLED": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
            assert config.is_active() is False

    @pytest.mark.asyncio
    async def test_chain_shadow_enabled(self):
        env = {
            "JARVIS_REASONING_CHAIN_SHADOW": "true",
            "JARVIS_REASONING_CHAIN_ENABLED": "false",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ChainConfig.from_env()
            assert config.is_active() is True
            assert config.phase == ChainPhase.SHADOW

    @pytest.mark.asyncio
    async def test_chain_result_confirmation(self):
        result = ChainResult(
            handled=True,
            phase=ChainPhase.SOFT_ENABLE,
            trace_id="t1",
            original_command="start my day",
            expanded_intents=["check email", "check calendar"],
            needs_confirmation=True,
            confirmation_prompt="Handle separately? check email, check calendar",
        )
        assert result.needs_confirmation is True
        assert "check email" in result.confirmation_prompt

    @pytest.mark.asyncio
    async def test_chain_result_with_mind_results(self):
        result = ChainResult(
            handled=True,
            phase=ChainPhase.FULL_ENABLE,
            trace_id="t1",
            original_command="start my day",
            expanded_intents=["check email", "check calendar"],
            mind_results=[
                {"status": "plan_ready", "plan": {"sub_goals": [{"goal": "opened gmail"}]}},
                {"status": "plan_ready", "plan": {"sub_goals": [{"goal": "opened calendar"}]}},
            ],
        )
        assert result.handled is True
