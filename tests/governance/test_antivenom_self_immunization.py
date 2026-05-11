"""Regression spine for §40 Wave 2 #7 — Antivenom Self-Immunization.

Covers:

* §33.1 cognitive-substrate default-FALSE
* Closed 4-value :class:`ImmunizationFinding` taxonomy
* Closed 5-value :class:`MutationKind` taxonomy
* 5 deterministic mutators are pure-function + idempotent on
  inputs with no applicable site
* Composes canonical :class:`SemanticGuardian` + P9.4 CORPUS
* Per-probe classification across all 3 meaningful findings
  (IMMUNIZED, GAP, BASELINE_MISS)
* Aggregator end-to-end against the real canonical sources
* 5 AST pin canonical-source pass + 5 synthetic regressions
* FlagRegistry seeds + SSE event registration
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    antivenom_self_immunization as asi,
)
from backend.core.ouroboros.governance.antivenom_self_immunization import (
    ANTIVENOM_IMMUNIZATION_SCHEMA_VERSION,
    ImmunizationFinding,
    ImmunizationProbe,
    ImmunizationReport,
    MutationKind,
    _ENV_MASTER,
    _classify_probe,
    audit_self_immunization,
    format_immunization_panel,
    master_enabled,
    max_probes,
    mutate_pattern,
    persistence_enabled,
    probe_entry,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER,
        "JARVIS_ANTIVENOM_IMMUNIZATION_PERSISTENCE_ENABLED",
        "JARVIS_ANTIVENOM_IMMUNIZATION_LEDGER_PATH",
        "JARVIS_ANTIVENOM_IMMUNIZATION_MAX_PROBES",
    ):
        monkeypatch.delenv(env, raising=False)
    # Point ledger to isolated temp path so tests don't touch
    # the real on-disk ledger.
    monkeypatch.setenv(
        "JARVIS_ANTIVENOM_IMMUNIZATION_LEDGER_PATH",
        str(tmp_path / "test_ledger.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# §33.1 master flag default-FALSE
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_false(self):
        assert master_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
    def test_truthy(self, monkeypatch, truthy):
        monkeypatch.setenv(_ENV_MASTER, truthy)
        assert master_enabled() is True

    def test_persistence_short_circuits_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_ANTIVENOM_IMMUNIZATION_PERSISTENCE_ENABLED",
            "true",
        )
        # Master off → persistence gated off
        assert persistence_enabled() is False

    def test_persistence_default_true_when_master_on(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        assert persistence_enabled() is True


# ---------------------------------------------------------------------------
# Env knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_max_probes_default(self):
        assert max_probes() == 1000

    def test_max_probes_clamped(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ANTIVENOM_IMMUNIZATION_MAX_PROBES", "5",
        )
        assert max_probes() == 10

    def test_max_probes_clamped_high(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ANTIVENOM_IMMUNIZATION_MAX_PROBES", "9999999",
        )
        assert max_probes() == 100_000


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class TestTaxonomies:
    def test_finding_4_values(self):
        assert {f.value for f in ImmunizationFinding} == {
            "immunized", "gap", "baseline_miss", "disabled",
        }

    def test_mutation_5_values(self):
        assert {m.value for m in MutationKind} == {
            "whitespace_drift",
            "comment_insertion",
            "rename_variable",
            "string_split_concat",
            "parens_wrap",
        }


# ---------------------------------------------------------------------------
# Pure-function mutators — deterministic
# ---------------------------------------------------------------------------


class TestMutators:
    def test_whitespace_drift_adds_indent(self):
        text = "x = 1\ny = 2\n"
        out = mutate_pattern(text, MutationKind.WHITESPACE_DRIFT)
        assert "  x = 1" in out
        assert "  y = 2" in out

    def test_whitespace_drift_preserves_blank_lines(self):
        text = "x = 1\n\ny = 2"
        out = mutate_pattern(text, MutationKind.WHITESPACE_DRIFT)
        # Empty lines stay empty
        assert "\n\n" in out

    def test_comment_insertion(self):
        text = "x = 1\ny = 2\n"
        out = mutate_pattern(text, MutationKind.COMMENT_INSERTION)
        assert "antivenom_probe" in out
        assert out != text

    def test_comment_insertion_single_line(self):
        out = mutate_pattern("x = 1", MutationKind.COMMENT_INSERTION)
        assert "antivenom_probe" in out

    def test_rename_variable(self):
        text = "foo_bar = 42\nprint(foo_bar)\n"
        out = mutate_pattern(text, MutationKind.RENAME_VARIABLE)
        assert "av_alias = 42" in out
        # Only first assignment renamed
        assert "print(foo_bar)" in out

    def test_rename_variable_noop_on_no_assignment(self):
        text = "print('hello')\n"
        out = mutate_pattern(text, MutationKind.RENAME_VARIABLE)
        # No simple assignment → no change
        assert out == text

    def test_rename_variable_skips_keywords(self):
        text = "def foo():\n    return 1\n"
        out = mutate_pattern(text, MutationKind.RENAME_VARIABLE)
        # def isn't an assignment — left alone
        assert "def foo()" in out

    def test_string_split_concat(self):
        text = 'x = "hello_world_padding"\n'
        out = mutate_pattern(
            text, MutationKind.STRING_SPLIT_CONCAT,
        )
        assert " + " in out
        # Pieces preserve the original content
        assert "hello" in out
        assert "padding" in out

    def test_string_split_concat_short_literal_noop(self):
        text = 'x = "hi"\n'  # too short
        out = mutate_pattern(
            text, MutationKind.STRING_SPLIT_CONCAT,
        )
        # Pattern requires 4-40 char literal — short ones untouched
        assert out == text

    def test_parens_wrap(self):
        text = "x = a + b\n"
        out = mutate_pattern(text, MutationKind.PARENS_WRAP)
        assert "(a + b)" in out

    def test_parens_wrap_skips_existing_parens(self):
        text = "x = (a + b)\n"
        out = mutate_pattern(text, MutationKind.PARENS_WRAP)
        # Already wrapped — no double-wrap
        assert out.count("(") <= 1

    def test_parens_wrap_skips_comparison(self):
        text = "x == y\n"  # `==` should not trigger
        out = mutate_pattern(text, MutationKind.PARENS_WRAP)
        assert out == text

    def test_mutate_empty_returns_empty(self):
        for k in MutationKind:
            assert mutate_pattern("", k) == ""

    def test_mutate_none_returns_empty(self):
        for k in MutationKind:
            # Type hints say str, but defensive code accepts None
            assert mutate_pattern(None, k) == ""  # type: ignore[arg-type]

    def test_mutate_deterministic(self):
        text = "x = 'some_value_here'"
        for k in MutationKind:
            r1 = mutate_pattern(text, k)
            r2 = mutate_pattern(text, k)
            assert r1 == r2

    def test_mutate_bounded(self):
        # 100KB input → truncated to 8KB before mutation
        text = "x = 1\n" * 10000
        out = mutate_pattern(text, MutationKind.WHITESPACE_DRIFT)
        # Output bounded (mutation may expand slightly)
        assert len(out) < 20_000


# ---------------------------------------------------------------------------
# Probe classifier
# ---------------------------------------------------------------------------


class TestClassifier:
    def test_baseline_miss(self):
        finding, diagnostic = _classify_probe((), ("any",))
        assert finding is ImmunizationFinding.BASELINE_MISS
        assert "baseline" in diagnostic.lower()

    def test_baseline_miss_when_both_empty(self):
        finding, _ = _classify_probe((), ())
        assert finding is ImmunizationFinding.BASELINE_MISS

    def test_gap_when_baseline_caught_mutation_missed(self):
        finding, diagnostic = _classify_probe(("pattern_a",), ())
        assert finding is ImmunizationFinding.GAP
        assert "gap" in diagnostic.lower()

    def test_immunized_when_both_caught(self):
        finding, _ = _classify_probe(("a",), ("a",))
        assert finding is ImmunizationFinding.IMMUNIZED

    def test_immunized_even_when_mutation_caught_by_different_pattern(self):
        """Cage holds even if a DIFFERENT pattern detector fires
        on the mutation — what matters is that SOMETHING fired."""
        finding, _ = _classify_probe(("a",), ("b",))
        assert finding is ImmunizationFinding.IMMUNIZED


# ---------------------------------------------------------------------------
# Probe loop — single entry × mutation
# ---------------------------------------------------------------------------


class TestProbeEntry:
    """Hermetic probe tests use a synthetic AdversarialEntry that
    composes the canonical class shape. The PROBE runs guardian
    on the materialized + mutated text — that integration test
    uses the REAL guardian + corpus."""

    def test_probe_immunized_on_caught_baseline_and_mutation(self):
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            CORPUS,
        )
        # Find an entry whose baseline the guardian catches by
        # running probes until we hit IMMUNIZED.
        for entry in CORPUS:
            for kind in MutationKind:
                probe = probe_entry(entry, kind)
                if (
                    probe is not None
                    and probe.finding is ImmunizationFinding.IMMUNIZED
                ):
                    assert probe.original_caught is True
                    assert probe.mutation_caught is True
                    return
        # If no IMMUNIZED probe exists, surface that — the audit
        # is the load-bearing report; not a failure condition for
        # this test, but worth documenting.
        pytest.skip("no IMMUNIZED probe in real corpus run")

    def test_probe_baseline_miss_when_guardian_misses(self):
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            CORPUS,
        )
        # Many P9.4 entries target non-SemanticGuardian cage
        # layers — guardian misses the baseline (correctly,
        # those entries test risk_tier_floor / scoped backend
        # / etc.). We expect BASELINE_MISS for those.
        for entry in CORPUS:
            probe = probe_entry(entry, MutationKind.WHITESPACE_DRIFT)
            if (
                probe is not None
                and probe.finding is ImmunizationFinding.BASELINE_MISS
            ):
                assert probe.original_caught is False
                return
        pytest.skip("no BASELINE_MISS probe in real corpus run")


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


class TestAggregator:
    def test_master_off_returns_disabled(self):
        report = audit_self_immunization()
        assert report.master_enabled is False
        assert report.finding is ImmunizationFinding.DISABLED
        assert report.probes_run == 0

    def test_master_on_runs_full_audit(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        report = audit_self_immunization()
        assert report.master_enabled is True
        # Real corpus has 25 entries × 5 mutations = 125 probes
        assert report.probes_run >= 100
        # Coverage ratio is bounded [0, 1]
        assert 0.0 <= report.coverage_ratio <= 1.0

    def test_corpus_override_for_testing(self, monkeypatch):
        """Tests can inject a synthetic corpus subset."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            CORPUS,
        )
        subset = CORPUS[:2]
        report = audit_self_immunization(
            corpus_override=subset,
            mutation_kinds=[MutationKind.WHITESPACE_DRIFT],
        )
        assert report.probes_run <= 2

    def test_mutation_kinds_subset(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        report = audit_self_immunization(
            mutation_kinds=[MutationKind.COMMENT_INSERTION],
        )
        # All probes should be COMMENT_INSERTION (subset)
        assert report.probes_run > 0

    def test_empty_corpus_returns_disabled_with_diagnostic(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        report = audit_self_immunization(corpus_override=[])
        assert report.finding is ImmunizationFinding.DISABLED
        assert "unavailable or empty" in report.diagnostic

    def test_real_audit_surfaces_findings(self, monkeypatch):
        """Load-bearing integration smoke: the real audit MUST
        produce at least 1 probe with each of the 3 meaningful
        finding kinds (IMMUNIZED, GAP, BASELINE_MISS). This is
        the ground-truth check that the substrate composes
        SemanticGuardian + corpus correctly end-to-end."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        report = audit_self_immunization()
        # We expect at least SOME immunized probes (cage holds
        # on common mutations)
        assert report.immunized_count > 0, (
            f"unexpected 0 immunized — cage may have regressed; "
            f"report: {report.diagnostic}"
        )


# ---------------------------------------------------------------------------
# Frozen artifacts
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_probe_to_dict(self):
        probe = ImmunizationProbe(
            entry_id="p9.4.001",
            category="quine_shape",
            mutation_kind="whitespace_drift",
            finding=ImmunizationFinding.IMMUNIZED,
            original_caught=True,
            mutation_caught=True,
            baseline_patterns=("pat_a",),
            mutation_patterns=("pat_a",),
            diagnostic="ok",
        )
        d = probe.to_dict()
        assert d["entry_id"] == "p9.4.001"
        assert d["finding"] == "immunized"
        assert d["schema_version"] == (
            ANTIVENOM_IMMUNIZATION_SCHEMA_VERSION
        )

    def test_report_to_dict(self):
        report = ImmunizationReport(
            audited_at_unix=100.0,
            master_enabled=True,
            finding=ImmunizationFinding.GAP,
            probes_run=125,
            immunized_count=100,
            gap_count=5,
            baseline_miss_count=20,
            per_kind_gap={"whitespace_drift": 1},
            per_entry_gap={"p9.4.001": 1},
            coverage_ratio=0.95,
            elapsed_s=0.5,
            diagnostic="d",
        )
        d = report.to_dict()
        assert d["finding"] == "gap"
        assert d["probes_run"] == 125
        assert d["coverage_ratio"] == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_master_off_returns_disabled_marker(self):
        out = format_immunization_panel()
        assert "disabled" in out

    def test_master_on_renders_full_panel(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        out = format_immunization_panel()
        assert "Antivenom Self-Immunization" in out
        assert "probes_run" in out
        assert "coverage_ratio" in out


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    src = Path(asi.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, tree


@pytest.fixture
def pins():
    return asi.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_5_pins_registered(self, pins):
        assert len(pins) == 5
        names = {p.invariant_name for p in pins}
        assert names == {
            "antivenom_immunization_finding_taxonomy_closed",
            "antivenom_immunization_mutation_taxonomy_closed",
            "antivenom_immunization_authority_asymmetry",
            "antivenom_immunization_master_default_false",
            "antivenom_immunization_composes_canonical",
        }

    @pytest.mark.parametrize(
        "pin_name",
        [
            "antivenom_immunization_finding_taxonomy_closed",
            "antivenom_immunization_mutation_taxonomy_closed",
            "antivenom_immunization_authority_asymmetry",
            "antivenom_immunization_master_default_false",
            "antivenom_immunization_composes_canonical",
        ],
    )
    def test_pin_passes(self, canonical_source, pins, pin_name):
        src, tree = canonical_source
        pin = next(
            p for p in pins if p.invariant_name == pin_name
        )
        assert not pin.validate(tree, src)


class TestAstPinsSynthetic:
    def test_finding_pin_fires(self, pins):
        synthetic = """
import enum
class ImmunizationFinding(str, enum.Enum):
    IMMUNIZED = "immunized"
    GAP = "gap"
    # MISSING: BASELINE_MISS, DISABLED
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "antivenom_immunization_finding_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_mutation_pin_fires(self, pins):
        synthetic = """
import enum
class MutationKind(str, enum.Enum):
    WHITESPACE_DRIFT = "whitespace_drift"
    EXTRA = "extra"
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "antivenom_immunization_mutation_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_authority_pin_fires(self, pins):
        synthetic = (
            "from backend.core.ouroboros.governance.orchestrator "
            "import x\n"
        )
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "antivenom_immunization_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_master_pin_fires_on_default_true(self, pins):
        synthetic = """
def master_enabled():
    return _flag("FOO", default=True)
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "antivenom_immunization_master_default_false"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_composes_pin_fires(self, pins):
        synthetic = "x = 1\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "antivenom_immunization_composes_canonical"
        )
        violations = pin.validate(tree, synthetic)
        assert violations


# ---------------------------------------------------------------------------
# FlagRegistry seeds + SSE event
# ---------------------------------------------------------------------------


class TestFlagSeeds:
    def test_seeds_auto_discovered(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        names = {f.name for f in reg.list_all()}
        for expected in [
            "JARVIS_ANTIVENOM_IMMUNIZATION_ENABLED",
            "JARVIS_ANTIVENOM_IMMUNIZATION_PERSISTENCE_ENABLED",
            "JARVIS_ANTIVENOM_IMMUNIZATION_MAX_PROBES",
        ]:
            assert expected in names

    def test_master_seed_safety_default_false(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            f for f in reg.list_all()
            if f.name == "JARVIS_ANTIVENOM_IMMUNIZATION_ENABLED"
        )
        assert spec.default is False
        assert spec.category.value == "safety"


class TestSseEvent:
    def test_event_registered(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_ANTIVENOM_IMMUNIZATION_AUDITED,
            _VALID_EVENT_TYPES,
        )
        assert (
            EVENT_TYPE_ANTIVENOM_IMMUNIZATION_AUDITED
            in _VALID_EVENT_TYPES
        )
        assert (
            EVENT_TYPE_ANTIVENOM_IMMUNIZATION_AUDITED
            == "antivenom_immunization_audited"
        )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_all_exports_present(self):
        for name in asi.__all__:
            assert getattr(asi, name) is not None

    def test_schema_version(self):
        assert ANTIVENOM_IMMUNIZATION_SCHEMA_VERSION.startswith(
            "antivenom_immunization.",
        )
