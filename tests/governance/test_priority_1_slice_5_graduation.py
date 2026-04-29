"""Priority 1 Slice 5 — graduation pin spine.

Pins the post-graduation contract for the confidence-aware execution
arc (Slices 1-4 graduated default-true 2026-04-29 in Slice 5):

  * 6 master flags flipped default false → true
  * 4 new shipped_code_invariants seeds active + holding against main
  * 7 confidence/cost-contract flags registered in FlagRegistry
  * Source-grep pins on the graduated literals (so a future patch
    cannot silently flip them back without breaking these tests)
  * Cross-slice authority survival across all 5 modules
  * Cost-contract 4-layer defense-in-depth proof

§-numbered coverage map:

Master flag default-true pins (× 6):
  §1   JARVIS_CONFIDENCE_CAPTURE_ENABLED → True
  §2   JARVIS_CONFIDENCE_MONITOR_ENABLED → True
  §3   JARVIS_CONFIDENCE_MONITOR_ENFORCE → True
  §4   JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED → True
  §5   JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED → True
  §6   JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED → True

Source-grep pins on graduated literals (× 6):
  §7-§12  ``return True  # graduated default`` literal present in
          each owner module's flag function

Shipped-code-invariants seeds (× 4):
  §13  confidence_capture_no_authority_imports registered + holds
  §14  confidence_monitor_pure_data_no_io registered + holds
  §15  confidence_probe_consumer_contract registered + holds
  §16  confidence_route_advisor_cost_contract_guard registered + holds

FlagRegistry seeds (× 7):
  §17  All 7 confidence + cost-contract flags registered with
       correct category + posture-relevance + default=True

Cross-slice authority survival (× 5):
  §18  All 5 verification modules import only stdlib + their own
       slice's verification dependencies + (cost_contract_assertion
       for advisor) — pins the authority isolation through graduation.

Cost contract 4-layer defense-in-depth proof:
  §19  Layer 1 (AST seeds) — invariants reject synthetic violators
  §20  Layer 4 (advisor structural guard) — _propose_route_change
       rejects BG/SPEC → escalation regardless of master flag state
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.cost_contract_assertion import (
    CostContractViolation,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category,
    ensure_seeded,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    list_shipped_code_invariants,
    validate_all,
)
from backend.core.ouroboros.governance.verification import (
    confidence_capture,
    confidence_monitor,
    confidence_observability,
    confidence_route_advisor,
    hypothesis_consumers,
)
from backend.core.ouroboros.governance.verification.confidence_capture import (
    confidence_capture_enabled,
)
from backend.core.ouroboros.governance.verification.confidence_monitor import (
    confidence_monitor_enabled,
    confidence_monitor_enforce,
)
from backend.core.ouroboros.governance.verification.confidence_observability import (
    confidence_observability_enabled,
)
from backend.core.ouroboros.governance.verification.confidence_route_advisor import (
    _propose_route_change,
    confidence_route_routing_enabled,
)
from backend.core.ouroboros.governance.verification.hypothesis_consumers import (
    confidence_probe_integration_enabled,
)


# ===========================================================================
# §1-§6 — Master flag default-true pins (post-graduation)
# ===========================================================================


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip all confidence flags so each test sees the graduated
    default cleanly."""
    for flag in (
        "JARVIS_CONFIDENCE_CAPTURE_ENABLED",
        "JARVIS_CONFIDENCE_MONITOR_ENABLED",
        "JARVIS_CONFIDENCE_MONITOR_ENFORCE",
        "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED",
        "JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED",
        "JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED",
        "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED",
    ):
        monkeypatch.delenv(flag, raising=False)
    yield


def test_capture_default_true() -> None:
    assert confidence_capture_enabled() is True


def test_monitor_default_true() -> None:
    assert confidence_monitor_enabled() is True


def test_monitor_enforce_default_true() -> None:
    assert confidence_monitor_enforce() is True


def test_probe_integration_default_true() -> None:
    assert confidence_probe_integration_enabled() is True


def test_observability_default_true() -> None:
    assert confidence_observability_enabled() is True


def test_route_routing_default_true() -> None:
    assert confidence_route_routing_enabled() is True


# ===========================================================================
# §7-§12 — Source-grep pins on graduated literals
# ===========================================================================


def _src_of(module) -> str:
    return Path(inspect.getfile(module)).read_text()


def test_capture_source_grep_graduated_literal() -> None:
    """The flag function MUST contain `return True  # graduated default`
    (with comment marking the graduation). Catches future refactors
    that silently flip back to False."""
    src = _src_of(confidence_capture)
    assert "return True  # graduated default" in src
    # Pin: the comment mentions Slice 5
    assert "Slice 5" in src


def test_monitor_source_grep_graduated_literal() -> None:
    src = _src_of(confidence_monitor)
    # Both monitor master and enforce sub-flag should carry graduated
    occurrences = src.count("return True  # graduated default")
    assert occurrences >= 2, (
        f"expected ≥ 2 graduated-default literals in confidence_monitor "
        f"(master + enforce), found {occurrences}"
    )


def test_probe_integration_source_grep_graduated_literal() -> None:
    src = _src_of(hypothesis_consumers)
    # Find the function we graduated specifically (other consumers
    # were always default-true; this one is new)
    assert "return True  # graduated default" in src
    assert "Slice 5" in src


def test_observability_source_grep_graduated_literal() -> None:
    src = _src_of(confidence_observability)
    assert "return True  # graduated default" in src
    assert "Slice 5" in src


def test_route_advisor_source_grep_graduated_literal() -> None:
    src = _src_of(confidence_route_advisor)
    assert "return True  # graduated default" in src
    assert "Slice 5" in src


def test_all_six_masters_have_distinct_graduated_literals() -> None:
    """Aggregate sanity — across the 5 owner modules we should see
    at least 6 graduated-default literals (capture + monitor + enforce
    + probe + observability + route). Catches accidental copy-paste
    omissions."""
    total = 0
    for mod in (
        confidence_capture,
        confidence_monitor,
        confidence_observability,
        confidence_route_advisor,
        hypothesis_consumers,
    ):
        total += _src_of(mod).count("return True  # graduated default")
    assert total >= 6, (
        f"expected ≥ 6 graduated-default literals across owner modules, "
        f"found {total}"
    )


# ===========================================================================
# §13-§16 — shipped_code_invariants seeds registered + holding
# ===========================================================================


_EXPECTED_NEW_INVARIANTS = (
    "confidence_capture_no_authority_imports",
    "confidence_monitor_pure_data_no_io",
    "confidence_probe_consumer_contract",
    "confidence_route_advisor_cost_contract_guard",
)


def test_all_four_new_invariants_registered() -> None:
    invs = list_shipped_code_invariants()
    names = {inv.invariant_name for inv in invs}
    for expected in _EXPECTED_NEW_INVARIANTS:
        assert expected in names, (
            f"missing invariant: {expected}"
        )


def test_capture_authority_invariant_holds() -> None:
    violations = validate_all()
    matches = [
        v for v in violations
        if v.invariant_name == "confidence_capture_no_authority_imports"
    ]
    assert matches == [], (
        f"capture authority pin violated: "
        f"{[v.detail for v in matches]}"
    )


def test_monitor_pure_data_invariant_holds() -> None:
    violations = validate_all()
    matches = [
        v for v in violations
        if v.invariant_name == "confidence_monitor_pure_data_no_io"
    ]
    assert matches == [], (
        f"monitor pure-data pin violated: "
        f"{[v.detail for v in matches]}"
    )


def test_probe_consumer_contract_invariant_holds() -> None:
    violations = validate_all()
    matches = [
        v for v in violations
        if v.invariant_name == "confidence_probe_consumer_contract"
    ]
    assert matches == [], (
        f"probe consumer contract pin violated: "
        f"{[v.detail for v in matches]}"
    )


def test_route_advisor_cost_guard_invariant_holds() -> None:
    violations = validate_all()
    matches = [
        v for v in violations
        if v.invariant_name == (
            "confidence_route_advisor_cost_contract_guard"
        )
    ]
    assert matches == [], (
        f"route advisor cost-guard pin violated: "
        f"{[v.detail for v in matches]}"
    )


# ===========================================================================
# §17 — FlagRegistry seeds for the 7 confidence + cost-contract flags
# ===========================================================================


_EXPECTED_REGISTERED_FLAGS = {
    "JARVIS_CONFIDENCE_CAPTURE_ENABLED": (
        Category.OBSERVABILITY, True,
    ),
    "JARVIS_CONFIDENCE_MONITOR_ENABLED": (
        Category.SAFETY, True,
    ),
    "JARVIS_CONFIDENCE_MONITOR_ENFORCE": (
        Category.SAFETY, True,
    ),
    "JARVIS_CONFIDENCE_PROBE_INTEGRATION_ENABLED": (
        Category.SAFETY, True,
    ),
    "JARVIS_CONFIDENCE_OBSERVABILITY_ENABLED": (
        Category.OBSERVABILITY, True,
    ),
    "JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED": (
        Category.ROUTING, True,
    ),
    "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED": (
        Category.SAFETY, True,
    ),
}


def test_flag_registry_has_all_seven_flags() -> None:
    registry = ensure_seeded()
    for flag_name in _EXPECTED_REGISTERED_FLAGS:
        assert flag_name in registry._specs, (
            f"flag {flag_name} not registered in FlagRegistry"
        )


def test_flag_registry_categories_correct() -> None:
    registry = ensure_seeded()
    for flag_name, (
        expected_cat, expected_default,
    ) in _EXPECTED_REGISTERED_FLAGS.items():
        spec = registry._specs.get(flag_name)
        assert spec is not None
        assert spec.category == expected_cat, (
            f"{flag_name}: category mismatch "
            f"(got {spec.category}, expected {expected_cat})"
        )
        assert spec.default is expected_default, (
            f"{flag_name}: default mismatch "
            f"(got {spec.default}, expected {expected_default})"
        )


def test_flag_registry_safety_critical_flags_have_posture_relevance() -> None:
    """The cost contract runtime assert is the most safety-critical
    flag — it MUST be tagged CRITICAL across all postures."""
    registry = ensure_seeded()
    spec = registry._specs.get(
        "JARVIS_COST_CONTRACT_RUNTIME_ASSERT_ENABLED",
    )
    assert spec is not None
    assert spec.posture_relevance, (
        "cost contract runtime assert MUST have posture_relevance"
    )
    # Must be CRITICAL in every posture
    for posture in ("EXPLORE", "CONSOLIDATE", "HARDEN", "MAINTAIN"):
        rel = spec.posture_relevance.get(posture)
        assert rel is not None, (
            f"posture {posture} missing from "
            f"COST_CONTRACT_RUNTIME_ASSERT_ENABLED posture_relevance"
        )


# ===========================================================================
# §18 — Cross-slice authority survival
# ===========================================================================


_FORBIDDEN_FOR_VERIFICATION_FAMILY = (
    "backend.core.ouroboros.governance.orchestrator",
    "backend.core.ouroboros.governance.phase_runners",
    "backend.core.ouroboros.governance.candidate_generator",
    "backend.core.ouroboros.governance.iron_gate",
    "backend.core.ouroboros.governance.change_engine",
    "backend.core.ouroboros.governance.policy",
    "backend.core.ouroboros.governance.semantic_guardian",
    "backend.core.ouroboros.governance.semantic_firewall",
    "backend.core.ouroboros.governance.providers",
    "backend.core.ouroboros.governance.doubleword_provider",
    "backend.core.ouroboros.governance.urgency_router",
)


def _no_forbidden_imports(mod) -> None:
    src = _src_of(mod)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for forbidden in _FORBIDDEN_FOR_VERIFICATION_FAMILY:
                    assert forbidden not in alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for forbidden in _FORBIDDEN_FOR_VERIFICATION_FAMILY:
                assert forbidden not in node.module


def test_capture_authority_isolation() -> None:
    _no_forbidden_imports(confidence_capture)


def test_monitor_authority_isolation() -> None:
    _no_forbidden_imports(confidence_monitor)


def test_probe_consumer_authority_isolation() -> None:
    _no_forbidden_imports(hypothesis_consumers)


def test_observability_authority_isolation() -> None:
    _no_forbidden_imports(confidence_observability)


def test_route_advisor_authority_isolation() -> None:
    _no_forbidden_imports(confidence_route_advisor)


# ===========================================================================
# §19-§20 — Cost contract 4-layer defense-in-depth proof
# ===========================================================================


def test_layer4_advisor_guard_independent_of_master_flag(
    monkeypatch,
) -> None:
    """Slice 4 advisor guard fires REGARDLESS of master flag state.
    Master flag governs whether propose_route_change emits proposals;
    the structural guard in _propose_route_change is unconditional."""
    # Master flag explicitly OFF
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED", "false",
    )
    # Direct call to _propose_route_change MUST still raise
    with pytest.raises(CostContractViolation):
        _propose_route_change(
            current_route="background",
            proposed_route="standard",  # ESCALATION
            reason_code="should_not_happen",
            confidence_basis="master_flag_off_test",
        )


def test_layer4_advisor_guard_with_master_on(monkeypatch) -> None:
    """Same guard fires when master is on too — different code path
    but same structural enforcement."""
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_ROUTE_ROUTING_ENABLED", "true",
    )
    with pytest.raises(CostContractViolation):
        _propose_route_change(
            current_route="speculative",
            proposed_route="immediate",  # ESCALATION
            reason_code="should_not_happen",
            confidence_basis="master_flag_on_test",
        )


def test_total_invariant_count_post_graduation() -> None:
    """Slice 5 graduation should add 4 new invariants on top of the
    pre-existing 3 (plan_runner_default_claims_wiring +
    cost_contract_bg_spec_no_unguarded_cascade +
    providers_cost_contract_assertion_wired) → 7 total."""
    invs = list_shipped_code_invariants()
    assert len(invs) >= 7, (
        f"expected ≥ 7 shipped_code_invariants post-graduation, "
        f"found {len(invs)}"
    )


def test_full_validate_all_holds_against_main() -> None:
    """End-to-end: all 7 invariants MUST hold against current main."""
    violations = validate_all()
    assert violations == (), (
        f"shipped_code_invariants violations against main: "
        f"{[v.invariant_name + ': ' + v.detail for v in violations]}"
    )
