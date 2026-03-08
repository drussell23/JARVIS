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
            except SyntaxError as exc:
                raise RuntimeError(
                    f"{provider_name}_schema_invalid:candidate_{i}_syntax_error"
                ) from exc

        validated.append({"file": file_path, "content": content})

    return GenerationResult(
        candidates=tuple(validated),
        provider_name=provider_name,
        generation_duration_s=duration_s,
    )


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
