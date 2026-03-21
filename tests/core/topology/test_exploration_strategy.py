"""Tests for ExplorationStrategy — multi-phase capability research."""
import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

import pytest

from backend.core.topology.exploration_strategy import (
    ExplorationConfig,
    ExplorationResult,
    ExplorationStrategy,
    ResearchFindings,
    SynthesisResult,
    ValidationResult,
)
from backend.core.topology.topology_map import CapabilityNode
from backend.core.topology.hardware_env import ComputeTier, HardwareEnvironmentState
from backend.core.topology.curiosity_engine import CuriosityTarget


def _make_hardware():
    return HardwareEnvironmentState(
        os_family="darwin", cpu_logical_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, compute_tier=ComputeTier.LOCAL_CPU, gpu=None,
        hostname="test", python_version="3.11.0",
        max_parallel_inference_tasks=4, max_shadow_harness_workers=4,
    )


def _make_target():
    node = CapabilityNode(name="parse_parquet", domain="data_io", repo_owner="reactor")
    return CuriosityTarget(
        capability=node, ucb_score=1.5, entropy_score=0.8,
        feasibility_score=1.0, rationale="Domain 'data_io' has Shannon Entropy H=0.918",
    )


def _make_prime_response(content: str):
    """Create a mock PrimeResponse."""
    mock = MagicMock()
    mock.content = content
    mock.source = "mock_prime"
    mock.tokens_used = 100
    return mock


class TestExplorationConfig:
    def test_from_env_defaults(self):
        config = ExplorationConfig.from_env()
        assert config.max_web_fetches == 5
        assert config.synthesis_max_tokens == 8192
        assert config.test_timeout_s == 120.0

    def test_from_env_override(self):
        with patch.dict("os.environ", {"JARVIS_EXPLORE_MAX_TOKENS": "4096"}):
            config = ExplorationConfig.from_env()
            assert config.synthesis_max_tokens == 4096


class TestResearchFindings:
    def test_frozen(self):
        r = ResearchFindings({}, {}, "", [], [])
        with pytest.raises(AttributeError):
            r.web_docs = {"new": "data"}


class TestExplorationStrategy:
    @pytest.mark.asyncio
    async def test_run_without_prime_client_returns_blocked(self):
        config = ExplorationConfig()
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ExplorationStrategy(
                config=config,
                scratch_path=tmpdir,
                prime_client=None,
            )
            target = _make_target()
            hw = _make_hardware()
            sem = asyncio.Semaphore(4)
            result = await strategy.run(target, hw, sem)
            assert result.success is False
            assert "No PrimeClient" in result.failure_reason
            assert "RESEARCH" in result.phases_completed

    @pytest.mark.asyncio
    async def test_run_with_mock_prime_and_valid_json(self):
        """Full pipeline with mock PrimeClient returning valid JSON."""
        config = ExplorationConfig(test_timeout_s=10.0)
        prime = AsyncMock()
        prime.generate = AsyncMock(return_value=_make_prime_response(json.dumps({
            "schema_version": "exploration-1.0",
            "files": {"parse_parquet.py": "def parse(): return True"},
            "tests": {"test_parse_parquet.py": (
                "def test_parse():\n"
                "    from parse_parquet import parse\n"
                "    assert parse() is True\n"
            )},
            "explanation": "Simple parquet parser stub",
        })))

        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ExplorationStrategy(
                config=config,
                scratch_path=tmpdir,
                prime_client=prime,
            )
            target = _make_target()
            hw = _make_hardware()
            sem = asyncio.Semaphore(4)
            result = await strategy.run(target, hw, sem)

            assert "RESEARCH" in result.phases_completed
            assert "SYNTHESIZE" in result.phases_completed
            assert "VALIDATE" in result.phases_completed
            assert "PACKAGE" in result.phases_completed
            assert result.synthesis is not None
            assert "parse_parquet.py" in result.synthesis.generated_files
            assert result.validation is not None

    @pytest.mark.asyncio
    async def test_run_with_empty_generation(self):
        """Prime returns JSON with no files -> failure."""
        config = ExplorationConfig()
        prime = AsyncMock()
        prime.generate = AsyncMock(return_value=_make_prime_response(json.dumps({
            "schema_version": "exploration-1.0",
            "files": {},
            "tests": {},
            "explanation": "Nothing to generate",
        })))

        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ExplorationStrategy(
                config=config,
                scratch_path=tmpdir,
                prime_client=prime,
            )
            result = await strategy.run(_make_target(), _make_hardware(), asyncio.Semaphore(4))
            assert result.success is False
            assert "no files" in result.failure_reason.lower()

    @pytest.mark.asyncio
    async def test_run_with_malformed_json_falls_back_to_markdown(self):
        """Prime returns markdown instead of JSON -> fallback parser."""
        config = ExplorationConfig(test_timeout_s=10.0)
        prime = AsyncMock()
        prime.generate = AsyncMock(return_value=_make_prime_response(
            "Here is the code:\n```python\ndef hello(): return 'world'\n```\n"
            "And tests:\n```python\ndef test_hello():\n    assert hello() == 'world'\n```"
        ))

        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ExplorationStrategy(
                config=config,
                scratch_path=tmpdir,
                prime_client=prime,
            )
            result = await strategy.run(_make_target(), _make_hardware(), asyncio.Semaphore(4))
            assert "SYNTHESIZE" in result.phases_completed
            assert result.synthesis is not None
            assert result.synthesis.schema_version == "raw-markdown"
            # Should have extracted at least one file from code blocks
            total_files = len(result.synthesis.generated_files) + len(result.synthesis.test_files)
            assert total_files >= 1

    @pytest.mark.asyncio
    async def test_research_phase_runs_parallel(self):
        """Research phase should gather web + codebase + deps in parallel."""
        config = ExplorationConfig()
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ExplorationStrategy(
                config=config,
                scratch_path=tmpdir,
                web_tool=None,        # no web -> graceful skip
                repo_registry=None,   # no registry -> graceful skip
            )
            target = _make_target()
            hw = _make_hardware()
            sem = asyncio.Semaphore(4)
            # Run research directly
            findings = await strategy._phase_research(target, sem)
            assert isinstance(findings, ResearchFindings)
            assert "WebTool not available" in findings.errors
            assert "RepoRegistry not available" in findings.errors

    @pytest.mark.asyncio
    async def test_shadow_validation_catches_syntax_error(self):
        """Shadow check should catch SyntaxError in generated code."""
        config = ExplorationConfig()
        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ExplorationStrategy(
                config=config,
                scratch_path=tmpdir,
            )
            synthesis = SynthesisResult(
                generated_files={"bad.py": "def broken(:\n    pass"},
                test_files={},
                explanation="broken",
                schema_version="test",
                provider_used="test",
                prompt_tokens=0,
                response_tokens=0,
            )
            validation = await strategy._phase_validate(synthesis)
            assert validation.shadow_passed is False
            assert any("SyntaxError" in e for e in validation.shadow_errors)

    @pytest.mark.asyncio
    async def test_comm_protocol_events_emitted(self):
        """CommProtocol should receive lifecycle events during exploration."""
        config = ExplorationConfig()
        comm = AsyncMock()
        comm.emit_intent = AsyncMock()
        comm.emit_heartbeat = AsyncMock()
        comm.emit_decision = AsyncMock()
        comm.emit_postmortem = AsyncMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            strategy = ExplorationStrategy(
                config=config,
                scratch_path=tmpdir,
                prime_client=None,  # will block at SYNTHESIZE
                comm_protocol=comm,
            )
            result = await strategy.run(_make_target(), _make_hardware(), asyncio.Semaphore(4))
            assert comm.emit_intent.called
            assert comm.emit_heartbeat.called
            assert comm.emit_decision.called

    def test_build_search_queries(self):
        config = ExplorationConfig()
        strategy = ExplorationStrategy(config=config, scratch_path="/tmp/test")
        cap = CapabilityNode(name="parse_parquet", domain="data_io", repo_owner="reactor")
        queries = strategy._build_search_queries(cap)
        assert len(queries) > 0
        assert any("parse_parquet" in q or "parse parquet" in q for q in queries)

    def test_parse_synthesis_response_valid_json(self):
        config = ExplorationConfig()
        strategy = ExplorationStrategy(config=config, scratch_path="/tmp/test")
        cap = CapabilityNode(name="test_cap", domain="test", repo_owner="jarvis")
        content = json.dumps({
            "schema_version": "exploration-1.0",
            "files": {"impl.py": "x = 1"},
            "tests": {"test_impl.py": "assert True"},
            "explanation": "simple",
        })
        files, tests, explanation, schema = strategy._parse_synthesis_response(content, cap)
        assert "impl.py" in files
        assert "test_impl.py" in tests
        assert schema == "exploration-1.0"

    def test_parse_synthesis_response_markdown_fallback(self):
        config = ExplorationConfig()
        strategy = ExplorationStrategy(config=config, scratch_path="/tmp/test")
        cap = CapabilityNode(name="my_cap", domain="test", repo_owner="jarvis")
        content = "```python\ndef foo(): pass\n```\n```python\ndef test_foo(): pass\n```"
        files, tests, explanation, schema = strategy._parse_synthesis_response(content, cap)
        assert schema == "raw-markdown"
        total = len(files) + len(tests)
        assert total >= 1

    def test_extract_code_blocks_classifies_correctly(self):
        config = ExplorationConfig()
        strategy = ExplorationStrategy(config=config, scratch_path="/tmp/test")
        cap = CapabilityNode(name="my_cap", domain="test", repo_owner="jarvis")
        content = (
            "```python\ndef impl(): return 42\n```\n"
            "```python\nimport pytest\ndef test_impl(): assert impl() == 42\n```"
        )
        files, tests = strategy._extract_code_blocks(content, cap)
        assert len(files) >= 1
        assert len(tests) >= 1
