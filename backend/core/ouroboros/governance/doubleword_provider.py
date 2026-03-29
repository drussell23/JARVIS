"""
DoublewordProvider — Tier 0 batch inference via Doubleword's 397B MoE model.

Implements the CandidateProvider protocol for the Ouroboros governance pipeline.
Uses Doubleword's 4-stage async batch API (upload → create → poll → retrieve).

Boundary Principle:
  Deterministic: Batch protocol, JSONL formatting, polling cadence, cost tracking.
  Agentic: The routing decision to USE Doubleword (complexity > 0.85, ULTRA_TASKS)
           is made by the governance pipeline's routing layer, not this provider.

Doubleword API docs: https://docs.doubleword.ai
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — all env-driven, no hardcoding (Manifesto §5)
# ---------------------------------------------------------------------------

_DW_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
_DW_BASE_URL = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
_DW_MODEL = os.environ.get(
    "DOUBLEWORD_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8"
)
_DW_COMPLETION_WINDOW = os.environ.get("DOUBLEWORD_WINDOW", "1h")
_DW_MAX_TOKENS = int(os.environ.get("DOUBLEWORD_MAX_TOKENS", "10000"))
_DW_POLL_INTERVAL_S = float(os.environ.get("DOUBLEWORD_POLL_INTERVAL_S", "15"))
_DW_MAX_WAIT_S = float(os.environ.get("DOUBLEWORD_MAX_WAIT_S", "3600"))
_DW_TEMPERATURE = float(os.environ.get("DOUBLEWORD_TEMPERATURE", "0.2"))

# Pricing (March 2026)
_DW_INPUT_COST_PER_M = float(os.environ.get("DOUBLEWORD_INPUT_COST_PER_M", "0.10"))
_DW_OUTPUT_COST_PER_M = float(os.environ.get("DOUBLEWORD_OUTPUT_COST_PER_M", "0.40"))


@dataclass
class DoublewordStats:
    """Cumulative stats for observability (Pillar 7)."""
    total_batches: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_latency_s: float = 0.0
    failed_batches: int = 0
    empty_content_retries: int = 0


@dataclass
class PendingBatch:
    """Tracks an in-flight Doubleword batch for async retrieval."""
    op_id: str
    batch_id: str
    file_id: str
    prompt: str
    submitted_at: float  # time.monotonic()
    wall_submitted_at: float = field(default_factory=time.time)


@dataclass
class CompletedBatch:
    """Stores a completed Doubleword batch result for deferred application."""
    op_id: str
    batch_id: str
    result: "GenerationResult"
    completed_at: float  # time.monotonic()
    wall_completed_at: float = field(default_factory=time.time)


class DoublewordProvider:
    """Tier 0 CandidateProvider using Doubleword batch API with 397B MoE model.

    Follows the same protocol as PrimeProvider and ClaudeProvider:
      - generate(ctx) → GenerationResult
      - health_probe() → bool

    The batch API is 4-stage async:
      1. Upload JSONL file
      2. Create batch job
      3. Poll until completion
      4. Retrieve and parse results
    """

    def __init__(
        self,
        api_key: str = _DW_API_KEY,
        base_url: str = _DW_BASE_URL,
        model: str = _DW_MODEL,
        max_tokens: int = _DW_MAX_TOKENS,
        repo_root: Optional[Path] = None,
        repo_roots: Optional[Dict[str, Path]] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._repo_root = repo_root or Path(".")
        self._repo_roots = repo_roots or {}
        self._stats = DoublewordStats()
        self._session: Optional[Any] = None  # aiohttp.ClientSession (lazy)

    @property
    def is_available(self) -> bool:
        """Check if Doubleword is configured."""
        return bool(self._api_key)

    async def _get_session(self) -> Any:
        """Lazy-init persistent aiohttp session."""
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    # ------------------------------------------------------------------
    # Async decoupled API: submit_batch() + poll_and_retrieve()
    # ------------------------------------------------------------------

    async def submit_batch(
        self,
        ctx: OperationContext,
        *,
        prompt_override: Optional[str] = None,
    ) -> Optional[PendingBatch]:
        """Stage 1+2: Upload JSONL and create batch. Returns immediately.

        This is the fast path — typically completes in <2s. The caller
        should fire a background task to poll_and_retrieve() later.
        Returns None on failure (caller falls through to Tier 1).
        """
        if not self.is_available:
            return None

        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        prompt = prompt_override or _build_codegen_prompt(ctx)
        operation_id = getattr(ctx, "operation_id", f"dw-{int(time.time())}")

        jsonl_line = json.dumps({
            "custom_id": operation_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": "You are a code generation assistant for the Trinity AI ecosystem. Return valid JSON matching the requested schema."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": self._max_tokens,
                "temperature": _DW_TEMPERATURE,
            },
        })

        try:
            file_id = await self._upload_file(jsonl_line)
            if not file_id:
                logger.warning("[DoublewordProvider] submit_batch: file upload failed")
                return None

            batch_id = await self._create_batch(file_id)
            if not batch_id:
                logger.warning("[DoublewordProvider] submit_batch: batch creation failed")
                return None

            logger.info(
                "[DoublewordProvider] Batch %s submitted async (model=%s, op=%s)",
                batch_id, self._model, operation_id,
            )
            return PendingBatch(
                op_id=operation_id,
                batch_id=batch_id,
                file_id=file_id,
                prompt=prompt,
                submitted_at=time.monotonic(),
            )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[DoublewordProvider] submit_batch failed")
            return None

    async def poll_and_retrieve(
        self,
        pending: PendingBatch,
        ctx: OperationContext,
    ) -> Optional[GenerationResult]:
        """Stage 3+4: Poll batch to completion and parse results.

        This is the slow path — may take minutes. Designed to run as a
        background task via asyncio.create_task(). Returns None on failure.
        """
        t0 = pending.submitted_at

        try:
            output_file_id = await self._poll_batch(pending.batch_id)
            if not output_file_id:
                self._stats.failed_batches += 1
                logger.warning(
                    "[DoublewordProvider] Batch %s failed or timed out",
                    pending.batch_id,
                )
                return None

            content, usage = await self._retrieve_result(
                output_file_id, pending.op_id,
            )

            elapsed = time.monotonic() - t0
            self._stats.total_batches += 1
            self._stats.total_latency_s += elapsed

            if usage:
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                self._stats.total_input_tokens += input_tokens
                self._stats.total_output_tokens += output_tokens
                cost = (
                    input_tokens * _DW_INPUT_COST_PER_M / 1_000_000
                    + output_tokens * _DW_OUTPUT_COST_PER_M / 1_000_000
                )
                self._stats.total_cost_usd += cost

            if not content:
                self._stats.empty_content_retries += 1
                logger.warning(
                    "[DoublewordProvider] Batch %s returned empty content "
                    "(reasoning model exhausted token budget).",
                    pending.batch_id,
                )
                return None

            from backend.core.ouroboros.governance.providers import (
                _parse_generation_response,
            )
            import hashlib as _hl
            _src = pending.prompt[:500] if pending.prompt else ""
            _src_hash = _hl.sha256(_src.encode()).hexdigest()[:16]

            return _parse_generation_response(
                raw=content,
                provider_name="doubleword",
                duration_s=elapsed,
                ctx=ctx,
                source_hash=_src_hash,
                source_path="",
                repo_roots=self._repo_roots or None,
                repo_root=self._repo_root,
            )

        except asyncio.CancelledError:
            raise
        except Exception:
            self._stats.failed_batches += 1
            logger.exception(
                "[DoublewordProvider] poll_and_retrieve failed for batch %s",
                pending.batch_id,
            )
            return None

    # ------------------------------------------------------------------
    # Synchronous generate() — kept for backwards compatibility.
    # Combines submit_batch + poll_and_retrieve in a single blocking call.
    # ------------------------------------------------------------------

    async def generate(
        self,
        ctx: OperationContext,
        deadline: Any = None,
        *,
        prompt_override: Optional[str] = None,
    ) -> GenerationResult:
        """Generate code via Doubleword batch API (blocking).

        Parameters
        ----------
        ctx:
            OperationContext with target files and description.
        deadline:
            datetime deadline from orchestrator (used to cap poll time).
            Conforms to CandidateProvider protocol.
        prompt_override:
            Optional prompt to use instead of building from ctx.

        Returns GenerationResult with provider_used="doubleword".
        Falls through to empty result on failure (caller handles fallback).

        For non-blocking usage, prefer submit_batch() + poll_and_retrieve().
        """
        if not self.is_available:
            return GenerationResult(
                candidates=(),
                provider_name="doubleword",
                generation_duration_s=0.0,
            )

        t0 = time.monotonic()
        pending = await self.submit_batch(ctx, prompt_override=prompt_override)
        if pending is None:
            return self._empty_result(t0, "Batch submission failed")

        result = await self.poll_and_retrieve(pending, ctx)
        if result is None:
            return self._empty_result(t0, "Batch retrieval failed")
        return result

    # ------------------------------------------------------------------
    # Batch API stages (all deterministic — Tier 0 protocol)
    # ------------------------------------------------------------------

    async def _upload_file(self, jsonl_content: str) -> Optional[str]:
        """Stage 1: Upload JSONL file to Doubleword."""
        session = await self._get_session()
        import aiohttp

        data = aiohttp.FormData()
        data.add_field(
            "file",
            io.BytesIO(jsonl_content.encode()),
            filename="batch_input.jsonl",
            content_type="application/jsonl",
        )
        data.add_field("purpose", "batch")

        try:
            async with session.post(
                f"{self._base_url}/files",
                data=data,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("[DoublewordProvider] File upload failed: %s %s", resp.status, body[:500])
                    return None
                result = await resp.json()
                return result.get("id")
        except Exception:
            logger.exception("[DoublewordProvider] File upload error")
            return None

    async def _create_batch(self, input_file_id: str) -> Optional[str]:
        """Stage 2: Create batch job."""
        session = await self._get_session()
        try:
            async with session.post(
                f"{self._base_url}/batches",
                json={
                    "input_file_id": input_file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": _DW_COMPLETION_WINDOW,
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("[DoublewordProvider] Batch create failed: %s %s", resp.status, body[:500])
                    return None
                result = await resp.json()
                return result.get("id")
        except Exception:
            logger.exception("[DoublewordProvider] Batch create error")
            return None

    async def _poll_batch(self, batch_id: str) -> Optional[str]:
        """Stage 3: Poll until batch completes. Returns output_file_id or None."""
        session = await self._get_session()
        deadline = time.monotonic() + _DW_MAX_WAIT_S

        while time.monotonic() < deadline:
            try:
                async with session.get(
                    f"{self._base_url}/batches/{batch_id}",
                ) as resp:
                    if resp.status != 200:
                        logger.warning("[DoublewordProvider] Poll error: %s", resp.status)
                        await asyncio.sleep(_DW_POLL_INTERVAL_S)
                        continue
                    data = await resp.json()
                    status = data.get("status", "unknown")

                    if status == "completed":
                        output_file_id = data.get("output_file_id")
                        logger.info(
                            "[DoublewordProvider] Batch %s completed (output=%s)",
                            batch_id, output_file_id,
                        )
                        return output_file_id
                    elif status in ("failed", "expired", "cancelled"):
                        logger.error(
                            "[DoublewordProvider] Batch %s terminal: %s",
                            batch_id, status,
                        )
                        return None
                    # Still in_progress — keep polling
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[DoublewordProvider] Poll exception", exc_info=True)

            await asyncio.sleep(_DW_POLL_INTERVAL_S)

        logger.error("[DoublewordProvider] Batch %s timed out after %ds", batch_id, _DW_MAX_WAIT_S)
        return None

    async def _retrieve_result(
        self, output_file_id: str, operation_id: str
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Stage 4: Retrieve and parse batch output. Returns (content, usage)."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{self._base_url}/files/{output_file_id}/content",
            ) as resp:
                if resp.status != 200:
                    logger.error("[DoublewordProvider] Retrieve failed: %s", resp.status)
                    return ("", None)
                raw = await resp.text()

            # Parse JSONL — find the line matching our operation_id
            for line in raw.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("custom_id") == operation_id:
                        response = entry.get("response", {})
                        body = response.get("body", {})
                        choices = body.get("choices", [])
                        usage = body.get("usage")

                        if choices:
                            message = choices[0].get("message", {})
                            # Extract content, NOT reasoning_content
                            content = message.get("content", "")
                            return (content, usage)
                except json.JSONDecodeError:
                    continue

            logger.warning(
                "[DoublewordProvider] No matching result for operation_id=%s",
                operation_id,
            )
            return ("", None)

        except Exception:
            logger.exception("[DoublewordProvider] Retrieve error")
            return ("", None)

    # ------------------------------------------------------------------
    # Governance-free inference: prompt_only()
    # ------------------------------------------------------------------

    async def prompt_only(
        self,
        prompt: str,
        model: Optional[str] = None,
        caller_id: str = "ouroboros_cognition",
        response_format: Optional[Dict] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Direct inference via Doubleword batch API without OperationContext.

        Intended for cognition layers (Synthesis Engine, Architecture Agent)
        that need 397B inference without governance pipeline overhead.

        Runs the full 4-stage batch cycle synchronously (upload → create →
        poll → retrieve) and returns the raw text response.

        Parameters
        ----------
        prompt:
            User prompt text. A default system message is applied.
        model:
            Override the model slug. Defaults to self._model.
        caller_id:
            Identifier embedded in the JSONL custom_id for traceability.
        response_format:
            Optional response_format dict (e.g. ``{"type": "json_object"}``)
            passed directly to the chat completions body.
        max_tokens:
            Token cap. Defaults to self._max_tokens.

        Returns
        -------
        str
            The assistant message content from choices[0].message.content.
            Returns an empty string on failure (caller handles fallback).

        Raises
        ------
        ValueError
            If DOUBLEWORD_API_KEY is not configured.
        """
        if not self._api_key:
            raise ValueError(
                "DOUBLEWORD_API_KEY is not set — cannot call prompt_only()"
            )

        await self._get_session()

        effective_model = model or self._model
        effective_max_tokens = max_tokens if max_tokens is not None else self._max_tokens
        custom_id = f"prompt_only_{caller_id}"

        body: Dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a senior AI reasoning engine for the JARVIS Trinity "
                        "ecosystem. Think step by step and return well-structured output."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": effective_max_tokens,
            "temperature": _DW_TEMPERATURE,
        }
        if response_format is not None:
            body["response_format"] = response_format

        jsonl_line = json.dumps({
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": body,
        })

        t0 = time.monotonic()

        try:
            file_id = await self._upload_file(jsonl_line)
            if not file_id:
                logger.warning("[DoublewordProvider] prompt_only: file upload failed (caller=%s)", caller_id)
                return ""

            batch_id = await self._create_batch(file_id)
            if not batch_id:
                logger.warning("[DoublewordProvider] prompt_only: batch creation failed (caller=%s)", caller_id)
                return ""

            logger.info(
                "[DoublewordProvider] prompt_only batch %s submitted (model=%s, caller=%s)",
                batch_id, effective_model, caller_id,
            )

            output_file_id = await self._poll_batch(batch_id)
            if not output_file_id:
                self._stats.failed_batches += 1
                logger.warning(
                    "[DoublewordProvider] prompt_only: batch %s failed or timed out (caller=%s)",
                    batch_id, caller_id,
                )
                return ""

            content, usage = await self._retrieve_result(output_file_id, custom_id)

            elapsed = time.monotonic() - t0
            self._stats.total_batches += 1
            self._stats.total_latency_s += elapsed

            if usage:
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                self._stats.total_input_tokens += input_tokens
                self._stats.total_output_tokens += output_tokens
                cost = (
                    input_tokens * _DW_INPUT_COST_PER_M / 1_000_000
                    + output_tokens * _DW_OUTPUT_COST_PER_M / 1_000_000
                )
                self._stats.total_cost_usd += cost

            if not content:
                self._stats.empty_content_retries += 1
                logger.warning(
                    "[DoublewordProvider] prompt_only: empty content returned (caller=%s, batch=%s)",
                    caller_id, batch_id,
                )
                return ""

            logger.info(
                "[DoublewordProvider] prompt_only complete: %.1fs, %d chars (caller=%s)",
                elapsed, len(content), caller_id,
            )
            return content

        except asyncio.CancelledError:
            raise
        except Exception:
            self._stats.failed_batches += 1
            logger.exception(
                "[DoublewordProvider] prompt_only unexpected error (caller=%s)", caller_id
            )
            return ""

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _empty_result(self, t0: float, reason: str) -> GenerationResult:
        """Return empty GenerationResult with timing."""
        logger.debug("[DoublewordProvider] Empty result: %s", reason)
        return GenerationResult(
            candidates=(),
            provider_name="doubleword",
            generation_duration_s=time.monotonic() - t0,
        )

    async def health_probe(self) -> bool:
        """Quick health check — verify API key works and models endpoint responds."""
        if not self.is_available:
            return False
        try:
            session = await self._get_session()
            async with session.get(f"{self._base_url}/models") as resp:
                return resp.status == 200
        except Exception:
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Return cumulative stats for observability."""
        return {
            "provider": "doubleword",
            "model": self._model,
            "total_batches": self._stats.total_batches,
            "failed_batches": self._stats.failed_batches,
            "total_input_tokens": self._stats.total_input_tokens,
            "total_output_tokens": self._stats.total_output_tokens,
            "total_cost_usd": round(self._stats.total_cost_usd, 6),
            "total_latency_s": round(self._stats.total_latency_s, 1),
            "empty_content_retries": self._stats.empty_content_retries,
            "available": self.is_available,
        }

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
