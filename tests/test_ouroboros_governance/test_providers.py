"""Tests for provider adapters — schema validation and prompt building."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, Tuple

import pytest

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
