"""Tests for scripts/ignite_a1_soak.py -- pure-helper unit tests (no Docker, no soak)."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Make the repo root importable so we can import the script as a module.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from ignite_a1_soak import (  # noqa: E402
    docker_responsive,
    find_failure_telemetry,
    format_for_claude,
    tee_run,
)


# ---------------------------------------------------------------------------
# docker_responsive -- probe injection
# ---------------------------------------------------------------------------

class TestDockerResponsive:
    def test_probe_ok(self):
        ok, reason = docker_responsive(probe=lambda: (True, "ok"))
        assert ok is True
        assert reason == "ok"

    def test_probe_down(self):
        ok, reason = docker_responsive(probe=lambda: (False, "down"))
        assert ok is False
        assert reason == "down"

    def test_probe_callable_used_not_real_subprocess(self):
        """Ensure the probe bypasses the real subprocess path."""
        calls = []

        def counting_probe():
            calls.append(1)
            return (True, "stub")

        ok, reason = docker_responsive(probe=counting_probe)
        assert ok is True
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# find_failure_telemetry
# ---------------------------------------------------------------------------

_VALID_TELEMETRY = {
    "fsm_phase": "APPLY",
    "causal_chain": [{"seq": 1, "causal_parent_seq": 0}],
    "memory_snapshot": {"level": "OK"},
    "a1trace_hops": ["emit", "ingest"],
}


def _write_telemetry(root: Path, stamp: str, data: dict) -> Path:
    subdir = root / "r1" / "telemetry" / f"failure_telemetry_{stamp}"
    subdir.mkdir(parents=True, exist_ok=True)
    p = subdir / "failure_telemetry.json"
    p.write_text(json.dumps(data))
    return p


class TestFindFailureTelemetry:
    def test_finds_artifact(self, tmp_path):
        _write_telemetry(tmp_path, "20260630T0000Z", _VALID_TELEMETRY)
        result = find_failure_telemetry(tmp_path)
        assert result is not None
        assert result["fsm_phase"] == "APPLY"
        assert "_artifact_path" in result
        assert result["causal_chain"] == [{"seq": 1, "causal_parent_seq": 0}]
        assert result["a1trace_hops"] == ["emit", "ingest"]

    def test_returns_none_when_absent(self, tmp_path):
        result = find_failure_telemetry(tmp_path)
        assert result is None

    def test_picks_newest_when_two_exist(self, tmp_path):
        import os

        # Write first artifact then backdate it so it's definitively older
        p1 = _write_telemetry(tmp_path, "20260630T0000Z", {**_VALID_TELEMETRY, "fsm_phase": "VALIDATE"})
        old_time = time.time() - 10
        os.utime(str(p1), (old_time, old_time))

        # Write second artifact -- newer by wall clock
        _write_telemetry(tmp_path, "20260630T0001Z", {**_VALID_TELEMETRY, "fsm_phase": "GENERATE"})

        result = find_failure_telemetry(tmp_path)
        assert result is not None
        assert result["fsm_phase"] == "GENERATE"

    def test_skips_missing_fsm_phase(self, tmp_path):
        """A JSON file without fsm_phase must be skipped."""
        bad_dir = tmp_path / "r1" / "telemetry" / "failure_telemetry_bad"
        bad_dir.mkdir(parents=True)
        (bad_dir / "failure_telemetry.json").write_text(json.dumps({"other": "key"}))
        result = find_failure_telemetry(tmp_path)
        assert result is None

    def test_artifact_path_is_string(self, tmp_path):
        _write_telemetry(tmp_path, "20260630T0002Z", _VALID_TELEMETRY)
        result = find_failure_telemetry(tmp_path)
        assert isinstance(result["_artifact_path"], str)


# ---------------------------------------------------------------------------
# format_for_claude
# ---------------------------------------------------------------------------

class TestFormatForClaude:
    _LOG_TAIL = ["line A", "line B", "line C"]

    def test_with_telemetry_contains_marker(self, tmp_path):
        tel = {**_VALID_TELEMETRY, "_artifact_path": str(tmp_path / "failure_telemetry.json")}
        out = format_for_claude(tel, log_path="/logs/run.log", exit_code=1, log_tail=self._LOG_TAIL)
        assert "[FOR CLAUDE]" in out

    def test_with_telemetry_contains_fsm_phase(self, tmp_path):
        tel = {**_VALID_TELEMETRY, "_artifact_path": str(tmp_path / "t.json")}
        out = format_for_claude(tel, log_path="/logs/run.log", exit_code=1, log_tail=self._LOG_TAIL)
        assert "APPLY" in out

    def test_with_telemetry_contains_log_path(self, tmp_path):
        tel = {**_VALID_TELEMETRY, "_artifact_path": str(tmp_path / "t.json")}
        out = format_for_claude(tel, log_path="/logs/run.log", exit_code=99, log_tail=self._LOG_TAIL)
        assert "/logs/run.log" in out

    def test_with_telemetry_contains_hop_count(self, tmp_path):
        tel = {**_VALID_TELEMETRY, "_artifact_path": str(tmp_path / "t.json")}
        out = format_for_claude(tel, log_path="/logs/run.log", exit_code=1, log_tail=self._LOG_TAIL)
        assert "2 total" in out  # 2 hops in _VALID_TELEMETRY

    def test_with_telemetry_contains_log_tail(self, tmp_path):
        tel = {**_VALID_TELEMETRY, "_artifact_path": str(tmp_path / "t.json")}
        out = format_for_claude(tel, log_path="/logs/run.log", exit_code=1, log_tail=self._LOG_TAIL)
        for line in self._LOG_TAIL:
            assert line in out

    def test_none_telemetry_contains_marker(self):
        out = format_for_claude(None, log_path="/logs/run.log", exit_code=1, log_tail=self._LOG_TAIL)
        assert "[FOR CLAUDE]" in out

    def test_none_telemetry_contains_artifact_not_found(self):
        out = format_for_claude(None, log_path="/logs/run.log", exit_code=1, log_tail=self._LOG_TAIL)
        # Must mention no telemetry found
        assert "No failure_telemetry" in out or "artifact" in out.lower()

    def test_none_telemetry_contains_log_tail(self):
        out = format_for_claude(None, log_path="/logs/run.log", exit_code=1, log_tail=self._LOG_TAIL)
        for line in self._LOG_TAIL:
            assert line in out


# ---------------------------------------------------------------------------
# tee_run -- live subprocess, proves merged tee + zero loss
# ---------------------------------------------------------------------------

class TestTeeRun:
    def test_returns_zero_on_success(self, tmp_path):
        log_path = tmp_path / "tee.log"
        with log_path.open("w") as fh:
            rc = tee_run(
                [sys.executable, "-c", "print('hello')"],
                fh,
            )
        assert rc == 0

    def test_stdout_and_stderr_both_captured(self, tmp_path):
        log_path = tmp_path / "tee.log"
        with log_path.open("w") as fh:
            rc = tee_run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('hello'); sys.stderr.write('werr\\n')",
                ],
                fh,
            )
        assert rc == 0
        content = log_path.read_text()
        assert "hello" in content
        assert "werr" in content

    def test_returns_nonzero_exit_code(self, tmp_path):
        log_path = tmp_path / "tee.log"
        with log_path.open("w") as fh:
            rc = tee_run(
                [sys.executable, "-c", "raise SystemExit(42)"],
                fh,
            )
        assert rc == 42

    def test_writes_to_both_stdout_and_log(self, tmp_path, capsys):
        log_path = tmp_path / "tee.log"
        with log_path.open("w") as fh:
            tee_run(
                [sys.executable, "-c", "print('dualwrite')"],
                fh,
            )
        captured = capsys.readouterr()
        assert "dualwrite" in captured.out
        assert "dualwrite" in log_path.read_text()
