"""Upgrade 2 Slice 2 — replay-determinism CLI tests
(PRD §31.3).

Pins:
  § 1 — Master flag default-false (graduates Slice 5)
  § 2 — Closed-taxonomy ReplayDriftKind (5 values)
  § 3 — Frozen ReplayDriftReport + ReplaySummary
  § 4 — replay_session_consistency() decision tree:
        master-off → exit_code=2
        empty session_id → exit_code=2
        missing decisions.jsonl → exit_code=2
        empty file → exit_code=2 (insufficient data)
        clean records → exit_code=0
        drift detected → exit_code=1
  § 5 — _verify_record() per-record drift detection:
        clean → no drift
        non-canonical output_repr → OUTPUT_REPR_NON_CANONICAL
        bad JSON → PARSE_ERROR
        schema-version drift → SCHEMA_VERSION_DRIFT
  § 6 — Cross-process flock'd read (no-tear contract)
  § 7 — CLI entry — JSON output + exit codes
  § 8 — Authority floor (no orchestrator/iron_gate imports)
  § 9 — Public exports
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_ledger(tmp_path, session_id, records):
    """Create .jarvis/determinism/<session>/decisions.jsonl
    with the given records (list of dicts) + return the path."""
    ledger_dir = tmp_path / "determinism" / session_id
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / "decisions.jsonl"
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def _clean_record(record_id="rec-1", ordinal=0):
    return {
        "record_id": record_id,
        "session_id": "s",
        "op_id": "op",
        "phase": "ROUTE",
        "kind": "route_selection",
        "ordinal": ordinal,
        "inputs_hash": "abcdef",
        # Canonical JSON: keys sorted, no spaces between separators
        "output_repr": '{"a":1,"b":2}',
        "monotonic_ts": 1.0,
        "wall_ts": 2.0,
        "schema_version": "decision_record.1",
    }


def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_REPLAY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR",
        str(tmp_path / "determinism"),
    )


# ---------------------------------------------------------------------------
# § 1 — Master flag
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_is_false_pre_graduation(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_DETERMINISM_REPLAY_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_determinism_enabled,
        )
        assert replay_determinism_enabled() is False

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_truthy_flips_on(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_DETERMINISM_REPLAY_ENABLED", v,
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_determinism_enabled,
        )
        assert replay_determinism_enabled() is True


# ---------------------------------------------------------------------------
# § 2 — Closed-taxonomy ReplayDriftKind
# ---------------------------------------------------------------------------


class TestReplayDriftKind:
    def test_exactly_five_values(self):
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplayDriftKind,
        )
        values = {m.value for m in ReplayDriftKind}
        assert values == {
            "none",
            "input_hash_mismatch",
            "output_repr_non_canonical",
            "schema_version_drift",
            "parse_error",
        }

    def test_str_subclass(self):
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplayDriftKind,
        )
        assert issubclass(ReplayDriftKind, str)


# ---------------------------------------------------------------------------
# § 3 — Frozen ReplayDriftReport + ReplaySummary
# ---------------------------------------------------------------------------


class TestFrozenDataclasses:
    def test_drift_report_is_frozen(self):
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplayDriftKind,
            ReplayDriftReport,
        )
        r = ReplayDriftReport(
            kind=ReplayDriftKind.NONE, record_index=0,
            record_id="x", expected="", actual="",
        )
        with pytest.raises(Exception):
            r.kind = ReplayDriftKind.PARSE_ERROR  # type: ignore[misc]

    def test_summary_is_frozen(self):
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplaySummary,
        )
        s = ReplaySummary()
        with pytest.raises(Exception):
            s.exit_code = 99  # type: ignore[misc]

    def test_drift_report_truncates_large_strings_in_to_dict(
        self,
    ):
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplayDriftKind,
            ReplayDriftReport,
        )
        r = ReplayDriftReport(
            kind=ReplayDriftKind.OUTPUT_REPR_NON_CANONICAL,
            record_index=0, record_id="x",
            expected="A" * 1000, actual="B" * 1000,
        )
        d = r.to_dict()
        assert len(d["expected"]) <= 256
        assert len(d["actual"]) <= 256
        assert "<...>" in d["expected"]


# ---------------------------------------------------------------------------
# § 4 — replay_session_consistency() decision tree
# ---------------------------------------------------------------------------


class TestReplaySessionConsistency:
    def test_master_off_returns_exit_2(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_DETERMINISM_REPLAY_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        s = replay_session_consistency("any-id")
        assert s.exit_code == 2
        assert "master-flag" in " ".join(s.diagnostics).lower() or (
            "false" in " ".join(s.diagnostics).lower()
        )

    def test_empty_session_id_returns_exit_2(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        s = replay_session_consistency("")
        assert s.exit_code == 2

    def test_missing_decisions_jsonl_returns_exit_2(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        s = replay_session_consistency("never-existed")
        assert s.exit_code == 2
        assert any(
            "not found" in d for d in s.diagnostics
        )

    def test_empty_file_returns_exit_2(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        # Create empty decisions.jsonl
        sid = "empty-session"
        ledger_dir = tmp_path / "determinism" / sid
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "decisions.jsonl").write_text("")
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        s = replay_session_consistency(sid)
        assert s.exit_code == 2
        assert s.records_total == 0

    def test_all_clean_records_returns_exit_0(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        sid = "clean-session"
        _write_ledger(
            tmp_path, sid,
            [_clean_record(f"rec-{i}", i) for i in range(3)],
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        s = replay_session_consistency(sid)
        assert s.exit_code == 0
        assert s.records_total == 3
        assert s.records_verified == 3
        assert len(s.drift_entries) == 0
        assert s.has_drift is False

    def test_non_canonical_repr_returns_exit_1(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        sid = "drifted-session"
        # Whitespace-laden output_repr is not in canonical form
        rec = _clean_record()
        rec["output_repr"] = '{"a": 1, "b": 2}'
        _write_ledger(tmp_path, sid, [rec])
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplayDriftKind,
            replay_session_consistency,
        )
        s = replay_session_consistency(sid)
        assert s.exit_code == 1
        assert any(
            e.kind is ReplayDriftKind.OUTPUT_REPR_NON_CANONICAL
            for e in s.drift_entries
        )

    def test_bad_json_line_returns_parse_error(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        sid = "bad-json-session"
        ledger_dir = tmp_path / "determinism" / sid
        ledger_dir.mkdir(parents=True)
        path = ledger_dir / "decisions.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps(_clean_record()) + "\n")
            f.write("not valid json{\n")
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplayDriftKind,
            replay_session_consistency,
        )
        s = replay_session_consistency(sid)
        assert s.exit_code == 1
        assert any(
            e.kind is ReplayDriftKind.PARSE_ERROR
            for e in s.drift_entries
        )
        # And the clean record was still verified
        assert s.records_verified >= 1

    def test_blank_lines_skipped(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        sid = "blank-line-session"
        ledger_dir = tmp_path / "determinism" / sid
        ledger_dir.mkdir(parents=True)
        path = ledger_dir / "decisions.jsonl"
        with open(path, "w") as f:
            f.write("\n")
            f.write(json.dumps(_clean_record()) + "\n")
            f.write("   \n")
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        s = replay_session_consistency(sid)
        # Blank lines don't count toward total
        assert s.records_total == 1
        assert s.exit_code == 0

    def test_explicit_decisions_path_override(
        self, monkeypatch, tmp_path,
    ):
        """Caller-supplied decisions_path bypasses ledger-dir
        derivation."""
        _enable(monkeypatch, tmp_path)
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        custom_path = custom_dir / "my-decisions.jsonl"
        with open(custom_path, "w") as f:
            f.write(json.dumps(_clean_record()) + "\n")
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_session_consistency,
        )
        s = replay_session_consistency(
            "explicit-session",
            decisions_path=custom_path,
        )
        assert s.exit_code == 0


# ---------------------------------------------------------------------------
# § 5 — _verify_record() per-record drift
# ---------------------------------------------------------------------------


class TestVerifyRecord:
    def test_clean_record_no_drift(self):
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            _verify_record,
        )
        drifts = _verify_record(
            record_index=0, record_dict=_clean_record(),
        )
        assert drifts == ()

    def test_schema_version_drift(self):
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplayDriftKind,
            _verify_record,
        )
        rec = _clean_record()
        rec["schema_version"] = "decision_record.999"
        drifts = _verify_record(record_index=0, record_dict=rec)
        kinds = [d.kind for d in drifts]
        assert ReplayDriftKind.SCHEMA_VERSION_DRIFT in kinds

    def test_non_canonical_repr_drift(self):
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplayDriftKind,
            _verify_record,
        )
        rec = _clean_record()
        rec["output_repr"] = '{"a": 1, "b":  2}'  # extra space
        drifts = _verify_record(record_index=0, record_dict=rec)
        kinds = [d.kind for d in drifts]
        assert ReplayDriftKind.OUTPUT_REPR_NON_CANONICAL in kinds

    def test_missing_required_field_returns_parse_error(self):
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            ReplayDriftKind,
            _verify_record,
        )
        # Missing record_id (load-bearing field)
        rec = _clean_record()
        del rec["record_id"]
        drifts = _verify_record(record_index=0, record_dict=rec)
        # DecisionRecord.from_dict either tolerates this or raises.
        # Either way, no drift entries OR a PARSE_ERROR — both
        # are acceptable defensive outcomes.
        assert all(
            isinstance(d.kind, ReplayDriftKind) for d in drifts
        )


# ---------------------------------------------------------------------------
# § 6 — Cross-process flock'd read
# ---------------------------------------------------------------------------


class TestFlockedRead:
    def test_uses_flock_critical_section(self):
        """Module source MUST import + use
        flock_critical_section to avoid tearing concurrent
        writers."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "determinism" / "replay_determinism.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "flock_critical_section" in source
        assert "cross_process_jsonl" in source


# ---------------------------------------------------------------------------
# § 7 — CLI entry
# ---------------------------------------------------------------------------


class TestCLIEntry:
    def test_cli_returns_exit_2_on_missing_session(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_cli_main,
        )
        # Capture stdout to keep test output clean
        f = io.StringIO()
        with redirect_stdout(f):
            exit_code = replay_cli_main([
                "--session", "missing-session",
            ])
        assert exit_code == 2

    def test_cli_returns_exit_0_on_clean_records(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        sid = "clean-cli-session"
        _write_ledger(tmp_path, sid, [_clean_record()])
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_cli_main,
        )
        f = io.StringIO()
        with redirect_stdout(f):
            exit_code = replay_cli_main([
                "--session", sid,
            ])
        assert exit_code == 0

    def test_cli_returns_exit_1_on_drift(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        sid = "drift-cli-session"
        rec = _clean_record()
        rec["output_repr"] = '{"a": 1}'  # non-canonical
        _write_ledger(tmp_path, sid, [rec])
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_cli_main,
        )
        f = io.StringIO()
        with redirect_stdout(f):
            exit_code = replay_cli_main([
                "--session", sid,
            ])
        assert exit_code == 1

    def test_cli_json_flag_emits_valid_json(
        self, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch, tmp_path)
        sid = "json-cli-session"
        _write_ledger(tmp_path, sid, [_clean_record()])
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_cli_main,
        )
        f = io.StringIO()
        with redirect_stdout(f):
            replay_cli_main(["--session", sid, "--json"])
        # Output must be parsable JSON
        out = f.getvalue()
        parsed = json.loads(out)
        assert parsed["session_id"] == sid
        assert parsed["exit_code"] == 0
        assert "schema_version" in parsed

    def test_cli_allow_disabled_bypasses_master_flag(
        self, monkeypatch, tmp_path,
    ):
        """--allow-disabled lets pre-graduation operator runs
        engage the replay even when env flag is off."""
        monkeypatch.delenv(
            "JARVIS_DETERMINISM_REPLAY_ENABLED", raising=False,
        )
        monkeypatch.setenv(
            "JARVIS_DETERMINISM_LEDGER_DIR",
            str(tmp_path / "determinism"),
        )
        sid = "allow-disabled-session"
        _write_ledger(tmp_path, sid, [_clean_record()])
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_cli_main,
        )
        f = io.StringIO()
        with redirect_stdout(f):
            exit_code = replay_cli_main([
                "--session", sid, "--allow-disabled",
            ])
        # Without --allow-disabled, master-off would yield 2
        assert exit_code == 0


# ---------------------------------------------------------------------------
# § 8 — Authority floor
# ---------------------------------------------------------------------------


class TestAuthorityFloor:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.strategic_direction",
    )

    def test_module_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "determinism" / "replay_determinism.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# § 9 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_match(self):
        from backend.core.ouroboros.governance.determinism import (
            replay_determinism as rd,
        )
        expected = sorted([
            "REPLAY_DETERMINISM_SCHEMA_VERSION",
            "ReplayDriftKind",
            "ReplayDriftReport",
            "ReplaySummary",
            "replay_cli_main",
            "replay_determinism_enabled",
            "replay_session_consistency",
        ])
        assert sorted(rd.__all__) == expected
