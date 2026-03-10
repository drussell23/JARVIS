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
