"""Degraded-but-usable: report, remember, proceed — never abort.

Replays session ``bt-2026-09-08-202025``, op ``op-01a082ae-a96b``: the
Sentinel's own sanctioned goal reached a worker for the first time (Phase 2
queue precedence) and was killed by this interceptor in 64.79 s with
``tools_used=0 tokens=0``, before generating a single token.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import capability_assurance as CA
from backend.core.ouroboros.governance.capability_assurance import (
    FATAL,
    RECOVERABLE,
    CapabilityVerdict,
)

LIVE_REASON = (
    "silent capability degradation: the diff schema is armed and "
    "qwen3-coder-ov:30b is diff-capable on a single-file op, but the schema "
    "decision came out full_content"
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("JARVIS_CAPABILITY_ASSURANCE_ENFORCE", raising=False)
    monkeypatch.delenv("JARVIS_CAPABILITY_DEGRADED_REGISTRY_MAX", raising=False)
    CA.clear_degradations_for_tests()
    yield
    CA.clear_degradations_for_tests()


def _live_verdict(**kw):
    """The exact verdict that killed the Sentinel's op."""
    base = dict(
        ok=False, reason=LIVE_REASON, enforceable=True, severity=RECOVERABLE,
        checks={"diff_schema_flag": True, "single_file_scope": True,
                "telemetry_present": True},
        detail={"capability": "full_content_and_diff",
                "served_model": "qwen3-coder-ov:30b", "target_files": "1"},
    )
    base.update(kw)
    return CapabilityVerdict(**base)


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_the_live_verdict_is_no_longer_fatal():
    """THE regression. Every precondition held and the op still died."""
    assert _live_verdict().is_fatal is False
    assert _live_verdict().degraded_but_usable is True


def test_enforceable_and_severity_are_different_questions():
    """The first version conflated them: `enforceable` asks WHOSE work this is,
    `severity` asks whether the loss actually prevents a candidate."""
    assert _live_verdict(enforceable=True, severity=RECOVERABLE).is_fatal is False
    assert _live_verdict(enforceable=False, severity=FATAL).is_fatal is False
    assert _live_verdict(enforceable=True, severity=FATAL).is_fatal is True


def test_a_passing_verdict_is_never_fatal():
    assert CapabilityVerdict(True, "fine", enforceable=True).is_fatal is False


def test_severity_defaults_to_recoverable():
    """An abort must be argued for, not assumed."""
    assert CapabilityVerdict(False, "x").severity == RECOVERABLE


# --------------------------------------------------------------------------
# Every observed failure mode is recoverable
# --------------------------------------------------------------------------

def test_the_two_real_failure_modes_are_recoverable():
    """full_content (2b.1) is the historic, always-supported schema: lower
    fidelity, never an inability to generate."""
    src = inspect.getsource(CA.assert_generation_capability)
    # Both failure returns must name RECOVERABLE explicitly.
    assert src.count("severity=RECOVERABLE") >= 2


def test_a_broken_CHECK_does_not_abort():
    """`assurance_unavailable` is a bug in the diagnostic, and is no evidence
    at all about the schema."""
    verdict = CA.assert_generation_capability(object(), force_full_content=False)
    assert verdict.is_fatal is False


# --------------------------------------------------------------------------
# Report, remember, flag
# --------------------------------------------------------------------------

class _Ctx:
    op_id = "op-01a082ae-a96b-7ce4-a69d-b8d691f87bfb-cau"
    target_files = ("tests/test_debug_app_classification.py",)


def test_a_degradation_is_flagged_against_the_op():
    CA.mark_degraded(_Ctx.op_id, _live_verdict())
    rec = CA.degradation_for(_Ctx.op_id)
    assert rec is not None
    assert rec["severity"] == RECOVERABLE
    assert "full_content" in rec["reason"]


def test_the_flag_is_absent_for_a_clean_op():
    assert CA.degradation_for("op-clean") is None


def test_the_lesson_names_the_degradation_and_says_it_is_usable():
    kwargs = CA.degradation_lesson_kwargs(_Ctx(), _live_verdict())
    assert kwargs is not None
    assert kwargs["failure_class"] == "capability_degraded"
    assert kwargs["error_class"] == "capability_degraded"
    assert kwargs["phase"] == "GENERATE"
    assert "CapabilityDegradedWarning" in kwargs["summary"]
    assert "usable" in kwargs["summary"]
    assert kwargs["target_files"] == _Ctx.target_files


def test_no_lesson_for_a_passing_verdict():
    assert CA.degradation_lesson_kwargs(_Ctx(), CapabilityVerdict(True, "ok")) is None


def test_the_registry_is_bounded(monkeypatch):
    monkeypatch.setenv("JARVIS_CAPABILITY_DEGRADED_REGISTRY_MAX", "8")
    for i in range(200):
        CA.mark_degraded(f"op-{i}", _live_verdict())
    assert CA.degradation_stats()["open"] <= 8


def test_recording_without_a_running_loop_still_flags():
    """The lesson write is fire-and-forget on the loop; the FLAG must not
    depend on one, or a synchronous prompt build records nothing."""
    CA.record_degradation(_Ctx(), _live_verdict())
    assert CA.degradation_for(_Ctx.op_id) is not None


@pytest.mark.parametrize("hostile", [None, object(), 42, "str"])
def test_nothing_here_raises(hostile):
    CA.mark_degraded(hostile, _live_verdict())
    CA.degradation_for(hostile)
    CA.degradation_lesson_kwargs(hostile, hostile)
    CA.record_degradation(hostile, hostile)


# --------------------------------------------------------------------------
# The seams
# --------------------------------------------------------------------------

def test_the_generation_seam_aborts_only_on_is_fatal():
    from backend.core.ouroboros.governance import providers

    src = inspect.getsource(providers)
    assert "if enforcement_enabled() and _cap.is_fatal:" in src, (
        "the generation seam still aborts on enforceable alone"
    )
    assert "_ca.record_degradation(ctx, _cap)" in src


def test_the_compliance_gate_applies_the_SAME_policy():
    """Otherwise the block just moves from GENERATE to approval and no
    self-directed work can land while the diff schema stays broken."""
    from backend.core.ouroboros.governance.autonomy import sentinel

    src = inspect.getsource(sentinel)
    assert 'checks["capability_intact"] = bool(cap.ok) or cap.degraded_but_usable' in src
    assert "degraded_but_usable:" in src


def test_enforcement_flag_still_disarms_even_a_fatal():
    """The operator keeps the last word in both directions."""
    from backend.core.ouroboros.governance import providers

    src = inspect.getsource(providers)
    assert "enforcement_enabled() and" in src
