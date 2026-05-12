"""FlagRegistry seed regression spine for Treefinement Phase 0.

Pins the load-bearing structural invariants for the seven
``JARVIS_L2_*`` Treefinement flags:

* ``register_flags`` is auto-discovered by
  ``flag_registry_seed._discover_module_provided_flags`` via the
  ``backend.core.ouroboros.governance`` provider package walk —
  zero edits to ``flag_registry_seed.SEED_SPECS``.
* The seven FlagSpecs have the canonical shapes the registry
  contract demands (correct FlagType, default values, Category
  slots, source_file pointer to ``repair_tree.py``).
* The master flag default is FALSE (§33.1 graduation contract —
  drift here would silently graduate tree mode without an
  evidence ladder).
* CROSS_BRANCH_LEARNING flag default is TRUE — without it tree
  mode degrades to race-the-loop (the AlphaVerus delta is the
  cross-branch signal).
"""
from __future__ import annotations

from typing import Iterator

import pytest

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagType,
    ensure_seeded,
    reset_default_registry,
)
from backend.core.ouroboros.governance.repair_tree import (
    BEAM_WIDTH_ENV_VAR,
    BRANCH_DEDUP_ENV_VAR,
    CROSS_BRANCH_LEARNING_ENV_VAR,
    EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR,
    MASTER_FLAG_ENV_VAR,
    MAX_BRANCHES_PER_LAYER_ENV_VAR,
    STRATEGY_ENV_VAR,
    register_flags,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Each test starts with a fresh default registry. Reset on
    exit so the next test's auto-discovery sees a clean slate."""
    reset_default_registry()
    yield
    reset_default_registry()


# ---------------------------------------------------------------------------
# Direct registration — substrate-owned register_flags
# ---------------------------------------------------------------------------


def test_register_flags_installs_all_specs():
    """The substrate's ``register_flags`` MUST install exactly
    15 Treefinement flags (7 Phase 0 + 5 Phase 2 + 3 Phase 3).
    Drift here (e.g. silently dropping one) is operator-visible
    via this count assertion."""
    registry = FlagRegistry()
    count = register_flags(registry)
    assert count == 15, (
        f"expected 15 specs (7 P0 + 5 P2 + 3 P3), got {count}"
    )


def test_master_flag_spec_shape():
    """The master flag MUST be BOOL / SAFETY / default-FALSE.
    Drift here is the §33.1 graduation-contract violation."""
    registry = FlagRegistry()
    register_flags(registry)
    spec = registry.get_spec(MASTER_FLAG_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.BOOL
    assert spec.default is False, (
        f"{MASTER_FLAG_ENV_VAR} MUST default FALSE per §33.1 "
        "graduation contract — drift would silently graduate tree "
        "mode without an evidence ladder"
    )
    assert spec.category == Category.SAFETY
    assert "repair_tree" in spec.source_file


def test_strategy_flag_shape():
    registry = FlagRegistry()
    register_flags(registry)
    spec = registry.get_spec(STRATEGY_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.STR
    assert spec.default == "linear", (
        "Strategy default MUST be 'linear' — preserves byte-"
        "identical legacy FSM behavior"
    )
    assert spec.category == Category.ROUTING


def test_max_branches_flag_shape():
    registry = FlagRegistry()
    register_flags(registry)
    spec = registry.get_spec(MAX_BRANCHES_PER_LAYER_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.INT
    assert spec.default == 3, (
        "K=3 is the operator-approved Phase 0 default "
        "(chat 2026-05-11)"
    )
    assert spec.category == Category.CAPACITY


def test_beam_width_flag_shape():
    registry = FlagRegistry()
    register_flags(registry)
    spec = registry.get_spec(BEAM_WIDTH_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.INT
    assert spec.default == 2
    assert spec.category == Category.CAPACITY


def test_branch_dedup_flag_shape():
    registry = FlagRegistry()
    register_flags(registry)
    spec = registry.get_spec(BRANCH_DEDUP_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.BOOL
    assert spec.default is True, (
        "Dedup default MUST be True — the canonical _patch_sig "
        "composition is the single signature source"
    )
    assert spec.category == Category.TUNING


def test_cross_branch_learning_flag_shape():
    """Default TRUE — this is the AlphaVerus delta. Without it,
    tree mode degrades to race-the-loop and the entire arc loses
    its published-research grounding."""
    registry = FlagRegistry()
    register_flags(registry)
    spec = registry.get_spec(CROSS_BRANCH_LEARNING_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.BOOL
    assert spec.default is True, (
        f"{CROSS_BRANCH_LEARNING_ENV_VAR} MUST default TRUE — "
        "this is the AlphaVerus delta (sibling outcomes inform "
        "next-layer GENERATE). Default FALSE would silently "
        "regress tree mode to naive parallel repair."
    )
    assert spec.category == Category.TUNING


def test_emergency_demote_threshold_flag_shape():
    registry = FlagRegistry()
    register_flags(registry)
    spec = registry.get_spec(EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.FLOAT
    assert spec.default == 0.85
    assert spec.category == Category.TUNING


def test_register_flags_idempotent():
    """Re-registering on the same registry MUST be a no-op
    (override-in-place); count stays 7."""
    registry = FlagRegistry()
    register_flags(registry)
    register_flags(registry)
    register_flags(registry)
    specs = registry.list_all()
    treefinement_specs = [
        s for s in specs
        if s.name in (
            MASTER_FLAG_ENV_VAR,
            STRATEGY_ENV_VAR,
            MAX_BRANCHES_PER_LAYER_ENV_VAR,
            BEAM_WIDTH_ENV_VAR,
            BRANCH_DEDUP_ENV_VAR,
            CROSS_BRANCH_LEARNING_ENV_VAR,
            EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR,
        )
    ]
    assert len(treefinement_specs) == 7, (
        f"Idempotent re-registration broken — got "
        f"{len(treefinement_specs)} matching specs"
    )


def test_register_flags_never_raises_on_malformed_registry():
    """The substrate contract is fail-open — boot-time fail in
    one substrate MUST NOT block other substrates from registering.
    Mirrors permission_decision_archive Phase 0 discipline."""

    class _BrokenRegistry:
        def register(self, _spec):
            raise RuntimeError("registry exploded")

    count = register_flags(_BrokenRegistry())
    assert count == 0, (
        "fail-open contract: when every register() raises, count "
        "is 0 but the function MUST NOT propagate"
    )


def test_register_flags_partial_failure_returns_partial_count():
    """When the registry rejects SOME specs (not all), count
    reflects successful installs — operators can see the
    partial-degradation in /help flags."""

    class _SelectiveRegistry:
        def __init__(self):
            self.calls = 0
            self.installed = []

        def register(self, spec):
            self.calls += 1
            # Reject every other spec
            if self.calls % 2 == 0:
                raise RuntimeError("rejected")
            self.installed.append(spec)

    reg = _SelectiveRegistry()
    count = register_flags(reg)
    assert 0 < count < 15, (
        f"partial-failure path broken — got count={count}"
    )
    assert count == len(reg.installed)


# ---------------------------------------------------------------------------
# Auto-discovery via canonical seed walker (§33.3 naming-cage)
# ---------------------------------------------------------------------------


def test_auto_discovery_picks_up_all_seven_specs():
    """``ensure_seeded()`` walks ``_FLAG_PROVIDER_PACKAGES`` for
    ``register_flags`` callables. The Treefinement substrate MUST
    be discovered zero-edit (no additions to
    ``flag_registry_seed.SEED_SPECS``)."""
    registry = ensure_seeded()
    expected = [
        MASTER_FLAG_ENV_VAR,
        STRATEGY_ENV_VAR,
        MAX_BRANCHES_PER_LAYER_ENV_VAR,
        BEAM_WIDTH_ENV_VAR,
        BRANCH_DEDUP_ENV_VAR,
        CROSS_BRANCH_LEARNING_ENV_VAR,
        EMERGENCY_DEMOTE_THRESHOLD_ENV_VAR,
    ]
    for env_var in expected:
        spec = registry.get_spec(env_var)
        assert spec is not None, (
            f"{env_var} MUST be auto-discovered via the canonical "
            "seed walker — drift here means the §33.3 naming-cage "
            "discipline for flags regressed"
        )
        assert "repair_tree" in spec.source_file, (
            f"{env_var} source_file MUST point to repair_tree.py"
        )
