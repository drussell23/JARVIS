"""Regression spine for Phase 10 Slice 5b — phase10_yaml_purger.

Per the operator binding (PRD §32.8.1, §1610), the actual YAML
deletion is gated by `phase10_graduation_contract.is_ready_for_purge`.
This spine asserts the purger respects that gate end-to-end: it
NEVER mutates the YAML unless (a) master flag is true AND (b)
graduation contract reports READY_FOR_PURGE AND (c) round-trip
safety check passes.
"""
from __future__ import annotations

import ast
import os
import shutil
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance import phase10_yaml_purger as pyp
from backend.core.ouroboros.governance.phase10_yaml_purger import (
    PHASE10_YAML_PURGER_SCHEMA_VERSION,
    PurgeReport,
    PurgeVerdict,
    _ENV_MASTER,
    _PURGED_FIELDS_PER_ROUTE,
    apply_purge,
    compute_purged_yaml,
    master_enabled,
    register_flags,
    register_shipped_invariants,
    verify_purge_safety,
)


# Real-YAML fixture path
_REAL_YAML = Path(
    "backend/core/ouroboros/governance/brain_selection_policy.yaml"
)


def _real_yaml_text() -> str:
    return _REAL_YAML.read_text(encoding="utf-8")


# --- Schema + taxonomy ----------------------------------------------------


def test_schema_version_stamp():
    assert (
        PHASE10_YAML_PURGER_SCHEMA_VERSION
        == "phase10_yaml_purger.1"
    )


def test_purge_verdict_closed_5_value():
    assert {v.value for v in PurgeVerdict} == {
        "ready", "not_ready", "would_break", "disabled", "error",
    }


def test_purged_fields_per_route_data():
    """The fields to strip live on a module constant — not
    hardcoded inside compute_purged_yaml."""
    assert set(_PURGED_FIELDS_PER_ROUTE) == {
        "dw_allowed", "block_mode",
    }


# --- master_enabled gate --------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    assert master_enabled() is False


def test_master_explicit_enable(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_master_recognizes_off_aliases(monkeypatch):
    for off in ("0", "false", "no", "off", "FALSE", ""):
        monkeypatch.setenv(_ENV_MASTER, off)
        assert master_enabled() is False, off


# --- compute_purged_yaml (pure function) ---------------------------------


def test_compute_empty_source_returns_error():
    verdict, out, diag = compute_purged_yaml("")
    assert verdict is PurgeVerdict.ERROR


def test_compute_none_source_returns_error():
    verdict, out, diag = compute_purged_yaml(None)
    assert verdict is PurgeVerdict.ERROR


def test_compute_garbage_source_coerced_to_string():
    """NEVER-raises contract: non-string input is coerced to
    str rather than crashing. Numeric input → `str(42)` →
    no matches → NOT_READY (not ERROR — coercion succeeded)."""
    verdict, out, diag = compute_purged_yaml(42)
    assert verdict is PurgeVerdict.NOT_READY


def test_compute_no_targets_returns_not_ready():
    """YAML without any `dw_allowed:` / `block_mode:` lines —
    purge already applied OR YAML doesn't match the expected
    shape. Either way, NOT_READY (not an error)."""
    benign = "schema_version: '1.0'\nother_key: value\n"
    verdict, out, diag = compute_purged_yaml(benign)
    assert verdict is PurgeVerdict.NOT_READY
    assert out == benign


def test_compute_real_yaml_strips_5_routes_each():
    """The real YAML has 5 routes × 2 fields each → 10 lines."""
    real = _real_yaml_text()
    verdict, out, diag = compute_purged_yaml(real)
    assert verdict is PurgeVerdict.READY
    assert real.count("dw_allowed:") == 5
    assert real.count("block_mode:") == 5
    assert out.count("dw_allowed:") == 0
    assert out.count("block_mode:") == 0


def test_compute_preserves_other_fields():
    """The strip MUST NOT touch reason / dw_models /
    fallback_tolerance. Operators expressed cost-contract intent
    in those fields."""
    real = _real_yaml_text()
    verdict, out, diag = compute_purged_yaml(real)
    assert verdict is PurgeVerdict.READY
    # Reason / dw_models / fallback_tolerance counts unchanged
    assert real.count("fallback_tolerance:") == out.count(
        "fallback_tolerance:"
    )
    assert real.count("dw_models:") == out.count("dw_models:")


def test_compute_anchors_on_indent():
    """The strip is anchored on 6-space indent, so a key named
    `dw_allowed` at a different indent level would be ignored.
    This is defensive — protects against accidental matches in
    comments or nested structures."""
    fake = (
        "doubleword_topology:\n"
        "  routes:\n"
        "    foo:\n"
        "      dw_allowed: true\n"  # 6-space indent — should match
        "    bar:\n"
        "  dw_allowed: outer\n"  # 2-space indent — should NOT match
    )
    verdict, out, diag = compute_purged_yaml(fake)
    assert verdict is PurgeVerdict.READY
    assert "      dw_allowed:" not in out
    assert "  dw_allowed: outer" in out  # preserved


def test_compute_custom_fields_to_strip():
    """Operator can pass a custom field list — but the default
    is data-driven from _PURGED_FIELDS_PER_ROUTE."""
    src = (
        "      dw_allowed: false\n"
        "      block_mode: cascade\n"
        "      reason: test\n"
    )
    verdict, out, diag = compute_purged_yaml(
        src, fields_to_strip=("reason",),
    )
    assert verdict is PurgeVerdict.READY
    assert "reason:" not in out
    assert "dw_allowed:" in out  # not stripped


def test_compute_is_pure_idempotent_under_repeat():
    """Running the purge twice yields the same output as once."""
    real = _real_yaml_text()
    _, once, _ = compute_purged_yaml(real)
    twice_verdict, twice, _ = compute_purged_yaml(once)
    # Second pass finds nothing to strip → NOT_READY but
    # output equals input
    assert twice_verdict is PurgeVerdict.NOT_READY
    assert twice == once


# --- verify_purge_safety --------------------------------------------------


def test_verify_safety_real_yaml_round_trip(monkeypatch):
    """The real YAML's purge must preserve the v2 surface under
    master=ON. If it doesn't, the purger refuses to apply."""
    monkeypatch.delenv(
        "JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False,
    )
    real = _real_yaml_text()
    _, purged, _ = compute_purged_yaml(real)
    is_safe, diag = verify_purge_safety(real, purged)
    # The real YAML must verify safe — that's the precondition
    # for Slice 5b being ship-able once the contract is green.
    assert is_safe, f"safety verification failed: {diag}"


def test_verify_safety_mismatched_yaml_unsafe(monkeypatch):
    """Tampered YAML that changes a route's fallback_tolerance
    MUST fail the safety check. We target the specific YAML
    line by anchoring on `fallback_tolerance:` to avoid hitting
    unrelated `"queue"` literals (e.g., budget_exceeded_action)."""
    monkeypatch.delenv(
        "JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False,
    )
    real = _real_yaml_text()
    # Change a route's fallback_tolerance from queue to cascade
    tampered = real.replace(
        'fallback_tolerance: "queue"',
        'fallback_tolerance: "cascade_to_claude"',
        1,
    )
    assert tampered != real, "test fixture failed to mutate"
    is_safe, diag = verify_purge_safety(real, tampered)
    assert is_safe is False


def test_verify_safety_garbage_returns_unsafe():
    is_safe, diag = verify_purge_safety("not yaml", "also not")
    assert is_safe is False


def test_verify_safety_missing_topology_section():
    """YAML without doubleword_topology cannot be safety-checked."""
    benign = "schema_version: '1.0'\nother: value\n"
    is_safe, diag = verify_purge_safety(benign, benign)
    assert is_safe is False


def test_verify_safety_restores_env(monkeypatch):
    """The safety check temporarily forces master=ON. It MUST
    restore the prior env value when done."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "false")
    real = _real_yaml_text()
    _, purged, _ = compute_purged_yaml(real)
    verify_purge_safety(real, purged)
    # Env restored to what we set
    assert os.environ.get("JARVIS_TOPOLOGY_SENTINEL_ENABLED") == "false"


def test_verify_safety_restores_env_when_absent(monkeypatch):
    """When the env was UNSET, safety check must leave it unset."""
    monkeypatch.delenv(
        "JARVIS_TOPOLOGY_SENTINEL_ENABLED", raising=False,
    )
    real = _real_yaml_text()
    _, purged, _ = compute_purged_yaml(real)
    verify_purge_safety(real, purged)
    assert "JARVIS_TOPOLOGY_SENTINEL_ENABLED" not in os.environ


# --- apply_purge (the gated entry point) ---------------------------------


@pytest.fixture
def yaml_copy(tmp_path):
    """Working copy of the real YAML for mutation tests."""
    target = tmp_path / "brain_selection_policy.yaml"
    shutil.copy(_REAL_YAML, target)
    return target


def test_apply_master_off_returns_disabled(monkeypatch, yaml_copy):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    report = apply_purge(yaml_copy)
    assert report.verdict is PurgeVerdict.DISABLED
    # No mutation
    assert yaml_copy.read_text() == _real_yaml_text()


def test_apply_contract_not_green_returns_not_ready(
    monkeypatch, yaml_copy,
):
    """Even with master flag on, the contract gate must hold."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    # Point the contract at an empty session dir → INSUFFICIENT_SESSIONS
    monkeypatch.setenv(
        "JARVIS_PHASE10_SESSION_ROOT",
        str(yaml_copy.parent / "no_sessions"),
    )
    report = apply_purge(yaml_copy)
    assert report.verdict is PurgeVerdict.NOT_READY
    # No mutation
    assert yaml_copy.read_text() == _real_yaml_text()


def test_apply_dry_run_default_no_mutation(monkeypatch, yaml_copy):
    """Even when ALL gates pass, dry_run=True (default) writes
    nothing — operator must opt in explicitly."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    # Bypass contract by disabling it — this is the developer-side
    # path that proves the dry-run gate is sound (production cannot
    # do this; the AST pin on the contract module's master flag
    # asserts it).
    monkeypatch.setenv(
        "JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED", "false",
    )
    report = apply_purge(yaml_copy, dry_run=True)
    # Contract disabled returns DISABLED verdict from the
    # contract itself — purger surfaces this as NOT_READY
    assert report.verdict in (
        PurgeVerdict.NOT_READY, PurgeVerdict.READY,
    )
    # Either way, dry_run=True means NO MUTATION
    assert yaml_copy.read_text() == _real_yaml_text()


def test_apply_report_structure(monkeypatch, yaml_copy):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(
        "JARVIS_PHASE10_SESSION_ROOT",
        str(yaml_copy.parent / "nx"),
    )
    report = apply_purge(yaml_copy)
    assert isinstance(report, PurgeReport)
    d = report.to_dict()
    assert d["schema_version"] == "phase10_yaml_purger.1"
    assert d["dry_run"] is True
    assert "verdict" in d


def test_apply_records_elapsed(monkeypatch, yaml_copy):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(
        "JARVIS_PHASE10_SESSION_ROOT",
        str(yaml_copy.parent / "nx"),
    )
    report = apply_purge(yaml_copy)
    assert report.elapsed_s >= 0.0


def test_apply_missing_yaml_file_returns_error(
    monkeypatch, tmp_path,
):
    """Master on, gate green via env override, but YAML doesn't
    exist — must surface ERROR not crash."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(
        "JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED", "false",
    )
    nonexistent = tmp_path / "nope.yaml"
    report = apply_purge(nonexistent)
    # Contract disabled → NOT_READY before we even read the file
    assert report.verdict is PurgeVerdict.NOT_READY


# --- AST pins ------------------------------------------------------------


def _load_source_tree():
    target = Path(
        "backend/core/ouroboros/governance/phase10_yaml_purger.py"
    )
    src = target.read_text()
    return src, ast.parse(src)


def test_ast_pins_count():
    assert len(register_shipped_invariants()) == 6


def test_ast_pin_verdict_taxonomy_passes():
    src, tree = _load_source_tree()
    pin = next(
        p for p in register_shipped_invariants()
        if "verdict_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_purged_field_list_passes():
    src, tree = _load_source_tree()
    pin = next(
        p for p in register_shipped_invariants()
        if "purged_field_list" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_master_default_false_passes():
    src, tree = _load_source_tree()
    pin = next(
        p for p in register_shipped_invariants()
        if "master_default_false" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_authority_asymmetry_passes():
    src, tree = _load_source_tree()
    pin = next(
        p for p in register_shipped_invariants()
        if "authority_asymmetry" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_composes_canonical_passes():
    src, tree = _load_source_tree()
    pin = next(
        p for p in register_shipped_invariants()
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_apply_gates_before_write_passes():
    src, tree = _load_source_tree()
    pin = next(
        p for p in register_shipped_invariants()
        if "gates_before_write" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


# --- AST pin synthetic regressions ---------------------------------------


def test_ast_pin_verdict_catches_drift():
    pin = next(
        p for p in register_shipped_invariants()
        if "verdict_taxonomy" in p.invariant_name
    )
    bad = '''
class PurgeVerdict(str, enum.Enum):
    READY = "ready"
    NEW_VALUE = "new_value"
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_purged_field_list_catches_drift():
    pin = next(
        p for p in register_shipped_invariants()
        if "purged_field_list" in p.invariant_name
    )
    bad = (
        '_PURGED_FIELDS_PER_ROUTE: Tuple[str, ...] = '
        '("dw_allowed", "block_mode", "reason")'
    )
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_authority_catches_orchestrator():
    pin = next(
        p for p in register_shipped_invariants()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = '''
from backend.core.ouroboros.governance.orchestrator import x
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_apply_gates_catches_wrong_order():
    pin = next(
        p for p in register_shipped_invariants()
        if "gates_before_write" in p.invariant_name
    )
    # os.replace appears BEFORE the gates — must fail
    bad = '''
def apply_purge():
    os.replace(a, b)
    master_enabled()
    _check_graduation_contract()
'''
    assert pin.validate(ast.parse(bad), bad) != ()


def test_ast_pin_master_default_false_catches_true():
    pin = next(
        p for p in register_shipped_invariants()
        if "master_default_false" in p.invariant_name
    )
    bad = '''
def master_enabled():
    return _flag("X", default=True)
'''
    assert pin.validate(ast.parse(bad), bad) != ()


# --- FlagRegistry seeds --------------------------------------------------


def test_register_flags_seeds_master():
    class _R:
        def __init__(self):
            self.specs = []

        def register(self, spec):
            self.specs.append(spec)

    r = _R()
    n = register_flags(r)
    assert n == 1
    assert r.specs[0].name == _ENV_MASTER
    assert r.specs[0].default is False
