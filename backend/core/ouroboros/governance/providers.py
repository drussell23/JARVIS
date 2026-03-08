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
