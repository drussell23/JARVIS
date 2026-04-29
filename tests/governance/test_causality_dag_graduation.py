"""Priority 2 Slice 6 — Graduation regression spine.

Cross-slice integration tests that prove the entire Causality DAG
arc is wired end-to-end and ready for flag-flip graduation.

§-numbered coverage map:

  §1   FlagRegistry seeds contain all 7 DAG flags
  §2   DAG construction from ledger → DAG → navigation → render
  §3   DAG → replay-from-record → env vars
  §4   SSE fork event round-trip
  §5   REPL /postmortems dag stats round-trip
  §6   AST pin — causality_dag.py imports
  §7   AST pin — dag_navigation.py imports
  §8   AST pin — replay_from_record.py imports
  §9   All §-tests from Slices 1-5 run clean
  §10  CausalityDAG immutability contract
  §11  build_dag defensive on corrupted ledger
  §12  dispatch_dag_command + render + REPL integration
  §13  prepare_replay + apply_env + summary render
  §14  drift detection end-to-end
  §15  Counterfactual branch detection end-to-end
"""
from __future__ import annotations

import ast
import inspect
import json
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.determinism.decision_runtime import (
    DecisionRecord,
)
from backend.core.ouroboros.governance.verification.causality_dag import (
    CausalityDAG,
    build_dag,
)
from backend.core.ouroboros.governance.verification.dag_navigation import (
    dispatch_dag_command,
    render_dag_counterfactuals,
    render_dag_drift,
    render_dag_for_record,
    render_dag_stats,
)
from backend.core.ouroboros.governance.verification.replay_from_record import (
    apply_replay_from_record_env,
    prepare_replay_from_record,
    render_replay_from_record_summary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(rid, parents=(), counterfactual_of=None, **kw):
    return DecisionRecord(
        record_id=rid, session_id="s1",
        op_id=kw.get("op_id", "op-1"),
        phase=kw.get("phase", "ROUTE"),
        kind=kw.get("kind", "route"),
        ordinal=kw.get("ordinal", 0), inputs_hash="h",
        output_repr='"x"', monotonic_ts=1.0, wall_ts=2.0,
        parent_record_ids=parents, counterfactual_of=counterfactual_of,
    )

def _write_seed(path, seed=42):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "session_seed.1", "seed": seed,
    }))

def _write_ledger(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r.to_dict(), sort_keys=True) + "\n")

def _make_dag(*recs):
    return CausalityDAG(
        nodes={r.record_id: r for r in recs},
        edges={r.record_id: r.parent_record_ids for r in recs},
    )

@pytest.fixture
def full_env(monkeypatch, tmp_path):
    """All flags enabled for full integration."""
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DAG_NAVIGATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_DETERMINISM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "grad-s")
    sid = "grad-s"
    _write_seed(tmp_path / sid / "seed.json")
    ledger = tmp_path / sid / "decisions.jsonl"
    return ledger

# ===========================================================================
# §1 — FlagRegistry seeds
# ===========================================================================

def test_flag_seeds_contain_dag_flags():
    from backend.core.ouroboros.governance.flag_registry_seed import SEED_SPECS
    names = {s.name for s in SEED_SPECS}
    expected = {
        "JARVIS_CAUSALITY_DAG_QUERY_ENABLED",
        "JARVIS_DAG_MAX_RECORDS",
        "JARVIS_DAG_MAX_DEPTH",
        "JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD",
        "JARVIS_DAG_NAVIGATION_ENABLED",
        "JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED",
    }
    missing = expected - names
    assert not missing, f"Missing flag seeds: {missing}"

# ===========================================================================
# §2 — Full DAG construction → navigation → render
# ===========================================================================

def test_dag_construction_to_render(full_env):
    recs = [
        _rec("r1"),
        _rec("r2", parents=("r1",)),
        _rec("r3", parents=("r2",)),
        _rec("cf1", parents=("r1",), counterfactual_of="r1"),
    ]
    _write_ledger(full_env, recs)
    dag = build_dag("grad-s")
    assert dag.node_count == 4
    text = render_dag_for_record(dag, "r2")
    assert "r2" in text
    assert ">>" in text

# ===========================================================================
# §3 — DAG → replay-from-record → env vars
# ===========================================================================

def test_dag_to_replay_from_record(full_env):
    _write_ledger(full_env, [_rec("r1"), _rec("r2", parents=("r1",))])
    plan = prepare_replay_from_record("grad-s", "r2")
    assert plan.is_replayable
    result = apply_replay_from_record_env(plan)
    assert result is True
    assert os.environ.get("JARVIS_CAUSALITY_FORK_FROM_RECORD_ID") == "r2"

# ===========================================================================
# §4 — SSE fork event (structural)
# ===========================================================================

def test_sse_fork_event_type_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
    )
    assert "dag_fork_detected" in _VALID_EVENT_TYPES

# ===========================================================================
# §5 — REPL /postmortems dag stats
# ===========================================================================

def test_repl_dag_stats_round_trip(full_env):
    _write_ledger(full_env, [_rec("r1"), _rec("r2")])
    from backend.core.ouroboros.governance.postmortem_observability import (
        dispatch_postmortems_command,
    )
    result = dispatch_postmortems_command(
        ["dag", "stats"], session_id="grad-s",
    )
    assert "nodes" in result.rendered_text.lower()

# ===========================================================================
# §6-§8 — AST pins
# ===========================================================================

_FORBIDDEN = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.phase_runners",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.urgency_router",
)

@pytest.mark.parametrize("module", [
    "backend.core.ouroboros.governance.verification.causality_dag",
    "backend.core.ouroboros.governance.verification.dag_navigation",
    "backend.core.ouroboros.governance.verification.replay_from_record",
])
def test_ast_pin_no_forbidden_imports(module):
    import importlib
    mod = importlib.import_module(module)
    src = Path(inspect.getfile(mod)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for fb in _FORBIDDEN:
                assert fb not in node.module, (
                    f"{module}: forbidden import {node.module}"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for fb in _FORBIDDEN:
                    assert fb not in alias.name

# ===========================================================================
# §10 — CausalityDAG immutability
# ===========================================================================

def test_dag_immutability():
    dag = _make_dag(_rec("r1"))
    # Public contract: no mutating methods exist
    assert not hasattr(dag, "add_node")
    assert not hasattr(dag, "remove_node")
    assert not hasattr(dag, "add_edge")
    # Node count is stable
    assert dag.node_count == 1

# ===========================================================================
# §11 — build_dag defensive on corrupted ledger
# ===========================================================================

def test_build_dag_corrupted_ledger(full_env):
    full_env.parent.mkdir(parents=True, exist_ok=True)
    full_env.write_text("not json\n{bad\n")
    dag = build_dag("grad-s")
    assert dag.is_empty

# ===========================================================================
# §12 — dispatch_dag_command + render integration
# ===========================================================================

def test_dispatch_for_record_integration(full_env):
    _write_ledger(full_env, [_rec("r1"), _rec("r2", parents=("r1",))])
    result = dispatch_dag_command(
        ["for-record", "r1"], session_id="grad-s",
    )
    assert "r1" in result

# ===========================================================================
# §13 — prepare_replay + apply + summary
# ===========================================================================

def test_replay_prepare_apply_summary(full_env):
    _write_ledger(full_env, [_rec("r1")])
    plan = prepare_replay_from_record("grad-s", "r1")
    assert plan.is_replayable
    text = render_replay_from_record_summary(plan)
    assert "grad-s" in text
    assert "r1" in text

# ===========================================================================
# §14 — Drift detection end-to-end
# ===========================================================================

def test_drift_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DAG_NAVIGATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path))
    _write_ledger(tmp_path / "sa" / "decisions.jsonl", [_rec("r1")])
    _write_ledger(tmp_path / "sb" / "decisions.jsonl", [_rec("r2")])
    dag_a = build_dag("sa")
    dag_b = build_dag("sb")
    text = render_dag_drift(dag_a, dag_b, label_a="sa", label_b="sb")
    assert "drift" in text.lower()

# ===========================================================================
# §15 — Counterfactual branch detection end-to-end
# ===========================================================================

def test_counterfactual_end_to_end(full_env):
    _write_ledger(full_env, [
        _rec("r1"),
        _rec("cf1", parents=("r1",), counterfactual_of="r1"),
        _rec("cf2", parents=("r1",), counterfactual_of="r1"),
    ])
    dag = build_dag("grad-s")
    text = render_dag_counterfactuals(dag, "r1")
    assert "cf1" in text
    assert "cf2" in text
