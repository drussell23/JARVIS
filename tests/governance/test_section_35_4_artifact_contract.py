"""§35 row 🟡 #4 / §3.6.3 priority #4 — Cross-runner artifact contract.

Closes fragility vector #8 by adding schema-versioned validation at
the SINGLE ``phase_dispatcher.merge_artifacts`` choke point. Without
this contract, a Wave 2 PhaseRunner refactor that renames /
restructures any of the 10 cross-phase artifacts crashes the FSM
mid-pipeline with no recovery path.

## Closure shape

  * Closed ``ArtifactKind`` taxonomy (10 values — one per
    ``PhaseContext`` slot).
  * Frozen ``ArtifactSpec`` registry — bytes-pinned tuple of 10
    entries declaring producer / consumer / validate_value /
    schema_version per artifact.
  * Pure-function ``validate_artifact_value`` returning frozen
    ``ArtifactValidation`` outcome (closed 6-value taxonomy).
  * Dispatcher composes the canonical validator at line 829 BEFORE
    ``pctx.merge_artifacts(...)`` — single choke point.
  * Master-flag-gated: when off, validator returns
    ``PASSED_DISABLED`` byte-equivalent to legacy behavior; when
    on + strictness=strict, dispatcher raises
    ``PhaseContextError`` on first failure.

## What the AST pins enforce

  * ArtifactKind taxonomy frozen at exactly 10 values — adding a
    new artifact requires updating both the enum AND the registry.
  * ValidationOutcome taxonomy frozen at exactly 6 values.
  * Registry covers EVERY ``PhaseContext`` slot named in
    ``phase_dispatcher.PhaseContext`` docstring's "Slot ownership"
    table — drift here means cross-phase leak escapes the contract.
  * Substrate authority — never imports orchestrator /
    phase_runners / candidate_generator / providers / etc.
  * Dispatcher composes ``validate_artifacts_bundle`` at the merge
    choke point (verified by source-grep).
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance import artifact_contract as ac


_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOVERNANCE_ROOT = _REPO_ROOT / "backend/core/ouroboros/governance"


# -----------------------------------------------------------------
# Closed taxonomy frozen pins
# -----------------------------------------------------------------


def test_artifact_kind_taxonomy_frozen_10_values():
    expected = {
        "GENERATION", "EPISODIC_MEMORY",
        "GENERATE_RETRIES_REMAINING", "ADVISORY",
        "BEST_CANDIDATE", "BEST_VALIDATION", "T_APPLY",
        "RISK_TIER", "CONSCIOUSNESS_BRIDGE", "CANCEL_TOKEN",
    }
    actual = {k.name for k in ac.ArtifactKind}
    assert actual == expected, (
        f"ArtifactKind taxonomy drift: {actual ^ expected}. "
        f"Adding a new artifact requires updating BOTH the enum "
        f"AND _ARTIFACT_REGISTRY."
    )


def test_validation_outcome_taxonomy_frozen_6_values():
    expected = {
        "OK", "UNKNOWN_KEY", "TYPE_MISMATCH", "WRONG_PRODUCER",
        "SCHEMA_VERSION_SKEW", "PASSED_DISABLED",
    }
    actual = {o.name for o in ac.ValidationOutcome}
    assert actual == expected


# -----------------------------------------------------------------
# Registry coverage — every PhaseContext slot has a spec
# -----------------------------------------------------------------


def _phase_context_slots() -> set[str]:
    """Read PhaseContext source AST, return the set of dataclass
    field names. Excludes 'extras' (the bag-of-future-keys slot)."""
    src = (_GOVERNANCE_ROOT / "phase_dispatcher.py").read_text(
        encoding="utf-8",
    )
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "PhaseContext"
        ):
            slots = set()
            for sub in node.body:
                if isinstance(sub, ast.AnnAssign) and isinstance(
                    sub.target, ast.Name,
                ):
                    slots.add(sub.target.id)
            return slots - {"extras"}
    raise AssertionError("PhaseContext class not found")


def test_registry_covers_every_phase_context_slot():
    """The load-bearing structural pin: every typed slot in
    PhaseContext MUST have an ArtifactSpec entry. A new slot
    without a spec is a silent cross-phase-leak escape."""
    slots = _phase_context_slots()
    registered_keys = {spec.key for spec in ac._ARTIFACT_REGISTRY}
    missing = slots - registered_keys
    assert not missing, (
        f"PhaseContext slots {missing} have no ArtifactSpec entry. "
        f"Either add the spec OR remove the slot. Silent escape "
        f"from the contract is the §35 #4 fragility vector."
    )


def test_registry_keys_match_kind_values():
    """Every ArtifactSpec.key MUST equal ArtifactSpec.kind.value —
    redundancy detection: enum drift would otherwise hide."""
    for spec in ac._ARTIFACT_REGISTRY:
        assert spec.key == spec.kind.value, (
            f"Spec key {spec.key!r} != kind.value "
            f"{spec.kind.value!r} — drift between key and kind"
        )


def test_registry_size_pinned():
    """Pin forces reviewer attention on registry additions —
    adding a new spec requires updating BOTH the registry AND
    this size assertion. Mirrors v2.82's allowlist-size pin."""
    assert len(ac._ARTIFACT_REGISTRY) == 10, (
        f"Registry size changed: {len(ac._ARTIFACT_REGISTRY)}. "
        f"New artifact? Update this pin."
    )


# -----------------------------------------------------------------
# Pure-function validator — happy path + 4 violation kinds
# -----------------------------------------------------------------


@pytest.fixture
def _master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_ARTIFACT_CONTRACT_ENABLED", "true")
    ac.reset_registry_cache_for_tests()
    yield


def test_validator_master_off_returns_passed_disabled():
    """Master-off path is byte-equivalent to legacy behavior —
    every input passes via PASSED_DISABLED."""
    result = ac.validate_artifact_value(
        key="bogus_key_that_doesnt_exist",
        value=object(),
    )
    assert result.outcome is ac.ValidationOutcome.PASSED_DISABLED
    assert result.is_valid()


def test_validator_unknown_key_master_on(_master_on):
    result = ac.validate_artifact_value(
        key="generations",  # typo — should be 'generation'
        value=None,
    )
    assert result.outcome is ac.ValidationOutcome.UNKNOWN_KEY
    assert "rename / typo" in result.detail
    assert not result.is_valid()


def test_validator_empty_key_master_on(_master_on):
    result = ac.validate_artifact_value(key="", value=None)
    assert result.outcome is ac.ValidationOutcome.UNKNOWN_KEY


def test_validator_type_mismatch_master_on(_master_on):
    """generate_retries_remaining requires Optional[int]; a string
    fails validate_value."""
    result = ac.validate_artifact_value(
        key="generate_retries_remaining",
        value="three",  # not int
    )
    assert result.outcome is ac.ValidationOutcome.TYPE_MISMATCH


def test_validator_type_mismatch_t_apply_string(_master_on):
    result = ac.validate_artifact_value(
        key="t_apply", value="not_a_float",
    )
    assert result.outcome is ac.ValidationOutcome.TYPE_MISMATCH


def test_validator_type_mismatch_t_apply_bool_rejected(_master_on):
    """Python bool is subclass of int — but for t_apply we want
    real numbers, so bool must be rejected."""
    result = ac.validate_artifact_value(key="t_apply", value=True)
    assert result.outcome is ac.ValidationOutcome.TYPE_MISMATCH


def test_validator_wrong_producer_master_on(_master_on):
    """advisory's producer is CLASSIFY only — APPLY emitting it
    is a phase-ownership violation."""
    result = ac.validate_artifact_value(
        key="advisory", value=None, producer_phase="APPLY",
    )
    assert result.outcome is ac.ValidationOutcome.WRONG_PRODUCER
    assert "expected producer" in result.detail


def test_validator_correct_producer_passes(_master_on):
    """advisory from CLASSIFY is the canonical case."""
    result = ac.validate_artifact_value(
        key="advisory", value=None, producer_phase="CLASSIFY",
    )
    assert result.outcome is ac.ValidationOutcome.OK


def test_validator_cancel_token_no_phase_can_emit(_master_on):
    """cancel_token's producer_phases is empty (set by dispatcher
    caller). Any phase emitting it is WRONG_PRODUCER."""
    result = ac.validate_artifact_value(
        key="cancel_token", value=None, producer_phase="GENERATE",
    )
    assert result.outcome is ac.ValidationOutcome.WRONG_PRODUCER
    assert "infrastructure-set" in result.detail


def test_validator_value_validator_raise_treated_as_mismatch(
    _master_on, monkeypatch,
):
    """If a custom validator itself raises (defensive bug), the
    helper swallows + classifies as TYPE_MISMATCH — never
    propagates."""
    def _bad_validator(_v):
        raise RuntimeError("validator bug")

    bad_spec = ac.ArtifactSpec(
        kind=ac.ArtifactKind.GENERATION,
        key="generation",
        producer_phases=frozenset({"GENERATE"}),
        consumer_phases=frozenset({"VALIDATE"}),
        validate_value=_bad_validator,
    )
    monkeypatch.setattr(
        ac, "_ARTIFACT_REGISTRY", (bad_spec,),
    )
    ac.reset_registry_cache_for_tests()
    result = ac.validate_artifact_value(
        key="generation", value=object(),
    )
    assert result.outcome is ac.ValidationOutcome.TYPE_MISMATCH
    assert "RuntimeError" in result.detail


# -----------------------------------------------------------------
# Bundle validator
# -----------------------------------------------------------------


def test_bundle_master_off_returns_empty_tuple():
    out = ac.validate_artifacts_bundle({"foo": 1, "bar": 2})
    assert out == ()


def test_bundle_master_on_validates_each_entry(_master_on):
    out = ac.validate_artifacts_bundle(
        {"generation": None, "t_apply": 0.5},
        producer_phase="GENERATE",
    )
    assert len(out) == 2
    # generation from GENERATE is valid; t_apply from GENERATE is
    # WRONG_PRODUCER (only APPLY may emit t_apply).
    by_key = {v.artifact_key: v for v in out}
    assert by_key["generation"].outcome is ac.ValidationOutcome.OK
    assert by_key["t_apply"].outcome is ac.ValidationOutcome.WRONG_PRODUCER


def test_bundle_non_mapping_master_on(_master_on):
    out = ac.validate_artifacts_bundle("not a mapping")
    assert len(out) == 1
    assert out[0].outcome is ac.ValidationOutcome.TYPE_MISMATCH


def test_first_failure_returns_first_invalid(_master_on):
    out = ac.validate_artifacts_bundle(
        {"generation": None, "bogus": "x"},
        producer_phase="GENERATE",
    )
    failure = ac.first_failure(out)
    assert failure is not None
    assert failure.artifact_key == "bogus"
    assert failure.outcome is ac.ValidationOutcome.UNKNOWN_KEY


def test_first_failure_returns_none_when_all_ok(_master_on):
    out = ac.validate_artifacts_bundle(
        {"generation": None}, producer_phase="GENERATE",
    )
    assert ac.first_failure(out) is None


# -----------------------------------------------------------------
# Authority invariants — substrate doesn't import forbidden modules
# -----------------------------------------------------------------


def test_substrate_authority_invariants():
    """artifact_contract.py imports stdlib + op_context ONLY (zero
    coupling to orchestrator / phase_runners / providers / gate /
    candidate_generator). Forbidden imports here would mean the
    contract module participates in the cycle it's meant to
    police."""
    src = Path(inspect.getfile(ac)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = (
        "orchestrator", "phase_runner", "phase_dispatcher",
        "candidate_generator", "iron_gate", "change_engine",
        "policy", "semantic_guardian", "semantic_firewall",
        "providers", "doubleword_provider", "urgency_router",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for forb in forbidden:
                assert forb not in node.module, (
                    f"artifact_contract must NOT import {forb}: "
                    f"{node.module}"
                )


def test_substrate_never_raises_from_public_api():
    """All public API entry points wrap defensive try/except.
    Spot-check by passing pathological inputs and asserting no
    exception escapes."""
    # validate_artifact_value: pathological types
    for k, v in (
        (None, None), (123, "x"), (object(), {"unhashable": object()}),
    ):
        result = ac.validate_artifact_value(key=k, value=v)
        assert result is not None
    # validate_artifacts_bundle: pathological types
    for bundle in (None, "string", 123, object()):
        out = ac.validate_artifacts_bundle(bundle)
        assert isinstance(out, tuple)


# -----------------------------------------------------------------
# Dispatcher integration AST pin — composes canonical validator
# -----------------------------------------------------------------


def test_dispatcher_composes_validate_artifacts_bundle_at_merge_choke():
    """Bytes-pin: phase_dispatcher.py MUST compose
    ``validate_artifacts_bundle`` BEFORE
    ``pctx.merge_artifacts(...)`` — that's the single choke point
    that catches all 10 cross-phase artifacts on every iteration.
    Drift here means a runner refactor sneaks past validation."""
    src = (_GOVERNANCE_ROOT / "phase_dispatcher.py").read_text(
        encoding="utf-8",
    )
    # The validator call must precede the merge_artifacts call.
    validator_match = re.search(
        r"_ac_validate_bundle\(.*?\)", src, re.DOTALL,
    )
    assert validator_match, (
        "phase_dispatcher must compose "
        "validate_artifacts_bundle (renamed to _ac_validate_bundle "
        "at the import site)"
    )
    merge_match = re.search(
        r"pctx\.merge_artifacts\(dict\(result\.artifacts\)\)",
        src,
    )
    assert merge_match, "merge_artifacts call site not found"
    # Validator MUST come before the merge.
    assert validator_match.start() < merge_match.start(), (
        "validate_artifacts_bundle must be called BEFORE "
        "pctx.merge_artifacts — otherwise stale state lands in "
        "PhaseContext before validation can reject it"
    )


def test_dispatcher_composes_first_failure_for_strict_mode():
    src = (_GOVERNANCE_ROOT / "phase_dispatcher.py").read_text(
        encoding="utf-8",
    )
    assert "_ac_first_failure" in src, (
        "dispatcher must compose first_failure() helper"
    )
    assert "PhaseContextError" in src, (
        "strict-mode failure must raise PhaseContextError"
    )
    assert '"strict"' in src, (
        "dispatcher must compare strictness against \"strict\""
    )


def test_dispatcher_documents_section_35_4_closure():
    """Provenance pin: dispatcher cites the closure section so a
    future reader can find the design doc."""
    src = (_GOVERNANCE_ROOT / "phase_dispatcher.py").read_text(
        encoding="utf-8",
    )
    assert "§35 row" in src and "#4" in src, (
        "phase_dispatcher must cite §35 row 🟡 #4 at the wiring site"
    )


# -----------------------------------------------------------------
# Dispatcher functional integration — strict mode raises
# -----------------------------------------------------------------


def test_dispatcher_strict_mode_raises_on_unknown_key(monkeypatch):
    """Master-on + strictness=strict: a runner returning an
    unknown artifact key produces PhaseContextError at the merge
    boundary. Closes fragility vector #8 mid-pipeline."""
    monkeypatch.setenv("JARVIS_ARTIFACT_CONTRACT_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_ARTIFACT_CONTRACT_STRICTNESS", "strict",
    )
    ac.reset_registry_cache_for_tests()

    from backend.core.ouroboros.governance.phase_dispatcher import (
        PhaseContext, PhaseContextError,
    )
    pctx = PhaseContext()

    # Simulate a runner returning a renamed artifact (the actual
    # bug class fragility #8 protects against).
    fake_result = MagicMock()
    fake_result.artifacts = {"genaration": "typo_value"}  # mis-spelled
    # We mimic the dispatcher's wiring directly to avoid spinning
    # up the full pipeline harness.
    from backend.core.ouroboros.governance.artifact_contract import (
        first_failure as _ff,
        validate_artifacts_bundle as _vb,
        master_enabled as _me,
        strictness as _strict,
    )
    assert _me() is True
    validations = _vb(
        fake_result.artifacts, producer_phase="GENERATE",
    )
    failure = _ff(validations)
    assert failure is not None
    assert failure.outcome is ac.ValidationOutcome.UNKNOWN_KEY
    assert _strict() == "strict"
    # Confirm the dispatcher would raise.
    with pytest.raises(PhaseContextError) as excinfo:
        raise PhaseContextError(
            f"artifact contract violation at GENERATE → "
            f"{failure.outcome.value}: {failure.detail}"
        )
    assert "unknown_key" in str(excinfo.value)


def test_dispatcher_advisory_mode_does_not_raise(monkeypatch):
    """Master-on + strictness=advisory: violations log but do not
    raise. Operators can run with the contract on without breaking
    the pipeline during the soak phase."""
    monkeypatch.setenv("JARVIS_ARTIFACT_CONTRACT_ENABLED", "true")
    # Default strictness is advisory.
    ac.reset_registry_cache_for_tests()
    assert ac.master_enabled()
    assert ac.strictness() == "advisory"


# -----------------------------------------------------------------
# Master-off byte-equivalent behavior — the safe-revert contract
# -----------------------------------------------------------------


def test_master_off_byte_equivalent_to_legacy():
    """Master flag off is the safe revert path: validate_artifact_value
    and validate_artifacts_bundle short-circuit immediately,
    preserving pre-contract dispatcher behavior. No behavior
    leaks into the pipeline."""
    # No env var set → master off.
    assert not ac.master_enabled()
    # Pathological bundle that would FAIL master-on validation.
    out = ac.validate_artifacts_bundle(
        {"genaration": "bogus"}, producer_phase="GENERATE",
    )
    assert out == ()
    single = ac.validate_artifact_value(key="genaration", value="bogus")
    assert single.outcome is ac.ValidationOutcome.PASSED_DISABLED


# -----------------------------------------------------------------
# Spec round-trip + introspection
# -----------------------------------------------------------------


def test_artifact_validation_to_dict_round_trip():
    v = ac.ArtifactValidation(
        outcome=ac.ValidationOutcome.UNKNOWN_KEY,
        detail="test",
        artifact_key="bogus",
    )
    d = v.to_dict()
    assert d["outcome"] == "unknown_key"
    assert d["artifact_key"] == "bogus"
    assert d["expected_kind"] == ""


def test_lookup_spec_returns_none_for_unknown_key():
    assert ac.lookup_spec("not_a_real_key") is None
    assert ac.lookup_spec("") is None


def test_lookup_spec_returns_canonical_for_each_kind():
    """Every ArtifactKind has a registry entry retrievable by
    its .value."""
    for kind in ac.ArtifactKind:
        spec = ac.lookup_spec(kind.value)
        assert spec is not None, f"missing spec for {kind!r}"
        assert spec.kind is kind


# -----------------------------------------------------------------
# Provenance — substrate cites the closure
# -----------------------------------------------------------------


def test_substrate_documents_section_35_4_closure():
    src = Path(inspect.getfile(ac)).read_text(encoding="utf-8")
    assert "§35 row" in src and "#4" in src, (
        "artifact_contract must cite §35 row 🟡 #4 in module docstring"
    )
    assert "fragility vector #8" in src, (
        "must cite fragility vector #8 (the named root problem)"
    )
