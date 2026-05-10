"""§3.6.2 Vector #5 — Cross-Session Coherence Harness
regression spine (PRD v2.79 to v2.80, 2026-05-09).

Validates the harness substrate composes the 4 canonical
cross-session memory surfaces correctly:
  * UserPreferenceStore (markdown files in
    .jarvis/user_preferences/)
  * AdaptationLedger (JSONL ledger)
  * SemanticIndex (centroid in .jarvis/semantic_index.npz)
  * LastSessionSummary (summary.json parser)

Plants synthetic signals across N sessions, simulates
boundaries, asserts carryover + drift bounds.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_vector5(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CROSS_SESSION_HARNESS_ENABLED", raising=False,
    )
    yield


# ---------- master flag


def test_master_default_false():
    from backend.core.ouroboros.governance.cross_session_harness import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_CROSS_SESSION_HARNESS_ENABLED", value,
    )
    from backend.core.ouroboros.governance.cross_session_harness import (
        master_enabled,
    )
    assert master_enabled() is True


# ---------- closed taxonomies


def test_axis_taxonomy_4_values():
    from backend.core.ouroboros.governance.cross_session_harness import (
        CoherenceAxis,
    )
    assert {m.name for m in CoherenceAxis} == {
        "USER_PREFS", "ADAPTATIONS",
        "SEMANTIC_CENTROID", "SESSION_HISTORY",
    }


def test_level_taxonomy_4_values():
    from backend.core.ouroboros.governance.cross_session_harness import (
        DriftLevel,
    )
    assert {m.name for m in DriftLevel} == {
        "STABLE", "DRIFTING", "DIVERGED", "CORRUPTED",
    }


# ---------- frozen artifacts


def test_axis_digest_to_dict():
    from backend.core.ouroboros.governance.cross_session_harness import (
        AxisDigest, CoherenceAxis,
        CROSS_SESSION_HARNESS_SCHEMA_VERSION,
    )
    d = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=3,
        content_hash="abc123",
    )
    out = d.to_dict()
    assert out["axis"] == "user_prefs"
    assert out["record_count"] == 3
    assert (
        out["schema_version"]
        == CROSS_SESSION_HARNESS_SCHEMA_VERSION
    )


def test_cross_session_digest_axis_lookup():
    from backend.core.ouroboros.governance.cross_session_harness import (
        AxisDigest, CoherenceAxis, CrossSessionDigest,
    )
    d = AxisDigest(axis=CoherenceAxis.USER_PREFS)
    snap = CrossSessionDigest(digests=(d,))
    assert (
        snap.digest_for_axis(CoherenceAxis.USER_PREFS) is d
    )
    assert (
        snap.digest_for_axis(CoherenceAxis.ADAPTATIONS)
        is None
    )


# ---------- aggregate_digest


def test_aggregate_master_off_empty():
    from backend.core.ouroboros.governance.cross_session_harness import (
        aggregate_digest,
    )
    snap = aggregate_digest(project_root=Path("/tmp"))
    assert snap.digests == ()


def test_aggregate_master_on_returns_4_axes(
    monkeypatch, tmp_path,
):
    """Empty project_root should still produce 4 axes
    (one digest per CoherenceAxis value)."""
    monkeypatch.setenv(
        "JARVIS_CROSS_SESSION_HARNESS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_session_harness import (
        CoherenceAxis, aggregate_digest,
    )
    snap = aggregate_digest(project_root=tmp_path)
    seen = {d.axis for d in snap.digests}
    assert seen == set(CoherenceAxis)


def test_aggregate_deterministic_on_same_root(
    monkeypatch, tmp_path,
):
    """Same project_root with no changes MUST produce
    identical digests across repeated calls."""
    monkeypatch.setenv(
        "JARVIS_CROSS_SESSION_HARNESS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_session_harness import (
        CoherenceAxis, aggregate_digest,
    )
    s1 = aggregate_digest(project_root=tmp_path)
    s2 = aggregate_digest(project_root=tmp_path)
    for axis in CoherenceAxis:
        a = s1.digest_for_axis(axis)
        b = s2.digest_for_axis(axis)
        assert a is not None and b is not None
        assert a.content_hash == b.content_hash
        assert a.record_count == b.record_count


# ---------- compute_drift pure-function


def test_drift_both_empty_is_stable():
    from backend.core.ouroboros.governance.cross_session_harness import (
        AxisDigest, CoherenceAxis, DriftLevel, compute_drift,
    )
    a = AxisDigest(axis=CoherenceAxis.USER_PREFS)
    b = AxisDigest(axis=CoherenceAxis.USER_PREFS)
    drift = compute_drift(before=a, after=b)
    assert drift.level is DriftLevel.STABLE


def test_drift_identical_is_stable():
    from backend.core.ouroboros.governance.cross_session_harness import (
        AxisDigest, CoherenceAxis, DriftLevel, compute_drift,
    )
    a = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=5, content_hash="abc",
    )
    b = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=5, content_hash="abc",
    )
    drift = compute_drift(before=a, after=b)
    assert drift.level is DriftLevel.STABLE


def test_drift_additive_growth_is_stable():
    """Append-only growth (count up, hash necessarily
    different but no records rewritten) → STABLE."""
    from backend.core.ouroboros.governance.cross_session_harness import (
        AxisDigest, CoherenceAxis, DriftLevel, compute_drift,
    )
    a = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=3, content_hash="abc",
    )
    b = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=5, content_hash="def",
    )
    drift = compute_drift(before=a, after=b)
    assert drift.level is DriftLevel.STABLE
    assert drift.record_count_delta == 2


def test_drift_deletion_is_drifting():
    """Count went down → DRIFTING (not STABLE — deletion
    is potentially-lossy)."""
    from backend.core.ouroboros.governance.cross_session_harness import (
        AxisDigest, CoherenceAxis, DriftLevel, compute_drift,
    )
    a = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=5, content_hash="abc",
    )
    b = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=3, content_hash="def",
    )
    drift = compute_drift(before=a, after=b)
    assert drift.level is DriftLevel.DRIFTING
    assert drift.record_count_delta == -2


def test_drift_same_count_different_hash_is_diverged():
    """Records were rewritten in place — append-only
    substrates SHOULD never see this. → DIVERGED."""
    from backend.core.ouroboros.governance.cross_session_harness import (
        AxisDigest, CoherenceAxis, DriftLevel, compute_drift,
    )
    a = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=3, content_hash="abc",
    )
    b = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        record_count=3, content_hash="def",
    )
    drift = compute_drift(before=a, after=b)
    assert drift.level is DriftLevel.DIVERGED


def test_drift_diagnostic_marks_corrupted():
    """Either side carrying a load-failure diagnostic →
    CORRUPTED."""
    from backend.core.ouroboros.governance.cross_session_harness import (
        AxisDigest, CoherenceAxis, DriftLevel, compute_drift,
    )
    a = AxisDigest(
        axis=CoherenceAxis.USER_PREFS,
        diagnostic="load_failed:OSError",
    )
    b = AxisDigest(axis=CoherenceAxis.USER_PREFS)
    drift = compute_drift(before=a, after=b)
    assert drift.level is DriftLevel.CORRUPTED


def test_drift_axis_mismatch_is_corrupted():
    """Drift between different axes is meaningless →
    CORRUPTED."""
    from backend.core.ouroboros.governance.cross_session_harness import (
        AxisDigest, CoherenceAxis, DriftLevel, compute_drift,
    )
    a = AxisDigest(axis=CoherenceAxis.USER_PREFS)
    b = AxisDigest(axis=CoherenceAxis.ADAPTATIONS)
    drift = compute_drift(before=a, after=b)
    assert drift.level is DriftLevel.CORRUPTED


# ---------- end-to-end N-session scenarios


def test_3_session_arc_with_user_pref_growth(
    monkeypatch, tmp_path,
):
    """Plant signals across 3 simulated sessions; verify
    additive growth lands as STABLE in all boundaries."""
    monkeypatch.setenv(
        "JARVIS_CROSS_SESSION_HARNESS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_session_harness import (
        CoherenceAxis, DriftLevel, aggregate_digest,
        report_coherence, simulate_session_boundary,
    )

    user_pref_dir = (
        tmp_path / ".jarvis" / "user_preferences"
    )
    user_pref_dir.mkdir(parents=True, exist_ok=True)

    # Session 1 — empty.
    snap1 = aggregate_digest(project_root=tmp_path)

    # Plant 1 user pref → session 2 boundary.
    (user_pref_dir / "mem1.md").write_text(
        "---\nname: m1\ntype: user\ndescription: x\nsource: t\n---\n\nbody1"
    )
    snap2 = simulate_session_boundary(
        project_root=tmp_path,
    )

    # Plant 2nd user pref → session 3 boundary.
    (user_pref_dir / "mem2.md").write_text(
        "---\nname: m2\ntype: feedback\ndescription: y\nsource: t\n---\n\nbody2"
    )
    snap3 = simulate_session_boundary(
        project_root=tmp_path,
    )

    report = report_coherence((snap1, snap2, snap3))
    assert report.boundary_count == 2
    # Both boundaries: user_prefs grew by 1 → STABLE.
    for boundary_drifts in report.drifts_per_boundary:
        for drift in boundary_drifts:
            if drift.axis is CoherenceAxis.USER_PREFS:
                assert (
                    drift.level is DriftLevel.STABLE
                )
                assert drift.record_count_delta == 1


def test_deletion_produces_drifting_level(
    monkeypatch, tmp_path,
):
    """Deleting a user pref between sessions is correctly
    classified as DRIFTING (not STABLE — deletion is
    lossy)."""
    monkeypatch.setenv(
        "JARVIS_CROSS_SESSION_HARNESS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_session_harness import (
        CoherenceAxis, DriftLevel, aggregate_digest,
        report_coherence, simulate_session_boundary,
    )

    pref_dir = tmp_path / ".jarvis" / "user_preferences"
    pref_dir.mkdir(parents=True, exist_ok=True)
    (pref_dir / "m1.md").write_text(
        "---\nname: m1\ntype: user\ndescription: x\nsource: t\n---\n\nb"
    )
    (pref_dir / "m2.md").write_text(
        "---\nname: m2\ntype: user\ndescription: y\nsource: t\n---\n\nb"
    )
    snap_before = aggregate_digest(project_root=tmp_path)

    # Delete one between sessions.
    (pref_dir / "m1.md").unlink()
    snap_after = simulate_session_boundary(
        project_root=tmp_path,
    )

    report = report_coherence((snap_before, snap_after))
    user_drift = next(
        d for d in report.drifts_per_boundary[0]
        if d.axis is CoherenceAxis.USER_PREFS
    )
    assert user_drift.level is DriftLevel.DRIFTING
    assert user_drift.record_count_delta == -1
    assert report.overall_stable is False


def test_zero_session_report_is_stable():
    """0 boundaries (1 or 0 digests) → trivially stable."""
    from backend.core.ouroboros.governance.cross_session_harness import (
        report_coherence,
    )
    r = report_coherence(())
    assert r.boundary_count == 0
    assert r.overall_stable is True


def test_simulate_session_boundary_resets_singletons(
    monkeypatch, tmp_path,
):
    """simulate_session_boundary MUST reset the canonical
    in-process default singletons (UserPreferenceStore +
    LastSessionSummary) so the digest reads from disk
    rather than a stale cache."""
    monkeypatch.setenv(
        "JARVIS_CROSS_SESSION_HARNESS_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_session_harness import (
        simulate_session_boundary,
    )
    # Just verify it doesn't raise + returns a valid digest.
    snap = simulate_session_boundary(
        project_root=tmp_path,
    )
    assert snap is not None
    assert len(snap.digests) == 4


# ---------- AST pins


def _pins():
    from backend.core.ouroboros.governance.cross_session_harness import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _src():
    return Path(
        "backend/core/ouroboros/governance/"
        "cross_session_harness.py"
    ).read_text()


def test_pins_register_6():
    assert len(_pins()) == 6


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4, 5])
def test_pin_passes_canonical(idx):
    pins = _pins()
    src = _src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_fires():
    pin = next(
        p for p in _pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    assert pin.validate(ast.parse(bad), bad)


def test_pin_axis_taxonomy_fires():
    pin = next(
        p for p in _pins()
        if "axis_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class CoherenceAxis(str, enum.Enum):\n"
        "    USER_PREFS = 'user_prefs'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_level_taxonomy_fires():
    pin = next(
        p for p in _pins()
        if "level_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class DriftLevel(str, enum.Enum):\n"
        "    STABLE = 'stable'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_all_4_substrates_fires():
    pin = next(
        p for p in _pins()
        if "composes_all_4_substrates" in p.invariant_name
    )
    bad = "x = 1\n"
    assert pin.validate(ast.parse(bad), bad)


def test_pin_digesters_cover_all_axes_fires():
    pin = next(
        p for p in _pins()
        if "digesters_cover_all_axes" in p.invariant_name
    )
    bad = "x = 1\n"
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority_fires():
    pin = next(
        p for p in _pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_register_flags_count():
    from backend.core.ouroboros.governance.cross_session_harness import (
        register_flags,
    )

    class _M:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _M()
    assert register_flags(reg) == 1


# ---------- canonical-source smokes


def test_canonical_event_coherence_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_COHERENCE_REPORTED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_COHERENCE_REPORTED == "coherence_reported"
    assert EVENT_TYPE_COHERENCE_REPORTED in _VALID_EVENT_TYPES


def test_canonical_4_substrates_importable():
    """Lockstep regression — Vector #5 harness depends on
    all 4 canonical substrates being importable."""
    from backend.core.ouroboros.governance.user_preference_memory import (  # noqa: F401, E501
        UserPreferenceStore,
    )
    from backend.core.ouroboros.governance.adaptation.ledger import (  # noqa: F401, E501
        AdaptationLedger,
    )
    from backend.core.ouroboros.governance.semantic_index import (  # noqa: F401, E501
        SemanticIndex,
    )
    from backend.core.ouroboros.governance.last_session_summary import (  # noqa: F401, E501
        LastSessionSummary,
    )
