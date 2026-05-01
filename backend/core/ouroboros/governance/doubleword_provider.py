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
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)
from backend.core.ouroboros.governance.stream_rupture import (
    StreamRuptureError,
    stream_inter_chunk_timeout_s as _stream_inter_chunk_timeout_s,
    stream_rupture_timeout_s as _stream_rupture_timeout_s,
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
_DW_MAX_TOKENS = int(os.environ.get("DOUBLEWORD_MAX_TOKENS", "16384"))

# Complexity-aware max_tokens: lower ceilings for simpler tasks make DW
# respond faster (fewer tokens to generate) without sacrificing quality.
# Trivial one-liner fixes don't need 16K output tokens.
#
# Why 8192 for trivial (not 4096):
# bt-2026-04-10-091829/debug.log:677 showed DW's STANDARD-demotion path
# emitting a 7401-char JSON response that truncated mid-string for a
# "trivial" requirements.txt op (Python 3.9→3.11 upgrade). The response
# exceeded the 4096-token cap because the generated content was larger
# than the 3518-byte source file (adding packages). A 4096 ceiling gives
# roughly ~7400 chars of JSON-escaped output — exactly where it broke.
# 8192 gives ~14.5KB headroom at ~+$0.002/op — strictly better than
# truncated responses that cost the full op + retry.
_DW_COMPLEXITY_MAX_TOKENS: Dict[str, int] = {
    "trivial": 8192,
    "moderate": 8192,
    "standard": 12288,
    "complex": 16384,
    "heavy_code": 16384,
}

# Dynamic max_tokens constants — parallel to _CLAUDE_OUTPUT_* in providers.py.
# Task #187 added dynamic output budgets to Claude but not DW, so a
# "trivial" task targeting a 7KB requirements.txt was truncated at the
# 4096 ceiling — bt-2026-04-10-091829 debug.log:677 showed DW streaming
# ~4203 chars of valid ASCII content before JSON parse failed with
# "Unterminated string". The formula: needed = (bytes/CHARS_PER_TOKEN) *
# SAFETY + OVERHEAD, floored at the complexity ceiling, capped at
# _DW_MAX_TOKENS. Small files keep the cheap complexity ceiling; large
# files get a proportionally bigger budget so full-file rewrites never
# truncate.
_DW_CHARS_PER_TOKEN = 3.5
_DW_OUTPUT_SAFETY = 1.4
_DW_OUTPUT_OVERHEAD_TOKENS = 2048  # JSON schema wrapper + rationale + slack
_DW_POLL_INTERVAL_S = float(os.environ.get("DOUBLEWORD_POLL_INTERVAL_S", "5"))
_DW_MAX_WAIT_S = float(os.environ.get("DOUBLEWORD_MAX_WAIT_S", "3600"))
_DW_TEMPERATURE = float(os.environ.get("DOUBLEWORD_TEMPERATURE", "0.2"))
_DW_CONNECT_TIMEOUT_S = float(os.environ.get("DOUBLEWORD_CONNECT_TIMEOUT_S", "10"))
_DW_REQUEST_TIMEOUT_S = float(os.environ.get("DOUBLEWORD_REQUEST_TIMEOUT_S", "120"))

# Pricing (March 2026)
_DW_INPUT_COST_PER_M = float(os.environ.get("DOUBLEWORD_INPUT_COST_PER_M", "0.10"))
_DW_OUTPUT_COST_PER_M = float(os.environ.get("DOUBLEWORD_OUTPUT_COST_PER_M", "0.40"))
_DW_MAX_COST_PER_OP = float(os.environ.get("DOUBLEWORD_MAX_COST_PER_OP", "0.10"))
_DW_DAILY_BUDGET = float(os.environ.get("DOUBLEWORD_DAILY_BUDGET", "5.00"))


class DoublewordInfraError(Exception):
    """Infrastructure failure from DoublewordProvider.

    Propagated to the CandidateGenerator's FailbackStateMachine so it can
    classify the failure mode (rate limit vs timeout vs connection error)
    and predict recovery timing.  The ``status_code`` field carries the
    HTTP status (429, 500, etc.) or 0 for non-HTTP failures.

    Phase 12 Slice F — Substrate Error Unmasking (operator-mandated
    2026-04-27): added ``response_body`` and ``model_id`` so the
    sentinel + classifier can distinguish 4xx modality errors (NON_CHAT
    models silently slotted into generative routes) from 5xx transport
    errors (genuine endpoint instability) without regex-matching on the
    string repr.

    Failure-class taxonomy carried structurally:
      * ``status_code in (400, 404, 422)`` AND modality body markers
        → terminal/modality error, model permanently excluded by
        Slice H breaker until next catalog refresh
      * ``status_code in (429, 503)`` → rate limit / overload, retry
      * ``status_code in (500, 502, 504)`` → transient transport
      * ``status_code == 401`` / ``403`` → auth failure, terminal
      * ``status_code == 0`` → non-HTTP (DNS/TLS/timeout)
    """

    def __init__(
        self,
        reason: str,
        status_code: int = 0,
        *,
        response_body: str = "",
        model_id: str = "",
    ) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.response_body = (response_body or "")[:1024]  # bounded
        self.model_id = (model_id or "")[:128]

    def is_modality_error(self) -> bool:
        """True iff the response indicates the model can't accept
        ``/chat/completions`` payloads. Used by Slice H breaker to
        decide TERMINAL_OPEN vs transient.

        Heuristic on KNOWN-AT-RUNTIME signals (NOT regex on model id):
          * status_code in {400, 404, 422} — bad request / not found /
            unprocessable entity (classic OpenAI-compat modality 4xx)
          * AND response_body contains a modality marker the DW server
            actually emits
        Both required: a 400 about a bad max_tokens is NOT modality.
        """
        if self.status_code not in (400, 404, 422):
            return False
        body_lower = (self.response_body or "").lower()
        # These markers are observed in DW + OpenAI-compat error
        # responses for modality-mismatched calls. Matched on the
        # SERVER's response body (which is ground truth from DW),
        # NOT on our local model_id string. If DW returns a body
        # without these markers, we conservatively treat it as
        # transient — we don't infer modality from absence.
        markers = (
            "does not support chat",
            "not a chat model",
            "endpoint not supported",
            "embedding only",
            "model_not_chat",
            "task mismatch",
            "wrong endpoint",
            "unsupported endpoint",
            "model is not available for chat",
        )
        return any(m in body_lower for m in markers)

    def is_terminal_auth_error(self) -> bool:
        """401/403 → permanent auth failure for this model_id."""
        return self.status_code in (401, 403)

    def is_transient(self) -> bool:
        """5xx + 429 → transient; should retry per backoff schedule."""
        if self.status_code in (429, 503, 500, 502, 504):
            return True
        # Non-HTTP failures (status_code == 0) — DNS/TLS/timeout —
        # treated as transient unless the reason text indicates
        # something terminal. Conservative: assume transient.
        return self.status_code == 0


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


@dataclass
class CompleteSyncResult:
    """Result of a non-streaming complete_sync() call.

    Functions-not-Agents path: structured return for short, bounded,
    schema-validated function callers (CompactionCaller, BlastRadius,
    FailureClustering, DreamSeed). Never used by the agent cascade —
    agent-shaped workloads go through generate()/Venom/SSE.
    """
    content: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    model: str


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
        rate_limiter: Optional[Any] = None,
        max_cost_per_op: float = _DW_MAX_COST_PER_OP,
        daily_budget: float = _DW_DAILY_BUDGET,
        tool_loop: Optional[Any] = None,
        realtime_enabled: bool = True,
        batch_registry: Optional[Any] = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._repo_root = repo_root or Path(".")
        self._repo_roots = repo_roots or {}
        # Phase 1 Step 3B — state hoist. aiohttp session, cumulative
        # stats, spend tracking, last-error-status, and stream activity
        # timestamps all live on a ``DoubleWordProviderState`` routed
        # through the process-lifetime singleton under
        # ``JARVIS_UNQUARANTINE_PROVIDERS=true``. The legacy path mints
        # a fresh state per instance — behavior is bit-for-bit
        # identical to the pre-hoist version.
        from ._governance_state import (
            DoubleWordProviderState,
            get_doubleword_provider_state,
            unquarantine_providers_enabled,
        )
        if unquarantine_providers_enabled():
            self._state = get_doubleword_provider_state()
        else:
            self._state = DoubleWordProviderState.fresh()
        # ``_stats`` is mutated in place (``self._stats.total_batches += 1``)
        # and never rebound, so an alias onto the state dataclass is
        # alias-safe — no property indirection needed.
        self._stats = self._state.stats
        self._rate_limiter = rate_limiter
        self._tool_loop = tool_loop
        self._batch_registry = batch_registry
        # Real-time mode uses /v1/chat/completions with SSE streaming —
        # zero polling, token-by-token output, Venom tool loop support.
        # Battle testing shows batch (16-22s) and real-time (20-40s) have
        # comparable latency, but real-time enables streaming + eliminates
        # the polling loop (Manifesto §3: Zero polling. Pure reflex.).
        # Default: ON. Opt out via DOUBLEWORD_REALTIME_ENABLED=false.
        self._realtime_enabled = (
            realtime_enabled
            and os.environ.get("DOUBLEWORD_REALTIME_ENABLED", "true").lower() != "false"
        )
        # Cost gating (matches ClaudeProvider pattern)
        self._max_cost_per_op = max_cost_per_op
        self._daily_budget = daily_budget
        self._mcp_client: Optional[Any] = None  # Injected by GLS for MCP tool forwarding (Gap #7)

    def _resolve_effective_model(self, ctx: Any) -> str:
        """Resolve the DW model for this call.

        Resolution order (first match wins):

          1. ``topology_sentinel.DW_MODEL_OVERRIDE_VAR`` — per-attempt
             override set by the AsyncTopologySentinel-driven dispatch
             in ``candidate_generator`` (Phase 10 P10.3+P10.3.6).
             When the sentinel is walking a route's ranked
             ``dw_models`` list, each attempt sets this ContextVar
             via ``set_dw_model_override(model_id)``; this method
             reads it via ``get_dw_model_override()``. ContextVar is
             async-safe per asyncio task, so concurrent ops can each
             have their own value without leaking. Replaces the
             Slice 3 ``setattr(ctx, "_dw_model_override", ...)``
             pattern, which raised ``FrozenInstanceError`` on the
             frozen ``OperationContext`` dataclass and silently
             defeated the dispatcher.
          2. ``topology.model_for_route(route)`` — v1 single-model
             per-route mapping. Honored when no per-attempt override
             is set (legacy path; default behavior when sentinel is
             disabled).
          3. ``self._model`` — instance default (env-configured).

        Falls back to ``self._model`` when the topology is disabled,
        the route is unmapped, or the ctx lacks a ``provider_route``
        attribute — identical to the pre-topology behavior.

        NEVER raises — every layer is defensive.
        """
        # (1) Per-attempt override from sentinel-driven dispatch via
        # ContextVar (async-safe; survives the frozen-ctx contract).
        try:
            from backend.core.ouroboros.governance.topology_sentinel import (
                get_dw_model_override,
            )
            attempt_override = get_dw_model_override()
            if isinstance(attempt_override, str) and attempt_override:
                return attempt_override
        except Exception:  # noqa: BLE001 — defensive
            # Sentinel module not importable (test environment, branch
            # without Slice 1) → silently fall through to legacy.
            pass
        # (2) v1 route → model mapping.
        route = getattr(ctx, "provider_route", "") or ""
        if not route:
            return self._model
        try:
            from backend.core.ouroboros.governance.provider_topology import (
                get_topology,
            )
        except Exception:
            return self._model
        override = get_topology().model_for_route(route)
        return override or self._model

    # ------------------------------------------------------------------
    # Hoisted state accessors (Phase 1 Step 3B)
    # ------------------------------------------------------------------
    # Every rebound field on ``DoubleWordProviderState`` gets paired
    # getter/setter descriptors so assignments like
    # ``self._session = aiohttp.ClientSession(...)`` on reload-surviving
    # instances can't plant a real instance attribute and drift from
    # ``self._state.session``.

    @property
    def _session(self) -> Any:
        return self._state.session

    @_session.setter
    def _session(self, value: Any) -> None:
        self._state.session = value

    @property
    def _daily_spend(self) -> float:
        return self._state.counters.daily_spend

    @_daily_spend.setter
    def _daily_spend(self, value: float) -> None:
        self._state.counters.daily_spend = value

    @property
    def _budget_reset_date(self) -> str:
        return self._state.counters.budget_reset_date

    @_budget_reset_date.setter
    def _budget_reset_date(self, value: str) -> None:
        self._state.counters.budget_reset_date = value

    @property
    def _last_error_status(self) -> int:
        return self._state.counters.last_error_status

    @_last_error_status.setter
    def _last_error_status(self, value: int) -> None:
        self._state.counters.last_error_status = value

    @property
    def _last_chunk_at(self) -> float:
        return self._state.counters.last_chunk_at

    @_last_chunk_at.setter
    def _last_chunk_at(self, value: float) -> None:
        self._state.counters.last_chunk_at = value

    def _record_ttft_safely(
        self,
        *,
        model_id: str,
        ttft_ms: int,
        op_id: str = "",
    ) -> None:
        """Phase 12.2 Slice C — feed first-chunk latency into:

          1. ``TtftObserver`` (rolling stats for promotion + cold-
             storage gates) — only when tracking_enabled() is true.
          2. ``PromotionLedger`` ``record_success`` (legacy count gate
             keep-alive so master-flag-off path stays bit-for-bit
             unchanged from Phase 12 Slice B).

        NEVER raises. All faults swallowed at this seam — a broken
        observer or ledger must NEVER take down the SSE stream.
        Singleton lookup is lazy (deferred until first call) so
        master-flag-off + tracking-off → zero observer instantiated."""
        if not model_id or ttft_ms < 0:
            return
        # Observer feed (TTFT mode)
        try:
            from backend.core.ouroboros.governance.dw_discovery_runner import (
                get_ttft_observer,
            )
            obs = get_ttft_observer()
            if obs is not None:
                obs.record_ttft(model_id, ttft_ms, op_id=op_id)
        except Exception:  # noqa: BLE001 — defensive
            pass
        # Ledger feed (legacy count gate keep-alive). The ledger's
        # auto-register-on-first-success path means we don't need to
        # know whether the model was previously quarantined — the
        # ledger handles it.
        try:
            from backend.core.ouroboros.governance.dw_discovery_runner import (
                _get_or_create_ledger,
            )
            led = _get_or_create_ledger()
            led.record_success(model_id, ttft_ms)
        except Exception:  # noqa: BLE001 — defensive
            pass

    @property
    def provider_name(self) -> str:
        """Human-readable name for CandidateProvider protocol."""
        return "doubleword-397b"

    @property
    def is_available(self) -> bool:
        """Check if Doubleword is configured."""
        return bool(self._api_key)

    async def _get_session(self) -> Any:
        """Lazy-init persistent aiohttp session.

        NOTE: Content-Type is NOT set at session level. The session default
        ``application/json`` was overriding the multipart boundary generated
        by aiohttp.FormData during file uploads, causing Doubleword to reject
        the request with "Invalid boundary for multipart/form-data".
        Each request sets its own Content-Type as needed.
        """
        _needs_new = (
            self._session is None
            or self._session.closed
            # aiohttp connector can be poisoned by CancelledError during
            # connection attempts.  session.closed doesn't always reflect
            # this, so check the connector directly.
            or getattr(self._session.connector, "_closed", False)
        )
        if _needs_new:
            import aiohttp
            # Close the old session cleanly if it exists
            if self._session is not None and not self._session.closed:
                try:
                    await self._session.close()
                except Exception:
                    pass
            # CRITICAL: aiohttp 3.9+ requires ClientSession to be created
            # inside a running event loop task. The default timeout parameter
            # triggers "Timeout context manager should be used inside a task".
            # Solution: create with connector only, no timeout object at all.
            # Per-request timeouts are applied via _request_timeout() instead.
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._api_key}"},
                connector=connector,
                trust_env=True,  # honour HTTP_PROXY / HTTPS_PROXY env vars
            )
        return self._session

    @staticmethod
    def _request_timeout() -> "aiohttp.ClientTimeout":
        """Per-request timeout safe to use inside aiohttp 3.9+ tasks."""
        import aiohttp
        return aiohttp.ClientTimeout(
            total=_DW_REQUEST_TIMEOUT_S,
            connect=_DW_CONNECT_TIMEOUT_S,
        )

    # ------------------------------------------------------------------
    # Cost gating (matches ClaudeProvider pattern)
    # ------------------------------------------------------------------

    def _maybe_reset_daily_budget(self) -> None:
        """Reset daily spend if the UTC day has changed."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today > self._budget_reset_date:
            self._daily_spend = 0.0
            self._budget_reset_date = today

    def _check_budget(self) -> None:
        """Raise if daily budget is exhausted. Called before each generation."""
        self._maybe_reset_daily_budget()
        if self._daily_spend >= self._daily_budget:
            raise DoublewordInfraError(
                f"doubleword_budget_exhausted: daily spend ${self._daily_spend:.4f} "
                f">= budget ${self._daily_budget:.2f}",
                status_code=0,
            )

    def _record_cost(self, cost: float) -> None:
        """Record cost from a completed batch and check per-op limit."""
        self._daily_spend += cost
        if cost > self._max_cost_per_op:
            logger.warning(
                "[DoublewordProvider] Op cost $%.4f exceeds max_cost_per_op $%.2f",
                cost, self._max_cost_per_op,
            )

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
        self._check_budget()

        from backend.core.ouroboros.governance.providers import _build_codegen_prompt
        # Always use full_content schema (2b.1) — the 397B can't reliably
        # produce verbatim context lines for unified diffs (2b.1-diff).
        prompt = prompt_override or _build_codegen_prompt(
            ctx,
            repo_root=self._repo_root,
            repo_roots=self._repo_roots or None,
            force_full_content=True,
            provider_route=getattr(ctx, "provider_route", "") or "",
        )
        operation_id = getattr(ctx, "operation_id", f"dw-{int(time.time())}")
        _effective_model = self._resolve_effective_model(ctx)

        jsonl_line = json.dumps({
            "custom_id": operation_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": _effective_model,
                "messages": [
                    {"role": "system", "content": (
                        "You are a code generation assistant. RESPOND WITH ONLY A SINGLE VALID JSON OBJECT. "
                        "RULES: "
                        "1. Start your response with { and end with }. "
                        "2. No text before or after the JSON. No markdown fences. No explanations. "
                        "3. All string values must use double quotes. Escape special characters: use \\n for newlines, \\t for tabs, \\\\ for backslashes. "
                        "4. No trailing commas before } or ]. "
                        "5. Use schema_version '2b.1' with full_content containing the COMPLETE file. "
                        "6. NEVER return unified diffs, patches, or partial file content. "
                        "7. CRITICAL: Every candidate MUST include a non-empty 'rationale' field "
                        "(1 sentence, max 200 chars). Missing rationale will be rejected."
                    )},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": self._max_tokens,
                "temperature": _DW_TEMPERATURE,
                # Qwen3.5 reasoning models: disable thinking mode so output
                # goes to 'content' field instead of being consumed by internal
                # reasoning. Without this, content is empty (all tokens used for thinking).
                "chat_template_kwargs": {"enable_thinking": False},
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
                batch_id, _effective_model, operation_id,
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
            output_file_id = await self._await_batch_result(pending.batch_id)
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

            _batch_cost = 0.0
            if usage:
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                self._stats.total_input_tokens += input_tokens
                self._stats.total_output_tokens += output_tokens
                _batch_cost = (
                    input_tokens * _DW_INPUT_COST_PER_M / 1_000_000
                    + output_tokens * _DW_OUTPUT_COST_PER_M / 1_000_000
                )
                self._stats.total_cost_usd += _batch_cost
                self._record_cost(_batch_cost)

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
                _file_source_hash,
            )
            # Source hash: full SHA-256 of target file content (matches
            # _check_source_drift at GATE).  Old code hashed prompt[:500]
            # which guaranteed false-positive drift for every DW candidate.
            _src_hash = ""
            _src_path_str = ""
            if ctx.target_files:
                _src_path_str = ctx.target_files[0]
                _abs = (self._repo_root / _src_path_str).resolve()
                try:
                    if _abs.is_file():
                        _src_hash = _file_source_hash(
                            _abs.read_text(encoding="utf-8", errors="replace")
                        )
                except OSError:
                    pass

            # Log raw response preview for debugging parse failures
            _preview = content[:200].replace("\n", "\\n") if content else "(empty)"
            logger.info(
                "[DoublewordProvider] Batch %s response preview (%d chars): %s",
                pending.batch_id, len(content), _preview,
            )

            # Auto-fix: if the 397B returned natural language instead of JSON,
            # try to extract any JSON block that might be embedded deeper in the
            # response. If truly no JSON exists, _parse_generation_response will
            # raise and the caller handles the failure.
            from backend.core.ouroboros.governance.providers import _extract_json_block
            _extracted = _extract_json_block(content)
            if _extracted and not _extracted.lstrip().startswith("{"):
                logger.warning(
                    "[DoublewordProvider] 397B returned natural language instead of JSON "
                    "(batch %s). Response starts with: %s",
                    pending.batch_id, _extracted[:100].replace("\n", " "),
                )
                # Return None — caller treats as "no candidates" and retries
                return None

            result = _parse_generation_response(
                raw=content,
                provider_name="doubleword",
                duration_s=elapsed,
                ctx=ctx,
                source_hash=_src_hash,
                source_path=_src_path_str,
                repo_roots=self._repo_roots or None,
                repo_root=self._repo_root,
            )
            # Attach token usage and cost from batch
            if usage or _batch_cost > 0:
                result = dataclasses.replace(
                    result,
                    total_input_tokens=input_tokens,
                    total_output_tokens=output_tokens,
                    cost_usd=_batch_cost,
                )
            return result

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stats.failed_batches += 1
            # Log the raw response for debugging parse failures
            logger.warning(
                "[DoublewordProvider] poll_and_retrieve failed for batch %s: %s. "
                "Raw response first 300 chars: %s",
                pending.batch_id,
                exc,
                content[:300].replace("\n", "\\n") if content else "(no content)",
            )
            return None

    # ------------------------------------------------------------------
    # Dynamic output-token budget (parallel to ClaudeProvider's
    # _compute_output_budget). Used by both the batch path and the
    # real-time SSE path so a "trivial" complexity task that happens
    # to target a large file still gets enough tokens to fit the full
    # rewrite in one response. Falls back to the complexity ceiling
    # when target files can't be resolved (new file / bad path).
    # ------------------------------------------------------------------

    def _compute_dynamic_max_tokens(
        self,
        context: Any,
        *,
        is_tool_round: bool = False,
    ) -> int:
        """Compute max_tokens for a DW generation call.

        Always starts from the complexity-derived ceiling, then scales *up*
        by the actual target file size so full-file rewrites don't truncate
        mid-string.

        Formula:
            raw = total_bytes / _DW_CHARS_PER_TOKEN
            needed = int(raw * _DW_OUTPUT_SAFETY) + _DW_OUTPUT_OVERHEAD_TOKENS
            result = max(needed, complexity_ceiling)
            result = min(result, _DW_MAX_TOKENS)

        Parameters
        ----------
        context:
            The current ``OperationContext``. Expected attributes:
            ``task_complexity`` (str) and ``target_files`` (seq of rel paths).
        is_tool_round:
            Advisory only — no longer affects the budget. The flag is set
            *before* the call based on ``round_index > 0``, but the model
            decides per-response whether to emit a short tool-call JSON or
            the final ``full_content`` candidate. Capping at 1024 on
            ``round > 0`` truncated the terminal round's patch mid-string
            (battle test bt-2026-04-11-065233). DW bills on actual output
            tokens, so a generous cap on every round costs nothing when the
            model naturally stops short on an intermediate tool-call round.
        """
        del is_tool_round  # advisory only — see docstring
        complexity = getattr(context, "task_complexity", "") or ""
        complexity_ceiling = _DW_COMPLEXITY_MAX_TOKENS.get(
            complexity, self._max_tokens,
        )

        # Resolve target files → total bytes. New files (non-existent
        # paths) contribute 0. Multi-file rewrites sum all bytes so the
        # budget covers the whole candidate array.
        target_files = getattr(context, "target_files", ()) or ()
        total_bytes = 0
        resolved = 0
        for rel in target_files:
            if not rel:
                continue
            try:
                abs_path = (self._repo_root / str(rel)).resolve()
                if abs_path.exists() and abs_path.is_file():
                    total_bytes += abs_path.stat().st_size
                    resolved += 1
            except (OSError, ValueError):
                continue

        if resolved == 0 or total_bytes == 0:
            # No file-size data — keep the complexity ceiling as-is.
            return min(int(complexity_ceiling), _DW_MAX_TOKENS)

        # Scale proportionally: bytes → tokens → safety margin → overhead.
        raw_tokens = total_bytes / _DW_CHARS_PER_TOKEN
        needed = int(raw_tokens * _DW_OUTPUT_SAFETY) + _DW_OUTPUT_OVERHEAD_TOKENS
        # Never squeeze below the complexity ceiling — dynamic budget is
        # strictly a floor-raiser, never a ceiling-lowerer.
        needed = max(needed, int(complexity_ceiling))
        return min(needed, _DW_MAX_TOKENS)

    # ------------------------------------------------------------------
    # Synchronous generate() — kept for backwards compatibility.
    # Combines submit_batch + poll_and_retrieve in a single blocking call.
    # ------------------------------------------------------------------

    async def generate(
        self,
        context: OperationContext,
        deadline: Any = None,
        *,
        prompt_override: Optional[str] = None,
    ) -> GenerationResult:
        """Generate code via Doubleword batch API (blocking).

        Parameters
        ----------
        context:
            OperationContext with target files and description.
        deadline:
            datetime deadline from orchestrator (used to cap poll time).
            Conforms to CandidateProvider protocol.
        prompt_override:
            Optional prompt to use instead of building from context.

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

        # Real-time mode: /v1/chat/completions with SSE streaming + Venom tool loop
        # On 429/503, fall back to batch within DW (stay cheap) instead of
        # cascading to the 150x more expensive Claude fallback.
        if self._realtime_enabled:
            try:
                return await self._generate_realtime(context, deadline, prompt_override=prompt_override)
            except DoublewordInfraError as exc:
                if exc.status_code in (429, 503):
                    logger.info(
                        "[DoublewordProvider] Real-time returned %d, falling back to batch",
                        exc.status_code,
                    )
                    # Fall through to batch mode below
                else:
                    raise  # Non-retriable: propagate to CandidateGenerator FSM

        # Batch mode: 4-stage async batch API (fallback from real-time, or explicit opt-in)
        t0 = time.monotonic()
        self._last_error_status = 0  # reset before attempt

        pending = await self.submit_batch(context, prompt_override=prompt_override)
        if pending is None:
            raise DoublewordInfraError(
                "Batch submission failed", status_code=self._last_error_status,
            )

        result = await self.poll_and_retrieve(pending, context)
        if result is None:
            raise DoublewordInfraError(
                "Batch retrieval failed", status_code=self._last_error_status,
            )
        return result

    # ------------------------------------------------------------------
    # Real-time generation via /v1/chat/completions (Venom-compatible)
    # ------------------------------------------------------------------

    async def _generate_realtime(
        self,
        context: OperationContext,
        deadline: Any = None,
        *,
        prompt_override: Optional[str] = None,
    ) -> GenerationResult:
        """Generate code via DoubleWord real-time chat completions API.

        Uses ``/v1/chat/completions`` (OpenAI-compatible) instead of the
        batch API.  This enables the Venom tool loop: the provider can
        call read_file, search_code, run_tests, bash, etc. during
        generation — the same multi-turn agentic loop that ClaudeProvider
        supports.

        30-37x cheaper than Claude with the same tool-use capability.
        """
        from backend.core.ouroboros.governance.providers import (
            _build_codegen_prompt,
            _build_lean_codegen_prompt,
            _should_use_lean_prompt,
            _parse_generation_response,
        )
        from datetime import datetime, timezone

        self._check_budget()
        t0 = time.monotonic()
        total_cost = 0.0
        self._last_chunk_at = 0.0  # reset — prevents stale timestamps from prior generation

        # Gap #7: discover MCP tools for prompt injection
        _mcp_tools = None
        if self._mcp_client is not None:
            try:
                _mcp_tools = await self._mcp_client.discover_tools()
            except Exception:
                pass

        # P0.1: Lean tool-first prompt — 60-70% smaller than the full prompt.
        # When Venom tool loop is available, send a minimal instruction and let
        # the model pull context incrementally via read_file/search_code/etc.
        # Manifesto §5: "Agentic intelligence handles the 5% that is novel."
        _complexity = getattr(context, "task_complexity", "")
        _will_skip_tools = _complexity in ("trivial", "simple")
        _tools_available = self._tool_loop is not None and not _will_skip_tools
        _preloaded_files: List[str] = []
        if prompt_override:
            prompt = prompt_override
        elif _should_use_lean_prompt(context, tools_enabled=_tools_available):
            prompt = _build_lean_codegen_prompt(
                context,
                repo_root=self._repo_root,
                repo_roots=self._repo_roots or None,
                force_full_content=True,
                mcp_tools=_mcp_tools,
                preloaded_out=_preloaded_files,
            )
            logger.info(
                "[DoublewordProvider] RT: using lean prompt (%d chars, ~%d tokens, preloaded=%d)",
                len(prompt), len(prompt) // 4, len(_preloaded_files),
            )
        else:
            prompt = _build_codegen_prompt(
                context,
                repo_root=self._repo_root,
                repo_roots=self._repo_roots or None,
                force_full_content=True,
                mcp_tools=_mcp_tools,
                provider_route=getattr(context, "provider_route", "") or "",
            )
            logger.info(
                "[DoublewordProvider] RT: using full prompt (%d chars, ~%d tokens, route=%s)",
                len(prompt), len(prompt) // 4,
                getattr(context, "provider_route", "") or "unknown",
            )

        _SYSTEM_PROMPT = (
            "You are a code generation assistant for the JARVIS Trinity AI Ecosystem. "
            "RESPOND WITH ONLY A SINGLE VALID JSON OBJECT. "
            "RULES: "
            "1. Start your response with { and end with }. "
            "2. No text before or after the JSON. No markdown fences. No explanations. "
            "3. All string values must use double quotes. Escape special characters: "
            "use \\n for newlines, \\t for tabs, \\\\ for backslashes. "
            "4. No trailing commas before } or ]. "
            "5. Use schema_version '2b.1' with full_content containing the COMPLETE file. "
            "6. NEVER return unified diffs, patches, or partial file content. "
            "7. CRITICAL: Every candidate MUST include a non-empty 'rationale' field "
            "(1 sentence, max 200 chars) explaining WHY the change is being made. "
            "Missing or empty rationale will cause the response to be rejected."
        )

        # Mutable container to capture token usage from _generate_raw
        _token_usage: Dict[str, int] = {"input": 0, "output": 0}

        # Resolve effective model once via topology — routes map to
        # distinct DW models under the Brain Selection Topology
        # (STANDARD→397B, BACKGROUND/SPECULATIVE→Gemma 4 31B). The lookup
        # is pure yaml-driven, no env overrides. Hard-blocked routes
        # (IMMEDIATE + COMPLEX) never reach this method.
        _effective_model = self._resolve_effective_model(context)
        if _effective_model != self._model:
            logger.info(
                "[DoublewordProvider] RT: topology override model=%s "
                "(default=%s, route=%s)",
                _effective_model, self._model,
                getattr(context, "provider_route", "?"),
            )

        async def _generate_raw(p: str) -> str:
            """Single chat completion call (used by tool_loop.run())."""
            nonlocal total_cost
            session = await self._get_session()

            # Dynamic max_tokens: complexity-aware ceiling as the floor,
            # scaled up by actual target file bytes so full-file rewrites
            # don't truncate mid-string. Tool rounds get a small fixed
            # budget (tool call JSON is ~1K tokens). See
            # _compute_dynamic_max_tokens for the formula.
            _is_tool_round = (
                self._tool_loop is not None
                and getattr(self._tool_loop, "is_tool_round", False)
            )
            _eff_max_tokens = self._compute_dynamic_max_tokens(
                context, is_tool_round=_is_tool_round,
            )

            # Streaming callback for token-by-token TUI output
            _stream_callback = None
            if self._tool_loop is not None:
                _stream_callback = getattr(self._tool_loop, "on_token", None)

            # Multi-modal user content — when ctx.attachments is non-empty and
            # the GENERATE purpose gate is open, splice OpenAI-compatible
            # image_url blocks alongside the text prompt. Manifesto §1:
            # Tri-Partite Microkernel — Mind perceives what Senses captured.
            # Lazy import avoids providers.py ↔ doubleword_provider.py cycle.
            from backend.core.ouroboros.governance.providers import (
                _serialize_attachments as _dw_serialize_attachments,
            )
            _att_blocks = _dw_serialize_attachments(
                context, provider_kind="doubleword", purpose="generate",
            )
            if _att_blocks:
                _user_content: Any = [{"type": "text", "text": p}, *_att_blocks]
                _atts = getattr(context, "attachments", ())
                _kinds = ",".join(sorted({a.kind for a in _atts})) or "-"
                _mimes = ",".join(sorted({a.mime_type for a in _atts})) or "-"
                _hashes = ",".join(a.hash8 for a in _atts) or "-"
                _bytes = 0
                for _a in _atts:
                    try:
                        _bytes += os.path.getsize(_a.image_path)
                    except OSError:
                        pass
                logger.info(
                    "[DoublewordProvider] multi_modal op=%s blocks=%d "
                    "attachments=%d bytes=%d kinds=[%s] mime_kinds=[%s] "
                    "hash8s=[%s] route=%s purpose=generate",
                    getattr(context, "operation_id", "-"),
                    len(_att_blocks), len(_atts), _bytes, _kinds, _mimes, _hashes,
                    (getattr(context, "provider_route", "") or "-"),
                )
            else:
                _user_content = p

            body = {
                "model": _effective_model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _user_content},
                ],
                "max_tokens": _eff_max_tokens,
                "temperature": _DW_TEMPERATURE,
                "chat_template_kwargs": {"enable_thinking": False},
            }

            if _stream_callback is not None:
                # Streaming path: SSE for token-by-token output.
                # Use a generous per-chunk timeout (30s between chunks)
                # to detect stalled streams without killing slow generation.
                body["stream"] = True
                content = ""
                input_tokens = 0
                output_tokens = 0
                _PER_CHUNK_TIMEOUT = 30.0  # seconds between SSE chunks

                # Priority 1 Slice 1 — confidence capture (PRD §26.5.1).
                # Master-flag-gated; when enabled, request OpenAI-compat
                # per-token logprobs from the provider so the streaming
                # parse below can capture the top-1/top-2 margin signal
                # into ctx.artifacts["confidence_capturer"]. Capture is
                # purely additive on the response; the request shape
                # only changes when the flag is on (byte-for-byte
                # preserved when off).
                from backend.core.ouroboros.governance.verification.confidence_capture import (
                    ConfidenceCapturer,
                    confidence_capture_enabled,
                    confidence_capture_top_k,
                    extract_openai_compat_logprobs_from_chunk,
                )
                # Priority 1 Slice 2 — confidence monitor + circuit-breaker.
                # When ENABLED, the monitor consumes the per-token margin
                # signal alongside the capturer and produces a verdict;
                # ENFORCE sub-flag governs whether BELOW_FLOOR raises
                # ConfidenceCollapseError mid-stream. Slice 2 ships
                # SHADOW only — both flags default false; ENFORCE flips
                # in Slice 5 graduation.
                from backend.core.ouroboros.governance.verification.confidence_monitor import (
                    ConfidenceMonitor,
                    ConfidenceVerdict,
                    confidence_monitor_enabled,
                    confidence_monitor_enforce,
                )
                _confidence_capturer: Optional[ConfidenceCapturer] = None
                _confidence_monitor: Optional[ConfidenceMonitor] = None
                _monitor_enforce_active: bool = False
                if confidence_capture_enabled():
                    body["logprobs"] = True
                    body["top_logprobs"] = confidence_capture_top_k()
                    _confidence_capturer = ConfidenceCapturer(
                        provider="doubleword",
                        model_id=str(_effective_model or ""),
                    )
                    # Slice 2 monitor wakes only when its own master flag
                    # is on. Capture without monitor remains valid (Slice 1
                    # observation-only mode for ledger/replay use).
                    if confidence_monitor_enabled():
                        _confidence_monitor = ConfidenceMonitor(
                            provider="doubleword",
                            model_id=str(_effective_model or ""),
                            op_id=str(
                                getattr(context, "op_id", "") or "",
                            ),
                        )
                        _monitor_enforce_active = (
                            confidence_monitor_enforce()
                        )
                    # Stash on ctx.artifacts so downstream phase runners
                    # (Slice 2 monitor) can read the trace post-stream.
                    try:
                        _artifacts = getattr(context, "artifacts", None)
                        if isinstance(_artifacts, dict):
                            _artifacts["confidence_capturer"] = (
                                _confidence_capturer
                            )
                            if _confidence_monitor is not None:
                                _artifacts["confidence_monitor"] = (
                                    _confidence_monitor
                                )
                    except Exception:  # noqa: BLE001 — capture must
                        pass        # never break the stream loop
                # Phase 12.2 Slice C — TTFT measurement window opens
                # the moment we issue the request and closes on first
                # non-empty content chunk. monotonic() is jump-proof
                # under wall-clock corrections.
                _ttft_request_start_monotonic = time.monotonic()
                _ttft_first_chunk_seen = False

                async with session.post(
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=self._request_timeout(),
                ) as resp:
                    if resp.status >= 300:
                        self._last_error_status = resp.status
                        err_body = await resp.text()
                        # Phase 12 Slice F — Substrate Error Unmasking.
                        # Preserve full response body + model_id so
                        # downstream classifier can distinguish modality
                        # 4xx from transient 5xx without regex on str(exc).
                        raise DoublewordInfraError(
                            f"Chat completions (stream) failed: "
                            f"{resp.status} {err_body[:200]}",
                            status_code=resp.status,
                            response_body=err_body,
                            model_id=_effective_model,
                        )

                    # Two-Phase Stream Rupture Breaker.
                    # Phase 1 (TTFT): generous timeout for first token.
                    # Phase 2 (Inter-Chunk): tight timeout once streaming.
                    _rupture_ttft = _stream_rupture_timeout_s()
                    _rupture_ic = _stream_inter_chunk_timeout_s()
                    _chunk_phase_timeout = _rupture_ttft  # Phase 1
                    _sse_has_tokens = False
                    # Phase-Aware Heartbeat — pulse the harness
                    # ActivityMonitor every Nth content chunk so a long
                    # DW stream stays observably fresh (Move 2 v4).
                    _stream_op_id = str(getattr(context, "op_id", "") or "")
                    _stream_chunk_count = 0
                    # Parse SSE stream with per-chunk timeout to detect stalled streams
                    while True:
                        try:
                            line = await asyncio.wait_for(
                                resp.content.readline(), timeout=_chunk_phase_timeout,
                            )
                        except asyncio.TimeoutError:
                            _rupt_elapsed = time.monotonic() - _ttft_request_start_monotonic
                            _rupt_phase = "ttft" if not _sse_has_tokens else "inter_chunk"
                            logger.error(
                                "[DoublewordProvider] STREAM RUPTURE "
                                "(phase=%s): no chunk for %.0fs "
                                "(elapsed=%.1fs, bytes=%d)",
                                _rupt_phase,
                                _chunk_phase_timeout,
                                _rupt_elapsed,
                                len(content),
                            )
                            raise StreamRuptureError(
                                provider="doubleword",
                                elapsed_s=_rupt_elapsed,
                                bytes_received=len(content),
                                rupture_timeout_s=_chunk_phase_timeout,
                                phase=_rupt_phase,
                            )
                        if not line:
                            break
                        line_str = line.decode("utf-8", errors="replace").strip()
                        if not line_str or not line_str.startswith("data: "):
                            continue
                        data_str = line_str[6:]  # Remove "data: " prefix
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            token = delta.get("content", "")
                            # Priority 1 Slice 1 + Slice 2 — capture +
                            # monitor. Reads chunk's logprobs structure;
                            # feeds capturer (always when capture enabled)
                            # and monitor (when monitor enabled). Master-
                            # flag-gated upstream; when off, both are None.
                            # NEVER raises into the SSE loop EXCEPT for
                            # the explicit ConfidenceCollapseError path
                            # under ENFORCE — which propagates as a
                            # structured RuntimeError that the caller
                            # already handles via "background_dw_error" /
                            # GENERATE_RETRY routing.
                            if _confidence_capturer is not None:
                                try:
                                    for _t, _lp, _top in (
                                        extract_openai_compat_logprobs_from_chunk(
                                            chunk,
                                        )
                                    ):
                                        _confidence_capturer.append(
                                            token=_t,
                                            logprob=_lp,
                                            top_logprobs=_top,
                                        )
                                        # Slice 2 monitor: feed the
                                        # top-1/top-2 margin if there
                                        # are at least 2 alternatives.
                                        if _confidence_monitor is not None:
                                            try:
                                                if (
                                                    isinstance(_top, list)
                                                    and len(_top) >= 2
                                                ):
                                                    _entry0 = _top[0]
                                                    _entry1 = _top[1]
                                                    _lp0 = (
                                                        _entry0.get(
                                                            "logprob"
                                                        ) if isinstance(
                                                            _entry0, dict
                                                        ) else None
                                                    )
                                                    _lp1 = (
                                                        _entry1.get(
                                                            "logprob"
                                                        ) if isinstance(
                                                            _entry1, dict
                                                        ) else None
                                                    )
                                                    if (
                                                        _lp0 is not None
                                                        and _lp1 is not None
                                                    ):
                                                        _confidence_monitor.observe(
                                                            float(_lp0)
                                                            - float(_lp1)
                                                        )
                                            except Exception:  # noqa: BLE001
                                                pass
                                except Exception:  # noqa: BLE001
                                    pass

                                # Slice 2 mid-stream verdict check. Cheap
                                # (O(K)). Slice 5 graduation flips ENFORCE
                                # on; until then, the verdict is observed
                                # but never aborts. SHADOW mode tags
                                # ctx.artifacts only; ENFORCE mode raises
                                # ConfidenceCollapseError.
                                if _confidence_monitor is not None:
                                    try:
                                        _posture: Optional[str] = None
                                        try:
                                            _posture = (
                                                getattr(
                                                    context,
                                                    "current_posture", None,
                                                )
                                                or getattr(
                                                    context,
                                                    "posture", None,
                                                )
                                            )
                                        except Exception:  # noqa: BLE001
                                            _posture = None
                                        _verdict = (
                                            _confidence_monitor.evaluate(
                                                posture=(
                                                    str(_posture)
                                                    if _posture else None
                                                ),
                                            )
                                        )
                                        # Tier 1 #1 — fire SSE on
                                        # verdict state transitions.
                                        # Best-effort, defensive,
                                        # never raises. Master-flag-
                                        # gated by
                                        # JARVIS_CONFIDENCE_SSE_PRODUCER_ENABLED.
                                        try:
                                            from backend.core.ouroboros.governance.verification.confidence_sse_producer import (  # noqa: E501
                                                observe_streaming_verdict,
                                            )
                                            _snap = (
                                                _confidence_monitor
                                                .snapshot()
                                            )
                                            observe_streaming_verdict(
                                                op_id=getattr(
                                                    context,
                                                    "op_id", "",
                                                ),
                                                verdict=_verdict,
                                                rolling_margin=(
                                                    _snap.rolling_margin
                                                ),
                                                window_size=(
                                                    _snap.window_size
                                                ),
                                                observations_count=(
                                                    _snap.observations_count
                                                ),
                                                posture=(
                                                    str(_posture)
                                                    if _posture
                                                    else None
                                                ),
                                                provider="doubleword",
                                                model_id=getattr(
                                                    _confidence_monitor,
                                                    "model_id", "",
                                                ),
                                            )
                                        except Exception:  # noqa: BLE001
                                            pass
                                        if _verdict != ConfidenceVerdict.OK:
                                            try:
                                                _arts = getattr(
                                                    context,
                                                    "artifacts", None,
                                                )
                                                if isinstance(
                                                    _arts, dict,
                                                ):
                                                    _arts[
                                                        "confidence_verdict"
                                                    ] = _verdict.value
                                                    _arts[
                                                        "confidence_margin"
                                                    ] = (
                                                        _confidence_monitor
                                                        .current_margin()
                                                    )
                                            except Exception:  # noqa: BLE001
                                                pass
                                            if (
                                                _monitor_enforce_active
                                                and _verdict
                                                == ConfidenceVerdict.BELOW_FLOOR
                                            ):
                                                raise (
                                                    _confidence_monitor
                                                    .to_collapse_error(
                                                        verdict=_verdict,
                                                        posture=(
                                                            str(_posture)
                                                            if _posture
                                                            else None
                                                        ),
                                                    )
                                                )
                                    except (
                                        Exception
                                    ) as _conf_exc:  # noqa: BLE001
                                        # Re-raise ConfidenceCollapseError
                                        # so caller's retry path engages.
                                        # Other exceptions from the verdict
                                        # path are swallowed defensively.
                                        from backend.core.ouroboros.governance.verification.confidence_monitor import (
                                            ConfidenceCollapseError,
                                        )
                                        if isinstance(
                                            _conf_exc,
                                            ConfidenceCollapseError,
                                        ):
                                            raise
                            if token:
                                content += token
                                self._last_chunk_at = time.monotonic()
                                # Stream Rupture Breaker: Phase 2 step-down.
                                # Once first token arrives, tighten the
                                # watchdog to inter-chunk timeout.
                                if not _sse_has_tokens:
                                    _sse_has_tokens = True
                                    _chunk_phase_timeout = _rupture_ic
                                    # Phase-Aware Heartbeat: pulse activity
                                    # on first token (TTFT → producing).
                                    try:
                                        from backend.core.ouroboros.governance.providers import (
                                            _emit_stream_activity as _activity_pulse,
                                        )
                                        _activity_pulse(_stream_op_id)
                                    except Exception:  # noqa: BLE001
                                        pass
                                _stream_chunk_count += 1
                                # Phase-Aware Heartbeat — every Nth content
                                # chunk pulses ActivityMonitor so a long DW
                                # stream stays fresh between phase transitions.
                                if (
                                    _stream_chunk_count > 0
                                    and _stream_chunk_count % 8 == 0
                                ):
                                    try:
                                        from backend.core.ouroboros.governance.providers import (
                                            _emit_stream_activity as _activity_pulse,
                                        )
                                        _activity_pulse(_stream_op_id)
                                    except Exception:  # noqa: BLE001
                                        pass
                                # Phase 12.2 Slice C — record TTFT once
                                # per request on first non-empty content
                                # chunk. NEVER raises into the SSE loop:
                                # observer faults are swallowed so a
                                # broken observer can't kill generation.
                                if not _ttft_first_chunk_seen:
                                    _ttft_first_chunk_seen = True
                                    try:
                                        _ttft_ms = int(
                                            (self._last_chunk_at
                                             - _ttft_request_start_monotonic)
                                            * 1000.0
                                        )
                                        self._record_ttft_safely(
                                            model_id=_effective_model,
                                            ttft_ms=_ttft_ms,
                                            op_id=getattr(
                                                context, "op_id", "",
                                            ) or "",
                                        )
                                    except Exception:  # noqa: BLE001
                                        pass
                                try:
                                    _stream_callback(token)
                                except Exception:
                                    pass
                            # Capture usage from final chunk
                            _usage = chunk.get("usage")
                            if _usage:
                                input_tokens = _usage.get("prompt_tokens", 0)
                                output_tokens = _usage.get("completion_tokens", 0)
                        except json.JSONDecodeError:
                            continue
            else:
                # Non-streaming path
                async with session.post(
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=self._request_timeout(),
                ) as resp:
                    if resp.status >= 300:
                        self._last_error_status = resp.status
                        err_body = await resp.text()
                        # Slice F — preserve full body + model_id
                        raise DoublewordInfraError(
                            f"Chat completions failed: "
                            f"{resp.status} {err_body[:200]}",
                            status_code=resp.status,
                            response_body=err_body,
                            model_id=_effective_model,
                        )

                    data = await resp.json()
                    choices = data.get("choices", [])
                    usage = data.get("usage", {})

                    if not choices:
                        raise DoublewordInfraError("No choices in response", status_code=0)

                    content = choices[0].get("message", {}).get("content", "")
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)

            # Accumulate token usage for outer scope
            _token_usage["input"] += input_tokens
            _token_usage["output"] += output_tokens

            # Track cost
            cost = (
                input_tokens * _DW_INPUT_COST_PER_M / 1_000_000
                + output_tokens * _DW_OUTPUT_COST_PER_M / 1_000_000
            )
            self._stats.total_input_tokens += input_tokens
            self._stats.total_output_tokens += output_tokens
            self._stats.total_cost_usd += cost
            self._record_cost(cost)
            total_cost += cost

            if total_cost >= self._max_cost_per_op:
                raise DoublewordInfraError(
                    f"doubleword_budget_exhausted_op:{total_cost:.4f}",
                    status_code=0,
                )

            return content

        def _parse_tool_call_response(raw: str) -> Optional[List[Any]]:
            """Parse tool call(s) from the model's response.

            Supports both singular ``tool_call`` and plural ``tool_calls``
            (parallel execution). Returns None if the response is a final
            answer (no tool call).
            """
            import re
            # Match either tool_call or tool_calls key
            match = re.search(
                r'\{\s*"schema_version"\s*:\s*"2b\.2-tool".*?"tool_call',
                raw, re.DOTALL,
            )
            if not match:
                return None
            # Extract the full JSON object
            try:
                brace_count = 0
                start = match.start()
                for i in range(start, len(raw)):
                    if raw[i] == "{":
                        brace_count += 1
                    elif raw[i] == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            tool_json = json.loads(raw[start:i + 1])
                            from backend.core.ouroboros.governance.tool_executor import ToolCall

                            def _parse_one(tc_dict: dict) -> Optional[Any]:
                                name = tc_dict.get("name", "")
                                if not name:
                                    return None
                                return ToolCall(
                                    name=name,
                                    arguments=tc_dict.get("arguments", {}),
                                )

                            # Parallel: tool_calls (plural)
                            plural = tool_json.get("tool_calls")
                            if isinstance(plural, list) and plural:
                                valid = [_parse_one(item) for item in plural if isinstance(item, dict)]
                                valid = [c for c in valid if c is not None]
                                return valid if valid else None

                            # Singular: tool_call
                            tc = tool_json.get("tool_call", {})
                            parsed = _parse_one(tc)
                            return [parsed] if parsed is not None else None
            except (json.JSONDecodeError, KeyError):
                pass
            return None

        # Execute with or without tool loop.
        # Complexity routing: skip Venom only for TRIVIAL tasks on DW.
        # Previously also skipped SIMPLE, but those still face the Iron Gate
        # exploration-first check (per CLAUDE.md: "trivial ops bypass").
        # Battle test bt-2026-04-11-085929 traced STANDARD route failures to
        # DW producing one-shot patches on simple ops → Iron Gate rejection
        # (0/2 exploration) → 71s of 120s budget burned → Claude fallback
        # starved to 48.7s → stream cut mid-output at 9KB. Keeping Venom on
        # for simple ops means DW does its own exploration and either passes
        # the gate directly or produces a correctly-shaped candidate for Claude.
        _complexity = getattr(context, "task_complexity", "")
        _ceiling = _DW_COMPLEXITY_MAX_TOKENS.get(_complexity, self._max_tokens)
        # Dynamic budget: complexity ceiling is the floor, scale up by
        # actual target file bytes. Matches what _generate_raw will
        # actually pass to the API (kept in sync so the log is truthful).
        _eff_mt = self._compute_dynamic_max_tokens(context, is_tool_round=False)
        _skip_tools = _complexity == "trivial"
        if _skip_tools:
            if _eff_mt > _ceiling:
                logger.info(
                    "[DoublewordProvider] \u26a1 %s task — skipping Venom tool loop "
                    "(one-shot, max_tokens=%d, dynamic: +%d above %s ceiling)",
                    _complexity or "trivial", _eff_mt, _eff_mt - _ceiling, _complexity or "trivial",
                )
            else:
                logger.info(
                    "[DoublewordProvider] \u26a1 %s task — skipping Venom tool loop "
                    "(one-shot, max_tokens=%d)", _complexity or "trivial", _eff_mt,
                )
        elif _eff_mt != self._max_tokens:
            logger.info(
                "[DoublewordProvider] Complexity=%s → max_tokens=%d (default=%d)",
                _complexity, _eff_mt, self._max_tokens,
            )

        tool_records: tuple = ()
        venom_edits: Tuple[Dict[str, Any], ...] = ()
        raw: str = ""

        if self._tool_loop is not None and not _skip_tools:
            deadline_mono = time.monotonic() + max(
                0.0,
                (deadline - datetime.now(tz=timezone.utc)).total_seconds()
                if deadline else 120.0,
            )
            raw, tool_records_list = await self._tool_loop.run(
                prompt=prompt,
                generate_fn=_generate_raw,
                parse_fn=_parse_tool_call_response,
                repo=getattr(context, "primary_repo", "jarvis"),
                op_id=getattr(context, "operation_id", f"dw-rt-{int(time.time())}"),
                deadline=deadline_mono,
                risk_tier=getattr(context, "risk_tier", None),
                is_read_only=bool(getattr(context, "is_read_only", False)),
            )
            tool_records = tuple(tool_records_list)
            # Venom mutation audit — captured from per-op ToolExecutor at
            # run() exit. Empty when no edit/write/delete tools fired.
            _hist_fn = getattr(self._tool_loop, "get_last_edit_history", None)
            if callable(_hist_fn):
                try:
                    _hist_raw = _hist_fn()
                except Exception:
                    _hist_raw = None
                if isinstance(_hist_raw, list):
                    venom_edits = tuple(_hist_raw)
        else:
            raw = await _generate_raw(prompt)

        elapsed = time.monotonic() - t0
        self._stats.total_batches += 1
        self._stats.total_latency_s += elapsed

        if not raw:
            raise DoublewordInfraError("Empty response from real-time API", status_code=0)

        # Parse the response into GenerationResult
        # Source hash must match what _check_source_drift() computes at GATE:
        # full SHA-256 of the target file's content (not the prompt).
        from backend.core.ouroboros.governance.providers import (
            _extract_json_block,
            _file_source_hash,
        )
        _src_hash = ""
        _src_path_str = ""
        if context.target_files:
            _src_path_str = context.target_files[0]
            _abs = (self._repo_root / _src_path_str).resolve()
            try:
                if _abs.is_file():
                    _src_hash = _file_source_hash(
                        _abs.read_text(encoding="utf-8", errors="replace")
                    )
            except OSError:
                pass
        _extracted = _extract_json_block(raw)
        if _extracted and not _extracted.lstrip().startswith("{"):
            logger.warning(
                "[DoublewordProvider] RT: 397B returned natural language instead of JSON. "
                "Response starts with: %s",
                _extracted[:100].replace("\n", " "),
            )
            raise DoublewordInfraError("Non-JSON response from real-time API", status_code=0)

        result = _parse_generation_response(
            raw=raw,
            provider_name="doubleword",
            duration_s=elapsed,
            ctx=context,
            source_hash=_src_hash,
            source_path=_src_path_str,
            repo_roots=self._repo_roots or None,
            repo_root=self._repo_root,
        )
        if _preloaded_files:
            result = dataclasses.replace(
                result, prompt_preloaded_files=tuple(_preloaded_files),
            )

        # Attach token usage and cost from _generate_raw
        if _token_usage["input"] or _token_usage["output"] or total_cost > 0:
            result = dataclasses.replace(
                result,
                total_input_tokens=_token_usage["input"],
                total_output_tokens=_token_usage["output"],
                cost_usd=total_cost,
            )

        logger.info(
            "[DoublewordProvider] RT: %d candidates in %.1fs ($%.4f, %d tool calls, %d+%d tokens)",
            len(result.candidates), elapsed, total_cost, len(tool_records),
            _token_usage["input"], _token_usage["output"],
        )

        # Attach Venom mutation audit (empty when no mutating tools fired).
        if venom_edits:
            result = result.with_venom_edits(venom_edits)

        return result

    # ------------------------------------------------------------------
    # plan() — CandidateProvider protocol (used by ContextExpander)
    # ------------------------------------------------------------------

    async def plan(self, prompt: str, deadline: Any = None) -> str:
        """Send a lightweight planning prompt via the batch API.

        Used by ContextExpander for context expansion rounds. Returns
        raw string response. On failure, raises so the caller can skip
        the expansion round gracefully (ContextExpander expects this).

        The ``ouroboros_plan`` caller is mapped by the Brain Selection
        Topology to Gemma 4 31B (basal ganglia). If topology is disabled
        or the caller is unmapped, falls back to the provider default.
        """
        del deadline  # reserved for future budget-aware planning
        try:
            from backend.core.ouroboros.governance.provider_topology import (
                get_topology,
            )
            _caller_model = get_topology().model_for_caller("ouroboros_plan")
        except Exception:
            _caller_model = None
        result = await self.prompt_only(
            prompt=prompt,
            model=_caller_model,
            caller_id="ouroboros_plan",
            max_tokens=4000,
        )
        return result or ""

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

        _rl_t0 = time.monotonic()
        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.acquire("doubleword", "files_upload")
            except Exception:
                raise  # Let CircuitBreakerOpen propagate

        try:
            async with session.post(
                f"{self._base_url}/files",
                data=data,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._request_timeout(),
            ) as resp:
                if self._rate_limiter is not None:
                    self._rate_limiter.record("doubleword", "files_upload",
                                              latency_s=time.monotonic() - _rl_t0, status=resp.status)
                if resp.status >= 300:
                    self._last_error_status = resp.status
                    body = await resp.text()
                    logger.error("[DoublewordProvider] File upload failed: %s %s", resp.status, body[:500])
                    return None
                result = await resp.json()
                return result.get("id")
        except Exception as exc:
            self._last_error_status = 0  # non-HTTP failure
            logger.warning("[DoublewordProvider] File upload error: %s: %s", type(exc).__name__, exc)
            return None

    async def _create_batch(self, input_file_id: str) -> Optional[str]:
        """Stage 2: Create batch job."""
        session = await self._get_session()
        _rl_t0 = time.monotonic()
        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.acquire("doubleword", "batches_create")
            except Exception:
                raise  # Let CircuitBreakerOpen propagate

        try:
            async with session.post(
                f"{self._base_url}/batches",
                json={
                    "input_file_id": input_file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": _DW_COMPLETION_WINDOW,
                },
                headers={"Content-Type": "application/json"},
                timeout=self._request_timeout(),
            ) as resp:
                if self._rate_limiter is not None:
                    self._rate_limiter.record("doubleword", "batches_create",
                                              latency_s=time.monotonic() - _rl_t0, status=resp.status)
                if resp.status >= 300:
                    self._last_error_status = resp.status
                    body = await resp.text()
                    logger.error("[DoublewordProvider] Batch create failed: %s %s", resp.status, body[:500])
                    return None
                result = await resp.json()
                batch_id = result.get("id")
                # Register webhook future (Tier 1) if registry is wired
                if batch_id and self._batch_registry is not None:
                    self._batch_registry.register(batch_id)
                return batch_id
        except Exception:
            self._last_error_status = 0
            logger.exception("[DoublewordProvider] Batch create error")
            return None

    # ------------------------------------------------------------------
    # Batch result awaiting: Tier 1 (webhook future) → Tier 2 (adaptive poll)
    # ------------------------------------------------------------------

    async def _await_batch_result(self, batch_id: str) -> Optional[str]:
        """Wait for batch result via webhook future or adaptive poll fallback.

        Tier 1: If a ``BatchFutureRegistry`` is wired and the batch has a
        registered future, await it (zero polling — webhook resolves it).

        Tier 2: Adaptive exponential backoff polling with jitter.
        """
        # Tier 1: webhook-driven (if registry wired)
        registry = getattr(self, "_batch_registry", None)
        if registry is not None:
            try:
                return await registry.wait(batch_id, timeout=_DW_MAX_WAIT_S)
            except asyncio.TimeoutError:
                logger.warning("[DoublewordProvider] Webhook wait timed out for %s", batch_id)
                return None
            except Exception:
                pass  # No future registered or rejected — fall through to Tier 2

        # Tier 2: adaptive backoff poll
        return await self._adaptive_poll_batch(batch_id)

    @staticmethod
    def _next_poll_interval(attempt: int, *, network_error: bool = False) -> float:
        """Compute next poll interval with exponential backoff + jitter.

        Starting interval: 2s (normal) or 15s (network error).
        Multiplier: 1.5x per attempt. Cap: 30s. Jitter: +/-25%.
        """
        import random
        base = 15.0 if network_error else 2.0
        interval = min(base * (1.5 ** attempt), 30.0)
        jitter = interval * 0.25 * (2 * random.random() - 1)
        return max(0.5, interval + jitter)

    async def _adaptive_poll_batch(self, batch_id: str) -> Optional[str]:
        """Stage 3: Adaptive backoff polling until batch completes.

        Replaces the fixed 5s poll with exponential backoff + jitter.
        Network-aware: connection errors trigger aggressive backoff.
        Returns output_file_id or None on failure/timeout.
        """
        deadline = time.monotonic() + _DW_MAX_WAIT_S
        attempt = 0

        while time.monotonic() < deadline:
            try:
                # Re-acquire session each iteration: if the connector was
                # poisoned by a CancelledError on a prior iteration,
                # _get_session() detects session.closed and creates a fresh one.
                session = await self._get_session()

                _rl_t0 = time.monotonic()
                if self._rate_limiter is not None:
                    try:
                        await self._rate_limiter.acquire("doubleword", "batches_poll")
                    except Exception:
                        raise  # Let CircuitBreakerOpen propagate

                async with session.get(
                    f"{self._base_url}/batches/{batch_id}",
                    timeout=self._request_timeout(),
                ) as resp:
                    if self._rate_limiter is not None:
                        self._rate_limiter.record("doubleword", "batches_poll",
                                                  latency_s=time.monotonic() - _rl_t0, status=resp.status)
                    if resp.status >= 300:
                        self._last_error_status = resp.status
                        logger.warning("[DoublewordProvider] Poll error: %s", resp.status)
                        await asyncio.sleep(self._next_poll_interval(attempt))
                        attempt += 1
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
                    # Still in_progress — adaptive backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _is_network = "connect" in str(exc).lower() or "timeout" in str(exc).lower()
                logger.debug(
                    "[DoublewordProvider] Poll attempt %d: %s (network=%s)",
                    attempt, type(exc).__name__, _is_network,
                )
                if _is_network:
                    attempt = max(attempt, 3)  # Jump to higher backoff for network errors

            await asyncio.sleep(self._next_poll_interval(attempt))
            attempt += 1

        logger.error("[DoublewordProvider] Batch %s timed out after %ds", batch_id, _DW_MAX_WAIT_S)
        return None

    async def _retrieve_result(
        self, output_file_id: str, operation_id: str
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Stage 4: Retrieve and parse batch output. Returns (content, usage)."""
        session = await self._get_session()
        _rl_t0 = time.monotonic()
        if self._rate_limiter is not None:
            try:
                await self._rate_limiter.acquire("doubleword", "batches_retrieve")
            except Exception:
                raise  # Let CircuitBreakerOpen propagate

        try:
            async with session.get(
                f"{self._base_url}/files/{output_file_id}/content",
                timeout=self._request_timeout(),
            ) as resp:
                if self._rate_limiter is not None:
                    self._rate_limiter.record("doubleword", "batches_retrieve",
                                              latency_s=time.monotonic() - _rl_t0, status=resp.status)
                if resp.status >= 300:
                    self._last_error_status = resp.status
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
                            # Extract content — for reasoning models like Qwen3.5,
                            # the actual answer may be in 'content' or 'reasoning_content'.
                            # Try 'content' first; if empty, fall back to 'reasoning_content'.
                            content = message.get("content", "")
                            if not content:
                                content = message.get("reasoning_content", "")
                                if content:
                                    logger.info(
                                        "[DoublewordProvider] Using reasoning_content "
                                        "(content was empty) for op=%s",
                                        operation_id,
                                    )
                            logger.debug(
                                "[DoublewordProvider] Response keys: %s, content_len=%d",
                                list(message.keys()), len(content),
                            )
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
        self._check_budget()

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
            # Qwen3.5: disable thinking mode so output goes to 'content'
            "chat_template_kwargs": {"enable_thinking": False},
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

            output_file_id = await self._await_batch_result(batch_id)
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
                self._record_cost(cost)

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
    # Functions-not-Agents path: complete_sync()
    # ------------------------------------------------------------------

    async def complete_sync(
        self,
        prompt: str,
        *,
        system_prompt: str,
        caller_id: str,
        model: Optional[str] = None,
        max_tokens: int = 512,
        timeout_s: float = 10.0,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
    ) -> CompleteSyncResult:
        """Non-streaming, short-output, caller-timed synchronous completion.

        This is the **Functions-not-Agents** code path. It bypasses the SSE
        streaming endpoint entirely and instead hits ``/v1/chat/completions``
        with ``stream=false``, awaiting a single JSON body. It is the single
        entry point for structured-function callers (CompactionCaller,
        BlastRadius, FailureClustering, DreamSeed) that need short, bounded,
        schema-validated output without the agent cascade's tool loops.

        Calibration context: bt-2026-04-14-182446 and bt-2026-04-14-203740
        established that DW's SSE streaming endpoint stalls post-accept
        across Qwen 397B and Gemma 4 31B. This method avoids the stall
        surface by never opening an SSE stream. It is the load-bearing
        primitive of the reseated DW topology (Manifesto §5).

        The caller enforces the timeout via ``asyncio.wait_for()``. If the
        request exceeds ``timeout_s``, ``asyncio.TimeoutError`` propagates
        to the caller, which is expected to handle circuit-breaker logic
        and fall back to its deterministic path.

        Parameters
        ----------
        prompt:
            User prompt text. Passed verbatim as the user message.
        system_prompt:
            Caller-specific system prompt. Required — no default. Every
            caller is expected to own its system prompt so the Functions
            path has no implicit shared instructions.
        caller_id:
            Identifier used in log messages and telemetry. Short string
            like ``"compaction"``, ``"blast_radius"``, ``"dream_seed"``.
        model:
            Override the model slug. Defaults to ``self._model``. The
            reseated topology expects callers to pass the model from
            ``provider_topology.get_topology().model_for_caller(caller_id)``
            so the yaml remains the single source of truth.
        max_tokens:
            Output token ceiling. Defaults to 512 — the Functions path is
            for short structured output, not long-form generation.
        timeout_s:
            Hard caller-supplied timeout enforced via ``asyncio.wait_for``.
            Raises ``asyncio.TimeoutError`` on expiry.
        response_format:
            Optional OpenAI-style response_format dict. Typical usage:
            ``{"type": "json_object"}`` for JSON-mode output.
        temperature:
            Override sampling temperature. Defaults to ``_DW_TEMPERATURE``.

        Returns
        -------
        CompleteSyncResult
            Structured result with content, token usage, cost, latency.

        Raises
        ------
        ValueError
            If DOUBLEWORD_API_KEY is not configured.
        asyncio.TimeoutError
            If the request exceeds ``timeout_s``.
        DoublewordInfraError
            On HTTP errors, empty choices, or cost-budget violations.
        """
        if not self._api_key:
            raise ValueError(
                "DOUBLEWORD_API_KEY is not set — cannot call complete_sync()"
            )
        self._check_budget()

        effective_model = model or self._model
        effective_temperature = (
            temperature if temperature is not None else _DW_TEMPERATURE
        )

        body: Dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": effective_temperature,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if response_format is not None:
            body["response_format"] = response_format

        session = await self._get_session()
        t0 = time.monotonic()

        async def _do_request() -> Tuple[str, int, int]:
            async with session.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=self._request_timeout(),
            ) as resp:
                if resp.status >= 300:
                    self._last_error_status = resp.status
                    err_body = await resp.text()
                    raise DoublewordInfraError(
                        f"complete_sync[{caller_id}] HTTP {resp.status}: {err_body[:200]}",
                        status_code=resp.status,
                    )
                data = await resp.json()
                choices = data.get("choices", [])
                if not choices:
                    raise DoublewordInfraError(
                        f"complete_sync[{caller_id}] no choices in response",
                        status_code=0,
                    )
                message = choices[0].get("message", {}) or {}
                _content = message.get("content", "") or ""
                usage = data.get("usage", {}) or {}
                _input_tokens = int(usage.get("prompt_tokens", 0) or 0)
                _output_tokens = int(usage.get("completion_tokens", 0) or 0)
                return _content, _input_tokens, _output_tokens

        try:
            content, input_tokens, output_tokens = await asyncio.wait_for(
                _do_request(), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            self._stats.failed_batches += 1
            logger.warning(
                "[DoublewordProvider] complete_sync[%s] timeout after %.1fs (model=%s)",
                caller_id, timeout_s, effective_model,
            )
            raise

        elapsed = time.monotonic() - t0
        cost = (
            input_tokens * _DW_INPUT_COST_PER_M / 1_000_000
            + output_tokens * _DW_OUTPUT_COST_PER_M / 1_000_000
        )
        self._stats.total_batches += 1
        self._stats.total_latency_s += elapsed
        self._stats.total_input_tokens += input_tokens
        self._stats.total_output_tokens += output_tokens
        self._stats.total_cost_usd += cost
        self._record_cost(cost)

        if not content:
            self._stats.empty_content_retries += 1
            logger.warning(
                "[DoublewordProvider] complete_sync[%s] empty content (model=%s, %.2fs)",
                caller_id, effective_model, elapsed,
            )

        logger.info(
            "[DoublewordProvider] complete_sync[%s] ok: %.2fs, %d chars, $%.5f (model=%s)",
            caller_id, elapsed, len(content), cost, effective_model,
        )

        return CompleteSyncResult(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_s=elapsed,
            model=effective_model,
        )

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
            async with session.get(f"{self._base_url}/models", timeout=self._request_timeout()) as resp:
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
