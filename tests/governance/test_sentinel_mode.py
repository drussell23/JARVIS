"""Autonomous Sentinel Mode — the organism approves its own work, bounded.

The operator's model: green and yellow run unattended, orange is theirs to
decide, RED ALWAYS STOPS FOR A HUMAN. These tests pin that boundary from both
sides, because the cost of a wrong NO is a prompt and the cost of a wrong YES
is an unreviewed commit to a production tree.
"""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.governance.autonomy.sentinel import (
    auto_approval_verdict,
    sentinel_enabled,
    spec_compliance_verdict,
    tier_ceiling,
)
from backend.core.ouroboros.governance.autonomy.sentinel_cooldown import (
    TargetCooldownLedger,
)
from backend.core.ouroboros.governance.op_context import OperationContext

PROD = "backend/api/clean_vision_response.py"
CAGE = "backend/core/ouroboros/governance/risk_engine.py"


class _Validation:
    def __init__(self, passed=True, failed=(), ambient=(), total=12):
        self.passed = passed
        self.failed_test_ids = tuple(failed)
        self.ambient_red_tests = tuple(ambient)
        self.test_total = total
        self.failure_class = ""


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_MODE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_SENTINEL_AUTO_APPROVE_MAX_TIER", raising=False)
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "false")


def _ctx(files=(PROD,)):
    return OperationContext.create(
        target_files=tuple(files), description="d", op_id="op-sentinel-1",
    )


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------

def test_sentinel_is_off_unless_deliberately_armed(monkeypatch):
    monkeypatch.delenv("JARVIS_SENTINEL_MODE_ENABLED", raising=False)
    assert sentinel_enabled() is False
    v = auto_approval_verdict(_ctx(), risk_tier="SAFE_AUTO",
                              validation=_Validation())
    assert v.approved is False
    assert "off" in v.reason


# --------------------------------------------------------------------------
# THE FLOOR — red, the cage, Order-2, recursion. No score buys these.
# --------------------------------------------------------------------------

def test_red_tier_always_escalates(armed):
    v = auto_approval_verdict(_ctx(), risk_tier="BLOCKED",
                              validation=_Validation())
    assert v.approved is False
    assert "red" in v.reason.lower()


def test_a_perfect_score_does_not_buy_red(armed):
    """The whole point: quality is not authority."""
    v = auto_approval_verdict(
        _ctx(), risk_tier="BLOCKED", validation=_Validation(),
        guardian_detections=(),
    )
    assert v.approved is False


def test_touching_the_cage_always_escalates(armed):
    """An op editing the governance substrate is the organism reaching for
    its own brakes — always a human, at any tier."""
    v = auto_approval_verdict(_ctx(files=(CAGE,)), risk_tier="SAFE_AUTO",
                              validation=_Validation())
    assert v.approved is False
    assert "floor" in v.reason.lower()


def test_order2_rsi_always_escalates(armed):
    v = auto_approval_verdict(_ctx(), risk_tier="SAFE_AUTO",
                              validation=_Validation(), is_order2_rsi=True)
    assert v.approved is False


def test_a_recursion_breach_always_escalates(armed):
    v = auto_approval_verdict(_ctx(), risk_tier="SAFE_AUTO",
                              validation=_Validation(), recursion_exceeded=True)
    assert v.approved is False


def test_the_ceiling_can_never_be_raised_to_red(monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_AUTO_APPROVE_MAX_TIER", "BLOCKED")
    assert tier_ceiling() == "APPROVAL_REQUIRED"


@pytest.mark.parametrize("junk", ["", "banana", "GREEN", "safe auto"])
def test_an_unreadable_ceiling_falls_back_not_open(monkeypatch, junk):
    monkeypatch.setenv("JARVIS_SENTINEL_AUTO_APPROVE_MAX_TIER", junk)
    assert tier_ceiling() == "APPROVAL_REQUIRED"


@pytest.mark.parametrize("tier", [None, "", "WHAT_IS_THIS", "RiskTier.MYSTERY"])
def test_an_unknown_tier_escalates(armed, tier):
    v = auto_approval_verdict(_ctx(), risk_tier=tier, validation=_Validation())
    assert v.approved is False


# --------------------------------------------------------------------------
# The operator's model: green + yellow + orange auto, when perfect
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["SAFE_AUTO", "NOTIFY_APPLY", "APPROVAL_REQUIRED"])
def test_green_yellow_and_orange_auto_approve_when_perfect(armed, tier):
    v = auto_approval_verdict(
        _ctx(), risk_tier=tier, validation=_Validation(), guardian_detections=(),
    )
    assert v.approved is True, v.render()
    assert v.compliance is not None and v.compliance.score == 1.0


def test_an_enum_style_tier_is_understood(armed):
    v = auto_approval_verdict(_ctx(), risk_tier="RiskTier.SAFE_AUTO",
                              validation=_Validation(), guardian_detections=())
    assert v.approved is True


def test_lowering_the_ceiling_stops_orange(armed, monkeypatch):
    monkeypatch.setenv("JARVIS_SENTINEL_AUTO_APPROVE_MAX_TIER", "NOTIFY_APPLY")
    assert auto_approval_verdict(
        _ctx(), risk_tier="NOTIFY_APPLY", validation=_Validation(),
        guardian_detections=(),
    ).approved is True
    v = auto_approval_verdict(
        _ctx(), risk_tier="APPROVAL_REQUIRED", validation=_Validation(),
        guardian_detections=(),
    )
    assert v.approved is False
    assert "ceiling" in v.reason


# --------------------------------------------------------------------------
# Compliance must be PERFECT — every gate, not most of them
# --------------------------------------------------------------------------

def test_a_regression_the_candidate_caused_blocks_approval(armed):
    val = _Validation(passed=False, failed=("tests/test_a.py::test_x",))
    v = auto_approval_verdict(_ctx(), risk_tier="SAFE_AUTO", validation=val)
    assert v.approved is False
    assert "zero_regressions" in v.reason or "validation_passed" in v.reason


def test_an_ambient_red_is_not_the_candidates_fault(armed):
    """Tests already red at HEAD belong to the environment. The differential
    verdict is what separates them, and it must not block a clean candidate."""
    val = _Validation(
        passed=True,
        failed=("tests/test_a.py::test_ambient",),
        ambient=("tests/test_a.py::test_ambient",),
    )
    v = spec_compliance_verdict(_ctx(), validation=val, guardian_detections=())
    assert v.checks["zero_regressions"] is True


def test_a_guardian_detection_blocks_approval(armed):
    v = auto_approval_verdict(
        _ctx(), risk_tier="SAFE_AUTO", validation=_Validation(),
        guardian_detections=("suspicious rewrite",),
    )
    assert v.approved is False
    assert "guardian_clean" in v.reason


def test_a_vacuous_validation_never_reads_as_a_pass(armed):
    """test_total == 0 means the validate proved nothing — the exact shape
    that once let an empty run approve itself."""
    v = auto_approval_verdict(
        _ctx(), risk_tier="SAFE_AUTO", validation=_Validation(total=0),
        guardian_detections=(),
    )
    assert v.approved is False
    assert "validation_non_vacuous" in v.reason


def test_no_validation_at_all_is_a_refusal(armed):
    v = auto_approval_verdict(_ctx(), risk_tier="SAFE_AUTO", validation=None)
    assert v.approved is False


def test_compliance_requires_a_perfect_score(armed):
    v = spec_compliance_verdict(
        _ctx(), validation=_Validation(total=0), guardian_detections=(),
    )
    assert v.ok is False
    assert 0.0 < v.score < 1.0    # partial is still a refusal


@pytest.mark.parametrize("junk", [None, object(), "not-a-ctx"])
def test_the_verdict_never_raises(armed, junk):
    assert auto_approval_verdict(junk, risk_tier="SAFE_AUTO") is not None


# --------------------------------------------------------------------------
# Reachability — the Iron Gate must actually consult this
# --------------------------------------------------------------------------

def test_the_iron_gate_consults_the_sentinel_before_prompting():
    import inspect

    from backend.core.ouroboros.battle_test import serpent_flow

    src = inspect.getsource(serpent_flow.SerpentFlow.request_execution_permission)
    assert "auto_approval_verdict" in src, "the gate never asks the sentinel"
    assert src.index("auto_approval_verdict") < src.index(
        "_headless_auto_approve_reason()"
    ), (
        "the sentinel must be consulted BEFORE the headless bypass — otherwise "
        "a missing TTY, not a risk decision, is what approves the op"
    )


# --------------------------------------------------------------------------
# Phase 3 — the runaway brake
# --------------------------------------------------------------------------

def test_cooldown_doubles_with_consecutive_failures(tmp_path):
    led = TargetCooldownLedger(tmp_path / "cd.json")
    d1, d2, d3 = led.delay_for(1), led.delay_for(2), led.delay_for(3)
    assert d2 == pytest.approx(d1 * 2)
    assert d3 == pytest.approx(d1 * 4)


def test_cooldown_is_bounded(tmp_path):
    led = TargetCooldownLedger(tmp_path / "cd.json")
    assert led.delay_for(9999) == led.delay_for(10000)   # ceiling, not overflow


def test_a_failed_target_is_excluded_from_the_next_discovery(tmp_path):
    led = TargetCooldownLedger(tmp_path / "cd.json")
    led.record_failure(PROD, reason="validation failed")
    assert led.is_cooling(PROD) is True
    assert PROD not in led.filter_available([PROD, "backend/other.py"])
    assert "backend/other.py" in led.filter_available([PROD, "backend/other.py"])


def test_success_forgives_completely(tmp_path):
    """Backoff measures CONSECUTIVE failure. Keeping a decayed count would
    teach the organism to avoid the code it just proved it can fix."""
    led = TargetCooldownLedger(tmp_path / "cd.json")
    led.record_failure(PROD)
    led.record_failure(PROD)
    led.record_success(PROD)
    assert led.is_cooling(PROD) is False
    assert led.entry_for(PROD) is None
    # ...and the next failure starts from the base delay again
    entry = led.record_failure(PROD)
    assert entry.consecutive_failures == 1


def test_the_cooldown_survives_a_restart(tmp_path):
    path = tmp_path / "cd.json"
    TargetCooldownLedger(path).record_failure(PROD, reason="boom")
    assert TargetCooldownLedger(path).is_cooling(PROD) is True


def test_a_corrupt_ledger_degrades_to_nothing_cooling(tmp_path):
    """A brake that cannot be read must stop braking, never crash the loop."""
    path = tmp_path / "cd.json"
    path.write_text("{not json at all", encoding="utf-8")
    led = TargetCooldownLedger(path)
    assert led.is_cooling(PROD) is False
    assert led.record_failure(PROD).consecutive_failures == 1


def test_expired_entries_stop_cooling(tmp_path):
    led = TargetCooldownLedger(tmp_path / "cd.json")
    led.record_failure(PROD)
    future = time.time() + led.delay_for(1) + 5
    assert led.is_cooling(PROD, now=future) is False
    assert led.prune_expired(now=future) == 1


def test_an_empty_target_is_ignored(tmp_path):
    led = TargetCooldownLedger(tmp_path / "cd.json")
    assert led.record_failure("").consecutive_failures == 0
    assert led.is_cooling("") is False
