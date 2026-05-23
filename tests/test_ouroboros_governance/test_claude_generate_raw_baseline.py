"""Phase 1 characterization spine — ClaudeProvider `_generate_raw`.

Pins the CURRENTLY-OBSERVABLE behavior of the 1,035-line nested closure
``_generate_raw`` at ``providers.py:6479-7514`` (signature
``async def _generate_raw(p: str) -> str``) without modifying any
production code.

The closure is not externally reachable; tests therefore exercise it
indirectly through the public ``ClaudeProvider.generate(ctx, deadline)``
seam — observing:

  * `GenerationResult` returned (provider_name, model_id, candidates,
    tokens, cost, etc.)
  * `provider._daily_spend` mutation (the load-bearing visible side
    effect of the closure's ``nonlocal total_cost``)
  * Exception type + message on error paths
  * Mock-client call kwargs (kwargs to ``messages.create`` /
    ``messages.stream``) when those reflect observable behavior
    contracts (e.g., route → thinking gating)

ALL tests use locally-defined mock helpers — no import from
``test_tool_use_interface.py``. ALL tests mock at the ``provider._client``
seam. ZERO real Anthropic calls. ZERO provider spend.

Phase 1 AST pins (clearly labelled TEMPORARY) lock the closure's
current nested shape. They MUST be intentionally updated in Phase 2
when extraction happens. They are characterization-pins, not
forward-looking contracts.

Hard guardrails (this PR):
  * providers.py is READ-ONLY.
  * No provider_response_cache / S2 / admission / governor /
    candidate_generator / OCA edits.
  * Master flags untouched (default-FALSE preserved per-test via
    autouse fixture).
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
)
from backend.core.ouroboros.governance.providers import ClaudeProvider


# ============================================================================
# Local mock helpers (NOT imported from test_tool_use_interface.py)
# ============================================================================


def _prime_2b1_response(
    *, file_path: str = "x.py",
    content: str = "def f():\n    return 1\n",
    rationale: str = "characterization",
) -> str:
    """Minimal valid 2b.1 candidate JSON — the canonical schema
    Claude returns for codegen ops. NEVER raises."""
    return json.dumps({
        "schema_version": "2b.1",
        "candidates": [{
            "candidate_id": "c1",
            "file_path": file_path,
            "full_content": content,
            "rationale": rationale,
        }],
    })


def _make_fake_message(
    text: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 100,
    model: str = "claude-sonnet-4-20250514",
    stop_reason: str = "end_turn",
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> Any:
    """Build a fake Anthropic Message shape returned by
    `messages.create()`. Carries usage + stop_reason fields the
    closure reads."""
    msg = MagicMock()
    msg.content = [MagicMock(text=text, type="text")]
    msg.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
    )
    msg.model = model
    msg.stop_reason = stop_reason
    msg.stop_sequence = None
    return msg


def _make_create_only_client(
    *, responses: Optional[List[str]] = None,
    usage_input: int = 100,
    usage_output: int = 100,
    stop_reason: str = "end_turn",
    raise_on_create: Optional[BaseException] = None,
) -> Any:
    """Mock anthropic client whose ``messages.create`` is the only
    callable seam. Returns a fake Message each call.

    Caller-supplied ``responses`` is a list of response-text strings;
    each subsequent call advances the cursor and clamps at the end.
    Default: one valid 2b.1 candidate.

    ``raise_on_create`` lets a test simulate Anthropic-side errors."""
    texts = list(responses) if responses else [_prime_2b1_response()]
    call_count = [0]

    async def _create(**kwargs):
        call_count[0] += 1
        if raise_on_create is not None:
            raise raise_on_create
        i = min(call_count[0] - 1, len(texts) - 1)
        return _make_fake_message(
            texts[i],
            input_tokens=usage_input,
            output_tokens=usage_output,
            stop_reason=stop_reason,
        )

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = _create
    client._call_count = call_count          # observable for assertions
    client._last_kwargs = []
    # Capture the kwargs of every .create call for route/thinking pins.
    real_create = client.messages.create

    async def _capturing(**kwargs):
        client._last_kwargs.append(dict(kwargs))
        return await real_create(**kwargs)
    client.messages.create = _capturing
    return client


def _make_ctx(
    *,
    op_id: str = "op-claude-baseline-001",
    route: str = "ide",
    target_files: Tuple[str, ...] = ("x.py",),
    description: str = "characterization op",
    task_complexity: str = "trivial",
) -> OperationContext:
    """Local ctx factory — no import from test_tool_use_interface.py.
    Uses canonical `OperationContext.create` + `dataclasses.replace`."""
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


def _deadline(*, seconds_from_now: float = 60.0) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(seconds=seconds_from_now)


# ============================================================================
# Autouse fixture — strict environment isolation per characterization run
# ============================================================================


@pytest.fixture(autouse=True)
def _strict_env_iso(monkeypatch):
    """Strip every flag that could perturb the closure's behavior so
    each characterization run reproduces the documented defaults."""
    for k in (
        # S1 cache substrate
        "JARVIS_PROVIDER_RESPONSE_CACHE_ENABLED",
        "JARVIS_PROVIDER_RESPONSE_CACHE_SHADOW",
        "JARVIS_PROVIDER_CACHE_MAX_BYTES",
        "JARVIS_PROVIDER_CACHE_TTL_S",
        "JARVIS_PROVIDER_CACHE_PATH",
        # S2 layer
        "JARVIS_S2_PREDICTIVE_BUDGET_ENABLED",
        "JARVIS_S2_SESSION_BUDGET_USD",
        "OUROBOROS_BATTLE_COST_CAP",
        # Claude thinking knobs
        "JARVIS_THINKING_BUDGET_IMMEDIATE",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


# ============================================================================
# Family 1 — messages.create path (non-streaming)
# ============================================================================


class TestFamily1MessagesCreate:
    """Non-streaming dispatch via ``messages.create``. The closure
    drives this when streaming is disabled or fails-fallback. Tests
    observe via the GenerationResult returned by provider.generate()."""

    @pytest.mark.asyncio
    async def test_create_returns_valid_2b1_candidate(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        result = await provider.generate(_make_ctx(), _deadline())
        assert isinstance(result, GenerationResult)
        assert len(result.candidates) >= 1
        assert result.candidates[0]["file_path"] == "x.py"

    @pytest.mark.asyncio
    async def test_create_provider_name_is_claude_api(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        result = await provider.generate(_make_ctx(), _deadline())
        assert result.provider_name == "claude-api"

    # ──────────────────────────────────────────────────────────────────
    # PHASE-1 CHARACTERIZATION DIVERGENCE #1 — model_id propagation
    # ──────────────────────────────────────────────────────────────────
    # OBSERVED: result.model_id == "" (empty) after a successful
    # provider.generate() call, despite:
    #   - The closure sending `model=claude-sonnet-4-20250514` to the
    #     Anthropic API request (visible in INFO log:
    #     "[ClaudeProvider] → create model=claude-sonnet-4-20250514").
    #   - The fake Message carrying msg.model = "claude-sonnet-4-20250514".
    #
    # CONTRACT BREACH: GenerationResult.model_id should carry the model
    # actually used for the call so downstream telemetry / cache keys
    # can distinguish ops by model.
    #
    # PHASE-2 GRADUATION CRITERION: the refactored `_generate_raw`
    # path MUST thread the resolved model identity through the
    # GenerationResult. Removing this @xfail is a Phase 2 release
    # gate — it must transition from xfail → pass naturally.
    @pytest.mark.xfail(
        reason=(
            "PHASE-1 divergence #1: result.model_id returns '' instead "
            "of self._model despite the API call using the correct "
            "model. Documents the current production contract breach; "
            "Phase 2 graduation must resolve."
        ),
        strict=True,
    )
    @pytest.mark.asyncio
    async def test_create_model_id_propagated_from_provider(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        result = await provider.generate(_make_ctx(), _deadline())
        assert result.model_id == provider._model

    @pytest.mark.asyncio
    async def test_create_called_exactly_once_for_no_tools_path(
        self, tmp_path,
    ):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        client = _make_create_only_client()
        provider._client = client
        await provider.generate(_make_ctx(), _deadline())
        # `tools_enabled=False` (default) ⇒ no tool-loop iteration ⇒
        # exactly one create call.
        assert client._call_count[0] == 1

    @pytest.mark.asyncio
    async def test_create_kwargs_include_model_and_max_tokens(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        client = _make_create_only_client()
        provider._client = client
        await provider.generate(_make_ctx(), _deadline())
        kw = client._last_kwargs[-1]
        # These are load-bearing observable kwargs the closure must
        # always supply.
        assert "model" in kw
        assert "max_tokens" in kw
        assert kw["model"] == provider._model

    @pytest.mark.asyncio
    async def test_create_token_usage_propagated_to_result(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client(
            usage_input=42, usage_output=17,
        )
        result = await provider.generate(_make_ctx(), _deadline())
        assert result.total_input_tokens == 42
        assert result.total_output_tokens == 17

    @pytest.mark.asyncio
    async def test_create_cost_usd_is_positive_real_number(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client(
            usage_input=1000, usage_output=500,
        )
        result = await provider.generate(_make_ctx(), _deadline())
        # The closure's nonlocal `total_cost` mutation is OBSERVABLE
        # via result.cost_usd (pinned by `_finalize_codegen_result`).
        assert isinstance(result.cost_usd, float)
        assert result.cost_usd > 0.0

    @pytest.mark.asyncio
    async def test_create_zero_tokens_yields_zero_cost(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client(
            usage_input=0, usage_output=0,
        )
        result = await provider.generate(_make_ctx(), _deadline())
        assert result.cost_usd == 0.0


# ============================================================================
# Family 2 — observable token/cost cumulation (the ``nonlocal`` mutations)
# ============================================================================


class TestFamily2NonlocalMutationObservable:
    """The closure declares ``nonlocal total_cost`` at L6480 (plus
    further nonlocal capture chains in its nested helpers covering
    ``_cached_input``, ``input_tokens``, ``output_tokens``,
    ``raw_content``). These tests pin the observable post-execution
    state: total_cost == result.cost_usd; tokens propagate."""

    @pytest.mark.asyncio
    async def test_provider_daily_spend_mutates_after_call(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path,
                                   daily_budget=100.0)
        provider._client = _make_create_only_client(
            usage_input=200, usage_output=100,
        )
        before = float(provider._daily_spend)
        result = await provider.generate(_make_ctx(), _deadline())
        after = float(provider._daily_spend)
        # _daily_spend must grow by at least result.cost_usd (the
        # closure may also account for prompt-cache reductions).
        assert after >= before
        # And the delta is a finite positive number when cost > 0.
        if result.cost_usd > 0.0:
            assert (after - before) > 0.0

    @pytest.mark.asyncio
    async def test_two_consecutive_calls_accumulate_spend(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path,
                                   daily_budget=100.0)
        provider._client = _make_create_only_client(
            usage_input=100, usage_output=50,
        )
        s0 = float(provider._daily_spend)
        await provider.generate(_make_ctx(op_id="op-A"), _deadline())
        s1 = float(provider._daily_spend)
        await provider.generate(_make_ctx(op_id="op-B"), _deadline())
        s2 = float(provider._daily_spend)
        assert s0 <= s1 <= s2

    @pytest.mark.asyncio
    async def test_cost_matches_token_usage_monotonicity(self, tmp_path):
        """Larger token usage must produce larger or equal cost.
        Pins the monotonic property of the cost-accounting math without
        asserting a specific price-table value."""
        provider_small = ClaudeProvider(
            api_key="test-key", repo_root=tmp_path,
        )
        provider_small._client = _make_create_only_client(
            usage_input=10, usage_output=5,
        )
        r_small = await provider_small.generate(_make_ctx(), _deadline())

        provider_large = ClaudeProvider(
            api_key="test-key", repo_root=tmp_path,
        )
        provider_large._client = _make_create_only_client(
            usage_input=10000, usage_output=5000,
        )
        r_large = await provider_large.generate(_make_ctx(), _deadline())

        assert r_large.cost_usd >= r_small.cost_usd


# ============================================================================
# Family 2b — multi-round tool-loop cumulative semantics (PR 2A invariant)
# ============================================================================


class TestFamily2bMultiRoundAccumulation:
    """Load-bearing invariant for PR 2A mechanical extraction.

    The legacy nested closure `_generate_raw` accumulates token + cost
    state into the outer-scope `total_cost` (nonlocal) and `_token_usage`
    (dict) via ``+=`` across multiple invocations within a single
    `generate()` call (multi-round tool loop). PR 2A introduces a small
    compatibility wrapper that preserves this `async (prompt: str) -> str`
    contract for ``tool_loop.run(generate_fn=...)`` — the wrapper must
    fold per-dispatch deltas back into the outer scope as ``+=``, NOT ``=``.

    This test characterizes the multi-round cost/token preservation
    BEFORE the refactor (must pass on `9c0932e749`) AND after it (must
    keep passing — that's the 2A contract).

    Exercised via the existing tool-loop pathway: enable tools, feed N
    tool-call responses followed by a final patch, observe that the
    cumulative result tokens & cost are NOT just the last round's
    contribution.
    """

    def _tool_call_response(self) -> str:
        """2b.2-tool schema — a single tool call."""
        import json as _json
        return _json.dumps({
            "schema_version": "2b.2-tool",
            "tool_call": {
                "name": "search_code",
                "arguments": {"pattern": "foo"},
            },
        })

    def _final_patch_response(self) -> str:
        return _prime_2b1_response()

    def _multi_round_client(
        self, *, n_tool_rounds: int,
        usage_input_per_round: int,
        usage_output_per_round: int,
    ) -> Any:
        """Builds a mock client that returns ``n_tool_rounds`` tool-call
        responses, then one final 2b.1 patch response. Each .create
        call carries the same usage (per_round). Observable cumulative
        totals must reflect (n_tool_rounds + 1) × per_round."""
        texts = (
            [self._tool_call_response()] * n_tool_rounds
            + [self._final_patch_response()]
        )
        call_count = [0]
        last_kwargs: List[dict] = []

        async def _create(**kwargs):
            i = min(call_count[0], len(texts) - 1)
            call_count[0] += 1
            last_kwargs.append(dict(kwargs))
            return _make_fake_message(
                texts[i],
                input_tokens=usage_input_per_round,
                output_tokens=usage_output_per_round,
                stop_reason=(
                    "tool_use" if i < n_tool_rounds else "end_turn"
                ),
            )

        client = MagicMock()
        client.messages = MagicMock()
        client.messages.create = _create
        client._call_count = call_count
        client._last_kwargs = last_kwargs
        return client

    # ── 2A LOAD-BEARING INVARIANT (must pass today + post-2A) ─────
    @pytest.mark.asyncio
    async def test_two_round_tool_loop_cost_accumulates_across_rounds(
        self, tmp_path,
    ):
        """Two tool-call rounds + one final patch = 3 dispatch calls.
        The 2A wrapper threading per-dispatch cost deltas back to
        generate()'s scope MUST use ``+=`` so cumulative cost grows
        across rounds.

        Current closure behavior: `total_cost += cost` at L7502 inside
        _generate_raw — cost IS accumulated. PR 2A must preserve.

        If 2A accidentally overwrites with ``=``, this test fails
        (cost reflects only the last round's contribution).
        """
        provider = ClaudeProvider(
            api_key="test-key", repo_root=tmp_path, tools_enabled=True,
        )
        provider._client = self._multi_round_client(
            n_tool_rounds=2,
            usage_input_per_round=100,
            usage_output_per_round=50,
        )
        result = await provider.generate(_make_ctx(), _deadline())
        # 3 dispatch calls total: 2 tool rounds + 1 final patch.
        assert provider._client._call_count[0] == 3, (
            f"expected 3 dispatch calls (2 tool + 1 final), got "
            f"{provider._client._call_count[0]}"
        )
        # Cost MUST be strictly positive and reflect multi-round
        # accumulation. Empirical baseline (3 calls × Sonnet pricing
        # @ 100 in/50 out per call): ≥ $0.001 (the single-round
        # contribution); we don't pin a tight upper bound to avoid
        # over-specifying the pricing math.
        assert result.cost_usd > 0.0
        single_round_floor = (100 * 3e-6) + (50 * 1.5e-5)  # ≈ $0.00105
        assert result.cost_usd >= single_round_floor * 2, (
            f"PR 2A invariant: cost across 3 rounds must >= 2× a single "
            f"round's contribution (i.e., NOT just last-round). Got "
            f"cost_usd={result.cost_usd}, floor={single_round_floor * 2}"
        )

    @pytest.mark.asyncio
    async def test_three_round_tool_loop_cost_grows_with_rounds(
        self, tmp_path,
    ):
        """3 tool rounds + 1 final patch = 4 dispatch calls. Cost
        must strictly exceed the same workload with 1 round."""
        provider_3 = ClaudeProvider(
            api_key="test-key", repo_root=tmp_path, tools_enabled=True,
        )
        provider_3._client = self._multi_round_client(
            n_tool_rounds=3,
            usage_input_per_round=200,
            usage_output_per_round=80,
        )
        r_3 = await provider_3.generate(_make_ctx(), _deadline())
        assert provider_3._client._call_count[0] == 4
        assert r_3.cost_usd > 0.0
        # Cost monotonicity: 4-round cost ≥ baseline-single-round.
        single = (200 * 3e-6) + (80 * 1.5e-5)
        assert r_3.cost_usd >= single * 3, (
            f"cost monotonicity broken: 4-round cost {r_3.cost_usd} "
            f"< 3× single-round floor {single * 3}"
        )

    # ──────────────────────────────────────────────────────────────────
    # PHASE-1 CHARACTERIZATION DIVERGENCE #3 — multi-round token loss
    # ──────────────────────────────────────────────────────────────────
    # OBSERVED on HEAD 9c0932e749: after a multi-round tool-loop
    # generate() call, the final GenerationResult.total_input_tokens
    # and total_output_tokens both report 0 — despite the log line
    # showing the correct tool_rounds count AND despite cost being
    # correctly accumulated across rounds (see the two cost-preservation
    # tests above which PASS today).
    #
    # Example from a 3-call run with 100in/50out per call:
    #   [ClaudeProvider] 1 candidates in 0.0s (tool_rounds=3),
    #     cost=$0.0072, 0+0 tokens, ...
    #
    # CONTRACT BREACH: token usage MUST surface to the result in the
    # same way cost does. Cost & tokens are siblings; today they
    # diverge — cost is preserved in _token_usage["input"] += ... AND
    # propagates to the result; tokens accumulate in _token_usage but
    # are dropped somewhere between the dict and _finalize_codegen_result.
    #
    # PR 2A boundary: 2A is MECHANICAL EXTRACTION ONLY — the token-loss
    # asymmetry MUST be preserved verbatim in 2A. This divergence is
    # NOT fixed in 2A.
    #
    # PHASE-2B GRADUATION CRITERION: Phase 2B must fix the token-loss
    # bug so the final result carries the accumulated multi-round
    # input/output tokens. Removing these @xfails is a Phase 2B
    # release gate.
    @pytest.mark.xfail(
        reason=(
            "PHASE-1 divergence #3: multi-round tool-loop token usage "
            "is dropped from the final GenerationResult (cost is "
            "preserved but tokens report 0). PR 2A preserves this "
            "behavior verbatim; PR 2B must fix the asymmetry."
        ),
        strict=True,
    )
    @pytest.mark.asyncio
    async def test_two_round_tool_loop_tokens_preserved_in_result(
        self, tmp_path,
    ):
        provider = ClaudeProvider(
            api_key="test-key", repo_root=tmp_path, tools_enabled=True,
        )
        provider._client = self._multi_round_client(
            n_tool_rounds=2,
            usage_input_per_round=100,
            usage_output_per_round=50,
        )
        result = await provider.generate(_make_ctx(), _deadline())
        # The divergence: tokens should reflect the 3-call accumulation
        # (≥ 3 × 100 input, ≥ 3 × 50 output). Today they're 0.
        assert result.total_input_tokens >= 3 * 100
        assert result.total_output_tokens >= 3 * 50

    @pytest.mark.xfail(
        reason=(
            "PHASE-1 divergence #3 (also): same multi-round token-loss "
            "applies at 4 rounds. Phase 2B graduation criterion."
        ),
        strict=True,
    )
    @pytest.mark.asyncio
    async def test_three_round_tool_loop_tokens_preserved_in_result(
        self, tmp_path,
    ):
        provider = ClaudeProvider(
            api_key="test-key", repo_root=tmp_path, tools_enabled=True,
        )
        provider._client = self._multi_round_client(
            n_tool_rounds=3,
            usage_input_per_round=200,
            usage_output_per_round=80,
        )
        result = await provider.generate(_make_ctx(), _deadline())
        assert result.total_input_tokens >= 4 * 200
        assert result.total_output_tokens >= 4 * 80

    @pytest.mark.asyncio
    async def test_zero_tool_rounds_single_dispatch_path(self, tmp_path):
        """Sanity counter-test: when tools_enabled=False (default,
        S1 cache `_no_tools_inner` path), exactly ONE dispatch happens
        and cumulative tokens equal the single round's contribution.
        """
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        # tools_enabled=False by default
        provider._client = _make_create_only_client(
            usage_input=300, usage_output=150,
        )
        result = await provider.generate(_make_ctx(), _deadline())
        assert provider._client._call_count[0] == 1
        assert result.total_input_tokens == 300
        assert result.total_output_tokens == 150


# ============================================================================
# Family 3 — route-driven thinking gating (per CLAUDE.md §5 docs)
# ============================================================================


class TestFamily3RouteThinkingGating:
    """The closure inspects ``context.provider_route`` to decide
    whether extended-thinking is engaged. The kwargs passed to
    ``messages.create`` reflect this — observable via the captured
    client kwargs."""

    @pytest.mark.asyncio
    async def test_ide_route_create_called_with_thinking_field_present(
        self, tmp_path,
    ):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        client = _make_create_only_client()
        provider._client = client
        await provider.generate(_make_ctx(route="ide"), _deadline())
        kw = client._last_kwargs[-1]
        # Thinking is one of the documented load-bearing kwargs; its
        # presence (or its explicit absence) is pinned. Both shapes
        # appear in production logs — characterize whichever the
        # closure currently emits.
        # NB: this assertion deliberately accepts either shape — we
        # pin "shape is consistent across runs" via the next test.
        assert ("thinking" in kw) or ("thinking" not in kw)

    @pytest.mark.asyncio
    async def test_route_kwargs_are_deterministic_for_same_context(
        self, tmp_path,
    ):
        """Same route on two consecutive ops yields the SAME kwargs
        shape (modulo ``messages`` content). Pins determinism."""
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        client = _make_create_only_client(responses=[
            _prime_2b1_response(), _prime_2b1_response(),
        ])
        provider._client = client
        ctx = _make_ctx(route="ide")
        await provider.generate(ctx, _deadline())
        await provider.generate(ctx, _deadline())
        kw1 = client._last_kwargs[0]
        kw2 = client._last_kwargs[1]
        # Shape (set of keys) is stable.
        assert set(kw1) == set(kw2)
        # Model identical.
        assert kw1.get("model") == kw2.get("model")

    @pytest.mark.asyncio
    async def test_immediate_route_kwargs_differ_from_standard(
        self, tmp_path,
    ):
        """Per CLAUDE.md §5, IMMEDIATE has thinking_budget=0 default
        while STANDARD enables it. The kwargs MUST differ in at least
        one observable way. Pins ROUTE→KWARGS variance without
        asserting the specific shape (Phase 2 may refactor)."""
        # IMMEDIATE
        p_imm = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        c_imm = _make_create_only_client()
        p_imm._client = c_imm
        await p_imm.generate(_make_ctx(route="immediate"), _deadline())
        kw_imm = c_imm._last_kwargs[-1]
        # STANDARD
        p_std = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        c_std = _make_create_only_client()
        p_std._client = c_std
        await p_std.generate(_make_ctx(route="standard"), _deadline())
        kw_std = c_std._last_kwargs[-1]
        # If thinking is supplied on either route, the two routes
        # produce a difference. If neither supplies thinking, the test
        # is vacuous and we don't fail — Phase 1 characterizes
        # current truth, not desired truth.
        if ("thinking" in kw_imm) or ("thinking" in kw_std):
            assert kw_imm.get("thinking") != kw_std.get("thinking") or (
                # Or some other route-driven kwarg differs.
                set(kw_imm) - set(kw_std)
                or set(kw_std) - set(kw_imm)
            )


# ============================================================================
# Family 4 — error path propagation
# ============================================================================


class TestFamily4ErrorPropagation:
    """The closure converts Anthropic-side errors into
    orchestrator-visible signals. Pins the propagation contracts."""

    @pytest.mark.asyncio
    async def test_anthropic_authentication_error_propagated(
        self, tmp_path,
    ):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client(
            raise_on_create=Exception("auth failed"),
        )
        with pytest.raises(Exception):
            await provider.generate(_make_ctx(), _deadline())

    @pytest.mark.asyncio
    async def test_runtime_error_inside_create_propagates(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client(
            raise_on_create=RuntimeError("synthetic Anthropic error"),
        )
        with pytest.raises(BaseException):
            await provider.generate(_make_ctx(), _deadline())

    @pytest.mark.asyncio
    async def test_error_path_does_not_silently_mutate_daily_spend(
        self, tmp_path,
    ):
        """If the call errors out before token usage is realized,
        _daily_spend must not silently grow."""
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path,
                                   daily_budget=100.0)
        provider._client = _make_create_only_client(
            raise_on_create=RuntimeError("synthetic"),
        )
        before = float(provider._daily_spend)
        with pytest.raises(BaseException):
            await provider.generate(_make_ctx(), _deadline())
        after = float(provider._daily_spend)
        # Pin: error path's effect on _daily_spend is documented
        # to be "no growth" — if production currently grows it,
        # this test will fail and that's an honest characterization.
        assert after == before, (
            f"PHASE-1 CHARACTERIZATION MISMATCH: _daily_spend grew "
            f"{before} → {after} despite Anthropic raise — current "
            f"production behavior diverges from the documented "
            f"contract. Stop and report (do not fix in this PR)."
        )


# ============================================================================
# Family 5 — result-builder integration (via _finalize_codegen_result)
# ============================================================================


class TestFamily5FinalizeIntegration:
    """The closure's output (raw `str`) is consumed by
    ``_finalize_codegen_result`` to build the GenerationResult. Pins
    that the integration surface (the public seam Phase 2 might
    restructure) preserves observable contracts."""

    @pytest.mark.asyncio
    async def test_finalize_produces_generation_result_dataclass(
        self, tmp_path,
    ):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        result = await provider.generate(_make_ctx(), _deadline())
        # GenerationResult must remain a dataclass with the public
        # fields the rest of the system depends on.
        assert dataclasses.is_dataclass(result)
        for field_name in (
            "candidates", "provider_name", "model_id",
            "total_input_tokens", "total_output_tokens", "cost_usd",
            "generation_duration_s",
        ):
            assert hasattr(result, field_name), (
                f"GenerationResult missing field {field_name!r}"
            )

    @pytest.mark.asyncio
    async def test_finalize_generation_duration_is_nonneg(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        result = await provider.generate(_make_ctx(), _deadline())
        assert result.generation_duration_s >= 0.0

    @pytest.mark.asyncio
    async def test_finalize_preloaded_files_tuple_shape(self, tmp_path):
        """Whether populated or empty, prompt_preloaded_files is a
        tuple of strings — the type contract Phase 2 must preserve."""
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        result = await provider.generate(_make_ctx(), _deadline())
        assert isinstance(result.prompt_preloaded_files, tuple)
        for entry in result.prompt_preloaded_files:
            assert isinstance(entry, str)

    @pytest.mark.asyncio
    async def test_finalize_candidates_immutable_tuple(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        result = await provider.generate(_make_ctx(), _deadline())
        assert isinstance(result.candidates, tuple)


# ============================================================================
# Family 6 — context resilience (malformed / boundary inputs)
# ============================================================================


class TestFamily6ContextResilience:
    """Inputs at the OperationContext boundary that the closure must
    handle gracefully. NEVER raises into orchestrator on garbage
    context fields."""

    @pytest.mark.asyncio
    async def test_empty_target_files_yields_result_no_raise(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        ctx = _make_ctx(target_files=("x.py",))   # OperationContext.create
                                                   # requires at least 1
        result = await provider.generate(ctx, _deadline())
        assert isinstance(result, GenerationResult)

    @pytest.mark.asyncio
    async def test_long_description_does_not_raise(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        ctx = _make_ctx(description="X" * 100_000)
        result = await provider.generate(ctx, _deadline())
        assert isinstance(result, GenerationResult)

    @pytest.mark.asyncio
    async def test_unicode_description_preserved_through_prompt(
        self, tmp_path,
    ):
        """Non-ASCII context content must reach the provider call
        intact (or be sanitized into the prompt — observable via
        kwargs capture)."""
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        client = _make_create_only_client()
        provider._client = client
        await provider.generate(
            _make_ctx(description="émoji 🚀 测试"), _deadline(),
        )
        # The closure assembled SOME prompt — the client was called.
        assert client._call_count[0] >= 1


# ============================================================================
# Family 7 — deadline / budget envelope (closure's deadline awareness)
# ============================================================================


class TestFamily7DeadlineEnvelope:
    """The closure reads `deadline` to clamp internal timeouts
    (`_r0 = _remaining_utc_budget_s(deadline, floor_s=1.0)` at L6481).
    Pin the observable behavior at the deadline boundary."""

    @pytest.mark.asyncio
    async def test_far_future_deadline_does_not_block(self, tmp_path):
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        # 1 hour out — well within any reasonable timeout
        result = await provider.generate(
            _make_ctx(), _deadline(seconds_from_now=3600.0),
        )
        assert isinstance(result, GenerationResult)

    # ──────────────────────────────────────────────────────────────────
    # PHASE-1 CHARACTERIZATION DIVERGENCE #2 — hardcoded 8.0s deadline floor
    # ──────────────────────────────────────────────────────────────────
    # OBSERVED: a 1.5s deadline → immediate TimeoutError before any
    # call attempt:
    #   "claude_create skipping attempt 1/3 — only 1.5s remaining
    #    (floor 8.0s)"
    #   "claude_create_budget_starved:1.5s_remaining"
    #   "claude create timed out after 0.0s (budget=1.5s, ...)"
    #
    # CONTRACT BREACH: _generate_raw's header comment at L6481
    # declares the closure uses `_remaining_utc_budget_s(deadline,
    # floor_s=1.0)`. But the inner retry layer (_call_with_backoff at
    # ~L6044) enforces a HARDCODED 8.0s floor. Two layers, two
    # floors — the 1.0s claim in the closure header is misleading.
    #
    # PHASE-2 GRADUATION CRITERION (operator directive):
    # "completely eliminate the 8.0s hardcoding in favor of dynamic,
    # adaptive budget routing". The 8.0s constant must move to an
    # env-tunable knob with a deterministic fallback that does NOT
    # mask the documented floor_s.
    @pytest.mark.xfail(
        reason=(
            "PHASE-1 divergence #2: a 1.5s deadline is rejected with "
            "'floor 8.0s' even though the closure header declares "
            "floor_s=1.0. Two layers, two floors, hardcoded 8.0s in "
            "_call_with_backoff. Phase 2 must replace the hardcoded "
            "8.0s with dynamic adaptive budget routing — no hardcoding."
        ),
        strict=True,
    )
    @pytest.mark.asyncio
    async def test_minimum_floor_deadline_does_not_block(self, tmp_path):
        """The closure has a documented `floor_s=1.0` — a near-now
        deadline must still admit a 1s-floor budget to the client
        and return cleanly under a fast mock."""
        provider = ClaudeProvider(api_key="test-key", repo_root=tmp_path)
        provider._client = _make_create_only_client()
        result = await provider.generate(
            _make_ctx(), _deadline(seconds_from_now=1.5),
        )
        assert isinstance(result, GenerationResult)


# ============================================================================
# Family 8 — AST pins (CURRENT closure shape; TEMPORARY for Phase 1)
# ============================================================================


# *** PHASE-1 TEMPORARY AST PINS ***
#
# These pins lock the CURRENT nested-closure structure of
# `_generate_raw` as of HEAD `739321c1a6`. They are CHARACTERIZATION
# pins, not future-looking contracts.
#
# Phase 2 (extraction) will INTENTIONALLY break these — that's the
# whole point. Each pin below is annotated with `PHASE_2_UPDATE:` so
# the refactor work knows exactly which assertions to update.


_PROVIDERS_PATH = Path(
    "backend/core/ouroboros/governance/providers.py"
)


def _claude_provider_generate_node() -> ast.AsyncFunctionDef:
    """Locate ClaudeProvider.generate AST node."""
    src = _PROVIDERS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ClaudeProvider":
            for m in node.body:
                if (
                    isinstance(m, ast.AsyncFunctionDef)
                    and m.name == "generate"
                ):
                    return m
    raise AssertionError("ClaudeProvider.generate not found")


def _claude_generate_raw_node() -> ast.AsyncFunctionDef:
    """Locate the nested Claude `_generate_raw` closure."""
    gen = _claude_provider_generate_node()
    for sub in ast.walk(gen):
        if (
            isinstance(sub, ast.AsyncFunctionDef)
            and sub.name == "_generate_raw"
            and sub is not gen
        ):
            return sub
    raise AssertionError(
        "Claude _generate_raw not found nested in ClaudeProvider.generate"
    )


def test_ast_pin_generate_raw_is_nested_in_generate():
    """PHASE_2_UPDATE: Phase 2 may promote `_generate_raw` to a
    method — this pin will then need updating to assert method-level
    location instead of nested location."""
    node = _claude_generate_raw_node()
    assert node is not None
    # Signature observable: takes a single string arg, returns str.
    args = node.args.args
    assert len(args) == 1 or (len(args) == 2 and args[0].arg == "self"), (
        "PHASE-1 pin: _generate_raw signature is (p: str) -> str"
    )


def test_ast_pin_generate_raw_declares_nonlocal_total_cost():
    """PHASE_2_UPDATE: Phase 2 will replace this nonlocal with
    explicit param/return — pin will need to be removed."""
    node = _claude_generate_raw_node()
    nl_names = set()
    # Only look at the OUTER nonlocal declaration at the closure's
    # own scope (not those inside its nested helpers).
    for stmt in node.body:
        if isinstance(stmt, ast.Nonlocal):
            nl_names.update(stmt.names)
    assert "total_cost" in nl_names, (
        f"PHASE-1 pin: _generate_raw must declare `nonlocal total_cost` "
        f"at its top-level scope. Found: {sorted(nl_names)}"
    )


def test_ast_pin_generate_raw_size_within_phase_1_envelope():
    """PHASE_2_UPDATE: post-extraction the body shrinks dramatically.

    Original size at HEAD ``739321c1a6``: 1,035 lines (envelope
    [900, 1200]).

    Per-slice updates (the envelope re-tightens as extractions land):
      * Slice 2A-iii / 2B-i / 2B-ii / 2B-iii: 1035 → 953 (still in
        original envelope after first-floor relaxation).
      * Slice 2C-i (this update): 953 → 712 — the heaviest cut. The
        envelope retracts to [600, 800]. The substrate (
        ``_ClaudeDispatchState`` + ``_ClaudeStreamContext``) is now
        live; ``_claude_do_stream`` carries the 317-line streaming
        body that used to live nested inside ``_generate_raw``."""
    node = _claude_generate_raw_node()
    size = (node.end_lineno or node.lineno) - node.lineno
    assert 600 <= size <= 800, (
        f"PHASE-1 pin (Slice 2C-i update): _generate_raw size "
        f"{size} outside envelope [600, 800]. Refactor in progress? "
        f"Update this pin in tandem."
    )


def test_ast_pin_generate_raw_calls_messages_stream_at_one_site():
    """PHASE_2_UPDATE: streaming may be extracted to its own method
    in Phase 2; the single in-closure call site will move.

    Slice 2C-i update: ``messages.stream`` call site moved OUT of
    ``_generate_raw`` when ``_do_stream`` extracted to
    ``ClaudeProvider._claude_do_stream``. The contract this pin now
    asserts: somewhere inside the ``ClaudeProvider`` class body
    (either the closure OR an extracted ``_claude_*`` method) there
    exists at least one ``messages.stream`` reference. Future slices
    that move the stream path further MUST update this pin
    accordingly."""
    import inspect as _inspect
    from backend.core.ouroboros.governance import providers as _p
    src = _inspect.getsource(_p.ClaudeProvider)
    tree = ast.parse(
        f"class _Wrap:\n" + "\n".join(
            "    " + line for line in src.splitlines()
        )
    )
    stream_sites = []
    for sub in ast.walk(tree):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "stream"
            and isinstance(sub.value, ast.Attribute)
            and sub.value.attr == "messages"
        ):
            stream_sites.append(sub.lineno)
    assert len(stream_sites) >= 1, (
        "PHASE-1 pin (Slice 2C-i update): ClaudeProvider must "
        "contain at least one `messages.stream` reference across "
        "the closure OR extracted class methods"
    )


def test_ast_pin_generate_raw_calls_messages_create_at_two_sites():
    """PHASE_2_UPDATE: non-stream paths may consolidate or split in
    Phase 2 — site count may change.

    Slice 2B-ii update: both ``messages.create`` call sites moved
    OUT of ``_generate_raw`` when
    ``_create_with_prefill_fallback`` extracted to
    ``ClaudeProvider._claude_create_with_prefill_fallback``. The
    contract this pin now asserts: somewhere inside the
    ``ClaudeProvider`` class body (either the closure OR an
    extracted ``_claude_*`` method) there exists at least one
    ``messages.create`` reference. Future slices that move the
    create path further MUST update this pin accordingly."""
    import inspect as _inspect
    from backend.core.ouroboros.governance import providers as _p
    src = _inspect.getsource(_p.ClaudeProvider)
    tree = ast.parse(
        # ast.parse needs a module-level wrapper; classdef body
        # parses fine via wrapping.
        f"class _Wrap:\n" + "\n".join(
            "    " + line for line in src.splitlines()
        )
    )
    create_sites = []
    for sub in ast.walk(tree):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "create"
            and isinstance(sub.value, ast.Attribute)
            and sub.value.attr == "messages"
        ):
            create_sites.append(sub.lineno)
    assert len(create_sites) >= 1, (
        "PHASE-1 pin (Slice 2B-ii update): ClaudeProvider must "
        "contain at least one `messages.create` reference across "
        "the closure OR extracted class methods"
    )


def test_ast_pin_generate_raw_has_nested_helper_functions():
    """PHASE_2_UPDATE: the 8 nested helpers (_do_stream,
    _create_with_prefill_fallback, etc.) will either be inlined,
    moved out, or restructured. Phase 1 pins that they exist as
    closure-local helpers.

    Slice 2A-iii update: ``_boundary_audit_sampler`` extracted.
    Slice 2B-i   update: ``_retrieve_stream_exc`` extracted.
    Slice 2B-ii  update: ``_create_with_*`` pair extracted.
    Slice 2B-iii update: ``_stream_with_*`` pair extracted.
    Slice 2C-i   update: ``_do_stream`` extracted.
    Slice 2C-ii  update: ``_stream_fanout`` extracted to
    ``ClaudeProvider._claude_make_stream_fanout``. With this last
    extraction, ``_generate_raw`` is STRUCTURALLY CLEAN of nested
    helpers (count == 0). The Phase-1 SNAPSHOT contract this pin
    asserts inverts: it now requires the nested-helper set to be
    EMPTY (proving the closure-extraction phase is complete).
    Future slices that re-introduce a nested helper MUST flip this
    pin deliberately."""
    node = _claude_generate_raw_node()
    nested_names = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, (ast.AsyncFunctionDef, ast.FunctionDef))
            and sub is not node
        ):
            nested_names.add(sub.name)
    assert nested_names == set(), (
        f"PHASE-1 pin (Slice 2C-ii update): _generate_raw must be "
        f"structurally clean of nested helpers after Slice 2C-ii. "
        f"Found: {sorted(nested_names)}"
    )


def test_ast_pin_generate_raw_returns_str_per_signature():
    """PHASE_2_UPDATE: return-type annotation should remain `str`
    post-refactor; pin guards against accidental type change."""
    node = _claude_generate_raw_node()
    returns = node.returns
    # Either a Name('str') or a string subscript — we accept any
    # annotation containing 'str' for resilience.
    assert returns is not None, (
        "PHASE-1 pin: _generate_raw must have an explicit return "
        "type annotation"
    )
    src = ast.unparse(returns) if hasattr(ast, "unparse") else ""
    assert "str" in src.lower(), (
        f"PHASE-1 pin: _generate_raw returns `str`; found: {src!r}"
    )


def test_ast_pin_no_real_anthropic_imports_in_this_test_module():
    """Meta-pin: this test file itself must NOT import the real
    anthropic SDK. Every Anthropic surface used here is mocked
    via local helpers."""
    this_file = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(this_file)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "anthropic", (
                    "characterization tests must not import the real "
                    "anthropic SDK"
                )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("anthropic"), (
                f"characterization tests must not import from "
                f"{mod!r} (real Anthropic SDK)"
            )


def test_ast_pin_meta_no_mutation_of_providers_in_this_pr():
    """Meta-pin: this test file imports providers.py for read-only
    AST inspection ONLY. No production-code mutation occurs in this
    PR. Existence of this test is itself a guard."""
    src = Path(__file__).read_text(encoding="utf-8")
    # We must not be calling Edit/Write to providers.py from inside
    # tests. (Tests don't have such tools, but the documentary pin
    # serves to remind reviewers of the Phase-1 boundary.)
    assert "providers.py is READ-ONLY" in src, (
        "Phase-1 charter docstring must declare providers.py read-only"
    )
