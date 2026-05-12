"""Treefinement Production Wiring Phase A — extracted primitive spine.

Pins the contract of ``RepairEngine._generate_repair_candidate``,
the single-source generation primitive composed by:

  * The legacy LINEAR FSM (``_run_inner``) — passes
    ``hypothesis_seed=None``; semantics MUST remain byte-equivalent
    to the pre-Phase-A inline GENERATE block.
  * The Phase C ``ProductionBranchGenerator`` — passes a
    ``hypothesis_seed`` carrying parent branch context.

Invariants covered
------------------
* Provider call composes ``self._prime.generate(ctx, deadline,
  repair_context=repair_context)`` exactly — no extra arguments,
  no extra wrapping.
* Provider exception → ``CandidateGenerationResult(candidate=None,
  stop_reason="generate_error:<TypeName>")`` quarantine.
* Empty candidates list → ``stop_reason="empty_candidates"`` with
  provider attribution preserved (None sentinels).
* Successful response → ``candidate=dict(gen_result.candidates[0])``
  + ``model_id`` / ``provider_name`` extracted via getattr-with-None-
  sentinel (preserves "no value supplied" vs "explicit empty
  string" distinction — load-bearing for byte-equivalent ``_run_inner``
  semantics).
* ``asyncio.CancelledError`` propagates (orchestrator-handled
  POSTMORTEM contract).
* ``hypothesis_seed`` is a Phase C composition hook — Phase A
  accepts but does not consume; signature stability is the AST
  pin verified by ``test_signature_pin``.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance.repair_engine import (
    CandidateGenerationResult,
    RepairBudget,
    RepairEngine,
)


# ===========================================================================
# Test fixtures
# ===========================================================================


class _StubGenResult:
    """Mimics the provider response shape consumed by the primitive."""

    def __init__(
        self,
        *,
        candidates: List[Any],
        model_id: Any = ...,
        provider_name: Any = ...,
    ):
        self.candidates = candidates
        if model_id is not ...:
            self.model_id = model_id
        if provider_name is not ...:
            self.provider_name = provider_name


class _StubProvider:
    """Records calls + returns canned results / raises canned exceptions."""

    def __init__(
        self,
        *,
        result: Optional[_StubGenResult] = None,
        raises: Optional[BaseException] = None,
    ):
        self.result = result
        self.raises = raises
        self.calls: List[dict] = []

    async def generate(self, ctx, pipeline_deadline, *, repair_context):
        self.calls.append({
            "ctx": ctx,
            "pipeline_deadline": pipeline_deadline,
            "repair_context": repair_context,
        })
        if self.raises is not None:
            raise self.raises
        return self.result


def _make_engine(*, provider: _StubProvider) -> RepairEngine:
    return RepairEngine(
        budget=RepairBudget(),
        prime_provider=provider,
        repo_root=Path("."),
    )


def _invoke(
    engine: RepairEngine,
    *,
    repair_context: Any = "stub-repair-context",
    hypothesis_seed: Optional[str] = None,
):
    return asyncio.run(engine._generate_repair_candidate(
        ctx=object(),
        pipeline_deadline=datetime.now(timezone.utc),
        repair_context=repair_context,
        hypothesis_seed=hypothesis_seed,
    ))


# ===========================================================================
# Provider invocation pin — exactly the call shape _run_inner used pre-A
# ===========================================================================


def test_provider_called_with_canonical_signature():
    """``self._prime.generate(ctx, pipeline_deadline,
    repair_context=repair_context)`` — no extra args, no wrapping.
    Drift here breaks byte-equivalent semantics with the pre-Phase-A
    inline block."""
    provider = _StubProvider(
        result=_StubGenResult(candidates=[{"file_path": "x"}]),
    )
    _invoke(_make_engine(provider=provider))
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["repair_context"] == "stub-repair-context"
    assert call["pipeline_deadline"] is not None


# ===========================================================================
# Successful generation — happy path
# ===========================================================================


def test_success_returns_dict_candidate():
    """Returned candidate MUST be a fresh dict (not an alias) so
    callers can mutate without affecting the provider's state."""
    original = {"file_path": "foo.py", "unified_diff": "--- a\n"}
    provider = _StubProvider(
        result=_StubGenResult(
            candidates=[original],
            model_id="claude-test",
            provider_name="claude",
        ),
    )
    out = _invoke(_make_engine(provider=provider))
    assert isinstance(out, CandidateGenerationResult)
    assert out.candidate == original
    assert out.candidate is not original  # MUST be a copy
    assert out.model_id == "claude-test"
    assert out.provider_name == "claude"
    assert out.stop_reason is None


def test_success_preserves_first_candidate_only():
    """Multi-candidate response → first candidate consumed (matches
    pre-Phase-A `gen_result.candidates[0]` semantic)."""
    provider = _StubProvider(
        result=_StubGenResult(
            candidates=[{"a": 1}, {"a": 2}, {"a": 3}],
        ),
    )
    out = _invoke(_make_engine(provider=provider))
    assert out.candidate == {"a": 1}


# ===========================================================================
# getattr-with-None-sentinel semantic — load-bearing for _run_inner
# ===========================================================================


def test_missing_model_id_attribute_yields_none_sentinel():
    """When provider response has NO ``model_id`` attribute, the
    primitive returns ``None`` (sentinel). _run_inner uses this to
    preserve previous model_id (matches pre-Phase-A
    ``getattr(gen_result, "model_id", model_id)`` fallback)."""
    provider = _StubProvider(
        result=_StubGenResult(candidates=[{"x": 1}]),
        # No model_id / provider_name attributes set
    )
    out = _invoke(_make_engine(provider=provider))
    assert out.model_id is None, (
        "Sentinel None when attribute missing — REQUIRED for "
        "byte-equivalent getattr-with-fallback in _run_inner"
    )
    assert out.provider_name is None


def test_explicit_empty_string_model_id_passes_through():
    """When provider explicitly sets ``model_id=""``, the primitive
    returns ``""`` (NOT None). _run_inner overwrites with this empty
    string — matches the pre-Phase-A attribute-exists behavior."""
    provider = _StubProvider(
        result=_StubGenResult(
            candidates=[{"x": 1}], model_id="", provider_name="",
        ),
    )
    out = _invoke(_make_engine(provider=provider))
    assert out.model_id == "", (
        "Empty string MUST pass through — distinguishes 'attribute "
        "exists with empty value' from 'attribute missing'"
    )
    assert out.provider_name == ""


# ===========================================================================
# Failure quarantine — provider exception → stop_reason
# ===========================================================================


def test_provider_runtime_error_quarantines():
    provider = _StubProvider(raises=RuntimeError("provider exploded"))
    out = _invoke(_make_engine(provider=provider))
    assert out.candidate is None
    assert out.stop_reason == "generate_error:RuntimeError"
    assert out.model_id is None  # no value to report on failure
    assert out.provider_name is None


def test_provider_value_error_quarantines():
    """Verify the exception class name appears in stop_reason
    (operator-greppable failure mode)."""
    provider = _StubProvider(raises=ValueError("bad config"))
    out = _invoke(_make_engine(provider=provider))
    assert out.candidate is None
    assert out.stop_reason == "generate_error:ValueError"


def test_empty_candidates_list_quarantines():
    """Provider returns gen_result with empty candidates → quarantine
    to ``stop_reason="empty_candidates"`` (matches pre-Phase-A
    ``if not gen_result.candidates: return _stopped("empty_candidates")``
    in _run_inner)."""
    provider = _StubProvider(result=_StubGenResult(candidates=[]))
    out = _invoke(_make_engine(provider=provider))
    assert out.candidate is None
    assert out.stop_reason == "empty_candidates"


# ===========================================================================
# CancelledError propagation — orchestrator POSTMORTEM contract
# ===========================================================================


def test_cancellation_propagates():
    """CancelledError MUST propagate (orchestrator handles POSTMORTEM).
    All other exceptions quarantine. Mirrors _run_inner discipline."""
    provider = _StubProvider(raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        _invoke(_make_engine(provider=provider))


# ===========================================================================
# hypothesis_seed parameter — Phase C composition hook
# ===========================================================================


def test_hypothesis_seed_accepted_phase_a_no_op():
    """Phase A: hypothesis_seed parameter accepted but explicitly
    unused (Phase C will thread it through provider extensions OR
    via prompt-injection layer). Verify None and non-None both work
    without affecting current behavior."""
    provider = _StubProvider(
        result=_StubGenResult(candidates=[{"x": 1}]),
    )
    out_none = _invoke(
        _make_engine(provider=provider), hypothesis_seed=None,
    )
    provider.calls.clear()
    out_with = _invoke(
        _make_engine(provider=provider),
        hypothesis_seed="parent-strategy: rename foo to bar",
    )
    # Both invocations succeed identically — Phase A treats seed as
    # transparent metadata.
    assert out_none.candidate == out_with.candidate


# ===========================================================================
# Signature pin — Phase B/C wiring depends on this contract
# ===========================================================================


def test_signature_pin():
    """Pin the primitive's signature so Phase C wiring (in
    ProductionBranchGenerator) doesn't break silently when the
    contract drifts."""
    sig = inspect.signature(RepairEngine._generate_repair_candidate)
    # Required params (positional + kw)
    for name in (
        "self", "ctx", "pipeline_deadline",
        "repair_context", "hypothesis_seed",
    ):
        assert name in sig.parameters, (
            f"signature MUST expose {name!r} — Phase B/C wiring "
            "depends on this contract"
        )
    # repair_context MUST be keyword-only
    rc = sig.parameters["repair_context"]
    assert rc.kind == inspect.Parameter.KEYWORD_ONLY
    # hypothesis_seed MUST be keyword-only with None default
    hs = sig.parameters["hypothesis_seed"]
    assert hs.kind == inspect.Parameter.KEYWORD_ONLY
    assert hs.default is None
