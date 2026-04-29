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


# ===========================================================================
# §16-§19 — shipped_code_invariants seeds registered + holding
# ===========================================================================
#
# Slice 6 graduation adds 4 new structural invariants pinning the
# Causality DAG arc's authority + read-only + cost-contract contracts.
# These are AST-walked at boot + APPLY; future refactors that violate
# them fail the build.


_EXPECTED_DAG_INVARIANTS = (
    "causality_dag_no_authority_imports",
    "causality_dag_bounded_traversal",
    "dag_navigation_no_ctx_mutation",
    "dag_replay_cost_contract_preserved",
)


def test_all_four_dag_invariants_registered() -> None:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        list_shipped_code_invariants,
    )
    invs = list_shipped_code_invariants()
    names = {inv.invariant_name for inv in invs}
    for expected in _EXPECTED_DAG_INVARIANTS:
        assert expected in names, (
            f"missing invariant: {expected}"
        )


def test_causality_dag_no_authority_imports_invariant_holds() -> None:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        validate_all,
    )
    violations = validate_all()
    matches = [
        v for v in violations
        if v.invariant_name == "causality_dag_no_authority_imports"
    ]
    assert matches == [], (
        f"causality_dag authority pin violated: "
        f"{[v.detail for v in matches]}"
    )


def test_causality_dag_bounded_traversal_invariant_holds() -> None:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        validate_all,
    )
    violations = validate_all()
    matches = [
        v for v in violations
        if v.invariant_name == "causality_dag_bounded_traversal"
    ]
    assert matches == [], (
        f"bounded traversal pin violated: "
        f"{[v.detail for v in matches]}"
    )


def test_dag_navigation_no_ctx_mutation_invariant_holds() -> None:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        validate_all,
    )
    violations = validate_all()
    matches = [
        v for v in violations
        if v.invariant_name == "dag_navigation_no_ctx_mutation"
    ]
    assert matches == [], (
        f"navigation read-only pin violated: "
        f"{[v.detail for v in matches]}"
    )


def test_dag_replay_cost_contract_preserved_invariant_holds() -> None:
    """The replay-from-record path MUST go through the existing
    orchestrator entry point — no shortcut bypass of §26.6 four-layer
    defense. Pinned by validating that scripts/ouroboros_battle_test.py
    references prepare_replay_from_record + apply_replay_from_record_env
    AND requires --rerun for session identity AND contains zero
    direct provider construction tokens."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        validate_all,
    )
    violations = validate_all()
    matches = [
        v for v in violations
        if v.invariant_name == "dag_replay_cost_contract_preserved"
    ]
    assert matches == [], (
        f"cost contract preservation pin violated: "
        f"{[v.detail for v in matches]}"
    )


# ===========================================================================
# §20-§22 — Master flag default-true source-grep pins
# ===========================================================================


def test_decision_runtime_source_has_graduated_literals() -> None:
    """decision_runtime.py — schema + per-worker master + per-worker
    enforce all carry `return True  # graduated default` post-Slice-6."""
    from backend.core.ouroboros.governance.determinism import decision_runtime
    src = Path(inspect.getfile(decision_runtime)).read_text()
    occurrences = src.count("return True  # graduated default")
    assert occurrences >= 3, (
        f"expected >= 3 graduated-default literals in decision_runtime "
        f"(schema + per_worker_enabled + per_worker_enforce), "
        f"found {occurrences}"
    )
    # Source-grep the Slice 6 graduation marker
    assert src.count("Slice 6") >= 3


def test_dag_modules_source_have_graduated_literals() -> None:
    """causality_dag + dag_navigation + replay_from_record all carry
    the graduated literal post-Slice-6."""
    from backend.core.ouroboros.governance.verification import (
        causality_dag, dag_navigation, replay_from_record,
    )
    total = 0
    for mod in (causality_dag, dag_navigation, replay_from_record):
        src = Path(inspect.getfile(mod)).read_text()
        total += src.count("return True  # graduated default")
    assert total >= 3, (
        f"expected >= 3 graduated-default literals across "
        f"causality_dag + dag_navigation + replay_from_record, "
        f"found {total}"
    )


def test_six_master_flags_default_true_at_runtime() -> None:
    """End-to-end runtime check — all 6 Causality DAG master flags
    default true after the Slice 6 graduation flip."""
    import os
    for k in (
        "JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED",
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED",
        "JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE",
        "JARVIS_CAUSALITY_DAG_QUERY_ENABLED",
        "JARVIS_DAG_NAVIGATION_ENABLED",
        "JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED",
    ):
        os.environ.pop(k, None)
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        causality_dag_schema_enabled, per_worker_ordinals_enabled,
        per_worker_ordinals_enforce,
    )
    from backend.core.ouroboros.governance.verification.causality_dag import (
        dag_query_enabled,
    )
    from backend.core.ouroboros.governance.verification.dag_navigation import (
        dag_navigation_enabled,
    )
    from backend.core.ouroboros.governance.verification.replay_from_record import (
        replay_from_record_enabled,
    )
    assert causality_dag_schema_enabled() is True
    assert per_worker_ordinals_enabled() is True
    assert per_worker_ordinals_enforce() is True
    assert dag_query_enabled() is True
    assert dag_navigation_enabled() is True
    assert replay_from_record_enabled() is True


# ===========================================================================
# §23 — FlagRegistry seeds for all 9 Causality DAG flags
# ===========================================================================


_EXPECTED_REGISTERED_DAG_FLAGS = {
    "JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED": True,
    "JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED": True,
    "JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE": True,
    "JARVIS_CAUSALITY_DAG_QUERY_ENABLED": True,
    "JARVIS_DAG_NAVIGATION_ENABLED": True,
    "JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED": True,
    "JARVIS_DAG_MAX_RECORDS": 100_000,
    "JARVIS_DAG_MAX_DEPTH": 8,
    "JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD": 0.25,
}


def test_flag_registry_has_all_nine_dag_flags() -> None:
    from backend.core.ouroboros.governance.flag_registry import ensure_seeded
    registry = ensure_seeded()
    for flag_name, expected_default in _EXPECTED_REGISTERED_DAG_FLAGS.items():
        assert flag_name in registry._specs, (
            f"flag {flag_name} not registered in FlagRegistry"
        )
        spec = registry._specs[flag_name]
        assert spec.default == expected_default, (
            f"{flag_name}: default mismatch (got {spec.default}, "
            f"expected {expected_default})"
        )


# ===========================================================================
# §24-§26 — COST CONTRACT load-bearing tests (4-layer defense-in-depth)
# ===========================================================================
#
# Critical: the Causality DAG arc must NOT introduce any path that
# escalates a BG/SPEC op to Claude. The §26.6 four-layer defense
# (AST invariant + runtime CostContractViolation + Property Oracle
# claim + advisor structural guard) MUST hold under all DAG /
# replay states.


def test_cost_contract_layer_4_advisor_guard_still_fires() -> None:
    """Layer 4 — confidence_route_advisor structural guard fires
    regardless of any DAG / replay flag state. Synthetic
    BG/SPEC → escalation attempt MUST raise CostContractViolation."""
    from backend.core.ouroboros.governance.cost_contract_assertion import (
        CostContractViolation,
    )
    from backend.core.ouroboros.governance.verification.confidence_route_advisor import (
        _propose_route_change,
    )
    with pytest.raises(CostContractViolation):
        _propose_route_change(
            current_route="background",
            proposed_route="standard",  # ESCALATION
            reason_code="should_not_happen_under_dag_arc",
            confidence_basis="post_slice6_graduation_test",
        )


def test_cost_contract_layer_2_runtime_assertion_still_fires() -> None:
    """Layer 2 — providers.py runtime CostContractViolation gate
    fires on any BG/SPEC + Claude + non-read-only attempt,
    independent of DAG / replay flags."""
    from backend.core.ouroboros.governance.cost_contract_assertion import (
        CostContractViolation, assert_provider_route_compatible,
    )
    with pytest.raises(CostContractViolation):
        assert_provider_route_compatible(
            op_id="op-test",
            provider_route="speculative",
            provider_tier="claude",
            is_read_only=False,
        )


def test_cost_contract_replay_path_does_not_introduce_bypass() -> None:
    """Slice 5 replay path goes through the existing orchestrator
    entry point — pinned by the dag_replay_cost_contract_preserved
    shipped_code_invariants seed. Re-validate at runtime."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        validate_all,
    )
    violations = validate_all()
    assert violations == (), (
        f"shipped_code_invariants violations against main: "
        f"{[v.invariant_name + ': ' + v.detail for v in violations]}"
    )


def test_total_invariant_count_post_slice_6_graduation() -> None:
    """Pre-Slice-6: 7 invariants (Priority 1 Slice 5 + §26.6 seeds).
    Post-Slice-6: 11 invariants (4 new DAG seeds added)."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        list_shipped_code_invariants,
    )
    invs = list_shipped_code_invariants()
    assert len(invs) >= 11, (
        f"expected >= 11 shipped_code_invariants post-Slice-6, "
        f"found {len(invs)}"
    )


# ===========================================================================
# §27-§29 — Cross-slice authority survival
# ===========================================================================


_FORBIDDEN_DAG_IMPORTS = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.phase_runners",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
    "backend.core.ouroboros.governance.urgency_router",
)


def _no_forbidden_imports(mod) -> None:
    src = Path(inspect.getfile(mod)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for forbidden in _FORBIDDEN_DAG_IMPORTS:
                    assert forbidden not in alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for forbidden in _FORBIDDEN_DAG_IMPORTS:
                assert forbidden not in node.module


def test_causality_dag_authority_isolation() -> None:
    from backend.core.ouroboros.governance.verification import causality_dag
    _no_forbidden_imports(causality_dag)


def test_dag_navigation_authority_isolation() -> None:
    from backend.core.ouroboros.governance.verification import dag_navigation
    _no_forbidden_imports(dag_navigation)


def test_replay_from_record_authority_isolation() -> None:
    from backend.core.ouroboros.governance.verification import replay_from_record
    _no_forbidden_imports(replay_from_record)


# ===========================================================================
# §30 — Hot-revert proof for all 6 master flags
# ===========================================================================


def test_six_master_flags_hot_revert_via_explicit_false(monkeypatch) -> None:
    """Operator can hot-revert each graduated flag independently
    via `export FLAG=false`. Critical for incident response."""
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        causality_dag_schema_enabled, per_worker_ordinals_enabled,
        per_worker_ordinals_enforce,
    )
    from backend.core.ouroboros.governance.verification.causality_dag import (
        dag_query_enabled,
    )
    from backend.core.ouroboros.governance.verification.dag_navigation import (
        dag_navigation_enabled,
    )
    from backend.core.ouroboros.governance.verification.replay_from_record import (
        replay_from_record_enabled,
    )
    flag_pairs = [
        ("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", causality_dag_schema_enabled),
        ("JARVIS_DAG_PER_WORKER_ORDINALS_ENABLED", per_worker_ordinals_enabled),
        ("JARVIS_DAG_PER_WORKER_ORDINALS_ENFORCE", per_worker_ordinals_enforce),
        ("JARVIS_CAUSALITY_DAG_QUERY_ENABLED", dag_query_enabled),
        ("JARVIS_DAG_NAVIGATION_ENABLED", dag_navigation_enabled),
        ("JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED", replay_from_record_enabled),
    ]
    for env_name, flag_fn in flag_pairs:
        monkeypatch.setenv(env_name, "false")
        assert flag_fn() is False, (
            f"hot-revert failed for {env_name}"
        )
        monkeypatch.delenv(env_name, raising=False)
