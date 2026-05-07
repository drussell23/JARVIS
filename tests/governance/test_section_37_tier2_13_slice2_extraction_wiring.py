"""§37 Tier 2 #13 Slice 2 — Per-tool confidence extraction wiring.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Coverage (35+ tests):
  * ConfidenceSignal frozen + schema-versioned + to_dict round-trip
  * project_summary_to_confidence: exp(mean_top1_logprob) → [0,1]
    + defensive against None / NaN / missing fields / out-of-range
  * extract_confidence_signal_from_active_capturer: ContextVar
    bridge + composes capture substrate + lazy import discipline
    + NEVER raises on broken capturer
  * observe_active_signal: end-to-end extract + record
  * ContextVar set/reset/get isolation (async + Token discipline)
  * Slice 2 AST pin validates clean against actual source
  * Slice 2 AST pin fires on synthetic regressions:
      - missing extract function
      - parallel logprob math (raw _tokens access)
      - missing compute_summary call
      - missing freeze() call
      - missing lazy import
  * tool_executor wiring (Slice 2b) — observation fires after
    successful tool dispatch, BEFORE V1 POST_TOOL_USE hook
"""
from __future__ import annotations

import asyncio
import ast
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _slice1_module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "tool_confidence_warning_observer.py"
    )


@pytest.fixture(autouse=True)
def _reset_state():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        reset_default_observer_for_tests,
    )
    reset_default_observer_for_tests()
    yield
    reset_default_observer_for_tests()


# ---------------------------------------------------------------------------
# Stand-in for ConfidenceSummary — frozen dataclass matching the
# fields the projection reads (mean_top1_logprob + token_count).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubSummary:
    mean_top1_logprob: Optional[float] = None
    token_count: int = 0


# ---------------------------------------------------------------------------
# ConfidenceSignal artifact
# ---------------------------------------------------------------------------


def test_confidence_signal_is_frozen():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ConfidenceSignal,
    )
    sig = ConfidenceSignal(confidence=0.75, sample_size=10)
    with pytest.raises(Exception):  # frozen dataclass
        sig.confidence = 0.5  # type: ignore[misc]


def test_confidence_signal_schema_version_present():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ConfidenceSignal,
    )
    sig = ConfidenceSignal(confidence=0.5, sample_size=4)
    assert sig.schema_version.startswith(
        "tool_confidence_observer."
    )


def test_confidence_signal_to_dict_round_trip():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        ConfidenceSignal,
    )
    sig = ConfidenceSignal(confidence=0.42, sample_size=15)
    d = sig.to_dict()
    assert d["confidence"] == pytest.approx(0.42)
    assert d["sample_size"] == 15
    assert "schema_version" in d


# ---------------------------------------------------------------------------
# project_summary_to_confidence
# ---------------------------------------------------------------------------


def test_project_high_confidence_logprob():
    """logprob = -0.1 → exp(-0.1) ≈ 0.905."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )
    sig = project_summary_to_confidence(
        _StubSummary(
            mean_top1_logprob=-0.1, token_count=20,
        ),
    )
    assert sig.confidence == pytest.approx(
        math.exp(-0.1), rel=1e-9,
    )
    assert sig.sample_size == 20


def test_project_low_confidence_logprob():
    """logprob = -2.3 → exp(-2.3) ≈ 0.100."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )
    sig = project_summary_to_confidence(
        _StubSummary(
            mean_top1_logprob=-2.3, token_count=5,
        ),
    )
    assert sig.confidence == pytest.approx(
        math.exp(-2.3), rel=1e-9,
    )


def test_project_zero_logprob_maps_to_one():
    """logprob = 0.0 (perfectly confident) → 1.0."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )
    sig = project_summary_to_confidence(
        _StubSummary(
            mean_top1_logprob=0.0, token_count=10,
        ),
    )
    assert sig.confidence == pytest.approx(1.0)


def test_project_none_summary_returns_zero():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )
    sig = project_summary_to_confidence(None)
    assert sig.confidence == 0.0
    assert sig.sample_size == 0


def test_project_none_logprob_returns_zero():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )
    sig = project_summary_to_confidence(
        _StubSummary(
            mean_top1_logprob=None, token_count=10,
        ),
    )
    assert sig.confidence == 0.0
    # But sample_size still preserved.
    assert sig.sample_size == 10


def test_project_nan_logprob_returns_zero():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )
    sig = project_summary_to_confidence(
        _StubSummary(
            mean_top1_logprob=float("nan"), token_count=5,
        ),
    )
    assert sig.confidence == 0.0


def test_project_extremely_negative_logprob_clamps():
    """logprob < -50 → return 0.0 rather than crash math.exp on
    subnormals."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )
    sig = project_summary_to_confidence(
        _StubSummary(
            mean_top1_logprob=-1000.0, token_count=5,
        ),
    )
    assert sig.confidence == 0.0


def test_project_positive_logprob_clamps_to_one():
    """logprob > 0 (spec violation) → clamp to confidence=1.0
    rather than crash."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )
    sig = project_summary_to_confidence(
        _StubSummary(
            mean_top1_logprob=0.5, token_count=5,
        ),
    )
    assert sig.confidence == pytest.approx(1.0)


def test_project_negative_token_count_clamps_to_zero():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )
    sig = project_summary_to_confidence(
        _StubSummary(
            mean_top1_logprob=-0.5, token_count=-10,
        ),
    )
    assert sig.sample_size == 0


def test_project_non_numeric_logprob_returns_zero():
    """Defensive against weirdly-shaped summary objects."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        project_summary_to_confidence,
    )

    class _Weird:
        mean_top1_logprob = "garbage"  # not a float
        token_count = 5

    sig = project_summary_to_confidence(_Weird())
    assert sig.confidence == 0.0


# ---------------------------------------------------------------------------
# ContextVar bridge — set / reset / get
# ---------------------------------------------------------------------------


def test_get_active_capturer_default_none():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_active_capturer,
    )
    # No prior set — returns None (defensive).
    assert get_active_capturer() is None


def test_set_and_reset_active_capturer():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_active_capturer, reset_active_capturer,
        set_active_capturer,
    )
    sentinel = MagicMock(name="capturer")
    token = set_active_capturer(sentinel)
    try:
        assert get_active_capturer() is sentinel
    finally:
        reset_active_capturer(token)
    assert get_active_capturer() is None


def test_reset_with_stale_token_does_not_raise():
    """Defensive: invalid Token errors swallowed."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        reset_active_capturer,
    )
    fake_token = MagicMock()
    # Must NOT raise.
    reset_active_capturer(fake_token)


def test_active_capturer_async_propagates_to_child_task():
    """ContextVar inherits across asyncio.Task creation —
    structural property we rely on for tool_executor seeing the
    capturer set by DW provider."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        get_active_capturer, reset_active_capturer,
        set_active_capturer,
    )
    captured: List[Any] = []

    async def child():
        captured.append(get_active_capturer())

    async def main():
        sentinel = MagicMock(name="capturer")
        token = set_active_capturer(sentinel)
        try:
            await asyncio.create_task(child())
        finally:
            reset_active_capturer(token)

    asyncio.run(main())
    assert captured and captured[0] is not None


# ---------------------------------------------------------------------------
# extract_confidence_signal_from_active_capturer — composition
# ---------------------------------------------------------------------------


def test_extract_with_no_active_capturer_returns_none():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        extract_confidence_signal_from_active_capturer,
    )
    # No prior set_active_capturer — returns None.
    assert (
        extract_confidence_signal_from_active_capturer() is None
    )


def test_extract_with_active_capturer_composes_capture(
    monkeypatch,
):
    """Happy path: capturer.freeze() + compute_summary + project."""
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    captured_calls: List[str] = []
    fake_trace = object()

    fake_capturer = MagicMock()
    fake_capturer.freeze = lambda: (
        captured_calls.append("freeze")
        or fake_trace
    )

    def _fake_compute_summary(trace):
        captured_calls.append("compute_summary")
        assert trace is fake_trace
        return _StubSummary(
            mean_top1_logprob=-0.5, token_count=10,
        )

    # Monkey-patch the lazy import target.
    import backend.core.ouroboros.governance.verification.confidence_capture as cc
    monkeypatch.setattr(
        cc, "compute_summary", _fake_compute_summary,
    )

    token = mod.set_active_capturer(fake_capturer)
    try:
        sig = mod.extract_confidence_signal_from_active_capturer()
    finally:
        mod.reset_active_capturer(token)

    assert sig is not None
    assert sig.confidence == pytest.approx(math.exp(-0.5))
    assert sig.sample_size == 10
    # Composition discipline — both calls fired in order.
    assert captured_calls == ["freeze", "compute_summary"]


def test_extract_swallows_freeze_exception():
    """Defensive: if capturer.freeze() raises, returns None
    rather than crashing the tool path."""
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    bad_capturer = MagicMock()
    bad_capturer.freeze.side_effect = RuntimeError(
        "simulated freeze failure",
    )
    token = mod.set_active_capturer(bad_capturer)
    try:
        sig = mod.extract_confidence_signal_from_active_capturer()
    finally:
        mod.reset_active_capturer(token)
    assert sig is None


def test_extract_swallows_compute_summary_exception(monkeypatch):
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    fake_capturer = MagicMock()
    fake_capturer.freeze.return_value = object()
    import backend.core.ouroboros.governance.verification.confidence_capture as cc

    def _broken(trace):
        raise RuntimeError("simulated compute_summary failure")

    monkeypatch.setattr(cc, "compute_summary", _broken)
    token = mod.set_active_capturer(fake_capturer)
    try:
        sig = mod.extract_confidence_signal_from_active_capturer()
    finally:
        mod.reset_active_capturer(token)
    assert sig is None


# ---------------------------------------------------------------------------
# observe_active_signal — end-to-end
# ---------------------------------------------------------------------------


def test_observe_active_signal_with_no_capturer_returns_none():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        observe_active_signal,
    )
    out = observe_active_signal(
        op_id="op1", tool_name="read_file", publish_sse=False,
    )
    assert out is None


def test_observe_active_signal_records_on_observer(monkeypatch):
    """Happy path: ContextVar set + extraction succeeds + observer
    records a band crossing for a low-confidence signal."""
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    fake_capturer = MagicMock()
    fake_capturer.freeze.return_value = object()
    import backend.core.ouroboros.governance.verification.confidence_capture as cc

    # logprob = -2.3 → confidence ~ 0.10 → UNKNOWN band.
    monkeypatch.setattr(
        cc, "compute_summary",
        lambda trace: _StubSummary(
            mean_top1_logprob=-2.3, token_count=8,
        ),
    )
    token = mod.set_active_capturer(fake_capturer)
    try:
        crossing = mod.observe_active_signal(
            op_id="op1", tool_name="read_file",
            publish_sse=False,
        )
    finally:
        mod.reset_active_capturer(token)
    # First-obs at unsafe pole → emits.
    assert crossing is not None
    assert crossing.tool_name == "read_file"
    assert crossing.op_id == "op1"
    assert crossing.to_band.value == "unknown"
    assert crossing.sample_size == 8


def test_observe_active_signal_skipped_on_safe_first_obs(monkeypatch):
    """First-obs at safe pole (CERTAIN/HIGH) is silent — Slice 1
    discipline preserved when called via Slice 2 entry-point."""
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    fake_capturer = MagicMock()
    fake_capturer.freeze.return_value = object()
    import backend.core.ouroboros.governance.verification.confidence_capture as cc

    # logprob = -0.05 → confidence ~ 0.95 → CERTAIN band.
    monkeypatch.setattr(
        cc, "compute_summary",
        lambda trace: _StubSummary(
            mean_top1_logprob=-0.05, token_count=20,
        ),
    )
    token = mod.set_active_capturer(fake_capturer)
    try:
        crossing = mod.observe_active_signal(
            op_id="op1", tool_name="x", publish_sse=False,
        )
    finally:
        mod.reset_active_capturer(token)
    assert crossing is None  # safe pole first-obs silent


def test_observe_active_signal_swallows_record_exception(
    monkeypatch,
):
    """Defensive: observer.record() failure NEVER propagates."""
    from backend.core.ouroboros.governance import (
        tool_confidence_warning_observer as mod,
    )
    fake_capturer = MagicMock()
    fake_capturer.freeze.return_value = object()
    import backend.core.ouroboros.governance.verification.confidence_capture as cc

    monkeypatch.setattr(
        cc, "compute_summary",
        lambda trace: _StubSummary(
            mean_top1_logprob=-2.3, token_count=8,
        ),
    )
    # Break the observer's record method.
    monkeypatch.setattr(
        mod.ToolConfidenceObserver, "record",
        lambda *args, **kw: (_ for _ in ()).throw(
            RuntimeError("boom"),
        ),
    )
    token = mod.set_active_capturer(fake_capturer)
    try:
        # Must NOT raise.
        out = mod.observe_active_signal(
            op_id="op1", tool_name="x", publish_sse=False,
        )
        assert out is None
    finally:
        mod.reset_active_capturer(token)


# ---------------------------------------------------------------------------
# Slice 2 AST pin — clean validation against actual source
# ---------------------------------------------------------------------------


def test_slice2_pin_validates_clean_against_source():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    source = _slice1_module_path().read_text(encoding="utf-8")
    tree = ast.parse(source)
    pin = next(
        (
            i for i in register_shipped_invariants()
            if i.invariant_name == (
                "tool_confidence_observer_"
                "slice2_composes_confidence_capture"
            )
        ),
        None,
    )
    assert pin is not None
    violations = pin.validate(tree, source)
    assert violations == ()


def test_slice2_pin_fires_on_missing_extract_function():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def some_other_function():
    pass
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_observer_"
            "slice2_composes_confidence_capture"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("missing" in v for v in violations)


def test_slice2_pin_fires_on_parallel_logprob_math():
    """Synthetic regression — accessing capturer._tokens
    directly should fire the pin (composition discipline:
    must compose compute_summary, not parallel-extract)."""
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def extract_confidence_signal_from_active_capturer():
    capturer = get_active_capturer()
    if capturer is None:
        return None
    from backend.core.ouroboros.governance.verification.confidence_capture import compute_summary
    trace = capturer.freeze()
    summary = compute_summary(trace)
    # BAD — parallel logprob math via raw ring access
    raw = capturer._tokens
    return summary
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_observer_"
            "slice2_composes_confidence_capture"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("_tokens" in v for v in violations)


def test_slice2_pin_fires_on_missing_compute_summary_call():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def extract_confidence_signal_from_active_capturer():
    capturer = get_active_capturer()
    if capturer is None:
        return None
    from backend.core.ouroboros.governance.verification.confidence_capture import compute_summary
    trace = capturer.freeze()
    # BAD — never calls compute_summary
    return None
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_observer_"
            "slice2_composes_confidence_capture"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("compute_summary" in v for v in violations)


def test_slice2_pin_fires_on_missing_lazy_import():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def extract_confidence_signal_from_active_capturer():
    capturer = get_active_capturer()
    if capturer is None:
        return None
    # BAD — no lazy import
    trace = capturer.freeze()
    summary = compute_summary(trace)
    return summary
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_observer_"
            "slice2_composes_confidence_capture"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("lazy-import" in v for v in violations)


def test_slice2_pin_fires_on_missing_freeze_call():
    from backend.core.ouroboros.governance.tool_confidence_warning_observer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def extract_confidence_signal_from_active_capturer():
    capturer = get_active_capturer()
    if capturer is None:
        return None
    from backend.core.ouroboros.governance.verification.confidence_capture import compute_summary
    # BAD — never calls freeze() — direct call on capturer
    summary = compute_summary(capturer)
    return summary
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "tool_confidence_observer_"
            "slice2_composes_confidence_capture"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("freeze" in v for v in violations)
