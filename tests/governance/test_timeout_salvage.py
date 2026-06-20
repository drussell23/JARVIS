"""Completed-work salvage on subprocess timeout (live-soak blocker, 2026-06-20).

A graduation soak finished its work and wrote session_outcome=complete, then hung
in post-summary cleanup (leaked asyncio shutdown tasks) until the hard subprocess
kill cap → TimeoutExpired. Pre-fix, the harness discarded the COMPLETE summary
and recorded outcome=infra → no soak ever counted clean. The salvage grades the
finished soak on its real summary; a genuine hang (no complete summary) still
propagates the timeout.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.graduation import live_fire_soak as LFS


def _make_script(root: Path) -> None:
    # _run_battle_test_subprocess early-returns -1 if the script is absent.
    s = root / "scripts" / "ouroboros_battle_test.py"
    s.parent.mkdir(parents=True, exist_ok=True)
    s.write_text("# stub\n")


def _write_session(root: Path, sid: str, outcome: str, stop_reason: str) -> None:
    d = root / ".ouroboros" / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "session_id": sid,
        "session_outcome": outcome,
        "stop_reason": stop_reason,
        "duration_s": 2445.0,
        "cost_total": 0.003,
    }))
    (d / "debug.log").write_text("phase=COMPLETE state=applied\n")


def test_salvage_grades_complete_summary_on_timeout(tmp_path, monkeypatch):
    _make_script(tmp_path)
    monkeypatch.setenv("JARVIS_LIVE_FIRE_TIMEOUT_SALVAGE_ENABLED", "true")
    _write_session(tmp_path, "bt-complete", "complete", "wall_clock_cap")
    # subprocess.run raises TimeoutExpired (process hung in cleanup after summary).
    monkeypatch.setattr(
        LFS.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="x", timeout=2700)
        ),
    )
    # ensure the session mtime is >= the run anchor
    exit_code, summary, _tail = LFS._run_battle_test_subprocess(
        env={}, cost_cap_usd=1.0, max_wall_seconds=2400,
        timeout_s=2700, project_root=tmp_path,
    )
    assert exit_code == 0
    assert summary.get("session_outcome") == "complete"
    assert summary.get("stop_reason") == "wall_clock_cap"


def test_genuine_hang_no_complete_summary_propagates(tmp_path, monkeypatch):
    _make_script(tmp_path)
    monkeypatch.setenv("JARVIS_LIVE_FIRE_TIMEOUT_SALVAGE_ENABLED", "true")
    # An incomplete_kill summary is NOT salvageable (work didn't finish).
    _write_session(tmp_path, "bt-killed", "incomplete_kill", "sigterm")
    monkeypatch.setattr(
        LFS.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="x", timeout=2700)
        ),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        LFS._run_battle_test_subprocess(
            env={}, cost_cap_usd=1.0, max_wall_seconds=2400,
            timeout_s=2700, project_root=tmp_path,
        )


def test_no_summary_at_all_propagates(tmp_path, monkeypatch):
    _make_script(tmp_path)
    monkeypatch.setenv("JARVIS_LIVE_FIRE_TIMEOUT_SALVAGE_ENABLED", "true")
    monkeypatch.setattr(
        LFS.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="x", timeout=2700)
        ),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        LFS._run_battle_test_subprocess(
            env={}, cost_cap_usd=1.0, max_wall_seconds=2400,
            timeout_s=2700, project_root=tmp_path,
        )


def test_salvage_disabled_propagates_even_with_complete(tmp_path, monkeypatch):
    _make_script(tmp_path)
    monkeypatch.setenv("JARVIS_LIVE_FIRE_TIMEOUT_SALVAGE_ENABLED", "false")
    _write_session(tmp_path, "bt-complete", "complete", "wall_clock_cap")
    monkeypatch.setattr(
        LFS.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="x", timeout=2700)
        ),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        LFS._run_battle_test_subprocess(
            env={}, cost_cap_usd=1.0, max_wall_seconds=2400,
            timeout_s=2700, project_root=tmp_path,
        )


def test_salvage_default_enabled(monkeypatch):
    monkeypatch.delenv("JARVIS_LIVE_FIRE_TIMEOUT_SALVAGE_ENABLED", raising=False)
    assert LFS._timeout_salvage_enabled() is True


def test_normal_exit_unaffected(tmp_path, monkeypatch):
    # No timeout → legacy path: returns the real exit code + parsed summary.
    _make_script(tmp_path)
    _write_session(tmp_path, "bt-ok", "complete", "idle_timeout")

    class _Proc:
        returncode = 0
    monkeypatch.setattr(LFS.subprocess, "run", lambda *a, **k: _Proc())
    exit_code, summary, _ = LFS._run_battle_test_subprocess(
        env={}, cost_cap_usd=1.0, max_wall_seconds=2400,
        timeout_s=2700, project_root=tmp_path,
    )
    assert exit_code == 0
    assert summary.get("session_outcome") == "complete"
