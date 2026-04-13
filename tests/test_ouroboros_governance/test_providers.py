"""Tests for provider adapters — schema validation and prompt building."""

from __future__ import annotations

import asyncio
import json
import time as _time
from datetime import datetime, timezone
from typing import Dict, Tuple
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from backend.core.ouroboros.governance.candidate_generator import CandidateProvider
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)


def _make_context(
    *,
    op_id: str = "op-test-001",
    description: str = "Add edge case tests for utils.py",
    target_files: Tuple[str, ...] = ("tests/test_utils.py",),
) -> OperationContext:
    return OperationContext.create(
        target_files=target_files,
        description=description,
        op_id=op_id,
        _timestamp=_FIXED_TS,
    )


# ---------------------------------------------------------------------------
# Test _parse_generation_response
# ---------------------------------------------------------------------------


class TestParseGenerationResponse:
    """Tests for the shared JSON schema parser."""

    def test_valid_single_candidate(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {
                    "candidate_id": "c1",
                    "file_path": "tests/test_utils.py",
                    "full_content": "def test_edge():\n    assert True\n",
                    "rationale": "Added edge case test",
                }
            ],
            "provider_metadata": {"model_id": "jarvis-prime-7b"},
        })
        result = _parse_generation_response(raw, "test-provider", 1.5, _make_context(), "abc123", "tests/test_utils.py")
        assert isinstance(result, GenerationResult)
        assert len(result.candidates) == 1
        assert result.candidates[0]["file_path"] == "tests/test_utils.py"
        assert result.provider_name == "test-provider"
        assert result.generation_duration_s == 1.5

    def test_valid_multiple_candidates(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {"candidate_id": "c1", "file_path": "tests/test_a.py", "full_content": "def test_a():\n    pass\n", "rationale": "a"},
                {"candidate_id": "c2", "file_path": "tests/test_b.py", "full_content": "def test_b():\n    pass\n", "rationale": "b"},
            ],
        })
        result = _parse_generation_response(raw, "test-provider", 2.0, _make_context(), "abc123", "tests/test_a.py")
        assert len(result.candidates) == 2

    def test_rejects_non_json(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response("not json at all", "test-provider", 0.0, _make_context(), "abc123", "test.py")

    def test_rejects_missing_candidates_key(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({"schema_version": "2b.1"})
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0, _make_context(), "abc123", "test.py")

    def test_rejects_empty_candidates(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({"schema_version": "2b.1", "candidates": []})
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0, _make_context(), "abc123", "test.py")

    def test_rejects_candidate_missing_file(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "schema_version": "2b.1",
            "candidates": [{"candidate_id": "c1", "full_content": "def f(): pass\n", "rationale": "x"}],  # missing file_path
        })
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0, _make_context(), "abc123", "test.py")

    def test_rejects_candidate_missing_content(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "schema_version": "2b.1",
            "candidates": [{"candidate_id": "c1", "file_path": "test.py", "rationale": "x"}],  # missing full_content
        })
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0, _make_context(), "abc123", "test.py")

    def test_rejects_invalid_python_syntax(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {"candidate_id": "c1", "file_path": "test.py", "full_content": "def broken(\n", "rationale": "test"}
            ],
        })
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0, _make_context(), "abc123", "test.py")

    def test_non_python_file_skips_ast_validation(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {"candidate_id": "c1", "file_path": "config.yaml", "full_content": "key: value\n", "rationale": "config"},
            ],
        })
        result = _parse_generation_response(raw, "test-provider", 0.5, _make_context(), "abc123", "config.yaml")
        assert len(result.candidates) == 1

    def test_extracts_json_from_markdown_fences(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = "Some preamble\n```json\n" + json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {"candidate_id": "c1", "file_path": "test.py", "full_content": "def f():\n    pass\n", "rationale": "test"},
            ],
        }) + "\n```\nSome postamble"
        result = _parse_generation_response(raw, "test-provider", 1.0, _make_context(), "abc123", "test.py")
        assert len(result.candidates) == 1

    def test_metadata_preserved(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {"candidate_id": "c1", "file_path": "test.py", "full_content": "def f():\n    pass\n", "rationale": "test"},
            ],
            "provider_metadata": {"model_id": "prime-7b", "reasoning_summary": "test reasoning"},
        })
        result = _parse_generation_response(raw, "test-provider", 1.0, _make_context(), "abc123", "test.py")
        # Metadata should be available via candidates or generation result
        assert result.provider_name == "test-provider"


class TestBuildCodegenPrompt:
    """Tests for the shared prompt builder."""

    def test_includes_target_files(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _build_codegen_prompt,
        )

        ctx = _make_context()
        prompt = _build_codegen_prompt(ctx)
        assert "tests/test_utils.py" in prompt

    def test_includes_description(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _build_codegen_prompt,
        )

        ctx = _make_context(description="Fix the broken parser")
        prompt = _build_codegen_prompt(ctx)
        assert "Fix the broken parser" in prompt

    def test_includes_json_schema_instruction(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _build_codegen_prompt,
        )

        ctx = _make_context()
        prompt = _build_codegen_prompt(ctx)
        assert "candidates" in prompt
        assert "JSON" in prompt

    def test_includes_file_content_constraint(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _build_codegen_prompt,
            _SCHEMA_VERSION_DIFF,
        )

        # Single-file context → diff schema (Task 4)
        ctx_single = _make_context(target_files=("tests/test_utils.py",))
        prompt_single = _build_codegen_prompt(ctx_single)
        assert "file" in prompt_single
        assert _SCHEMA_VERSION_DIFF in prompt_single
        assert "unified_diff" in prompt_single

        # Multi-file context → full_content schema
        ctx_multi = _make_context(target_files=("tests/test_a.py", "tests/test_b.py"))
        prompt_multi = _build_codegen_prompt(ctx_multi)
        assert "file" in prompt_multi
        assert "full_content" in prompt_multi


# ---------------------------------------------------------------------------
# Helpers for PrimeProvider tests
# ---------------------------------------------------------------------------


def _mock_prime_client(
    content: str = "",
    status: str = "available",
    latency_ms: float = 500.0,
    tokens_used: int = 200,
) -> MagicMock:
    """Build a mock PrimeClient."""
    client = MagicMock()
    response = MagicMock()
    response.content = content
    response.request_id = "req-001"
    response.model = "jarvis-prime-7b"
    response.source = "gcp_prime"
    response.latency_ms = latency_ms
    response.tokens_used = tokens_used
    response.metadata = {}
    client.generate = AsyncMock(return_value=response)

    # Health check mock
    health_result = MagicMock()
    health_result.name = status.upper()
    client._check_health = AsyncMock(return_value=health_result)
    client._last_health_data = {"status": status}

    return client


# ---------------------------------------------------------------------------
# Test PrimeProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPrimeProvider:
    """Tests for PrimeProvider adapter."""

    async def test_satisfies_candidate_provider_protocol(self) -> None:
        from backend.core.ouroboros.governance.providers import PrimeProvider
        client = _mock_prime_client()
        provider = PrimeProvider(client)
        assert isinstance(provider, CandidateProvider)

    async def test_provider_name(self) -> None:
        from backend.core.ouroboros.governance.providers import PrimeProvider
        provider = PrimeProvider(_mock_prime_client())
        assert provider.provider_name == "gcp-jprime"

    async def test_generate_success(self) -> None:
        from backend.core.ouroboros.governance.providers import PrimeProvider
        valid_response = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {
                    "candidate_id": "c1",
                    "file_path": "tests/test_foo.py",
                    "full_content": "def test_foo():\n    assert True\n",
                    "rationale": "Added test",
                }
            ],
            "provider_metadata": {"model_id": "prime-7b"},
        })
        client = _mock_prime_client(content=valid_response)
        provider = PrimeProvider(client)
        ctx = _make_context()
        deadline = datetime(2026, 3, 7, 12, 5, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert len(result.candidates) == 1
        assert result.provider_name == "gcp-jprime"
        client.generate.assert_called_once()

    async def test_generate_schema_failure_raises(self) -> None:
        from backend.core.ouroboros.governance.providers import PrimeProvider
        client = _mock_prime_client(content="not valid json")
        provider = PrimeProvider(client)
        ctx = _make_context()
        deadline = datetime(2026, 3, 7, 12, 5, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="schema_invalid"):
            await provider.generate(ctx, deadline)

    async def test_generate_uses_low_temperature(self) -> None:
        from backend.core.ouroboros.governance.providers import PrimeProvider
        valid_response = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {"candidate_id": "c1", "file_path": "test.py", "full_content": "def f():\n    pass\n", "rationale": "test"},
            ],
        })
        client = _mock_prime_client(content=valid_response)
        provider = PrimeProvider(client)
        ctx = _make_context()
        deadline = datetime(2026, 3, 7, 12, 5, 0, tzinfo=timezone.utc)
        await provider.generate(ctx, deadline)
        call_kwargs = client.generate.call_args
        assert call_kwargs.kwargs.get("temperature", call_kwargs[1].get("temperature")) == 0.2

    async def test_health_probe_available(self) -> None:
        from backend.core.ouroboros.governance.providers import PrimeProvider
        client = _mock_prime_client(status="available")
        provider = PrimeProvider(client)
        assert await provider.health_probe() is True

    async def test_health_probe_unavailable(self) -> None:
        from backend.core.ouroboros.governance.providers import PrimeProvider
        client = _mock_prime_client(status="unavailable")
        provider = PrimeProvider(client)
        assert await provider.health_probe() is False

    async def test_health_probe_exception_returns_false(self) -> None:
        from backend.core.ouroboros.governance.providers import PrimeProvider
        client = _mock_prime_client()
        client._check_health = AsyncMock(side_effect=ConnectionError("unreachable"))
        provider = PrimeProvider(client)
        assert await provider.health_probe() is False


# ---------------------------------------------------------------------------
# Test ClaudeProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestClaudeProvider:
    """Tests for ClaudeProvider adapter."""

    async def test_satisfies_candidate_provider_protocol(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider(api_key="test-key")
        assert isinstance(provider, CandidateProvider)

    async def test_provider_name(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider(api_key="test-key")
        assert provider.provider_name == "claude-api"

    async def test_generate_success(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        valid_response = json.dumps({
            "schema_version": "2b.1",
            "candidates": [
                {
                    "candidate_id": "c1",
                    "file_path": "tests/test_foo.py",
                    "full_content": "def test_foo():\n    assert True\n",
                    "rationale": "Added test",
                }
            ],
            "provider_metadata": {"model_id": "claude-sonnet"},
        })
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=valid_response)]
        mock_message.usage = MagicMock(input_tokens=100, output_tokens=200)
        mock_message.model = "claude-sonnet-4-20250514"

        provider = ClaudeProvider(api_key="test-key")
        mock_client = AsyncMock()
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)
        provider._client = mock_client

        ctx = _make_context()
        deadline = datetime(2026, 3, 7, 12, 5, 0, tzinfo=timezone.utc)
        result = await provider.generate(ctx, deadline)
        assert len(result.candidates) == 1
        assert result.provider_name == "claude-api"

    async def test_budget_exhausted_raises(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider(
            api_key="test-key",
            max_cost_per_op=0.50,
            daily_budget=0.001,
        )
        provider._daily_spend = 0.01
        ctx = _make_context()
        deadline = datetime(2026, 3, 7, 12, 5, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="claude_budget_exhausted"):
            await provider.generate(ctx, deadline)

    async def test_cost_tracking_accumulates(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider(api_key="test-key", daily_budget=100.0)
        assert provider._daily_spend == 0.0
        provider._record_cost(0.05)
        assert provider._daily_spend == 0.05
        provider._record_cost(0.03)
        assert provider._daily_spend == 0.08

    async def test_daily_budget_resets(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider(api_key="test-key", daily_budget=10.0)
        provider._daily_spend = 5.0
        provider._budget_reset_date = datetime(2026, 3, 6, tzinfo=timezone.utc).date()
        provider._maybe_reset_daily_budget()
        assert provider._daily_spend == 0.0

    async def test_health_probe_returns_true_with_valid_key(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider(api_key="test-key")
        mock_client = AsyncMock()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="ok")]
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)
        provider._client = mock_client
        assert await provider.health_probe() is True

    async def test_health_probe_returns_false_on_error(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider
        provider = ClaudeProvider(api_key="test-key")
        mock_client = AsyncMock()
        mock_client.messages = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("auth failed"))
        provider._client = mock_client
        assert await provider.health_probe() is False


class TestPrimeProviderPlan:
    async def test_plan_calls_client_and_returns_string(self):
        from unittest.mock import AsyncMock, MagicMock
        from datetime import datetime, timedelta, timezone
        from backend.core.ouroboros.governance.providers import PrimeProvider

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        mock_client.generate = AsyncMock(return_value=mock_response)

        provider = PrimeProvider(prime_client=mock_client)
        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
        result = await provider.plan("describe the task", deadline)

        assert isinstance(result, str)
        mock_client.generate.assert_called_once()
        call_kwargs = mock_client.generate.call_args.kwargs
        assert call_kwargs.get("max_tokens", 9999) <= 512
        assert call_kwargs.get("temperature", 1.0) == 0.0


class TestClaudeProviderPlan:
    async def test_plan_calls_api_and_returns_string(self):
        from unittest.mock import AsyncMock, MagicMock
        from datetime import datetime, timedelta, timezone
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        provider = ClaudeProvider(api_key="test-key")
        mock_message = MagicMock()
        mock_message.content = [MagicMock(
            text='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "ok"}'
        )]
        mock_message.usage = MagicMock(input_tokens=10, output_tokens=5)
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_message)
        provider._client = mock_client

        deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
        result = await provider.plan("describe the task", deadline)

        assert isinstance(result, str)
        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs.get("max_tokens", 9999) <= 512
        assert call_kwargs.get("temperature", 1.0) == 0.0


class TestBuildCodegenPromptExpandedContext:
    def test_expanded_files_appear_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase

        (tmp_path / "target.py").write_text("def foo(): pass\n")
        (tmp_path / "helpers.py").write_text("def bar(): pass\n")

        ctx = OperationContext.create(
            target_files=("target.py",), description="update foo"
        )
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        ctx = ctx.with_expanded_files(("helpers.py",))
        ctx = ctx.advance(OperationPhase.GENERATE)

        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)

        assert "helpers.py" in prompt
        assert "CONTEXT ONLY" in prompt
        assert "DO NOT MODIFY" in prompt

    def test_no_expanded_files_omits_section(self, tmp_path):
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        (tmp_path / "target.py").write_text("def foo(): pass\n")
        ctx = OperationContext.create(
            target_files=("target.py",), description="update foo"
        )

        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "CONTEXT ONLY" not in prompt

    def test_expanded_file_content_appears_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase

        (tmp_path / "target.py").write_text("def foo(): pass\n")
        (tmp_path / "helpers.py").write_text("UNIQUE_MARKER_XYZ = 42\n")

        ctx = OperationContext.create(
            target_files=("target.py",), description="update foo"
        )
        ctx = ctx.advance(OperationPhase.ROUTE)
        ctx = ctx.advance(OperationPhase.CONTEXT_EXPANSION)
        ctx = ctx.with_expanded_files(("helpers.py",))
        ctx = ctx.advance(OperationPhase.GENERATE)

        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "UNIQUE_MARKER_XYZ" in prompt


# ---------------------------------------------------------------------------
# Task 4: System Context block injection
# ---------------------------------------------------------------------------

def _make_telemetry_context_for_prompt() -> "TelemetryContext":
    from backend.core.ouroboros.governance.op_context import (
        HostTelemetry,
        RoutingIntentTelemetry,
        TelemetryContext,
    )
    ht = HostTelemetry(
        schema_version="1.0",
        arch="arm64",
        cpu_percent=14.20,
        ram_available_gb=6.80,
        pressure="NORMAL",
        sampled_at_utc=datetime.now(tz=timezone.utc).isoformat(),
        sampled_monotonic_ns=_time.monotonic_ns(),
        collector_status="ok",
        sample_age_ms=3,
    )
    ri = RoutingIntentTelemetry(expected_provider="GCP_PRIME_SPOT", policy_reason="NORMAL")
    return TelemetryContext(local_node=ht, routing_intent=ri)


class TestSystemContextBlock:
    """Tests for ## System Context block injection in _build_codegen_prompt."""

    def test_absent_when_telemetry_none(self, tmp_path):
        """Default ctx (telemetry=None) → no ## System Context in prompt."""
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=(),
            description="test op",
        )
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "## System Context" not in prompt

    def test_present_when_telemetry_set(self, tmp_path):
        """ctx.telemetry set → ## System Context block appears in prompt."""
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=(),
            description="test op",
        )
        ctx = ctx.with_telemetry(_make_telemetry_context_for_prompt())
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "## System Context" in prompt

    def test_block_contains_host_fields(self, tmp_path):
        """Block contains arch, CPU%, RAM, pressure from HostTelemetry."""
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=(),
            description="test op",
        )
        ctx = ctx.with_telemetry(_make_telemetry_context_for_prompt())
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "arm64" in prompt
        assert "14.20" in prompt
        assert "6.80" in prompt
        assert "NORMAL" in prompt

    def test_block_contains_route_intent(self, tmp_path):
        """Block contains expected_provider and policy_reason."""
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=(),
            description="test op",
        )
        ctx = ctx.with_telemetry(_make_telemetry_context_for_prompt())
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "GCP_PRIME_SPOT" in prompt

    def test_block_includes_routing_actual_when_set(self, tmp_path):
        """If routing_actual is set, the Actual: line appears."""
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import (
            OperationContext,
            RoutingActualTelemetry,
        )

        ctx = OperationContext.create(
            target_files=(),
            description="test op",
        )
        ctx = ctx.with_telemetry(_make_telemetry_context_for_prompt())
        ra = RoutingActualTelemetry(
            provider_name="GCP_PRIME_SPOT",
            endpoint_class="gcp_spot",
            fallback_chain=(),
            was_degraded=False,
        )
        ctx = ctx.with_routing_actual(ra)
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "Actual:" in prompt
        assert "gcp_spot" in prompt
        assert "Degraded: False" in prompt

    def test_block_position_after_task_before_snapshot(self, tmp_path):
        """## System Context appears after ## Task and before ## Source Snapshot."""
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=(),
            description="test op",
        )
        ctx = ctx.with_telemetry(_make_telemetry_context_for_prompt())
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        task_pos = prompt.index("## Task")
        sys_ctx_pos = prompt.index("## System Context")
        snapshot_pos = prompt.index("## Source Snapshot")
        assert task_pos < sys_ctx_pos < snapshot_pos


class TestStrategicMemoryPromptBlock:
    def test_absent_when_not_stamped(self, tmp_path):
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=(),
            description="test op",
        )
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "## Strategic Memory" not in prompt

    def test_present_when_stamped(self, tmp_path):
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=(),
            description="test op",
        )
        ctx = ctx.with_strategic_memory_context(
            strategic_intent_id="intent-001",
            strategic_memory_fact_ids=("fact-001",),
            strategic_memory_prompt=(
                "## Strategic Memory (advisory context only)\n"
                "- [confidence=0.90 | provenance=user:op-1] keep architecture consistent"
            ),
            strategic_memory_digest="digest-001",
        )
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        assert "## Strategic Memory" in prompt
        assert "keep architecture consistent" in prompt

    def test_position_after_system_context_before_snapshot(self, tmp_path):
        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        from backend.core.ouroboros.governance.op_context import OperationContext

        ctx = OperationContext.create(
            target_files=(),
            description="test op",
        )
        ctx = ctx.with_telemetry(_make_telemetry_context_for_prompt())
        ctx = ctx.with_strategic_memory_context(
            strategic_intent_id="intent-001",
            strategic_memory_fact_ids=("fact-001",),
            strategic_memory_prompt="## Strategic Memory (advisory context only)\n- preserve architecture",
            strategic_memory_digest="digest-001",
        )
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)
        sys_ctx_pos = prompt.index("## System Context")
        strategic_pos = prompt.index("## Strategic Memory")
        snapshot_pos = prompt.index("## Source Snapshot")
        assert sys_ctx_pos < strategic_pos < snapshot_pos


# ---------------------------------------------------------------------------
# Phase 1 Step 3B — Provider state hoist acceptance tests
# ---------------------------------------------------------------------------


class TestProviderStateUnquarantine:
    """Verify ``JARVIS_UNQUARANTINE_PROVIDERS`` routes provider state
    through the process-lifetime singletons in ``_governance_state``.

    The three providers (Claude, Prime, DoubleWord) each maintain
    reload-hostile state — HTTP clients with open connection pools,
    daily spend counters, cascade telemetry ring buffers, aiohttp
    sessions — that ``importlib.reload()`` on the provider module
    would otherwise drop on the floor. This class mirrors
    ``TestGeneratorStateUnquarantine`` from ``test_candidate_generator.py``
    and locks in the two-path contract: fresh per-instance state when
    the flag is off, singleton-shared state when it is on.
    """

    def _reset_singletons(self) -> None:
        """Clear every provider state singleton.

        Global singletons leak across tests because they live in a
        module-level dict — every test in this class must start clean
        or a sibling test's Claude client would still be wired up.
        """
        from backend.core.ouroboros.governance import _governance_state
        _governance_state.reset_for_tests()

    # ------------------------------------------------------------------
    # ClaudeProvider
    # ------------------------------------------------------------------

    def test_claude_flag_off_mints_fresh_state(self, monkeypatch):
        """Default path: two ClaudeProviders get independent state.

        Incrementing the client generation on one instance must not
        bleed into a sibling — proves the legacy per-instance
        behavior is preserved when the un-quarantine flag is off.
        """
        monkeypatch.delenv("JARVIS_UNQUARANTINE_PROVIDERS", raising=False)
        self._reset_singletons()
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        a = ClaudeProvider(api_key="test-a")
        b = ClaudeProvider(api_key="test-b")

        assert a._state is not b._state
        assert a._state.counters is not b._state.counters
        a._client_generation += 5
        a._daily_spend += 1.25
        assert b._client_generation == 0
        assert b._daily_spend == 0.0

    def test_claude_flag_on_shares_singleton(self, monkeypatch):
        """Un-quarantine path: Claude instances share one state blob.

        Mutating cascade counters or injecting a fake client on the
        first instance must be visible on every subsequent instance —
        this is the whole point of the hoist.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_PROVIDERS", "true")
        self._reset_singletons()
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        a = ClaudeProvider(api_key="test-a")
        a._client = "fake-anthropic-client"
        a._client_generation = 3
        a._daily_spend = 0.85
        a._recycle_events.append({"reason": "seed", "gen": 3})

        b = ClaudeProvider(api_key="test-b")
        assert b._state is a._state
        assert b._client == "fake-anthropic-client"
        assert b._client_generation == 3
        assert b._daily_spend == 0.85
        assert b._recycle_events[-1] == {"reason": "seed", "gen": 3}

    def test_claude_setter_prevents_shadow_on_client_rebind(self, monkeypatch):
        """Derek's critical invariant: ``self._client = None`` must
        route through the setter into ``_state.client``, NOT plant a
        real instance attribute on ``self`` that shadows the descriptor.

        If the setter is missing, this test fails because ``b._client``
        would still read the singleton while ``a._client`` would be the
        stale ``None`` alias — exactly the split-brain the property
        pair exists to prevent.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_PROVIDERS", "true")
        self._reset_singletons()
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        a = ClaudeProvider(api_key="test-a")
        a._client = "client-v1"
        b = ClaudeProvider(api_key="test-b")
        assert b._client == "client-v1"

        # Here's the shadowing hazard: a plain getter-only @property
        # would let this write land on a.__dict__ and leave
        # a._state.client untouched. With the setter, it routes.
        a._client = None
        assert a._state.client is None
        assert b._client is None  # b sees the same state
        # And a._client reads back from state, not from any shadow.
        assert a._client is None
        assert "_client" not in a.__dict__

    def test_claude_recycle_client_preserves_state_identity(self, monkeypatch):
        """``_recycle_client`` reassigns ``self._client = None`` and
        truncates ring buffers via slice rebind. Every mutation must
        land on the shared state, not drift into instance shadows.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_PROVIDERS", "true")
        self._reset_singletons()
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        p = ClaudeProvider(api_key="test")
        p._client = "client-before"
        new_gen = p._recycle_client("unit_test_trigger")
        assert new_gen == 1
        assert p._state.client is None
        assert p._state.counters.client_generation == 1
        assert p._state.recycle_events[-1]["reason"] == "unit_test_trigger"

    # ------------------------------------------------------------------
    # PrimeProvider
    # ------------------------------------------------------------------

    def test_prime_flag_on_first_wins_semantics(self, monkeypatch):
        """PrimeProvider gets first-wins semantics on the singleton
        path: the second instance's ``prime_client`` param is ignored
        because the singleton already holds a live client handle from
        the pre-reload incarnation. This mirrors ``get_generator_state``
        first-call-wins.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_PROVIDERS", "true")
        self._reset_singletons()
        from backend.core.ouroboros.governance.providers import PrimeProvider

        first = PrimeProvider(prime_client="client-A")
        second = PrimeProvider(prime_client="client-B")
        assert first._state is second._state
        assert first._client == "client-A"
        assert second._client == "client-A"  # B was ignored — first wins

    def test_prime_flag_off_each_instance_gets_its_own_client(self, monkeypatch):
        """Legacy path: every PrimeProvider binds to the constructor
        argument, no singleton sharing."""
        monkeypatch.delenv("JARVIS_UNQUARANTINE_PROVIDERS", raising=False)
        self._reset_singletons()
        from backend.core.ouroboros.governance.providers import PrimeProvider

        first = PrimeProvider(prime_client="client-A")
        second = PrimeProvider(prime_client="client-B")
        assert first._state is not second._state
        assert first._client == "client-A"
        assert second._client == "client-B"

    # ------------------------------------------------------------------
    # DoubleWordProvider
    # ------------------------------------------------------------------

    def test_doubleword_flag_on_shares_session_and_stats(self, monkeypatch):
        """aiohttp session, cumulative stats, and counters flow through
        the shared singleton when the flag is on.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_PROVIDERS", "true")
        self._reset_singletons()
        from backend.core.ouroboros.governance.doubleword_provider import (
            DoublewordProvider,
        )

        a = DoublewordProvider(api_key="test-a")
        a._session = "fake-aiohttp-session"
        a._daily_spend = 2.50
        a._last_error_status = 429
        a._stats.total_batches += 7

        b = DoublewordProvider(api_key="test-b")
        assert b._state is a._state
        assert b._session == "fake-aiohttp-session"
        assert b._daily_spend == 2.50
        assert b._last_error_status == 429
        assert b._stats.total_batches == 7

    def test_doubleword_flag_off_mints_fresh_state(self, monkeypatch):
        """Legacy path: DoubleWord instances get independent state."""
        monkeypatch.delenv("JARVIS_UNQUARANTINE_PROVIDERS", raising=False)
        self._reset_singletons()
        from backend.core.ouroboros.governance.doubleword_provider import (
            DoublewordProvider,
        )

        a = DoublewordProvider(api_key="test-a")
        b = DoublewordProvider(api_key="test-b")
        assert a._state is not b._state
        a._daily_spend += 3.14
        a._stats.total_batches += 1
        assert b._daily_spend == 0.0
        assert b._stats.total_batches == 0

    def test_doubleword_session_rebind_goes_through_state(self, monkeypatch):
        """Critical: ``self._session = aiohttp.ClientSession(...)``
        in ``_get_session`` must land in ``_state.session``, not on a
        shadow instance attribute. Replays the split-brain check from
        the Claude tests but against DoubleWord's setter.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_PROVIDERS", "true")
        self._reset_singletons()
        from backend.core.ouroboros.governance.doubleword_provider import (
            DoublewordProvider,
        )

        a = DoublewordProvider(api_key="test-a")
        a._session = "session-v1"
        b = DoublewordProvider(api_key="test-b")
        assert b._session == "session-v1"

        a._session = None
        assert a._state.session is None
        assert b._session is None
        assert "_session" not in a.__dict__

    # ------------------------------------------------------------------
    # importlib.reload acceptance test
    # ------------------------------------------------------------------

    def test_reload_providers_preserves_claude_client_and_counters(
        self, monkeypatch
    ):
        """The acceptance test for Phase 1 Step 3B (Claude arm).

        Build a ClaudeProvider, inject a fake client and mutate
        counters, then ``importlib.reload(providers)``. Rebuilding
        ClaudeProvider after the reload must observe the same client
        identity and the same counter values — proving the reload did
        not discard live state.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_PROVIDERS", "true")
        self._reset_singletons()

        import importlib
        from backend.core.ouroboros.governance import providers

        before = providers.ClaudeProvider(api_key="pre-reload")
        before._client = "live-anthropic-client"
        before._client_generation = 9
        before._daily_spend = 4.42
        before._recycle_events.append({"reason": "warmup", "gen": 9})

        reloaded = importlib.reload(providers)

        after = reloaded.ClaudeProvider(api_key="post-reload")
        assert after._client == "live-anthropic-client"
        assert after._client_generation == 9
        assert after._daily_spend == 4.42
        assert after._recycle_events[-1] == {"reason": "warmup", "gen": 9}
        assert after._state is before._state

    def test_reload_doubleword_preserves_session_and_stats(self, monkeypatch):
        """The acceptance test for Phase 1 Step 3B (DoubleWord arm).

        Same shape as the Claude reload test — verifies the DoubleWord
        state path survives ``importlib.reload(doubleword_provider)``.
        """
        monkeypatch.setenv("JARVIS_UNQUARANTINE_PROVIDERS", "true")
        self._reset_singletons()

        import importlib
        from backend.core.ouroboros.governance import doubleword_provider

        before = doubleword_provider.DoublewordProvider(api_key="pre-reload")
        before._session = "live-aiohttp-session"
        before._daily_spend = 1.23
        before._last_error_status = 503
        before._stats.total_batches += 4

        reloaded = importlib.reload(doubleword_provider)

        after = reloaded.DoublewordProvider(api_key="post-reload")
        assert after._session == "live-aiohttp-session"
        assert after._daily_spend == 1.23
        assert after._last_error_status == 503
        assert after._stats.total_batches == 4
        assert after._state is before._state
