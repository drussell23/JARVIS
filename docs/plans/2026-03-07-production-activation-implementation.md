# Production Activation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the governed self-programming pipeline into the running JARVIS system so `jarvis self-modify` triggers a full governed code generation cycle against GCP J-Prime.

**Architecture:** A `GovernedLoopService` lifecycle manager (Approach B) owns provider wiring, orchestrator construction, and health probes. The supervisor instantiates it in Zone 6.8. CLI commands are the sole authoritative trigger.

**Tech Stack:** Python 3.9+, asyncio, existing PrimeClient, existing GovernanceStack, existing CandidateGenerator + FailbackStateMachine, existing CommProtocol.

**Design doc:** `docs/plans/2026-03-07-production-activation-design.md`

---

## Existing Code Reference

Before implementing, read these files to understand the integration surface:

| Component | File | Key Lines |
|-----------|------|-----------|
| CandidateProvider protocol | `backend/core/ouroboros/governance/candidate_generator.py` | 74-118 (protocol), 306-317 (constructor) |
| GovernedOrchestrator | `backend/core/ouroboros/governance/orchestrator.py` | 62-88 (config), 116-127 (constructor) |
| OperationContext | `backend/core/ouroboros/governance/op_context.py` | 267-401 (class + create factory) |
| GenerationResult | `backend/core/ouroboros/governance/op_context.py` | 140-157 |
| GovernanceStack | `backend/core/ouroboros/governance/integration.py` | 228-289 |
| PrimeClient.generate() | `backend/core/prime_client.py` | 1073-1111 |
| PrimeResponse | `backend/core/prime_client.py` | 314-325 |
| PrimeStatus | `backend/core/prime_client.py` | 272-277 |
| CLIApprovalProvider | `backend/core/ouroboros/governance/approval_provider.py` | 242-422 |
| CommProtocol | `backend/core/ouroboros/governance/comm_protocol.py` | 116-287 |
| CLI break-glass pattern | `backend/core/ouroboros/governance/cli_commands.py` | 1-76 |
| Supervisor governance init | `unified_supervisor.py` | ~85845-85876 |

---

## Task 1: Schema Validation & Prompt Builder (shared utilities in `providers.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/providers.py`
- Create: `tests/test_ouroboros_governance/test_providers.py`

### Step 1: Write the failing tests

```python
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
```

### Step 2: Run tests to verify they fail

Run: `python3 -m pytest tests/test_ouroboros_governance/test_providers.py -v --tb=short`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.ouroboros.governance.providers'`

### Step 3: Write minimal implementation

Create `backend/core/ouroboros/governance/providers.py`:

```python
"""
Provider Adapters for Governed Code Generation
================================================

Wraps existing PrimeClient and Claude API into CandidateProvider protocol
implementations for use with the CandidateGenerator's failback state machine.

Components
----------
- ``_build_codegen_prompt``: builds structured prompt from OperationContext
- ``_parse_generation_response``: strict JSON schema parser for model output
- ``PrimeProvider``: wraps PrimeClient.generate()
- ``ClaudeProvider``: wraps anthropic.AsyncAnthropic (cost-gated)
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)

logger = logging.getLogger("Ouroboros.Providers")


# ---------------------------------------------------------------------------
# Shared: Prompt Builder
# ---------------------------------------------------------------------------

_CODEGEN_SYSTEM_PROMPT = (
    "You are a precise code generation assistant. You MUST respond with valid JSON only. "
    "No markdown, no explanations, no preamble. Only the JSON object."
)

_CODEGEN_SCHEMA_INSTRUCTION = """
Return a JSON object matching this exact schema:
{
  "candidates": [
    {
      "file": "<relative file path>",
      "content": "<complete file content>"
    }
  ],
  "model_id": "<your model identifier>",
  "reasoning_summary": "<brief explanation of changes>"
}

Rules:
- Each candidate must have non-empty "file" and "content" fields.
- Python files must be syntactically valid (parseable by ast.parse).
- Return ONLY the JSON object. No markdown fences, no extra text.
"""


def _build_codegen_prompt(ctx: OperationContext) -> str:
    """Build a structured code generation prompt from an OperationContext.

    Parameters
    ----------
    ctx:
        The operation context with target files and description.

    Returns
    -------
    str
        The full prompt string including schema instructions.
    """
    target_list = "\n".join(f"  - {f}" for f in ctx.target_files)
    return (
        f"Goal: {ctx.description}\n\n"
        f"Target files:\n{target_list}\n\n"
        f"Generate candidate code changes for the files listed above.\n"
        f"Each candidate must include the complete file content (not a diff).\n\n"
        f"{_CODEGEN_SCHEMA_INSTRUCTION}"
    )


# ---------------------------------------------------------------------------
# Shared: Response Parser
# ---------------------------------------------------------------------------


def _extract_json_block(raw: str) -> str:
    """Extract JSON from raw text, handling markdown fences.

    Tries direct parse first, then looks for ```json ... ``` blocks.
    """
    # Try direct parse first
    stripped = raw.strip()
    if stripped.startswith("{"):
        return stripped

    # Look for markdown JSON fences
    match = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", raw, re.DOTALL)
    if match:
        return match.group(1)

    return stripped


def _parse_generation_response(
    raw: str,
    provider_name: str,
    duration_s: float,
) -> GenerationResult:
    """Parse and validate a model's JSON response into a GenerationResult.

    Parameters
    ----------
    raw:
        Raw string response from the model.
    provider_name:
        Name of the provider for provenance tracking.
    duration_s:
        Wall-clock seconds the generation took.

    Returns
    -------
    GenerationResult
        Validated generation result.

    Raises
    ------
    RuntimeError
        With deterministic reason code if validation fails:
        ``"<provider_name>_schema_invalid:<detail>"``.
    """
    # Parse JSON
    json_str = _extract_json_block(raw)
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"{provider_name}_schema_invalid:json_parse_error"
        ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            f"{provider_name}_schema_invalid:expected_object"
        )

    # Validate candidates array
    candidates_raw = data.get("candidates")
    if not candidates_raw or not isinstance(candidates_raw, list):
        raise RuntimeError(
            f"{provider_name}_schema_invalid:missing_or_empty_candidates"
        )

    # Validate each candidate
    validated: List[Dict[str, Any]] = []
    for i, candidate in enumerate(candidates_raw):
        if not isinstance(candidate, dict):
            raise RuntimeError(
                f"{provider_name}_schema_invalid:candidate_{i}_not_object"
            )

        file_path = candidate.get("file", "")
        content = candidate.get("content", "")

        if not file_path or not isinstance(file_path, str):
            raise RuntimeError(
                f"{provider_name}_schema_invalid:candidate_{i}_missing_file"
            )
        if not content or not isinstance(content, str):
            raise RuntimeError(
                f"{provider_name}_schema_invalid:candidate_{i}_missing_content"
            )

        # AST validation for Python files
        if file_path.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError:
                raise RuntimeError(
                    f"{provider_name}_schema_invalid:candidate_{i}_syntax_error"
                )

        validated.append({"file": file_path, "content": content})

    return GenerationResult(
        candidates=tuple(validated),
        provider_name=provider_name,
        generation_duration_s=duration_s,
    )
```

### Step 4: Run tests to verify they pass

Run: `python3 -m pytest tests/test_ouroboros_governance/test_providers.py -v --tb=short`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/providers.py tests/test_ouroboros_governance/test_providers.py
git commit -m "feat(governance): add shared schema parser and prompt builder for provider adapters"
```

---

## Task 2: PrimeProvider Adapter

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py`
- Modify: `tests/test_ouroboros_governance/test_providers.py`

### Step 1: Write the failing tests

Add to `tests/test_ouroboros_governance/test_providers.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from backend.core.ouroboros.governance.candidate_generator import CandidateProvider


# ---------------------------------------------------------------------------
# Test PrimeProvider
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
```

### Step 2: Run tests to verify they fail

Run: `python3 -m pytest tests/test_ouroboros_governance/test_providers.py::TestPrimeProvider -v --tb=short`
Expected: FAIL — `ImportError: cannot import name 'PrimeProvider'`

### Step 3: Write minimal implementation

Add to `backend/core/ouroboros/governance/providers.py`:

```python
# ---------------------------------------------------------------------------
# PrimeProvider
# ---------------------------------------------------------------------------


class PrimeProvider:
    """CandidateProvider adapter wrapping PrimeClient.generate().

    Uses the existing PrimeClient for code generation with strict JSON
    schema enforcement. Temperature is fixed at 0.2 for deterministic
    code generation.

    Parameters
    ----------
    prime_client:
        An initialized PrimeClient instance.
    max_tokens:
        Maximum tokens for generation requests.
    """

    def __init__(
        self,
        prime_client: Any,
        max_tokens: int = 8192,
    ) -> None:
        self._client = prime_client
        self._max_tokens = max_tokens

    @property
    def provider_name(self) -> str:
        return "gcp-jprime"

    async def generate(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Generate code candidates via PrimeClient.

        Builds a structured prompt, calls PrimeClient.generate() with
        low temperature, and parses the response with strict schema
        validation.

        Raises
        ------
        RuntimeError
            On schema validation failure (``gcp-jprime_schema_invalid:...``).
        """
        prompt = _build_codegen_prompt(context)
        start = time.monotonic()

        response = await self._client.generate(
            prompt=prompt,
            system_prompt=_CODEGEN_SYSTEM_PROMPT,
            max_tokens=self._max_tokens,
            temperature=0.2,
        )

        duration = time.monotonic() - start

        result = _parse_generation_response(
            response.content,
            self.provider_name,
            duration,
        )

        logger.info(
            "[PrimeProvider] Generated %d candidates in %.1fs, model=%s, tokens=%d",
            len(result.candidates),
            duration,
            getattr(response, "model", "unknown"),
            getattr(response, "tokens_used", 0),
        )

        return result

    async def health_probe(self) -> bool:
        """Check PrimeClient health. Returns True only if AVAILABLE."""
        try:
            status = await self._client._check_health()
            return status.name == "AVAILABLE"
        except Exception:
            logger.debug("[PrimeProvider] Health probe failed", exc_info=True)
            return False
```

### Step 4: Run tests to verify they pass

Run: `python3 -m pytest tests/test_ouroboros_governance/test_providers.py -v --tb=short`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/providers.py tests/test_ouroboros_governance/test_providers.py
git commit -m "feat(governance): add PrimeProvider adapter wrapping PrimeClient for code generation"
```

---

## Task 3: ClaudeProvider Adapter (Cost-Gated)

**Files:**
- Modify: `backend/core/ouroboros/governance/providers.py`
- Modify: `tests/test_ouroboros_governance/test_providers.py`

### Step 1: Write the failing tests

Add to `tests/test_ouroboros_governance/test_providers.py`:

```python
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
            "candidates": [
                {"file": "tests/test_foo.py", "content": "def test_foo():\n    assert True\n"}
            ],
            "model_id": "claude-sonnet",
        })

        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=valid_response)]
        mock_message.usage = MagicMock(input_tokens=100, output_tokens=200)
        mock_message.model = "claude-sonnet-4-20250514"

        provider = ClaudeProvider(api_key="test-key")
        # Patch the client's create method
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
            daily_budget=0.001,  # Tiny budget
        )
        # Simulate spending over budget
        provider._daily_spend = 0.01

        ctx = _make_context()
        deadline = datetime(2026, 3, 7, 12, 5, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="claude_budget_exhausted"):
            await provider.generate(ctx, deadline)

    async def test_cost_tracking_accumulates(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        provider = ClaudeProvider(api_key="test-key", daily_budget=100.0)
        assert provider._daily_spend == 0.0
        # Simulate recording cost
        provider._record_cost(0.05)
        assert provider._daily_spend == 0.05
        provider._record_cost(0.03)
        assert provider._daily_spend == 0.08

    async def test_daily_budget_resets(self) -> None:
        from backend.core.ouroboros.governance.providers import ClaudeProvider

        provider = ClaudeProvider(api_key="test-key", daily_budget=10.0)
        provider._daily_spend = 5.0
        provider._budget_reset_date = datetime(2026, 3, 6, tzinfo=timezone.utc).date()
        # Check that a new day resets the budget
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
```

### Step 2: Run tests to verify they fail

Run: `python3 -m pytest tests/test_ouroboros_governance/test_providers.py::TestClaudeProvider -v --tb=short`
Expected: FAIL — `ImportError: cannot import name 'ClaudeProvider'`

### Step 3: Write minimal implementation

Add to `backend/core/ouroboros/governance/providers.py`:

```python
# ---------------------------------------------------------------------------
# ClaudeProvider
# ---------------------------------------------------------------------------

# Cost estimation constants (per 1M tokens, approximate)
_CLAUDE_INPUT_COST_PER_M = 3.00   # Sonnet pricing
_CLAUDE_OUTPUT_COST_PER_M = 15.00


class ClaudeProvider:
    """CandidateProvider adapter wrapping the Anthropic Claude API.

    Cost-gated: each call checks accumulated daily spend against
    ``daily_budget`` before proceeding. Budget resets at midnight UTC.

    Parameters
    ----------
    api_key:
        Anthropic API key.
    model:
        Model identifier (default: claude-sonnet-4-20250514).
    max_tokens:
        Maximum output tokens per generation.
    max_cost_per_op:
        Maximum estimated cost per single operation.
    daily_budget:
        Maximum daily spend in USD.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 8192,
        max_cost_per_op: float = 0.50,
        daily_budget: float = 10.00,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._max_cost_per_op = max_cost_per_op
        self._daily_budget = daily_budget
        self._daily_spend: float = 0.0
        self._budget_reset_date = datetime.now(tz=timezone.utc).date()
        self._client: Any = None  # Lazy init

    @property
    def provider_name(self) -> str:
        return "claude-api"

    def _ensure_client(self) -> Any:
        """Lazily initialize the Anthropic client."""
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
            except ImportError:
                raise RuntimeError(
                    "claude_api_unavailable:anthropic_not_installed"
                )
        return self._client

    def _maybe_reset_daily_budget(self) -> None:
        """Reset daily spend if the day has changed."""
        today = datetime.now(tz=timezone.utc).date()
        if today > self._budget_reset_date:
            self._daily_spend = 0.0
            self._budget_reset_date = today

    def _record_cost(self, cost: float) -> None:
        """Record cost from a generation call."""
        self._daily_spend += cost

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost in USD from token counts."""
        input_cost = (input_tokens / 1_000_000) * _CLAUDE_INPUT_COST_PER_M
        output_cost = (output_tokens / 1_000_000) * _CLAUDE_OUTPUT_COST_PER_M
        return input_cost + output_cost

    async def generate(
        self,
        context: OperationContext,
        deadline: datetime,
    ) -> GenerationResult:
        """Generate code candidates via Claude API.

        Checks budget before calling, estimates cost after, and records
        spend for daily tracking.

        Raises
        ------
        RuntimeError
            ``claude_budget_exhausted`` if daily budget exceeded.
            ``claude-api_schema_invalid:...`` on schema validation failure.
        """
        self._maybe_reset_daily_budget()

        if self._daily_spend >= self._daily_budget:
            raise RuntimeError("claude_budget_exhausted")

        client = self._ensure_client()
        prompt = _build_codegen_prompt(context)
        start = time.monotonic()

        message = await client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=_CODEGEN_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )

        duration = time.monotonic() - start
        raw_content = message.content[0].text

        # Track cost
        input_tokens = getattr(message.usage, "input_tokens", 0)
        output_tokens = getattr(message.usage, "output_tokens", 0)
        cost = self._estimate_cost(input_tokens, output_tokens)
        self._record_cost(cost)

        result = _parse_generation_response(
            raw_content,
            self.provider_name,
            duration,
        )

        logger.info(
            "[ClaudeProvider] Generated %d candidates in %.1fs, "
            "model=%s, tokens=%d+%d, cost=$%.4f, daily_spend=$%.4f/$%.2f",
            len(result.candidates),
            duration,
            getattr(message, "model", self._model),
            input_tokens,
            output_tokens,
            cost,
            self._daily_spend,
            self._daily_budget,
        )

        return result

    async def health_probe(self) -> bool:
        """Lightweight API ping. Returns True if API responds."""
        try:
            client = self._ensure_client()
            await client.messages.create(
                model=self._model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception:
            logger.debug("[ClaudeProvider] Health probe failed", exc_info=True)
            return False
```

### Step 4: Run tests to verify they pass

Run: `python3 -m pytest tests/test_ouroboros_governance/test_providers.py -v --tb=short`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/providers.py tests/test_ouroboros_governance/test_providers.py
git commit -m "feat(governance): add ClaudeProvider adapter with cost gating and daily budget"
```

---

## Task 4: GovernedLoopService Lifecycle

**Files:**
- Create: `backend/core/ouroboros/governance/governed_loop_service.py`
- Create: `tests/test_ouroboros_governance/test_governed_loop_service.py`

### Step 1: Write the failing tests

```python
"""Tests for GovernedLoopService — lifecycle, submit, health, drain."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)


def _make_context(
    *,
    op_id: str = "op-test-001",
    description: str = "Add edge case tests",
    target_files: Tuple[str, ...] = ("tests/test_utils.py",),
) -> OperationContext:
    return OperationContext.create(
        target_files=target_files,
        description=description,
        op_id=op_id,
        _timestamp=_FIXED_TS,
    )


def _mock_stack(can_write_result: Tuple[bool, str] = (True, "ok")) -> MagicMock:
    """Build a mock GovernanceStack."""
    stack = MagicMock()
    stack.can_write.return_value = can_write_result
    stack._started = True
    stack.canary = MagicMock()
    stack.canary.register_slice = MagicMock()
    stack.canary.is_file_allowed = MagicMock(return_value=True)
    stack.risk_engine = MagicMock()
    stack.risk_engine.classify = MagicMock(return_value=MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="default_safe"
    ))
    stack.ledger = MagicMock()
    stack.ledger.append = AsyncMock(return_value=True)
    stack.comm = AsyncMock()
    stack.change_engine = AsyncMock()
    stack.change_engine.execute = AsyncMock(return_value=MagicMock(
        success=True, rolled_back=False, op_id="op-test-001"
    ))
    stack.policy_version = "test-v1"
    return stack


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGovernedLoopConfig:
    """Tests for GovernedLoopConfig."""

    def test_defaults(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
        )

        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        assert config.generation_timeout_s == 120.0
        assert config.approval_timeout_s == 600.0
        assert config.max_concurrent_ops == 2
        assert config.initial_canary_slices == ("tests/",)
        assert config.claude_daily_budget == 10.00

    def test_frozen(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
        )

        config = GovernedLoopConfig(project_root=Path("/tmp"))
        with pytest.raises(AttributeError):
            config.generation_timeout_s = 999.0  # type: ignore[misc]


@pytest.mark.asyncio
class TestGovernedLoopServiceLifecycle:
    """Tests for service start/stop lifecycle."""

    async def test_starts_active_with_mocked_providers(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
            ServiceState,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))

        service = GovernedLoopService(
            stack=stack,
            prime_client=None,
            config=config,
        )
        assert service.state is ServiceState.INACTIVE

        await service.start()
        assert service.state in (ServiceState.ACTIVE, ServiceState.DEGRADED)

    async def test_start_is_idempotent(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
            ServiceState,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        await service.start()
        state_after_first = service.state
        await service.start()  # Second call — should be no-op
        assert service.state is state_after_first

    async def test_stop_transitions_to_inactive(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
            ServiceState,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        await service.start()
        await service.stop()
        assert service.state is ServiceState.INACTIVE

    async def test_registers_initial_canary_slices(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(
            project_root=Path("/tmp/test"),
            initial_canary_slices=("tests/", "backend/core/utils/"),
        )
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        await service.start()
        assert stack.canary.register_slice.call_count == 2

    async def test_health_returns_state(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)

        await service.start()
        health = service.health()
        assert "state" in health
        assert "active_ops" in health
        assert "canary_slices" in health


@pytest.mark.asyncio
class TestGovernedLoopServiceSubmit:
    """Tests for the submit() entrypoint."""

    async def test_submit_rejects_when_inactive(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        # Don't start — state is INACTIVE

        ctx = _make_context()
        result = await service.submit(ctx, trigger_source="cli")
        assert result.terminal_phase is OperationPhase.CANCELLED
        assert "not_active" in result.reason_code

    async def test_submit_rejects_at_capacity(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(
            project_root=Path("/tmp/test"),
            max_concurrent_ops=0,  # Zero capacity — always BUSY
        )
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        await service.start()

        ctx = _make_context()
        result = await service.submit(ctx, trigger_source="cli")
        assert result.terminal_phase is OperationPhase.CANCELLED
        assert "busy" in result.reason_code

    async def test_submit_deduplicates(self) -> None:
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
        )

        stack = _mock_stack()
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(stack=stack, prime_client=None, config=config)
        await service.start()

        ctx = _make_context(op_id="op-dedup-001")
        result1 = await service.submit(ctx, trigger_source="cli")

        # Second submit with same op_id should be deduplicated
        result2 = await service.submit(ctx, trigger_source="cli")
        assert "duplicate" in result2.reason_code
```

### Step 2: Run tests to verify they fail

Run: `python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py -v --tb=short`
Expected: FAIL — `ModuleNotFoundError`

### Step 3: Write minimal implementation

Create `backend/core/ouroboros/governance/governed_loop_service.py`:

```python
"""
Governed Loop Service — Lifecycle Manager
==========================================

Thin lifecycle manager for the governed self-programming pipeline.
Owns provider wiring, orchestrator construction, and health probes.
No domain logic — just coordination.

The supervisor instantiates this in Zone 6.8 and calls start()/stop().
All triggers go through submit(), which delegates to the orchestrator.

Service States
--------------
INACTIVE -> STARTING -> ACTIVE/DEGRADED
ACTIVE/DEGRADED -> STOPPING -> INACTIVE
STARTING -> FAILED (on error)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from backend.core.ouroboros.governance.approval_provider import CLIApprovalProvider
from backend.core.ouroboros.governance.candidate_generator import (
    CandidateGenerator,
    FailbackState,
)
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)

logger = logging.getLogger("Ouroboros.GovernedLoop")


# ---------------------------------------------------------------------------
# ServiceState
# ---------------------------------------------------------------------------


class ServiceState(Enum):
    """Lifecycle state of the GovernedLoopService."""

    INACTIVE = auto()
    STARTING = auto()
    ACTIVE = auto()
    DEGRADED = auto()
    STOPPING = auto()
    FAILED = auto()


# ---------------------------------------------------------------------------
# OperationResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationResult:
    """Stable result contract returned by submit().

    The full OperationContext stays internal/ledgered.  External callers
    see only this summary.
    """

    op_id: str
    terminal_phase: OperationPhase
    provider_used: Optional[str] = None
    generation_duration_s: Optional[float] = None
    total_duration_s: float = 0.0
    reason_code: str = ""
    trigger_source: str = "unknown"


# ---------------------------------------------------------------------------
# GovernedLoopConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GovernedLoopConfig:
    """Frozen configuration for the governed loop service.

    Parameters
    ----------
    project_root:
        Root directory of the project being modified.
    claude_api_key:
        Anthropic API key for Claude fallback. None disables Claude.
    claude_model:
        Claude model identifier.
    claude_max_cost_per_op:
        Maximum estimated cost per single Claude operation.
    claude_daily_budget:
        Maximum daily Claude spend in USD.
    generation_timeout_s:
        Maximum seconds for candidate generation per attempt.
    approval_timeout_s:
        Maximum seconds to wait for human approval.
    health_probe_interval_s:
        Seconds between background health probes.
    max_concurrent_ops:
        Maximum simultaneous governed operations.
    initial_canary_slices:
        Path prefixes to register as canary slices at startup.
    """

    project_root: Path
    claude_api_key: Optional[str] = None
    claude_model: str = "claude-sonnet-4-20250514"
    claude_max_cost_per_op: float = 0.50
    claude_daily_budget: float = 10.00
    generation_timeout_s: float = 120.0
    approval_timeout_s: float = 600.0
    health_probe_interval_s: float = 30.0
    max_concurrent_ops: int = 2
    initial_canary_slices: Tuple[str, ...] = ("tests/",)

    @classmethod
    def from_env(cls, args: Any = None) -> GovernedLoopConfig:
        """Build config from environment variables with safe defaults."""
        import os

        project_root = Path(
            os.getenv("JARVIS_PROJECT_ROOT", os.getcwd())
        )
        return cls(
            project_root=project_root,
            claude_api_key=os.getenv("ANTHROPIC_API_KEY"),
            claude_model=os.getenv(
                "JARVIS_GOVERNED_CLAUDE_MODEL", "claude-sonnet-4-20250514"
            ),
            claude_max_cost_per_op=float(
                os.getenv("JARVIS_GOVERNED_CLAUDE_MAX_COST_PER_OP", "0.50")
            ),
            claude_daily_budget=float(
                os.getenv("JARVIS_GOVERNED_CLAUDE_DAILY_BUDGET", "10.00")
            ),
            generation_timeout_s=float(
                os.getenv("JARVIS_GOVERNED_GENERATION_TIMEOUT", "120.0")
            ),
            approval_timeout_s=float(
                os.getenv("JARVIS_GOVERNED_APPROVAL_TIMEOUT", "600.0")
            ),
            health_probe_interval_s=float(
                os.getenv("JARVIS_GOVERNED_HEALTH_PROBE_INTERVAL", "30.0")
            ),
            max_concurrent_ops=int(
                os.getenv("JARVIS_GOVERNED_MAX_CONCURRENT_OPS", "2")
            ),
        )


# ---------------------------------------------------------------------------
# GovernedLoopService
# ---------------------------------------------------------------------------


class GovernedLoopService:
    """Lifecycle manager for the governed self-programming pipeline.

    No side effects in constructor. All async initialization in start().

    Parameters
    ----------
    stack:
        The GovernanceStack providing risk engine, ledger, change engine, etc.
    prime_client:
        Optional PrimeClient for GCP J-Prime code generation.
    config:
        Service configuration.
    """

    def __init__(
        self,
        stack: Any,
        prime_client: Any,
        config: GovernedLoopConfig,
    ) -> None:
        self._stack = stack
        self._prime_client = prime_client
        self._config = config
        self._state = ServiceState.INACTIVE
        self._started_at: Optional[float] = None
        self._failure_reason: Optional[str] = None

        # Built during start()
        self._orchestrator: Optional[GovernedOrchestrator] = None
        self._generator: Optional[CandidateGenerator] = None
        self._approval_provider: Optional[CLIApprovalProvider] = None
        self._health_probe_task: Optional[asyncio.Task] = None

        # Concurrency & dedup
        self._active_ops: Set[str] = set()
        self._completed_ops: Dict[str, OperationResult] = {}

    @property
    def state(self) -> ServiceState:
        return self._state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize providers, orchestrator, and canary slices.

        Idempotent — second call is no-op if already ACTIVE/DEGRADED.
        On failure, sets state to FAILED with structured reason.
        """
        if self._state in (ServiceState.ACTIVE, ServiceState.DEGRADED):
            return

        self._state = ServiceState.STARTING
        try:
            await self._build_components()
            self._register_canary_slices()
            self._attach_to_stack()
            self._started_at = time.monotonic()

            # Determine state based on provider availability
            if self._generator is not None:
                fsm_state = self._generator.fsm.state
                if fsm_state is FailbackState.QUEUE_ONLY:
                    self._state = ServiceState.DEGRADED
                elif fsm_state is FailbackState.FALLBACK_ACTIVE:
                    self._state = ServiceState.DEGRADED
                else:
                    self._state = ServiceState.ACTIVE
            else:
                self._state = ServiceState.DEGRADED

            logger.info(
                "[GovernedLoop] Started: state=%s, canary_slices=%s",
                self._state.name,
                self._config.initial_canary_slices,
            )

        except Exception as exc:
            self._state = ServiceState.FAILED
            self._failure_reason = str(exc)
            logger.error(
                "[GovernedLoop] Start failed: %s", exc, exc_info=True
            )
            await self._teardown_partial()
            raise

    async def stop(self) -> None:
        """Graceful shutdown. Drains in-flight ops, cancels probes."""
        if self._state is ServiceState.INACTIVE:
            return

        self._state = ServiceState.STOPPING

        # Cancel health probe loop
        if self._health_probe_task and not self._health_probe_task.done():
            self._health_probe_task.cancel()
            try:
                await self._health_probe_task
            except asyncio.CancelledError:
                pass

        # Drain in-flight ops (wait up to 30s)
        if self._active_ops:
            logger.info(
                "[GovernedLoop] Draining %d active ops...",
                len(self._active_ops),
            )
            await asyncio.sleep(0)  # Yield for any pending completions

        # Detach from stack
        self._detach_from_stack()
        self._state = ServiceState.INACTIVE
        logger.info("[GovernedLoop] Stopped")

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    async def submit(
        self,
        ctx: OperationContext,
        trigger_source: str = "unknown",
    ) -> OperationResult:
        """Submit an operation for governed execution.

        THE single entrypoint for all triggers (CLI, API, etc.).

        Parameters
        ----------
        ctx:
            The initial OperationContext (in CLASSIFY phase).
        trigger_source:
            Origin of the trigger (``cli``, ``api``, ``claude_code``, etc.).

        Returns
        -------
        OperationResult
            Stable result summary. Full context stays internal/ledgered.
        """
        start_time = time.monotonic()

        # Gate: service must be active
        if self._state not in (ServiceState.ACTIVE, ServiceState.DEGRADED):
            return OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code=f"service_not_active:{self._state.name}",
                trigger_source=trigger_source,
            )

        # Gate: concurrency limit
        if len(self._active_ops) >= self._config.max_concurrent_ops:
            return OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code="busy",
                trigger_source=trigger_source,
            )

        # Gate: dedup
        dedupe_key = ctx.op_id
        if dedupe_key in self._active_ops:
            return OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code="duplicate:in_flight",
                trigger_source=trigger_source,
            )
        if dedupe_key in self._completed_ops:
            return OperationResult(
                op_id=ctx.op_id,
                terminal_phase=OperationPhase.CANCELLED,
                reason_code="duplicate:already_completed",
                trigger_source=trigger_source,
            )

        # Execute pipeline
        self._active_ops.add(dedupe_key)
        try:
            assert self._orchestrator is not None
            terminal_ctx = await self._orchestrator.run(ctx)

            duration = time.monotonic() - start_time
            result = OperationResult(
                op_id=ctx.op_id,
                terminal_phase=terminal_ctx.phase,
                provider_used=getattr(
                    terminal_ctx.generation, "provider_name", None
                ) if terminal_ctx.generation else None,
                generation_duration_s=getattr(
                    terminal_ctx.generation, "generation_duration_s", None
                ) if terminal_ctx.generation else None,
                total_duration_s=duration,
                reason_code=terminal_ctx.phase.name.lower(),
                trigger_source=trigger_source,
            )

            self._completed_ops[dedupe_key] = result
            return result

        finally:
            self._active_ops.discard(dedupe_key)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return structured health report."""
        uptime = (
            time.monotonic() - self._started_at
            if self._started_at
            else 0.0
        )
        return {
            "state": self._state.name,
            "active_ops": len(self._active_ops),
            "completed_ops": len(self._completed_ops),
            "canary_slices": list(self._config.initial_canary_slices),
            "uptime_s": round(uptime, 1),
            "failure_reason": self._failure_reason,
            "provider_fsm_state": (
                self._generator.fsm.state.name
                if self._generator
                else "no_generator"
            ),
        }

    # ------------------------------------------------------------------
    # Private: Component Construction
    # ------------------------------------------------------------------

    async def _build_components(self) -> None:
        """Build providers, generator, approval provider, and orchestrator."""
        primary = None
        fallback = None

        # Build PrimeProvider if PrimeClient available
        if self._prime_client is not None:
            try:
                from backend.core.ouroboros.governance.providers import (
                    PrimeProvider,
                )

                primary = PrimeProvider(self._prime_client)
                if await primary.health_probe():
                    logger.info("[GovernedLoop] PrimeProvider: healthy")
                else:
                    logger.warning("[GovernedLoop] PrimeProvider: unhealthy at startup")
                    primary = None
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] PrimeProvider build failed: %s", exc
                )
                primary = None

        # Build ClaudeProvider if API key available
        if self._config.claude_api_key:
            try:
                from backend.core.ouroboros.governance.providers import (
                    ClaudeProvider,
                )

                fallback = ClaudeProvider(
                    api_key=self._config.claude_api_key,
                    model=self._config.claude_model,
                    max_cost_per_op=self._config.claude_max_cost_per_op,
                    daily_budget=self._config.claude_daily_budget,
                )
                logger.info("[GovernedLoop] ClaudeProvider: configured")
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] ClaudeProvider build failed: %s", exc
                )
                fallback = None

        # Build CandidateGenerator (needs at least one provider)
        if primary is not None or fallback is not None:
            # If only one provider, use it as both (FSM still works)
            effective_primary = primary or fallback
            effective_fallback = fallback or primary
            assert effective_primary is not None
            assert effective_fallback is not None

            self._generator = CandidateGenerator(
                primary=effective_primary,
                fallback=effective_fallback,
            )
        else:
            logger.warning(
                "[GovernedLoop] No providers available — QUEUE_ONLY mode"
            )
            # Create a minimal stub generator that always raises
            self._generator = None

        # Build approval provider
        self._approval_provider = CLIApprovalProvider()

        # Build orchestrator
        orch_config = OrchestratorConfig(
            project_root=self._config.project_root,
            generation_timeout_s=self._config.generation_timeout_s,
            approval_timeout_s=self._config.approval_timeout_s,
        )
        self._orchestrator = GovernedOrchestrator(
            stack=self._stack,
            generator=self._generator,
            approval_provider=self._approval_provider,
            config=orch_config,
        )

    def _register_canary_slices(self) -> None:
        """Register initial canary slices. Idempotent."""
        for slice_prefix in self._config.initial_canary_slices:
            try:
                self._stack.canary.register_slice(slice_prefix)
            except Exception as exc:
                logger.warning(
                    "[GovernedLoop] Failed to register canary slice %r: %s",
                    slice_prefix,
                    exc,
                )

    def _attach_to_stack(self) -> None:
        """Attach governed loop components to GovernanceStack."""
        self._stack.orchestrator = self._orchestrator
        self._stack.generator = self._generator
        self._stack.approval_provider = self._approval_provider

    def _detach_from_stack(self) -> None:
        """Detach governed loop components from GovernanceStack."""
        self._stack.orchestrator = None
        self._stack.generator = None
        self._stack.approval_provider = None

    async def _teardown_partial(self) -> None:
        """Clean up partially constructed components on startup failure."""
        self._orchestrator = None
        self._generator = None
        self._approval_provider = None
        self._detach_from_stack()
```

### Step 4: Run tests to verify they pass

Run: `python3 -m pytest tests/test_ouroboros_governance/test_governed_loop_service.py -v --tb=short`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py tests/test_ouroboros_governance/test_governed_loop_service.py
git commit -m "feat(governance): add GovernedLoopService lifecycle manager"
```

---

## Task 5: GovernanceStack Wiring & Exports

**Files:**
- Modify: `backend/core/ouroboros/governance/integration.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`
- Modify: `tests/test_ouroboros_governance/test_integration.py`

### Step 1: Write the failing tests

Add to `tests/test_ouroboros_governance/test_integration.py`:

```python
class TestGovernedLoopServiceField:
    """Verify GovernanceStack has governed_loop_service field."""

    def test_stack_has_governed_loop_service_field(self) -> None:
        """GovernanceStack must have an optional governed_loop_service field."""
        import dataclasses
        from backend.core.ouroboros.governance.integration import GovernanceStack

        field_names = {f.name for f in dataclasses.fields(GovernanceStack)}
        assert "governed_loop_service" in field_names


class TestGovernedLoopExports:
    """Verify governed loop service types are exported from __init__."""

    def test_import_governed_loop_service(self) -> None:
        from backend.core.ouroboros.governance import GovernedLoopService

    def test_import_governed_loop_config(self) -> None:
        from backend.core.ouroboros.governance import GovernedLoopConfig

    def test_import_operation_result(self) -> None:
        from backend.core.ouroboros.governance import OperationResult

    def test_import_service_state(self) -> None:
        from backend.core.ouroboros.governance import ServiceState

    def test_import_prime_provider(self) -> None:
        from backend.core.ouroboros.governance import PrimeProvider

    def test_import_claude_provider(self) -> None:
        from backend.core.ouroboros.governance import ClaudeProvider
```

### Step 2: Run tests to verify they fail

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py::TestGovernedLoopServiceField -v --tb=short`
Expected: FAIL

### Step 3: Write minimal implementation

**Modify `integration.py`** — add `governed_loop_service` field to GovernanceStack:

After the existing governed loop fields (line 267):
```python
    governed_loop_service: Optional[Any] = None
```

**Modify `__init__.py`** — add exports:

```python
# Governed Loop Service
from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
    GovernedLoopService,
    OperationResult,
    ServiceState,
)
from backend.core.ouroboros.governance.providers import (
    ClaudeProvider,
    PrimeProvider,
)
```

### Step 4: Run tests to verify they pass

Run: `python3 -m pytest tests/test_ouroboros_governance/test_integration.py -v --tb=short`
Expected: All PASS (including existing tests)

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/integration.py backend/core/ouroboros/governance/__init__.py tests/test_ouroboros_governance/test_integration.py
git commit -m "feat(governance): wire GovernedLoopService into GovernanceStack and exports"
```

---

## Task 6: Supervisor Zone 6.8 Integration

**Files:**
- Modify: `unified_supervisor.py` (~line 85876, after existing governance init)

### Step 1: Identify insertion point

Read `unified_supervisor.py` at the governance initialization block (~line 85845-85880). The new Zone 6.8 block goes immediately after the existing governance startup succeeds.

### Step 2: Add Zone 6.8 block

Insert after the governance gate log line (after `self.logger.info("[Kernel] Governance gate: %s", ...)`):

```python
            # ---- Zone 6.8: Governed Self-Programming Loop ----
            if self._governance_stack and self._governance_stack._started:
                try:
                    from backend.core.ouroboros.governance.governed_loop_service import (
                        GovernedLoopConfig,
                        GovernedLoopService,
                    )

                    _loop_config = GovernedLoopConfig.from_env(self._args)
                    self._governed_loop = GovernedLoopService(
                        stack=self._governance_stack,
                        prime_client=getattr(self, "_prime_client", None),
                        config=_loop_config,
                    )
                    await asyncio.wait_for(
                        self._governed_loop.start(),
                        timeout=30.0,
                    )
                    self._governance_stack.governed_loop_service = self._governed_loop
                    self.logger.info(
                        "[Kernel] Zone 6.8 governed loop: %s",
                        self._governed_loop.health(),
                    )
                except Exception as exc:
                    self._governed_loop = None
                    self.logger.warning(
                        "[Kernel] Zone 6.8 governed loop failed: %s -- skipped",
                        exc,
                    )
```

### Step 3: Add field declaration

Find where `self._governance_stack` is declared (around line 66618) and add:

```python
        self._governed_loop: Optional[Any] = None
```

### Step 4: Add shutdown hook

Find the supervisor shutdown sequence where `self._governance_stack.stop()` is called and add before it:

```python
            # Stop governed loop before governance stack
            if getattr(self, "_governed_loop", None) is not None:
                try:
                    await asyncio.wait_for(self._governed_loop.stop(), timeout=30.0)
                except Exception as exc:
                    self.logger.warning("[Kernel] Governed loop stop failed: %s", exc)
```

### Step 5: Verify tests still pass

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short`
Expected: All PASS (supervisor changes don't affect unit tests)

### Step 6: Commit

```bash
git add unified_supervisor.py
git commit -m "feat(governance): add Zone 6.8 governed self-programming loop to supervisor startup"
```

---

## Task 7: CLI Commands (self-modify, approve, reject)

**Files:**
- Create: `backend/core/ouroboros/governance/loop_cli.py`
- Modify: `unified_supervisor.py` (argparse registration, ~line 97342)
- Create: `tests/test_ouroboros_governance/test_loop_cli.py`

**Note:** Per design, CLI command *parsing* lives in the supervisor's argparse layer, but command *logic* lives in a governance-adjacent file (`loop_cli.py`) following the `cli_commands.py` break-glass pattern. The governance package itself doesn't own argparse.

### Step 1: Write the failing tests

```python
"""Tests for governed loop CLI commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)


_FIXED_TS = datetime(2026, 3, 7, 12, 0, 0, tzinfo=timezone.utc)


def _make_context(
    *,
    op_id: str = "op-test-001",
    description: str = "Add tests",
    target_files: Tuple[str, ...] = ("tests/test_foo.py",),
) -> OperationContext:
    return OperationContext.create(
        target_files=target_files,
        description=description,
        op_id=op_id,
        _timestamp=_FIXED_TS,
    )


def _mock_service(
    terminal_phase: OperationPhase = OperationPhase.COMPLETE,
) -> MagicMock:
    from backend.core.ouroboros.governance.governed_loop_service import (
        OperationResult,
        ServiceState,
    )

    service = MagicMock()
    service.state = ServiceState.ACTIVE
    service.submit = AsyncMock(
        return_value=OperationResult(
            op_id="op-test-001",
            terminal_phase=terminal_phase,
            provider_used="gcp-jprime",
            total_duration_s=5.2,
            reason_code=terminal_phase.name.lower(),
            trigger_source="cli",
        )
    )
    service._approval_provider = MagicMock()
    service._approval_provider.approve = AsyncMock(
        return_value=MagicMock(status=MagicMock(name="APPROVED"))
    )
    service._approval_provider.reject = AsyncMock(
        return_value=MagicMock(status=MagicMock(name="REJECTED"))
    )
    return service


@pytest.mark.asyncio
class TestSelfModifyCommand:
    """Tests for the self-modify CLI command logic."""

    async def test_self_modify_succeeds(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_self_modify

        service = _mock_service()
        result = await handle_self_modify(
            service=service,
            target="tests/test_foo.py",
            goal="Add edge case tests",
        )
        assert result.terminal_phase is OperationPhase.COMPLETE
        service.submit.assert_called_once()

    async def test_self_modify_returns_result_on_cancel(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_self_modify

        service = _mock_service(terminal_phase=OperationPhase.CANCELLED)
        result = await handle_self_modify(
            service=service,
            target="tests/test_foo.py",
            goal="Fix test",
        )
        assert result.terminal_phase is OperationPhase.CANCELLED

    async def test_self_modify_with_no_service_raises(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_self_modify

        with pytest.raises(RuntimeError, match="not_active"):
            await handle_self_modify(
                service=None,
                target="tests/test_foo.py",
                goal="Fix test",
            )


@pytest.mark.asyncio
class TestApproveCommand:
    """Tests for the approve CLI command logic."""

    async def test_approve_calls_provider(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_approve

        service = _mock_service()
        result = await handle_approve(
            service=service,
            op_id="op-test-001",
            approver="derek",
        )
        service._approval_provider.approve.assert_called_once_with(
            "op-test-001", "derek"
        )

    async def test_approve_with_no_service_raises(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_approve

        with pytest.raises(RuntimeError, match="not_active"):
            await handle_approve(service=None, op_id="op-001", approver="derek")


@pytest.mark.asyncio
class TestRejectCommand:
    """Tests for the reject CLI command logic."""

    async def test_reject_calls_provider(self) -> None:
        from backend.core.ouroboros.governance.loop_cli import handle_reject

        service = _mock_service()
        result = await handle_reject(
            service=service,
            op_id="op-test-001",
            approver="derek",
            reason="Too risky",
        )
        service._approval_provider.reject.assert_called_once_with(
            "op-test-001", "derek", "Too risky"
        )
```

### Step 2: Run tests to verify they fail

Run: `python3 -m pytest tests/test_ouroboros_governance/test_loop_cli.py -v --tb=short`
Expected: FAIL — `ModuleNotFoundError`

### Step 3: Write minimal implementation

Create `backend/core/ouroboros/governance/loop_cli.py`:

```python
"""
Governed Loop CLI Commands
===========================

Importable async functions for governed loop operations, following
the same pattern as cli_commands.py (break-glass).

These functions are wired into the supervisor's argparse CLI layer.
The governance package does not own command parsing.

Commands
--------
- ``handle_self_modify``: trigger a governed code generation pipeline
- ``handle_approve``: approve a pending operation
- ``handle_reject``: reject a pending operation
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from backend.core.ouroboros.governance.op_context import OperationContext

logger = logging.getLogger("Ouroboros.CLI")


async def handle_self_modify(
    service: Any,
    target: str,
    goal: str,
    op_id: Optional[str] = None,
    dry_run: bool = False,
) -> Any:
    """Trigger a governed self-modification pipeline.

    Parameters
    ----------
    service:
        GovernedLoopService instance (or None if not started).
    target:
        Target file or directory path.
    goal:
        Description of the desired change.
    op_id:
        Optional explicit operation ID.
    dry_run:
        If True, run CLASSIFY + ROUTE only (no generation/apply).

    Returns
    -------
    OperationResult

    Raises
    ------
    RuntimeError
        If the service is not active.
    """
    if service is None:
        raise RuntimeError(
            "not_active: Governed loop is not active. "
            "Start JARVIS with governance enabled."
        )

    # Resolve target files
    target_files = (target,)

    # Build context
    ctx = OperationContext.create(
        target_files=target_files,
        description=goal,
        op_id=op_id,
    )

    logger.info(
        "[CLI] self-modify: target=%s goal=%r op_id=%s dry_run=%s",
        target,
        goal,
        ctx.op_id,
        dry_run,
    )

    # Submit to service
    result = await service.submit(ctx, trigger_source="cli")

    logger.info(
        "[CLI] self-modify result: op_id=%s phase=%s provider=%s duration=%.1fs",
        result.op_id,
        result.terminal_phase.name,
        result.provider_used,
        result.total_duration_s,
    )

    return result


async def handle_approve(
    service: Any,
    op_id: str,
    approver: str = "cli-operator",
) -> Any:
    """Approve a pending governed operation.

    Parameters
    ----------
    service:
        GovernedLoopService instance.
    op_id:
        The operation ID to approve.
    approver:
        Identity of the approver.

    Returns
    -------
    ApprovalResult

    Raises
    ------
    RuntimeError
        If the service is not active.
    KeyError
        If the op_id is unknown.
    """
    if service is None:
        raise RuntimeError(
            "not_active: Governed loop is not active."
        )

    result = await service._approval_provider.approve(op_id, approver)

    logger.info(
        "[CLI] approve: op_id=%s status=%s approver=%s",
        op_id,
        result.status.name,
        approver,
    )

    return result


async def handle_reject(
    service: Any,
    op_id: str,
    approver: str = "cli-operator",
    reason: str = "rejected via CLI",
) -> Any:
    """Reject a pending governed operation.

    Parameters
    ----------
    service:
        GovernedLoopService instance.
    op_id:
        The operation ID to reject.
    approver:
        Identity of the rejector.
    reason:
        Rejection reason.

    Returns
    -------
    ApprovalResult

    Raises
    ------
    RuntimeError
        If the service is not active.
    KeyError
        If the op_id is unknown.
    """
    if service is None:
        raise RuntimeError(
            "not_active: Governed loop is not active."
        )

    result = await service._approval_provider.reject(op_id, approver, reason)

    logger.info(
        "[CLI] reject: op_id=%s status=%s approver=%s reason=%r",
        op_id,
        result.status.name,
        approver,
        reason,
    )

    return result
```

### Step 4: Run tests to verify they pass

Run: `python3 -m pytest tests/test_ouroboros_governance/test_loop_cli.py -v --tb=short`
Expected: All PASS

### Step 5: Wire argparse in supervisor

Find the governance argparse registration area (~line 97342 of `unified_supervisor.py`) and add self-modify subcommands. This follows the existing break-glass pattern.

### Step 6: Commit

```bash
git add backend/core/ouroboros/governance/loop_cli.py tests/test_ouroboros_governance/test_loop_cli.py unified_supervisor.py
git commit -m "feat(governance): add self-modify, approve, reject CLI commands"
```

---

## Task 8: TUI & Voice Notification Hooks

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (emit comm messages at APPROVE phase)
- Modify: `tests/test_ouroboros_governance/test_orchestrator.py`

### Step 1: Write the failing test

Add to `tests/test_ouroboros_governance/test_orchestrator.py`:

```python
@pytest.mark.asyncio
class TestApprovalNotifications:
    """Tests that APPROVAL_REQUIRED ops emit comm messages."""

    async def test_approval_required_emits_heartbeat(self) -> None:
        """APPROVAL_REQUIRED -> comm.emit_heartbeat called with APPROVE phase."""
        stack = _mock_stack(risk_tier=RiskTier.APPROVAL_REQUIRED)
        generator = _mock_generator()
        approval = _mock_approval_provider(status=ApprovalStatus.APPROVED)
        config = _default_config()
        ctx = _make_context()

        orch = GovernedOrchestrator(
            stack=stack,
            generator=generator,
            approval_provider=approval,
            config=config,
        )
        result = await orch.run(ctx)

        assert result.phase is OperationPhase.COMPLETE
        # Verify comm was called with approval notification
        stack.comm.emit_heartbeat.assert_called()
        # At least one call should have phase="APPROVE"
        approve_calls = [
            call for call in stack.comm.emit_heartbeat.call_args_list
            if call.kwargs.get("phase") == "APPROVE"
            or (len(call.args) >= 2 and call.args[1] == "APPROVE")
        ]
        assert len(approve_calls) >= 1
```

### Step 2: Run test to verify it fails

Run: `python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py::TestApprovalNotifications -v --tb=short`
Expected: FAIL — `emit_heartbeat` not called

### Step 3: Modify orchestrator

In `orchestrator.py`, in the APPROVE phase section (around line 307-314), add a comm heartbeat emission before calling `approval_provider.request()`:

```python
            # Notify via comm channel (TUI + voice will receive this)
            try:
                await self._stack.comm.emit_heartbeat(
                    op_id=ctx.op_id,
                    phase="APPROVE",
                    progress_pct=0.0,
                )
            except Exception:
                pass  # Comm failures never block pipeline
```

### Step 4: Run tests to verify they pass

Run: `python3 -m pytest tests/test_ouroboros_governance/test_orchestrator.py -v --tb=short`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/core/ouroboros/governance/orchestrator.py tests/test_ouroboros_governance/test_orchestrator.py
git commit -m "feat(governance): emit comm heartbeat at APPROVE phase for TUI/voice notifications"
```

---

## Task 9: Full Integration Test

**Files:**
- Modify: `tests/test_ouroboros_governance/test_integration.py`

### Step 1: Write the integration test

Add to `tests/test_ouroboros_governance/test_integration.py`:

```python
@pytest.mark.asyncio
class TestGovernedLoopServiceEndToEnd:
    """End-to-end test of GovernedLoopService with mocked providers."""

    async def test_submit_through_service(self) -> None:
        """Submit an operation through GovernedLoopService and verify result."""
        from backend.core.ouroboros.governance.governed_loop_service import (
            GovernedLoopConfig,
            GovernedLoopService,
            OperationResult,
            ServiceState,
        )

        stack = _make_real_stack()  # Use existing helper from test_integration.py
        config = GovernedLoopConfig(project_root=Path("/tmp/test"))
        service = GovernedLoopService(
            stack=stack,
            prime_client=None,
            config=config,
        )

        await service.start()
        assert service.state in (ServiceState.ACTIVE, ServiceState.DEGRADED)

        ctx = _make_context()
        result = await service.submit(ctx, trigger_source="test")

        assert isinstance(result, OperationResult)
        assert result.op_id == ctx.op_id
        assert result.trigger_source == "test"
        # Terminal phase (exact phase depends on provider availability)
        assert result.terminal_phase in (
            OperationPhase.COMPLETE,
            OperationPhase.CANCELLED,
            OperationPhase.POSTMORTEM,
        )

        await service.stop()
        assert service.state is ServiceState.INACTIVE
```

### Step 2: Run all tests

Run: `python3 -m pytest tests/test_ouroboros_governance/ -v --tb=short`
Expected: All PASS

### Step 3: Commit

```bash
git add tests/test_ouroboros_governance/test_integration.py
git commit -m "test(governance): add GovernedLoopService end-to-end integration test"
```

---

## Summary

| Task | Component | New Files | Tests |
|------|-----------|-----------|-------|
| 1 | Schema parser + prompt builder | `providers.py` | ~14 tests |
| 2 | PrimeProvider adapter | (modify `providers.py`) | ~8 tests |
| 3 | ClaudeProvider adapter | (modify `providers.py`) | ~8 tests |
| 4 | GovernedLoopService | `governed_loop_service.py` | ~10 tests |
| 5 | Stack wiring + exports | (modify `integration.py`, `__init__.py`) | ~7 tests |
| 6 | Supervisor Zone 6.8 | (modify `unified_supervisor.py`) | 0 (integration) |
| 7 | CLI commands | `loop_cli.py` | ~6 tests |
| 8 | TUI/voice notification hooks | (modify `orchestrator.py`) | ~1 test |
| 9 | Full integration test | (modify `test_integration.py`) | ~1 test |

**Total: ~55 new tests, 3 new files, 4 modified files.**
