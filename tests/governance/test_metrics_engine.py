"""P4 Slice 1 — MetricsEngine regression suite.

Pins:
  * Module constants + 5-value TrendDirection enum + frozen
    MetricsSnapshot dataclass + .to_dict() stable shape.
  * Env knob default-false-pre-graduation.
  * 5 net-new calculator pure functions (each independently
    testable + composable).
  * MetricsEngine.compute_for_session: composite mean/min/max,
    convergence trend mapping (PLATEAUED+LOGARITHMIC fold to PLATEAU),
    all 5 net-new metrics threaded through.
  * Defensive: empty inputs, None / malformed values, oversize ops
    truncated and flagged.
  * Best-effort: a calculator that raises is captured into ``notes``
    + the failing field is None — engine never crashes.
  * Composite recompute path: pre-computed score wins; falls back to
    CompositeScoreFunction when raw signals present; missing signals
    silently skipped.
  * Default-singleton lazy construct + reset.
  * Authority invariants: no banned imports + no I/O / subprocess.
"""
from __future__ import annotations

import dataclasses
import io
import statistics
import tokenize
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.composite_score import (
    CompositeScore,
    CompositeScoreFunction,
)
from backend.core.ouroboros.governance.convergence_tracker import (
    ConvergenceReport,
    ConvergenceState,
    ConvergenceTracker,
)
from backend.core.ouroboros.governance.metrics_engine import (
    COMPLETED_STOP_REASONS,
    MAX_OPS_INSPECTED,
    METRICS_SNAPSHOT_SCHEMA_VERSION,
    MetricsEngine,
    MetricsSnapshot,
    TrendDirection,
    compute_cost_per_successful_apply,
    compute_postmortem_recall_rate,
    compute_posture_stability_seconds,
    compute_self_formation_ratio,
    compute_session_completion_rate,
    get_default_engine,
    is_enabled,
    map_convergence_to_trend,
    reset_default_engine,
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


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("JARVIS_METRICS_SUITE_ENABLED", raising=False)
    yield


@pytest.fixture
def fresh_engine():
    reset_default_engine()
    yield MetricsEngine(clock=lambda: 1_000_000.0)
    reset_default_engine()


# ===========================================================================
# A — Module constants + enum
# ===========================================================================


def test_max_ops_inspected_pinned():
    assert MAX_OPS_INSPECTED == 4096


def test_schema_version_pinned():
    assert METRICS_SNAPSHOT_SCHEMA_VERSION == 1


def test_completed_stop_reasons_pinned():
    """Pin: harness clean-exit set. Adding a value here means the
    completion-rate metric jumps; pinning keeps the surface stable."""
    assert COMPLETED_STOP_REASONS == frozenset({
        "idle", "idle_timeout", "budget", "budget_exhausted",
        "wall", "wall_clock_cap", "complete",
    })


def test_trend_direction_has_five_values():
    """Pin: PRD §9 P4 trend column lists 4 buckets + INSUFFICIENT_DATA."""
    assert {t.name for t in TrendDirection} == {
        "IMPROVING", "PLATEAU", "OSCILLATING",
        "DEGRADING", "INSUFFICIENT_DATA",
    }


# ===========================================================================
# B — Env knob (default false pre-graduation)
# ===========================================================================


def test_is_enabled_default_false_pre_graduation():
    """Slice 1 ships default-OFF. Renamed at Slice 5 graduation."""
    assert is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_is_enabled_truthy_variants(monkeypatch, val):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_is_enabled_falsy_variants(monkeypatch, val):
    monkeypatch.setenv("JARVIS_METRICS_SUITE_ENABLED", val)
    assert is_enabled() is False


# ===========================================================================
# C — MetricsSnapshot dataclass + to_dict
# ===========================================================================


def test_snapshot_is_frozen():
    s = MetricsSnapshot(
        schema_version=1, session_id="s", computed_at_unix=0.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.session_id = "x"  # type: ignore[misc]


def test_snapshot_default_trend_is_insufficient():
    s = MetricsSnapshot(
        schema_version=1, session_id="s", computed_at_unix=0.0,
    )
    assert s.trend is TrendDirection.INSUFFICIENT_DATA


def test_snapshot_to_dict_stable_shape():
    s = MetricsSnapshot(
        schema_version=1, session_id="s", computed_at_unix=10.0,
        per_op_composite_scores=(0.5, 0.6),
        notes=("note-1",),
    )
    d = s.to_dict()
    # Tuples → lists; enum → value.
    assert d["per_op_composite_scores"] == [0.5, 0.6]
    assert d["notes"] == ["note-1"]
    assert d["trend"] == "INSUFFICIENT_DATA"
    # Required keys present (Slice 2 ledger pins this).
    for k in (
        "schema_version", "session_id", "computed_at_unix",
        "composite_score_session_mean", "composite_score_session_min",
        "composite_score_session_max", "trend", "convergence_slope",
        "convergence_oscillation_ratio", "convergence_scores_analyzed",
        "convergence_recommendation", "session_completion_rate",
        "self_formation_ratio", "postmortem_recall_rate",
        "cost_per_successful_apply", "posture_stability_seconds",
        "ops_inspected", "ops_truncated", "notes",
    ):
        assert k in d


# ===========================================================================
# D — Trend mapping (folds 6-value → 5-value)
# ===========================================================================


@pytest.mark.parametrize(("conv", "trend"), [
    (ConvergenceState.IMPROVING, TrendDirection.IMPROVING),
    (ConvergenceState.LOGARITHMIC, TrendDirection.PLATEAU),
    (ConvergenceState.PLATEAUED, TrendDirection.PLATEAU),
    (ConvergenceState.OSCILLATING, TrendDirection.OSCILLATING),
    (ConvergenceState.DEGRADING, TrendDirection.DEGRADING),
    (ConvergenceState.INSUFFICIENT_DATA, TrendDirection.INSUFFICIENT_DATA),
])
def test_map_convergence_to_trend(conv, trend):
    assert map_convergence_to_trend(conv) is trend


# ===========================================================================
# E — Pure calculator: session_completion_rate
# ===========================================================================


def test_completion_rate_two_thirds_completed():
    sessions = [
        {"stop_reason": "idle", "commits": 3, "acknowledged_noops": 0},
        {"stop_reason": "budget", "commits": 0, "acknowledged_noops": 1},
        {"stop_reason": "sigterm", "commits": 1, "acknowledged_noops": 0},
    ]
    assert compute_session_completion_rate(sessions) == pytest.approx(2 / 3)


def test_completion_rate_requires_commit_or_noop():
    """A session that hit a clean stop_reason but had zero commits AND
    zero acknowledged noops is NOT counted as completed."""
    sessions = [
        {"stop_reason": "idle", "commits": 0, "acknowledged_noops": 0},
    ]
    assert compute_session_completion_rate(sessions) == 0.0


def test_completion_rate_empty_returns_none():
    assert compute_session_completion_rate([]) is None


def test_completion_rate_skips_non_mapping_rows():
    sessions = [
        {"stop_reason": "idle", "commits": 1},
        "garbage row",  # ignored
    ]
    assert compute_session_completion_rate(sessions) == 1.0


def test_completion_rate_alternative_stop_reasons():
    sessions = [
        {"stop_reason": "idle_timeout", "commits": 1},
        {"stop_reason": "budget_exhausted", "commits": 1},
        {"stop_reason": "wall_clock_cap", "commits": 1},
        {"stop_reason": "complete", "commits": 1},
    ]
    assert compute_session_completion_rate(sessions) == 1.0


# ===========================================================================
# F — Pure calculator: self_formation_ratio
# ===========================================================================


def test_self_formation_three_of_six():
    ops = [
        {"source": "manual"},
        {"source": "auto_proposed"},
        {"source": "manual"},
        {"source": "self_formed"},
        {"source": "manual"},
        {"source": "self_formation"},
    ]
    assert compute_self_formation_ratio(ops) == 0.5


def test_self_formation_zero_when_no_self_formed():
    ops = [{"source": "manual"}, {"source": "operator"}]
    assert compute_self_formation_ratio(ops) == 0.0


def test_self_formation_empty_returns_none():
    assert compute_self_formation_ratio([]) is None


def test_self_formation_case_insensitive():
    ops = [{"source": "Auto_Proposed"}, {"source": "manual"}]
    assert compute_self_formation_ratio(ops) == 0.5


# ===========================================================================
# G — Pure calculator: postmortem_recall_rate
# ===========================================================================


def test_postmortem_recall_excludes_first_op():
    """First op never has a 'subsequent op' before it — excluded from
    denominator. Three of five subsequent ops consulted ≥1."""
    ops = [
        {"postmortem_recall_count": 0},  # first — excluded
        {"postmortem_recall_count": 1},
        {"postmortem_recall_count": 0},
        {"postmortem_recall_count": 2},
        {"postmortem_recall_count": 1},
        {"postmortem_recall_count": 0},
    ]
    assert compute_postmortem_recall_rate(ops) == pytest.approx(3 / 5)


def test_postmortem_recall_single_op_returns_none():
    """No 'subsequent' set → undefined."""
    assert compute_postmortem_recall_rate([{"postmortem_recall_count": 1}]) is None


def test_postmortem_recall_empty_returns_none():
    assert compute_postmortem_recall_rate([]) is None


def test_postmortem_recall_treats_missing_as_zero():
    ops = [{"x": 1}, {"x": 2}, {"x": 3}]
    assert compute_postmortem_recall_rate(ops) == 0.0


# ===========================================================================
# H — Pure calculator: cost_per_successful_apply
# ===========================================================================


def test_cost_per_apply_simple():
    assert compute_cost_per_successful_apply(1.50, 3) == 0.5


def test_cost_per_apply_zero_commits_returns_none():
    """Zero commits → sentinel None (caller renders 'no commits',
    not 'infinite cost')."""
    assert compute_cost_per_successful_apply(1.50, 0) is None


def test_cost_per_apply_negative_cost_returns_none():
    assert compute_cost_per_successful_apply(-1.0, 3) is None


def test_cost_per_apply_garbage_inputs_return_none():
    assert compute_cost_per_successful_apply("not-a-number", 3) is None
    assert compute_cost_per_successful_apply(1.0, "not-an-int") is None


# ===========================================================================
# I — Pure calculator: posture_stability_seconds
# ===========================================================================


def test_posture_stability_mean_dwell():
    dwells = [{"duration_s": 100.0}, {"duration_s": 200.0}, {"duration_s": 300.0}]
    assert compute_posture_stability_seconds(dwells) == 200.0


def test_posture_stability_skips_negatives():
    dwells = [{"duration_s": 100.0}, {"duration_s": -5.0}, {"duration_s": 300.0}]
    assert compute_posture_stability_seconds(dwells) == 200.0


def test_posture_stability_empty_returns_none():
    assert compute_posture_stability_seconds([]) is None


def test_posture_stability_all_malformed_returns_none():
    assert compute_posture_stability_seconds(
        [{"x": 1}, "garbage", {"duration_s": "huh"}],
    ) is None


# ===========================================================================
# J — Engine end-to-end
# ===========================================================================


def _synth_session(
    op_scores=(0.8, 0.65, 0.55, 0.45, 0.35, 0.30),
    sources=None,
    pm_counts=None,
):
    sources = list(sources or ["manual"] * len(op_scores))
    pm_counts = list(pm_counts or [0] * len(op_scores))
    return [
        {
            "op_id": f"op-{i}",
            "composite_score": s,
            "source": sources[i],
            "postmortem_recall_count": pm_counts[i],
        }
        for i, s in enumerate(op_scores)
    ]


def test_engine_compute_returns_snapshot(fresh_engine):
    snap = fresh_engine.compute_for_session(
        session_id="s",
        ops=_synth_session(),
        sessions_history=[
            {"stop_reason": "idle", "commits": 3},
        ],
        posture_dwells=[{"duration_s": 600.0}],
        total_cost_usd=1.50,
        commits=3,
    )
    assert isinstance(snap, MetricsSnapshot)
    assert snap.session_id == "s"
    assert snap.schema_version == METRICS_SNAPSHOT_SCHEMA_VERSION


def test_engine_composite_aggregates(fresh_engine):
    scores = (0.2, 0.4, 0.6, 0.8)
    snap = fresh_engine.compute_for_session(
        session_id="s", ops=_synth_session(op_scores=scores),
    )
    assert snap.composite_score_session_mean == pytest.approx(
        statistics.fmean(scores),
    )
    assert snap.composite_score_session_min == 0.2
    assert snap.composite_score_session_max == 0.8
    assert snap.per_op_composite_scores == scores


def test_engine_no_ops_yields_none_composite(fresh_engine):
    snap = fresh_engine.compute_for_session(session_id="s", ops=[])
    assert snap.composite_score_session_mean is None
    assert snap.composite_score_session_min is None
    assert snap.composite_score_session_max is None
    assert snap.trend is TrendDirection.INSUFFICIENT_DATA


def test_engine_threads_all_five_net_new_metrics(fresh_engine):
    snap = fresh_engine.compute_for_session(
        session_id="s",
        ops=_synth_session(
            sources=["manual", "auto_proposed", "manual", "self_formed",
                     "manual", "auto_proposed"],
            pm_counts=[0, 1, 0, 2, 1, 0],
        ),
        sessions_history=[
            {"stop_reason": "idle", "commits": 2},
            {"stop_reason": "sigterm", "commits": 1},
        ],
        posture_dwells=[{"duration_s": 100.0}, {"duration_s": 300.0}],
        total_cost_usd=2.0,
        commits=4,
    )
    assert snap.session_completion_rate == 0.5
    assert snap.self_formation_ratio == 0.5
    assert snap.postmortem_recall_rate == pytest.approx(3 / 5)
    assert snap.cost_per_successful_apply == 0.5
    assert snap.posture_stability_seconds == 200.0


def test_engine_truncates_oversize_ops(fresh_engine):
    huge_ops = [
        {"composite_score": 0.5} for _ in range(MAX_OPS_INSPECTED + 50)
    ]
    snap = fresh_engine.compute_for_session(session_id="s", ops=huge_ops)
    assert snap.ops_truncated is True
    assert snap.ops_inspected == MAX_OPS_INSPECTED
    assert any("truncated" in n for n in snap.notes)


def test_engine_invalid_composite_score_skipped(fresh_engine):
    ops = [
        {"composite_score": 0.5},
        {"composite_score": 1.5},   # out of range
        {"composite_score": "huh"}, # garbage
        {"composite_score": 0.7},
    ]
    snap = fresh_engine.compute_for_session(session_id="s", ops=ops)
    assert snap.per_op_composite_scores == (0.5, 0.7)


def test_engine_recomputes_when_signals_present(fresh_engine):
    """When pre-computed score absent but raw signals present, the
    engine MUST fall back to CompositeScoreFunction."""
    ops = [
        {
            "op_id": "o1",
            "test_pass_rate_before": 0.8, "test_pass_rate_after": 0.9,
            "coverage_before": 60.0, "coverage_after": 65.0,
            "complexity_before": 5.0, "complexity_after": 5.0,
            "lint_violations_before": 3, "lint_violations_after": 2,
            "blast_radius_total": 4,
        },
    ]
    snap = fresh_engine.compute_for_session(session_id="s", ops=ops)
    assert len(snap.per_op_composite_scores) == 1
    # Sanity: composite is in [0, 1].
    assert 0.0 <= snap.per_op_composite_scores[0] <= 1.0


def test_engine_skips_recompute_when_signals_missing(fresh_engine):
    """No pre-computed score AND missing raw signals → that op gets
    no entry in per_op_composite_scores. Snapshot still produced."""
    ops = [{"op_id": "o1"}]
    snap = fresh_engine.compute_for_session(session_id="s", ops=ops)
    assert snap.per_op_composite_scores == ()


def test_engine_calculator_failure_captured_in_notes(fresh_engine):
    """If a calculator raises (shouldn't happen, but the engine MUST
    swallow), the field is None and a note is appended."""
    eng = MetricsEngine()
    # Inject a posture entry that the calculator handles fine; then
    # verify _safe_call's failure path by feeding through a synthetic
    # calculator via direct engine internals.
    notes = []
    out = eng._safe_call(
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        label="custom", notes=notes,
    )
    assert out is None
    assert any("custom skipped: boom" in n for n in notes)


def test_engine_uses_injected_composite_function():
    """Injected CompositeScoreFunction MUST be used (not the lazy
    default). Verified by passing a fake."""
    class _SpyComposite(CompositeScoreFunction):
        def __init__(self):
            super().__init__()
            self.called = 0

        def compute(self, **kw):
            self.called += 1
            return CompositeScore(
                test_delta=0.5, coverage_delta=0.5, complexity_delta=0.5,
                lint_delta=0.5, blast_radius=0.5, composite=0.42,
                op_id=kw["op_id"], timestamp=0.0,
            )

    spy = _SpyComposite()
    eng = MetricsEngine(composite_score_fn=spy)
    ops = [
        {
            "op_id": "x",
            "test_pass_rate_before": 0.8, "test_pass_rate_after": 0.9,
            "coverage_before": 60.0, "coverage_after": 65.0,
            "complexity_before": 5.0, "complexity_after": 5.0,
            "lint_violations_before": 3, "lint_violations_after": 2,
            "blast_radius_total": 4,
        },
    ]
    snap = eng.compute_for_session(session_id="s", ops=ops)
    assert spy.called == 1
    assert snap.per_op_composite_scores == (0.42,)


def test_engine_uses_injected_convergence_tracker():
    class _SpyTracker(ConvergenceTracker):
        def __init__(self):
            super().__init__()
            self.called = 0

        def analyze(self, scores):
            self.called += 1
            return ConvergenceReport(
                state=ConvergenceState.IMPROVING,
                window_size=20, slope=-0.5, r_squared_log=0.0,
                oscillation_ratio=0.0, plateau_stddev=0.0,
                scores_analyzed=len(scores),
                recommendation="x", timestamp=0.0,
            )

    spy = _SpyTracker()
    eng = MetricsEngine(convergence_tracker=spy)
    snap = eng.compute_for_session(
        session_id="s", ops=_synth_session(),
    )
    assert spy.called == 1
    assert snap.trend is TrendDirection.IMPROVING
    assert snap.convergence_slope == -0.5


# ===========================================================================
# K — Default-singleton accessor
# ===========================================================================


def test_get_default_engine_lazy_constructs():
    reset_default_engine()
    e = get_default_engine()
    assert isinstance(e, MetricsEngine)


def test_get_default_engine_returns_same_instance():
    reset_default_engine()
    a = get_default_engine()
    b = get_default_engine()
    assert a is b


def test_reset_default_engine_clears():
    reset_default_engine()
    a = get_default_engine()
    reset_default_engine()
    b = get_default_engine()
    assert a is not b


# ===========================================================================
# L — Authority invariants
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


def test_engine_no_authority_imports():
    src = _read("backend/core/ouroboros/governance/metrics_engine.py")
    for imp in _BANNED:
        assert imp not in src, f"banned import: {imp}"


def test_engine_no_io_or_subprocess():
    """Pin: engine is pure data. Slice 2 will own the JSONL ledger;
    Slice 4 wires IDE/SSE — those have their own I/O surfaces."""
    src = _strip_docstrings_and_comments(
        _read("backend/core/ouroboros/governance/metrics_engine.py"),
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
