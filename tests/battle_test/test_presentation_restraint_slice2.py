"""Tests for Gap #7 Slice 2 — color discipline + idle status content +
TTY gate fix.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.presentation_restraint import (
    MASTER_FLAG_ENV_VAR,
    chrome_color,
    format_idle_breadcrumb,
    real_stdout_isatty,
)
from backend.core.ouroboros.battle_test.status_line import (
    StatusSnapshot,
    _format_plain,
    should_render,
    status_line_enabled,
)


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv("JARVIS_UI_STATUS_LINE_ENABLED", raising=False)
    yield


# ===========================================================================
# chrome_color — green only for outcomes
# ===========================================================================


def test_chrome_color_returns_default_when_restraint_off():
    assert chrome_color() == "bright_green"


def test_chrome_color_returns_dim_under_restraint(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    assert chrome_color() == "dim"


def test_chrome_color_passes_through_custom_default():
    """Caller can pass any default — it's preserved when restraint off."""
    assert chrome_color("yellow") == "yellow"


def test_chrome_color_overrides_custom_default_under_restraint(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    assert chrome_color("yellow") == "dim"


# ===========================================================================
# real_stdout_isatty — bypasses patch_stdout proxy
# ===========================================================================


def test_real_stdout_uses_dunder_stdout():
    """Verifies the helper checks sys.__stdout__ first."""
    with mock.patch.object(sys, "__stdout__", mock.Mock()) as fake_real:
        fake_real.isatty.return_value = True
        # Simulate patch_stdout having replaced sys.stdout with a non-TTY proxy
        with mock.patch.object(sys, "stdout", mock.Mock()) as fake_proxy:
            fake_proxy.isatty.return_value = False
            assert real_stdout_isatty() is True
            # We must check __stdout__, not the proxy
            fake_real.isatty.assert_called()


def test_real_stdout_falls_back_when_dunder_is_none():
    """Defensive: __stdout__ may be None on Windows pythonw etc."""
    with mock.patch.object(sys, "__stdout__", None):
        with mock.patch.object(sys, "stdout", mock.Mock()) as fake:
            fake.isatty.return_value = True
            assert real_stdout_isatty() is True


def test_real_stdout_returns_false_when_both_fail():
    """Both stdout sources non-TTY → False."""
    with mock.patch.object(sys, "__stdout__", mock.Mock()) as r:
        r.isatty.return_value = False
        with mock.patch.object(sys, "stdout", mock.Mock()) as p:
            p.isatty.return_value = False
            assert real_stdout_isatty() is False


def test_real_stdout_handles_isatty_raise():
    """Pathological stream whose isatty() raises → False, never propagate."""
    bad = mock.Mock()
    bad.isatty.side_effect = RuntimeError("boom")
    with mock.patch.object(sys, "__stdout__", bad):
        with mock.patch.object(sys, "stdout", bad):
            assert real_stdout_isatty() is False


# ===========================================================================
# should_render — uses real_stdout_isatty (TTY gate fix)
# ===========================================================================


def test_should_render_passes_when_real_stdout_is_tty(monkeypatch):
    """The fix: status line surfaces during REPL even when patch_stdout
    has replaced sys.stdout with a non-TTY proxy."""
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_ENABLED", "1")
    with mock.patch.object(sys, "__stdout__", mock.Mock()) as r:
        r.isatty.return_value = True
        with mock.patch.object(sys, "stdout", mock.Mock()) as p:
            p.isatty.return_value = False  # patch_stdout proxy
            assert should_render() is True


def test_should_render_false_when_neither_is_tty(monkeypatch):
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_ENABLED", "1")
    with mock.patch.object(sys, "__stdout__", mock.Mock()) as r:
        r.isatty.return_value = False
        with mock.patch.object(sys, "stdout", mock.Mock()) as p:
            p.isatty.return_value = False
            assert should_render() is False


def test_should_render_false_when_kill_switch_off(monkeypatch):
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_ENABLED", "false")
    # Even with TTY, kill switch wins
    with mock.patch.object(sys, "__stdout__", mock.Mock()) as r:
        r.isatty.return_value = True
        assert should_render() is False


# ===========================================================================
# format_idle_breadcrumb — minimal IDLE format
# ===========================================================================


def test_format_idle_breadcrumb_minimal():
    """No optional fields → just IDLE marker."""
    out = format_idle_breadcrumb()
    assert out == "IDLE"


def test_format_idle_breadcrumb_with_branch():
    out = format_idle_breadcrumb(branch="main")
    assert "IDLE" in out
    assert "main" in out
    assert " · " in out


def test_format_idle_breadcrumb_with_cost():
    out = format_idle_breadcrumb(cost_spent=0.04, cost_budget=0.50)
    assert "$0.04/$0.50" in out


def test_format_idle_breadcrumb_omits_budget_when_zero():
    """If no budget configured, only show spent."""
    out = format_idle_breadcrumb(cost_spent=0.00)
    # Spent=0, budget=0 → no cost segment
    assert "$" not in out
    out2 = format_idle_breadcrumb(cost_spent=0.05)
    assert "$0.05" in out2


def test_format_idle_breadcrumb_with_posture():
    out = format_idle_breadcrumb(posture="EXPLORE")
    assert "EXPLORE" in out


def test_format_idle_breadcrumb_with_op_id_tail():
    """Last-completed op surfaces a short tail for memory."""
    out = format_idle_breadcrumb(op_id="op-019d8347-foo")
    assert "prev:" in out


def test_format_idle_breadcrumb_full_format():
    out = format_idle_breadcrumb(
        branch="main",
        cost_spent=0.04,
        cost_budget=0.50,
        posture="EXPLORE",
    )
    # Order: IDLE · branch · cost · posture
    parts = out.split(" · ")
    assert parts[0] == "IDLE"
    assert "main" in parts
    assert any("0.04" in p for p in parts)
    assert "EXPLORE" in parts


def test_format_idle_breadcrumb_handles_non_string_inputs():
    """Pathological input → still returns a useful string."""
    out = format_idle_breadcrumb(branch=None, posture=None)  # type: ignore[arg-type]
    assert "IDLE" in out


# ===========================================================================
# _format_plain — IDLE branch under restraint uses breadcrumb
# ===========================================================================


def test_format_plain_idle_under_restraint_uses_breadcrumb(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    snap = StatusSnapshot(
        phase="IDLE",
        cost_spent_usd=0.04,
        cost_budget_usd=0.50,
    )
    out = _format_plain(snap, compact=False)
    # New compact format
    assert out.startswith("IDLE")
    assert "$0.04/$0.50" in out
    # Verbose labels suppressed
    assert "Phase:" not in out
    assert "Cost:" not in out


def test_format_plain_idle_legacy_when_restraint_off():
    """No master flag → legacy verbose format preserved."""
    snap = StatusSnapshot(
        phase="IDLE",
        cost_spent_usd=0.04,
        cost_budget_usd=0.50,
    )
    out = _format_plain(snap, compact=False)
    # Legacy format with "Phase:" prefix
    assert "Phase:" in out
    assert "Cost:" in out


def test_format_plain_active_phase_unchanged_under_restraint(monkeypatch):
    """Restraint only affects IDLE — active phases use legacy verbose
    (operator wants full info during execution)."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    snap = StatusSnapshot(
        phase="GENERATE",
        cost_spent_usd=0.04,
        cost_budget_usd=0.50,
    )
    out = _format_plain(snap, compact=False)
    assert "Phase:" in out
    assert "GENERATE" in out


def test_format_plain_empty_phase_treated_as_idle(monkeypatch):
    """Empty/missing phase → idle breadcrumb."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    snap = StatusSnapshot(phase="")
    out = _format_plain(snap, compact=False)
    assert out.startswith("IDLE")


# ===========================================================================
# Source-level regression — start() uses chrome_color
# ===========================================================================


_SERPENT_FLOW = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


def test_start_uses_chrome_color_helper():
    """The activity ribbon must consult chrome_color() so green stays
    reserved for outcomes under restraint."""
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "start":
                body = ast.unparse(node)
                if "chrome_color" in body and "event stream active" in body:
                    found = True
                    break
    assert found, "start() must call chrome_color() for activity ribbon"


def test_status_line_should_render_uses_real_stdout():
    """Regression pin: TTY gate must use the real (unpatched) stdout."""
    src = (
        _REPO / "backend/core/ouroboros/battle_test/status_line.py"
    ).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "should_render":
            body = ast.unparse(node)
            assert "real_stdout_isatty" in body, (
                "should_render() must use real_stdout_isatty() — direct "
                "sys.stdout.isatty() check fails under patch_stdout"
            )
            return
    pytest.fail("should_render not found")


# ===========================================================================
# End-to-end: idle breadcrumb surfaces through the full path
# ===========================================================================


def test_end_to_end_idle_breadcrumb_under_restraint(monkeypatch):
    """All the pieces wired together: master flag on, builder
    constructed, snap with phase=IDLE → render_plain returns the
    breadcrumb."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_ENABLED", "1")

    from backend.core.ouroboros.battle_test.status_line import (
        StatusLineBuilder,
    )
    builder = StatusLineBuilder()
    # Patch the snapshot to known values
    fake_snap = StatusSnapshot(
        phase="IDLE",
        cost_spent_usd=0.10,
        cost_budget_usd=0.50,
    )
    with mock.patch.object(builder, "snapshot", return_value=fake_snap):
        out = builder.render_plain()
    assert out.startswith("IDLE")
    assert "$0.10/$0.50" in out
