"""Regression spine for §40 Wave 1 #4 — M10 ArchitectureProposer
graduation contract.

Covers the §33.1 canonical-shape contract that gates the
``JARVIS_M10_ARCH_PROPOSER_ENABLED`` master-flag flip per
operator binding §30.5.2 ("30+ proposal-acceptance audit"):

* 5-value closed :class:`M10GraduationVerdict` taxonomy
* 5-gate first-match-wins cadence — every verdict reachable
* Composes canonical M10 substrate (M10ProposalPhase enum +
  aggregate_phase_histogram reader + m10_arch_proposer_enabled)
  — zero parallel state, zero hardcoded phase strings
* 4 AST pins canonical-source pass + 4 synthetic regressions
* §33.5 frozen report to_dict projection completeness
* Env knob clamping discipline (no hardcoding)
* §33.1 harness master flag default-TRUE separation
* FlagRegistry seeds auto-discovered
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, Dict

import pytest


from backend.core.ouroboros.governance import (
    m10_arch_proposer_graduation_contract as gc,
)
from backend.core.ouroboros.governance.m10_arch_proposer_graduation_contract import (  # noqa: E501
    M10GraduationReport,
    M10GraduationVerdict,
    M10_GRADUATION_REPORT_SCHEMA_VERSION,
    _AcceptanceSnapshot,
    _canonical_terminal_phase_values,
    is_harness_enabled,
    is_ready_for_graduation,
    max_rejection_ratio_knob,
    min_required_acceptances_knob,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_m10_contract(monkeypatch):
    """Reset env state per-test. The substrate flag MUST be off so
    Gate 1 falls through to Gates 2-3; the harness flag MUST be at
    default so the default-TRUE behavior is exercised."""
    for env in (
        "JARVIS_M10_GRADUATION_CONTRACT_ENABLED",
        "JARVIS_M10_GRADUATION_MIN_REQUIRED_ACCEPTANCES",
        "JARVIS_M10_GRADUATION_MAX_REJECTION_RATIO",
        "JARVIS_M10_ARCH_PROPOSER_ENABLED",
    ):
        monkeypatch.delenv(env, raising=False)
    yield


# ---------------------------------------------------------------------------
# §33.1 — harness master flag default-TRUE
# ---------------------------------------------------------------------------


class TestHarnessMasterFlag:
    """Per §33.1 separation — the contract is a measurement
    surface; default-TRUE so operators can query it any time. The
    cognitive substrate (JARVIS_M10_ARCH_PROPOSER_ENABLED) stays
    default-FALSE per §30.5.2 verbatim."""

    def test_harness_default_true(self):
        assert is_harness_enabled() is True

    @pytest.mark.parametrize(
        "truthy", ["1", "true", "TRUE", "yes", "on", "Yes"],
    )
    def test_harness_truthy_keeps_on(self, monkeypatch, truthy):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_CONTRACT_ENABLED", truthy,
        )
        assert is_harness_enabled() is True

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off", "bogus"],
    )
    def test_harness_falsy_turns_off(self, monkeypatch, falsy):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_CONTRACT_ENABLED", falsy,
        )
        assert is_harness_enabled() is False

    def test_empty_keeps_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_CONTRACT_ENABLED", "  ",
        )
        # whitespace-only treated as unset → default TRUE
        assert is_harness_enabled() is True


# ---------------------------------------------------------------------------
# Env knob clamping discipline
# ---------------------------------------------------------------------------


class TestEnvKnobClamping:
    def test_min_required_default(self):
        # §30.5.2 binding
        assert min_required_acceptances_knob() == 30

    def test_min_required_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_MIN_REQUIRED_ACCEPTANCES", "100",
        )
        assert min_required_acceptances_knob() == 100

    @pytest.mark.parametrize("bad", ["0", "-5", "not-int", ""])
    def test_min_required_invalid_falls_back(
        self, monkeypatch, bad,
    ):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_MIN_REQUIRED_ACCEPTANCES", bad,
        )
        assert min_required_acceptances_knob() == 30

    def test_max_ratio_default(self):
        assert max_rejection_ratio_knob() == pytest.approx(0.50)

    def test_max_ratio_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_MAX_REJECTION_RATIO", "0.25",
        )
        assert max_rejection_ratio_knob() == pytest.approx(0.25)

    def test_max_ratio_clamped_above_one(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_MAX_REJECTION_RATIO", "999.0",
        )
        assert max_rejection_ratio_knob() == 1.0

    @pytest.mark.parametrize("bad", ["-0.1", "not-float", ""])
    def test_max_ratio_invalid_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_MAX_REJECTION_RATIO", bad,
        )
        assert max_rejection_ratio_knob() == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# Closed 5-value verdict taxonomy
# ---------------------------------------------------------------------------


class TestVerdictTaxonomy:
    def test_exactly_5_values(self):
        values = {v.value for v in M10GraduationVerdict}
        assert values == {
            "ready_for_graduation",
            "insufficient_proposals",
            "excessive_rejections",
            "already_graduated",
            "disabled",
        }

    def test_str_subclass(self):
        # Inherits str for ergonomic comparison
        assert (
            M10GraduationVerdict.READY_FOR_GRADUATION
            == "ready_for_graduation"
        )


# ---------------------------------------------------------------------------
# §33.5 frozen report artifact
# ---------------------------------------------------------------------------


class TestReportArtifact:
    def test_to_dict_full_shape(self):
        report = M10GraduationReport(
            schema_version=M10_GRADUATION_REPORT_SCHEMA_VERSION,
            verdict=M10GraduationVerdict.INSUFFICIENT_PROPOSALS,
            observed_accepted=12,
            observed_rejected=3,
            rejection_ratio=0.20,
            detail="diag",
        )
        d = report.to_dict()
        expected_keys = {
            "schema_version", "verdict",
            "observed_accepted", "observed_rejected",
            "rejection_ratio", "detail",
        }
        assert set(d.keys()) == expected_keys
        assert d["verdict"] == "insufficient_proposals"
        assert d["observed_accepted"] == 12
        assert d["observed_rejected"] == 3
        assert d["rejection_ratio"] == pytest.approx(0.20)
        assert d["schema_version"] == (
            M10_GRADUATION_REPORT_SCHEMA_VERSION
        )

    def test_detail_clamped_to_256_chars(self):
        report = M10GraduationReport(
            schema_version=M10_GRADUATION_REPORT_SCHEMA_VERSION,
            verdict=M10GraduationVerdict.READY_FOR_GRADUATION,
            observed_accepted=100,
            observed_rejected=0,
            rejection_ratio=0.0,
            detail="x" * 1000,
        )
        d = report.to_dict()
        assert len(d["detail"]) == 256

    def test_report_is_frozen(self):
        report = M10GraduationReport(
            schema_version=M10_GRADUATION_REPORT_SCHEMA_VERSION,
            verdict=M10GraduationVerdict.DISABLED,
            observed_accepted=0,
            observed_rejected=0,
            rejection_ratio=0.0,
            detail="",
        )
        with pytest.raises(Exception):
            report.observed_accepted = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Composition: canonical M10 substrate
# ---------------------------------------------------------------------------


class TestComposesCanonicalSubstrate:
    """The contract MUST compose canonical sources, not duplicate
    them. These tests pin the substrate composition end-to-end."""

    def test_terminal_phase_values_match_enum(self):
        """The accept/reject phase sets are derived from the
        canonical M10ProposalPhase enum, not hardcoded strings.
        Drift here = the enum changed and we missed it."""
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        accept, reject = _canonical_terminal_phase_values()
        # ACCEPT terminal = GRADUATED
        assert accept == {M10ProposalPhase.GRADUATED.value}
        # REJECT terminals = FAILED + REJECTED + EXPIRED + PUSH_FAILED
        assert reject == {
            M10ProposalPhase.FAILED.value,
            M10ProposalPhase.REJECTED.value,
            M10ProposalPhase.EXPIRED.value,
            M10ProposalPhase.PUSH_FAILED.value,
        }

    def test_terminal_phase_values_are_disjoint(self):
        accept, reject = _canonical_terminal_phase_values()
        assert not (accept & reject)


# ---------------------------------------------------------------------------
# 5-gate first-match-wins cadence — every verdict reachable
# ---------------------------------------------------------------------------


def _snapshot(accepted: int, rejected: int):
    return lambda: _AcceptanceSnapshot(
        accepted=accepted, rejected=rejected,
    )


class TestGateCascade:

    # --- Gate 0: harness disabled ----------------------------------

    def test_gate_0_harness_off_routes_disabled(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_CONTRACT_ENABLED", "false",
        )
        r = is_ready_for_graduation(snapshot_reader=_snapshot(100, 0))
        assert r.verdict is M10GraduationVerdict.DISABLED
        assert r.detail == "harness_master_off"
        assert r.observed_accepted == 0
        assert r.observed_rejected == 0

    # --- Gate 1: substrate already graduated -----------------------

    def test_gate_1_substrate_on_routes_already_graduated(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
        )
        r = is_ready_for_graduation(
            snapshot_reader=_snapshot(5, 50),  # would otherwise fail
        )
        assert r.verdict is M10GraduationVerdict.ALREADY_GRADUATED
        assert "already flipped" in r.detail.lower()

    # --- Gate 2: insufficient proposals ----------------------------

    def test_gate_2_zero_acceptances_routes_insufficient(self):
        r = is_ready_for_graduation(snapshot_reader=_snapshot(0, 0))
        assert (
            r.verdict is M10GraduationVerdict.INSUFFICIENT_PROPOSALS
        )
        assert r.observed_accepted == 0
        # detail mentions §30.5.2 binding
        assert "§30.5.2" in r.detail

    def test_gate_2_below_threshold_routes_insufficient(self):
        r = is_ready_for_graduation(snapshot_reader=_snapshot(29, 5))
        assert (
            r.verdict is M10GraduationVerdict.INSUFFICIENT_PROPOSALS
        )

    def test_gate_2_threshold_boundary_passes_into_gate3(self):
        # Exactly 30 accepted, 0 rejected → passes gate 2 → READY
        r = is_ready_for_graduation(snapshot_reader=_snapshot(30, 0))
        assert (
            r.verdict is M10GraduationVerdict.READY_FOR_GRADUATION
        )

    def test_gate_2_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_MIN_REQUIRED_ACCEPTANCES", "5",
        )
        r = is_ready_for_graduation(snapshot_reader=_snapshot(4, 0))
        assert (
            r.verdict is M10GraduationVerdict.INSUFFICIENT_PROPOSALS
        )

    # --- Gate 3: excessive rejections ------------------------------

    def test_gate_3_high_rejection_routes_excessive(self):
        # 30 accepted, 100 rejected → ratio ~0.77 > 0.50
        r = is_ready_for_graduation(
            snapshot_reader=_snapshot(30, 100),
        )
        assert (
            r.verdict is M10GraduationVerdict.EXCESSIVE_REJECTIONS
        )
        assert r.rejection_ratio > 0.50

    def test_gate_3_boundary_passes(self):
        # 30 accepted, 30 rejected → ratio exactly 0.50, NOT > 0.50
        r = is_ready_for_graduation(snapshot_reader=_snapshot(30, 30))
        assert (
            r.verdict is M10GraduationVerdict.READY_FOR_GRADUATION
        )

    def test_gate_3_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_GRADUATION_MAX_REJECTION_RATIO", "0.10",
        )
        # 30 accepted, 10 rejected → ratio 0.25 > 0.10
        r = is_ready_for_graduation(snapshot_reader=_snapshot(30, 10))
        assert (
            r.verdict is M10GraduationVerdict.EXCESSIVE_REJECTIONS
        )

    # --- Gate 4: ready for graduation ------------------------------

    def test_gate_4_clean_acceptance_routes_ready(self):
        r = is_ready_for_graduation(
            snapshot_reader=_snapshot(50, 5),
        )
        assert (
            r.verdict is M10GraduationVerdict.READY_FOR_GRADUATION
        )
        assert "JARVIS_M10_ARCH_PROPOSER_ENABLED" in r.detail

    def test_gate_4_extreme_volume(self):
        r = is_ready_for_graduation(
            snapshot_reader=_snapshot(10_000, 100),
        )
        assert (
            r.verdict is M10GraduationVerdict.READY_FOR_GRADUATION
        )
        assert r.rejection_ratio < 0.05


# ---------------------------------------------------------------------------
# Defensive: NEVER raises
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_snapshot_reader_raises_falls_back_to_zero(self):
        def boom():
            raise RuntimeError("ledger blew up")
        r = is_ready_for_graduation(snapshot_reader=boom)
        # Falls back to INSUFFICIENT_PROPOSALS (gate 2 with 0 acceptances)
        assert (
            r.verdict is M10GraduationVerdict.INSUFFICIENT_PROPOSALS
        )
        assert r.observed_accepted == 0
        assert r.observed_rejected == 0

    def test_default_reader_with_real_canonical_substrate(self):
        # No snapshot_reader → uses _collect_evidence_default which
        # composes aggregate_phase_histogram from the real substrate.
        # Result depends on .jarvis/m10/proposals.jsonl state; with
        # an empty / nonexistent ledger we expect INSUFFICIENT.
        r = is_ready_for_graduation()
        # One of these two is correct; both are honest readings of
        # the canonical store (most environments will be empty).
        assert r.verdict in (
            M10GraduationVerdict.INSUFFICIENT_PROPOSALS,
            M10GraduationVerdict.READY_FOR_GRADUATION,
            M10GraduationVerdict.EXCESSIVE_REJECTIONS,
        )


# ---------------------------------------------------------------------------
# AST pins — 4 canonical-source pass + 4 synthetic regressions
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    """Load the canonical contract source + tree."""
    path = Path(gc.__file__)
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, tree


@pytest.fixture
def pins():
    return gc.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_4_pins_registered(self, pins):
        assert len(pins) == 4
        names = {p.invariant_name for p in pins}
        assert names == {
            "m10_graduation_verdict_taxonomy_closed",
            "m10_graduation_authority_asymmetry",
            "m10_graduation_pattern_compliance",
            "m10_graduation_composes_canonical_store",
        }

    def test_verdict_taxonomy_pin_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_verdict_taxonomy_closed"
        )
        assert not pin.validate(tree, src)

    def test_authority_asymmetry_pin_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_authority_asymmetry"
        )
        assert not pin.validate(tree, src)

    def test_pattern_compliance_pin_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_pattern_compliance"
        )
        assert not pin.validate(tree, src)

    def test_composes_canonical_store_pin_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_composes_canonical_store"
        )
        assert not pin.validate(tree, src)


class TestAstPinsSyntheticRegression:
    """Each pin MUST fire when its invariant is violated."""

    def test_verdict_taxonomy_pin_fires_on_missing_value(self, pins):
        synthetic = """
import enum
class M10GraduationVerdict(str, enum.Enum):
    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_PROPOSALS = "insufficient_proposals"
    EXCESSIVE_REJECTIONS = "excessive_rejections"
    ALREADY_GRADUATED = "already_graduated"
    # MISSING: DISABLED
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_verdict_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "missing" in violations[0]

    def test_verdict_taxonomy_pin_fires_on_drift_value(self, pins):
        synthetic = """
import enum
class M10GraduationVerdict(str, enum.Enum):
    READY_FOR_GRADUATION = "ready_for_graduation"
    INSUFFICIENT_PROPOSALS = "insufficient_proposals"
    EXCESSIVE_REJECTIONS = "excessive_rejections"
    ALREADY_GRADUATED = "already_graduated"
    DISABLED = "disabled"
    UNEXPECTED_NEW_VERDICT = "drift_value"
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_verdict_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "drift" in violations[0]

    @pytest.mark.parametrize(
        "forbidden_module",
        [
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            # The archived legacy module — explicitly forbidden.
            (
                "backend.core.ouroboros.governance"
                ".graduation_orchestrator"
            ),
        ],
    )
    def test_authority_pin_fires_on_forbidden_import(
        self, pins, forbidden_module,
    ):
        synthetic = f"from {forbidden_module} import something\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert any(
            forbidden_module in v for v in violations
        )

    def test_authority_pin_does_not_fire_on_allowed_m10_imports(
        self, pins,
    ):
        # m10.primitives + m10.proposal_store are the canonical
        # sources we MUST compose — they MUST NOT trigger.
        synthetic = (
            "from backend.core.ouroboros.governance.m10.primitives "
            "import M10ProposalPhase\n"
            "from backend.core.ouroboros.governance.m10.proposal_store "
            "import aggregate_phase_histogram\n"
        )
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert not violations

    def test_pattern_pin_fires_on_missing_symbol(self, pins):
        synthetic = "x = 1\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_pattern_compliance"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "missing" in violations[0]

    def test_composes_store_pin_fires_on_hardcoded_strings(
        self, pins,
    ):
        # Synthetic file that hardcodes phase strings instead of
        # composing the canonical M10ProposalPhase enum.
        synthetic = (
            "ACCEPT_PHASES = {'graduated'}\n"
            "REJECT_PHASES = {'failed', 'rejected', 'expired'}\n"
        )
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "m10_graduation_composes_canonical_store"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        # All three checks fire
        assert any("M10ProposalPhase" in v for v in violations)
        assert any(
            "aggregate_phase_histogram" in v for v in violations
        )
        assert any(
            "m10_arch_proposer_enabled" in v for v in violations
        )


# ---------------------------------------------------------------------------
# Canonical-source smoke
# ---------------------------------------------------------------------------


class TestCanonicalSourceSmokes:
    def test_m10_primitives_master_flag_accessor_exists(self):
        from backend.core.ouroboros.governance.m10 import primitives
        assert callable(primitives.m10_arch_proposer_enabled)
        # Default-FALSE invariant honored by canonical substrate
        assert primitives.m10_arch_proposer_enabled() is False

    def test_m10_proposal_phase_enum_complete(self):
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        # We rely on GRADUATED / FAILED / REJECTED / EXPIRED /
        # PUSH_FAILED being canonical — if any are removed our
        # contract silently miscounts.
        canonical_required = {
            "GRADUATED", "FAILED", "REJECTED", "EXPIRED",
            "PUSH_FAILED",
        }
        names = {p.name for p in M10ProposalPhase}
        assert canonical_required <= names

    def test_aggregate_phase_histogram_callable(self):
        from backend.core.ouroboros.governance.m10.proposal_store import (  # noqa: E501
            aggregate_phase_histogram,
        )
        # Should never raise on an empty or missing ledger.
        result = aggregate_phase_histogram()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# FlagRegistry seeds auto-discovered
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    def test_all_3_seeds_auto_discovered(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        names = {f.name for f in reg.list_all()}
        for expected in [
            "JARVIS_M10_GRADUATION_CONTRACT_ENABLED",
            "JARVIS_M10_GRADUATION_MIN_REQUIRED_ACCEPTANCES",
            "JARVIS_M10_GRADUATION_MAX_REJECTION_RATIO",
        ]:
            assert expected in names, f"missing seed: {expected}"

    def test_harness_seed_default_true(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            (
                f for f in reg.list_all()
                if f.name == "JARVIS_M10_GRADUATION_CONTRACT_ENABLED"
            ),
            None,
        )
        assert spec is not None
        # §33.1 separation — harness default-TRUE
        assert spec.default is True
        assert spec.category.value == "observability"

    def test_min_required_seed_default_30(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            (
                f for f in reg.list_all()
                if f.name == (
                    "JARVIS_M10_GRADUATION_MIN_REQUIRED_ACCEPTANCES"
                )
            ),
            None,
        )
        assert spec is not None
        # §30.5.2 binding: 30+ proposal-acceptance audit
        assert spec.default == 30
        assert spec.category.value == "tuning"


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApiStability:
    def test_all_exports_callable_or_class(self):
        for name in gc.__all__:
            obj = getattr(gc, name)
            assert obj is not None

    def test_schema_version_constant(self):
        assert isinstance(
            M10_GRADUATION_REPORT_SCHEMA_VERSION, str,
        )
        assert (
            M10_GRADUATION_REPORT_SCHEMA_VERSION
            .startswith("m10_graduation_report.")
        )
