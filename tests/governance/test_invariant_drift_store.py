"""Move 4 Slice 2 — InvariantDriftStore + boot helper regression spine.

Coverage tracks the four public surfaces of the slice:

  * ``InvariantSnapshot.from_dict`` round-trip — full schema, edge
    cases, schema-mismatch tolerance.
  * ``InvariantDriftStore`` — atomic write, JSON round-trip, history
    ring buffer, audit append, schema mismatch handling, corruption
    tolerance.
  * ``install_boot_snapshot`` decision tree — every branch in the
    explicit closed taxonomy of ``BootSnapshotOutcome``.
  * Async wrapper + authority invariants AST-pinned.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import threading
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import invariant_drift_store as ids
from backend.core.ouroboros.governance.invariant_drift_auditor import (
    DriftKind,
    DriftSeverity,
    ExplorationFloorPin,
    InvariantDriftRecord,
    InvariantSnapshot,
)
from backend.core.ouroboros.governance.invariant_drift_store import (
    BaselineAuditRecord,
    BootSnapshotOutcome,
    BootSnapshotResult,
    INVARIANT_DRIFT_STORE_SCHEMA,
    InvariantDriftStore,
    default_base_dir,
    default_history_size,
    get_default_store,
    install_boot_snapshot,
    install_boot_snapshot_async,
    reset_default_store,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _master_flag_on(monkeypatch):
    """Slice 2 tests assume master flag on by default; tests that
    need it off override explicitly."""
    monkeypatch.setenv(
        "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "true",
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_default_store():
    """Slice 2 default-store singleton must not leak across tests."""
    reset_default_store()
    yield
    reset_default_store()


@pytest.fixture
def store(tmp_path) -> InvariantDriftStore:
    return InvariantDriftStore(tmp_path)


def _stub_snapshot(
    snapshot_id: str = "stub",
    captured_at_utc: float = 1000.0,
    **overrides,
) -> InvariantSnapshot:
    base = dict(
        snapshot_id=snapshot_id,
        captured_at_utc=captured_at_utc,
        shipped_invariant_names=("alpha", "beta"),
        shipped_violation_signature="sig123",
        shipped_violation_count=0,
        flag_registry_hash="flag_v1",
        flag_count=42,
        exploration_floor_pins=(
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
        posture_confidence=0.9,
    )
    base.update(overrides)
    return InvariantSnapshot(**base)


# ---------------------------------------------------------------------------
# 1. from_dict round-trip — Slice 2 additive surface on Slice 1 dataclasses
# ---------------------------------------------------------------------------


class TestFromDictRoundTrip:
    def test_snapshot_round_trip_preserves_equality(self):
        snap = _stub_snapshot()
        d = snap.to_dict()
        restored = InvariantSnapshot.from_dict(d)
        assert restored == snap

    def test_snapshot_round_trip_via_json(self):
        snap = _stub_snapshot()
        text = json.dumps(snap.to_dict())
        restored = InvariantSnapshot.from_dict(json.loads(text))
        assert restored == snap

    def test_floor_pin_round_trip(self):
        pin = ExplorationFloorPin(
            complexity="architectural",
            min_score=11.0,
            min_categories=4,
            required_categories=("call_graph", "history"),
        )
        restored = ExplorationFloorPin.from_dict(pin.to_dict())
        assert restored == pin

    def test_drift_record_round_trip(self):
        rec = InvariantDriftRecord(
            drift_kind=DriftKind.SHIPPED_INVARIANT_REMOVED,
            severity=DriftSeverity.CRITICAL,
            detail="x",
            affected_keys=("k1", "k2"),
        )
        restored = InvariantDriftRecord.from_dict(rec.to_dict())
        assert restored == rec

    def test_snapshot_schema_mismatch_returns_none(self):
        snap = _stub_snapshot()
        d = snap.to_dict()
        d["schema_version"] = "totally_wrong_schema"
        assert InvariantSnapshot.from_dict(d) is None

    def test_snapshot_missing_schema_returns_none(self):
        snap = _stub_snapshot()
        d = snap.to_dict()
        del d["schema_version"]
        assert InvariantSnapshot.from_dict(d) is None

    def test_snapshot_missing_required_field_returns_none(self):
        snap = _stub_snapshot()
        d = snap.to_dict()
        del d["snapshot_id"]
        assert InvariantSnapshot.from_dict(d) is None

    def test_snapshot_malformed_pin_returns_none(self):
        snap = _stub_snapshot()
        d = snap.to_dict()
        d["exploration_floor_pins"] = [
            {"complexity": "x"},  # missing min_score
        ]
        assert InvariantSnapshot.from_dict(d) is None

    def test_floor_pin_malformed_returns_none(self):
        assert ExplorationFloorPin.from_dict(
            {"complexity": "x"},
        ) is None

    def test_drift_record_unknown_kind_returns_none(self):
        assert InvariantDriftRecord.from_dict(
            {"drift_kind": "bogus", "severity": "info", "detail": ""},
        ) is None

    def test_drift_record_unknown_severity_returns_none(self):
        assert InvariantDriftRecord.from_dict(
            {
                "drift_kind": "posture_drift",
                "severity": "BOGUS",
                "detail": "",
            },
        ) is None


# ---------------------------------------------------------------------------
# 2. Store — atomic write + JSON round-trip
# ---------------------------------------------------------------------------


class TestBaselineWriteRead:
    def test_write_then_load_equals_original(self, store):
        snap = _stub_snapshot()
        store.write_baseline(snap)
        loaded = store.load_baseline()
        assert loaded == snap

    def test_load_with_no_file_returns_none(self, store):
        assert store.load_baseline() is None

    def test_overwrite_replaces_baseline(self, store):
        a = _stub_snapshot(snapshot_id="a")
        b = _stub_snapshot(snapshot_id="b")
        store.write_baseline(a)
        store.write_baseline(b)
        assert store.load_baseline().snapshot_id == "b"

    def test_corrupt_json_returns_none(self, store):
        store.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        store.baseline_path.write_text("not valid json {{{",
                                       encoding="utf-8")
        assert store.load_baseline() is None

    def test_non_object_payload_returns_none(self, store):
        store.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        store.baseline_path.write_text("[1,2,3]", encoding="utf-8")
        assert store.load_baseline() is None

    def test_schema_mismatch_returns_none(self, store):
        snap = _stub_snapshot()
        d = snap.to_dict()
        d["schema_version"] = "old.1"
        store.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        store.baseline_path.write_text(
            json.dumps(d), encoding="utf-8",
        )
        assert store.load_baseline() is None

    def test_atomic_write_no_temp_files_left(self, store):
        snap = _stub_snapshot()
        store.write_baseline(snap)
        # No leftover .tmp files in the directory
        leftovers = list(store.base_dir.glob("*.tmp"))
        assert leftovers == []

    def test_atomic_write_no_temp_files_left_after_failure(
        self, store, monkeypatch,
    ):
        # Force os.replace to fail; verify the temp file is cleaned up.
        original_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(os, "replace", boom)
        store.write_baseline(_stub_snapshot())
        # Restore so the cleanup pass works
        monkeypatch.setattr(os, "replace", original_replace)
        # Even with the simulated failure, no .tmp leftovers
        leftovers = list(store.base_dir.glob("*.tmp"))
        assert leftovers == []

    def test_has_baseline_true_after_write(self, store):
        store.write_baseline(_stub_snapshot())
        assert store.has_baseline() is True

    def test_has_baseline_false_when_corrupt(self, store):
        store.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        store.baseline_path.write_text("garbage", encoding="utf-8")
        assert store.has_baseline() is False

    def test_clear_baseline_idempotent(self, store):
        store.write_baseline(_stub_snapshot())
        store.clear_baseline()
        store.clear_baseline()  # second time is no-op
        assert not store.baseline_path.exists()

    def test_write_baseline_never_raises_on_disk_failure(
        self, store, monkeypatch,
    ):
        def boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(store, "_atomic_write", boom)
        # Should swallow the exception, not propagate.
        store.write_baseline(_stub_snapshot())


# ---------------------------------------------------------------------------
# 3. Store — history ring buffer
# ---------------------------------------------------------------------------


class TestHistory:
    def test_append_and_load(self, store):
        a = _stub_snapshot(snapshot_id="a")
        b = _stub_snapshot(snapshot_id="b")
        store.append_history(a)
        store.append_history(b)
        loaded = store.load_history()
        assert [s.snapshot_id for s in loaded] == ["a", "b"]

    def test_history_trimmed_to_capacity(self, tmp_path):
        store = InvariantDriftStore(tmp_path, history_size=3)
        for i in range(5):
            store.append_history(_stub_snapshot(snapshot_id=f"s{i}"))
        loaded = store.load_history()
        assert [s.snapshot_id for s in loaded] == ["s2", "s3", "s4"]

    def test_load_empty_when_no_file(self, store):
        assert store.load_history() == []

    def test_load_with_limit(self, store):
        for i in range(5):
            store.append_history(_stub_snapshot(snapshot_id=f"s{i}"))
        loaded = store.load_history(limit=2)
        assert [s.snapshot_id for s in loaded] == ["s3", "s4"]

    def test_load_skips_malformed_lines(self, store):
        store.append_history(_stub_snapshot(snapshot_id="ok"))
        # Inject a junk line into the file
        existing = store.history_path.read_text(encoding="utf-8")
        store.history_path.write_text(
            existing + "this is not json\n", encoding="utf-8",
        )
        # Plus a schema-mismatched line
        d = _stub_snapshot(snapshot_id="bad").to_dict()
        d["schema_version"] = "wrong.1"
        store.history_path.write_text(
            store.history_path.read_text(encoding="utf-8")
            + json.dumps(d) + "\n",
            encoding="utf-8",
        )
        loaded = store.load_history()
        assert [s.snapshot_id for s in loaded] == ["ok"]

    def test_default_history_size_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_HISTORY_SIZE", "5",
        )
        # Floor is 16 — anything less should clamp.
        assert default_history_size() == 16

    def test_default_history_size_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_HISTORY_SIZE", "100",
        )
        assert default_history_size() == 100

    def test_default_history_size_garbage_falls_to_default(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_HISTORY_SIZE", "not_a_number",
        )
        assert default_history_size() == 256


# ---------------------------------------------------------------------------
# 4. Store — audit log (immutable §8)
# ---------------------------------------------------------------------------


class TestAudit:
    def test_audit_append_and_load(self, store):
        rec = BaselineAuditRecord(
            event="initial", at_utc=100.0, snapshot_id="s1",
        )
        store.append_audit(rec)
        loaded = store.load_audit()
        assert len(loaded) == 1
        assert loaded[0].event == "initial"
        assert loaded[0].snapshot_id == "s1"

    def test_audit_is_append_only_never_trimmed(self, store):
        for i in range(50):
            store.append_audit(BaselineAuditRecord(
                event="initial", at_utc=float(i),
                snapshot_id=f"s{i}",
            ))
        loaded = store.load_audit()
        assert len(loaded) == 50

    def test_audit_load_with_limit(self, store):
        for i in range(10):
            store.append_audit(BaselineAuditRecord(
                event="initial", at_utc=float(i),
                snapshot_id=f"s{i}",
            ))
        loaded = store.load_audit(limit=3)
        assert [r.snapshot_id for r in loaded] == ["s7", "s8", "s9"]

    def test_audit_skips_malformed_lines(self, store):
        store.append_audit(BaselineAuditRecord(
            event="initial", at_utc=1.0, snapshot_id="ok",
        ))
        # Inject junk
        with store.audit_path.open("a", encoding="utf-8") as fh:
            fh.write("not_json\n")
            fh.write('{"missing_fields": true}\n')
        loaded = store.load_audit()
        assert [r.snapshot_id for r in loaded] == ["ok"]


# ---------------------------------------------------------------------------
# 5. install_boot_snapshot — full BootSnapshotOutcome decision tree
# ---------------------------------------------------------------------------


class TestBootSnapshot:
    def test_disabled_when_master_flag_off(
        self, store, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "false",
        )
        result = install_boot_snapshot(
            store=store, snapshot=_stub_snapshot(),
        )
        assert result.outcome is BootSnapshotOutcome.DISABLED
        assert result.snapshot is None
        # No disk side effects when disabled
        assert not store.baseline_path.exists()
        assert not store.audit_path.exists()

    def test_first_boot_writes_initial_baseline(self, store):
        snap = _stub_snapshot()
        result = install_boot_snapshot(store=store, snapshot=snap)
        assert result.outcome is BootSnapshotOutcome.NEW_BASELINE
        assert result.snapshot == snap
        assert store.has_baseline()
        audit = store.load_audit()
        assert len(audit) == 1
        assert audit[0].event == "initial"

    def test_returning_boot_no_drift_returns_matched(self, store):
        snap = _stub_snapshot()
        # First boot — establish baseline
        install_boot_snapshot(store=store, snapshot=snap)
        # Second boot — same snapshot
        result = install_boot_snapshot(store=store, snapshot=snap)
        assert result.outcome is BootSnapshotOutcome.BASELINE_MATCHED
        assert result.snapshot == snap
        assert result.drift_records == ()
        # Audit unchanged — no boot_drift, no rebaseline
        audit = store.load_audit()
        assert len(audit) == 1
        assert audit[0].event == "initial"

    def test_returning_boot_with_drift_returns_drifted_no_replace(
        self, store,
    ):
        # Establish baseline
        baseline_snap = _stub_snapshot(
            snapshot_id="baseline",
            shipped_invariant_names=("alpha", "beta"),
        )
        install_boot_snapshot(
            store=store, snapshot=baseline_snap,
        )
        baseline_loaded_before = store.load_baseline()
        # Second boot — beta removed (CRITICAL drift)
        drifted_snap = _stub_snapshot(
            snapshot_id="drifted",
            shipped_invariant_names=("alpha",),
        )
        result = install_boot_snapshot(
            store=store, snapshot=drifted_snap,
        )
        assert result.outcome is BootSnapshotOutcome.BASELINE_DRIFTED
        assert result.snapshot == drifted_snap
        assert len(result.drift_records) >= 1
        assert any(
            r.drift_kind is DriftKind.SHIPPED_INVARIANT_REMOVED
            for r in result.drift_records
        )
        # Critical: baseline NOT auto-replaced
        baseline_after = store.load_baseline()
        assert baseline_after == baseline_loaded_before
        # Audit recorded the drift
        audit = store.load_audit()
        events = [r.event for r in audit]
        assert "boot_drift" in events
        assert events.count("initial") == 1

    def test_corrupt_baseline_replaced_with_audit(self, store):
        snap = _stub_snapshot()
        # Plant a corrupt baseline
        store.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        store.baseline_path.write_text(
            "garbage_not_json", encoding="utf-8",
        )
        result = install_boot_snapshot(store=store, snapshot=snap)
        assert result.outcome is BootSnapshotOutcome.NEW_BASELINE
        audit = store.load_audit()
        assert len(audit) == 1
        assert audit[0].event == "corrupted"

    def test_schema_mismatch_baseline_replaced(self, store):
        snap = _stub_snapshot()
        # Plant a schema-mismatched baseline
        d = snap.to_dict()
        d["schema_version"] = "ancient.0"
        d["snapshot_id"] = "ancient_baseline"
        store.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        store.baseline_path.write_text(
            json.dumps(d), encoding="utf-8",
        )
        result = install_boot_snapshot(store=store, snapshot=snap)
        assert result.outcome is BootSnapshotOutcome.NEW_BASELINE
        audit = store.load_audit()
        assert any(r.event == "schema_mismatch" for r in audit)

    def test_non_object_baseline_replaced(self, store):
        snap = _stub_snapshot()
        store.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        store.baseline_path.write_text("[1,2,3]", encoding="utf-8")
        result = install_boot_snapshot(store=store, snapshot=snap)
        assert result.outcome is BootSnapshotOutcome.NEW_BASELINE
        audit = store.load_audit()
        assert any(r.event == "corrupted" for r in audit)

    def test_force_rebaseline_overwrites_even_with_match(self, store):
        snap = _stub_snapshot()
        install_boot_snapshot(store=store, snapshot=snap)
        # Forced re-baseline with a NEW snapshot (different id)
        new_snap = _stub_snapshot(snapshot_id="forced")
        result = install_boot_snapshot(
            store=store, snapshot=new_snap, force_rebaseline=True,
        )
        assert result.outcome is BootSnapshotOutcome.NEW_BASELINE
        audit = store.load_audit()
        assert any(r.event == "forced_rebaseline" for r in audit)
        # Baseline reflects the forced-new snapshot
        loaded = store.load_baseline()
        assert loaded.snapshot_id == "forced"

    def test_capture_failure_returns_failed(self, store, monkeypatch):
        # Patch capture_snapshot at the module level used by the
        # boot helper.
        def boom(**kw):
            raise RuntimeError("capture failed")

        monkeypatch.setattr(ids, "capture_snapshot", boom)
        result = install_boot_snapshot(store=store)
        assert result.outcome is BootSnapshotOutcome.FAILED
        assert result.snapshot is None
        # No baseline written
        assert not store.has_baseline()

    def test_returning_boot_uses_default_store_when_unspecified(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BASE_DIR", str(tmp_path),
        )
        reset_default_store()
        snap = _stub_snapshot()
        result = install_boot_snapshot(snapshot=snap)
        assert result.outcome is BootSnapshotOutcome.NEW_BASELINE
        # Default store should have written into tmp_path
        ds = get_default_store()
        assert ds.baseline_path.parent == tmp_path

    def test_install_async_returns_same_result(self, store):
        snap = _stub_snapshot()

        async def _run() -> BootSnapshotResult:
            return await install_boot_snapshot_async(
                store=store, snapshot=snap,
            )

        result = asyncio.run(_run())
        assert result.outcome is BootSnapshotOutcome.NEW_BASELINE
        assert result.snapshot == snap


# ---------------------------------------------------------------------------
# 6. Default store singleton + concurrency
# ---------------------------------------------------------------------------


class TestDefaultStoreAndConcurrency:
    def test_get_default_store_returns_singleton(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BASE_DIR", str(tmp_path),
        )
        reset_default_store()
        a = get_default_store()
        b = get_default_store()
        assert a is b

    def test_reset_default_store_replaces_instance(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BASE_DIR", str(tmp_path),
        )
        reset_default_store()
        a = get_default_store()
        reset_default_store()
        b = get_default_store()
        assert a is not b

    def test_default_base_dir_respects_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BASE_DIR", str(tmp_path),
        )
        result = default_base_dir()
        assert result == tmp_path.resolve()

    def test_default_base_dir_unset_uses_dot_jarvis(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_BASE_DIR", raising=False,
        )
        result = default_base_dir()
        assert result.name == ".jarvis"

    def test_concurrent_writes_do_not_corrupt(self, store):
        # Hammer the store with concurrent appends — none should
        # cause a file corruption (lock should serialize).
        errors = []

        def worker(idx: int):
            try:
                for i in range(5):
                    store.append_history(
                        _stub_snapshot(
                            snapshot_id=f"w{idx}-{i}",
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        loaded = store.load_history()
        # 8 threads × 5 appends = 40 entries total (under capacity)
        assert len(loaded) == 40


# ---------------------------------------------------------------------------
# 7. Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_empty_store(self, store):
        s = store.stats()
        assert s["has_baseline"] is False
        assert s["history_count"] == 0
        assert s["audit_count"] == 0
        assert s["schema_version"] == INVARIANT_DRIFT_STORE_SCHEMA

    def test_stats_after_writes(self, store):
        store.write_baseline(_stub_snapshot())
        store.append_history(_stub_snapshot())
        store.append_audit(BaselineAuditRecord(
            event="initial", at_utc=1.0, snapshot_id="x",
        ))
        s = store.stats()
        assert s["has_baseline"] is True
        assert s["history_count"] == 1
        assert s["audit_count"] == 1


# ---------------------------------------------------------------------------
# 8. BootSnapshotResult shape + serialization
# ---------------------------------------------------------------------------


class TestBootSnapshotResult:
    def test_to_dict_disabled(self):
        r = BootSnapshotResult(
            outcome=BootSnapshotOutcome.DISABLED,
            snapshot=None,
            detail="x",
        )
        d = r.to_dict()
        assert d["outcome"] == "disabled"
        assert d["snapshot"] is None
        assert d["drift_records"] == []

    def test_to_dict_with_snapshot_and_drift(self):
        snap = _stub_snapshot()
        rec = InvariantDriftRecord(
            drift_kind=DriftKind.POSTURE_DRIFT,
            severity=DriftSeverity.INFO,
            detail="x",
        )
        r = BootSnapshotResult(
            outcome=BootSnapshotOutcome.BASELINE_DRIFTED,
            snapshot=snap,
            drift_records=(rec,),
        )
        d = r.to_dict()
        assert d["outcome"] == "baseline_drifted"
        assert d["snapshot"]["snapshot_id"] == snap.snapshot_id
        assert len(d["drift_records"]) == 1

    def test_result_is_frozen(self):
        r = BootSnapshotResult(
            outcome=BootSnapshotOutcome.DISABLED, snapshot=None,
        )
        with pytest.raises((AttributeError, Exception)):
            r.detail = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 9. Authority invariants — AST-pinned
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
    # Slice 2 store consumes ONLY the auditor — it must NOT
    # re-import the four read-only surfaces directly. That's the
    # auditor's job.
    "posture_observer",
    "shipped_code_invariants",
    "exploration_engine",
    "flag_registry",
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "invariant_drift_store.py"
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
            f"invariant_drift_store.py imports forbidden modules: "
            f"{offenders}"
        )

    def test_only_consumes_auditor_module_from_governance(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        # Allowed: invariant_drift_auditor (Slice 2's snapshot type)
        # + cross_process_jsonl (Tier 1 #3 cross-process flock helper).
        allowed = (
            "invariant_drift_auditor",
            "cross_process_jsonl",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    continue
                ok = any(sub in mod for sub in allowed)
                assert ok, (
                    f"invariant_drift_store imports unexpected "
                    f"governance module: {mod}"
                )

    def test_public_api_exported(self):
        expected_exports = {
            "BaselineAuditRecord",
            "BootSnapshotOutcome",
            "BootSnapshotResult",
            "INVARIANT_DRIFT_STORE_SCHEMA",
            "InvariantDriftStore",
            "default_base_dir",
            "default_history_size",
            "get_default_store",
            "install_boot_snapshot",
            "install_boot_snapshot_async",
            "reset_default_store",
        }
        assert set(ids.__all__) == expected_exports

    def test_boot_outcome_taxonomy_pinned(self):
        # 5-value closed taxonomy — adding a value requires explicit
        # slice work; this test catches silent additions.
        expected = {
            "new_baseline",
            "baseline_matched",
            "baseline_drifted",
            "disabled",
            "failed",
        }
        assert {o.value for o in BootSnapshotOutcome} == expected

    def test_install_uses_atomic_replace(self):
        # Pin: the implementation MUST use os.replace via the
        # _atomic_write helper — never a direct write that could
        # leave a partial file on crash.
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        assert "os.replace(" in source, (
            "invariant_drift_store.py must use os.replace for "
            "atomic file writes"
        )
        assert "tempfile.mkstemp" in source, (
            "invariant_drift_store.py must use tempfile.mkstemp "
            "for atomic write staging"
        )
