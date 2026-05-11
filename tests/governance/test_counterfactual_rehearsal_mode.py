"""Regression spine for §40 Wave 3 #7 — Counterfactual Rehearsal Mode.

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 4-value :class:`RehearsalVerdict` taxonomy
* Closed 4-value :class:`RehearsalIntensity` taxonomy
* Path normalization composes Wave 2 #5 ``_normalize_path``
* Composes ``governance_boundary_gate.is_boundary_crossed``
* Composes ``postmortem_recall.gather_recent_postmortems``
* Every reachable verdict (CLEAN / CONCERN_RAISED / ESCALATE /
  DISABLED) + intensity (SKIP / LIGHTWEIGHT / HEAVYWEIGHT /
  DISABLED)
* File overlap detection (intersection semantics)
* 5 AST pin canonical-source pass + 5 synthetic regressions
* FlagRegistry seeds auto-discovered
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Tuple

import pytest


from backend.core.ouroboros.governance import (
    counterfactual_rehearsal_mode as crm,
)
from backend.core.ouroboros.governance.counterfactual_rehearsal_mode import (
    COUNTERFACTUAL_REHEARSAL_SCHEMA_VERSION,
    RehearsalConcern,
    RehearsalIntensity,
    RehearsalReport,
    RehearsalVerdict,
    _ENV_MASTER,
    _file_overlap,
    _normalize_target_files,
    concern_threshold,
    evaluate_rehearsal,
    format_rehearsal_panel,
    master_enabled,
    max_postmortems_to_match,
    verdict_glyph,
)


# ---------------------------------------------------------------------------
# Fake PostmortemRecord — minimal duck-typed for hermetic tests
# ---------------------------------------------------------------------------


@dataclass
class _FakePostmortemRecord:
    op_id: str = "op-fake"
    session_id: str = "bt-test"
    failed_phase: str = "GENERATE"
    root_cause: str = "synthetic test failure"
    target_files: Tuple[str, ...] = field(default_factory=tuple)
    timestamp_iso: str = "2026-05-10T00:00:00"


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for env in (
        _ENV_MASTER,
        "JARVIS_COUNTERFACTUAL_REHEARSAL_MAX_POSTMORTEMS",
        "JARVIS_COUNTERFACTUAL_REHEARSAL_CONCERN_THRESHOLD",
    ):
        monkeypatch.delenv(env, raising=False)
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

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off", ""],
    )
    def test_falsy(self, monkeypatch, falsy):
        monkeypatch.setenv(_ENV_MASTER, falsy)
        assert master_enabled() is False


# ---------------------------------------------------------------------------
# Env knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_max_postmortems_default(self):
        assert max_postmortems_to_match() == 50

    def test_max_postmortems_clamped_low(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_COUNTERFACTUAL_REHEARSAL_MAX_POSTMORTEMS", "0",
        )
        assert max_postmortems_to_match() == 1

    def test_max_postmortems_clamped_high(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_COUNTERFACTUAL_REHEARSAL_MAX_POSTMORTEMS",
            "999999999",
        )
        assert max_postmortems_to_match() == 10_000

    def test_concern_threshold_default(self):
        assert concern_threshold() == 1

    def test_concern_threshold_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_COUNTERFACTUAL_REHEARSAL_CONCERN_THRESHOLD",
            "5",
        )
        assert concern_threshold() == 5


# ---------------------------------------------------------------------------
# Closed taxonomies
# ---------------------------------------------------------------------------


class TestTaxonomies:
    def test_verdict_4_values(self):
        assert {v.value for v in RehearsalVerdict} == {
            "clean", "concern_raised", "escalate", "disabled",
        }

    def test_intensity_4_values(self):
        assert {i.value for i in RehearsalIntensity} == {
            "skip", "lightweight", "heavyweight", "disabled",
        }

    def test_glyph_covers_all_verdicts(self):
        for v in RehearsalVerdict:
            assert verdict_glyph(v) != "?"

    def test_glyph_unknown_returns_question(self):
        assert verdict_glyph("bogus") == "?"
        assert verdict_glyph(None) == "?"


# ---------------------------------------------------------------------------
# §33.5 frozen artifacts
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_concern_to_dict(self):
        c = RehearsalConcern(
            op_id="op-x",
            session_id="bt-1",
            failed_phase="APPLY",
            root_cause="reason",
            overlapping_files=("a.py",),
            timestamp_iso="2026-01-01T00:00:00",
        )
        d = c.to_dict()
        assert d["op_id"] == "op-x"
        assert d["overlapping_files"] == ["a.py"]
        assert d["schema_version"] == (
            COUNTERFACTUAL_REHEARSAL_SCHEMA_VERSION
        )

    def test_report_to_dict(self):
        r = RehearsalReport(
            evaluated_at_unix=100.0,
            master_enabled=True,
            verdict=RehearsalVerdict.CLEAN,
            intensity=RehearsalIntensity.LIGHTWEIGHT,
            candidate_target_files=("a.py",),
            postmortems_scanned=10,
            concerns=(),
            boundary_crossed=False,
            diagnostic="ok",
            elapsed_s=0.01,
        )
        d = r.to_dict()
        expected = {
            "evaluated_at_unix", "master_enabled", "verdict",
            "intensity", "candidate_target_files",
            "postmortems_scanned", "concerns",
            "boundary_crossed", "diagnostic", "elapsed_s",
            "schema_version",
        }
        assert set(d.keys()) == expected
        assert d["verdict"] == "clean"
        assert d["intensity"] == "lightweight"

    def test_concerns_bounded(self, monkeypatch):
        """100 overlapping postmortems must clamp to 32 entries."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        pms = [
            _FakePostmortemRecord(
                op_id=f"op-{i}",
                session_id=f"bt-{i}",
                target_files=("shared.py",),
            )
            for i in range(100)
        ]
        report = evaluate_rehearsal(
            ["shared.py"], postmortem_records=pms,
        )
        assert report.verdict is RehearsalVerdict.CONCERN_RAISED
        # Bounded at 32 entries
        assert len(report.concerns) == 32
        assert report.postmortems_scanned == 100


# ---------------------------------------------------------------------------
# Path normalization composition
# ---------------------------------------------------------------------------


class TestPathNormalization:
    def test_normalize_empty(self):
        assert _normalize_target_files([]) == ()
        assert _normalize_target_files(None) == ()

    def test_normalize_backslashes(self):
        result = _normalize_target_files(["a\\b\\c.py"])
        assert result == ("a/b/c.py",)

    def test_normalize_mixed_types(self):
        result = _normalize_target_files([
            "str.py",
            Path("path.py"),
            None,  # filtered out
            "",    # filtered out
        ])
        assert "str.py" in result
        assert "path.py" in result


# ---------------------------------------------------------------------------
# File overlap detection
# ---------------------------------------------------------------------------


class TestFileOverlap:
    def test_empty_candidate_no_overlap(self):
        assert _file_overlap(frozenset(), ["a.py"]) == ()

    def test_empty_postmortem_no_overlap(self):
        assert _file_overlap(frozenset({"a.py"}), []) == ()

    def test_intersection_detected(self):
        result = _file_overlap(
            frozenset({"a.py", "b.py"}),
            ["a.py", "c.py"],
        )
        assert result == ("a.py",)

    def test_dedup_postmortem_repeats(self):
        result = _file_overlap(
            frozenset({"a.py"}),
            ["a.py", "a.py", "a.py"],
        )
        assert result == ("a.py",)

    def test_sorted_deterministic(self):
        result = _file_overlap(
            frozenset({"a.py", "b.py", "c.py"}),
            ["c.py", "a.py", "b.py"],
        )
        assert result == ("a.py", "b.py", "c.py")


# ---------------------------------------------------------------------------
# Verdict cascade — every reachable verdict
# ---------------------------------------------------------------------------


class TestVerdictCascade:
    def test_master_off_returns_disabled(self):
        r = evaluate_rehearsal(["a.py"])
        assert r.verdict is RehearsalVerdict.DISABLED
        assert r.intensity is RehearsalIntensity.DISABLED

    def test_master_on_empty_target_returns_clean_skip(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = evaluate_rehearsal([])
        assert r.verdict is RehearsalVerdict.CLEAN
        assert r.intensity is RehearsalIntensity.SKIP

    def test_master_on_none_target_returns_clean_skip(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = evaluate_rehearsal(None)
        assert r.verdict is RehearsalVerdict.CLEAN
        assert r.intensity is RehearsalIntensity.SKIP

    def test_downstream_clean_no_overlap(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = evaluate_rehearsal(
            ["frontend/app.tsx"],
            postmortem_records=[
                _FakePostmortemRecord(
                    target_files=("other.py",),
                ),
            ],
        )
        assert r.verdict is RehearsalVerdict.CLEAN
        assert r.intensity is RehearsalIntensity.LIGHTWEIGHT
        assert r.boundary_crossed is False

    def test_downstream_concern_raised_on_overlap(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = evaluate_rehearsal(
            ["frontend/app.tsx"],
            postmortem_records=[
                _FakePostmortemRecord(
                    target_files=("frontend/app.tsx",),
                ),
            ],
        )
        assert r.verdict is RehearsalVerdict.CONCERN_RAISED
        assert r.intensity is RehearsalIntensity.LIGHTWEIGHT
        assert len(r.concerns) == 1
        assert "frontend/app.tsx" in r.concerns[0].overlapping_files

    def test_cage_change_routes_escalate_even_without_overlap(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = evaluate_rehearsal(
            ["backend/core/ouroboros/governance/orchestrator.py"],
            postmortem_records=[],
        )
        assert r.verdict is RehearsalVerdict.ESCALATE
        assert r.intensity is RehearsalIntensity.HEAVYWEIGHT
        assert r.boundary_crossed is True

    def test_cage_change_routes_escalate_with_overlap(
        self, monkeypatch,
    ):
        """Even when postmortems overlap, cage changes route
        ESCALATE — boundary crossing wins."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        cage_path = (
            "backend/core/ouroboros/governance/iron_gate.py"
        )
        r = evaluate_rehearsal(
            [cage_path],
            postmortem_records=[
                _FakePostmortemRecord(
                    target_files=(cage_path,),
                ),
            ],
        )
        assert r.verdict is RehearsalVerdict.ESCALATE
        assert r.intensity is RehearsalIntensity.HEAVYWEIGHT
        assert r.boundary_crossed is True
        # Concerns still reported in detail
        assert len(r.concerns) == 1

    def test_concern_threshold_env_override(self, monkeypatch):
        """concern_threshold=3 means 2 overlaps → CLEAN, 3 → CONCERN."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        monkeypatch.setenv(
            "JARVIS_COUNTERFACTUAL_REHEARSAL_CONCERN_THRESHOLD",
            "3",
        )
        pms_2 = [
            _FakePostmortemRecord(
                op_id=f"op-{i}",
                target_files=("a.py",),
            )
            for i in range(2)
        ]
        r = evaluate_rehearsal(
            ["a.py"], postmortem_records=pms_2,
        )
        # 2 matches < 3 threshold → CLEAN
        assert r.verdict is RehearsalVerdict.CLEAN

        pms_3 = pms_2 + [
            _FakePostmortemRecord(
                op_id="op-3", target_files=("a.py",),
            ),
        ]
        r = evaluate_rehearsal(
            ["a.py"], postmortem_records=pms_3,
        )
        # 3 matches == threshold → CONCERN_RAISED
        assert r.verdict is RehearsalVerdict.CONCERN_RAISED


# ---------------------------------------------------------------------------
# Defensive behavior — NEVER raises
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_malformed_postmortem_skipped(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        class _Bad:
            @property
            def target_files(self):
                raise RuntimeError("broken record")
        r = evaluate_rehearsal(
            ["a.py"],
            postmortem_records=[
                _Bad(),
                _FakePostmortemRecord(target_files=("a.py",)),
            ],
        )
        # Broken record skipped; good one matched
        assert r.verdict is RehearsalVerdict.CONCERN_RAISED
        assert len(r.concerns) == 1


# ---------------------------------------------------------------------------
# Real-source composition smoke
# ---------------------------------------------------------------------------


class TestCanonicalComposition:
    def test_composes_real_postmortems(self, monkeypatch):
        """Load-bearing smoke: with master on, no caller-injected
        postmortems, the substrate composes the canonical
        gather_recent_postmortems walker."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = evaluate_rehearsal(["frontend/app.tsx"])
        # Real run — postmortems_scanned reflects on-disk state
        # (may be 0 if .ouroboros/sessions doesn't exist in the
        # test repo; we only assert structural correctness)
        assert r.master_enabled is True
        assert r.intensity is RehearsalIntensity.LIGHTWEIGHT

    def test_real_governance_path_escalates(self, monkeypatch):
        """Composes the real Wave 2 #5 governance_boundary_gate
        to detect cage paths."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = evaluate_rehearsal([
            "backend/core/ouroboros/governance/orchestrator.py",
        ])
        assert r.verdict is RehearsalVerdict.ESCALATE
        assert r.boundary_crossed is True


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_master_off_disabled_marker(self):
        out = format_rehearsal_panel(
            candidate_target_files=["a.py"],
        )
        assert "disabled" in out

    def test_master_on_renders_panel(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        out = format_rehearsal_panel(
            candidate_target_files=["a.py"],
        )
        assert "Counterfactual Rehearsal" in out

    def test_concern_concerns_render_top_n(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        pms = [
            _FakePostmortemRecord(
                op_id=f"op-{i}",
                session_id=f"bt-session-{i}",
                target_files=("a.py",),
            )
            for i in range(10)
        ]
        report = evaluate_rehearsal(
            ["a.py"], postmortem_records=pms,
        )
        out = format_rehearsal_panel(report)
        # Top-5 shown + "..." marker
        assert "concern_raised" in out
        assert "+5 more" in out


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    src = Path(crm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, tree


@pytest.fixture
def pins():
    return crm.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_5_pins_registered(self, pins):
        assert len(pins) == 5
        names = {p.invariant_name for p in pins}
        assert names == {
            "counterfactual_rehearsal_verdict_taxonomy_closed",
            "counterfactual_rehearsal_intensity_taxonomy_closed",
            "counterfactual_rehearsal_authority_asymmetry",
            "counterfactual_rehearsal_master_default_false",
            "counterfactual_rehearsal_composes_canonical",
        }

    @pytest.mark.parametrize(
        "pin_name",
        [
            "counterfactual_rehearsal_verdict_taxonomy_closed",
            "counterfactual_rehearsal_intensity_taxonomy_closed",
            "counterfactual_rehearsal_authority_asymmetry",
            "counterfactual_rehearsal_master_default_false",
            "counterfactual_rehearsal_composes_canonical",
        ],
    )
    def test_pin_passes(self, canonical_source, pins, pin_name):
        src, tree = canonical_source
        pin = next(
            p for p in pins if p.invariant_name == pin_name
        )
        assert not pin.validate(tree, src)


class TestAstPinsSynthetic:
    def test_verdict_pin_fires(self, pins):
        synthetic = """
import enum
class RehearsalVerdict(str, enum.Enum):
    CLEAN = "clean"
    # MISSING: concern_raised, escalate, disabled
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "counterfactual_rehearsal_verdict_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_intensity_pin_fires(self, pins):
        synthetic = """
import enum
class RehearsalIntensity(str, enum.Enum):
    SKIP = "skip"
    LIGHTWEIGHT = "lightweight"
    HEAVYWEIGHT = "heavyweight"
    DISABLED = "disabled"
    EXTRA = "extra"
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "counterfactual_rehearsal_intensity_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "drift" in violations[0]

    def test_authority_pin_fires(self, pins):
        synthetic = (
            "from backend.core.ouroboros.governance.orchestrator "
            "import x\n"
        )
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "counterfactual_rehearsal_authority_asymmetry"
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
            == "counterfactual_rehearsal_master_default_false"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_composes_pin_fires(self, pins):
        synthetic = "x = 1\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "counterfactual_rehearsal_composes_canonical"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert any(
            "governance_boundary_gate" in v for v in violations
        )
        assert any(
            "gather_recent_postmortems" in v for v in violations
        )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
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
            "JARVIS_COUNTERFACTUAL_REHEARSAL_ENABLED",
            "JARVIS_COUNTERFACTUAL_REHEARSAL_MAX_POSTMORTEMS",
            "JARVIS_COUNTERFACTUAL_REHEARSAL_CONCERN_THRESHOLD",
        ]:
            assert expected in names

    def test_master_safety_default_false(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            f for f in reg.list_all()
            if f.name == "JARVIS_COUNTERFACTUAL_REHEARSAL_ENABLED"
        )
        assert spec.default is False
        assert spec.category.value == "safety"


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_all_exports_present(self):
        for name in crm.__all__:
            assert getattr(crm, name) is not None

    def test_schema_version(self):
        assert COUNTERFACTUAL_REHEARSAL_SCHEMA_VERSION.startswith(
            "counterfactual_rehearsal.",
        )


# ---------------------------------------------------------------------------
# Public accessor in postmortem_recall (Singleton+Read-API extension)
# ---------------------------------------------------------------------------


class TestPostmortemAccessor:
    def test_gather_recent_postmortems_public(self):
        """The new public accessor in postmortem_recall.py."""
        from backend.core.ouroboros.governance.postmortem_recall import (  # noqa: E501
            gather_recent_postmortems,
        )
        rs = gather_recent_postmortems(max_total=3)
        assert isinstance(rs, list)

    def test_in_postmortem_recall_all(self):
        from backend.core.ouroboros.governance import (
            postmortem_recall,
        )
        assert (
            "gather_recent_postmortems" in postmortem_recall.__all__
        )
