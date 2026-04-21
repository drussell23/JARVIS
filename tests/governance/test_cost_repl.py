"""Tests for /cost REPL (Slice 4)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.cost_governor import (
    CostGovernor,
    CostGovernorConfig,
    reset_finalize_observers,
)
from backend.core.ouroboros.governance.cost_repl import (
    CostDispatchResult,
    dispatch_cost_command,
    reset_default_governor,
    set_default_governor,
)
from backend.core.ouroboros.governance.session_browser import (
    BookmarkStore,
    SessionBrowser,
    SessionIndex,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    reset_finalize_observers()
    reset_default_governor()
    yield
    reset_finalize_observers()
    reset_default_governor()


def _governor_with_charges() -> CostGovernor:
    g = CostGovernor(
        config=CostGovernorConfig(
            enabled=True, baseline_usd=1.0,
            max_cap_usd=100.0, min_cap_usd=0.01,
        ),
    )
    g.start("op-a", route="standard", complexity="light")
    g.charge("op-a", 0.40, "claude", phase="GENERATE")
    g.charge("op-a", 0.20, "claude", phase="VERIFY")
    g.charge("op-a", 0.05, "doubleword", phase="GENERATE")
    return g


# ===========================================================================
# Match / no-match
# ===========================================================================


def test_non_cost_line_does_not_match():
    res = dispatch_cost_command("/session help")
    assert res.matched is False


def test_empty_line_does_not_match():
    res = dispatch_cost_command("")
    assert res.matched is False


# ===========================================================================
# /cost help
# ===========================================================================


def test_help_lists_verbs():
    res = dispatch_cost_command("/cost help")
    assert res.ok
    for v in ("session", "op-id"):
        assert v in res.text.lower()


def test_help_via_question_mark():
    res = dispatch_cost_command("/cost ?")
    assert res.ok


# ===========================================================================
# /cost <op-id> — live drill-down
# ===========================================================================


def test_live_op_renders_breakdown():
    g = _governor_with_charges()
    res = dispatch_cost_command("/cost op-a", governor=g)
    assert res.ok
    assert "op-a" in res.text
    assert "GENERATE" in res.text
    assert "VERIFY" in res.text
    assert "$0.4500" in res.text  # 0.40 + 0.05 = 0.45 for GENERATE


def test_live_op_unknown_id_returns_error():
    g = _governor_with_charges()
    res = dispatch_cost_command("/cost op-ghost", governor=g)
    assert not res.ok
    assert "no live data" in res.text.lower()


def test_live_op_no_governor_attached():
    # No explicit governor, no module default.
    res = dispatch_cost_command("/cost op-x")
    assert not res.ok
    assert "no costgovernor" in res.text.lower()


def test_live_op_via_module_default():
    g = _governor_with_charges()
    set_default_governor(g)
    res = dispatch_cost_command("/cost op-a")
    assert res.ok
    assert "op-a" in res.text


# ===========================================================================
# /cost (no args) — session rollup
# ===========================================================================


def test_bare_cost_with_no_governor_is_graceful():
    res = dispatch_cost_command("/cost")
    assert res.ok
    assert "no costgovernor" in res.text.lower() or "nothing" in res.text.lower()


def test_bare_cost_with_no_live_ops_is_graceful():
    g = CostGovernor(config=CostGovernorConfig(enabled=True))
    res = dispatch_cost_command("/cost", governor=g)
    assert res.ok
    assert "no live ops" in res.text.lower()


def test_bare_cost_rollup_aggregates_across_ops():
    g = CostGovernor(
        config=CostGovernorConfig(
            enabled=True, baseline_usd=1.0,
            max_cap_usd=100.0, min_cap_usd=0.01,
        ),
    )
    g.start("op-a", route="standard", complexity="light")
    g.start("op-b", route="complex", complexity="heavy_code")
    g.charge("op-a", 0.30, "claude", phase="GENERATE")
    g.charge("op-b", 0.20, "claude", phase="GENERATE")
    g.charge("op-b", 0.10, "claude", phase="VERIFY")
    res = dispatch_cost_command("/cost", governor=g)
    assert res.ok
    assert "$0.6000" in res.text  # 0.30 + 0.20 + 0.10
    # GENERATE rollup = 0.50, VERIFY rollup = 0.10
    assert "GENERATE" in res.text
    assert "op-a" in res.text and "op-b" in res.text


# ===========================================================================
# /cost session <sid> — historical
# ===========================================================================


def _browser_with_session(tmp_path: Path, session_id: str, summary_payload: dict) -> SessionBrowser:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir(parents=True)
    d = sessions_root / session_id
    d.mkdir()
    (d / "summary.json").write_text(json.dumps(summary_payload))
    bm_root = tmp_path / "bm"
    bm_root.mkdir()
    browser = SessionBrowser(
        index=SessionIndex(root=sessions_root),
        bookmarks=BookmarkStore(bookmark_root=bm_root),
    )
    browser.index.scan()
    return browser


def test_session_historical_renders_phase_data(tmp_path: Path):
    browser = _browser_with_session(tmp_path, "bt-hist-1", {
        "stop_reason": "complete",
        "cost_by_phase": {"GENERATE": 0.50, "VERIFY": 0.30},
        "cost_by_op_phase": {"op-1": {"GENERATE": 0.50, "VERIFY": 0.30}},
    })
    res = dispatch_cost_command(
        "/cost session bt-hist-1", session_browser=browser,
    )
    assert res.ok
    assert "bt-hist-1" in res.text
    assert "GENERATE" in res.text
    assert "$0.5000" in res.text
    assert "op-1" in res.text


def test_session_historical_no_cost_data(tmp_path: Path):
    browser = _browser_with_session(tmp_path, "bt-nocost", {
        "stop_reason": "complete",
    })
    res = dispatch_cost_command(
        "/cost session bt-nocost", session_browser=browser,
    )
    assert res.ok
    assert "no per-phase cost data" in res.text.lower()


def test_session_unknown_session_is_error(tmp_path: Path):
    browser = _browser_with_session(tmp_path, "bt-other", {
        "stop_reason": "complete",
    })
    res = dispatch_cost_command(
        "/cost session bt-ghost", session_browser=browser,
    )
    assert not res.ok
    assert "unknown session" in res.text.lower()


def test_session_missing_arg_is_usage():
    res = dispatch_cost_command("/cost session")
    assert not res.ok
    assert "/cost session" in res.text


# ===========================================================================
# Parse errors
# ===========================================================================


def test_malformed_quoting_is_parse_error():
    res = dispatch_cost_command("/cost 'unclosed")
    assert not res.ok
    assert "parse" in res.text.lower()


# ===========================================================================
# Result dataclass
# ===========================================================================


def test_result_dataclass_defaults():
    r = CostDispatchResult(ok=True, text="hi")
    assert r.matched is True
