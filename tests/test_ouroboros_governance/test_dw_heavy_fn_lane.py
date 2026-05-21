"""DW Heavy Non-Streaming Lane — spine tests (Functions, Not Agents for codegen).

Pins the operator-approved design:
  * Composes existing ``DoublewordProvider.complete_sync()`` — does NOT
    duplicate the ``stream=false`` POST.
  * Returns ``GenerationResult`` (parsed via the existing
    ``_parse_generation_response``), NOT ``CompleteSyncResult``.
  * Resolves model via ``_resolve_effective_model`` — NO hardcoded models.
  * Master + 6 satellite knobs all default-FALSE / dormant on merge;
    operator opts in.
  * ``complete_sync()`` extension is **byte-identical for existing
    callers** when ``enable_thinking`` is omitted.

ALL helpers local to this file. ZERO real provider calls. ZERO spend.
S2 / S1 / OCA / admission / governor / sentinel substrates untouched.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance import (
    doubleword_provider as dw_module,
)
from backend.core.ouroboros.governance.doubleword_provider import (
    CompleteSyncResult,
    DoublewordProvider,
)
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)


# ============================================================================
# Local helpers — no imports from other test modules
# ============================================================================


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    """Strip every DW-heavy-lane env knob so each test starts at the
    documented defaults. NEVER touches existing provider state."""
    for k in (
        "JARVIS_DW_HEAVY_FN_LANE_ENABLED",
        "JARVIS_DW_HEAVY_FN_LANE_ELIGIBLE_COMPLEXITIES",
        "JARVIS_DW_HEAVY_FN_LANE_PREFER_OVER_SSE",
        "JARVIS_DW_HEAVY_FN_LANE_PREFER_ON_SSE_STALL",
        "JARVIS_DW_HEAVY_FN_LANE_TIMEOUT_S",
        "JARVIS_DW_HEAVY_FN_LANE_MAX_TOKENS",
        "JARVIS_DW_HEAVY_FN_LANE_ENABLE_THINKING",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def _prime_2b1_response(
    *, file_path: str = "x.py",
    content: str = "def f():\n    return 1\n",
) -> str:
    return json.dumps({
        "schema_version": "2b.1",
        "candidates": [{
            "candidate_id": "c1",
            "file_path": file_path,
            "full_content": content,
            "rationale": "heavy-lane test",
        }],
    })


def _make_dw_provider(
    tmp_path: Path,
    *,
    realtime_enabled: bool = False,
    tool_loop: Any = None,
) -> DoublewordProvider:
    """Construct a DW provider stub with the bare minimum surface for
    these tests. Avoids real network setup."""
    p = DoublewordProvider(
        api_key="test-dw-key",
        repo_root=tmp_path,
        realtime_enabled=realtime_enabled,
        tool_loop=tool_loop,
    )
    return p


def _make_ctx(
    *,
    op_id: str = "op-dw-heavy-baseline-001",
    route: str = "ide",
    task_complexity: str = "complex",
    target_files: tuple = ("x.py",),
    description: str = "heavy codegen op",
) -> OperationContext:
    import dataclasses
    ctx = OperationContext.create(
        target_files=target_files,
        description=description,
        op_id=op_id,
    )
    return dataclasses.replace(
        ctx,
        provider_route=route,
        task_complexity=task_complexity,
    )


# ============================================================================
# (1) complete_sync byte-identical for existing callers when enable_thinking
#     omitted — load-bearing additive-parameter test.
# ============================================================================


@pytest.mark.asyncio
async def test_complete_sync_default_preserves_enable_thinking_false(
    tmp_path,
):
    """Existing callers (CompactionCaller et al.) call ``complete_sync``
    without ``enable_thinking``. The body must send
    ``enable_thinking: False`` — byte-identical to pre-extension
    behavior."""
    provider = _make_dw_provider(tmp_path)

    captured: dict = {}

    def _fake_post(url, **kwargs):
        """aiohttp.ClientSession.post is SYNC — returns an async ctx mgr
        directly. NOT a coroutine."""
        captured["json"] = kwargs.get("json")
        resp = MagicMock()
        resp.status = 200
        # complete_sync calls await resp.json() (NOT resp.text)
        resp.json = AsyncMock(return_value={
            "choices": [{"message": {"content": _prime_2b1_response()}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        })
        resp.text = AsyncMock(return_value="")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    # Patch the provider's session getter (used by complete_sync)
    fake_session = MagicMock()
    fake_session.post = MagicMock(side_effect=_fake_post)
    provider._get_session = AsyncMock(return_value=fake_session)  # type: ignore[assignment]

    result = await provider.complete_sync(
        prompt="hello",
        system_prompt="you are a test",
        caller_id="compaction-style-test",
        timeout_s=10.0,
        # NOTE: enable_thinking deliberately omitted
    )

    assert isinstance(result, CompleteSyncResult)
    body = captured.get("json") or {}
    chat_template = body.get("chat_template_kwargs", {})
    assert chat_template.get("enable_thinking") is False, (
        f"Byte-identical legacy contract broken: expected "
        f"enable_thinking=False when omitted, got {chat_template!r}"
    )


@pytest.mark.asyncio
async def test_complete_sync_enable_thinking_true_threads_through(
    tmp_path,
):
    """When the heavy lane explicitly passes ``enable_thinking=True``,
    the wire body MUST carry that value."""
    provider = _make_dw_provider(tmp_path)
    captured: dict = {}

    def _fake_post(url, **kwargs):
        captured["json"] = kwargs.get("json")
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={
            "choices": [{"message": {"content": _prime_2b1_response()}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        })
        resp.text = AsyncMock(return_value="")
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    fake_session = MagicMock()
    fake_session.post = MagicMock(side_effect=_fake_post)
    provider._get_session = AsyncMock(return_value=fake_session)  # type: ignore[assignment]

    await provider.complete_sync(
        prompt="heavy reasoning op",
        system_prompt="heavy codegen system prompt",
        caller_id="heavy_codegen",
        timeout_s=120.0,
        enable_thinking=True,
    )

    body = captured.get("json") or {}
    chat_template = body.get("chat_template_kwargs", {})
    assert chat_template.get("enable_thinking") is True


# ============================================================================
# (2) Routing predicate — _should_use_heavy_nonstreaming_lane
# ============================================================================


class TestRoutingPredicate:
    """The predicate composes existing context state + sentinel SSE
    state — no new OperationContext fields. Master-flag check is the
    CALLER's responsibility per the predicate's docstring."""

    def test_ineligible_complexity_returns_false(self, tmp_path):
        provider = _make_dw_provider(tmp_path)
        ctx = _make_ctx(task_complexity="trivial")
        assert provider._should_use_heavy_nonstreaming_lane(
            ctx, sentinel_recent_sse_stall=False,
        ) is False
        assert provider._should_use_heavy_nonstreaming_lane(
            ctx, sentinel_recent_sse_stall=True,
        ) is False

    def test_heavy_no_tool_loop_no_prefer_no_stall_returns_false(
        self, tmp_path,
    ):
        provider = _make_dw_provider(tmp_path, tool_loop=None)
        ctx = _make_ctx(task_complexity="complex")
        assert provider._should_use_heavy_nonstreaming_lane(
            ctx, sentinel_recent_sse_stall=False,
        ) is False

    def test_heavy_no_tool_loop_prefer_over_sse_returns_true(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_DW_HEAVY_FN_LANE_PREFER_OVER_SSE", "true",
        )
        provider = _make_dw_provider(tmp_path, tool_loop=None)
        ctx = _make_ctx(task_complexity="complex")
        assert provider._should_use_heavy_nonstreaming_lane(
            ctx, sentinel_recent_sse_stall=False,
        ) is True

    def test_heavy_no_tool_loop_sse_stall_returns_true(self, tmp_path):
        provider = _make_dw_provider(tmp_path, tool_loop=None)
        ctx = _make_ctx(task_complexity="heavy_code")
        assert provider._should_use_heavy_nonstreaming_lane(
            ctx, sentinel_recent_sse_stall=True,
        ) is True

    def test_heavy_with_tool_loop_no_stall_returns_false(self, tmp_path):
        """Multi-turn agent op — stay on SSE absent transport stall."""
        provider = _make_dw_provider(
            tmp_path, tool_loop=MagicMock(),  # any truthy
        )
        ctx = _make_ctx(task_complexity="complex")
        assert provider._should_use_heavy_nonstreaming_lane(
            ctx, sentinel_recent_sse_stall=False,
        ) is False

    def test_heavy_with_tool_loop_sse_stall_routes_to_heavy_lane(
        self, tmp_path,
    ):
        """SSE stall + heavy op + PREFER_ON_SSE_STALL (default True)
        → heavy lane fires. Does NOT mark DW model weak; the
        decision is purely transport-driven."""
        provider = _make_dw_provider(tmp_path, tool_loop=MagicMock())
        ctx = _make_ctx(task_complexity="complex")
        assert provider._should_use_heavy_nonstreaming_lane(
            ctx, sentinel_recent_sse_stall=True,
        ) is True

    def test_heavy_with_tool_loop_sse_stall_but_prefer_disabled(
        self, tmp_path, monkeypatch,
    ):
        """Operator can disable the SSE-stall-triggered lane via env."""
        monkeypatch.setenv(
            "JARVIS_DW_HEAVY_FN_LANE_PREFER_ON_SSE_STALL", "false",
        )
        provider = _make_dw_provider(tmp_path, tool_loop=MagicMock())
        ctx = _make_ctx(task_complexity="complex")
        assert provider._should_use_heavy_nonstreaming_lane(
            ctx, sentinel_recent_sse_stall=True,
        ) is False

    def test_predicate_never_raises_on_garbage_context(self, tmp_path):
        provider = _make_dw_provider(tmp_path)

        class _BadCtx:
            # Force AttributeError on task_complexity access? Use
            # a property that raises:
            @property
            def task_complexity(self):
                raise RuntimeError("synthetic")
        # Predicate must catch and return False
        assert provider._should_use_heavy_nonstreaming_lane(
            _BadCtx(), sentinel_recent_sse_stall=True,
        ) is False


# ============================================================================
# (3) _generate_heavy_nonstreaming wrapper — calls complete_sync exactly
#     once and returns GenerationResult
# ============================================================================


def _patch_complete_sync(provider, *, return_text: str,
                         input_tokens: int = 100,
                         output_tokens: int = 50,
                         cost_usd: float = 0.001,
                         model: str = "Qwen/Qwen3.5-397B-A17B-FP8") -> List[dict]:
    """Replace ``provider.complete_sync`` with a spy that captures
    every call kwarg + returns a fake ``CompleteSyncResult``."""
    calls: List[dict] = []

    async def _spy(**kwargs):
        calls.append(dict(kwargs))
        return CompleteSyncResult(
            content=return_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_s=0.01,
            model=model,
        )

    provider.complete_sync = _spy  # type: ignore[assignment]
    return calls


@pytest.mark.asyncio
async def test_heavy_lane_calls_complete_sync_exactly_once(tmp_path):
    provider = _make_dw_provider(tmp_path)
    calls = _patch_complete_sync(
        provider, return_text=_prime_2b1_response(),
    )
    ctx = _make_ctx()
    result = await provider._generate_heavy_nonstreaming(ctx, None)
    assert len(calls) == 1, f"expected exactly 1 complete_sync call, got {len(calls)}"


@pytest.mark.asyncio
async def test_heavy_lane_returns_generation_result_not_complete_sync_result(
    tmp_path,
):
    provider = _make_dw_provider(tmp_path)
    _patch_complete_sync(provider, return_text=_prime_2b1_response())
    ctx = _make_ctx()
    result = await provider._generate_heavy_nonstreaming(ctx, None)
    assert isinstance(result, GenerationResult)
    assert not isinstance(result, CompleteSyncResult)
    assert len(result.candidates) >= 1


@pytest.mark.asyncio
async def test_heavy_lane_uses_codegen_system_prompt(tmp_path):
    """The wrapper passes ``_CODEGEN_SYSTEM_PROMPT`` (the shared
    codegen prompt), NOT a synthetic per-caller prompt."""
    from backend.core.ouroboros.governance.providers import (
        _CODEGEN_SYSTEM_PROMPT,
    )
    provider = _make_dw_provider(tmp_path)
    calls = _patch_complete_sync(
        provider, return_text=_prime_2b1_response(),
    )
    ctx = _make_ctx()
    await provider._generate_heavy_nonstreaming(ctx, None)
    assert calls[0]["system_prompt"] == _CODEGEN_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_heavy_lane_uses_caller_id_heavy_codegen(tmp_path):
    provider = _make_dw_provider(tmp_path)
    calls = _patch_complete_sync(
        provider, return_text=_prime_2b1_response(),
    )
    ctx = _make_ctx()
    await provider._generate_heavy_nonstreaming(ctx, None)
    assert calls[0]["caller_id"] == "heavy_codegen"


@pytest.mark.asyncio
async def test_heavy_lane_passes_enable_thinking_per_env(
    tmp_path, monkeypatch,
):
    """The wrapper threads the env-driven enable_thinking into
    complete_sync. Default TRUE for the heavy lane."""
    provider = _make_dw_provider(tmp_path)
    calls = _patch_complete_sync(
        provider, return_text=_prime_2b1_response(),
    )
    ctx = _make_ctx()
    # default: TRUE per design
    await provider._generate_heavy_nonstreaming(ctx, None)
    assert calls[-1]["enable_thinking"] is True
    # Override to FALSE
    monkeypatch.setenv(
        "JARVIS_DW_HEAVY_FN_LANE_ENABLE_THINKING", "false",
    )
    await provider._generate_heavy_nonstreaming(ctx, None)
    assert calls[-1]["enable_thinking"] is False


@pytest.mark.asyncio
async def test_heavy_lane_threads_timeout_env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_DW_HEAVY_FN_LANE_TIMEOUT_S", "45.0",
    )
    provider = _make_dw_provider(tmp_path)
    calls = _patch_complete_sync(
        provider, return_text=_prime_2b1_response(),
    )
    ctx = _make_ctx()
    await provider._generate_heavy_nonstreaming(ctx, None)
    assert calls[-1]["timeout_s"] == pytest.approx(45.0)


@pytest.mark.asyncio
async def test_heavy_lane_threads_max_tokens_env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_DW_HEAVY_FN_LANE_MAX_TOKENS", "8192",
    )
    provider = _make_dw_provider(tmp_path)
    calls = _patch_complete_sync(
        provider, return_text=_prime_2b1_response(),
    )
    ctx = _make_ctx()
    await provider._generate_heavy_nonstreaming(ctx, None)
    assert calls[-1]["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_heavy_lane_token_usage_threaded_into_result(tmp_path):
    provider = _make_dw_provider(tmp_path)
    _patch_complete_sync(
        provider, return_text=_prime_2b1_response(),
        input_tokens=999, output_tokens=222, cost_usd=0.0042,
    )
    ctx = _make_ctx()
    result = await provider._generate_heavy_nonstreaming(ctx, None)
    assert result.total_input_tokens == 999
    assert result.total_output_tokens == 222
    assert result.cost_usd == pytest.approx(0.0042)


@pytest.mark.asyncio
async def test_heavy_lane_model_id_propagates_from_complete_sync(
    tmp_path,
):
    provider = _make_dw_provider(tmp_path)
    _patch_complete_sync(
        provider, return_text=_prime_2b1_response(),
        model="Qwen/Qwen3.5-397B-A17B-FP8",
    )
    ctx = _make_ctx()
    result = await provider._generate_heavy_nonstreaming(ctx, None)
    assert result.model_id == "Qwen/Qwen3.5-397B-A17B-FP8"


@pytest.mark.asyncio
async def test_heavy_lane_provider_name_distinguishes_lane(tmp_path):
    """Provider name in result must distinguish this lane from RT/batch
    so downstream telemetry can attribute correctly."""
    provider = _make_dw_provider(tmp_path)
    _patch_complete_sync(provider, return_text=_prime_2b1_response())
    ctx = _make_ctx()
    result = await provider._generate_heavy_nonstreaming(ctx, None)
    assert "doubleword" in result.provider_name.lower()
    # The exact tag is implementation-defined; pin only the
    # heavy/nonstreaming distinction.
    assert (
        "heavy" in result.provider_name.lower()
        or "nonstream" in result.provider_name.lower()
    )


# ============================================================================
# (4) Master OFF byte-identical
# ============================================================================


@pytest.mark.asyncio
async def test_master_off_skips_heavy_lane_in_dispatch_internal(tmp_path):
    """Master OFF (default) ⇒ _dispatch_internal must NOT consult
    the heavy-lane predicate; behavior byte-identical to today.
    Verified by spying on the predicate via monkeypatch."""
    provider = _make_dw_provider(tmp_path, realtime_enabled=True)
    # Make sure master is OFF (autouse fixture already strips env)
    assert not dw_module._dw_heavy_fn_lane_master_enabled()

    predicate_calls = []
    original_predicate = provider._should_use_heavy_nonstreaming_lane

    def _spy(*args, **kwargs):
        predicate_calls.append((args, kwargs))
        return original_predicate(*args, **kwargs)

    provider._should_use_heavy_nonstreaming_lane = _spy  # type: ignore[assignment]

    # Stub _generate_realtime to short-circuit + return a fake result
    async def _stub_rt(*args, **kwargs):
        return GenerationResult(
            candidates=(),
            provider_name="doubleword-rt-stub",
            generation_duration_s=0.0,
        )
    provider._generate_realtime = _stub_rt  # type: ignore[assignment]

    ctx = _make_ctx()
    await provider._dispatch_internal(ctx, None)
    assert predicate_calls == [], (
        "Master OFF must not invoke the heavy-lane predicate; "
        f"saw {len(predicate_calls)} call(s)"
    )


# ============================================================================
# (5) AST pins
# ============================================================================


def test_ast_pin_no_hardcoded_model_names_in_heavy_lane():
    """Heavy lane MUST NOT carry literal model strings — model
    resolved via ``_resolve_effective_model(context)``."""
    src = Path(
        "backend/core/ouroboros/governance/doubleword_provider.py"
    ).read_text(encoding="utf-8")
    # Isolate the heavy lane method body
    import re
    match = re.search(
        r"async def _generate_heavy_nonstreaming\(.*?\n(.*?)\n    "
        r"# -+\n    # Real-time generation",
        src, re.DOTALL,
    )
    assert match is not None, "could not isolate _generate_heavy_nonstreaming"
    body = match.group(1)
    # Reject obvious model-name patterns inside the body
    forbidden_patterns = [
        '"Qwen/', "'Qwen/",
        '"claude-', "'claude-",
        '"gpt-', "'gpt-",
        '"deepseek', "'deepseek",
    ]
    for fp in forbidden_patterns:
        assert fp not in body, (
            f"heavy lane body contains hardcoded model literal {fp!r}"
        )


def test_ast_pin_heavy_lane_composes_complete_sync():
    """The wrapper MUST call ``self.complete_sync(...)`` — not
    duplicate the ``stream=false`` POST logic."""
    src = Path(
        "backend/core/ouroboros/governance/doubleword_provider.py"
    ).read_text(encoding="utf-8")
    import re
    match = re.search(
        r"async def _generate_heavy_nonstreaming\(.*?\n(.*?)\n    "
        r"# -+\n    # Real-time generation",
        src, re.DOTALL,
    )
    assert match is not None
    body = match.group(1)
    assert "self.complete_sync(" in body, (
        "heavy lane must compose self.complete_sync — found no call site"
    )
    # And MUST NOT contain its own /v1/chat/completions wire setup
    assert "/v1/chat/completions" not in body, (
        "heavy lane must not duplicate /v1/chat/completions wire code"
    )
    assert '"stream": False' not in body, (
        "heavy lane must not have its own stream=False body assembly"
    )


def test_ast_pin_heavy_lane_uses_existing_parse_response():
    src = Path(
        "backend/core/ouroboros/governance/doubleword_provider.py"
    ).read_text(encoding="utf-8")
    import re
    match = re.search(
        r"async def _generate_heavy_nonstreaming\(.*?\n(.*?)\n    "
        r"# -+\n    # Real-time generation",
        src, re.DOTALL,
    )
    assert match is not None
    body = match.group(1)
    assert "_parse_generation_response" in body, (
        "heavy lane must use the existing _parse_generation_response parser"
    )


def test_ast_pin_no_duplicate_prompt_builder():
    """Heavy lane uses prompt_override OR composes _build_codegen_prompt
    — must NOT define its own prompt-assembly helper."""
    src = Path(
        "backend/core/ouroboros/governance/doubleword_provider.py"
    ).read_text(encoding="utf-8")
    import re
    match = re.search(
        r"async def _generate_heavy_nonstreaming\(.*?\n(.*?)\n    "
        r"# -+\n    # Real-time generation",
        src, re.DOTALL,
    )
    assert match is not None
    body = match.group(1)
    # Must reuse the existing helper
    assert "_build_codegen_prompt" in body, (
        "heavy lane must reuse _build_codegen_prompt (no parallel "
        "prompt builder)"
    )
    # Must NOT define a new local function with prompt assembly intent
    assert "def _build_heavy_prompt" not in body
    assert "def _assemble_heavy_prompt" not in body


def test_ast_pin_complete_sync_signature_extension():
    """``complete_sync`` was extended additively with
    ``enable_thinking: Optional[bool] = None`` — new param at end,
    default None, preserves call-site compatibility."""
    import inspect
    sig = inspect.signature(DoublewordProvider.complete_sync)
    params = sig.parameters
    assert "enable_thinking" in params
    p = params["enable_thinking"]
    assert p.default is None, (
        "enable_thinking must default to None to preserve byte-"
        "identical legacy behavior for existing callers"
    )
    # Pin the existing parameters are unchanged (additive extension)
    for legacy in (
        "prompt", "system_prompt", "caller_id", "model",
        "max_tokens", "timeout_s", "response_format", "temperature",
    ):
        assert legacy in params, (
            f"complete_sync missing legacy param {legacy!r}"
        )


# ============================================================================
# (6) Failure cascading — heavy lane failures propagate; orchestrator
#     cascades to Claude unchanged
# ============================================================================


@pytest.mark.asyncio
async def test_heavy_lane_complete_sync_timeout_propagates(tmp_path):
    """If ``complete_sync`` raises ``asyncio.TimeoutError``, the
    wrapper must let it propagate — orchestrator handles cascade."""
    provider = _make_dw_provider(tmp_path)

    async def _timeout(**kwargs):
        raise asyncio.TimeoutError("synthetic timeout")
    provider.complete_sync = _timeout  # type: ignore[assignment]

    ctx = _make_ctx()
    with pytest.raises(asyncio.TimeoutError):
        await provider._generate_heavy_nonstreaming(ctx, None)


@pytest.mark.asyncio
async def test_heavy_lane_complete_sync_dw_infra_error_propagates(tmp_path):
    """``DoublewordInfraError`` from complete_sync propagates; the
    orchestrator's existing cascade-to-Claude branch handles it
    unchanged."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordInfraError,
    )
    provider = _make_dw_provider(tmp_path)

    async def _err(**kwargs):
        raise DoublewordInfraError("synthetic", status_code=500)
    provider.complete_sync = _err  # type: ignore[assignment]

    ctx = _make_ctx()
    with pytest.raises(DoublewordInfraError):
        await provider._generate_heavy_nonstreaming(ctx, None)
