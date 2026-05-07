"""Move 6.5 Slice 1 — Multi-prior planning materializer.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Different prompts → same AST signature is the gold signal;
   do not fake 'priors' with cosmetic prompt noise. Reuse
   generative_quorum.compute_consensus contracts. Style hints
   from config/table, not string literals scattered in call
   sites. Wire route + posture pre-checks as pure functions so
   tests do not need the full generator."

Pinned coverage (~32 tests):
  * Closed taxonomy (PriorKind = 2 values) bytes-pinned
  * Master flag default-FALSE per §33.1
  * k_default boundary cases (default + clamp floor + clamp
    ceiling + invalid env strings)
  * Pure-function gates (route + posture + composed)
  * materialize_priors gating: master / route / posture / blank
  * materialize_priors composition: K=4 default produces 1
    SEED_ONLY anchor + 3 STYLE_HINT priors
  * materialize_priors wraparound: K=8 produces deterministic
    repeat-block ordering
  * materialize_priors determinism (same inputs → same seeds)
    + variation (different op_id → different seeds)
  * Prior + PriorSet round-trip via to_dict/from_dict
  * Schema mismatch on from_dict → None (NEVER raises)
  * StyleHintEntry frozen + STYLE_HINT_TABLE entries auditable
  * get_style_hint_by_id hit + miss
  * 4 AST pins clean (parametrized) + each fires on synthetic
    regression
  * Authority asymmetry pin catches forbidden imports
  * No-consensus-math pin catches forbidden FunctionDef
  * Public API surface complete + register_flags seeds + swallows
    registry errors
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/verification/"
        "multi_prior_planning.py"
    )


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_prior_kind_taxonomy_2_values():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        PriorKind,
    )
    assert {k.name for k in PriorKind} == {
        "SEED_ONLY", "STYLE_HINT",
    }


def test_prior_kind_str_values_canonical():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        PriorKind,
    )
    assert PriorKind.SEED_ONLY.value == "seed_only"
    assert PriorKind.STYLE_HINT.value == "style_hint"


# ---------------------------------------------------------------------------
# Master flag + k_default
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy_values(monkeypatch):
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", v,
        )
        assert master_enabled() is True


def test_master_falsey_values(monkeypatch):
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        master_enabled,
    )
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(
            "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", v,
        )
        assert master_enabled() is False


def test_k_default_value(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_K_DEFAULT", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        k_default,
    )
    assert k_default() == 4


def test_k_default_clamps_floor(monkeypatch):
    monkeypatch.setenv("JARVIS_MULTI_PRIOR_K_DEFAULT", "1")
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        k_default,
    )
    assert k_default() == 2  # K_FLOOR


def test_k_default_clamps_ceiling(monkeypatch):
    monkeypatch.setenv("JARVIS_MULTI_PRIOR_K_DEFAULT", "100")
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        k_default,
    )
    assert k_default() == 8  # K_CEILING


def test_k_default_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_K_DEFAULT", "not-an-int",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        k_default,
    )
    assert k_default() == 4


# ---------------------------------------------------------------------------
# Pure-function gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route, expected", [
        ("complex", True),
        ("COMPLEX", True),
        ("Complex", True),
        ("standard", False),
        ("immediate", False),
        ("background", False),
        ("speculative", False),
        ("", False),
    ],
)
def test_route_gate(route, expected):
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        should_fire_for_route,
    )
    assert should_fire_for_route(route) is expected


@pytest.mark.parametrize(
    "posture, expected", [
        ("EXPLORE", True),
        ("explore", True),
        ("Explore", True),
        ("CONSOLIDATE", False),
        ("HARDEN", False),
        ("MAINTAIN", False),
        ("", False),
    ],
)
def test_posture_gate(posture, expected):
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        should_fire_for_posture,
    )
    assert should_fire_for_posture(posture) is expected


def test_composed_gate_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        should_fire_for_op,
    )
    assert should_fire_for_op(
        op_id="op-1", route="complex", posture="EXPLORE",
    ) is False


def test_composed_gate_full_fire(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        should_fire_for_op,
    )
    assert should_fire_for_op(
        op_id="op-1", route="complex", posture="EXPLORE",
    ) is True


def test_composed_gate_blank_op_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        should_fire_for_op,
    )
    assert should_fire_for_op(
        op_id="", route="complex", posture="EXPLORE",
    ) is False


# ---------------------------------------------------------------------------
# materialize_priors — gates
# ---------------------------------------------------------------------------


def test_materialize_returns_none_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    assert materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
    ) is None


def test_materialize_returns_none_route_fail(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    assert materialize_priors(
        op_id="op-1", route="standard", posture="EXPLORE",
    ) is None


def test_materialize_returns_none_posture_fail(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    assert materialize_priors(
        op_id="op-1", route="complex", posture="HARDEN",
    ) is None


# ---------------------------------------------------------------------------
# materialize_priors — composition + determinism
# ---------------------------------------------------------------------------


def test_materialize_k4_default_composition(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    monkeypatch.delenv(
        "JARVIS_MULTI_PRIOR_K_DEFAULT", raising=False,
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
        PriorKind,
    )
    ps = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
    )
    assert ps is not None
    assert ps.k == 4
    # 1 SEED_ONLY anchor + 3 STYLE_HINT
    kinds = [p.kind for p in ps.priors]
    assert kinds[0] is PriorKind.SEED_ONLY
    for k in kinds[1:]:
        assert k is PriorKind.STYLE_HINT


def test_materialize_k4_seed_only_has_empty_addendum(
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    ps = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
        k=4,
    )
    assert ps is not None
    assert ps.priors[0].system_prompt_addendum == ""
    for p in ps.priors[1:]:
        assert len(p.system_prompt_addendum) > 0


def test_materialize_k_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_MULTI_PRIOR_K_DEFAULT", "4")
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    ps = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
        k=2,
    )
    assert ps is not None
    assert ps.k == 2


def test_materialize_k_clamps(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    ps_low = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
        k=1,
    )
    assert ps_low is not None and ps_low.k == 2
    ps_hi = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
        k=99,
    )
    assert ps_hi is not None and ps_hi.k == 8


def test_materialize_k8_wraparound_block_pattern(
    monkeypatch,
):
    """K=8 → 1 block of (SEED_ONLY + 4 style_hints) = 5
    priors, then second block starts SEED_ONLY again. So
    indices 0 and 5 should be SEED_ONLY."""
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors, PriorKind,
    )
    ps = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
        k=8,
    )
    assert ps is not None
    assert ps.priors[0].kind is PriorKind.SEED_ONLY
    assert ps.priors[5].kind is PriorKind.SEED_ONLY


def test_materialize_determinism_same_inputs(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    ps1 = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
    )
    ps2 = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
    )
    assert ps1 is not None and ps2 is not None
    assert tuple(p.seed for p in ps1.priors) == tuple(
        p.seed for p in ps2.priors
    )
    assert tuple(p.prior_id for p in ps1.priors) == tuple(
        p.prior_id for p in ps2.priors
    )


def test_materialize_seeds_vary_by_op_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    ps_a = materialize_priors(
        op_id="op-A", route="complex", posture="EXPLORE",
    )
    ps_b = materialize_priors(
        op_id="op-B", route="complex", posture="EXPLORE",
    )
    assert ps_a is not None and ps_b is not None
    seeds_a = tuple(p.seed for p in ps_a.priors)
    seeds_b = tuple(p.seed for p in ps_b.priors)
    assert seeds_a != seeds_b


def test_materialize_seeds_in_int32_range(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    ps = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
    )
    assert ps is not None
    for p in ps.priors:
        assert 0 <= p.seed <= 0x7FFFFFFF


# ---------------------------------------------------------------------------
# Style-hint table
# ---------------------------------------------------------------------------


def test_style_hint_table_nonempty():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        STYLE_HINT_TABLE,
    )
    assert len(STYLE_HINT_TABLE) >= 4


def test_style_hint_entries_unique_ids():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        STYLE_HINT_TABLE,
    )
    ids = [e.hint_id for e in STYLE_HINT_TABLE]
    assert len(ids) == len(set(ids))


def test_get_style_hint_by_id_hit():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        STYLE_HINT_TABLE, get_style_hint_by_id,
    )
    first = STYLE_HINT_TABLE[0]
    assert get_style_hint_by_id(first.hint_id) is first


def test_get_style_hint_by_id_miss():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        get_style_hint_by_id,
    )
    assert get_style_hint_by_id(
        "no-such-hint",
    ) is None


# ---------------------------------------------------------------------------
# Frozen artifacts: round-trip + schema discipline
# ---------------------------------------------------------------------------


def test_prior_round_trip(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        Prior, materialize_priors,
    )
    ps = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
    )
    assert ps is not None
    p = ps.priors[0]
    rt = Prior.from_dict(p.to_dict())
    assert rt is not None
    assert rt.prior_id == p.prior_id
    assert rt.kind is p.kind
    assert rt.seed == p.seed


def test_prior_from_dict_schema_mismatch_returns_none():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        Prior,
    )
    assert Prior.from_dict({"schema_version": "wrong"}) is None


def test_prior_from_dict_malformed_returns_none():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        MULTI_PRIOR_PLANNING_SCHEMA_VERSION, Prior,
    )
    bad = {
        "schema_version": MULTI_PRIOR_PLANNING_SCHEMA_VERSION,
        "kind": "no-such-kind",
        "prior_id": "x", "seed": 0,
    }
    assert Prior.from_dict(bad) is None


def test_priorset_round_trip(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        PriorSet, materialize_priors,
    )
    ps = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
    )
    assert ps is not None
    rt = PriorSet.from_dict(ps.to_dict())
    assert rt is not None
    assert rt.k == ps.k
    assert tuple(p.prior_id for p in rt.priors) == tuple(
        p.prior_id for p in ps.priors
    )


def test_priorset_kind_distribution(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        materialize_priors,
    )
    ps = materialize_priors(
        op_id="op-1", route="complex", posture="EXPLORE",
        k=4,
    )
    assert ps is not None
    dist = ps.kind_distribution
    assert dist["seed_only"] == 1
    assert dist["style_hint"] == 3


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "multi_prior_planning_taxonomy_2_values",
        "multi_prior_planning_master_default_false",
        "multi_prior_planning_authority_asymmetry",
        "multi_prior_planning_no_consensus_math",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, src)
    assert violations == ()


def test_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class PriorKind:
    SEED_ONLY = "seed_only"
    STYLE_HINT = "style_hint"
    PLAN_VARIANT = "plan_variant"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_planning_taxonomy_2_values"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations  # Slice 1 closed at 2 values


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_planning_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_candidate_generator():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from backend.core.ouroboros.governance."
        "candidate_generator import x"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_planning_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_no_consensus_math_pin_fires_on_local_def():
    """Forbidden symbol assembled at runtime to avoid
    surfacing the literal in source (defense against future
    substring sweeps)."""
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        register_shipped_invariants,
    )
    forbidden = "compute" + "_consensus"
    bad = f"""
def {forbidden}(rolls):
    return None
"""
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_planning_no_consensus_math"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_no_consensus_math_pin_fires_on_top_level_import():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        register_shipped_invariants,
    )
    forbidden = "compute" + "_consensus"
    bad = (
        "from backend.core.ouroboros.governance.verification."
        f"generative_quorum import {forbidden}"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "multi_prior_planning_no_consensus_math"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_planning as mod,
    )
    expected = {
        "MULTI_PRIOR_PLANNING_SCHEMA_VERSION",
        "Prior", "PriorKind", "PriorSet",
        "STYLE_HINT_TABLE", "STYLE_HINT_TABLE_VERSION",
        "StyleHintEntry", "get_style_hint_by_id",
        "k_default", "master_enabled", "materialize_priors",
        "register_flags", "register_shipped_invariants",
        "should_fire_for_op", "should_fire_for_posture",
        "should_fire_for_route",
    }
    assert set(mod.__all__) == expected


def test_register_flags_seeds_two_knobs():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    register_flags(registry)
    assert registry.register.call_count == 2
    names = {
        c.kwargs["name"]
        for c in registry.register.call_args_list
    }
    assert names == {
        "JARVIS_MULTI_PRIOR_PLANNING_ENABLED",
        "JARVIS_MULTI_PRIOR_K_DEFAULT",
    }


def test_register_flags_swallows_registry_errors():
    from backend.core.ouroboros.governance.verification.multi_prior_planning import (  # noqa: E501
        register_flags,
    )
    registry = MagicMock()
    registry.register.side_effect = RuntimeError("boom")
    # MUST NOT raise
    register_flags(registry)


# ---------------------------------------------------------------------------
# Composition discipline — Slice 1 must NOT import Move 6's
# consensus authority (Slice 2 will lazy-import inside the
# runner instead). This is a regression-pin via runtime check
# on the imported module's __dict__.
# ---------------------------------------------------------------------------


def test_slice1_does_not_import_consensus_authority():
    """Confirms that importing the Slice 1 module does NOT
    pull in Move 6's consensus function at module-scope. Slice
    2's runner will lazy-import; Slice 1 stays pure."""
    from backend.core.ouroboros.governance.verification import (  # noqa: E501
        multi_prior_planning as mod,
    )
    forbidden = "compute" + "_consensus"
    assert forbidden not in mod.__dict__
