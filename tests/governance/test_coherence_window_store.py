"""Priority #1 Slice 2 — Window store regression tests.

Coverage:

  * **Defaults + clamps** — ``window_hours_default`` /
    ``max_signatures_default`` enforce floor + ceiling.
  * **Path resolution** — base dir env-tunable; window + audit
    paths derive from base.
  * **Append + read round-trip** — signature serializes via
    `to_dict`, reconstructs via `from_dict` (Slice 1's contract).
  * **Time-bounded read** — `window_hours` filter uses
    `window_end_ts` field (content-accurate, not file mtime).
  * **Rotation at cap** — bounded ring buffer evicts oldest when
    count exceeds max_signatures.
  * **Audit append-only** — never rotates per §8 invariant.
  * **Audit since_ts filter** — chronological filter via
    recorded_at_ts augmentation.
  * **Cross-process flock** — multi-process stress test verifies
    no lost writes.
  * **Schema-mismatch tolerance** — corrupt / wrong-version lines
    silently dropped, others returned.
  * **Defensive contract** — every public function NEVER raises.
  * **Authority invariants** — AST-pinned: stdlib + Tier 1 #3 +
    Slice 1 only; no orchestrator/etc; MUST reference
    flock_append_line; no exec/eval/compile; no async.
  * **5-value WindowOutcome closed taxonomy pin**.
  * **Frozen-dataclass schema integrity** for read-result
    containers.
"""
from __future__ import annotations

import ast
import json
import multiprocessing
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.verification.coherence_auditor import (
    BehavioralDriftFinding,
    BehavioralDriftKind,
    BehavioralDriftVerdict,
    BehavioralSignature,
    CoherenceOutcome,
    DriftSeverity,
)
from backend.core.ouroboros.governance.verification.coherence_window_store import (
    AuditReadResult,
    COHERENCE_WINDOW_STORE_SCHEMA_VERSION,
    WindowOutcome,
    WindowReadResult,
    coherence_audit_path,
    coherence_base_dir,
    coherence_window_path,
    max_signatures_default,
    read_drift_audit,
    read_window,
    record_drift_audit,
    record_signature,
    window_hours_default,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_base():
    """Isolated temp base dir per test. Resolved to canonical
    form so macOS ``/tmp`` → ``/private/tmp`` symlink expansion
    matches the module's ``.resolve()`` call."""
    d = Path(tempfile.mkdtemp(prefix="coherence_test_")).resolve()
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _make_sig(*, end_ts=None, p99=0, route="standard"):
    if end_ts is None:
        end_ts = time.time()
    return BehavioralSignature(
        window_start_ts=end_ts - 86400.0,
        window_end_ts=end_ts,
        route_distribution={route: 1.0},
        posture_distribution={"explore": 1.0},
        module_fingerprints={"foo.py": "abc"},
        p99_confidence_drop_count=p99,
        recurrence_index={},
        ops_summary={"apply": 1},
    )


def _make_verdict(*, outcome=None, kind=None, severity=None):
    o = outcome or CoherenceOutcome.DRIFT_DETECTED
    k = kind or BehavioralDriftKind.POSTURE_LOCKED
    s = severity or DriftSeverity.MEDIUM
    finding = BehavioralDriftFinding(
        kind=k, severity=s,
        detail="test finding",
        delta_metric=10.0, budget_metric=5.0,
    )
    return BehavioralDriftVerdict(
        outcome=o,
        findings=(finding,) if o is CoherenceOutcome.DRIFT_DETECTED else tuple(),
        largest_severity=(
            s if o is CoherenceOutcome.DRIFT_DETECTED
            else DriftSeverity.NONE
        ),
        drift_signature="b" * 64,
        detail="test",
    )


# ---------------------------------------------------------------------------
# 1. Path resolution + env knobs
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_base_dir_default(self):
        os.environ.pop("JARVIS_COHERENCE_BASE_DIR", None)
        assert coherence_base_dir().name == ".jarvis"

    def test_base_dir_env_override(self, tmp_base):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_BASE_DIR": str(tmp_base)},
        ):
            assert coherence_base_dir() == tmp_base

    def test_window_path_under_base(self, tmp_base):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_BASE_DIR": str(tmp_base)},
        ):
            assert (
                coherence_window_path()
                == tmp_base / "coherence_window.jsonl"
            )

    def test_audit_path_under_base(self, tmp_base):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_BASE_DIR": str(tmp_base)},
        ):
            assert (
                coherence_audit_path()
                == tmp_base / "coherence_audit.jsonl"
            )


# ---------------------------------------------------------------------------
# 2. Env knob clamps
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_window_hours_default(self):
        os.environ.pop("JARVIS_COHERENCE_WINDOW_HOURS", None)
        assert window_hours_default() == 168

    def test_window_hours_floor(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_WINDOW_HOURS": "1"},
        ):
            assert window_hours_default() == 24

    def test_window_hours_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_WINDOW_HOURS": "999999"},
        ):
            assert window_hours_default() == 720

    def test_max_signatures_default(self):
        os.environ.pop("JARVIS_COHERENCE_MAX_SIGNATURES", None)
        assert max_signatures_default() == 200

    def test_max_signatures_floor(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_MAX_SIGNATURES": "1"},
        ):
            assert max_signatures_default() == 10

    def test_max_signatures_ceiling(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_MAX_SIGNATURES": "999999"},
        ):
            assert max_signatures_default() == 5000

    def test_garbage_falls_back_to_default(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_MAX_SIGNATURES": "not-int"},
        ):
            assert max_signatures_default() == 200


# ---------------------------------------------------------------------------
# 3. Closed taxonomy — WindowOutcome 5-value pin
# ---------------------------------------------------------------------------


class TestClosedTaxonomy:
    def test_window_outcome_has_5_values(self):
        assert len(list(WindowOutcome)) == 5

    def test_window_outcome_values(self):
        expected = {
            "recorded", "window_rotated", "read_ok",
            "read_empty", "failed",
        }
        assert {o.value for o in WindowOutcome} == expected


# ---------------------------------------------------------------------------
# 4. record_signature + read_window round-trip
# ---------------------------------------------------------------------------


class TestSignatureRoundTrip:
    def test_empty_read_returns_empty(self, tmp_base):
        r = read_window(base_dir=tmp_base)
        assert r.outcome is WindowOutcome.READ_EMPTY
        assert r.signatures == ()

    def test_record_then_read_roundtrip(self, tmp_base):
        sig = _make_sig()
        out = record_signature(sig, base_dir=tmp_base)
        assert out is WindowOutcome.RECORDED
        r = read_window(base_dir=tmp_base)
        assert r.outcome is WindowOutcome.READ_OK
        assert len(r.signatures) == 1
        # Signature_id deterministic — round-trip preserves
        assert r.signatures[0].signature_id() == sig.signature_id()

    def test_record_preserves_distributions(self, tmp_base):
        sig = BehavioralSignature(
            window_start_ts=0.0, window_end_ts=time.time(),
            route_distribution={
                "standard": 0.6, "background": 0.4,
            },
            posture_distribution={"harden": 0.7},
            module_fingerprints={"a.py": "h1", "b.py": "h2"},
            p99_confidence_drop_count=42,
            recurrence_index={"to_fail": 5},
            ops_summary={"apply": 10, "verify": 9},
        )
        record_signature(sig, base_dir=tmp_base)
        r = read_window(base_dir=tmp_base)
        s = r.signatures[0]
        assert dict(s.route_distribution) == {
            "standard": 0.6, "background": 0.4,
        }
        assert dict(s.module_fingerprints) == {
            "a.py": "h1", "b.py": "h2",
        }
        assert s.p99_confidence_drop_count == 42
        assert dict(s.recurrence_index) == {"to_fail": 5}

    def test_multiple_records_chronologically_sorted(self, tmp_base):
        ts_now = time.time()
        # Insert in REVERSE order
        for offset in [3.0, 2.0, 1.0, 0.0]:
            record_signature(
                _make_sig(end_ts=ts_now - offset * 3600),
                base_dir=tmp_base,
            )
        r = read_window(base_dir=tmp_base, window_hours=24)
        # Assert chronologically ascending
        ts_list = [s.window_end_ts for s in r.signatures]
        assert ts_list == sorted(ts_list)

    def test_garbage_signature_returns_failed(self, tmp_base):
        out = record_signature(
            "not a signature",  # type: ignore[arg-type]
            base_dir=tmp_base,
        )
        assert out is WindowOutcome.FAILED


# ---------------------------------------------------------------------------
# 5. Time-bounded read filtering
# ---------------------------------------------------------------------------


class TestTimeBoundedRead:
    def test_old_signatures_excluded(self, tmp_base):
        ts_now = time.time()
        # 30 days ago — outside 7d window
        record_signature(
            _make_sig(end_ts=ts_now - 86400.0 * 30),
            base_dir=tmp_base,
        )
        # Within 7d window
        record_signature(
            _make_sig(end_ts=ts_now - 86400.0 * 2),
            base_dir=tmp_base,
        )
        r = read_window(
            base_dir=tmp_base, window_hours=168,
            now_ts=ts_now,
        )
        assert len(r.signatures) == 1

    def test_window_hours_floor_clamped_to_one(self, tmp_base):
        # window_hours=0 would degenerate; should clamp to 1h
        ts_now = time.time()
        record_signature(
            _make_sig(end_ts=ts_now), base_dir=tmp_base,
        )
        r = read_window(
            base_dir=tmp_base, window_hours=0, now_ts=ts_now,
        )
        # Should still find the just-recorded signature
        assert len(r.signatures) >= 1

    def test_all_outside_window_returns_empty(self, tmp_base):
        ts_now = time.time()
        record_signature(
            _make_sig(end_ts=ts_now - 86400.0 * 30),
            base_dir=tmp_base,
        )
        r = read_window(
            base_dir=tmp_base, window_hours=24, now_ts=ts_now,
        )
        assert r.outcome is WindowOutcome.READ_EMPTY


# ---------------------------------------------------------------------------
# 6. Rotation at max_signatures cap
# ---------------------------------------------------------------------------


class TestRotation:
    def test_no_rotation_under_cap(self, tmp_base):
        for i in range(5):
            out = record_signature(
                _make_sig(p99=i), base_dir=tmp_base,
                max_signatures=10,
            )
            assert out is WindowOutcome.RECORDED

    def test_rotation_at_cap(self, tmp_base):
        # Record 12 with cap=10; the last few must report ROTATED
        outcomes = []
        for i in range(12):
            out = record_signature(
                _make_sig(p99=i), base_dir=tmp_base,
                max_signatures=10,
            )
            outcomes.append(out)
        # First 10 records: RECORDED; last 2: WINDOW_ROTATED
        assert WindowOutcome.RECORDED in outcomes
        assert WindowOutcome.WINDOW_ROTATED in outcomes
        # Verify only 10 entries on disk
        r = read_window(
            base_dir=tmp_base, window_hours=720,
        )
        assert len(r.signatures) == 10

    def test_rotation_keeps_newest(self, tmp_base):
        ts_now = time.time()
        # Insert 15 signatures with increasing p99 markers
        for i in range(15):
            record_signature(
                _make_sig(end_ts=ts_now + i, p99=i),
                base_dir=tmp_base, max_signatures=5,
            )
        r = read_window(
            base_dir=tmp_base, window_hours=720,
        )
        # Most-recent 5 retained — p99 = 10..14
        p99s = sorted(s.p99_confidence_drop_count for s in r.signatures)
        assert p99s == [10, 11, 12, 13, 14]


# ---------------------------------------------------------------------------
# 7. Audit log append-only (NEVER rotates per §8)
# ---------------------------------------------------------------------------


class TestAuditAppendOnly:
    def test_record_audit(self, tmp_base):
        v = _make_verdict()
        out = record_drift_audit(v, base_dir=tmp_base)
        assert out is WindowOutcome.RECORDED

    def test_audit_round_trip(self, tmp_base):
        v = _make_verdict()
        record_drift_audit(v, base_dir=tmp_base)
        r = read_drift_audit(base_dir=tmp_base, since_ts=0.0)
        assert r.outcome is WindowOutcome.READ_OK
        assert len(r.verdicts) == 1
        recovered = r.verdicts[0]
        assert recovered.outcome is v.outcome
        assert recovered.largest_severity is v.largest_severity
        assert recovered.drift_signature == v.drift_signature
        assert recovered.findings[0].kind is v.findings[0].kind
        assert (
            recovered.findings[0].severity
            is v.findings[0].severity
        )

    def test_audit_append_does_not_rotate(self, tmp_base):
        # Record many — count must keep growing
        for i in range(50):
            record_drift_audit(
                _make_verdict(),
                base_dir=tmp_base,
            )
        r = read_drift_audit(base_dir=tmp_base, since_ts=0.0)
        assert len(r.verdicts) == 50

    def test_audit_garbage_returns_failed(self, tmp_base):
        out = record_drift_audit(
            "not a verdict",  # type: ignore[arg-type]
            base_dir=tmp_base,
        )
        assert out is WindowOutcome.FAILED

    def test_audit_empty_read(self, tmp_base):
        r = read_drift_audit(base_dir=tmp_base, since_ts=0.0)
        assert r.outcome is WindowOutcome.READ_EMPTY


# ---------------------------------------------------------------------------
# 8. since_ts filtering
# ---------------------------------------------------------------------------


class TestSinceTsFilter:
    def test_since_ts_zero_returns_all(self, tmp_base):
        record_drift_audit(_make_verdict(), base_dir=tmp_base)
        record_drift_audit(_make_verdict(), base_dir=tmp_base)
        r = read_drift_audit(base_dir=tmp_base, since_ts=0.0)
        assert len(r.verdicts) == 2

    def test_since_ts_future_returns_empty(self, tmp_base):
        record_drift_audit(_make_verdict(), base_dir=tmp_base)
        r = read_drift_audit(
            base_dir=tmp_base, since_ts=time.time() + 3600,
        )
        assert r.outcome is WindowOutcome.READ_EMPTY

    def test_limit_keeps_newest(self, tmp_base):
        for _ in range(5):
            record_drift_audit(_make_verdict(), base_dir=tmp_base)
            time.sleep(0.001)  # ensure distinct timestamps
        r = read_drift_audit(
            base_dir=tmp_base, since_ts=0.0, limit=2,
        )
        assert len(r.verdicts) == 2


# ---------------------------------------------------------------------------
# 9. Schema mismatch tolerance
# ---------------------------------------------------------------------------


class TestSchemaTolerance:
    def test_corrupt_line_skipped_others_returned(self, tmp_base):
        # Pre-populate with: 1 valid signature, 1 corrupt line,
        # 1 wrong-schema-version, 1 valid
        path = tmp_base / "coherence_window.jsonl"
        tmp_base.mkdir(parents=True, exist_ok=True)
        sig1 = _make_sig(p99=1)
        sig2 = _make_sig(p99=99)
        valid_line_1 = json.dumps(sig1.to_dict())
        valid_line_2 = json.dumps(sig2.to_dict())
        wrong_version = {"schema_version": "wrong.99"}
        wrong_line = json.dumps(wrong_version)
        with open(path, "w") as f:
            f.write(valid_line_1 + "\n")
            f.write("not-json-corrupt-line\n")
            f.write(wrong_line + "\n")
            f.write(valid_line_2 + "\n")
        r = read_window(base_dir=tmp_base, window_hours=720)
        assert r.outcome is WindowOutcome.READ_OK
        # 2 valid signatures recovered (corrupt + wrong-version dropped)
        assert len(r.signatures) == 2
        p99s = {s.p99_confidence_drop_count for s in r.signatures}
        assert p99s == {1, 99}

    def test_audit_corrupt_line_skipped(self, tmp_base):
        path = tmp_base / "coherence_audit.jsonl"
        tmp_base.mkdir(parents=True, exist_ok=True)
        v = _make_verdict()
        valid = v.to_dict()
        valid["recorded_at_ts"] = time.time()
        with open(path, "w") as f:
            f.write("garbage-line\n")
            f.write(json.dumps(valid) + "\n")
        r = read_drift_audit(base_dir=tmp_base, since_ts=0.0)
        assert len(r.verdicts) == 1


# ---------------------------------------------------------------------------
# 10. Cross-process flock — multi-process stress
# ---------------------------------------------------------------------------


def _writer_process(base_dir_str: str, n: int, p99_offset: int):
    """Worker process: append n signatures."""
    from backend.core.ouroboros.governance.verification.coherence_window_store import (  # noqa: E501
        record_signature,
    )
    from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
        BehavioralSignature,
    )
    import time as _time
    base_dir = Path(base_dir_str)
    for i in range(n):
        sig = BehavioralSignature(
            window_start_ts=0.0,
            window_end_ts=_time.time(),
            route_distribution={"standard": 1.0},
            posture_distribution={"explore": 1.0},
            module_fingerprints={},
            p99_confidence_drop_count=p99_offset + i,
            recurrence_index={},
            ops_summary={},
        )
        record_signature(
            sig, base_dir=base_dir,
            max_signatures=1000,
        )


class TestCrossProcess:
    def test_multi_process_no_lost_writes(self, tmp_base):
        """Two processes append concurrently. With cross-process
        flock, all writes must persist (no last-writer-wins
        truncation)."""
        try:
            ctx = multiprocessing.get_context("spawn")
        except ValueError:
            pytest.skip("spawn context unavailable")
        n_per_proc = 20
        p1 = ctx.Process(
            target=_writer_process,
            args=(str(tmp_base), n_per_proc, 0),
        )
        p2 = ctx.Process(
            target=_writer_process,
            args=(str(tmp_base), n_per_proc, 100),
        )
        p1.start()
        p2.start()
        p1.join(timeout=30)
        p2.join(timeout=30)
        assert p1.exitcode == 0
        assert p2.exitcode == 0
        r = read_window(base_dir=tmp_base, window_hours=720)
        # Both processes' writes should all land
        assert len(r.signatures) == n_per_proc * 2


# ---------------------------------------------------------------------------
# 11. Atomic-write — partial-write protection
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_window_file_never_partial(self, tmp_base):
        """After many record_signature calls, the file must always
        be fully readable (no partial writes due to crash mid-
        write — atomic rename guarantees)."""
        for i in range(20):
            record_signature(
                _make_sig(p99=i), base_dir=tmp_base,
                max_signatures=15,
            )
        path = tmp_base / "coherence_window.jsonl"
        assert path.exists()
        # Every line must parse as valid JSON (no truncation)
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                json.loads(ln)  # raises if partial


# ---------------------------------------------------------------------------
# 12. Result containers — frozen + schema integrity
# ---------------------------------------------------------------------------


class TestResultContainers:
    def test_window_read_result_frozen(self):
        r = WindowReadResult(outcome=WindowOutcome.READ_EMPTY)
        with pytest.raises((AttributeError, Exception)):
            r.outcome = WindowOutcome.READ_OK  # type: ignore[misc]

    def test_audit_read_result_frozen(self):
        r = AuditReadResult(outcome=WindowOutcome.READ_EMPTY)
        with pytest.raises((AttributeError, Exception)):
            r.outcome = WindowOutcome.READ_OK  # type: ignore[misc]

    def test_schema_version_stable(self):
        assert (
            COHERENCE_WINDOW_STORE_SCHEMA_VERSION
            == "coherence_window_store.1"
        )


# ---------------------------------------------------------------------------
# 13. Defensive contract — never raises
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_record_into_unwritable_dir_returns_failed(
        self, tmp_base,
    ):
        # Create a path where writing would fail
        bad = Path("/proc/no/such/path/x")
        out = record_signature(_make_sig(), base_dir=bad)
        # Either FAILED or RECORDED if some os let it through
        # — must not raise
        assert out in (
            WindowOutcome.FAILED, WindowOutcome.RECORDED,
        )

    def test_read_with_garbage_path_returns_empty(self, tmp_base):
        # Pass a path that doesn't exist — must not raise
        r = read_window(base_dir=tmp_base / "nonexistent")
        assert r.outcome is WindowOutcome.READ_EMPTY


# ---------------------------------------------------------------------------
# 14. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


def _module_source() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "verification" / "coherence_window_store.py"
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariants:
    @pytest.fixture
    def source(self):
        return _module_source()

    def test_no_orchestrator_imports(self, source):
        forbidden = [
            "orchestrator", "iron_gate", "policy",
            "change_engine", "candidate_generator", "providers",
            "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "phase_runners",
            "semantic_guardian", "semantic_firewall",
            "risk_engine",
        ]
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                module = module or ""
                for f in forbidden:
                    assert f not in module, (
                        f"forbidden import: {module}"
                    )

    def test_governance_imports_in_allowlist(self, source):
        """Slice 2 may import ONLY:
          * Tier 1 #3 (cross_process_jsonl)
          * Slice 1 (coherence_auditor)"""
        tree = ast.parse(source)
        allowed = {
            "backend.core.ouroboros.governance.cross_process_jsonl",
            "backend.core.ouroboros.governance.verification.coherence_auditor",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "governance" in node.module:
                    assert node.module in allowed, (
                        f"governance import outside allowlist: "
                        f"{node.module}"
                    )

    def test_must_reference_flock_append_line(self, source):
        """STRUCTURAL cross-process safety guard. Slice 2 MUST
        use the Tier 1 #3 flock_append_line for the audit log
        append path. Catches a refactor that drops cross-process
        safety."""
        assert "flock_append_line" in source, (
            "store dropped its reference to flock_append_line — "
            "the audit log cross-process safety guard is gone"
        )

    def test_must_reference_flock_critical_section(self, source):
        """STRUCTURAL: the bounded ring buffer MUST use
        flock_critical_section for read-trim-write coordination.
        Catches a refactor that drops this."""
        assert "flock_critical_section" in source

    def test_no_mutation_tools(self, source):
        forbidden = [
            "edit_file", "write_file", "delete_file",
            "subprocess." + "run", "subprocess." + "Popen",
            "os." + "system", "shutil.rmtree",
        ]
        for f in forbidden:
            assert f not in source, (
                f"store contains forbidden mutation token: {f!r}"
            )

    def test_no_exec_eval_compile(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    ), (
                        f"forbidden call: {node.func.id}"
                    )

    def test_no_async_functions(self, source):
        """Slice 2 is sync; Slice 3 introduces async."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef), (
                f"forbidden async function in Slice 2: "
                f"{node.name}"
            )

    def test_public_api_exported(self, source):
        for name in (
            "record_signature", "read_window",
            "record_drift_audit", "read_drift_audit",
            "WindowOutcome", "WindowReadResult",
            "AuditReadResult",
            "coherence_base_dir", "coherence_window_path",
            "coherence_audit_path",
            "window_hours_default", "max_signatures_default",
            "COHERENCE_WINDOW_STORE_SCHEMA_VERSION",
        ):
            assert f'"{name}"' in source, (
                f"public API {name!r} not in __all__"
            )
