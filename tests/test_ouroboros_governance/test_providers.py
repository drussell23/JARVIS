"""Tests for provider adapters — schema validation and prompt building."""

from __future__ import annotations

import asyncio
import json
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
            "candidates": [
                {"file": "tests/test_utils.py", "content": "def test_edge():\n    assert True\n"}
            ],
            "model_id": "jarvis-prime-7b",
            "reasoning_summary": "Added edge case test",
        })
        result = _parse_generation_response(raw, "test-provider", 1.5)
        assert isinstance(result, GenerationResult)
        assert len(result.candidates) == 1
        assert result.candidates[0]["file"] == "tests/test_utils.py"
        assert result.provider_name == "test-provider"
        assert result.generation_duration_s == 1.5

    def test_valid_multiple_candidates(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "candidates": [
                {"file": "tests/test_a.py", "content": "def test_a():\n    pass\n"},
                {"file": "tests/test_b.py", "content": "def test_b():\n    pass\n"},
            ],
            "model_id": "prime",
        })
        result = _parse_generation_response(raw, "test-provider", 2.0)
        assert len(result.candidates) == 2

    def test_rejects_non_json(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response("not json at all", "test-provider", 0.0)

    def test_rejects_missing_candidates_key(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({"model_id": "prime"})
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0)

    def test_rejects_empty_candidates(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({"candidates": []})
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0)

    def test_rejects_candidate_missing_file(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "candidates": [{"content": "def f(): pass\n"}]
        })
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0)

    def test_rejects_candidate_missing_content(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "candidates": [{"file": "test.py"}]
        })
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0)

    def test_rejects_invalid_python_syntax(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "candidates": [
                {"file": "test.py", "content": "def broken(\n"}
            ]
        })
        with pytest.raises(RuntimeError, match="schema_invalid"):
            _parse_generation_response(raw, "test-provider", 0.0)

    def test_non_python_file_skips_ast_validation(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "candidates": [
                {"file": "config.yaml", "content": "key: value\n"}
            ]
        })
        result = _parse_generation_response(raw, "test-provider", 0.5)
        assert len(result.candidates) == 1

    def test_extracts_json_from_markdown_fences(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = "Some preamble\n```json\n" + json.dumps({
            "candidates": [
                {"file": "test.py", "content": "def f():\n    pass\n"}
            ],
        }) + "\n```\nSome postamble"
        result = _parse_generation_response(raw, "test-provider", 1.0)
        assert len(result.candidates) == 1

    def test_metadata_preserved(self) -> None:
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )

        raw = json.dumps({
            "candidates": [
                {"file": "test.py", "content": "def f():\n    pass\n"}
            ],
            "model_id": "prime-7b",
            "reasoning_summary": "test reasoning",
        })
        result = _parse_generation_response(raw, "test-provider", 1.0)
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
        )

        ctx = _make_context()
        prompt = _build_codegen_prompt(ctx)
        assert "file" in prompt
        assert "content" in prompt


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
            "candidates": [
                {"file": "tests/test_foo.py", "content": "def test_foo():\n    assert True\n"}
            ],
            "model_id": "prime-7b",
            "reasoning_summary": "Added test",
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
            "candidates": [
                {"file": "test.py", "content": "def f():\n    pass\n"}
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
