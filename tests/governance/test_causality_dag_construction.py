"""Priority 2 Slice 3 — Causality DAG construction regression spine.

§-numbered coverage map:

  §1   Master flag default false
  §2   Master-off build_dag returns empty (no I/O)
  §3   build_dag reads JSONL and populates nodes
  §4   build_dag respects max_records bound
  §5   build_dag respects JARVIS_DAG_MAX_RECORDS env
  §6   node() O(1) lookup
  §7   parents() returns correct parent_record_ids
  §8   children() returns correct children via reverse-edge
  §9   subgraph() linear chain bounded by max_depth
  §10  subgraph() diamond DAG
  §11  subgraph() tree bounded upstream+downstream
  §12  subgraph() respects JARVIS_DAG_MAX_DEPTH env
  §13  counterfactual_branches() detects forks
  §14  counterfactual_branches() empty when no forks
  §15  topological_order() linear chain
  §16  topological_order() diamond DAG
  §17  topological_order() cycle detection
  §18  cluster_kind() confidence_collapse_cluster
  §19  cluster_kind() no pattern → unknown
  §20  Empty ledger → empty DAG
  §21  Missing ledger → empty DAG
  §22  Malformed JSONL rows skipped
  §23  Pre-Slice-1 records (no parents) → leaf nodes
  §24  DAG node_count / edge_count properties
  §25  JARVIS_DAG_MAX_RECORDS defensive bounds
  §26  JARVIS_DAG_MAX_DEPTH defensive bounds
  §27  JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD bounds
  §28  AST authority — no forbidden imports
  §29  build_dag never raises on corrupt ledger
  §30  Multi-session isolation
  §31  subgraph max_depth=0 returns single node
  §32  topological_order on empty DAG
  §33  Large fixture bounded traversal
  §34  cluster_kind repeated_failure_cluster
  §35  cluster_kind counterfactual_fork_cluster
  §36  CausalityDAG repr
  §37  record_ids property
  §38  is_empty property
"""
from __future__ import annotations

import ast
import inspect
import json
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.determinism.decision_runtime import (
    SCHEMA_VERSION, DecisionRecord,
)
from backend.core.ouroboros.governance.verification import causality_dag
from backend.core.ouroboros.governance.verification.causality_dag import (
    CausalityDAG, build_dag, dag_query_enabled,
    drift_threshold_knob, max_depth_knob, max_records_knob,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(rid, parents=(), counterfactual_of=None, **kw):
    base = dict(
        record_id=rid, session_id="s1", op_id=kw.get("op_id", "op-1"),
        phase=kw.get("phase", "ROUTE"), kind=kw.get("kind", "route"),
        ordinal=kw.get("ordinal", 0), inputs_hash="h",
        output_repr='"x"', monotonic_ts=1.0, wall_ts=2.0,
        parent_record_ids=parents, counterfactual_of=counterfactual_of,
    )
    return DecisionRecord(**base)

def _write_ledger(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r.to_dict(), sort_keys=True) + "\n")

@pytest.fixture
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "test-session")
    return tmp_path / "test-session" / "decisions.jsonl"

# ---------------------------------------------------------------------------
# §1 — Master flag default false
# ---------------------------------------------------------------------------

def test_master_flag_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", raising=False)
    assert dag_query_enabled() is False

@pytest.mark.parametrize("val", ["", " ", "\t"])
def test_master_flag_empty_default_false(monkeypatch, val):
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", val)
    assert dag_query_enabled() is False

@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_master_flag_truthy(monkeypatch, val):
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", val)
    assert dag_query_enabled() is True

# ---------------------------------------------------------------------------
# §2 — Master-off build_dag returns empty
# ---------------------------------------------------------------------------

def test_master_off_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "false")
    dag = build_dag("s1")
    assert dag.is_empty
    assert dag.node_count == 0

# ---------------------------------------------------------------------------
# §3 — build_dag reads JSONL
# ---------------------------------------------------------------------------

def test_build_dag_reads_jsonl(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    assert dag.node_count == 2
    assert dag.node("r1") is not None
    assert dag.node("r2") is not None

# ---------------------------------------------------------------------------
# §4 — build_dag respects max_records
# ---------------------------------------------------------------------------

def test_build_dag_max_records_bound(ledger_dir):
    recs = [_rec(f"r{i}") for i in range(20)]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session", max_records=5)
    assert dag.node_count == 5

# ---------------------------------------------------------------------------
# §5 — JARVIS_DAG_MAX_RECORDS env
# ---------------------------------------------------------------------------

def test_build_dag_env_max_records(monkeypatch, ledger_dir):
    monkeypatch.setenv("JARVIS_DAG_MAX_RECORDS", "3")
    recs = [_rec(f"r{i}") for i in range(10)]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    assert dag.node_count == 3

# ---------------------------------------------------------------------------
# §6 — node() O(1) lookup
# ---------------------------------------------------------------------------

def test_node_found(ledger_dir):
    _write_ledger(ledger_dir, [_rec("r1")])
    dag = build_dag("test-session")
    assert dag.node("r1").record_id == "r1"

def test_node_not_found(ledger_dir):
    _write_ledger(ledger_dir, [_rec("r1")])
    dag = build_dag("test-session")
    assert dag.node("missing") is None

# ---------------------------------------------------------------------------
# §7 — parents()
# ---------------------------------------------------------------------------

def test_parents_correct(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    assert dag.parents("r2") == ("r1",)
    assert dag.parents("r1") == ()

# ---------------------------------------------------------------------------
# §8 — children()
# ---------------------------------------------------------------------------

def test_children_correct(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",)), _rec("r3", parents=("r1",))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    children = dag.children("r1")
    assert set(children) == {"r2", "r3"}

# ---------------------------------------------------------------------------
# §9 — subgraph() linear chain
# ---------------------------------------------------------------------------

def test_subgraph_linear_chain(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",)),
            _rec("r3", parents=("r2",)), _rec("r4", parents=("r3",)),
            _rec("r5", parents=("r4",))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    sub = dag.subgraph("r3", max_depth=1)
    assert sub.node_count == 3  # r2, r3, r4

# ---------------------------------------------------------------------------
# §10 — subgraph() diamond DAG
# ---------------------------------------------------------------------------

def test_subgraph_diamond(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",)),
            _rec("r3", parents=("r1",)), _rec("r4", parents=("r2", "r3"))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    sub = dag.subgraph("r4", max_depth=2)
    assert sub.node_count == 4

# ---------------------------------------------------------------------------
# §11 — subgraph() tree bounded
# ---------------------------------------------------------------------------

def test_subgraph_tree_bounded(ledger_dir):
    recs = [_rec("root")]
    for i in range(10):
        recs.append(_rec(f"c{i}", parents=("root",)))
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    sub = dag.subgraph("root", max_depth=1)
    assert sub.node_count == 11  # root + 10 children

# ---------------------------------------------------------------------------
# §12 — subgraph() respects JARVIS_DAG_MAX_DEPTH env
# ---------------------------------------------------------------------------

def test_subgraph_env_max_depth(monkeypatch, ledger_dir):
    monkeypatch.setenv("JARVIS_DAG_MAX_DEPTH", "1")
    recs = [_rec("r1"), _rec("r2", parents=("r1",)),
            _rec("r3", parents=("r2",)), _rec("r4", parents=("r3",))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    sub = dag.subgraph("r2")  # default max_depth from env = 1
    assert "r1" in sub.record_ids
    assert "r3" in sub.record_ids
    assert "r4" not in sub.record_ids

# ---------------------------------------------------------------------------
# §13 — counterfactual_branches() detects forks
# ---------------------------------------------------------------------------

def test_counterfactual_branches_found(ledger_dir):
    recs = [_rec("r1"), _rec("cf1", parents=("r1",), counterfactual_of="r1"),
            _rec("cf2", parents=("r1",), counterfactual_of="r1")]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    branches = dag.counterfactual_branches("r1")
    assert set(branches) == {"cf1", "cf2"}

# ---------------------------------------------------------------------------
# §14 — counterfactual_branches() empty when no forks
# ---------------------------------------------------------------------------

def test_counterfactual_branches_empty(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    assert dag.counterfactual_branches("r1") == ()

# ---------------------------------------------------------------------------
# §15 — topological_order() linear chain
# ---------------------------------------------------------------------------

def test_topological_order_chain(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",)), _rec("r3", parents=("r2",))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    order = dag.topological_order()
    assert order.index("r1") < order.index("r2") < order.index("r3")

# ---------------------------------------------------------------------------
# §16 — topological_order() diamond
# ---------------------------------------------------------------------------

def test_topological_order_diamond(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",)),
            _rec("r3", parents=("r1",)), _rec("r4", parents=("r2", "r3"))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    order = dag.topological_order()
    assert len(order) == 4
    assert order.index("r1") < order.index("r2")
    assert order.index("r1") < order.index("r3")
    assert order.index("r2") < order.index("r4")
    assert order.index("r3") < order.index("r4")

# ---------------------------------------------------------------------------
# §17 — topological_order() cycle detection
# ---------------------------------------------------------------------------

def test_topological_order_cycle_returns_empty(ledger_dir):
    # Simulate a cycle: r1→r2→r1 (inject manually)
    dag = CausalityDAG(
        nodes={"r1": _rec("r1", parents=("r2",)),
               "r2": _rec("r2", parents=("r1",))},
        edges={"r1": ("r2",), "r2": ("r1",)},
    )
    order = dag.topological_order()
    assert order == ()

# ---------------------------------------------------------------------------
# §18 — cluster_kind() confidence_collapse_cluster
# ---------------------------------------------------------------------------

def test_cluster_kind_confidence_collapse():
    root = _rec("root")
    drops = [_rec(f"d{i}", parents=("root",), kind="confidence_drop")
             for i in range(3)]
    dag = CausalityDAG(
        nodes={r.record_id: r for r in [root] + drops},
        edges={r.record_id: r.parent_record_ids for r in [root] + drops},
    )
    assert dag.cluster_kind(drops) == "confidence_collapse_cluster"

# ---------------------------------------------------------------------------
# §19 — cluster_kind() no pattern → unknown
# ---------------------------------------------------------------------------

def test_cluster_kind_unknown():
    recs = [_rec("r1"), _rec("r2")]
    dag = CausalityDAG(
        nodes={r.record_id: r for r in recs},
        edges={r.record_id: () for r in recs},
    )
    assert dag.cluster_kind(recs) == "unknown"

# ---------------------------------------------------------------------------
# §20 — Empty ledger → empty DAG
# ---------------------------------------------------------------------------

def test_empty_ledger(ledger_dir):
    ledger_dir.parent.mkdir(parents=True, exist_ok=True)
    ledger_dir.write_text("")
    dag = build_dag("test-session")
    assert dag.is_empty

# ---------------------------------------------------------------------------
# §21 — Missing ledger → empty DAG
# ---------------------------------------------------------------------------

def test_missing_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path))
    dag = build_dag("nonexistent-session")
    assert dag.is_empty

# ---------------------------------------------------------------------------
# §22 — Malformed JSONL rows skipped
# ---------------------------------------------------------------------------

def test_malformed_rows_skipped(ledger_dir):
    ledger_dir.parent.mkdir(parents=True, exist_ok=True)
    good = _rec("r1")
    with ledger_dir.open("w") as f:
        f.write("not-json\n")
        f.write("{}\n")  # valid JSON, but not a valid record
        f.write(json.dumps(good.to_dict(), sort_keys=True) + "\n")
    dag = build_dag("test-session")
    assert dag.node_count == 1
    assert dag.node("r1") is not None

# ---------------------------------------------------------------------------
# §23 — Pre-Slice-1 records (no parents) → leaf nodes
# ---------------------------------------------------------------------------

def test_pre_slice1_records_are_leaves(ledger_dir):
    pre = {
        "record_id": "old-1", "session_id": "s1", "op_id": "op",
        "phase": "ROUTE", "kind": "route", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"x"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
        "schema_version": SCHEMA_VERSION,
    }
    ledger_dir.parent.mkdir(parents=True, exist_ok=True)
    ledger_dir.write_text(json.dumps(pre, sort_keys=True) + "\n")
    dag = build_dag("test-session")
    assert dag.node_count == 1
    assert dag.parents("old-1") == ()
    assert dag.children("old-1") == ()

# ---------------------------------------------------------------------------
# §24 — node_count / edge_count
# ---------------------------------------------------------------------------

def test_node_edge_counts(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",)),
            _rec("r3", parents=("r1", "r2"))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    assert dag.node_count == 3
    assert dag.edge_count == 3  # r2→r1, r3→r1, r3→r2

# ---------------------------------------------------------------------------
# §25 — JARVIS_DAG_MAX_RECORDS defensive bounds
# ---------------------------------------------------------------------------

def test_max_records_knob_clamp_low(monkeypatch):
    monkeypatch.setenv("JARVIS_DAG_MAX_RECORDS", "-5")
    assert max_records_knob() == 1

def test_max_records_knob_clamp_high(monkeypatch):
    monkeypatch.setenv("JARVIS_DAG_MAX_RECORDS", "99999999")
    assert max_records_knob() == 1_000_000

def test_max_records_knob_default(monkeypatch):
    monkeypatch.delenv("JARVIS_DAG_MAX_RECORDS", raising=False)
    assert max_records_knob() == 100_000

# ---------------------------------------------------------------------------
# §26 — JARVIS_DAG_MAX_DEPTH defensive bounds
# ---------------------------------------------------------------------------

def test_max_depth_knob_clamp_low(monkeypatch):
    monkeypatch.setenv("JARVIS_DAG_MAX_DEPTH", "0")
    assert max_depth_knob() == 1

def test_max_depth_knob_clamp_high(monkeypatch):
    monkeypatch.setenv("JARVIS_DAG_MAX_DEPTH", "999")
    assert max_depth_knob() == 64

def test_max_depth_knob_default(monkeypatch):
    monkeypatch.delenv("JARVIS_DAG_MAX_DEPTH", raising=False)
    assert max_depth_knob() == 8

# ---------------------------------------------------------------------------
# §27 — JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD bounds
# ---------------------------------------------------------------------------

def test_drift_threshold_clamp_low(monkeypatch):
    monkeypatch.setenv("JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD", "0.001")
    assert drift_threshold_knob() == pytest.approx(0.01)

def test_drift_threshold_clamp_high(monkeypatch):
    monkeypatch.setenv("JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD", "5.0")
    assert drift_threshold_knob() == pytest.approx(1.0)

def test_drift_threshold_default(monkeypatch):
    monkeypatch.delenv("JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD", raising=False)
    assert drift_threshold_knob() == pytest.approx(0.30)

# ---------------------------------------------------------------------------
# §28 — AST authority invariants
# ---------------------------------------------------------------------------

def test_no_forbidden_imports():
    src = Path(inspect.getfile(causality_dag)).read_text()
    tree = ast.parse(src)
    forbidden = (
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
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fb in forbidden:
                    assert fb not in alias.name, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            for fb in forbidden:
                assert fb not in node.module, f"Forbidden import: {node.module}"

def test_only_allowed_cross_module_imports():
    """causality_dag.py may only import from stdlib + determinism.*"""
    src = Path(inspect.getfile(causality_dag)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("backend."):
                assert (
                    node.module.startswith(
                        "backend.core.ouroboros.governance.determinism."
                    )
                ), f"Unexpected import: {node.module}"

# ---------------------------------------------------------------------------
# §29 — build_dag never raises on corrupt ledger
# ---------------------------------------------------------------------------

def test_build_dag_corrupt_ledger(ledger_dir):
    ledger_dir.parent.mkdir(parents=True, exist_ok=True)
    ledger_dir.write_bytes(b"\x00\xff\xfe corrupt binary garbage\n")
    dag = build_dag("test-session")
    assert dag.is_empty  # graceful degradation

# ---------------------------------------------------------------------------
# §30 — Multi-session isolation
# ---------------------------------------------------------------------------

def test_multi_session_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path))
    p1 = tmp_path / "s1" / "decisions.jsonl"
    p2 = tmp_path / "s2" / "decisions.jsonl"
    _write_ledger(p1, [_rec("r1")])
    _write_ledger(p2, [_rec("r2"), _rec("r3")])
    dag1 = build_dag("s1")
    dag2 = build_dag("s2")
    assert dag1.node_count == 1
    assert dag2.node_count == 2

# ---------------------------------------------------------------------------
# §31 — subgraph max_depth=0 returns single node
# ---------------------------------------------------------------------------

def test_subgraph_depth_zero(ledger_dir):
    recs = [_rec("r1"), _rec("r2", parents=("r1",))]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    sub = dag.subgraph("r1", max_depth=0)
    assert sub.node_count == 1
    assert sub.node("r1") is not None

# ---------------------------------------------------------------------------
# §32 — topological_order on empty DAG
# ---------------------------------------------------------------------------

def test_topological_order_empty():
    dag = CausalityDAG()
    assert dag.topological_order() == ()

# ---------------------------------------------------------------------------
# §33 — Large fixture bounded traversal
# ---------------------------------------------------------------------------

def test_large_fixture_bounded(ledger_dir):
    recs = [_rec("r0")]
    for i in range(1, 100):
        recs.append(_rec(f"r{i}", parents=(f"r{i-1}",)))
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    assert dag.node_count == 100
    sub = dag.subgraph("r50", max_depth=3)
    assert sub.node_count <= 7  # 3 up + center + 3 down

# ---------------------------------------------------------------------------
# §34 — cluster_kind repeated_failure_cluster
# ---------------------------------------------------------------------------

def test_cluster_kind_repeated_failure():
    recs = [_rec(f"f{i}", kind="validate_fail", op_id="op-x") for i in range(3)]
    dag = CausalityDAG(
        nodes={r.record_id: r for r in recs},
        edges={r.record_id: () for r in recs},
    )
    assert dag.cluster_kind(recs) == "repeated_failure_cluster"

# ---------------------------------------------------------------------------
# §35 — cluster_kind counterfactual_fork_cluster
# ---------------------------------------------------------------------------

def test_cluster_kind_counterfactual_fork():
    parent = _rec("parent")
    forks = [_rec(f"cf{i}", parents=("parent",), counterfactual_of="parent")
             for i in range(2)]
    dag = CausalityDAG(
        nodes={r.record_id: r for r in [parent] + forks},
        edges={r.record_id: r.parent_record_ids for r in [parent] + forks},
    )
    assert dag.cluster_kind(forks) == "counterfactual_fork_cluster"

# ---------------------------------------------------------------------------
# §36 — repr
# ---------------------------------------------------------------------------

def test_dag_repr():
    dag = CausalityDAG(nodes={"r1": _rec("r1")}, edges={"r1": ()})
    assert "nodes=1" in repr(dag)

# ---------------------------------------------------------------------------
# §37 — record_ids property
# ---------------------------------------------------------------------------

def test_record_ids_property(ledger_dir):
    recs = [_rec("r1"), _rec("r2")]
    _write_ledger(ledger_dir, recs)
    dag = build_dag("test-session")
    assert set(dag.record_ids) == {"r1", "r2"}

# ---------------------------------------------------------------------------
# §38 — is_empty property
# ---------------------------------------------------------------------------

def test_is_empty_true():
    assert CausalityDAG().is_empty is True

def test_is_empty_false():
    dag = CausalityDAG(nodes={"r1": _rec("r1")}, edges={"r1": ()})
    assert dag.is_empty is False
