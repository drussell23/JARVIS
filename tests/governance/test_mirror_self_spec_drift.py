"""§40 #14 Mirror-Self spec-drift validator — regression spine.

Coverage tracks the validator's contracts:

  * Parse contract — ``_parse_claimed_flag_defaults`` extracts ONLY
    unambiguous boolean default claims from CLAUDE.md's consistent
    phrasings (``default-TRUE`` / ``default-FALSE`` /
    ``(default `true`)`` / ``default `false```); conflicting claims
    for the same flag → ambiguous → excluded (fail-open).
  * Detect contract — ``detect_spec_drift`` emits a SpecDriftRecord
    ONLY when the flag IS registered AND its type is BOOL AND the
    registered default ≠ the claimed default. Absent / non-bool /
    ambiguous → SKIP (no false positive).
  * Severity contract — doll-metric-gated escalation: WARNING by
    default, CRITICAL only when completion_ratio ≥ gate ratio.
  * Composition contract — DRIFTED report → InvariantDriftRecords
    consumable by the existing invariant_drift_auto_action_bridge
    (no parallel bridge); §33.1 master default-FALSE;
    authority-asymmetry + AST pins.
  * Live contract — run against the REAL CLAUDE.md + populated
    registry and surface any genuine drift.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance import (
    mirror_self_spec_drift as msd,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
)
from backend.core.ouroboros.governance.invariant_drift_auditor import (
    DriftKind,
    DriftSeverity,
    InvariantDriftRecord,
)


_MASTER = "JARVIS_MIRROR_SELF_SPEC_DRIFT_ENABLED"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bool_spec(name: str, default: bool) -> FlagSpec:
    return FlagSpec(
        name=name,
        type=FlagType.BOOL,
        default=default,
        description="synthetic",
        category=Category.SAFETY,
        source_file="synthetic/test.py",
    )


def _int_spec(name: str, default: int = 5) -> FlagSpec:
    return FlagSpec(
        name=name,
        type=FlagType.INT,
        default=default,
        description="synthetic int",
        category=Category.TUNING,
        source_file="synthetic/test.py",
    )


@pytest.fixture()
def master_on():
    with patch.dict("os.environ", {_MASTER: "true"}, clear=False):
        yield


@pytest.fixture()
def master_off():
    with patch.dict("os.environ", {_MASTER: "false"}, clear=False):
        yield


# ---------------------------------------------------------------------------
# 1. _parse_claimed_flag_defaults
# ---------------------------------------------------------------------------


class TestParse:
    def test_default_true_dash_form(self):
        claims = msd._parse_claimed_flag_defaults(
            "Master `JARVIS_TOOL_RENDER_REGISTRY_ENABLED` default-TRUE."
        )
        assert claims.get("JARVIS_TOOL_RENDER_REGISTRY_ENABLED") is True

    def test_default_false_dash_form(self):
        claims = msd._parse_claimed_flag_defaults(
            "`JARVIS_FOO_ENABLED` default-FALSE per §33.1."
        )
        assert claims.get("JARVIS_FOO_ENABLED") is False

    def test_default_paren_true_form(self):
        claims = msd._parse_claimed_flag_defaults(
            "`JARVIS_MULTI_FILE_GEN_ENABLED` (default `true`)"
        )
        assert claims.get("JARVIS_MULTI_FILE_GEN_ENABLED") is True

    def test_default_backtick_false_form(self):
        claims = msd._parse_claimed_flag_defaults(
            "`JARVIS_BAR_ENABLED` default `false` — opt-in."
        )
        assert claims.get("JARVIS_BAR_ENABLED") is False

    def test_default_backtick_true_form(self):
        claims = msd._parse_claimed_flag_defaults(
            "`JARVIS_DIRECTION_INFERRER_ENABLED` default `true`"
        )
        assert claims.get("JARVIS_DIRECTION_INFERRER_ENABLED") is True

    def test_case_insensitive_dash(self):
        claims = msd._parse_claimed_flag_defaults(
            "`JARVIS_X_ENABLED` default-true and "
            "`JARVIS_Y_ENABLED` Default-False"
        )
        assert claims.get("JARVIS_X_ENABLED") is True
        assert claims.get("JARVIS_Y_ENABLED") is False

    def test_ambiguous_conflicting_claims_excluded(self):
        # Same flag claimed BOTH true AND false → fail-open exclude.
        claims = msd._parse_claimed_flag_defaults(
            "`JARVIS_AMBIG_ENABLED` default-TRUE ... later "
            "`JARVIS_AMBIG_ENABLED` (default `false`)"
        )
        assert "JARVIS_AMBIG_ENABLED" not in claims

    def test_consistent_repeated_claims_kept(self):
        # Same flag, same value twice → still unambiguous, kept.
        claims = msd._parse_claimed_flag_defaults(
            "`JARVIS_REPEAT_ENABLED` default-TRUE ... "
            "`JARVIS_REPEAT_ENABLED` default `true`"
        )
        assert claims.get("JARVIS_REPEAT_ENABLED") is True

    def test_non_flag_text_ignored(self):
        claims = msd._parse_claimed_flag_defaults(
            "The system is enabled by default and true to its nature."
        )
        assert claims == {}

    def test_non_jarvis_prefixed_name_ignored(self):
        claims = msd._parse_claimed_flag_defaults(
            "`SOME_OTHER_ENABLED` default-TRUE"
        )
        assert claims == {}

    def test_empty_and_garbage_never_raise(self):
        assert msd._parse_claimed_flag_defaults("") == {}
        assert msd._parse_claimed_flag_defaults(None) == {}  # type: ignore[arg-type]
        assert msd._parse_claimed_flag_defaults(12345) == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. detect_spec_drift
# ---------------------------------------------------------------------------


class TestDetect:
    def test_aligned_true_true(self, master_on):
        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_A_ENABLED", True))
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_A_ENABLED` default-TRUE",
            registry=reg,
        )
        assert report.verdict is msd.SpecDriftVerdict.ALIGNED
        assert report.records == ()

    def test_drifted_true_claim_false_actual(self, master_on):
        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_A_ENABLED", False))
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_A_ENABLED` default-TRUE",
            registry=reg,
        )
        assert report.verdict is msd.SpecDriftVerdict.DRIFTED
        assert len(report.records) == 1
        rec = report.records[0]
        assert rec.flag == "JARVIS_A_ENABLED"
        assert rec.claimed_default is True
        assert rec.actual_default is False
        assert rec.source_file == "synthetic/test.py"

    def test_drifted_false_claim_true_actual(self, master_on):
        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_B_ENABLED", True))
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_B_ENABLED` default `false`",
            registry=reg,
        )
        assert report.verdict is msd.SpecDriftVerdict.DRIFTED
        assert report.records[0].claimed_default is False
        assert report.records[0].actual_default is True

    def test_flag_not_in_registry_no_drift(self, master_on):
        reg = FlagRegistry()  # empty
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_MISSING_ENABLED` default-TRUE",
            registry=reg,
        )
        # No FP — absent flag is a separate concern, not drift.
        assert report.records == ()
        assert report.verdict is msd.SpecDriftVerdict.ALIGNED
        # ... but it may be counted as unregistered.
        assert report.unregistered_count >= 1

    def test_non_bool_flag_skipped(self, master_on):
        reg = FlagRegistry()
        reg.register(_int_spec("JARVIS_C_ENABLED", 5))
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_C_ENABLED` default-TRUE",
            registry=reg,
        )
        # Non-bool → skipped, no record, no FP.
        assert report.records == ()
        assert report.verdict is msd.SpecDriftVerdict.ALIGNED

    def test_ambiguous_claim_skipped(self, master_on):
        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_AMBIG_ENABLED", True))
        report = msd.detect_spec_drift(
            spec_text=(
                "`JARVIS_AMBIG_ENABLED` default-TRUE ... "
                "`JARVIS_AMBIG_ENABLED` (default `false`)"
            ),
            registry=reg,
        )
        # Ambiguous claim excluded by parser → no drift evaluation.
        assert report.records == ()

    def test_insufficient_data_when_no_claims(self, master_on):
        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_A_ENABLED", True))
        report = msd.detect_spec_drift(
            spec_text="no parseable flag claims here at all",
            registry=reg,
        )
        assert report.verdict is msd.SpecDriftVerdict.INSUFFICIENT_DATA
        assert report.records == ()

    def test_master_off_disabled_report(self, master_off):
        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_A_ENABLED", False))
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_A_ENABLED` default-TRUE",
            registry=reg,
        )
        assert report.verdict is msd.SpecDriftVerdict.DISABLED
        assert report.records == ()

    def test_unreadable_spec_path_never_raises(self, master_on):
        # spec_text None + a registry; if CLAUDE.md unreadable the
        # module falls back to empty — but we force the read to fail.
        with patch.object(msd, "_read_default_spec_text", return_value=""):
            report = msd.detect_spec_drift(registry=FlagRegistry())
        assert report.verdict in (
            msd.SpecDriftVerdict.INSUFFICIENT_DATA,
            msd.SpecDriftVerdict.ALIGNED,
        )
        assert report.records == ()

    def test_missing_registry_does_not_raise(self, master_on):
        # registry=None resolves the populated one; must not raise.
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_A_ENABLED` default-TRUE",
            registry=None,
        )
        assert isinstance(report, msd.SpecDriftReport)


# ---------------------------------------------------------------------------
# 3. Doll-metric severity gate (#15)
# ---------------------------------------------------------------------------


class TestDollSeverityGate:
    def _drifted_report(self, master_on_env):
        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_A_ENABLED", False))
        return msd.detect_spec_drift(
            spec_text="`JARVIS_A_ENABLED` default-TRUE",
            registry=reg,
        )

    def test_below_threshold_warning(self, master_on):
        # completion_ratio below gate → stays WARNING (conservative).
        with patch.object(
            msd, "_doll_completion_ratio", return_value=0.10,
        ):
            report = self._drifted_report(master_on)
        assert report.records[0].severity is DriftSeverity.WARNING

    def test_at_or_above_threshold_critical(self, master_on):
        with patch.object(
            msd, "_doll_completion_ratio", return_value=0.99,
        ):
            report = self._drifted_report(master_on)
        assert report.records[0].severity is DriftSeverity.CRITICAL

    def test_doll_unavailable_stays_warning(self, master_on):
        # None ratio (disabled / unavailable) → conservative WARNING.
        with patch.object(
            msd, "_doll_completion_ratio", return_value=None,
        ):
            report = self._drifted_report(master_on)
        assert report.records[0].severity is DriftSeverity.WARNING


# ---------------------------------------------------------------------------
# 4. Converter feeds the existing bridge
# ---------------------------------------------------------------------------


class TestBridgeConverter:
    def test_converter_yields_invariant_drift_records(self, master_on):
        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_A_ENABLED", False))
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_A_ENABLED` default-TRUE",
            registry=reg,
        )
        records = msd.to_invariant_drift_records(report)
        assert len(records) == 1
        rec = records[0]
        assert isinstance(rec, InvariantDriftRecord)
        assert rec.drift_kind is DriftKind.SPEC_DRIFT
        assert rec.severity is report.records[0].severity
        assert "JARVIS_A_ENABLED" in rec.affected_keys

    def test_aligned_report_yields_no_records(self, master_on):
        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_A_ENABLED", True))
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_A_ENABLED` default-TRUE",
            registry=reg,
        )
        assert msd.to_invariant_drift_records(report) == ()

    def test_records_accepted_by_existing_bridge(self, master_on):
        from backend.core.ouroboros.governance.auto_action_router import (
            AdvisoryActionType,
            AutoActionProposalLedger,
        )
        from backend.core.ouroboros.governance import (
            invariant_drift_auto_action_bridge as bridge_mod,
        )

        reg = FlagRegistry()
        reg.register(_bool_spec("JARVIS_A_ENABLED", False))
        report = msd.detect_spec_drift(
            spec_text="`JARVIS_A_ENABLED` default-TRUE",
            registry=reg,
        )
        records = msd.to_invariant_drift_records(report)

        # The bridge's pure drift→action mapping must accept our
        # records (it reads only .severity / .drift_kind).
        action_type = bridge_mod.drift_to_action_type(records)
        assert action_type is not AdvisoryActionType.NO_ACTION

        # And the full emit() path must accept them without error,
        # filing an advisory action into an in-memory ledger.
        ledger = AutoActionProposalLedger()
        bridge = bridge_mod.InvariantDriftAutoActionBridge(ledger=ledger)
        snapshot = msd.to_bridge_snapshot(report)
        with patch.object(bridge_mod, "bridge_enabled", return_value=True):
            bridge.emit(snapshot, records)
        stats = bridge.stats()
        assert stats["emit_count_total"] == 1
        assert stats["emit_count_appended"] >= 1
        assert stats["emit_count_failed_construction"] == 0


# ---------------------------------------------------------------------------
# 5. never-raises
# ---------------------------------------------------------------------------


class TestNeverRaises:
    def test_detect_with_broken_registry_never_raises(self, master_on):
        class Broken:
            def get_spec(self, name):
                raise RuntimeError("boom")

        report = msd.detect_spec_drift(
            spec_text="`JARVIS_A_ENABLED` default-TRUE",
            registry=Broken(),
        )
        assert isinstance(report, msd.SpecDriftReport)
        assert report.records == ()

    def test_converter_on_garbage_never_raises(self):
        assert msd.to_invariant_drift_records(None) == ()  # type: ignore[arg-type]

    def test_bridge_snapshot_on_garbage_never_raises(self):
        snap = msd.to_bridge_snapshot(None)  # type: ignore[arg-type]
        assert snap is not None


# ---------------------------------------------------------------------------
# 6. AST pins
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    def _pins(self):
        return msd.register_shipped_invariants()

    def _run(self, name, source):
        tree = ast.parse(source)
        for inv in self._pins():
            if inv.invariant_name == name:
                return inv.validate(tree, source)
        raise AssertionError(f"invariant {name} not registered")

    def test_canonical_source_all_pass(self):
        src = Path(
            "backend/core/ouroboros/governance/mirror_self_spec_drift.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for inv in self._pins():
            assert inv.validate(tree, src) == (), inv.invariant_name

    def test_verdict_taxonomy_regression(self):
        bad = (
            "import enum\n"
            "class SpecDriftVerdict(str, enum.Enum):\n"
            "    ALIGNED = 'aligned'\n"
            "    DRIFTED = 'drifted'\n"
            "    EXTRA = 'extra'\n"
        )
        assert self._run("spec_drift_verdict_taxonomy_closed", bad)

    def test_authority_asymmetry_regression(self):
        bad = (
            "from backend.core.ouroboros.governance.orchestrator "
            "import Orchestrator\n"
        )
        assert self._run("spec_drift_authority_asymmetry", bad)

    def test_master_default_false_regression(self):
        bad = (
            "def master_enabled():\n"
            "    return _flag('X', default=True)\n"
        )
        assert self._run("spec_drift_master_default_false", bad)

    def test_composes_canonical_regression(self):
        bad = "x = 1  # references nothing canonical\n"
        assert self._run("spec_drift_composes_canonical", bad)


# ---------------------------------------------------------------------------
# 7. FlagRegistry seed
# ---------------------------------------------------------------------------


class TestFlagSeed:
    def test_register_flags_count(self):
        reg = FlagRegistry()
        count = msd.register_flags(reg)
        assert count >= 1
        spec = reg.get_spec(_MASTER)
        assert spec is not None
        assert spec.type is FlagType.BOOL
        assert spec.default is False


# ---------------------------------------------------------------------------
# 8. LIVE — real CLAUDE.md × real populated registry
# ---------------------------------------------------------------------------


class TestLive:
    def test_against_real_claude_md_and_registry(self, master_on):
        from backend.core.ouroboros.governance.flag_registry import (
            ensure_seeded,
        )

        registry = ensure_seeded()
        report = msd.detect_spec_drift(registry=registry)
        # Must not raise; verdict is a valid enum member.
        assert isinstance(report.verdict, msd.SpecDriftVerdict)
        # Surface what it found for the operator-facing report.
        print(
            f"\n[LIVE spec-drift] verdict={report.verdict.value} "
            f"claims={report.claims_evaluated} "
            f"drift_records={len(report.records)} "
            f"unregistered={report.unregistered_count}"
        )
        for rec in report.records:
            print(
                f"  DRIFT {rec.flag}: CLAUDE.md claims "
                f"{rec.claimed_default} but registry has "
                f"{rec.actual_default} (source={rec.source_file}, "
                f"severity={rec.severity.value})"
            )
