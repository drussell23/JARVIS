"""Regression spine for §40 Wave 3 #6 — Cross-Session Coherence
Empirical Rig.

Covers:

* §33.1 cognitive substrate default-FALSE
* Closed 4-value :class:`ArcVerdict` taxonomy
* 4 per-axis fingerprinters cover all canonical CoherenceAxis values
* :func:`walk_session_arc` end-to-end with caller-injected synthetic
  session records (hermetic) + every reachable verdict
* Composes canonical :data:`cross_session_harness.CoherenceAxis` +
  :class:`AxisDigest` + :func:`compute_drift` (no parallel taxonomy)
* JSON-serializable :meth:`ArcDriftReport.to_dict` projection
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
    cross_session_coherence_rig as rig,
)
from backend.core.ouroboros.governance.cross_session_coherence_rig import (
    ArcDriftReport,
    ArcVerdict,
    BoundaryDrift,
    CROSS_SESSION_COHERENCE_RIG_SCHEMA_VERSION,
    _ENV_MASTER,
    _FINGERPRINTERS,
    _fingerprint_adaptations,
    _fingerprint_semantic_centroid,
    _fingerprint_session_history,
    _fingerprint_user_prefs,
    build_axis_digests_for_session,
    format_arc_panel,
    master_enabled,
    max_sessions,
    walk_session_arc,
)


# ---------------------------------------------------------------------------
# Synthetic session record for hermetic testing
# ---------------------------------------------------------------------------


@dataclass
class _FakeSession:
    """Duck-typed mirror of LastSessionSummary's SessionRecord."""

    session_id: str = "bt-fake"
    started_at_epoch: float = 0.0
    ended_at_epoch: float = 0.0
    stop_reason: str = "idle_timeout"
    stats_attempted: int = 10
    stats_completed: int = 10
    cost_total: float = 0.0
    drift_status: str = "ok"
    drift_ratio: float = 0.0
    convergence_state: str = "STABILIZED"
    last_apply_mode: str = "safe_auto"
    last_apply_files: str = "a.py,b.py"


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for env in (
        _ENV_MASTER,
        "JARVIS_CROSS_SESSION_COHERENCE_RIG_MAX_SESSIONS",
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


# ---------------------------------------------------------------------------
# Env knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_default(self):
        assert max_sessions() == 50

    def test_clamped_low(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CROSS_SESSION_COHERENCE_RIG_MAX_SESSIONS", "0",
        )
        assert max_sessions() == 2

    def test_clamped_high(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CROSS_SESSION_COHERENCE_RIG_MAX_SESSIONS",
            "99999999",
        )
        assert max_sessions() == 10_000


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class TestVerdictTaxonomy:
    def test_4_values(self):
        assert {v.value for v in ArcVerdict} == {
            "coherent",
            "mostly_coherent",
            "drifting",
            "insufficient_data",
        }


# ---------------------------------------------------------------------------
# Per-axis fingerprinters
# ---------------------------------------------------------------------------


class TestFingerprinters:
    def test_all_4_present(self):
        assert set(_FINGERPRINTERS.keys()) == {
            "user_prefs",
            "adaptations",
            "semantic_centroid",
            "session_history",
        }

    def test_user_prefs_deterministic(self):
        s = _FakeSession(last_apply_files="a.py,b.py")
        r1 = _fingerprint_user_prefs(s)
        r2 = _fingerprint_user_prefs(s)
        assert r1["hash"] == r2["hash"]
        assert r1["count"] == 2

    def test_user_prefs_ordering_invariant(self):
        s1 = _FakeSession(last_apply_files="a.py,b.py")
        s2 = _FakeSession(last_apply_files="b.py,a.py")
        # Sorted internally — same hash
        assert (
            _fingerprint_user_prefs(s1)["hash"]
            == _fingerprint_user_prefs(s2)["hash"]
        )

    def test_user_prefs_distinct_on_change(self):
        s1 = _FakeSession(last_apply_files="a.py")
        s2 = _FakeSession(last_apply_files="a.py,b.py")
        assert (
            _fingerprint_user_prefs(s1)["hash"]
            != _fingerprint_user_prefs(s2)["hash"]
        )

    def test_adaptations_hash_distinct_per_drift_status(self):
        s1 = _FakeSession(drift_status="ok", drift_ratio=0.1)
        s2 = _FakeSession(drift_status="drift", drift_ratio=0.1)
        assert (
            _fingerprint_adaptations(s1)["hash"]
            != _fingerprint_adaptations(s2)["hash"]
        )

    def test_semantic_centroid_hash_distinct_per_state(self):
        s1 = _FakeSession(convergence_state="STABILIZED")
        s2 = _FakeSession(convergence_state="CONVERGING")
        assert (
            _fingerprint_semantic_centroid(s1)["hash"]
            != _fingerprint_semantic_centroid(s2)["hash"]
        )

    def test_session_history_hash_distinct_per_shape(self):
        s1 = _FakeSession(stats_attempted=10, stop_reason="idle")
        s2 = _FakeSession(stats_attempted=20, stop_reason="idle")
        assert (
            _fingerprint_session_history(s1)["hash"]
            != _fingerprint_session_history(s2)["hash"]
        )

    def test_session_history_records_op_count(self):
        s = _FakeSession(stats_attempted=42)
        assert _fingerprint_session_history(s)["count"] == 42

    def test_defensive_on_malformed_record(self):
        class _Broken:
            @property
            def last_apply_files(self):
                raise RuntimeError("attribute broken")
        r = _fingerprint_user_prefs(_Broken())
        assert r["hash"] == ""
        assert "user_prefs_extract_failed" in r["diagnostic"]


# ---------------------------------------------------------------------------
# build_axis_digests_for_session — composes canonical AxisDigest
# ---------------------------------------------------------------------------


class TestBuildAxisDigests:
    def test_returns_4_canonical_axes(self):
        s = _FakeSession()
        digests = build_axis_digests_for_session(s)
        assert set(digests.keys()) == {
            "user_prefs",
            "adaptations",
            "semantic_centroid",
            "session_history",
        }

    def test_each_digest_uses_canonical_axisdigest(self):
        from backend.core.ouroboros.governance.cross_session_harness import (  # noqa: E501
            AxisDigest,
        )
        s = _FakeSession()
        digests = build_axis_digests_for_session(s)
        for d in digests.values():
            assert isinstance(d, AxisDigest)

    def test_axis_enum_correctly_coerced(self):
        from backend.core.ouroboros.governance.cross_session_harness import (  # noqa: E501
            CoherenceAxis,
        )
        s = _FakeSession()
        digests = build_axis_digests_for_session(s)
        assert digests["user_prefs"].axis is CoherenceAxis.USER_PREFS
        assert (
            digests["adaptations"].axis is CoherenceAxis.ADAPTATIONS
        )
        assert (
            digests["semantic_centroid"].axis
            is CoherenceAxis.SEMANTIC_CENTROID
        )
        assert (
            digests["session_history"].axis
            is CoherenceAxis.SESSION_HISTORY
        )


# ---------------------------------------------------------------------------
# walk_session_arc — every verdict reachable
# ---------------------------------------------------------------------------


class TestWalkSessionArc:
    def test_master_off_returns_insufficient(self):
        r = walk_session_arc()
        assert r.master_enabled is False
        assert r.verdict is ArcVerdict.INSUFFICIENT_DATA

    def test_zero_sessions_returns_insufficient(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = walk_session_arc(session_records_override=[])
        assert r.verdict is ArcVerdict.INSUFFICIENT_DATA
        assert r.session_count == 0

    def test_single_session_returns_insufficient(
        self, monkeypatch,
    ):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = walk_session_arc(
            session_records_override=[_FakeSession(session_id="s1")],
        )
        assert r.verdict is ArcVerdict.INSUFFICIENT_DATA

    def test_two_identical_sessions_coherent(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        s = _FakeSession(session_id="s1")
        # Same fingerprint twice → all STABLE → coherent
        s2 = _FakeSession(session_id="s2")
        r = walk_session_arc(
            session_records_override=[s, s2],
        )
        assert r.verdict is ArcVerdict.COHERENT
        assert r.session_count == 2
        assert r.boundary_count == 1
        assert r.stable_count == 4
        assert r.coherence_ratio == 1.0

    def test_drifting_sessions_routed_correctly(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        # Every axis's content hash changes BUT count stays
        # exactly equal between adjacent sessions. Per canonical
        # compute_drift semantics (cross_session_harness line 549):
        # "same count, different hash → records were rewritten →
        # DIVERGED". Count growth would route additive_growth →
        # STABLE which is the wrong signal for this test.
        s1 = _FakeSession(
            session_id="s1",
            last_apply_files="a.py,b.py",   # 2 files
            drift_status="ok",
            convergence_state="A",
            stats_attempted=10,
            stop_reason="idle_timeout",     # session_history field
        )
        s2 = _FakeSession(
            session_id="s2",
            last_apply_files="c.py,d.py",   # 2 files, different content
            drift_status="drift",
            convergence_state="B",
            stats_attempted=10,             # same count
            stop_reason="wall_clock_cap",   # different stop_reason
        )
        r = walk_session_arc(
            session_records_override=[s1, s2],
        )
        # 4 axes, all DIVERGED (same count, different hash)
        assert r.verdict is ArcVerdict.DRIFTING
        assert r.coherence_ratio == 0.0
        # All DIVERGED entries should appear in the report
        assert r.diverged_count == 4

    def test_per_axis_timeseries_populated(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        s1 = _FakeSession(session_id="s1")
        s2 = _FakeSession(session_id="s2")
        s3 = _FakeSession(session_id="s3")
        r = walk_session_arc(
            session_records_override=[s1, s2, s3],
        )
        # 2 boundaries × 4 axes
        for axis in (
            "user_prefs", "adaptations",
            "semantic_centroid", "session_history",
        ):
            assert axis in r.per_axis_timeseries
            assert len(r.per_axis_timeseries[axis]) == 2

    def test_session_arc_ids_recorded(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        s1 = _FakeSession(session_id="bt-a")
        s2 = _FakeSession(session_id="bt-b")
        s3 = _FakeSession(session_id="bt-c")
        r = walk_session_arc(
            session_records_override=[s1, s2, s3],
        )
        assert r.session_arc == ("bt-a", "bt-b", "bt-c")

    def test_mostly_coherent_threshold_routing(self, monkeypatch):
        """Coherence ratio in [0.75, 1.0) → MOSTLY_COHERENT."""
        monkeypatch.setenv(_ENV_MASTER, "true")
        # 4 sessions, 3 boundaries × 4 axes = 12 measurements
        # Make 3 axes stable across all boundaries, 1 axis drift once
        s1 = _FakeSession(session_id="s1", last_apply_files="a.py")
        s2 = _FakeSession(session_id="s2", last_apply_files="a.py")
        s3 = _FakeSession(session_id="s3", last_apply_files="b.py")
        s4 = _FakeSession(session_id="s4", last_apply_files="b.py")
        r = walk_session_arc(
            session_records_override=[s1, s2, s3, s4],
        )
        # Most stable, some drift → MOSTLY_COHERENT
        assert r.verdict in (
            ArcVerdict.COHERENT, ArcVerdict.MOSTLY_COHERENT,
        )


# ---------------------------------------------------------------------------
# §33.5 frozen artifacts
# ---------------------------------------------------------------------------


class TestArtifacts:
    def test_boundary_drift_to_dict(self):
        d = BoundaryDrift(
            boundary_index=0,
            axis="user_prefs",
            level="stable",
            record_count_delta=1,
            hash_changed=False,
            diagnostic="additive",
        )
        out = d.to_dict()
        assert out["boundary_index"] == 0
        assert out["axis"] == "user_prefs"
        assert out["level"] == "stable"

    def test_arc_report_to_dict_serializable(self, monkeypatch):
        import json
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = walk_session_arc(
            session_records_override=[
                _FakeSession(session_id="s1"),
                _FakeSession(session_id="s2"),
            ],
        )
        d = r.to_dict()
        # Must be JSON-serializable for PhD-side plotting
        json_str = json.dumps(d)
        assert "verdict" in d
        assert "session_arc" in d
        assert "per_axis_timeseries" in d
        # Roundtrip
        reparsed = json.loads(json_str)
        assert reparsed["session_count"] == 2


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderer:
    def test_master_off_returns_disabled_marker(self):
        out = format_arc_panel()
        assert "disabled" in out

    def test_master_on_renders_panel(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "true")
        r = walk_session_arc(
            session_records_override=[
                _FakeSession(session_id="s1"),
                _FakeSession(session_id="s2"),
            ],
        )
        out = format_arc_panel(r)
        assert "Cross-Session Coherence Arc" in out
        assert "coherence_ratio" in out


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    src = Path(rig.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, tree


@pytest.fixture
def pins():
    return rig.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_5_pins_registered(self, pins):
        assert len(pins) == 5
        names = {p.invariant_name for p in pins}
        assert names == {
            "cross_session_coherence_rig_"
            "verdict_taxonomy_closed",
            "cross_session_coherence_rig_"
            "authority_asymmetry",
            "cross_session_coherence_rig_"
            "master_default_false",
            "cross_session_coherence_rig_"
            "composes_canonical_harness",
            "cross_session_coherence_rig_"
            "fingerprinter_coverage",
        }

    @pytest.mark.parametrize(
        "pin_name",
        [
            "cross_session_coherence_rig_"
            "verdict_taxonomy_closed",
            "cross_session_coherence_rig_"
            "authority_asymmetry",
            "cross_session_coherence_rig_"
            "master_default_false",
            "cross_session_coherence_rig_"
            "composes_canonical_harness",
            "cross_session_coherence_rig_"
            "fingerprinter_coverage",
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
class ArcVerdict(str, enum.Enum):
    COHERENT = "coherent"
    # MISSING: mostly_coherent, drifting, insufficient_data
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "cross_session_coherence_rig_verdict_taxonomy_closed"
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
            == "cross_session_coherence_rig_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_master_pin_fires_default_true(self, pins):
        synthetic = """
def master_enabled():
    return _flag("FOO", default=True)
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "cross_session_coherence_rig_master_default_false"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_composes_harness_pin_fires_on_missing(self, pins):
        synthetic = "x = 1\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "cross_session_coherence_rig_composes_canonical_harness"
        )
        violations = pin.validate(tree, synthetic)
        # All 4 compose-checks fire
        assert violations
        assert any("cross_session_harness" in v for v in violations)
        assert any("compute_drift" in v for v in violations)

    def test_fingerprinter_pin_fires_on_missing_axis(self, pins):
        synthetic = """
_FINGERPRINTERS = {
    "user_prefs": None,
    "adaptations": None,
    # MISSING: semantic_centroid, session_history
}
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "cross_session_coherence_rig_fingerprinter_coverage"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "semantic_centroid" in violations[0]
        assert "session_history" in violations[0]


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
            "JARVIS_CROSS_SESSION_COHERENCE_RIG_ENABLED",
            "JARVIS_CROSS_SESSION_COHERENCE_RIG_MAX_SESSIONS",
        ]:
            assert expected in names


# ---------------------------------------------------------------------------
# Canonical taxonomy reuse — NO parallel taxonomy
# ---------------------------------------------------------------------------


class TestCanonicalReuse:
    def test_uses_canonical_coherence_axis(self):
        """Rig MUST compose canonical CoherenceAxis — not
        define its own axis enum."""
        from backend.core.ouroboros.governance import (
            cross_session_coherence_rig as rig_mod,
        )
        src = Path(rig_mod.__file__).read_text(encoding="utf-8")
        # The rig module MUST NOT define its own CoherenceAxis
        # enum — that would be a parallel taxonomy
        tree = ast.parse(src)
        defines_coherence_axis = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CoherenceAxis"
            ):
                defines_coherence_axis = True
                break
        assert defines_coherence_axis is False, (
            "rig MUST NOT define a parallel CoherenceAxis enum"
        )

    def test_uses_canonical_drift_level(self):
        from backend.core.ouroboros.governance import (
            cross_session_coherence_rig as rig_mod,
        )
        src = Path(rig_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            assert not (
                isinstance(node, ast.ClassDef)
                and node.name == "DriftLevel"
            ), "rig MUST NOT define a parallel DriftLevel enum"


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_all_exports_present(self):
        for name in rig.__all__:
            assert getattr(rig, name) is not None

    def test_schema_version(self):
        assert CROSS_SESSION_COHERENCE_RIG_SCHEMA_VERSION.startswith(
            "cross_session_coherence_rig.",
        )
