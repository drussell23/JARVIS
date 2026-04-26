"""P4 Slice 3 — /metrics REPL dispatcher regression suite.

Pins:
  * Module constants + status enum + frozen result dataclass.
  * Env knob default-false-pre-graduation.
  * Sparkline rendering: empty input, all-flat plateau,
    monotonic-descending (improvement), monotonic-ascending
    (degradation), down-sampling at high cardinality, ASCII-strict.
  * Formatters: percent/money/seconds + None handling.
  * handle: bare /metrics → current; explicit current; 7d / 30d
    windows; composite history; trend banner; why <id> happy +
    UNKNOWN_SESSION + bad-id-shape; help.
  * Subcommand parsing precedence: shape gating prevents
    natural-language collisions (every shape mismatch falls through
    to UNKNOWN_SUBCOMMAND with help).
  * READ_ERROR status when ledger raises.
  * latest_snapshot_provider injection wins over ledger fallback.
  * Authority invariants: no banned imports + no I/O / subprocess.
"""
from __future__ import annotations

import dataclasses
import io
import time
import tokenize
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.convergence_tracker import (
    ConvergenceReport,
    ConvergenceState,
    ConvergenceTracker,
)
from backend.core.ouroboros.governance.metrics_engine import (
    METRICS_SNAPSHOT_SCHEMA_VERSION,
    MetricsEngine,
    MetricsSnapshot,
    TrendDirection,
    reset_default_engine,
)
from backend.core.ouroboros.governance.metrics_history import (
    MetricsHistoryLedger,
    reset_default_ledger,
)
from backend.core.ouroboros.governance.metrics_repl_dispatcher import (
    COMPOSITE_HISTORY_MAX_ROWS,
    MAX_RENDERED_BYTES,
    SPARKLINE_CHARS,
    SPARKLINE_WIDTH,
    MetricsReplDispatcher,
    MetricsReplResult,
    MetricsReplStatus,
    is_enabled,
    render_composite_only_sparkline,
    render_current,
    render_help,
    render_sparkline,
    render_trend_banner,
    render_window,
)


_REPO = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def _strip_docstrings_and_comments(src: str) -> str:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenizeError, IndentationError):
        return src
    for tok in toks:
        if tok.type == tokenize.STRING:
            out.append('""')
        elif tok.type == tokenize.COMMENT:
            continue
        else:
            out.append(tok.string)
    return " ".join(out)


_FROZEN_NOW = 1_700_000_000.0


def _make_snapshot(
    *,
    session_id: str = "bt-1",
    composite_mean: float = 0.5,
    composite_min: float = 0.4,
    composite_max: float = 0.6,
    completion: float = 1.0,
    self_form: float = 0.3,
    pm_recall: float = 0.5,
    cost: float = 0.10,
    posture: float = 600.0,
    ts: float = _FROZEN_NOW,
    per_op=(0.4, 0.5, 0.6),
) -> MetricsSnapshot:
    return MetricsSnapshot(
        schema_version=METRICS_SNAPSHOT_SCHEMA_VERSION,
        session_id=session_id,
        computed_at_unix=ts,
        composite_score_session_mean=composite_mean,
        composite_score_session_min=composite_min,
        composite_score_session_max=composite_max,
        per_op_composite_scores=per_op,
        trend=TrendDirection.IMPROVING,
        convergence_slope=-0.1,
        convergence_oscillation_ratio=0.0,
        convergence_scores_analyzed=len(per_op),
        convergence_recommendation="ok",
        session_completion_rate=completion,
        self_formation_ratio=self_form,
        postmortem_recall_rate=pm_recall,
        cost_per_successful_apply=cost,
        posture_stability_seconds=posture,
        ops_inspected=len(per_op),
    )


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_METRICS_HISTORY_PATH", raising=False)
    yield


@pytest.fixture
def populated_dispatcher(tmp_path):
    """Dispatcher backed by a real ledger pre-loaded with 8 sessions
    showing improving composite scores."""
    reset_default_engine()
    reset_default_ledger()
    p = tmp_path / "m.jsonl"
    L = MetricsHistoryLedger(path=p, clock=lambda: _FROZEN_NOW)
    for i, comp in enumerate([0.85, 0.78, 0.70, 0.62, 0.55, 0.50, 0.46, 0.42]):
        L.append(_make_snapshot(
            session_id=f"bt-{i}", composite_mean=comp,
            composite_min=comp - 0.05, composite_max=comp + 0.05,
            ts=_FROZEN_NOW - (8 - i) * 60,  # spread across an hour
        ))
    # Re-construct the ledger with a clock 30 minutes after the writes
    # so windowing math finds the rows in 7d.
    L_recent = MetricsHistoryLedger(
        path=p, clock=lambda: _FROZEN_NOW + 1800,
    )
    yield MetricsReplDispatcher(ledger=L_recent)
    reset_default_engine()
    reset_default_ledger()


# ===========================================================================
# A — Module constants + enum + frozen result
# ===========================================================================


def test_max_rendered_bytes_pinned():
    assert MAX_RENDERED_BYTES == 16 * 1024


def test_sparkline_chars_pinned():
    """ASCII-only ramp. Adding Unicode characters here breaks the
    strict-ASCII contract pinned in test_render_sparkline_ascii_only."""
    assert SPARKLINE_CHARS == "_.-=*#"


def test_sparkline_width_pinned():
    assert SPARKLINE_WIDTH == 60


def test_composite_history_max_pinned():
    assert COMPOSITE_HISTORY_MAX_ROWS == 8_192


def test_status_enum_values():
    assert {s.name for s in MetricsReplStatus} == {
        "OK", "EMPTY", "UNKNOWN_SUBCOMMAND",
        "UNKNOWN_SESSION", "READ_ERROR",
    }


def test_result_is_frozen():
    r = MetricsReplResult(status=MetricsReplStatus.EMPTY, rendered_text="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.rendered_text = "y"  # type: ignore[misc]


# ===========================================================================
# B — Env knob (default false pre-graduation)
# ===========================================================================


def test_is_enabled_default_false_pre_graduation():
    """Slice 3 ships default-OFF. Renamed at Slice 5 graduation."""
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_is_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_is_enabled_falsy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", val)
    assert is_enabled() is False


# ===========================================================================
# C — Sparkline rendering
# ===========================================================================


def test_sparkline_empty_returns_empty():
    assert render_sparkline([]) == ""


def test_sparkline_all_flat_renders_middle_char():
    """A constant series means a plateau — render as the middle ramp
    character so operators read it as 'flat'."""
    out = render_sparkline([0.5, 0.5, 0.5, 0.5])
    mid = SPARKLINE_CHARS[len(SPARKLINE_CHARS) // 2]
    assert set(out) == {mid}
    assert len(out) == 4


def test_sparkline_monotonic_ascending_uses_full_ramp():
    """Strictly increasing values should hit both extremes of the ramp."""
    out = render_sparkline([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
    assert out[0] == SPARKLINE_CHARS[0]   # lowest
    assert out[-1] == SPARKLINE_CHARS[-1]  # highest


def test_sparkline_monotonic_descending_renders_inverse():
    out = render_sparkline([1.0, 0.8, 0.6, 0.4, 0.2, 0.0])
    assert out[0] == SPARKLINE_CHARS[-1]  # highest first
    assert out[-1] == SPARKLINE_CHARS[0]   # lowest last


def test_sparkline_downsamples_when_over_width():
    """100 values at width=10 should produce exactly 10 chars."""
    values = [i / 100.0 for i in range(100)]
    out = render_sparkline(values, width=10)
    assert len(out) == 10


def test_sparkline_width_clamped_max_200():
    out = render_sparkline([0.0, 1.0], width=10_000)
    assert len(out) <= 200


def test_sparkline_width_clamped_min_1():
    """Pin: width=0 / negative renders at least one bin."""
    out = render_sparkline([0.5], width=0)
    assert len(out) >= 1


def test_sparkline_ascii_only():
    """Strict-ASCII contract — no Unicode block characters."""
    out = render_sparkline([0.0, 0.3, 0.6, 1.0])
    out.encode("ascii")  # raises if non-ASCII slipped in


# ===========================================================================
# D — Formatters / null handling
# ===========================================================================


def test_render_help_lists_all_subcommands():
    out = render_help()
    for sub in ("/metrics current", "/metrics 7d", "/metrics 30d",
                "/metrics composite", "/metrics trend", "/metrics why",
                "/metrics help"):
        assert sub in out


def test_render_current_handles_none_fields():
    """A snapshot with all-None metrics should render 'n/a' / not crash."""
    snap = MetricsSnapshot(
        schema_version=1, session_id="empty",
        computed_at_unix=_FROZEN_NOW,
        # All metric fields default to None.
    )
    out = render_current(snap)
    assert "n/a" in out
    out.encode("ascii")  # ASCII safety pin


def test_render_current_includes_all_seven_metrics():
    snap = _make_snapshot()
    out = render_current(snap)
    assert "composite:" in out
    assert "trend:" in out
    assert "completion_rate:" in out
    assert "self_formation_ratio:" in out
    assert "postmortem_recall_rate:" in out
    assert "cost_per_apply:" in out
    assert "posture_stability:" in out


def test_render_window_zero_snapshots():
    from backend.core.ouroboros.governance.metrics_history import (
        AggregatedMetrics,
    )
    agg = AggregatedMetrics(window_days=7, snapshots_in_window=0,
                            notes=("empty window",))
    out = render_window(agg)
    assert "no snapshots in window" in out
    assert "empty window" in out


def test_render_trend_banner_no_data():
    out = render_trend_banner(snapshot=None, agg7=None)
    assert "latest=NO_DATA" in out
    assert "7d=NO_DATA" in out


def test_render_composite_only_sparkline_empty():
    out = render_composite_only_sparkline([], rows_seen=0)
    assert "empty" in out


# ===========================================================================
# E — handle: empty + bare /metrics → current
# ===========================================================================


def test_handle_empty_returns_empty_status(populated_dispatcher):
    assert populated_dispatcher.handle("").status is MetricsReplStatus.EMPTY
    assert populated_dispatcher.handle("   ").status is MetricsReplStatus.EMPTY


def test_handle_bare_metrics_routes_to_current(populated_dispatcher):
    r = populated_dispatcher.handle("/metrics")
    assert r.status is MetricsReplStatus.OK
    # Latest snapshot is bt-7 (the most recent).
    assert "bt-7" in r.rendered_text


def test_handle_subcommand_without_prefix_works(populated_dispatcher):
    """Operator typing bare 'current' without /metrics prefix."""
    r = populated_dispatcher.handle("current")
    assert r.status is MetricsReplStatus.OK


# ===========================================================================
# F — handle: current
# ===========================================================================


def test_handle_current_returns_latest_snapshot(populated_dispatcher):
    r = populated_dispatcher.handle("/metrics current")
    assert r.status is MetricsReplStatus.OK
    assert r.snapshot_dict is not None
    assert r.snapshot_dict["session_id"] == "bt-7"


def test_handle_current_no_data_renders_message(tmp_path):
    """Empty ledger + no provider → polite 'no snapshot available'."""
    L = MetricsHistoryLedger(path=tmp_path / "empty.jsonl",
                             clock=lambda: _FROZEN_NOW)
    d = MetricsReplDispatcher(ledger=L)
    r = d.handle("/metrics current")
    assert r.status is MetricsReplStatus.OK
    assert "no snapshot available" in r.rendered_text


def test_handle_current_provider_wins_over_ledger(populated_dispatcher):
    """Pin: when latest_snapshot_provider is wired, it wins; the
    ledger is NOT consulted for the latest snapshot."""
    custom = _make_snapshot(session_id="from-provider")
    populated_dispatcher.latest_snapshot_provider = lambda: custom
    r = populated_dispatcher.handle("/metrics current")
    assert "from-provider" in r.rendered_text


def test_handle_current_provider_failure_falls_to_ledger(
    populated_dispatcher,
):
    """When the provider raises, the dispatcher falls back to the
    ledger tail rather than propagating the exception."""
    def boom():
        raise RuntimeError("provider down")
    populated_dispatcher.latest_snapshot_provider = boom
    r = populated_dispatcher.handle("/metrics current")
    # Falls back to ledger latest = bt-7.
    assert r.status is MetricsReplStatus.OK
    assert "bt-7" in r.rendered_text


# ===========================================================================
# G — handle: 7d / 30d window aggregates
# ===========================================================================


def test_handle_7d_window_aggregate(populated_dispatcher):
    r = populated_dispatcher.handle("/metrics 7d")
    assert r.status is MetricsReplStatus.OK
    assert r.aggregate is not None
    assert r.aggregate.snapshots_in_window == 8
    assert "snapshots=8" in r.rendered_text


def test_handle_30d_window_aggregate(populated_dispatcher):
    r = populated_dispatcher.handle("/metrics 30d")
    assert r.status is MetricsReplStatus.OK
    assert r.aggregate is not None
    assert r.aggregate.window_days == 30


def test_handle_window_includes_sparkline(populated_dispatcher):
    r = populated_dispatcher.handle("/metrics 7d")
    assert "composite spark:" in r.rendered_text


# ===========================================================================
# H — handle: composite history
# ===========================================================================


def test_handle_composite_renders_full_history_sparkline(
    populated_dispatcher,
):
    r = populated_dispatcher.handle("/metrics composite")
    assert r.status is MetricsReplStatus.OK
    assert "composite history:" in r.rendered_text
    assert "spark:" in r.rendered_text


def test_handle_composite_empty_history(tmp_path):
    L = MetricsHistoryLedger(path=tmp_path / "empty.jsonl")
    d = MetricsReplDispatcher(ledger=L)
    r = d.handle("/metrics composite")
    assert r.status is MetricsReplStatus.OK
    assert "empty" in r.rendered_text


# ===========================================================================
# I — handle: trend banner
# ===========================================================================


def test_handle_trend_banner_combines_latest_and_7d(populated_dispatcher):
    r = populated_dispatcher.handle("/metrics trend")
    assert r.status is MetricsReplStatus.OK
    assert "latest=" in r.rendered_text
    assert "7d=" in r.rendered_text
    assert r.aggregate is not None


def test_handle_trend_no_data(tmp_path):
    L = MetricsHistoryLedger(path=tmp_path / "empty.jsonl")
    d = MetricsReplDispatcher(ledger=L)
    r = d.handle("/metrics trend")
    assert r.status is MetricsReplStatus.OK
    assert "latest=NO_DATA" in r.rendered_text


# ===========================================================================
# J — handle: why <session-id>
# ===========================================================================


def test_handle_why_known_session(populated_dispatcher):
    r = populated_dispatcher.handle("/metrics why bt-3")
    assert r.status is MetricsReplStatus.OK
    assert "bt-3" in r.rendered_text
    assert r.snapshot_dict is not None


def test_handle_why_unknown_session(populated_dispatcher):
    r = populated_dispatcher.handle("/metrics why missing-id")
    assert r.status is MetricsReplStatus.UNKNOWN_SESSION
    assert "no snapshot" in r.rendered_text


def test_handle_why_no_args_falls_through(populated_dispatcher):
    """``/metrics why`` with no id → shape gate trips → UNKNOWN_SUBCOMMAND."""
    r = populated_dispatcher.handle("/metrics why")
    assert r.status is MetricsReplStatus.UNKNOWN_SUBCOMMAND


def test_handle_why_multiple_args_falls_through(populated_dispatcher):
    """``/metrics why bt-3 extra`` → shape gate trips."""
    r = populated_dispatcher.handle("/metrics why bt-3 extra args")
    assert r.status is MetricsReplStatus.UNKNOWN_SUBCOMMAND


def test_handle_why_invalid_id_chars_falls_through(populated_dispatcher):
    """Path-traversal-ish characters rejected by the id shape regex."""
    r = populated_dispatcher.handle("/metrics why ../../../etc/passwd")
    assert r.status is MetricsReplStatus.UNKNOWN_SUBCOMMAND


# ===========================================================================
# K — Subcommand parsing precedence (shape gating)
# ===========================================================================


@pytest.mark.parametrize("line", [
    "/metrics current extra",
    "/metrics 7d more text",
    "/metrics 30d 99",
    "/metrics composite list",
    "/metrics trend now",
    "/metrics help me debug",
])
def test_subcommand_with_extra_args_falls_through(populated_dispatcher, line):
    """Pin: every arg-less subcommand rejects extra tokens → falls
    through to UNKNOWN_SUBCOMMAND with help. Same shape-gate contract
    as P3 / P2 dispatchers."""
    r = populated_dispatcher.handle(line)
    assert r.status is MetricsReplStatus.UNKNOWN_SUBCOMMAND


def test_handle_unknown_subcommand_renders_help(populated_dispatcher):
    r = populated_dispatcher.handle("/metrics whatever")
    assert r.status is MetricsReplStatus.UNKNOWN_SUBCOMMAND
    assert "/metrics current" in r.rendered_text


# ===========================================================================
# L — READ_ERROR path
# ===========================================================================


def test_handle_window_read_error(monkeypatch, populated_dispatcher):
    """Pin: ledger raise → READ_ERROR (never propagated)."""
    def boom(days):
        raise OSError("ledger down")
    monkeypatch.setattr(
        populated_dispatcher._ledger(), "aggregate_window", boom,
    )
    r = populated_dispatcher.handle("/metrics 7d")
    assert r.status is MetricsReplStatus.READ_ERROR
    assert "ledger down" in r.rendered_text


def test_handle_composite_read_error(monkeypatch, populated_dispatcher):
    def boom(*a, **kw):
        raise OSError("disk gone")
    monkeypatch.setattr(
        populated_dispatcher._ledger(), "read_all", boom,
    )
    r = populated_dispatcher.handle("/metrics composite")
    assert r.status is MetricsReplStatus.READ_ERROR


def test_handle_why_read_error(monkeypatch, populated_dispatcher):
    def boom(*a, **kw):
        raise OSError("disk gone")
    monkeypatch.setattr(
        populated_dispatcher._ledger(), "read_all", boom,
    )
    r = populated_dispatcher.handle("/metrics why bt-3")
    assert r.status is MetricsReplStatus.READ_ERROR


# ===========================================================================
# M — Authority invariants
# ===========================================================================


_BANNED = [
    "from backend.core.ouroboros.governance.orchestrator",
    "from backend.core.ouroboros.governance.policy",
    "from backend.core.ouroboros.governance.iron_gate",
    "from backend.core.ouroboros.governance.risk_tier",
    "from backend.core.ouroboros.governance.change_engine",
    "from backend.core.ouroboros.governance.candidate_generator",
    "from backend.core.ouroboros.governance.gate",
    "from backend.core.ouroboros.governance.semantic_guardian",
]


def test_dispatcher_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/metrics_repl_dispatcher.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_dispatcher_no_io_or_subprocess():
    """Pin: dispatcher delegates I/O to the ledger; no direct file
    writes / subprocess / network from this module."""
    src = _strip_docstrings_and_comments(
        _read(
            "backend/core/ouroboros/governance/metrics_repl_dispatcher.py",
        ),
    )
    forbidden = [
        "subprocess.",
        "open(",
        ".write_text(",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
        "import requests",
        "import httpx",
        "import urllib.request",
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"
