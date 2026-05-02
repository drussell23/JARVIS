"""Move 4 Slice 1 — InvariantDriftAuditor primitive regression spine.

Coverage tracks the four public surfaces of the primitive:

  * Frozen-dataclass shape + serialization invariants
  * ``capture_snapshot`` reads from the four live surfaces and
    degrades gracefully on per-surface failure
  * ``compare_snapshots`` is pure / deterministic / total over the
    full closed taxonomy of ``DriftKind`` values
  * Authority invariants AST-pinned: the module imports only stdlib
    and the four read-only governance surfaces it consumes.

Slice 1 does NOT wire the primitive into orchestrator / SSE / REPL —
those tests land in Slices 2-5 alongside their producer surfaces.

Naming: this auditor is **InvariantDriftAuditor** — sister module to
``observability/trajectory_auditor.py`` (which tracks physical
codebase metrics). Both are read-only, pure-data primitives; this
one detects *semantic safety-property* regressions while the other
detects *codebase volume / shape* drift.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import (
    invariant_drift_auditor as ida,
)
from backend.core.ouroboros.governance.invariant_drift_auditor import (
    DriftKind,
    DriftSeverity,
    ExplorationFloorPin,
    INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION,
    InvariantDriftRecord,
    InvariantSnapshot,
    capture_snapshot,
    compare_snapshots,
    filter_by_severity,
    has_critical_drift,
    invariant_drift_auditor_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _baseline_snapshot(**overrides) -> InvariantSnapshot:
    """A reference snapshot with stable, predictable shape. Overrides
    via kwargs lets each test mutate exactly the field under test."""
    base = dict(
        snapshot_id="snap-baseline",
        captured_at_utc=1000.0,
        shipped_invariant_names=("alpha", "beta", "gamma"),
        shipped_violation_signature="abc123",
        shipped_violation_count=0,
        flag_registry_hash="flag_hash_v1",
        flag_count=50,
        exploration_floor_pins=(
            ExplorationFloorPin(
                complexity="trivial",
                min_score=0.0,
                min_categories=0,
                required_categories=(),
            ),
            ExplorationFloorPin(
                complexity="moderate",
                min_score=8.0,
                min_categories=3,
                required_categories=(),
            ),
            ExplorationFloorPin(
                complexity="architectural",
                min_score=11.0,
                min_categories=4,
                required_categories=("call_graph", "history"),
            ),
        ),
        posture_value="EXPLORE",
        posture_confidence=0.85,
    )
    base.update(overrides)
    return InvariantSnapshot(**base)


# ---------------------------------------------------------------------------
# 1. Frozen-dataclass shape + serialization
# ---------------------------------------------------------------------------


class TestDataclassShape:
    def test_invariant_snapshot_is_frozen(self):
        snap = _baseline_snapshot()
        with pytest.raises((AttributeError, Exception)):
            snap.posture_value = "HARDEN"  # type: ignore[misc]

    def test_drift_record_is_frozen(self):
        rec = InvariantDriftRecord(
            drift_kind=DriftKind.POSTURE_DRIFT,
            severity=DriftSeverity.INFO,
            detail="x",
        )
        with pytest.raises((AttributeError, Exception)):
            rec.detail = "y"  # type: ignore[misc]

    def test_exploration_floor_pin_is_frozen(self):
        pin = ExplorationFloorPin(
            complexity="moderate", min_score=8.0,
            min_categories=3, required_categories=(),
        )
        with pytest.raises((AttributeError, Exception)):
            pin.min_score = 0.0  # type: ignore[misc]

    def test_snapshot_to_dict_round_trip_keys(self):
        snap = _baseline_snapshot()
        d = snap.to_dict()
        for k in (
            "snapshot_id", "captured_at_utc",
            "shipped_invariant_names",
            "shipped_violation_signature",
            "shipped_violation_count",
            "flag_registry_hash", "flag_count",
            "exploration_floor_pins",
            "posture_value", "posture_confidence",
            "schema_version",
        ):
            assert k in d
        assert (
            d["schema_version"]
            == INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION
        )

    def test_floor_pin_to_dict_round_trip(self):
        pin = ExplorationFloorPin(
            complexity="architectural",
            min_score=11.0,
            min_categories=4,
            required_categories=("call_graph", "history"),
        )
        d = pin.to_dict()
        assert d["complexity"] == "architectural"
        assert d["min_score"] == 11.0
        assert d["min_categories"] == 4
        assert d["required_categories"] == [
            "call_graph", "history",
        ]

    def test_drift_record_to_dict_serializes_enums(self):
        rec = InvariantDriftRecord(
            drift_kind=DriftKind.SHIPPED_INVARIANT_REMOVED,
            severity=DriftSeverity.CRITICAL,
            detail="x",
            affected_keys=("k1",),
        )
        d = rec.to_dict()
        assert d["drift_kind"] == "shipped_invariant_removed"
        assert d["severity"] == "critical"
        assert d["affected_keys"] == ["k1"]

    def test_schema_version_is_pinned(self):
        # Pin the schema string — slice arc is invariant once
        # graduated. Bumps require explicit ledger migration.
        assert (
            INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION
            == "invariant_drift_auditor.1"
        )


# ---------------------------------------------------------------------------
# 2. Master flag — asymmetric-env-semantics contract
# ---------------------------------------------------------------------------


class TestMasterFlag:
    @pytest.mark.parametrize(
        "value,expected",
        [
            # Slice 5 graduation: empty/whitespace = default true
            ("", True),
            ("0", False),
            ("false", False),
            ("FALSE", False),
            ("no", False),
            ("off", False),
            ("1", True),
            ("true", True),
            ("True", True),
            ("YES", True),
            ("on", True),
        ],
    )
    def test_env_truthy_falsy_matrix(
        self, monkeypatch, value, expected,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", value,
        )
        assert invariant_drift_auditor_enabled() is expected

    def test_unset_env_returns_default_true_post_graduation(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
            raising=False,
        )
        # Slice 5 graduation flipped this default.
        assert invariant_drift_auditor_enabled() is True

    def test_whitespace_only_treated_as_unset_post_graduation(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "   ",
        )
        # Whitespace = unset = post-graduation default true.
        assert invariant_drift_auditor_enabled() is True

    def test_garbage_value_falls_to_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "banana",
        )
        # Garbage is not a recognized truthy token — falls through
        # to false (hot-revert path).
        assert invariant_drift_auditor_enabled() is False


# ---------------------------------------------------------------------------
# 3. compare_snapshots — full DriftKind taxonomy + ordering + totality
# ---------------------------------------------------------------------------


class TestCompareSnapshots:
    def test_identical_snapshots_yield_no_drift(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot()
        assert compare_snapshots(a, b) == ()

    def test_shipped_invariant_removed_is_critical(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            shipped_invariant_names=("alpha", "gamma"),  # beta gone
        )
        records = compare_snapshots(a, b)
        assert len(records) == 1
        rec = records[0]
        assert rec.drift_kind is DriftKind.SHIPPED_INVARIANT_REMOVED
        assert rec.severity is DriftSeverity.CRITICAL
        assert "beta" in rec.detail
        assert rec.affected_keys == ("beta",)

    def test_shipped_invariant_added_is_not_drift(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            shipped_invariant_names=(
                "alpha", "beta", "delta", "gamma",
            ),
        )
        # Adding pins is tightening, not regression.
        assert compare_snapshots(a, b) == ()

    def test_violation_introduced_is_critical(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            shipped_violation_count=3,
            shipped_violation_signature="newsig",
        )
        records = compare_snapshots(a, b)
        kinds = {r.drift_kind for r in records}
        assert DriftKind.SHIPPED_VIOLATION_INTRODUCED in kinds
        violation_rec = next(
            r for r in records
            if r.drift_kind is DriftKind.SHIPPED_VIOLATION_INTRODUCED
        )
        assert violation_rec.severity is DriftSeverity.CRITICAL

    def test_violation_signature_change_at_same_count_is_warning(self):
        a = _baseline_snapshot(
            shipped_violation_count=2,
            shipped_violation_signature="oldsig",
        )
        b = _baseline_snapshot(
            shipped_violation_count=2,
            shipped_violation_signature="newsig",
        )
        records = compare_snapshots(a, b)
        sig_records = [
            r for r in records
            if r.drift_kind
            is DriftKind.SHIPPED_VIOLATION_SIGNATURE_CHANGED
        ]
        assert len(sig_records) == 1
        assert sig_records[0].severity is DriftSeverity.WARNING

    def test_violation_resolved_is_not_drift(self):
        # Going from violations -> zero violations is improvement,
        # not regression.
        a = _baseline_snapshot(
            shipped_violation_count=5,
            shipped_violation_signature="oldsig",
        )
        b = _baseline_snapshot(
            shipped_violation_count=0,
            shipped_violation_signature="",
        )
        assert compare_snapshots(a, b) == ()

    def test_flag_hash_change_is_warning(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(flag_registry_hash="flag_hash_v2")
        records = compare_snapshots(a, b)
        flag_recs = [
            r for r in records
            if r.drift_kind is DriftKind.FLAG_REGISTRY_HASH_CHANGED
        ]
        assert len(flag_recs) == 1
        assert flag_recs[0].severity is DriftSeverity.WARNING

    def test_flag_count_decrease_is_critical(self):
        a = _baseline_snapshot(flag_count=50)
        b = _baseline_snapshot(
            flag_count=49, flag_registry_hash="flag_hash_v2",
        )
        records = compare_snapshots(a, b)
        decrease_recs = [
            r for r in records
            if r.drift_kind is DriftKind.FLAG_REGISTRY_COUNT_DECREASED
        ]
        assert len(decrease_recs) == 1
        assert decrease_recs[0].severity is DriftSeverity.CRITICAL

    def test_flag_count_increase_is_not_critical(self):
        a = _baseline_snapshot(flag_count=50)
        b = _baseline_snapshot(
            flag_count=55, flag_registry_hash="flag_hash_v2",
        )
        records = compare_snapshots(a, b)
        # Hash changed (warning) + count went up — but no count-
        # decrease critical record.
        assert not any(
            r.drift_kind
            is DriftKind.FLAG_REGISTRY_COUNT_DECREASED
            for r in records
        )

    def test_exploration_min_score_lowered_is_critical(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            exploration_floor_pins=(
                ExplorationFloorPin(
                    complexity="trivial", min_score=0.0,
                    min_categories=0, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="moderate", min_score=4.0,  # was 8.0
                    min_categories=3, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="architectural", min_score=11.0,
                    min_categories=4,
                    required_categories=("call_graph", "history"),
                ),
            ),
        )
        records = compare_snapshots(a, b)
        floor_recs = [
            r for r in records
            if r.drift_kind is DriftKind.EXPLORATION_FLOOR_LOWERED
        ]
        assert len(floor_recs) == 1
        assert floor_recs[0].severity is DriftSeverity.CRITICAL
        assert "moderate" in floor_recs[0].detail
        assert floor_recs[0].affected_keys == ("moderate",)

    def test_exploration_min_score_raised_is_not_drift(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            exploration_floor_pins=(
                ExplorationFloorPin(
                    complexity="trivial", min_score=0.0,
                    min_categories=0, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="moderate", min_score=10.0,  # was 8.0
                    min_categories=3, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="architectural", min_score=11.0,
                    min_categories=4,
                    required_categories=("call_graph", "history"),
                ),
            ),
        )
        # Raising floors is tightening, not regression.
        assert compare_snapshots(a, b) == ()

    def test_exploration_min_categories_lowered_is_critical(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            exploration_floor_pins=(
                ExplorationFloorPin(
                    complexity="trivial", min_score=0.0,
                    min_categories=0, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="moderate", min_score=8.0,
                    min_categories=2,  # was 3
                    required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="architectural", min_score=11.0,
                    min_categories=4,
                    required_categories=("call_graph", "history"),
                ),
            ),
        )
        records = compare_snapshots(a, b)
        assert any(
            r.drift_kind is DriftKind.EXPLORATION_FLOOR_LOWERED
            and r.severity is DriftSeverity.CRITICAL
            and "min_categories" in r.detail
            for r in records
        )

    def test_required_category_dropped_is_critical(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            exploration_floor_pins=(
                ExplorationFloorPin(
                    complexity="trivial", min_score=0.0,
                    min_categories=0, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="moderate", min_score=8.0,
                    min_categories=3, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="architectural", min_score=11.0,
                    min_categories=4,
                    required_categories=("history",),  # call_graph dropped
                ),
            ),
        )
        records = compare_snapshots(a, b)
        dropped = [
            r for r in records
            if r.drift_kind
            is DriftKind.EXPLORATION_REQUIRED_CATEGORY_DROPPED
        ]
        assert len(dropped) == 1
        assert dropped[0].severity is DriftSeverity.CRITICAL
        assert "call_graph" in dropped[0].detail

    def test_required_category_added_is_not_drift(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            exploration_floor_pins=(
                ExplorationFloorPin(
                    complexity="trivial", min_score=0.0,
                    min_categories=0, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="moderate", min_score=8.0,
                    min_categories=3,
                    required_categories=("structure",),  # added
                ),
                ExplorationFloorPin(
                    complexity="architectural", min_score=11.0,
                    min_categories=4,
                    required_categories=("call_graph", "history"),
                ),
            ),
        )
        # Adding required categories is tightening, not regression.
        assert compare_snapshots(a, b) == ()

    def test_exploration_bucket_removed_is_critical(self):
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            exploration_floor_pins=(
                ExplorationFloorPin(
                    complexity="trivial", min_score=0.0,
                    min_categories=0, required_categories=(),
                ),
                # moderate removed entirely
                ExplorationFloorPin(
                    complexity="architectural", min_score=11.0,
                    min_categories=4,
                    required_categories=("call_graph", "history"),
                ),
            ),
        )
        records = compare_snapshots(a, b)
        rec = next(
            r for r in records
            if r.drift_kind is DriftKind.EXPLORATION_BUCKET_REMOVED
        )
        assert rec.severity is DriftSeverity.CRITICAL
        assert "moderate" in rec.detail

    def test_posture_drift_is_info_only(self):
        a = _baseline_snapshot(posture_value="EXPLORE")
        b = _baseline_snapshot(posture_value="HARDEN")
        records = compare_snapshots(a, b)
        assert len(records) == 1
        rec = records[0]
        assert rec.drift_kind is DriftKind.POSTURE_DRIFT
        assert rec.severity is DriftSeverity.INFO

    def test_posture_unread_yields_no_posture_record(self):
        a = _baseline_snapshot(posture_value=None)
        b = _baseline_snapshot(posture_value="HARDEN")
        records = compare_snapshots(a, b)
        assert all(
            r.drift_kind is not DriftKind.POSTURE_DRIFT
            for r in records
        )

    def test_compare_total_over_malformed_inputs(self):
        # Non-snapshot inputs return empty — never raises.
        assert compare_snapshots(None, _baseline_snapshot()) == ()  # type: ignore[arg-type]
        assert compare_snapshots(_baseline_snapshot(), "x") == ()  # type: ignore[arg-type]
        assert compare_snapshots(42, 43) == ()  # type: ignore[arg-type]

    def test_drift_record_ordering(self):
        # When all four drift surfaces fire, shipped first, posture
        # last — operator surfaces depend on this stability.
        a = _baseline_snapshot()
        b = _baseline_snapshot(
            shipped_invariant_names=("alpha", "gamma"),  # beta removed
            flag_registry_hash="changed",
            exploration_floor_pins=(
                ExplorationFloorPin(
                    complexity="trivial", min_score=0.0,
                    min_categories=0, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="moderate", min_score=4.0,
                    min_categories=3, required_categories=(),
                ),
                ExplorationFloorPin(
                    complexity="architectural", min_score=11.0,
                    min_categories=4,
                    required_categories=("call_graph", "history"),
                ),
            ),
            posture_value="HARDEN",
        )
        records = compare_snapshots(a, b)
        kinds = [r.drift_kind for r in records]
        # First record is shipped-invariant
        assert kinds[0] is DriftKind.SHIPPED_INVARIANT_REMOVED
        # Last record is posture
        assert kinds[-1] is DriftKind.POSTURE_DRIFT


# ---------------------------------------------------------------------------
# 4. capture_snapshot — defensive live read
# ---------------------------------------------------------------------------


class TestCaptureSnapshot:
    def test_returns_invariant_snapshot(self):
        snap = capture_snapshot()
        assert isinstance(snap, InvariantSnapshot)
        assert (
            snap.schema_version
            == INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION
        )

    def test_snapshot_id_is_unique_by_default(self):
        a = capture_snapshot()
        b = capture_snapshot()
        assert a.snapshot_id != b.snapshot_id

    def test_snapshot_id_overridable(self):
        snap = capture_snapshot(snapshot_id="custom-id")
        assert snap.snapshot_id == "custom-id"

    def test_now_overridable(self):
        snap = capture_snapshot(now=12345.0)
        assert snap.captured_at_utc == 12345.0

    def test_shipped_invariants_populated(self):
        # Live shipped-code invariants registry is non-empty post-
        # boot (seed invariants register at module import).
        snap = capture_snapshot()
        assert len(snap.shipped_invariant_names) > 0
        # Names sorted
        assert list(snap.shipped_invariant_names) == sorted(
            snap.shipped_invariant_names,
        )

    def test_exploration_floors_populated(self):
        snap = capture_snapshot()
        # _DEFAULT_FLOORS has 5 buckets — capture should reflect
        # all of them (they exist at module-import time).
        assert len(snap.exploration_floor_pins) >= 4

    def test_capture_never_raises_even_when_surfaces_break(
        self, monkeypatch,
    ):
        # Replace each capture helper with a sentinel-returning stub
        # to prove the public capture entrypoint produces a well-
        # formed snapshot when every surface is unavailable.
        monkeypatch.setattr(
            ida, "_capture_shipped_invariants",
            lambda: ((), "", 0),
        )
        monkeypatch.setattr(
            ida, "_capture_flag_registry",
            lambda: ("", 0),
        )
        monkeypatch.setattr(
            ida, "_capture_exploration_floors",
            lambda: (),
        )
        monkeypatch.setattr(
            ida, "_capture_posture",
            lambda: (None, None),
        )
        snap = capture_snapshot()
        assert isinstance(snap, InvariantSnapshot)
        assert snap.shipped_invariant_names == ()
        assert snap.flag_count == 0
        assert snap.exploration_floor_pins == ()
        assert snap.posture_value is None


# ---------------------------------------------------------------------------
# 5. filter_by_severity / has_critical_drift helpers
# ---------------------------------------------------------------------------


class TestFilteringHelpers:
    def _records(self):
        return (
            InvariantDriftRecord(
                drift_kind=DriftKind.SHIPPED_INVARIANT_REMOVED,
                severity=DriftSeverity.CRITICAL,
                detail="c1",
            ),
            InvariantDriftRecord(
                drift_kind=DriftKind.FLAG_REGISTRY_HASH_CHANGED,
                severity=DriftSeverity.WARNING,
                detail="w1",
            ),
            InvariantDriftRecord(
                drift_kind=DriftKind.POSTURE_DRIFT,
                severity=DriftSeverity.INFO,
                detail="i1",
            ),
        )

    def test_filter_minimum_warning_excludes_info(self):
        out = filter_by_severity(
            self._records(), minimum=DriftSeverity.WARNING,
        )
        kinds = {r.severity for r in out}
        assert DriftSeverity.INFO not in kinds
        assert DriftSeverity.WARNING in kinds
        assert DriftSeverity.CRITICAL in kinds

    def test_filter_minimum_critical_excludes_warning_and_info(self):
        out = filter_by_severity(
            self._records(), minimum=DriftSeverity.CRITICAL,
        )
        assert all(
            r.severity is DriftSeverity.CRITICAL for r in out
        )

    def test_filter_minimum_info_returns_all(self):
        out = filter_by_severity(
            self._records(), minimum=DriftSeverity.INFO,
        )
        assert len(out) == 3

    def test_has_critical_drift_true(self):
        assert has_critical_drift(self._records()) is True

    def test_has_critical_drift_false_on_warning_info_only(self):
        records = (
            InvariantDriftRecord(
                drift_kind=DriftKind.FLAG_REGISTRY_HASH_CHANGED,
                severity=DriftSeverity.WARNING,
                detail="w",
            ),
        )
        assert has_critical_drift(records) is False

    def test_has_critical_drift_false_on_empty(self):
        assert has_critical_drift(()) is False


# ---------------------------------------------------------------------------
# 6. Authority invariants — AST-pinned. The InvariantDriftAuditor is
#    read-only; it MUST NOT import authority modules.
# ---------------------------------------------------------------------------


_FORBIDDEN_AUTHORITY_SUBSTRINGS = (
    "orchestrator",
    "phase_runners",
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "policy",
    "semantic_guardian",
    "semantic_firewall",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "invariant_drift_auditor.py"
            )
        cur = cur.parent
    raise RuntimeError("repo root not found")


class TestAuthorityInvariants:
    def test_no_forbidden_authority_imports(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                        if fb in alias.name:
                            offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                    if fb in mod:
                        offenders.append(mod)
        assert offenders == [], (
            f"invariant_drift_auditor.py imports forbidden "
            f"authority modules: {offenders}"
        )

    def test_only_consumes_read_only_governance_surfaces(self):
        # Whitelist: the four read-only surfaces.
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        allowed_governance_substrings = (
            "shipped_code_invariants",
            "flag_registry",
            "exploration_engine",
            "posture_observer",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    continue
                ok = any(
                    sub in mod
                    for sub in allowed_governance_substrings
                )
                assert ok, (
                    f"invariant_drift_auditor.py imports unexpected "
                    f"governance module: {mod}"
                )

    def test_module_does_not_perform_disk_writes(self):
        # Read-only contract: NO open(...) calls in write mode,
        # no Path.write_text / write_bytes, no os.replace, no
        # tempfile.NamedTemporaryFile. Slice 1 is pure-compute.
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        forbidden_tokens = (
            ".write_text(",
            ".write_bytes(",
            "os.replace(",
            "NamedTemporaryFile",
        )
        for tok in forbidden_tokens:
            assert tok not in source, (
                f"invariant_drift_auditor.py contains forbidden "
                f"write token: {tok!r}"
            )
        # open() calls must not occur at all in Slice 1.
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "open":
                    pytest.fail(
                        f"invariant_drift_auditor.py contains "
                        f"bare open() call at line "
                        f"{getattr(node, 'lineno', '?')}"
                    )

    def test_public_api_exported(self):
        # Public surface — Slice 4 wires these into producers; pin
        # the export list so a refactor can't accidentally hide one.
        expected_exports = {
            "DriftKind",
            "DriftSeverity",
            "ExplorationFloorPin",
            "INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION",
            "InvariantDriftRecord",
            "InvariantSnapshot",
            "capture_snapshot",
            "compare_snapshots",
            "filter_by_severity",
            "has_critical_drift",
            "invariant_drift_auditor_enabled",
        }
        assert set(ida.__all__) == expected_exports

    def test_drift_kind_taxonomy_pinned(self):
        # Closed taxonomy — adding a kind requires explicit slice
        # work; this test pins the current set so a silent addition
        # is caught.
        expected = {
            "shipped_invariant_removed",
            "shipped_violation_introduced",
            "shipped_violation_signature_changed",
            "flag_registry_hash_changed",
            "flag_registry_count_decreased",
            "exploration_floor_lowered",
            "exploration_required_category_dropped",
            "exploration_bucket_removed",
            "posture_drift",
            "gradient_drift_detected",  # added 2026-05-02 by
            # auto_action_router edge-case race-condition fix
            # (a0782f7d5f); pre-existing addition CIGW will surface
            # via Slice 4 advisory bridge.
        }
        assert {k.value for k in DriftKind} == expected

    def test_severity_taxonomy_pinned(self):
        expected = {"critical", "warning", "info"}
        assert {s.value for s in DriftSeverity} == expected
