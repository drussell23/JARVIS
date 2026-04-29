"""Priority A Slice A2 — Default-claim registry + synthesizer regression spine.

The brain layer of mandatory claim density. Tests the registry that
holds DefaultClaimSpec entries + the pure-function synthesizer that
walks the registry and emits PropertyClaim objects subject to per-spec
filters.

Pins:
  §1   Master flag default true (asymmetric pattern)
  §2   Master flag empty/whitespace reads as default true
  §3   Master flag false-class strings disable
  §4   Master flag garbage values disable
  §5   DefaultClaimSpec is frozen + hashable
  §6   DefaultClaimSpec.to_dict round-trip
  §7   Three seed specs registered at module load
  §8   register_default_claim_spec idempotent on identical re-register
  §9   register_default_claim_spec rejects duplicate without overwrite
  §10  register_default_claim_spec accepts overwrite=True
  §11  unregister_default_claim_spec returns True/False appropriately
  §12  list_default_claim_specs returns alphabetical-stable tuple
  §13  reset_registry_for_tests clears + re-seeds
  §14  applies_to_op — file_pattern_filter matches via fnmatch
  §15  applies_to_op — file_pattern_filter empty-target rejected
  §16  applies_to_op — None file_pattern_filter always passes
  §17  applies_to_op — posture_filter matches case-insensitively
  §18  applies_to_op — posture_filter None-posture rejected
  §19  applies_to_op — None posture_filter always passes
  §20  applies_to_op — AND composition of file + posture filters
  §21  applies_to_op — never raises on garbage input
  §22  synthesize_default_claims master-off returns empty
  §23  synthesize_default_claims empty op_id returns empty
  §24  synthesize_default_claims happy path returns 3 default claims
  §25  synthesize_default_claims file-filter excludes non-Python ops
  §26  synthesize_default_claims posture-filter excludes wrong posture
  §27  synthesize_default_claims claim_ids deterministic across calls
  §28  synthesize_default_claims claim_ids differ for different op_ids
  §29  synthesize_default_claims marks all default-claims with
       metadata['default_claim']=True
  §30  synthesize_default_claims sets claimed_at_phase="PLAN"
  §31  synthesize_default_claims uses must_hold severity by default
  §32  synthesize_default_claims defensive — bad spec doesn't crash
       the whole synthesis
  §33  Registry + synthesizer NEVER imports orchestrator/phase_runner
  §34  Public API exposed from package __init__
  §35  Synthesized claims compatible with capture_claims (Slice 2.3)
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.core.ouroboros.governance.verification import (
    DefaultClaimSpec,
    PropertyClaim,
    SEVERITY_MUST_HOLD,
    capture_claims,
    default_claims_enabled,
    list_default_claim_specs,
    register_default_claim_spec,
    synthesize_default_claims,
    unregister_default_claim_spec,
)
from backend.core.ouroboros.governance.verification.default_claims import (
    DEFAULT_CLAIMS_SCHEMA_VERSION,
    reset_registry_for_tests,
)


@pytest.fixture
def fresh_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# ===========================================================================
# §1-§4 — Master flag
# ===========================================================================


def test_default_claims_enabled_default_true(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_DEFAULT_CLAIMS_ENABLED", raising=False)
    assert default_claims_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_default_claims_empty_reads_as_default_true(
    monkeypatch, val,
) -> None:
    monkeypatch.setenv("JARVIS_DEFAULT_CLAIMS_ENABLED", val)
    assert default_claims_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_default_claims_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_DEFAULT_CLAIMS_ENABLED", val)
    assert default_claims_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_default_claims_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_DEFAULT_CLAIMS_ENABLED", val)
    assert default_claims_enabled() is False


# ===========================================================================
# §5-§6 — Schema (frozen + serialization)
# ===========================================================================


def test_spec_is_frozen() -> None:
    spec = DefaultClaimSpec(claim_kind="test_passes")
    with pytest.raises((AttributeError, TypeError)):
        spec.claim_kind = "other"  # type: ignore[misc]


def test_spec_to_dict_round_trip() -> None:
    spec = DefaultClaimSpec(
        claim_kind="file_parses_after_change",
        severity=SEVERITY_MUST_HOLD,
        evidence_required=("target_files_post",),
        rationale="test rationale",
        file_pattern_filter="*.py",
        posture_filter=("HARDEN", "CONSOLIDATE"),
    )
    d = spec.to_dict()
    assert d["claim_kind"] == "file_parses_after_change"
    assert d["severity"] == SEVERITY_MUST_HOLD
    assert d["evidence_required"] == ["target_files_post"]
    assert d["file_pattern_filter"] == "*.py"
    assert d["posture_filter"] == ["HARDEN", "CONSOLIDATE"]
    assert d["schema_version"] == DEFAULT_CLAIMS_SCHEMA_VERSION


# ===========================================================================
# §7 — Seed specs
# ===========================================================================


def test_three_seed_specs_registered_at_module_load(fresh_registry) -> None:
    specs = list_default_claim_specs()
    kinds = sorted(s.claim_kind for s in specs)
    assert kinds == [
        "file_parses_after_change",
        "no_new_credential_shapes",
        "test_set_hash_stable",
    ]
    # All seeds use must_hold severity
    for spec in specs:
        assert spec.severity == SEVERITY_MUST_HOLD


# ===========================================================================
# §8-§13 — Registry surface
# ===========================================================================


def test_register_idempotent_on_identical(fresh_registry) -> None:
    spec = DefaultClaimSpec(
        claim_kind="custom_kind",
        evidence_required=("x",),
    )
    register_default_claim_spec(spec)
    register_default_claim_spec(spec)  # silent no-op
    specs = list_default_claim_specs()
    assert sum(1 for s in specs if s.claim_kind == "custom_kind") == 1


def test_register_rejects_duplicate_kind(fresh_registry) -> None:
    spec_a = DefaultClaimSpec(claim_kind="custom", rationale="A")
    spec_b = DefaultClaimSpec(claim_kind="custom", rationale="B")
    register_default_claim_spec(spec_a)
    register_default_claim_spec(spec_b)  # logged but not replaced
    specs = list_default_claim_specs()
    custom_specs = [s for s in specs if s.claim_kind == "custom"]
    assert len(custom_specs) == 1
    assert custom_specs[0].rationale == "A"


def test_register_overwrite_replaces(fresh_registry) -> None:
    spec_a = DefaultClaimSpec(claim_kind="custom", rationale="A")
    spec_b = DefaultClaimSpec(claim_kind="custom", rationale="B")
    register_default_claim_spec(spec_a)
    register_default_claim_spec(spec_b, overwrite=True)
    specs = list_default_claim_specs()
    custom_specs = [s for s in specs if s.claim_kind == "custom"]
    assert len(custom_specs) == 1
    assert custom_specs[0].rationale == "B"


def test_unregister_returns_correct_status(fresh_registry) -> None:
    register_default_claim_spec(DefaultClaimSpec(claim_kind="ephemeral"))
    assert unregister_default_claim_spec("ephemeral") is True
    assert unregister_default_claim_spec("ephemeral") is False
    assert unregister_default_claim_spec("never_registered") is False


def test_list_returns_alphabetical_stable(fresh_registry) -> None:
    specs1 = list_default_claim_specs()
    specs2 = list_default_claim_specs()
    kinds1 = [s.claim_kind for s in specs1]
    kinds2 = [s.claim_kind for s in specs2]
    assert kinds1 == kinds2
    assert kinds1 == sorted(kinds1)


def test_reset_clears_and_reseeds(fresh_registry) -> None:
    register_default_claim_spec(DefaultClaimSpec(claim_kind="extra"))
    assert any(s.claim_kind == "extra" for s in list_default_claim_specs())
    reset_registry_for_tests()
    assert all(s.claim_kind != "extra" for s in list_default_claim_specs())
    # Seeds re-registered
    assert len(list_default_claim_specs()) == 3


# ===========================================================================
# §14-§21 — applies_to_op predicate
# ===========================================================================


def test_file_pattern_matches_via_fnmatch() -> None:
    spec = DefaultClaimSpec(
        claim_kind="x", file_pattern_filter="*.py",
    )
    assert spec.applies_to_op(target_files=["a.py"])
    assert spec.applies_to_op(target_files=["x.txt", "a.py"])
    assert not spec.applies_to_op(target_files=["a.txt"])


def test_file_pattern_empty_targets_rejected() -> None:
    spec = DefaultClaimSpec(
        claim_kind="x", file_pattern_filter="*.py",
    )
    assert not spec.applies_to_op(target_files=())
    assert not spec.applies_to_op()


def test_no_file_pattern_always_passes_files() -> None:
    spec = DefaultClaimSpec(claim_kind="x", file_pattern_filter=None)
    assert spec.applies_to_op(target_files=())
    assert spec.applies_to_op(target_files=["any.txt"])


def test_posture_filter_case_insensitive() -> None:
    spec = DefaultClaimSpec(
        claim_kind="x", posture_filter=("HARDEN",),
    )
    assert spec.applies_to_op(posture="HARDEN")
    assert spec.applies_to_op(posture="harden")
    assert spec.applies_to_op(posture="Harden")
    assert not spec.applies_to_op(posture="EXPLORE")


def test_posture_filter_none_posture_rejected() -> None:
    spec = DefaultClaimSpec(
        claim_kind="x", posture_filter=("HARDEN",),
    )
    assert not spec.applies_to_op(posture=None)


def test_no_posture_filter_always_passes_posture() -> None:
    spec = DefaultClaimSpec(claim_kind="x", posture_filter=None)
    assert spec.applies_to_op(posture="EXPLORE")
    assert spec.applies_to_op(posture=None)


def test_filters_and_compose() -> None:
    spec = DefaultClaimSpec(
        claim_kind="x",
        file_pattern_filter="*.py",
        posture_filter=("HARDEN",),
    )
    assert spec.applies_to_op(target_files=["a.py"], posture="HARDEN")
    # Either filter failing → reject
    assert not spec.applies_to_op(target_files=["a.txt"], posture="HARDEN")
    assert not spec.applies_to_op(target_files=["a.py"], posture="EXPLORE")


def test_applies_never_raises_on_garbage() -> None:
    spec = DefaultClaimSpec(
        claim_kind="x",
        file_pattern_filter="*.py",
        posture_filter=("HARDEN",),
    )
    # None / int / object — nothing should crash
    assert isinstance(
        spec.applies_to_op(target_files=None, posture=None), bool,  # type: ignore[arg-type]
    )
    assert isinstance(
        spec.applies_to_op(target_files=[42, None], posture=42),  # type: ignore[arg-type]
        bool,
    )


# ===========================================================================
# §22-§32 — Synthesizer
# ===========================================================================


def test_synthesize_master_off_returns_empty(monkeypatch, fresh_registry) -> None:
    monkeypatch.setenv("JARVIS_DEFAULT_CLAIMS_ENABLED", "false")
    claims = synthesize_default_claims(
        op_id="op-1", target_files=["a.py"],
    )
    assert claims == ()


def test_synthesize_empty_op_id_returns_empty(fresh_registry) -> None:
    assert synthesize_default_claims(op_id="") == ()
    assert synthesize_default_claims(op_id="   ") == ()


def test_synthesize_happy_path_returns_three_claims(
    fresh_registry,
) -> None:
    claims = synthesize_default_claims(
        op_id="op-1", target_files=["a.py"],
    )
    assert len(claims) == 3
    kinds = sorted(c.property.kind for c in claims)
    assert kinds == [
        "file_parses_after_change",
        "no_new_credential_shapes",
        "test_set_hash_stable",
    ]


def test_synthesize_file_filter_excludes_non_python_ops(
    fresh_registry,
) -> None:
    # Op only touches yaml — file_parses_after_change should be filtered
    claims = synthesize_default_claims(
        op_id="op-1", target_files=["config.yaml"],
    )
    kinds = sorted(c.property.kind for c in claims)
    # file_parses_after_change has *.py filter — excluded
    # test_set_hash_stable + no_new_credential_shapes — None filter, included
    assert "file_parses_after_change" not in kinds
    assert "test_set_hash_stable" in kinds
    assert "no_new_credential_shapes" in kinds


def test_synthesize_posture_filter_excludes_wrong_posture(
    fresh_registry,
) -> None:
    # Register a posture-restricted spec
    register_default_claim_spec(
        DefaultClaimSpec(
            claim_kind="custom",
            posture_filter=("HARDEN",),
        ),
    )
    claims_explore = synthesize_default_claims(
        op_id="op-1", target_files=["a.py"], posture="EXPLORE",
    )
    claims_harden = synthesize_default_claims(
        op_id="op-1", target_files=["a.py"], posture="HARDEN",
    )
    assert "custom" not in [c.property.kind for c in claims_explore]
    assert "custom" in [c.property.kind for c in claims_harden]


def test_synthesize_claim_ids_deterministic(fresh_registry) -> None:
    c1 = synthesize_default_claims(
        op_id="op-deterministic", target_files=["a.py"],
    )
    c2 = synthesize_default_claims(
        op_id="op-deterministic", target_files=["a.py"],
    )
    ids1 = sorted(c.claim_id for c in c1)
    ids2 = sorted(c.claim_id for c in c2)
    assert ids1 == ids2


def test_synthesize_claim_ids_differ_for_different_ops(
    fresh_registry,
) -> None:
    c1 = synthesize_default_claims(op_id="op-A", target_files=["a.py"])
    c2 = synthesize_default_claims(op_id="op-B", target_files=["a.py"])
    ids1 = {c.claim_id for c in c1}
    ids2 = {c.claim_id for c in c2}
    assert not (ids1 & ids2)  # no overlap


def test_synthesize_marks_default_claim_metadata(fresh_registry) -> None:
    claims = synthesize_default_claims(
        op_id="op-1", target_files=["a.py"],
    )
    for c in claims:
        meta = dict(c.property.metadata)
        assert meta.get("default_claim") is True


def test_synthesize_sets_plan_phase(fresh_registry) -> None:
    claims = synthesize_default_claims(
        op_id="op-1", target_files=["a.py"],
    )
    for c in claims:
        assert c.claimed_at_phase == "PLAN"


def test_synthesize_uses_must_hold_for_seeds(fresh_registry) -> None:
    claims = synthesize_default_claims(
        op_id="op-1", target_files=["a.py"],
    )
    for c in claims:
        assert c.severity == SEVERITY_MUST_HOLD


def test_synthesize_defensive_per_spec(monkeypatch, fresh_registry) -> None:
    """Even if a spec's applies_to_op raises, synthesis continues
    and returns the survivors. Defensive contract."""
    # Register a spec whose claim_kind triggers a Property.make
    # path that should still succeed; manually raise from one
    # spec by monkeypatching its applies_to_op via a subclass.
    class BrokenSpec(DefaultClaimSpec):
        def applies_to_op(self, *, target_files=(), posture=None):
            raise RuntimeError("boom")
    bad_spec = BrokenSpec(claim_kind="broken")
    register_default_claim_spec(bad_spec)
    claims = synthesize_default_claims(
        op_id="op-1", target_files=["a.py"],
    )
    # The 3 healthy seed specs still synthesize; broken one skipped
    kinds = [c.property.kind for c in claims]
    assert "broken" not in kinds
    assert "file_parses_after_change" in kinds


# ===========================================================================
# §33 — Authority invariants
# ===========================================================================


def test_no_orchestrator_imports() -> None:
    from backend.core.ouroboros.governance.verification import default_claims
    src = inspect.getsource(default_claims)
    forbidden = ("orchestrator", "phase_runner", "candidate_generator")
    for token in forbidden:
        # Allow string-literal mentions in docstrings, but not actual imports
        # (rough check — confirm no `from ...orchestrator import` etc.)
        assert f"from backend.core.ouroboros.governance.{token}" not in src
        assert f"import backend.core.ouroboros.governance.{token}" not in src


# ===========================================================================
# §34 — Public API
# ===========================================================================


def test_public_api_exposed_from_package() -> None:
    from backend.core.ouroboros.governance import verification
    assert "DefaultClaimSpec" in verification.__all__
    assert "default_claims_enabled" in verification.__all__
    assert "list_default_claim_specs" in verification.__all__
    assert "register_default_claim_spec" in verification.__all__
    assert "synthesize_default_claims" in verification.__all__
    assert "unregister_default_claim_spec" in verification.__all__


# ===========================================================================
# §35 — End-to-end with capture_claims
# ===========================================================================


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    """End-to-end fixture: temporary ledger + all flags on."""
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_DEFAULT_CLAIMS_ENABLED", "true")
    monkeypatch.setenv(
        "OUROBOROS_BATTLE_SESSION_ID", "default-claims-test",
    )
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        reset_all_for_tests,
    )
    reset_all_for_tests()
    yield tmp_path
    reset_all_for_tests()


def test_synthesized_claims_persist_via_capture_claims(
    isolated_ledger, fresh_registry,
) -> None:
    claims = synthesize_default_claims(
        op_id="op-e2e", target_files=["a.py"],
    )
    assert len(claims) == 3

    async def _run():
        from backend.core.ouroboros.governance.verification import (
            get_recorded_claims,
        )
        captured = await capture_claims(
            op_id="op-e2e", claims=claims,
        )
        recovered = get_recorded_claims(
            op_id="op-e2e", session_id="default-claims-test",
        )
        return captured, recovered

    captured, recovered = asyncio.run(_run())
    assert captured == 3
    assert len(recovered) == 3
    kinds = sorted(c.property.kind for c in recovered)
    assert kinds == [
        "file_parses_after_change",
        "no_new_credential_shapes",
        "test_set_hash_stable",
    ]
    # All recovered claims are PropertyClaim instances (round-trip safe)
    for c in recovered:
        assert isinstance(c, PropertyClaim)
        assert c.severity == SEVERITY_MUST_HOLD
        assert c.is_load_bearing is True
