"""Tests for Wave 3 (6) Slice 1 — parallel_dispatch primitive.

Covers ``is_fanout_eligible`` decision chain + the fixed posture weight
table + env-flag readers + authority-import ban.

Scope: memory/project_wave3_item6_scope.md §4 invariants 1/2/3/4/5 +
§12 (b) max_units boundaries + §12 (c) fixed posture weight golden.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision as MemoryFanoutDecision,
    MemoryPressureGate,
    PressureLevel,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    POSTURE_CONFIDENCE_FLOOR,
    FanoutEligibility,
    ReasonCode,
    is_fanout_eligible,
    parallel_dispatch_enabled,
    parallel_dispatch_enforce_enabled,
    parallel_dispatch_max_units,
    parallel_dispatch_shadow_enabled,
    posture_weight_for,
)
from backend.core.ouroboros.governance.posture import Posture


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_gate(level: PressureLevel = PressureLevel.OK, free_pct: float = 60.0,
               cap_override: Optional[int] = None) -> MemoryPressureGate:
    """Build a MagicMock-based MemoryPressureGate substitute.

    Returns a deterministic FanoutDecision honoring ``level`` + optional
    ``cap_override``. When ``cap_override`` is None, the fake gate returns
    ``n_allowed == n_requested`` (no clamp).
    """
    gate = MagicMock(spec=MemoryPressureGate)

    def _can_fanout(n_requested: int) -> MemoryFanoutDecision:
        effective = n_requested if cap_override is None else min(n_requested, cap_override)
        allowed = effective >= 1 and level != PressureLevel.CRITICAL
        if level == PressureLevel.CRITICAL:
            effective = 1
        return MemoryFanoutDecision(
            allowed=allowed,
            n_requested=n_requested,
            n_allowed=effective,
            level=level,
            free_pct=free_pct,
            reason_code=f"mock_{level.value}",
            source="test",
        )

    gate.can_fanout.side_effect = _can_fanout
    return gate


def _posture_fn_factory(
    posture: Optional[Posture] = Posture.MAINTAIN,
    confidence: Optional[float] = 0.9,
):
    """Build a posture_fn injection for is_fanout_eligible."""

    def _fn() -> Tuple[Optional[Posture], Optional[float]]:
        return posture, confidence

    return _fn


@pytest.fixture
def master_on(monkeypatch):
    """Engage the master flag for tests that require it."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")


# ---------------------------------------------------------------------------
# (1) Env-flag readers
# ---------------------------------------------------------------------------


def test_master_flag_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", raising=False)
    assert parallel_dispatch_enabled() is False


def test_master_flag_reads_true_tokens(monkeypatch):
    for tok in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", tok)
        assert parallel_dispatch_enabled() is True, f"token {tok!r} should map to True"


def test_master_flag_reads_false_tokens(monkeypatch):
    for tok in ("0", "false", "FALSE", "no", "off"):
        monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", tok)
        assert parallel_dispatch_enabled() is False, f"token {tok!r} should map to False"


def test_shadow_and_enforce_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", raising=False)
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", raising=False)
    assert parallel_dispatch_shadow_enabled() is False
    assert parallel_dispatch_enforce_enabled() is False


def test_max_units_default_is_three(monkeypatch):
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", raising=False)
    assert parallel_dispatch_max_units() == 3


@pytest.mark.parametrize("value,expected", [("2", 2), ("3", 3), ("4", 4), ("10", 10)])
def test_max_units_reads_integer(monkeypatch, value, expected):
    """§12 (b): tests must pin 2 / 3 / 4 boundary behavior."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", value)
    assert parallel_dispatch_max_units() == expected


def test_max_units_clamps_non_positive_to_one(monkeypatch):
    for bad in ("-1", "0"):
        monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", bad)
        assert parallel_dispatch_max_units() == 1, f"value {bad!r} should clamp to 1"


def test_max_units_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "not-a-number")
    assert parallel_dispatch_max_units() == 3


# ---------------------------------------------------------------------------
# (2) Fixed posture weight table — §12 (c) golden
# ---------------------------------------------------------------------------


def test_posture_weight_table_golden():
    """§12 (c): posture weights are fixed in code; tests pin the exact table."""
    assert posture_weight_for(Posture.HARDEN) == 0.5
    assert posture_weight_for(Posture.MAINTAIN) == 1.0
    assert posture_weight_for(Posture.CONSOLIDATE) == 1.0
    assert posture_weight_for(Posture.EXPLORE) == 1.5


def test_posture_weight_none_is_neutral():
    assert posture_weight_for(None) == 1.0


# ---------------------------------------------------------------------------
# (3) Short-circuit reasons — master off / empty / single-file
# ---------------------------------------------------------------------------


def test_master_off_short_circuits_regardless_of_op_shape(monkeypatch):
    """§4 invariant: master flag off → MASTER_OFF regardless of inputs."""
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", raising=False)
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=5,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(),
        emit_log=False,
    )
    assert decision.allowed is False
    assert decision.reason_code == ReasonCode.MASTER_OFF
    assert decision.n_allowed == 1


def test_empty_candidate_list_returns_empty_reason(master_on):
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=0,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(),
        emit_log=False,
    )
    assert decision.allowed is False
    assert decision.reason_code == ReasonCode.EMPTY_CANDIDATE_LIST
    assert decision.n_allowed == 0


def test_single_file_op_returns_single_file_reason(master_on):
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=1,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(),
        emit_log=False,
    )
    assert decision.allowed is False
    assert decision.reason_code == ReasonCode.SINGLE_FILE_OP
    assert decision.n_allowed == 1


# ---------------------------------------------------------------------------
# (4) Emergency brake — posture confidence floor
# ---------------------------------------------------------------------------


def test_low_posture_confidence_forces_serial(master_on):
    """§4 invariant #2 emergency brake: confidence < floor → serial."""
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=3,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(
            posture=Posture.EXPLORE, confidence=POSTURE_CONFIDENCE_FLOOR - 0.01
        ),
        emit_log=False,
    )
    assert decision.allowed is False
    assert decision.reason_code == ReasonCode.POSTURE_LOW_CONFIDENCE
    assert decision.n_allowed == 1


def test_confidence_at_exact_floor_does_not_brake(master_on):
    """Floor is strict less-than; equality is acceptable."""
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=3,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(
            posture=Posture.MAINTAIN, confidence=POSTURE_CONFIDENCE_FLOOR
        ),
        emit_log=False,
    )
    # At floor, evaluation continues — decision is driven by other gates.
    assert decision.reason_code != ReasonCode.POSTURE_LOW_CONFIDENCE


def test_missing_posture_confidence_does_not_brake(master_on):
    """Missing posture (None, None) → evaluate with neutral weight."""
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=3,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=None, confidence=None),
        emit_log=False,
    )
    assert decision.reason_code != ReasonCode.POSTURE_LOW_CONFIDENCE
    # Neutral weight 1.0 + OK memory → fan out allowed at n_requested.
    assert decision.allowed is True
    assert decision.n_allowed == 3


# ---------------------------------------------------------------------------
# (5) MemoryPressureGate sovereignty (§4 invariant #1)
# ---------------------------------------------------------------------------


def test_memory_critical_forces_serial(master_on):
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=3,
        gate=_make_gate(level=PressureLevel.CRITICAL, free_pct=5.0),
        posture_fn=_posture_fn_factory(posture=Posture.EXPLORE, confidence=0.95),
        emit_log=False,
    )
    assert decision.allowed is False
    assert decision.reason_code == ReasonCode.MEMORY_CRITICAL
    assert decision.memory_level == PressureLevel.CRITICAL
    assert decision.n_allowed == 1


def test_memory_high_clamps_without_forcing_serial(master_on):
    """HIGH pressure allows some fan-out but may reduce n_allowed."""
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=5,
        gate=_make_gate(level=PressureLevel.HIGH, cap_override=2),
        posture_fn=_posture_fn_factory(posture=Posture.MAINTAIN, confidence=0.9),
        emit_log=False,
    )
    # max_units default 3 + posture 1.0 + memory cap 2 → final 2.
    assert decision.n_allowed == 2
    assert decision.allowed is True
    assert decision.reason_code == ReasonCode.MEMORY_CLAMP
    assert decision.memory_level == PressureLevel.HIGH


def test_memory_warn_narrows_cap(master_on):
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=4,
        gate=_make_gate(level=PressureLevel.WARN, cap_override=3),
        posture_fn=_posture_fn_factory(posture=Posture.MAINTAIN, confidence=0.9),
        emit_log=False,
    )
    # max_units=3 caps first; memory at 3 is not additionally binding.
    assert decision.n_allowed == 3
    assert decision.allowed is True
    assert decision.memory_level == PressureLevel.WARN


# ---------------------------------------------------------------------------
# (6) Posture weighting (§4 invariant #2 + §12 (c))
# ---------------------------------------------------------------------------


def test_harden_posture_halves_cap(master_on, monkeypatch):
    """HARDEN × 0.5 applied to base_cap; floor at 1."""
    # Use max_units=4 + n_requested=4 + memory OK to isolate posture effect.
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "4")
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=4,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.HARDEN, confidence=0.9),
        emit_log=False,
    )
    # base_cap=min(4,4)=4; posture_clamped=floor(4*0.5)=2.
    assert decision.n_allowed == 2
    assert decision.posture == Posture.HARDEN
    assert decision.posture_weight == 0.5
    assert decision.allowed is True
    assert decision.reason_code == ReasonCode.POSTURE_CLAMP


def test_explore_posture_does_not_exceed_base_cap(master_on, monkeypatch):
    """EXPLORE × 1.5 cannot exceed base_cap (n_requested × max_units floor)."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "3")
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=3,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.EXPLORE, confidence=0.9),
        emit_log=False,
    )
    # base_cap=min(3,3)=3; posture_clamped=min(floor(3*1.5)=4, base_cap=3) = 3.
    assert decision.n_allowed == 3
    assert decision.allowed is True
    assert decision.posture == Posture.EXPLORE
    assert decision.posture_weight == 1.5


def test_maintain_posture_is_neutral(master_on, monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "3")
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=3,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.MAINTAIN, confidence=0.9),
        emit_log=False,
    )
    assert decision.n_allowed == 3
    assert decision.allowed is True
    assert decision.posture_weight == 1.0


def test_consolidate_posture_is_neutral(master_on, monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "3")
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=3,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.CONSOLIDATE, confidence=0.9),
        emit_log=False,
    )
    assert decision.n_allowed == 3
    assert decision.allowed is True
    assert decision.posture_weight == 1.0


def test_harden_with_two_file_op_floors_at_one_and_denies(master_on, monkeypatch):
    """HARDEN + 2-file op → floor(2*0.5)=1; serial-equivalent; allowed=False."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "3")
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=2,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.HARDEN, confidence=0.9),
        emit_log=False,
    )
    # base_cap=2; posture_clamped=floor(2*0.5)=1 (floored at 1).
    assert decision.n_allowed == 1
    assert decision.allowed is False
    assert decision.reason_code == ReasonCode.POSTURE_CLAMP


# ---------------------------------------------------------------------------
# (7) MAX_UNITS boundaries (§12 (b))
# ---------------------------------------------------------------------------


def test_max_units_2_caps_fan_out(master_on, monkeypatch):
    """§12 (b): MAX_UNITS=2 pins fan-out ceiling to 2."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "2")
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=5,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.MAINTAIN, confidence=0.9),
        emit_log=False,
    )
    assert decision.n_allowed == 2
    assert decision.max_units_cap == 2
    assert decision.reason_code == ReasonCode.MAX_UNITS_CLAMP


def test_max_units_3_default_caps_fan_out(master_on, monkeypatch):
    """§12 (b): MAX_UNITS=3 (default) pins fan-out ceiling to 3."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "3")
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=10,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.MAINTAIN, confidence=0.9),
        emit_log=False,
    )
    assert decision.n_allowed == 3
    assert decision.max_units_cap == 3


def test_max_units_4_caps_fan_out(master_on, monkeypatch):
    """§12 (b): MAX_UNITS=4 pins fan-out ceiling to 4."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "4")
    decision = is_fanout_eligible(
        op_id="test-op",
        n_candidate_files=10,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.MAINTAIN, confidence=0.9),
        emit_log=False,
    )
    assert decision.n_allowed == 4
    assert decision.max_units_cap == 4


# ---------------------------------------------------------------------------
# (8) Happy path + log line shape
# ---------------------------------------------------------------------------


def test_happy_path_allows_at_n_requested(master_on, monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "3")
    decision = is_fanout_eligible(
        op_id="op-019db800-abcd-1234-5678-abcdef012345",
        n_candidate_files=3,
        gate=_make_gate(level=PressureLevel.OK),
        posture_fn=_posture_fn_factory(posture=Posture.MAINTAIN, confidence=0.9),
        emit_log=False,
    )
    assert decision.allowed is True
    assert decision.n_allowed == 3
    assert decision.reason_code == ReasonCode.ALLOWED
    assert decision.posture == Posture.MAINTAIN
    assert decision.memory_level == PressureLevel.OK


def test_log_line_has_stable_key_order(master_on):
    decision = is_fanout_eligible(
        op_id="op-019db800-abcd-1234-5678-abcdef012345",
        n_candidate_files=3,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.MAINTAIN, confidence=0.9),
        emit_log=False,
    )
    line = decision.log_line("op-019db800-abcd-1234-5678-abcdef012345")
    # Key order pinned — downstream dashboards depend on this shape.
    key_order = [
        "[ParallelDispatch]", "op=", "allowed=", "n_requested=", "n_allowed=",
        "reason=", "posture=", "posture_weight=", "posture_confidence=",
        "memory_level=", "memory_n_allowed=", "base_cap=", "max_units_cap=",
    ]
    idx = 0
    for key in key_order:
        pos = line.find(key, idx)
        assert pos >= idx, f"key {key!r} out of order in log line: {line!r}"
        idx = pos + len(key)


def test_log_line_truncates_op_id_to_16_chars(master_on):
    decision = is_fanout_eligible(
        op_id="op-019db800-abcd-1234-5678-abcdef012345-extra-tail",
        n_candidate_files=3,
        gate=_make_gate(),
        posture_fn=_posture_fn_factory(posture=Posture.MAINTAIN, confidence=0.9),
        emit_log=False,
    )
    line = decision.log_line("op-019db800-abcd-1234-5678-abcdef012345-extra-tail")
    # Convention established by Wave 2 (5) [PhaseRunnerDelegate] markers.
    m = re.search(r"op=(\S+)", line)
    assert m is not None
    assert len(m.group(1)) == 16


# ---------------------------------------------------------------------------
# (9) Authority-import ban (§4 invariant #3) — grep-enforced
# ---------------------------------------------------------------------------


def test_parallel_dispatch_has_no_authority_imports():
    """§4 invariant #6 (per scope doc): parallel_dispatch.py must not
    import orchestrator / policy / iron_gate / risk_tier / change_engine /
    candidate_generator / gate. This protects the module from becoming
    an authority-holder; fan-out is scheduling infrastructure, not a
    new execution authority.
    """
    module_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "parallel_dispatch.py"
    )
    assert module_path.exists(), f"module not found at {module_path}"
    source = module_path.read_text()
    # Banned modules — relative or absolute import form.
    banned_patterns = [
        r"from\s+backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"from\s+backend\.core\.ouroboros\.governance\.policy\b",
        r"from\s+backend\.core\.ouroboros\.governance\.iron_gate\b",
        r"from\s+backend\.core\.ouroboros\.governance\.risk_tier\b",
        r"from\s+backend\.core\.ouroboros\.governance\.change_engine\b",
        r"from\s+backend\.core\.ouroboros\.governance\.candidate_generator\b",
        # `gate` is a common word; the concrete imports use explicit paths
        # like `phase_runners.gate_runner` or module `gate.py` — check both.
        r"from\s+backend\.core\.ouroboros\.governance\.gate\b",
        r"from\s+backend\.core\.ouroboros\.governance\.phase_runners\.gate_runner\b",
        r"import\s+backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"import\s+backend\.core\.ouroboros\.governance\.policy\b",
        r"import\s+backend\.core\.ouroboros\.governance\.iron_gate\b",
        r"import\s+backend\.core\.ouroboros\.governance\.risk_tier\b",
        r"import\s+backend\.core\.ouroboros\.governance\.change_engine\b",
        r"import\s+backend\.core\.ouroboros\.governance\.candidate_generator\b",
        r"import\s+backend\.core\.ouroboros\.governance\.gate\b",
    ]
    for pattern in banned_patterns:
        matches = re.findall(pattern, source)
        assert not matches, (
            f"parallel_dispatch.py violates authority-import ban: "
            f"pattern {pattern!r} matched {matches!r}"
        )


# ---------------------------------------------------------------------------
# (10) FanoutEligibility immutability
# ---------------------------------------------------------------------------


def test_fanout_eligibility_is_frozen():
    """Immutability protects against downstream mutation of decision records."""
    e = FanoutEligibility(
        allowed=True,
        n_requested=3,
        n_allowed=3,
        reason_code=ReasonCode.ALLOWED,
    )
    with pytest.raises((AttributeError, Exception)):
        e.n_allowed = 99  # type: ignore[misc]


def test_reason_code_values_stable():
    """Reason codes are used in telemetry + dashboards — values must not
    silently drift. Lock the full enum value set."""
    expected = {
        "allowed",
        "master_off",
        "empty_candidate_list",
        "single_file_op",
        "posture_low_confidence",
        "memory_critical",
        "memory_clamp",
        "posture_clamp",
        "max_units_clamp",
    }
    actual = {c.value for c in ReasonCode}
    assert actual == expected
