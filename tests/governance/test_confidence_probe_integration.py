"""Priority 1 Slice 3 — confidence collapse probe consumer regression spine.

Pins the structural contract for ``probe_confidence_collapse`` and
the deterministic decision math in
``_decide_confidence_collapse_action``. Mirrors the §25 Priority C
test pattern (test_hypothesis_consumers.py).

§-numbered coverage map:

  §1   Master flag JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED — default false (Slice 3)
  §2   Knobs: distress_ratio / stylistic_ratio / inconclusive_thinking_factor
  §3   ConfidenceCollapseAction enum: 3 values, str-valued
  §4   ConfidenceCollapseVerdict frozen dataclass shape
  §5   Master-off short-circuits to safe legacy default
  §6   Three-layer flag gating (integration + consumers + probe)
  §7   Decision math: distress band → ESCALATE
  §8   Decision math: stylistic band → RETRY_WITH_FEEDBACK
  §9   Decision math: middle band → INCONCLUSIVE with thinking factor
  §10  Decision math: memorialized_dead → ESCALATE
  §11  Decision math: missing margin/floor → INCONCLUSIVE (defensive)
  §12  Feedback rendering — bounded length, op_id + posture preserved
  §13  Escalation rendering — bounded length
  §14  NEVER raises on malformed error inputs
  §15  Authority invariants (no forbidden imports, pure stdlib + verification.*)
  §16  End-to-end async dispatch (probe runs, returns verdict)
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.verification import hypothesis_consumers
from backend.core.ouroboros.governance.verification.hypothesis_consumers import (
    ConfidenceCollapseAction,
    ConfidenceCollapseVerdict,
    _decide_confidence_collapse_action,
    _render_confidence_collapse_escalation,
    _render_confidence_collapse_feedback,
    confidence_probe_integration_enabled,
    probe_confidence_collapse,
)
from backend.core.ouroboros.governance.verification.confidence_monitor import (
    ConfidenceCollapseError,
    ConfidenceVerdict,
)
from backend.core.ouroboros.governance.verification.hypothesis_probe import (
    ProbeResult,
    reset_ledger_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_failed_ledger():
    """Test isolation — clear failed-hypothesis memorialization
    between tests so memorialized_dead doesn't bleed across cases."""
    reset_ledger_for_tests()
    yield
    reset_ledger_for_tests()


def _make_error(
    *,
    rolling_margin=0.01,
    floor=0.05,
    effective_floor=0.10,
    posture="HARDEN",
    op_id="op-test",
    observations_count=14,
    window_size=16,
):
    return ConfidenceCollapseError(
        verdict=ConfidenceVerdict.BELOW_FLOOR,
        rolling_margin=rolling_margin,
        floor=floor,
        effective_floor=effective_floor,
        window_size=window_size,
        observations_count=observations_count,
        posture=posture,
        provider="dw",
        model_id="qwen-397b",
        op_id=op_id,
    )


def _make_probe_result(
    *,
    convergence_state="stable",
    posterior=0.5,
    cost=0.001,
):
    return ProbeResult(
        confidence_posterior=posterior,
        observation_summary=f"synthetic {convergence_state}",
        cost_usd=cost,
        iterations_used=1,
        convergence_state=convergence_state,
        evidence_hash="abc",
    )


# ===========================================================================
# §1 — Master flag default false
# ===========================================================================


def test_integration_flag_default_false(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", raising=False,
    )
    assert confidence_probe_integration_enabled() is False


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_integration_flag_empty_default_false(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", val,
    )
    assert confidence_probe_integration_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_integration_flag_explicit_true(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", val,
    )
    assert confidence_probe_integration_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_integration_flag_falsy_disables(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", val,
    )
    assert confidence_probe_integration_enabled() is False


# ===========================================================================
# §2 — Knob bounds + defensive fallbacks
# ===========================================================================


def test_distress_ratio_default(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_CONFIDENCE_COLLAPSE_DISTRESS_RATIO", raising=False,
    )
    assert hypothesis_consumers._confidence_distress_ratio() == 0.3


def test_distress_ratio_clamped_to_one(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_COLLAPSE_DISTRESS_RATIO", "5.0",
    )
    assert hypothesis_consumers._confidence_distress_ratio() == 1.0


def test_distress_ratio_floored_at_zero(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_COLLAPSE_DISTRESS_RATIO", "-0.5",
    )
    assert hypothesis_consumers._confidence_distress_ratio() == 0.0


def test_distress_ratio_garbage_falls_back(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_COLLAPSE_DISTRESS_RATIO", "garbage",
    )
    assert hypothesis_consumers._confidence_distress_ratio() == 0.3


def test_stylistic_ratio_default(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_CONFIDENCE_COLLAPSE_STYLISTIC_RATIO", raising=False,
    )
    assert hypothesis_consumers._confidence_stylistic_ratio() == 0.7


def test_stylistic_ratio_clamped_above_distress(monkeypatch) -> None:
    """Stylistic must be ≥ distress to preserve banding semantics."""
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_COLLAPSE_DISTRESS_RATIO", "0.5",
    )
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_COLLAPSE_STYLISTIC_RATIO", "0.2",
    )
    # Should clamp stylistic to distress floor
    assert hypothesis_consumers._confidence_stylistic_ratio() >= 0.5


def test_inconclusive_thinking_factor_default(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_CONFIDENCE_INCONCLUSIVE_THINKING_FACTOR", raising=False,
    )
    assert (
        hypothesis_consumers._confidence_inconclusive_thinking_factor()
        == 0.5
    )


def test_inconclusive_thinking_factor_floored(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_INCONCLUSIVE_THINKING_FACTOR", "0.0",
    )
    # Floored at 0.05 to avoid effectively disabling thinking
    assert (
        hypothesis_consumers._confidence_inconclusive_thinking_factor()
        == 0.05
    )


def test_inconclusive_thinking_factor_capped_at_one(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_INCONCLUSIVE_THINKING_FACTOR", "5.0",
    )
    assert (
        hypothesis_consumers._confidence_inconclusive_thinking_factor()
        == 1.0
    )


# ===========================================================================
# §3 — Action enum
# ===========================================================================


def test_action_three_values() -> None:
    assert ConfidenceCollapseAction.RETRY_WITH_FEEDBACK.value == (
        "retry_with_feedback"
    )
    assert ConfidenceCollapseAction.ESCALATE_TO_OPERATOR.value == (
        "escalate_to_operator"
    )
    assert ConfidenceCollapseAction.INCONCLUSIVE.value == "inconclusive"


def test_action_str_serializable() -> None:
    import json
    payload = {"action": ConfidenceCollapseAction.ESCALATE_TO_OPERATOR.value}
    assert json.dumps(payload) == '{"action": "escalate_to_operator"}'


# ===========================================================================
# §4 — Verdict frozen dataclass
# ===========================================================================


def test_verdict_frozen() -> None:
    v = ConfidenceCollapseVerdict(
        action=ConfidenceCollapseAction.RETRY_WITH_FEEDBACK,
        confidence_posterior=0.5,
        convergence_state="stable",
        observation_summary="x",
        cost_usd=0.01,
    )
    with pytest.raises((AttributeError, Exception)):
        v.action = ConfidenceCollapseAction.ESCALATE_TO_OPERATOR  # type: ignore


def test_verdict_default_thinking_factor_one() -> None:
    """RETRY/ESCALATE shouldn't reduce thinking budget."""
    v = ConfidenceCollapseVerdict(
        action=ConfidenceCollapseAction.RETRY_WITH_FEEDBACK,
        confidence_posterior=0.5,
        convergence_state="stable",
        observation_summary="x",
        cost_usd=0.0,
    )
    assert v.thinking_budget_reduction_factor == 1.0
    assert v.feedback_text == ""


# ===========================================================================
# §5 — Master-off → safe legacy default
# ===========================================================================


def test_master_off_returns_safe_default(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", "false",
    )
    err = _make_error()
    verdict = asyncio.run(probe_confidence_collapse(error=err))
    assert verdict.action == ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
    assert verdict.convergence_state == "disabled"
    # Feedback still rendered so legacy behavior is best-effort
    assert "CONFIDENCE-COLLAPSE-FEEDBACK" in verdict.feedback_text


def test_master_off_no_probe_dispatched(monkeypatch) -> None:
    """Verifies cost_usd is 0 when master-off → no probe ran."""
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", "false",
    )
    verdict = asyncio.run(
        probe_confidence_collapse(error=_make_error()),
    )
    assert verdict.cost_usd == 0.0


# ===========================================================================
# §6 — Three-layer flag gating
# ===========================================================================


def test_consumers_flag_off_short_circuits(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_HYPOTHESIS_CONSUMERS_ENABLED", "false")
    verdict = asyncio.run(
        probe_confidence_collapse(error=_make_error()),
    )
    assert verdict.convergence_state == "disabled"


def test_probe_flag_off_short_circuits(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_HYPOTHESIS_CONSUMERS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "false")
    verdict = asyncio.run(
        probe_confidence_collapse(error=_make_error()),
    )
    assert verdict.convergence_state == "disabled"


# ===========================================================================
# §7 — Decision math: distress band → ESCALATE
# ===========================================================================


def test_decide_distress_band_escalates() -> None:
    """margin/floor < 0.3 (distress threshold) → ESCALATE."""
    err = _make_error(rolling_margin=0.01, effective_floor=0.10)
    # 0.01/0.10 = 0.1 < 0.3 → distress
    result = _make_probe_result(convergence_state="stable")
    verdict = _decide_confidence_collapse_action(err, result)
    assert verdict.action == ConfidenceCollapseAction.ESCALATE_TO_OPERATOR
    assert "distress" in verdict.observation_summary.lower()
    assert "ESCALATION" in verdict.feedback_text


def test_decide_distress_at_exact_threshold_inconclusive() -> None:
    """At ratio = 0.3 (boundary), strict < math → not distress; → middle band."""
    err = _make_error(
        rolling_margin=0.03, effective_floor=0.10,
    )  # ratio = 0.3
    result = _make_probe_result()
    verdict = _decide_confidence_collapse_action(err, result)
    # 0.3 not less than 0.3 (strict <) → falls to next band check
    # 0.3 not greater than 0.7 → middle → INCONCLUSIVE
    assert verdict.action == ConfidenceCollapseAction.INCONCLUSIVE


# ===========================================================================
# §8 — Decision math: stylistic band → RETRY
# ===========================================================================


def test_decide_stylistic_band_retries() -> None:
    """margin/floor > 0.7 (stylistic threshold) → RETRY."""
    err = _make_error(rolling_margin=0.04, effective_floor=0.05)
    # 0.04/0.05 = 0.8 > 0.7 → stylistic
    result = _make_probe_result()
    verdict = _decide_confidence_collapse_action(err, result)
    assert verdict.action == ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
    assert "stylistic" in verdict.observation_summary.lower()
    assert "FEEDBACK" in verdict.feedback_text


# ===========================================================================
# §9 — Decision math: middle band → INCONCLUSIVE
# ===========================================================================


def test_decide_middle_band_inconclusive() -> None:
    """0.3 ≤ ratio ≤ 0.7 → INCONCLUSIVE with thinking factor."""
    err = _make_error(rolling_margin=0.025, effective_floor=0.05)
    # 0.025/0.05 = 0.5 → middle band
    result = _make_probe_result()
    verdict = _decide_confidence_collapse_action(err, result)
    assert verdict.action == ConfidenceCollapseAction.INCONCLUSIVE
    assert verdict.thinking_budget_reduction_factor < 1.0
    assert verdict.thinking_budget_reduction_factor >= 0.05  # floor


# ===========================================================================
# §10 — Decision math: memorialized_dead → ESCALATE
# ===========================================================================


def test_decide_memorialized_dead_escalates() -> None:
    """Recurring confidence collapse on the same claim → ESCALATE."""
    err = _make_error(rolling_margin=0.04, effective_floor=0.05)
    # ratio 0.8 would normally be RETRY, but memorialized takes precedence
    result = _make_probe_result(convergence_state="memorialized_dead")
    verdict = _decide_confidence_collapse_action(err, result)
    assert verdict.action == ConfidenceCollapseAction.ESCALATE_TO_OPERATOR
    assert "recurring" in verdict.observation_summary.lower()


# ===========================================================================
# §11 — Decision math: missing inputs → INCONCLUSIVE (defensive)
# ===========================================================================


def test_decide_missing_margin_returns_inconclusive() -> None:
    err = _make_error(rolling_margin=None)
    result = _make_probe_result()
    verdict = _decide_confidence_collapse_action(err, result)
    assert verdict.action == ConfidenceCollapseAction.INCONCLUSIVE


def test_decide_zero_floor_returns_inconclusive() -> None:
    """eff_floor=0 means we can't compute a ratio safely."""
    err = _make_error(effective_floor=0.0)
    result = _make_probe_result()
    verdict = _decide_confidence_collapse_action(err, result)
    assert verdict.action == ConfidenceCollapseAction.INCONCLUSIVE


# ===========================================================================
# §12 — Feedback rendering
# ===========================================================================


def test_feedback_includes_op_id_and_posture() -> None:
    err = _make_error(op_id="op-12345", posture="HARDEN")
    text = _render_confidence_collapse_feedback(err)
    assert "op-12345" in text
    assert "HARDEN" in text


def test_feedback_bounded_length() -> None:
    err = _make_error()
    text = _render_confidence_collapse_feedback(err)
    # Bounded so it doesn't dominate next prompt's token budget
    assert len(text) < 1000


def test_feedback_handles_malformed_error() -> None:
    """NEVER raises on bad input."""
    text = _render_confidence_collapse_feedback(None)
    assert isinstance(text, str)
    assert len(text) > 0  # falls back to generic guidance

    text2 = _render_confidence_collapse_feedback("not an error")
    assert isinstance(text2, str)


# ===========================================================================
# §13 — Escalation rendering
# ===========================================================================


def test_escalation_bounded_length() -> None:
    err = _make_error()
    text = _render_confidence_collapse_escalation(err)
    assert len(text) < 600


def test_escalation_includes_escalation_marker() -> None:
    err = _make_error(op_id="op-esc")
    text = _render_confidence_collapse_escalation(err)
    assert "ESCALATION" in text
    assert "op-esc" in text


def test_escalation_handles_malformed_error() -> None:
    text = _render_confidence_collapse_escalation(None)
    assert isinstance(text, str)
    assert len(text) > 0


# ===========================================================================
# §14 — NEVER raises on malformed inputs
# ===========================================================================


def test_consumer_handles_none_error(monkeypatch) -> None:
    """None error → safe legacy default, not exception."""
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "true")
    verdict = asyncio.run(probe_confidence_collapse(error=None))
    assert verdict.action == ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
    assert verdict.convergence_state == "disabled"


def test_decide_with_bad_error_object_falls_back() -> None:
    """Pure decision math doesn't blow up on object missing fields."""
    class BadError:
        pass
    bad = BadError()
    result = _make_probe_result()
    verdict = _decide_confidence_collapse_action(bad, result)
    # Missing rolling_margin / effective_floor → defensive INCONCLUSIVE
    assert verdict.action == ConfidenceCollapseAction.INCONCLUSIVE


# ===========================================================================
# §15 — Authority invariants (AST-pinned)
# ===========================================================================


_FORBIDDEN_IMPORTS = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.phase_runners",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.semantic_firewall",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
)


def test_authority_no_forbidden_imports() -> None:
    src = Path(inspect.getfile(hypothesis_consumers)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for forbidden in _FORBIDDEN_IMPORTS:
                    assert forbidden not in alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for forbidden in _FORBIDDEN_IMPORTS:
                assert forbidden not in node.module


def test_authority_only_verification_internal() -> None:
    """hypothesis_consumers should only depend on stdlib + verification.*"""
    src = Path(inspect.getfile(hypothesis_consumers)).read_text()
    tree = ast.parse(src)
    allowed_roots = {
        "logging", "os", "math", "pathlib",
        "dataclasses", "enum", "typing", "__future__",
        "backend",  # the verification.* family
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed_roots, (
                    f"unexpected import: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root in allowed_roots, (
                f"unexpected import: {node.module}"
            )
            # Specifically: backend.* imports must be verification.*
            if root == "backend":
                assert "verification" in node.module, (
                    f"non-verification backend import: {node.module}"
                )


# ===========================================================================
# §16 — End-to-end async dispatch (probe runs)
# ===========================================================================


def test_consumer_end_to_end_dispatches_probe(monkeypatch) -> None:
    """Verifies the full chain: master flag on → probe runs →
    verdict mapped from ProbeResult. Distress signal triggers
    ESCALATE."""
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_HYPOTHESIS_CONSUMERS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "true")
    err = _make_error(rolling_margin=0.005, effective_floor=0.10)
    # 0.005/0.10 = 0.05 < 0.3 → distress
    verdict = asyncio.run(probe_confidence_collapse(error=err))
    assert verdict.action == ConfidenceCollapseAction.ESCALATE_TO_OPERATOR


def test_consumer_end_to_end_stylistic_retries(monkeypatch) -> None:
    """Stylistic margin → RETRY_WITH_FEEDBACK with full feedback text."""
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_HYPOTHESIS_CONSUMERS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "true")
    err = _make_error(rolling_margin=0.04, effective_floor=0.05)
    # 0.04/0.05 = 0.8 > 0.7 → stylistic
    verdict = asyncio.run(probe_confidence_collapse(error=err))
    assert verdict.action == ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
    assert len(verdict.feedback_text) > 100
