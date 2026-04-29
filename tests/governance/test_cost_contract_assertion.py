"""Cost Contract Structural Reinforcement (§26.6) — regression spine.

Pins all three layers of the post-Phase-12 cost contract reinforcement:

  Layer 1 — AST invariant (`shipped_code_invariants.py` seeds:
            `cost_contract_bg_spec_no_unguarded_cascade` +
            `providers_cost_contract_assertion_wired`)
  Layer 2 — Runtime structural assertion (`cost_contract_assertion.py`:
            `CostContractViolation` + `assert_provider_route_compatible`)
  Layer 3 — Property Oracle claim (`property_oracle.py` evaluator
            `cost_contract_bg_op_did_not_use_claude` +
            `default_claims.py` spec)

Contract definition (post-Phase-12, refined from PRD §26.6 simplification):
  * SPEC route: NO Claude cascade, ever.
  * BG route: Claude cascade ONLY when is_read_only=True (Manifesto §5).
  * STANDARD/COMPLEX/IMMEDIATE: no contract restriction.

§-numbered coverage map:

Layer 2 (cost_contract_assertion.py):
  §1   Master flag asymmetric env semantics
  §2   CostContractViolation exception class
  §3   classify_route_compatibility — pure classification
  §4   assert_provider_route_compatible — gate behavior
  §5   Defensive normalization (corrupted inputs fail closed)
  §6   Master-off short-circuits to no-op
  §7   Authority invariants (no forbidden imports)

Layer 1 (shipped_code_invariants.py):
  §8   AST validator — SPEC has zero tolerance
  §9   AST validator — BG must reference is_read_only
  §10  AST validator — BG _call_fallback must be in if-block
  §11  AST validator — providers.py wiring presence pin
  §12  Validators NEVER raise on malformed source
  §13  Validators pass against current main

Layer 3 (property_oracle.py + default_claims.py):
  §14  Evaluator — non-Claude → PASSED
  §15  Evaluator — non-BG/SPEC route → PASSED
  §16  Evaluator — BG + read_only + claude → PASSED (reflex)
  §17  Evaluator — SPEC + read_only + claude → FAILED
  §18  Evaluator — BG + not read_only + claude → FAILED
  §19  Evaluator — providers_used trace with violation → FAILED
  §20  Evaluator — INSUFFICIENT_EVIDENCE on missing keys
  §21  default_claims spec registered with correct shape
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import cost_contract_assertion
from backend.core.ouroboros.governance.cost_contract_assertion import (
    BG_ROUTE,
    CLAUDE_TIER,
    COST_GATED_ROUTES,
    CostContractViolation,
    SPEC_ROUTE,
    assert_provider_route_compatible,
    classify_route_compatibility,
    cost_contract_runtime_assert_enabled,
)


# ===========================================================================
# §1 — Master flag asymmetric env semantics
# ===========================================================================


def test_master_flag_default_true(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED", raising=False,
    )
    assert cost_contract_runtime_assert_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_master_flag_empty_default_true(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED", val,
    )
    assert cost_contract_runtime_assert_enabled() is True


@pytest.mark.parametrize(
    "val", ["0", "false", "no", "off", "False", "NO", "garbage"],
)
def test_master_flag_falsy_disables(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED", val,
    )
    assert cost_contract_runtime_assert_enabled() is False


# ===========================================================================
# §2 — CostContractViolation exception class
# ===========================================================================


def test_cost_contract_violation_inherits_exception_directly() -> None:
    """Inheriting Exception directly (not RuntimeError) prevents
    `except Exception` catches in defensive code from accidentally
    swallowing this fatal exception."""
    assert issubclass(CostContractViolation, Exception)
    # Must NOT inherit RuntimeError (so RuntimeError catches don't swallow)
    assert not issubclass(CostContractViolation, RuntimeError)


def test_cost_contract_violation_carries_diagnostic_fields() -> None:
    exc = CostContractViolation(
        op_id="op-test",
        provider_route="background",
        provider_tier="claude",
        is_read_only=False,
        provider_name="claude-api",
        detail="dispatch boundary",
    )
    assert exc.op_id == "op-test"
    assert exc.provider_route == "background"
    assert exc.provider_tier == "claude"
    assert exc.is_read_only is False
    assert exc.provider_name == "claude-api"
    assert exc.detail == "dispatch boundary"
    # Message includes diagnostic context
    assert "op-test" in str(exc)
    assert "background" in str(exc)
    assert "claude" in str(exc)


# ===========================================================================
# §3 — classify_route_compatibility (pure classification)
# ===========================================================================


def test_classify_non_claude_returns_non_claude() -> None:
    assert classify_route_compatibility(
        provider_route="background", provider_tier="doubleword",
        is_read_only=False,
    ) == "non_claude"
    assert classify_route_compatibility(
        provider_route="speculative", provider_tier="prime",
        is_read_only=False,
    ) == "non_claude"


def test_classify_standard_claude_returns_ok() -> None:
    assert classify_route_compatibility(
        provider_route="standard", provider_tier="claude",
        is_read_only=False,
    ) == "ok"
    assert classify_route_compatibility(
        provider_route="complex", provider_tier="claude",
        is_read_only=True,
    ) == "ok"
    assert classify_route_compatibility(
        provider_route="immediate", provider_tier="claude",
        is_read_only=False,
    ) == "ok"


def test_classify_bg_readonly_claude_returns_reflex_allowed() -> None:
    """Manifesto §5 Nervous System Reflex — read-only BG ops MAY cascade."""
    assert classify_route_compatibility(
        provider_route="background", provider_tier="claude",
        is_read_only=True,
    ) == "reflex_allowed"


def test_classify_bg_readwrite_claude_returns_violation() -> None:
    assert classify_route_compatibility(
        provider_route="background", provider_tier="claude",
        is_read_only=False,
    ) == "violation"


def test_classify_spec_readonly_claude_returns_violation() -> None:
    """SPEC has zero tolerance — no Nervous System Reflex exception
    for SPEC route, only for BG."""
    assert classify_route_compatibility(
        provider_route="speculative", provider_tier="claude",
        is_read_only=True,
    ) == "violation"


def test_classify_spec_readwrite_claude_returns_violation() -> None:
    assert classify_route_compatibility(
        provider_route="speculative", provider_tier="claude",
        is_read_only=False,
    ) == "violation"


def test_classify_case_insensitive() -> None:
    assert classify_route_compatibility(
        provider_route="BACKGROUND", provider_tier="CLAUDE",
        is_read_only=True,
    ) == "reflex_allowed"
    assert classify_route_compatibility(
        provider_route="Background", provider_tier="Claude",
        is_read_only=False,
    ) == "violation"


# ===========================================================================
# §4 — assert_provider_route_compatible — gate behavior
# ===========================================================================


def test_assert_passes_on_standard_claude() -> None:
    assert_provider_route_compatible(
        op_id="op-1", provider_route="standard",
        provider_tier="claude", is_read_only=False,
    )


def test_assert_passes_on_bg_readonly_claude() -> None:
    """Nervous System Reflex — read-only BG cascade is allowed."""
    assert_provider_route_compatible(
        op_id="op-2", provider_route="background",
        provider_tier="claude", is_read_only=True,
    )


def test_assert_passes_on_non_claude_dispatch() -> None:
    """DW / Prime providers are never gated."""
    assert_provider_route_compatible(
        op_id="op-3", provider_route="background",
        provider_tier="doubleword", is_read_only=False,
    )
    assert_provider_route_compatible(
        op_id="op-4", provider_route="speculative",
        provider_tier="doubleword", is_read_only=False,
    )


def test_assert_raises_on_bg_readwrite_claude() -> None:
    with pytest.raises(CostContractViolation) as excinfo:
        assert_provider_route_compatible(
            op_id="op-5", provider_route="background",
            provider_tier="claude", is_read_only=False,
        )
    assert excinfo.value.op_id == "op-5"
    assert excinfo.value.provider_route == "background"
    assert excinfo.value.is_read_only is False


def test_assert_raises_on_spec_regardless_of_readonly() -> None:
    """SPEC has no read-only exception — Manifesto §5 only covers BG."""
    with pytest.raises(CostContractViolation):
        assert_provider_route_compatible(
            op_id="op-6", provider_route="speculative",
            provider_tier="claude", is_read_only=True,
        )
    with pytest.raises(CostContractViolation):
        assert_provider_route_compatible(
            op_id="op-7", provider_route="speculative",
            provider_tier="claude", is_read_only=False,
        )


# ===========================================================================
# §5 — Defensive normalization (corrupted inputs fail closed)
# ===========================================================================


def test_normalize_corrupted_is_read_only_fails_closed() -> None:
    """Unrecognized is_read_only value (not bool/str/numeric) → False
    → contract violation. Corrupted metadata MUST NOT accidentally
    pass through as read-only."""
    with pytest.raises(CostContractViolation):
        assert_provider_route_compatible(
            op_id="op-corrupt",
            provider_route="background",
            provider_tier="claude",
            is_read_only=object(),  # weird value
        )


def test_normalize_string_is_read_only_truthy_strings() -> None:
    """String "true"/"1"/"yes" coerce to True (Nervous System Reflex)."""
    for val in ("true", "1", "yes", "on", "True", "YES"):
        assert_provider_route_compatible(
            op_id=f"op-{val}",
            provider_route="background",
            provider_tier="claude",
            is_read_only=val,
        )  # must NOT raise


def test_normalize_string_is_read_only_falsy_strings() -> None:
    """String "false"/"0"/"no" coerce to False → violation."""
    for val in ("false", "0", "no", "", "garbage"):
        with pytest.raises(CostContractViolation):
            assert_provider_route_compatible(
                op_id=f"op-{val}",
                provider_route="background",
                provider_tier="claude",
                is_read_only=val,
            )


def test_normalize_none_inputs_handled_defensively() -> None:
    """None route/tier → empty string normalization → no violation
    (treated as non-Claude / non-cost-gated)."""
    assert_provider_route_compatible(
        op_id="op-none",
        provider_route=None,
        provider_tier=None,
        is_read_only=False,
    )


# ===========================================================================
# §6 — Master-off short-circuits to no-op
# ===========================================================================


def test_master_off_no_raise_on_violation(monkeypatch) -> None:
    """When the master flag is off, the gate becomes a no-op."""
    monkeypatch.setenv(
        "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED", "false",
    )
    # This would raise with the flag on; with off it must not raise
    assert_provider_route_compatible(
        op_id="op-off",
        provider_route="background",
        provider_tier="claude",
        is_read_only=False,
    )


def test_master_off_classify_still_works(monkeypatch) -> None:
    """classify_route_compatibility is NOT master-flag-gated — it's
    a pure helper for Layer 3, must work regardless."""
    monkeypatch.setenv(
        "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED", "false",
    )
    assert classify_route_compatibility(
        provider_route="background",
        provider_tier="claude",
        is_read_only=False,
    ) == "violation"


# ===========================================================================
# §7 — Authority invariants
# ===========================================================================


_FORBIDDEN_IMPORTS = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.phase_runners",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.semantic_guardian",
)


def test_authority_no_forbidden_imports() -> None:
    src = Path(inspect.getfile(cost_contract_assertion)).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for forbidden in _FORBIDDEN_IMPORTS:
                    assert forbidden not in alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for forbidden in _FORBIDDEN_IMPORTS:
                assert forbidden not in node.module


def test_authority_pure_stdlib_only() -> None:
    src = Path(inspect.getfile(cost_contract_assertion)).read_text()
    tree = ast.parse(src)
    allowed_roots = {"logging", "os", "typing", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in allowed_roots, (
                    f"non-stdlib import: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root in allowed_roots, (
                f"non-stdlib import: {node.module}"
            )


def test_module_constants_correct() -> None:
    assert BG_ROUTE == "background"
    assert SPEC_ROUTE == "speculative"
    assert CLAUDE_TIER == "claude"
    assert BG_ROUTE in COST_GATED_ROUTES
    assert SPEC_ROUTE in COST_GATED_ROUTES
    assert "standard" not in COST_GATED_ROUTES
    assert "complex" not in COST_GATED_ROUTES
    assert "immediate" not in COST_GATED_ROUTES


# ===========================================================================
# §8-§13 — Layer 1 AST invariant
# ===========================================================================


def test_layer1_invariants_registered() -> None:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        list_shipped_code_invariants,
    )
    invs = list_shipped_code_invariants()
    names = {inv.invariant_name for inv in invs}
    assert "cost_contract_bg_spec_no_unguarded_cascade" in names
    assert "providers_cost_contract_assertion_wired" in names


def test_layer1_invariants_pass_against_current_main() -> None:
    """The structural pin must pass against the current state of
    main — both invariants must hold or this test fails the build."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        validate_all,
    )
    violations = validate_all()
    cost_violations = [
        v for v in violations
        if v.invariant_name in (
            "cost_contract_bg_spec_no_unguarded_cascade",
            "providers_cost_contract_assertion_wired",
        )
    ]
    assert cost_violations == [], (
        f"Cost contract invariants violated: "
        f"{[v.detail for v in cost_violations]}"
    )


def test_layer1_validator_detects_spec_cascade() -> None:
    """Synthetic test — feed a fake source that has _call_fallback
    inside _generate_speculative; the validator must flag it."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        _validate_cost_contract_bg_spec,
    )
    bad_source = '''
class X:
    async def _generate_speculative(self, ctx, deadline):
        return await self._call_fallback(ctx, deadline)  # CONTRACT VIOLATION

    async def _generate_background(self, ctx, deadline):
        is_read_only = True
        if is_read_only:
            return await self._call_fallback(ctx, deadline)
'''
    tree = ast.parse(bad_source)
    violations = _validate_cost_contract_bg_spec(tree, bad_source)
    assert any("_generate_speculative" in v for v in violations)


def test_layer1_validator_detects_bg_unconditional_cascade() -> None:
    """Synthetic test — feed a fake source that has unconditional
    _call_fallback in _generate_background; validator must flag."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        _validate_cost_contract_bg_spec,
    )
    bad_source = '''
class X:
    async def _generate_background(self, ctx, deadline):
        is_read_only = False
        # Unconditional cascade — no if-block
        return await self._call_fallback(ctx, deadline)
'''
    tree = ast.parse(bad_source)
    violations = _validate_cost_contract_bg_spec(tree, bad_source)
    assert any("not contained" in v.lower() or "unconditional" in v.lower()
               for v in violations)


def test_layer1_validator_detects_bg_missing_readonly_wiring() -> None:
    """Synthetic test — _generate_background that doesn't reference
    is_read_only at all; validator must flag the wiring gap."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        _validate_cost_contract_bg_spec,
    )
    bad_source = '''
class X:
    async def _generate_background(self, ctx, deadline):
        # No is_read_only reference — wiring gap
        if some_other_condition:
            return await self._call_fallback(ctx, deadline)
'''
    tree = ast.parse(bad_source)
    violations = _validate_cost_contract_bg_spec(tree, bad_source)
    assert any(
        "is_read_only" in v and "not reference" in v.lower()
        for v in violations
    )


def test_layer1_validator_passes_correct_pattern() -> None:
    """Synthetic test — a properly-gated cascade must pass."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        _validate_cost_contract_bg_spec,
    )
    good_source = '''
class X:
    async def _generate_background(self, ctx, deadline):
        _is_read_only = bool(getattr(ctx, "is_read_only", False))
        if _is_read_only:
            return await self._call_fallback(ctx, deadline)
        # Otherwise, raise instead of cascade
        raise RuntimeError("background_dw_unavailable")

    async def _generate_speculative(self, ctx, deadline):
        # SPEC: no _call_fallback at all
        raise RuntimeError("speculative_deferred")
'''
    tree = ast.parse(good_source)
    violations = _validate_cost_contract_bg_spec(tree, good_source)
    assert violations == ()


def test_layer1_validator_never_raises_on_malformed_source() -> None:
    """Defensive — empty source, syntax errors, etc. must not raise."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        _validate_cost_contract_bg_spec,
    )
    # Empty
    _validate_cost_contract_bg_spec(ast.parse(""), "")
    # Just a comment
    _validate_cost_contract_bg_spec(ast.parse("# nothing"), "# nothing")


def test_layer1_providers_wiring_invariant_present_in_main() -> None:
    """The providers.py Layer 2 wiring presence pin must pass."""
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
        validate_all,
    )
    violations = validate_all()
    wiring_violations = [
        v for v in violations
        if v.invariant_name == "providers_cost_contract_assertion_wired"
    ]
    assert wiring_violations == [], (
        f"providers.py Layer 2 wiring missing: "
        f"{[v.detail for v in wiring_violations]}"
    )


# ===========================================================================
# §14-§20 — Layer 3 Property Oracle evaluator
# ===========================================================================


@pytest.fixture
def oracle_evaluator():
    """Return the registered cost_contract evaluator for direct testing."""
    from backend.core.ouroboros.governance.verification.property_oracle import (
        is_kind_registered, _EVALUATORS,
    )
    assert is_kind_registered("cost_contract_bg_op_did_not_use_claude")
    evaluator_obj = _EVALUATORS["cost_contract_bg_op_did_not_use_claude"]
    return evaluator_obj.evaluate


def _make_property():
    from backend.core.ouroboros.governance.verification.property_oracle import (
        Property,
    )
    return Property.make(
        kind="cost_contract_bg_op_did_not_use_claude",
        name="cost.bg_op_used_claude_must_be_false",
        evidence_required=("provider_route", "is_read_only", "providers_used"),
    )


def test_layer3_evaluator_non_claude_passes(oracle_evaluator) -> None:
    from backend.core.ouroboros.governance.verification.property_oracle import (
        VerdictKind,
    )
    verdict = oracle_evaluator(
        _make_property(),
        {
            "provider_route": "background",
            "is_read_only": False,
            "provider_tier": "doubleword",
        },
    )
    assert verdict.verdict == VerdictKind.PASSED


def test_layer3_evaluator_standard_claude_passes(oracle_evaluator) -> None:
    from backend.core.ouroboros.governance.verification.property_oracle import (
        VerdictKind,
    )
    verdict = oracle_evaluator(
        _make_property(),
        {
            "provider_route": "standard",
            "is_read_only": False,
            "provider_tier": "claude",
        },
    )
    assert verdict.verdict == VerdictKind.PASSED


def test_layer3_evaluator_bg_readonly_claude_passes(oracle_evaluator) -> None:
    """Nervous System Reflex — must pass."""
    from backend.core.ouroboros.governance.verification.property_oracle import (
        VerdictKind,
    )
    verdict = oracle_evaluator(
        _make_property(),
        {
            "provider_route": "background",
            "is_read_only": True,
            "provider_tier": "claude",
        },
    )
    assert verdict.verdict == VerdictKind.PASSED


def test_layer3_evaluator_spec_readonly_claude_fails(oracle_evaluator) -> None:
    """SPEC: no exception, even for read-only."""
    from backend.core.ouroboros.governance.verification.property_oracle import (
        VerdictKind,
    )
    verdict = oracle_evaluator(
        _make_property(),
        {
            "provider_route": "speculative",
            "is_read_only": True,
            "provider_tier": "claude",
        },
    )
    assert verdict.verdict == VerdictKind.FAILED


def test_layer3_evaluator_bg_readwrite_claude_fails(oracle_evaluator) -> None:
    from backend.core.ouroboros.governance.verification.property_oracle import (
        VerdictKind,
    )
    verdict = oracle_evaluator(
        _make_property(),
        {
            "provider_route": "background",
            "is_read_only": False,
            "provider_tier": "claude",
        },
    )
    assert verdict.verdict == VerdictKind.FAILED


def test_layer3_evaluator_providers_used_trace_with_violation(
    oracle_evaluator,
) -> None:
    """providers_used is a phase trace — if ANY entry is claude on a
    BG/non-readonly op, the whole claim FAILS."""
    from backend.core.ouroboros.governance.verification.property_oracle import (
        VerdictKind,
    )
    verdict = oracle_evaluator(
        _make_property(),
        {
            "provider_route": "background",
            "is_read_only": False,
            "providers_used": ["doubleword", "claude"],  # cascade happened
        },
    )
    assert verdict.verdict == VerdictKind.FAILED


def test_layer3_evaluator_providers_used_clean_trace_passes(
    oracle_evaluator,
) -> None:
    from backend.core.ouroboros.governance.verification.property_oracle import (
        VerdictKind,
    )
    verdict = oracle_evaluator(
        _make_property(),
        {
            "provider_route": "background",
            "is_read_only": False,
            "providers_used": ["doubleword", "doubleword"],  # no Claude
        },
    )
    assert verdict.verdict == VerdictKind.PASSED


def test_layer3_evaluator_insufficient_evidence_on_missing_keys(
    oracle_evaluator,
) -> None:
    from backend.core.ouroboros.governance.verification.property_oracle import (
        VerdictKind,
    )
    verdict = oracle_evaluator(
        _make_property(),
        {
            "provider_route": "background",
            "is_read_only": False,
            # neither provider_tier nor providers_used
        },
    )
    assert verdict.verdict == VerdictKind.INSUFFICIENT_EVIDENCE


# ===========================================================================
# §21 — default_claims spec registered with correct shape
# ===========================================================================


def test_layer3_default_claim_spec_registered() -> None:
    from backend.core.ouroboros.governance.verification.default_claims import (
        list_default_claim_specs,
    )
    specs_by_kind = {s.claim_kind: s for s in list_default_claim_specs()}
    assert "cost_contract_bg_op_did_not_use_claude" in specs_by_kind
    spec = specs_by_kind["cost_contract_bg_op_did_not_use_claude"]
    assert spec.severity == "must_hold"
    assert "provider_route" in spec.evidence_required
    assert "providers_used" in spec.evidence_required
    assert "is_read_only" in spec.evidence_required
    # Applies to all ops (no file pattern filter)
    assert spec.file_pattern_filter is None


def test_layer3_default_claim_spec_applies_to_all_ops() -> None:
    """Spec attaches unconditionally — the evaluator handles the
    route shape, the spec doesn't filter at PLAN time."""
    from backend.core.ouroboros.governance.verification.default_claims import (
        list_default_claim_specs,
    )
    specs_by_kind = {s.claim_kind: s for s in list_default_claim_specs()}
    spec = specs_by_kind["cost_contract_bg_op_did_not_use_claude"]
    # No filter → applies to op with no target files
    assert spec.applies_to_op(target_files=()) is True
    assert spec.applies_to_op(target_files=("foo.yaml",)) is True
    assert spec.applies_to_op(target_files=("foo.py",)) is True


def test_layer3_synthesize_includes_cost_contract_claim() -> None:
    from backend.core.ouroboros.governance.verification.default_claims import (
        synthesize_default_claims,
    )
    claims = synthesize_default_claims(
        op_id="op-test",
        target_files=("README.md",),
        posture="EXPLORE",
    )
    kinds = {c.property.kind for c in claims}
    assert "cost_contract_bg_op_did_not_use_claude" in kinds
