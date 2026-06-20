"""Formal Calibration Mode — scoped, sanctioned Advisor bypass tests.

Pins: off by default; both env-master AND a target match required; only the
sanctioned seed is greenlit; fail-closed; the Advisor honors it natively.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import calibration_context as cc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("JARVIS_CALIBRATION_MODE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_CALIBRATION_TARGET_PATH", raising=False)
    yield


def test_disabled_by_default():
    assert cc.calibration_mode_enabled() is False
    assert cc.is_calibration_target(("tests/seed/test_seed_defect.py",)) is False


def test_env_master_required(monkeypatch):
    # scope set but env master OFF → no bypass
    tok = cc.set_calibration_target("test_seed_defect.py")
    try:
        assert cc.is_calibration_target(("x/test_seed_defect.py",)) is False
    finally:
        cc.reset_calibration_target(tok)


def test_registered_seed_match(monkeypatch):
    monkeypatch.setenv("JARVIS_CALIBRATION_MODE_ENABLED", "true")
    # default seed path → basename test_seed_defect.py
    assert cc.is_calibration_target(("tests/seed/test_seed_defect.py",)) is True
    assert cc.is_calibration_target(("a/b/test_seed_defect.py",)) is True


def test_non_seed_target_not_bypassed(monkeypatch):
    monkeypatch.setenv("JARVIS_CALIBRATION_MODE_ENABLED", "true")
    # a real op in the same enabled mode is NOT greenlit
    assert cc.is_calibration_target(("backend/core/unified_supervisor.py",)) is False


def test_active_scope_match(monkeypatch):
    monkeypatch.setenv("JARVIS_CALIBRATION_MODE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CALIBRATION_TARGET_PATH", "nope/none.py")
    tok = cc.set_calibration_target("widget.py")
    try:
        assert cc.is_calibration_target(("pkg/widget.py",)) is True
        assert cc.is_calibration_target(("pkg/other.py",)) is False
    finally:
        cc.reset_calibration_target(tok)
    # scope closed → no longer matches
    assert cc.is_calibration_target(("pkg/widget.py",)) is False


def test_env_target_override(monkeypatch):
    monkeypatch.setenv("JARVIS_CALIBRATION_MODE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CALIBRATION_TARGET_PATH", "tests/seed/my_seed.py")
    assert cc.is_calibration_target(("tests/seed/my_seed.py",)) is True
    assert cc.is_calibration_target(("tests/seed/test_seed_defect.py",)) is False


def test_fail_closed_on_bad_input(monkeypatch):
    monkeypatch.setenv("JARVIS_CALIBRATION_MODE_ENABLED", "true")
    assert cc.is_calibration_target(()) is False
    assert cc.is_calibration_target((None,)) is False  # type: ignore[arg-type]


def test_advisor_greenlights_calibration_target(monkeypatch):
    """End-to-end: the OperationAdvisor formally greenlights the seed under
    calibration mode, with the auditable CALIBRATION_OVERRIDE reason."""
    monkeypatch.setenv("JARVIS_CALIBRATION_MODE_ENABLED", "true")
    from pathlib import Path
    from backend.core.ouroboros.governance.operation_advisor import (
        OperationAdvisor, AdvisoryDecision,
    )
    adv = OperationAdvisor(Path("."))
    out = adv.advise(
        target_files=("tests/seed/test_seed_defect.py",),
        description="repair seed defect",
        op_id="calib-1",
    )
    assert out.decision == AdvisoryDecision.RECOMMEND
    assert any("CALIBRATION_OVERRIDE" in r for r in out.reasons)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
