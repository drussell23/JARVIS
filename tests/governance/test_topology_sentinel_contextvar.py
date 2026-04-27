"""Slice 3.6 regression spine — ContextVar-based DW model override.

Pins the fix for the silent FrozenInstanceError that bit session
bt-2026-04-27-203746 (5 healthy preflight log lines + 0 sentinel
dispatch attempts because Slice 3's ``setattr(ctx, "_dw_model_override",
model_id)`` raised on the frozen ``OperationContext`` dataclass).

Three tests families:

  §1 ContextVar primitive — set/get/reset behavior, default None,
     async-task isolation, no-leak across .reset
  §2 DoublewordProvider integration — ``_resolve_effective_model``
     reads the ContextVar BEFORE the topology mapping
  §3 Dispatcher integration — source-level pin that the dispatcher
     uses set_dw_model_override + reset_dw_model_override (not the
     old setattr pattern)
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Optional

import pytest

from backend.core.ouroboros.governance import (
    candidate_generator as cg,
    doubleword_provider as dwp,
    topology_sentinel as ts,
)


# ===========================================================================
# §1 — ContextVar primitive
# ===========================================================================


def test_dw_model_override_var_default_is_none() -> None:
    # Reset to a clean state to guard against suite-order leakage.
    try:
        ts.DW_MODEL_OVERRIDE_VAR.set(None)
    except Exception:
        pass
    assert ts.get_dw_model_override() is None


def test_set_dw_model_override_returns_token() -> None:
    token = ts.set_dw_model_override("moonshotai/Kimi-K2.6")
    assert token is not None
    # Token must be a contextvars.Token shape (defensive duck-type
    # rather than isinstance to avoid tying to private API).
    assert hasattr(token, "var") or hasattr(token, "_var")
    ts.reset_dw_model_override(token)


def test_set_then_get_round_trip() -> None:
    token = ts.set_dw_model_override("zai-org/GLM-5.1-FP8")
    assert ts.get_dw_model_override() == "zai-org/GLM-5.1-FP8"
    ts.reset_dw_model_override(token)
    assert ts.get_dw_model_override() is None


def test_reset_with_invalid_token_does_not_raise() -> None:
    """``reset_dw_model_override`` must NEVER raise — caller might
    pass a stale token from a prior task. Defensive contract."""
    # A bogus object that can't be a real Token.
    ts.reset_dw_model_override(object())  # must not raise


def test_set_to_none_then_get_returns_none() -> None:
    token = ts.set_dw_model_override(None)
    assert ts.get_dw_model_override() is None
    ts.reset_dw_model_override(token)


@pytest.mark.asyncio
async def test_async_task_isolation() -> None:
    """The marquee correctness pin — the ContextVar must give each
    asyncio task its own value. If the dispatcher walks 4 models in
    parallel for STANDARD/COMPLEX (or just 2 BG ops run concurrently),
    no two tasks should see each other's overrides."""

    async def task_with_override(model_id: str) -> Optional[str]:
        token = ts.set_dw_model_override(model_id)
        # Yield to let other tasks run with their own values.
        await asyncio.sleep(0.001)
        observed = ts.get_dw_model_override()
        ts.reset_dw_model_override(token)
        return observed

    results = await asyncio.gather(
        task_with_override("model_a"),
        task_with_override("model_b"),
        task_with_override("model_c"),
    )
    # Each task observes its OWN value, not a sibling's.
    assert results == ["model_a", "model_b", "model_c"]


@pytest.mark.asyncio
async def test_async_no_leak_after_reset() -> None:
    """After a task resets its override, a sibling task that runs
    later sees None (or its own value, not the prior)."""

    async def task_a() -> None:
        token = ts.set_dw_model_override("model_a")
        ts.reset_dw_model_override(token)

    async def task_b() -> Optional[str]:
        return ts.get_dw_model_override()

    await task_a()
    observed = await task_b()
    # task_b sees None — it never set anything, and task_a's value
    # didn't leak.
    assert observed is None


# ===========================================================================
# §2 — DoublewordProvider._resolve_effective_model integration
# ===========================================================================


class _FakeCtx:
    """Minimal duck-type stub for OperationContext (frozen dataclass).
    The provider's resolver reads ``ctx.provider_route`` so the stub
    just needs that attribute."""

    def __init__(self, provider_route: str = "background") -> None:
        self.provider_route = provider_route


def test_resolver_reads_contextvar_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the ContextVar is set, the resolver returns it WITHOUT
    consulting the topology — async-safe per-attempt routing."""
    # Construct a fake provider with a default model. The resolver is
    # an instance method but doesn't need the full provider state.
    class _StubProvider:
        _model = "default-model-id"
        _resolve_effective_model = (
            dwp.DoublewordProvider._resolve_effective_model
        )

    p = _StubProvider()
    ctx = _FakeCtx("background")
    token = ts.set_dw_model_override("Qwen/Qwen3.6-35B-A3B-FP8")
    try:
        resolved = p._resolve_effective_model(ctx)
        assert resolved == "Qwen/Qwen3.6-35B-A3B-FP8"
    finally:
        ts.reset_dw_model_override(token)


def test_resolver_falls_back_when_contextvar_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the ContextVar is None (default), the resolver falls
    through to the topology mapping → self._model. Existing v1
    callers must keep working."""

    class _StubProvider:
        _model = "default-model-id"
        _resolve_effective_model = (
            dwp.DoublewordProvider._resolve_effective_model
        )

    p = _StubProvider()
    ctx = _FakeCtx("")  # no route → returns self._model
    # Defensive — make sure ContextVar is unset.
    try:
        ts.DW_MODEL_OVERRIDE_VAR.set(None)
    except Exception:
        pass
    resolved = p._resolve_effective_model(ctx)
    assert resolved == "default-model-id"


def test_resolver_empty_string_override_falls_through() -> None:
    """An empty-string override is treated as 'no override' to avoid
    routing to a malformed model_id. Defense against bad operator
    input or partial cleanup."""

    class _StubProvider:
        _model = "default-model-id"
        _resolve_effective_model = (
            dwp.DoublewordProvider._resolve_effective_model
        )

    p = _StubProvider()
    ctx = _FakeCtx("")
    token = ts.set_dw_model_override("")
    try:
        resolved = p._resolve_effective_model(ctx)
        # Empty string fails the `isinstance(s, str) and s` check;
        # falls through to topology / self._model.
        assert resolved == "default-model-id"
    finally:
        ts.reset_dw_model_override(token)


def test_resolver_ctx_without_dw_model_override_attr_no_raise() -> None:
    """The resolver must NOT depend on the (now-removed)
    ``ctx._dw_model_override`` attribute. Pure ContextVar consultation."""

    class _StubProvider:
        _model = "default-model-id"
        _resolve_effective_model = (
            dwp.DoublewordProvider._resolve_effective_model
        )

    p = _StubProvider()
    ctx = _FakeCtx("standard")  # no _dw_model_override attr
    try:
        ts.DW_MODEL_OVERRIDE_VAR.set(None)
    except Exception:
        pass
    # Must not raise.
    resolved = p._resolve_effective_model(ctx)
    # Without ContextVar set, falls through to topology — for
    # 'standard' route which isn't single-model under v1, returns
    # self._model.
    assert isinstance(resolved, str)


# ===========================================================================
# §3 — Dispatcher integration (source-level pins)
# ===========================================================================


def test_dispatcher_uses_set_override_helper() -> None:
    """Source-level pin: ``_dispatch_via_sentinel`` must use
    ``set_dw_model_override`` (the helper) not raw
    ``DW_MODEL_OVERRIDE_VAR.set`` so the function-level entry point
    is stable."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "_set_override(model_id)" in src or (
        "set_dw_model_override" in src
    )


def test_dispatcher_uses_reset_override_in_finally() -> None:
    """Source-level pin: the per-attempt try block must reset the
    ContextVar in a finally block so siblings + the post-loop
    cascade never see a stale override."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    # Look for either the helper alias or the public name.
    assert "_reset_override" in src or "reset_dw_model_override" in src
    # And it must be inside a finally block.
    assert "finally:" in src
    # Ordering: finally must follow the try that contains the DW call.
    finally_idx = src.index("finally:")
    set_idx = src.index("_set_override")
    assert finally_idx > set_idx


def test_dispatcher_no_longer_setattrs_dw_model_override() -> None:
    """The Slice 3 setattr-on-frozen-ctx pattern is the bug we're
    closing. Must be entirely gone from the dispatcher's executable
    code. We strip docstring + comments before checking so a
    historical reference in prose doesn't trip the test."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    # Remove docstring (everything between the first """ and second """).
    if '"""' in src:
        first = src.index('"""')
        rest = src[first + 3:]
        second = rest.index('"""')
        src = src[:first] + src[first + 3 + second + 3:]
    # Strip comment lines.
    code_lines = [
        line for line in src.splitlines()
        if not line.strip().startswith("#")
    ]
    code_only = "\n".join(code_lines)
    assert 'setattr(context, "_dw_model_override"' not in code_only
    # The provider-side ``getattr(ctx, "_dw_model_override", ...)``
    # was also removed — verified by separate test in §3 above.


def test_dispatcher_imports_override_helpers() -> None:
    """Source-level pin: dispatcher imports
    ``set_dw_model_override`` + ``reset_dw_model_override`` (or
    aliases) at the top of the function."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    assert "set_dw_model_override" in src
    assert "reset_dw_model_override" in src


def test_provider_resolver_imports_get_helper() -> None:
    """Source-level pin: provider's ``_resolve_effective_model``
    imports + calls ``get_dw_model_override`` (the public read API)."""
    src = inspect.getsource(dwp.DoublewordProvider._resolve_effective_model)
    assert "get_dw_model_override" in src


def test_provider_resolver_no_longer_reads_ctx_attr() -> None:
    """The Slice 3 ``getattr(ctx, "_dw_model_override", None)`` is
    gone — pure ContextVar consultation."""
    src = inspect.getsource(dwp.DoublewordProvider._resolve_effective_model)
    assert 'getattr(ctx, "_dw_model_override"' not in src


# ===========================================================================
# §4 — End-to-end async simulation
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_dispatcher_resolver_round_trip() -> None:
    """Simulate the dispatcher's set → provider's read → reset cycle.
    Pins the boundary contract end-to-end."""

    class _StubProvider:
        _model = "default"
        _resolve_effective_model = (
            dwp.DoublewordProvider._resolve_effective_model
        )

    provider = _StubProvider()
    ctx = _FakeCtx("background")

    # Step 1: dispatcher sets override.
    token = ts.set_dw_model_override("Qwen/Qwen3.6-35B-A3B-FP8")
    # Step 2: provider reads it.
    resolved = provider._resolve_effective_model(ctx)
    assert resolved == "Qwen/Qwen3.6-35B-A3B-FP8"
    # Step 3: dispatcher resets after attempt.
    ts.reset_dw_model_override(token)
    # Step 4: subsequent provider call (e.g. on the cascade-to-Claude
    # path) doesn't see a stale override.
    resolved_after = provider._resolve_effective_model(ctx)
    assert resolved_after != "Qwen/Qwen3.6-35B-A3B-FP8"


@pytest.mark.asyncio
async def test_e2e_two_concurrent_attempts_isolated() -> None:
    """Two BG ops dispatch in parallel — each picks a different
    model — they MUST NOT see each other's overrides."""

    class _StubProvider:
        _model = "default"
        _resolve_effective_model = (
            dwp.DoublewordProvider._resolve_effective_model
        )

    provider = _StubProvider()
    ctx = _FakeCtx("background")

    async def attempt(model_id: str) -> str:
        token = ts.set_dw_model_override(model_id)
        try:
            await asyncio.sleep(0.001)  # let other task interleave
            return provider._resolve_effective_model(ctx)
        finally:
            ts.reset_dw_model_override(token)

    results = await asyncio.gather(
        attempt("Qwen/Qwen3.6-35B-A3B-FP8"),
        attempt("moonshotai/Kimi-K2.6"),
    )
    assert results == [
        "Qwen/Qwen3.6-35B-A3B-FP8",
        "moonshotai/Kimi-K2.6",
    ]


# ===========================================================================
# §5 — Module export contract
# ===========================================================================


def test_export_get_set_reset_override() -> None:
    assert "DW_MODEL_OVERRIDE_VAR" in ts.__all__
    assert "get_dw_model_override" in ts.__all__
    assert "set_dw_model_override" in ts.__all__
    assert "reset_dw_model_override" in ts.__all__


def test_helper_signatures_pinned() -> None:
    # set returns Any (the Token); get returns Optional[str]; reset
    # takes any token.
    sig_set = inspect.signature(ts.set_dw_model_override)
    sig_get = inspect.signature(ts.get_dw_model_override)
    sig_reset = inspect.signature(ts.reset_dw_model_override)
    assert len(sig_set.parameters) == 1
    assert len(sig_get.parameters) == 0
    assert len(sig_reset.parameters) == 1
