"""§31 U2 empirical wiring Slice 1 — causality_consumer substrate
regression spine.

Pins per operator binding 2026-05-05 ("solve root, no shortcuts,
leverage existing"):

  * Master flag default-FALSE (§33.1 graduation contract pattern)
  * CausalDecisionAdvice closed 5-value taxonomy bytes-pinned
  * compute_op_causal_features composes canonical build_dag —
    NEVER constructs CausalityDAG directly
  * Authority asymmetry — substrate forbids orchestrator/iron_gate/
    policy/providers imports + .record() calls on decision/runtime/
    ledger receivers
  * NEVER raises across all paths
  * Determinism: same DAG + same record_id → bytes-identical
    artifact (insertion-order BFS preserved)
  * is_advisory_blocking single source of truth for friction logic
  * AST pins fire on synthetic regressions
  * Public API stable
  * CausalityDAG.ancestors_of read-API extension correctly walks
    parent edges, excludes self, respects max_depth, NEVER raises

Verifies (42 tests).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Stub DAG builder for deterministic feature tests
# ---------------------------------------------------------------------------


@dataclass
class _StubRec:
    record_id: str
    op_id: str = ""
    kind: str = "k"
    phase: str = "GENERATE"
    parent_record_ids: Tuple[str, ...] = ()
    inputs_hash: str = ""
    outputs_hash: str = ""


def _make_dag(records, edges):
    """Build a CausalityDAG from stubs. ``edges`` is a dict
    record_id → tuple of parent record_ids."""
    from backend.core.ouroboros.governance.verification.causality_dag import (  # noqa: E501
        CausalityDAG,
    )
    nodes = {r.record_id: r for r in records}
    return CausalityDAG(nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# CausalityDAG.ancestors_of read-API extension
# ---------------------------------------------------------------------------


def test_ancestors_of_walks_one_hop():
    dag = _make_dag(
        [_StubRec("a"), _StubRec("b")],
        {"b": ("a",)},
    )
    assert dag.ancestors_of("b") == ("a",)


def test_ancestors_of_walks_multi_hop_in_bfs_order():
    # a → b → c → d (linear chain)
    dag = _make_dag(
        [
            _StubRec("a"), _StubRec("b"),
            _StubRec("c"), _StubRec("d"),
        ],
        {"b": ("a",), "c": ("b",), "d": ("c",)},
    )
    out = dag.ancestors_of("d", max_depth=8)
    # BFS from d: parents at d-1 = c; d-2 = b; d-3 = a
    assert out == ("c", "b", "a")


def test_ancestors_of_excludes_self():
    dag = _make_dag(
        [_StubRec("a"), _StubRec("b")], {"b": ("a",)},
    )
    assert "b" not in dag.ancestors_of("b")


def test_ancestors_of_respects_max_depth():
    dag = _make_dag(
        [
            _StubRec("a"), _StubRec("b"),
            _StubRec("c"), _StubRec("d"),
        ],
        {"b": ("a",), "c": ("b",), "d": ("c",)},
    )
    # max_depth=1 → only depth-1 ancestors
    assert dag.ancestors_of("d", max_depth=1) == ("c",)
    # max_depth=2 → depth-1 + depth-2
    assert dag.ancestors_of("d", max_depth=2) == ("c", "b")


def test_ancestors_of_returns_empty_on_unknown_record():
    dag = _make_dag([_StubRec("a")], {})
    assert dag.ancestors_of("not_a_real_id") == ()


def test_ancestors_of_returns_empty_on_blank_input():
    dag = _make_dag([_StubRec("a")], {})
    assert dag.ancestors_of("") == ()
    assert dag.ancestors_of(None) == ()  # type: ignore


def test_ancestors_of_returns_empty_on_zero_depth():
    dag = _make_dag(
        [_StubRec("a"), _StubRec("b")], {"b": ("a",)},
    )
    assert dag.ancestors_of("b", max_depth=0) == ()
    assert dag.ancestors_of("b", max_depth=-1) == ()


def test_ancestors_of_handles_diamond():
    # diamond: a, b → c → d ; a, b → e → d
    dag = _make_dag(
        [
            _StubRec("a"), _StubRec("b"), _StubRec("c"),
            _StubRec("e"), _StubRec("d"),
        ],
        {
            "c": ("a", "b"),
            "e": ("a", "b"),
            "d": ("c", "e"),
        },
    )
    out = set(dag.ancestors_of("d", max_depth=8))
    # All four upstream nodes reached, no duplicates
    assert out == {"a", "b", "c", "e"}


def test_ancestors_of_never_raises_on_garbage():
    dag = _make_dag([_StubRec("a")], {})
    # Garbage max_depth
    assert dag.ancestors_of("a", max_depth="not_int") == ()  # type: ignore
    assert dag.ancestors_of("a", max_depth=None) == ()  # type: ignore


# ---------------------------------------------------------------------------
# Master flag — default FALSE per §33.1
# ---------------------------------------------------------------------------


def test_master_flag_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        is_consumer_enabled,
    )
    assert is_consumer_enabled() is False


def test_master_flag_truthy_values(monkeypatch):
    from backend.core.ouroboros.governance.causality_consumer import (
        is_consumer_enabled,
    )
    for v in ("1", "true", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv(
            "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", v,
        )
        assert is_consumer_enabled() is True, v


def test_master_flag_falsy_values(monkeypatch):
    from backend.core.ouroboros.governance.causality_consumer import (
        is_consumer_enabled,
    )
    for v in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv(
            "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", v,
        )
        assert is_consumer_enabled() is False, v


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_advice_taxonomy_has_5_values():
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice,
    )
    assert len(list(CausalDecisionAdvice)) == 5


def test_advice_values_bytes_pinned():
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice,
    )
    assert {a.value for a in CausalDecisionAdvice} == {
        "neutral", "recurrence_warning", "sibling_dedup",
        "deep_lineage_harden", "disabled",
    }


# ---------------------------------------------------------------------------
# is_advisory_blocking
# ---------------------------------------------------------------------------


def test_is_advisory_blocking_neutral_false():
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, is_advisory_blocking,
    )
    assert is_advisory_blocking(CausalDecisionAdvice.NEUTRAL) is False


def test_is_advisory_blocking_disabled_false():
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, is_advisory_blocking,
    )
    assert is_advisory_blocking(CausalDecisionAdvice.DISABLED) is False


def test_is_advisory_blocking_sibling_dedup_false():
    """SIBLING_DEDUP is informational, not friction-raising."""
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, is_advisory_blocking,
    )
    assert is_advisory_blocking(
        CausalDecisionAdvice.SIBLING_DEDUP,
    ) is False


def test_is_advisory_blocking_recurrence_true():
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, is_advisory_blocking,
    )
    assert is_advisory_blocking(
        CausalDecisionAdvice.RECURRENCE_WARNING,
    ) is True


def test_is_advisory_blocking_deep_lineage_true():
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, is_advisory_blocking,
    )
    assert is_advisory_blocking(
        CausalDecisionAdvice.DEEP_LINEAGE_HARDEN,
    ) is True


def test_is_advisory_blocking_handles_none_and_garbage():
    from backend.core.ouroboros.governance.causality_consumer import (
        is_advisory_blocking,
    )
    assert is_advisory_blocking(None) is False
    assert is_advisory_blocking("not_an_enum") is False  # type: ignore
    assert is_advisory_blocking(42) is False  # type: ignore


# ---------------------------------------------------------------------------
# OpCausalFeatures round-trip
# ---------------------------------------------------------------------------


def test_features_round_trip():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s1",
        record_id="r1",
        ancestor_count=5,
        distinct_phases_in_lineage=("GENERATE", "VALIDATE"),
        sibling_count=2,
        recurrence_score=0.3,
        parent_decisions_summary="parent-1",
        advice=CausalDecisionAdvice.NEUTRAL,
    )
    d = f.to_dict()
    rt = OpCausalFeatures.from_dict(d)
    assert rt.session_id == "s1"
    assert rt.record_id == "r1"
    assert rt.ancestor_count == 5
    assert rt.distinct_phases_in_lineage == ("GENERATE", "VALIDATE")
    assert rt.sibling_count == 2
    assert abs(rt.recurrence_score - 0.3) < 1e-9
    assert rt.advice == CausalDecisionAdvice.NEUTRAL


def test_features_from_dict_handles_unknown_advice():
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, OpCausalFeatures,
    )
    rt = OpCausalFeatures.from_dict({"advice": "not_a_real_advice"})
    # Defensive — unknown advice → NEUTRAL
    assert rt.advice == CausalDecisionAdvice.NEUTRAL


def test_features_from_dict_handles_garbage():
    from backend.core.ouroboros.governance.causality_consumer import (
        OpCausalFeatures,
    )
    # Bad inputs MUST NOT raise
    rt = OpCausalFeatures.from_dict({"ancestor_count": "not_int"})
    assert rt is not None


def test_features_summary_truncated_to_256():
    from backend.core.ouroboros.governance.causality_consumer import (
        CAUSAL_FEATURES_SCHEMA_VERSION, CausalDecisionAdvice,
        OpCausalFeatures,
    )
    f = OpCausalFeatures(
        schema_version=CAUSAL_FEATURES_SCHEMA_VERSION,
        session_id="s",
        record_id="r",
        ancestor_count=0,
        distinct_phases_in_lineage=(),
        sibling_count=0,
        recurrence_score=0.0,
        parent_decisions_summary="x" * 1000,
        advice=CausalDecisionAdvice.NEUTRAL,
    )
    d = f.to_dict()
    assert len(d["parent_decisions_summary"]) == 256


# ---------------------------------------------------------------------------
# compute_op_causal_features — empty / disabled paths
# ---------------------------------------------------------------------------


def test_compute_returns_disabled_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, compute_op_causal_features,
    )
    f = compute_op_causal_features(
        session_id="s", record_id="r",
    )
    assert f.advice == CausalDecisionAdvice.DISABLED
    assert f.ancestor_count == 0


def test_compute_returns_neutral_on_blank_inputs(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, compute_op_causal_features,
    )
    f = compute_op_causal_features(
        session_id="", record_id="",
    )
    # Blank inputs short-circuit to empty (NEUTRAL when master on)
    assert f.advice == CausalDecisionAdvice.NEUTRAL
    assert f.ancestor_count == 0


# ---------------------------------------------------------------------------
# compute_op_causal_features — feature extraction (master on,
# DAG injected)
# ---------------------------------------------------------------------------


def _patch_build_dag(monkeypatch, dag):
    """Patch the lazy-imported build_dag so compute_op_causal_features
    walks the injected DAG."""
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification."
        "causality_dag.build_dag",
        lambda **kwargs: dag,
    )


@pytest.fixture
def consumer_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_DECISION_CONSUMER_ENABLED", "1",
    )
    yield


def test_compute_with_no_lineage(consumer_on, monkeypatch):
    dag = _make_dag([_StubRec("solo")], {})
    _patch_build_dag(monkeypatch, dag)
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, compute_op_causal_features,
    )
    f = compute_op_causal_features(
        session_id="s", record_id="solo",
    )
    assert f.ancestor_count == 0
    assert f.advice == CausalDecisionAdvice.NEUTRAL


def test_compute_with_short_lineage(consumer_on, monkeypatch):
    # 3-hop chain — under deep-lineage threshold default 12
    records = [_StubRec(f"r{i}", phase="GENERATE") for i in range(4)]
    edges = {f"r{i}": (f"r{i-1}",) for i in range(1, 4)}
    dag = _make_dag(records, edges)
    _patch_build_dag(monkeypatch, dag)
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, compute_op_causal_features,
    )
    f = compute_op_causal_features(
        session_id="s", record_id="r3",
    )
    assert f.ancestor_count == 3
    # All 4 share signature → recurrence_score = 1.0 → triggers
    # RECURRENCE_WARNING (precedes SIBLING_DEDUP)
    assert f.recurrence_score == 1.0
    assert f.advice == CausalDecisionAdvice.RECURRENCE_WARNING


def test_compute_triggers_deep_lineage_advice(
    consumer_on, monkeypatch,
):
    monkeypatch.setenv("JARVIS_CAUSAL_DEEP_LINEAGE_THRESHOLD", "5")
    # 6-hop chain — exceeds threshold (5)
    records = [_StubRec(f"r{i}") for i in range(7)]
    edges = {f"r{i}": (f"r{i-1}",) for i in range(1, 7)}
    dag = _make_dag(records, edges)
    _patch_build_dag(monkeypatch, dag)
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, compute_op_causal_features,
    )
    f = compute_op_causal_features(
        session_id="s", record_id="r6",
    )
    assert f.ancestor_count == 6
    assert f.advice == CausalDecisionAdvice.DEEP_LINEAGE_HARDEN


def test_compute_triggers_sibling_dedup(
    consumer_on, monkeypatch,
):
    monkeypatch.setenv("JARVIS_CAUSAL_SIBLING_DEDUP_THRESHOLD", "2")
    # parent p has 3 children: target + 2 siblings; signatures
    # differ so recurrence stays low; ancestor count below
    # deep-lineage default 12.
    records = [
        _StubRec("p", kind="parent"),
        _StubRec("target", kind="t1"),
        _StubRec("sib1", kind="t2"),
        _StubRec("sib2", kind="t3"),
    ]
    edges = {
        "target": ("p",),
        "sib1": ("p",),
        "sib2": ("p",),
    }
    dag = _make_dag(records, edges)
    _patch_build_dag(monkeypatch, dag)
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, compute_op_causal_features,
    )
    f = compute_op_causal_features(
        session_id="s", record_id="target",
    )
    assert f.sibling_count == 2
    assert f.advice == CausalDecisionAdvice.SIBLING_DEDUP


def test_compute_extracts_distinct_phases(
    consumer_on, monkeypatch,
):
    records = [
        _StubRec("p1", phase="CLASSIFY"),
        _StubRec("p2", phase="GENERATE"),
        _StubRec("p3", phase="GENERATE"),  # dup → not in distinct
        _StubRec("target", phase="VALIDATE"),
    ]
    edges = {
        "p2": ("p1",),
        "p3": ("p2",),
        "target": ("p3",),
    }
    dag = _make_dag(records, edges)
    _patch_build_dag(monkeypatch, dag)
    from backend.core.ouroboros.governance.causality_consumer import (
        compute_op_causal_features,
    )
    f = compute_op_causal_features(
        session_id="s", record_id="target",
    )
    # Distinct phases of ANCESTORS (excludes target itself) —
    # BFS from target: p3(GENERATE) → p2(GENERATE) → p1(CLASSIFY)
    assert f.distinct_phases_in_lineage == ("GENERATE", "CLASSIFY")


def test_compute_determinism(consumer_on, monkeypatch):
    """Same DAG + same record_id → bytes-identical artifact."""
    records = [_StubRec(f"r{i}") for i in range(5)]
    edges = {f"r{i}": (f"r{i-1}",) for i in range(1, 5)}
    dag = _make_dag(records, edges)
    _patch_build_dag(monkeypatch, dag)
    from backend.core.ouroboros.governance.causality_consumer import (
        compute_op_causal_features,
    )
    f1 = compute_op_causal_features(
        session_id="s", record_id="r4",
    )
    f2 = compute_op_causal_features(
        session_id="s", record_id="r4",
    )
    assert f1.to_dict() == f2.to_dict()


def test_compute_never_raises_when_dag_unavailable(
    consumer_on, monkeypatch,
):
    """If build_dag raises, return empty/NEUTRAL without crashing."""
    def _broken(*a, **kw):
        raise RuntimeError("simulated DAG failure")
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification."
        "causality_dag.build_dag",
        _broken,
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        CausalDecisionAdvice, compute_op_causal_features,
    )
    f = compute_op_causal_features(
        session_id="s", record_id="r",
    )
    # Doesn't raise; returns sane default
    assert f.advice in (
        CausalDecisionAdvice.NEUTRAL,
        CausalDecisionAdvice.DISABLED,
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_4():
    from backend.core.ouroboros.governance.causality_consumer import (
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "causal_decision_advice_taxonomy_closed",
        "causal_consumer_master_flag_default_false",
        "causal_consumer_authority_asymmetry",
        "causal_consumer_composes_canonical_dag",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.causality_consumer import (
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/causality_consumer.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_taxonomy_pin_fires_on_unauthorized_addition():
    from backend.core.ouroboros.governance.causality_consumer import (
        register_shipped_invariants,
    )
    bad = '''
import enum
class CausalDecisionAdvice(str, enum.Enum):
    NEUTRAL = "neutral"
    RECURRENCE_WARNING = "recurrence_warning"
    SIBLING_DEDUP = "sibling_dedup"
    DEEP_LINEAGE_HARDEN = "deep_lineage_harden"
    DISABLED = "disabled"
    UNAUTHORIZED = "unauthorized"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "taxonomy_closed" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("unexpected" in v for v in violations)


def test_authority_asymmetry_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.causality_consumer import (
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_asymmetry_pin_fires_on_decision_record_call():
    from backend.core.ouroboros.governance.causality_consumer import (
        register_shipped_invariants,
    )
    bad = '''
def f(decision_runtime):
    decision_runtime.record(payload)
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "asymmetry" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("read-only" in v for v in violations)


def test_composes_canonical_dag_pin_fires_on_direct_construction():
    from backend.core.ouroboros.governance.causality_consumer import (
        register_shipped_invariants,
    )
    bad = '''
def f():
    return CausalityDAG()
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "composes_canonical" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_master_flag_pin_fires_on_alternative_flag_name():
    from backend.core.ouroboros.governance.causality_consumer import (
        register_shipped_invariants,
    )
    bad = '''
import os
def is_consumer_enabled():
    return os.environ.get("OTHER_FLAG", "") == "1"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "master_flag_default_false" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance import causality_consumer
    expected = {
        "CAUSAL_FEATURES_SCHEMA_VERSION",
        "CausalDecisionAdvice",
        "DEFAULT_CAUSAL_LINEAGE_PROMPT_BUDGET",
        "OpCausalFeatures",
        "causal_lineage_prompt_budget",
        "compose_causal_lineage_section",
        "compute_op_causal_features",
        "deep_lineage_threshold_knob",
        "is_advisory_blocking",
        "is_consumer_enabled",
        "max_ancestor_depth_knob",
        "recurrence_warning_threshold_knob",
        "recurrence_window_knob",
        "register_shipped_invariants",
        "sibling_dedup_threshold_knob",
    }
    assert set(causality_consumer.__all__) == expected


# ---------------------------------------------------------------------------
# Env knobs — all read defensive defaults
# ---------------------------------------------------------------------------


def test_max_depth_knob_default_16(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_MAX_ANCESTOR_DEPTH", raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        max_ancestor_depth_knob,
    )
    assert max_ancestor_depth_knob() == 16


def test_max_depth_knob_handles_garbage(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_MAX_ANCESTOR_DEPTH", "not_int",
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        max_ancestor_depth_knob,
    )
    assert max_ancestor_depth_knob() == 16  # fallback


def test_recurrence_threshold_clamps_to_1(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_RECURRENCE_WARNING_THRESHOLD", "5.0",
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        recurrence_warning_threshold_knob,
    )
    assert recurrence_warning_threshold_knob() == 1.0


def test_recurrence_threshold_clamps_negative(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CAUSAL_RECURRENCE_WARNING_THRESHOLD", "-0.5",
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        recurrence_warning_threshold_knob,
    )
    # Negative → fall back to 0.5 default
    assert recurrence_warning_threshold_knob() == 0.5


def test_sibling_threshold_default_3(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_SIBLING_DEDUP_THRESHOLD", raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        sibling_dedup_threshold_knob,
    )
    assert sibling_dedup_threshold_knob() == 3


def test_deep_lineage_threshold_default_12(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CAUSAL_DEEP_LINEAGE_THRESHOLD", raising=False,
    )
    from backend.core.ouroboros.governance.causality_consumer import (
        deep_lineage_threshold_knob,
    )
    assert deep_lineage_threshold_knob() == 12
