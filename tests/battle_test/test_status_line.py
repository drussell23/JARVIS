"""StatusLineBuilder tests — glanceable one-line operator status.

Covers the format contract + env gates + data-source sampling for the
Priority 2B UX fix. Uses ``render_plain()`` for exact-string assertions
(ANSI-free). The ``render_prompt_toolkit()`` HTML path was retired
in UI Slice 3 (2026-04-30) along with the persistent bottom_toolbar.

Mandates verified:
  • Env kill switch (``JARVIS_UI_STATUS_LINE_ENABLED``)
  • Compact-mode drops route badge + op tail
  • Color gradient thresholds (green <50%, yellow 50-80%, red >80%)
  • Proactive warn marker (⚠) at cost/idle >warn_threshold_pct
  • Phase sub-detail for L2 Repair (iter/max), elapsed-in-phase for
    GENERATE / VALIDATE / APPLY / VERIFY
  • Route + provider badge (``[complex·claude]`` / ``[bg·dw]``)
  • Multi-op indicator (``Op: 019d9368 (+2)``)
  • Truncated op ID (last 10-ish chars)
  • Refresh cadence env (``JARVIS_UI_STATUS_LINE_REFRESH_MS``)
  • SerpentFlow.bottom_toolbar AST canary — still consults the builder
"""
from __future__ import annotations

import ast
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.battle_test.status_line import (
    StatusLineBuilder,
    StatusSnapshot,
    _format_plain,
    compact_mode_enabled,
    get_status_line_builder,
    refresh_interval_s,
    register_status_line_builder,
    reset_status_line_builder,
    status_line_enabled,
    warn_threshold_pct,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_UI_STATUS_LINE_"):
            monkeypatch.delenv(key, raising=False)
    reset_status_line_builder()
    yield
    reset_status_line_builder()


# ---------------------------------------------------------------------------
# (1) Env gates — master switch, compact mode, refresh, warn threshold
# ---------------------------------------------------------------------------


def test_status_line_enabled_default_on():
    assert status_line_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE"])
def test_status_line_disabled_values(monkeypatch, value):
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_ENABLED", value)
    assert status_line_enabled() is False


def test_compact_mode_default_off():
    assert compact_mode_enabled() is False


def test_compact_mode_on(monkeypatch):
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_COMPACT", "1")
    assert compact_mode_enabled() is True


def test_refresh_interval_default_500ms():
    assert refresh_interval_s() == 0.5


def test_refresh_interval_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_REFRESH_MS", "250")
    assert refresh_interval_s() == 0.25


def test_refresh_interval_clamped_to_range(monkeypatch):
    """Guard against pathological values — always within [0.1, 5.0]."""
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_REFRESH_MS", "50")
    assert refresh_interval_s() == 0.1  # min clamp
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_REFRESH_MS", "99999")
    assert refresh_interval_s() == 5.0  # max clamp


def test_warn_threshold_default_80():
    assert warn_threshold_pct() == 80


def test_warn_threshold_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_WARN_PCT", "60")
    assert warn_threshold_pct() == 60


# ---------------------------------------------------------------------------
# (2) Singleton registry
# ---------------------------------------------------------------------------


def test_default_singleton_is_none():
    assert get_status_line_builder() is None


def test_register_and_reset():
    b = StatusLineBuilder()
    register_status_line_builder(b)
    assert get_status_line_builder() is b
    reset_status_line_builder()
    assert get_status_line_builder() is None


# ---------------------------------------------------------------------------
# (3) Format contract — plain render
# ---------------------------------------------------------------------------


def test_plain_format_idle_state():
    """No active op, no cost, no idle timer — still renders something."""
    snap = StatusSnapshot()
    line = _format_plain(snap, compact=False)
    # No Op/badge when nothing is active.
    assert "Phase: IDLE" in line
    assert "Cost: $0.00 / $0.00" in line
    assert "Idle: 0s / 0s" in line
    assert "Op:" not in line  # no primary op


def test_plain_format_full_line_matches_user_example():
    """Mirror the spec example: ``Phase: L2 Repair 2/8 · Cost: $0.22 /
    $0.50 · Idle: 847s / 2400s · Op: 019d9368 [complex·claude]``."""
    snap = StatusSnapshot(
        phase="L2 Repair",
        phase_detail="2/8",
        cost_spent_usd=0.22,
        cost_budget_usd=0.50,
        idle_elapsed_s=847,
        idle_timeout_s=2400,
        primary_op_id="op-019d9368-654b-7612-a031-6507ffde327c-cau",
        route="complex",
        provider="claude",
    )
    line = _format_plain(snap, compact=False)
    assert "Phase: L2 Repair 2/8" in line
    assert "Cost: $0.22 / $0.50" in line
    assert "Idle: 847s / 2400s" in line
    # Truncated op id (last chunk of the uuid).
    assert "Op: 019d9368" in line
    assert "[complex·claude]" in line
    # Verify separator is the middle-dot.
    assert " · " in line


def test_plain_format_compact_drops_op_and_badge():
    """Compact mode: keep Phase / Cost / Idle; drop Op + badge."""
    snap = StatusSnapshot(
        phase="GENERATE",
        phase_detail="47s",
        cost_spent_usd=0.10, cost_budget_usd=0.50,
        idle_elapsed_s=5, idle_timeout_s=600,
        primary_op_id="op-abc-xyz-cau",
        route="complex", provider="claude",
    )
    line = _format_plain(snap, compact=True)
    assert "Phase: GENERATE 47s" in line
    assert "Cost:" in line
    assert "Idle:" in line
    assert "Op:" not in line
    assert "[complex·claude]" not in line


# ---------------------------------------------------------------------------
# (4) Color gradient + warn marker
# ---------------------------------------------------------------------------


def test_warn_marker_absent_below_threshold():
    snap = StatusSnapshot(
        cost_spent_usd=0.10, cost_budget_usd=0.50,  # 20%
        idle_elapsed_s=100, idle_timeout_s=1000,     # 10%
    )
    line = _format_plain(snap, compact=False)
    assert "⚠" not in line


def test_warn_marker_present_above_threshold():
    snap = StatusSnapshot(
        cost_spent_usd=0.45, cost_budget_usd=0.50,  # 90% — past 80% default
        idle_elapsed_s=100, idle_timeout_s=1000,    # 10% — no warn
    )
    line = _format_plain(snap, compact=False)
    # Warn should appear on the Cost segment, not Idle.
    cost_segment = next(seg for seg in line.split(" · ") if "Cost:" in seg)
    assert "⚠" in cost_segment
    idle_segment = next(seg for seg in line.split(" · ") if "Idle:" in seg)
    assert "⚠" not in idle_segment


def test_warn_marker_on_both_when_both_hot():
    snap = StatusSnapshot(
        cost_spent_usd=0.48, cost_budget_usd=0.50,   # 96%
        idle_elapsed_s=550, idle_timeout_s=600,      # 92%
    )
    line = _format_plain(snap, compact=False)
    assert line.count("⚠") == 2


# NOTE: ``test_html_color_tokens_match_gradient`` was retired in UI
# Slice 3 (2026-04-30) along with ``_format_html`` and the
# ``render_prompt_toolkit`` rendering path. The gradient logic itself
# (``_level_for_fraction``) is still exercised by the warn-marker
# tests above, which assert on the plain-format ⚠ glyph emission.


def test_warn_threshold_env_override_moves_the_line(monkeypatch):
    """Lowering WARN_PCT to 40 pulls the ⚠ marker down to 40%."""
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_WARN_PCT", "40")
    snap = StatusSnapshot(
        cost_spent_usd=0.25, cost_budget_usd=0.50,  # 50% — now past 40% threshold
        idle_elapsed_s=100, idle_timeout_s=1000,
    )
    line = _format_plain(snap, compact=False)
    cost_segment = next(seg for seg in line.split(" · ") if "Cost:" in seg)
    assert "⚠" in cost_segment


# ---------------------------------------------------------------------------
# (5) Op ID truncation + multi-op indicator
# ---------------------------------------------------------------------------


def test_op_id_truncated_to_core_prefix():
    """Full ops look like 'op-019d9368-654b-7612-a031-6507ffde327c-cau' —
    the line should show only the first core segment for scannability."""
    snap = StatusSnapshot(
        primary_op_id="op-019d9368-654b-7612-a031-6507ffde327c-cau",
    )
    line = _format_plain(snap, compact=False)
    assert "019d9368" in line
    # Full uuid must NOT appear.
    assert "019d9368-654b-7612" not in line


def test_multi_op_indicator_appears_when_extra_ops_present():
    snap = StatusSnapshot(
        primary_op_id="op-019d9368-abc", extra_op_count=3,
    )
    line = _format_plain(snap, compact=False)
    assert "Op: 019d9368" in line
    assert "(+3)" in line


def test_multi_op_indicator_hidden_when_single_op():
    snap = StatusSnapshot(
        primary_op_id="op-019d9368-abc", extra_op_count=0,
    )
    line = _format_plain(snap, compact=False)
    assert "(+0)" not in line


# ---------------------------------------------------------------------------
# (6) Route + provider badge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route,provider,expected", [
    ("complex", "claude", "[complex·claude]"),
    ("background", "dw", "[bg·dw]"),
    ("immediate", "claude", "[imm·claude]"),
    ("standard", "claude", "[std·claude]"),
    ("speculative", "dw", "[spec·dw]"),
])
def test_route_provider_badge_abbreviations(route, provider, expected):
    snap = StatusSnapshot(
        primary_op_id="op-x", route=route, provider=provider,
    )
    line = _format_plain(snap, compact=False)
    assert expected in line


def test_badge_hidden_when_route_and_provider_both_empty():
    snap = StatusSnapshot(primary_op_id="op-x")
    line = _format_plain(snap, compact=False)
    assert "[" not in line


# ---------------------------------------------------------------------------
# (7) Phase sub-detail
# ---------------------------------------------------------------------------


def test_phase_with_detail():
    snap = StatusSnapshot(phase="L2 Repair", phase_detail="2/8")
    line = _format_plain(snap, compact=False)
    assert "Phase: L2 Repair 2/8" in line


def test_phase_without_detail():
    snap = StatusSnapshot(phase="APPROVE", phase_detail="")
    line = _format_plain(snap, compact=False)
    assert "Phase: APPROVE" in line
    # No trailing space artifact.
    assert "Phase: APPROVE " not in line.split(" · ")[0]


# ---------------------------------------------------------------------------
# (8) Builder — samplers read from mock refs
# ---------------------------------------------------------------------------


def _build_mock_gls_with_ops(fsm_contexts):
    gls = MagicMock()
    gls._active_ops = set(fsm_contexts.keys())
    gls._fsm_contexts = fsm_contexts
    # Block the _orchestrator attribute so _resolve_repair_engine
    # returns None cleanly (no L2 detail unless we wire one explicitly).
    gls._orchestrator = None
    return gls


def _mock_fsm_ctx(*, phase_name, entered_at, route="", provider=""):
    c = MagicMock()
    c.phase = MagicMock()
    c.phase.name = phase_name
    c.phase_entered_at = entered_at
    c.provider_route = route
    c.generation = MagicMock()
    c.generation.provider_name = provider
    return c


def test_builder_samples_cost_from_tracker():
    tracker = MagicMock()
    tracker.total_spent = 0.33
    tracker.budget_usd = 0.50
    builder = StatusLineBuilder(cost_tracker=tracker)
    snap = builder.snapshot()
    assert snap.cost_spent_usd == pytest.approx(0.33)
    assert snap.cost_budget_usd == pytest.approx(0.50)


def test_builder_samples_idle_from_watchdog():
    watchdog = MagicMock()
    watchdog._last_poke = time.monotonic() - 42.0
    watchdog.timeout_s = 600.0
    # Strip any attributes that might mask; keep it minimal.
    builder = StatusLineBuilder(idle_watchdog=watchdog)
    snap = builder.snapshot()
    assert snap.idle_timeout_s == 600.0
    assert 40.0 <= snap.idle_elapsed_s <= 50.0  # small scheduling slack


def test_builder_primary_op_is_most_recently_advanced():
    """When 3 ops are active, the primary op is the one whose FSM
    context was most recently advanced (largest phase_entered_at)."""
    now = datetime.now(tz=timezone.utc)
    ctxs = {
        "op-old":     _mock_fsm_ctx(phase_name="GENERATE", entered_at=now - timedelta(seconds=30)),
        "op-newest":  _mock_fsm_ctx(phase_name="VALIDATE", entered_at=now - timedelta(seconds=2)),
        "op-middle":  _mock_fsm_ctx(phase_name="GENERATE", entered_at=now - timedelta(seconds=15)),
    }
    gls = _build_mock_gls_with_ops(ctxs)
    builder = StatusLineBuilder(governed_loop_service=gls)
    snap = builder.snapshot()
    assert snap.primary_op_id == "op-newest"
    assert snap.extra_op_count == 2


def test_builder_surfaces_l2_iter_when_repair_running():
    """When the repair engine reports ``is_running=True``, phase should
    flip to ``L2 Repair`` with ``2/8`` detail regardless of FSM phase."""
    now = datetime.now(tz=timezone.utc)
    ctxs = {"op-1": _mock_fsm_ctx(phase_name="VALIDATE", entered_at=now)}
    gls = _build_mock_gls_with_ops(ctxs)

    repair = MagicMock()
    repair.is_running = True
    repair.current_iteration = 2
    repair.max_iterations_live = 8

    builder = StatusLineBuilder(
        governed_loop_service=gls, repair_engine=repair,
    )
    snap = builder.snapshot()
    assert snap.phase == "L2 Repair"
    assert snap.phase_detail == "2/8"


def test_builder_l2_iter_hidden_when_not_running():
    """is_running=False → phase falls back to FSM state."""
    now = datetime.now(tz=timezone.utc)
    ctxs = {"op-1": _mock_fsm_ctx(phase_name="VALIDATE", entered_at=now)}
    gls = _build_mock_gls_with_ops(ctxs)

    repair = MagicMock()
    repair.is_running = False
    builder = StatusLineBuilder(
        governed_loop_service=gls, repair_engine=repair,
    )
    snap = builder.snapshot()
    assert snap.phase == "VALIDATE"
    assert snap.phase_detail == ""  # not enough elapsed time in the mock


def test_builder_phase_detail_shows_elapsed_for_long_generate():
    """GENERATE / VALIDATE / APPLY / VERIFY get elapsed-in-phase sub-
    detail (``47s``). Other phases don't — keeps noise down for fast
    states like CLASSIFY / ROUTE."""
    now = datetime.now(tz=timezone.utc)
    entered = now - timedelta(seconds=47)
    ctxs = {"op-1": _mock_fsm_ctx(phase_name="GENERATE", entered_at=entered)}
    gls = _build_mock_gls_with_ops(ctxs)
    builder = StatusLineBuilder(governed_loop_service=gls)
    snap = builder.snapshot()
    assert snap.phase == "GENERATE"
    # Allow small jitter; exact boundary is "≥1s".
    assert snap.phase_detail in {"46s", "47s"}


def test_builder_route_and_provider_pulled_from_primary_ctx():
    now = datetime.now(tz=timezone.utc)
    ctxs = {
        "op-1": _mock_fsm_ctx(
            phase_name="GENERATE", entered_at=now,
            route="complex", provider="claude",
        ),
    }
    gls = _build_mock_gls_with_ops(ctxs)
    builder = StatusLineBuilder(governed_loop_service=gls)
    snap = builder.snapshot()
    assert snap.route == "complex"
    assert snap.provider == "claude"


# ---------------------------------------------------------------------------
# (9) End-to-end: render() returns empty when disabled
# ---------------------------------------------------------------------------


def test_render_returns_empty_when_kill_switch_off(monkeypatch):
    monkeypatch.setenv("JARVIS_UI_STATUS_LINE_ENABLED", "0")
    builder = StatusLineBuilder()
    assert builder.render_plain() == ""


def test_render_plain_non_empty_when_enabled():
    builder = StatusLineBuilder()
    plain = builder.render_plain()
    assert plain  # non-empty
    assert "Phase:" in plain


def test_render_never_raises_even_with_broken_refs():
    """Builder with refs that throw on every attribute must NOT propagate.

    Uses a custom class with ``__getattribute__`` that always raises —
    this mirrors the pathological "subsystem half-torn-down" state
    that could surface during shutdown. The status line must survive.
    """
    class _BrokenRef:
        def __getattribute__(self, name):
            raise RuntimeError("boom")

    broken = _BrokenRef()
    builder = StatusLineBuilder(
        cost_tracker=broken, idle_watchdog=broken,
        governed_loop_service=broken,
    )
    # Must not raise on the plain rendering path.
    plain = builder.render_plain()
    # Graceful degradation: returns string (possibly minimal "IDLE"
    # line, not empty-string — builder catches at the sample layer,
    # not the whole render).
    assert isinstance(plain, str)


# ---------------------------------------------------------------------------
# (10) AST canary — SerpentFlow still consults the builder
# ---------------------------------------------------------------------------


def test_serpent_flow_no_bottom_toolbar():
    """UI Slice 3 (2026-04-30) inverted contract: SerpentFlow MUST NOT
    construct its REPL with a persistent bottom_toolbar. The flowing
    CLI surfaces state on-demand via ``/status`` (Slice 5) and inline
    via op-completion receipts (Slice 6) — no fixed terminal regions.
    This guard prevents a future regression that re-introduces the
    persistent toolbar (which would re-create the duplicate paradigm
    we just removed)."""
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend" / "core" / "ouroboros" / "battle_test" / "serpent_flow.py"
    )
    src = path.read_text(encoding="utf-8")
    # ``bottom_toolbar=`` as a keyword argument MUST be absent. Token
    # may appear in comments / docstrings explaining the retirement;
    # we check only for the actual kwarg pattern.
    assert "bottom_toolbar=_toolbar" not in src
    assert "bottom_toolbar=lambda" not in src


def test_serpent_flow_no_refresh_interval():
    """UI Slice 3 inverted contract: no fixed refresh cadence. The
    flowing CLI doesn't tick a fixed UI region — events emit when
    they happen (op heartbeats, completion receipts, REPL prompts)."""
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend" / "core" / "ouroboros" / "battle_test" / "serpent_flow.py"
    )
    src = path.read_text(encoding="utf-8")
    # The kwarg must be absent from any PromptSession construction.
    assert "refresh_interval=" not in src


def test_harness_registers_status_line_builder():
    """Static guard: harness.py must call ``register_status_line_builder``
    during boot — without it, SerpentFlow's toolbar falls through to
    the legacy layout even when the kill switch is on."""
    path = (
        Path(__file__).resolve().parent.parent.parent
        / "backend" / "core" / "ouroboros" / "battle_test" / "harness.py"
    )
    src = path.read_text(encoding="utf-8")
    assert "register_status_line_builder" in src, (
        "harness.py no longer registers StatusLineBuilder — the "
        "glanceable line will be silently disabled in every battle test."
    )


def test_repair_engine_exposes_live_iteration_counters():
    """Static guard: RepairEngine must expose the ``current_iteration``
    and ``max_iterations_live`` properties the status line reads."""
    from backend.core.ouroboros.governance.repair_engine import RepairEngine
    assert hasattr(RepairEngine, "current_iteration")
    assert hasattr(RepairEngine, "max_iterations_live")
    assert hasattr(RepairEngine, "is_running")
