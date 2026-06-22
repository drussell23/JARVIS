"""Sovereign Egress Interceptor Mesh — T3 route-back tests.

Covers the three legs of T3 (spec 2026-06-22 §4.3):

  (a) ``LocalEgressOverweightError`` classifies to
      ``FailureSource.LOCAL_EGRESS_OVERWEIGHT`` at weight 0.0 — it is OUR
      mistake, never a vendor rupture, so it MUST mirror FSM_EXHAUSTED and
      never trip the topology breaker / surface-health.

  (b) ``decompose_for_block(goal, zero_coverage=False, compression_target=N)``
      slices so every returned sub-goal's estimated payload <= N chars, OR a
      single irreducible symbol is emitted with a WARNING (never silently
      exceeds).

  (c) The LIVE generate-failure path (the extracted
      ``phase_runners/generate_runner.py`` ``except`` block) routes a
      ``LOCAL_EGRESS_OVERWEIGHT`` op to ``decompose_for_block`` with
      ``compression_target=error.max_allowed_size``. We assert the structural
      seam (``orchestrator._decompose_block_or_legacy`` accepts the kwarg and
      threads it through) because the deep runner site is only reachable with a
      full live stack.

All behavior is fail-soft + OFF byte-identical (no behavior change when no
overweight occurs). ASCII only.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance.dw_egress_interceptor import (
    LocalEgressOverweightError,
    estimate_body_chars,
)
from backend.core.ouroboros.governance import goal_decomposition_planner as gdp
from backend.core.ouroboros.governance.goal_decomposition_planner import (
    decompose_for_block,
)
from backend.core.ouroboros.governance import topology_sentinel as ts
from backend.core.ouroboros.governance.dw_fault_taxonomy import (
    is_local_egress_overweight,
    is_fsm_exhaustion,
)
from backend.core.ouroboros.governance.ast_symbol_scoper import ScopedTarget


# ---------------------------------------------------------------------------
# Test goal stub + injectable scoper
# ---------------------------------------------------------------------------


class _Goal:
    def __init__(self, goal_id, title, description, target_files):
        self.goal_id = goal_id
        self.title = title
        self.description = description
        self.target_files = target_files


def _make_scoper(symbol_sizes):
    """Return an injectable scoper + the source registry it reads.

    ``symbol_sizes`` maps ``symbol_name -> approx_char_size``. The scoper
    returns one ScopedTarget per symbol on file ``f.py`` with line ranges
    chosen so the planner's source-segment estimator measures ~that many
    chars. We register the synthetic source in the planner's reader hook.
    """
    # Build a synthetic source whose per-symbol line ranges sum to the sizes.
    lines = []
    targets = []
    lineno = 1
    CHARS_PER_LINE = 50
    for name, size in symbol_sizes.items():
        n_lines = max(1, size // CHARS_PER_LINE)
        start = lineno
        for _ in range(n_lines):
            lines.append("x" * (CHARS_PER_LINE - 1))  # -1 for newline
            lineno += 1
        end = lineno - 1
        targets.append(ScopedTarget("f.py", name, start, end))
    source = "\n".join(lines) + "\n"

    def _scoper(file_path, description):  # noqa: ARG001
        return tuple(targets)

    return _scoper, source


# ---------------------------------------------------------------------------
# (a) FailureSource.LOCAL_EGRESS_OVERWEIGHT — weight 0.0, mirror FSM_EXHAUSTED
# ---------------------------------------------------------------------------


def test_local_egress_overweight_failure_source_exists():
    assert hasattr(ts.FailureSource, "LOCAL_EGRESS_OVERWEIGHT")
    assert ts.FailureSource.LOCAL_EGRESS_OVERWEIGHT.value == "local_egress_overweight"


def test_local_egress_overweight_weight_is_zero(monkeypatch):
    # Clear any env override so we read the default map.
    monkeypatch.delenv(
        "JARVIS_TOPOLOGY_WEIGHT_LOCAL_EGRESS_OVERWEIGHT", raising=False
    )
    assert ts.failure_weight(ts.FailureSource.LOCAL_EGRESS_OVERWEIGHT) == 0.0
    # Mirrors FSM_EXHAUSTED exactly.
    assert ts.failure_weight(ts.FailureSource.FSM_EXHAUSTED) == 0.0


def test_local_egress_overweight_never_trips_breaker(monkeypatch):
    """A streak of LOCAL_EGRESS_OVERWEIGHT failures must NOT trip the
    weighted-streak breaker (weight 0.0, mirroring FSM_EXHAUSTED)."""
    # Sentinel must be live for the breaker path to engage at all.
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.delenv("JARVIS_TOPOLOGY_FORCE_SEVERED", raising=False)
    sentinel = ts.TopologySentinel()
    model = "egress-test-model"
    sentinel.register_endpoint(model)
    # Hammer the same model far past any reasonable threshold.
    for _ in range(50):
        sentinel.report_failure(
            model, ts.FailureSource.LOCAL_EGRESS_OVERWEIGHT, "overweight"
        )
    # Weight 0.0 -> the weighted streak never accumulates -> breaker CLOSED.
    snap = sentinel._snapshots.get(model)
    assert snap is not None
    assert snap.weighted_failure_streak == 0.0
    assert sentinel.get_state(model) == "CLOSED"

    # FSM_EXHAUSTED behaves IDENTICALLY (the pattern we mirror).
    fsm_model = "fsm-test-model"
    sentinel.register_endpoint(fsm_model)
    for _ in range(50):
        sentinel.report_failure(
            fsm_model, ts.FailureSource.FSM_EXHAUSTED, "exhausted"
        )
    assert sentinel._snapshots[fsm_model].weighted_failure_streak == 0.0
    assert sentinel.get_state(fsm_model) == "CLOSED"


def test_taxonomy_classifies_local_egress_overweight():
    err = LocalEgressOverweightError(
        attempted_size=1_000_000, max_allowed_size=600_000, model="m"
    )
    assert is_local_egress_overweight(err) is True
    # A genuine FSM exhaustion is NOT an egress-overweight.
    assert is_local_egress_overweight(RuntimeError("all_providers_exhausted")) is False
    # And vice-versa: an egress-overweight is NOT mislabeled FSM-exhausted.
    assert is_fsm_exhaustion(err) is False


def test_taxonomy_local_egress_overweight_never_raises():
    # NEVER raises -> False on weird input.
    assert is_local_egress_overweight(None) is False  # type: ignore[arg-type]
    assert is_local_egress_overweight(RuntimeError("nothing")) is False


# ---------------------------------------------------------------------------
# (b) decompose_for_block(compression_target=N)
# ---------------------------------------------------------------------------


def test_decompose_compression_target_kwarg_exists():
    sig = inspect.signature(decompose_for_block)
    assert "compression_target" in sig.parameters
    assert sig.parameters["compression_target"].default is None


def test_decompose_without_target_is_byte_identical():
    """OFF byte-identical: compression_target=None must equal the legacy call."""
    scoper, _ = _make_scoper({"A": 100, "B": 100})
    goal = _Goal("g1", "do the thing", "mutate A and B", ("f.py",))
    legacy = decompose_for_block(goal, zero_coverage=False, scoper=scoper)
    explicit_none = decompose_for_block(
        goal, zero_coverage=False, scoper=scoper, compression_target=None
    )
    assert [s.to_dict() for s in legacy] == [s.to_dict() for s in explicit_none]


def test_decompose_splits_to_fit_compression_target(monkeypatch):
    """Every returned mutation sub-goal's estimated payload <= target."""
    # 4 symbols ~300 chars each; target 1000 -> must split into >=2 groups.
    scoper, source = _make_scoper({"A": 300, "B": 300, "C": 300, "D": 300})
    monkeypatch.setattr(
        gdp, "_read_source_for_estimate", lambda fp: source, raising=False
    )
    goal = _Goal("g2", "refactor many", "touch A B C D", ("f.py",))
    subs = decompose_for_block(
        goal, zero_coverage=False, scoper=scoper, compression_target=1000
    )
    assert len(subs) >= 1
    for sub in subs:
        payload = gdp.estimate_subgoal_payload_chars(sub, source_reader=lambda fp: source)
        # Each sub-goal that carries >1 symbol must fit; irreducible singles
        # may exceed but must be logged (asserted in the irreducible test).
        if len(sub.scoped_symbols) > 1:
            assert payload <= 1000, (
                f"sub {sub.sub_goal_id} payload {payload} > 1000"
            )


def test_decompose_irreducible_symbol_logged(monkeypatch, caplog):
    """A single symbol bigger than the target is emitted with a WARNING and
    never silently dropped."""
    scoper, source = _make_scoper({"Huge": 5000})
    monkeypatch.setattr(
        gdp, "_read_source_for_estimate", lambda fp: source, raising=False
    )
    goal = _Goal("g3", "mutate huge", "rewrite Huge", ("f.py",))
    import logging
    caplog.set_level(logging.WARNING)
    subs = decompose_for_block(
        goal, zero_coverage=False, scoper=scoper, compression_target=1000
    )
    # The symbol is NOT lost.
    all_syms = [s for sub in subs for s in sub.scoped_symbols]
    assert any("Huge" in s for s in all_syms)
    # A clear irreducible WARNING was emitted.
    assert any(
        "irreducible" in rec.message.lower() for rec in caplog.records
    ), [r.message for r in caplog.records]


# ---------------------------------------------------------------------------
# (c) LIVE generate-failure seam: orchestrator._decompose_block_or_legacy
#     accepts compression_target and threads it to decompose_for_block.
# ---------------------------------------------------------------------------


def test_orchestrator_decompose_seam_accepts_compression_target():
    from backend.core.ouroboros.governance.orchestrator import Orchestrator
    sig = inspect.signature(Orchestrator._decompose_block_or_legacy)
    assert "compression_target" in sig.parameters
    assert sig.parameters["compression_target"].default is None


def test_generate_runner_live_site_routes_overweight():
    """Structural assertion: the LIVE generate-failure handler (extracted
    runner) references LOCAL_EGRESS_OVERWEIGHT / egress-overweight detection
    and routes to the compression-target decompose seam. The deep call is only
    reachable with a full live stack, so we pin the wiring statically."""
    import backend.core.ouroboros.governance.phase_runners.generate_runner as gr
    src = inspect.getsource(gr)
    assert "is_local_egress_overweight" in src or "LocalEgressOverweightError" in src
    assert "compression_target" in src
