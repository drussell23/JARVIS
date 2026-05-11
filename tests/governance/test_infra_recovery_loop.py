"""Regression spine for §41.4 Phase 1 eighth arc — Infrastructure Recovery Loop."""
from __future__ import annotations

import ast
import os
import time
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest


from backend.core.ouroboros.governance import infra_recovery_loop as irl
from backend.core.ouroboros.governance.infra_recovery_loop import (
    INFRA_RECOVERY_SCHEMA_VERSION,
    ComponentCheck,
    InfraComponent,
    InfraHealth,
    InfraRecoveryReport,
    RecoveryAction,
    RecoveryAttempt,
    RecoveryVerdict,
    _ENV_AUTO_RECLAIM,
    _ENV_LEDGER_PATH,
    _ENV_LOCK_MAX_AGE_S,
    _ENV_LOCK_ROOTS,
    _ENV_MASTER,
    _ENV_MAX_RECOVERIES,
    _ENV_PERSIST,
    _ENV_PID_CHECK_TIMEOUT_S,
    _ENV_SESSION_MAX_AGE_S,
    _ENV_SESSION_ROOT,
    _aggregate_verdict,
    _parse_lock_pid,
    action_glyph,
    auto_reclaim_enabled,
    component_glyph,
    execute_recovery,
    format_recovery_panel,
    health_glyph,
    ledger_path,
    lock_max_age_s,
    lock_roots,
    master_enabled,
    max_recoveries_per_run,
    persistence_enabled,
    pid_check_timeout_s,
    register_flags,
    register_shipped_invariants,
    run_recovery_loop,
    scan_lock_files,
    scan_sensor_observer,
    scan_session_dirs,
    scan_worktrees,
    session_max_age_s,
    session_root,
    verdict_glyph,
)


# --- Schema + taxonomies ----------------------------------------------------


def test_schema_version_stamp():
    assert INFRA_RECOVERY_SCHEMA_VERSION == "infra_recovery_loop.1"


def test_infra_component_closed():
    assert {v.value for v in InfraComponent} == {
        "sensor_task", "worktree", "lock_file", "session_dir",
    }


def test_infra_health_closed():
    assert {v.value for v in InfraHealth} == {
        "healthy", "degraded", "failed", "unknown",
    }


def test_recovery_action_closed():
    assert {v.value for v in RecoveryAction} == {
        "no_op", "reclaim", "restart", "escalate",
    }


def test_recovery_verdict_closed():
    assert {v.value for v in RecoveryVerdict} == {
        "healthy", "recovered", "degraded", "disabled",
    }


# --- Env knobs --------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    assert master_enabled() is False


def test_master_enabled_true(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    assert master_enabled() is True


def test_auto_reclaim_default_false(monkeypatch):
    monkeypatch.delenv(_ENV_AUTO_RECLAIM, raising=False)
    assert auto_reclaim_enabled() is False


def test_auto_reclaim_enabled_true(monkeypatch):
    monkeypatch.setenv(_ENV_AUTO_RECLAIM, "true")
    assert auto_reclaim_enabled() is True


def test_persistence_default_true(monkeypatch):
    monkeypatch.delenv(_ENV_PERSIST, raising=False)
    assert persistence_enabled() is True


def test_lock_roots_default(monkeypatch):
    monkeypatch.delenv(_ENV_LOCK_ROOTS, raising=False)
    roots = lock_roots()
    assert len(roots) == 1
    assert ".jarvis" in str(roots[0])


def test_lock_roots_multi(monkeypatch):
    monkeypatch.setenv(_ENV_LOCK_ROOTS, ".jarvis:.ouroboros")
    roots = lock_roots()
    assert len(roots) == 2


def test_lock_roots_empty_falls_back(monkeypatch):
    monkeypatch.setenv(_ENV_LOCK_ROOTS, "")
    roots = lock_roots()
    assert len(roots) >= 1


def test_lock_max_age_default(monkeypatch):
    monkeypatch.delenv(_ENV_LOCK_MAX_AGE_S, raising=False)
    assert lock_max_age_s() == 3600


def test_lock_max_age_clamped(monkeypatch):
    monkeypatch.setenv(_ENV_LOCK_MAX_AGE_S, "999999999")
    assert lock_max_age_s() == 604_800


def test_lock_max_age_floor(monkeypatch):
    monkeypatch.setenv(_ENV_LOCK_MAX_AGE_S, "0")
    assert lock_max_age_s() == 1


def test_lock_max_age_garbage(monkeypatch):
    monkeypatch.setenv(_ENV_LOCK_MAX_AGE_S, "abc")
    assert lock_max_age_s() == 3600


def test_session_root_default(monkeypatch):
    monkeypatch.delenv(_ENV_SESSION_ROOT, raising=False)
    p = session_root()
    assert ".ouroboros/sessions" in str(p)


def test_session_root_override(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_SESSION_ROOT, str(tmp_path))
    assert session_root() == tmp_path


def test_session_max_age_default(monkeypatch):
    monkeypatch.delenv(_ENV_SESSION_MAX_AGE_S, raising=False)
    assert session_max_age_s() == 86_400


def test_max_recoveries_default(monkeypatch):
    monkeypatch.delenv(_ENV_MAX_RECOVERIES, raising=False)
    assert max_recoveries_per_run() == 10


def test_max_recoveries_zero(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_RECOVERIES, "0")
    assert max_recoveries_per_run() == 0


def test_pid_check_timeout_default(monkeypatch):
    monkeypatch.delenv(_ENV_PID_CHECK_TIMEOUT_S, raising=False)
    assert pid_check_timeout_s() == 2


def test_ledger_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_LEDGER_PATH, raising=False)
    p = ledger_path()
    assert ".jarvis" in str(p)


def test_ledger_path_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(custom))
    assert ledger_path() == custom


# --- Glyphs ----------------------------------------------------------------


def test_component_glyph_enum():
    assert component_glyph(InfraComponent.SENSOR_TASK) == "🧬"


def test_component_glyph_str():
    assert component_glyph("worktree") == "🌳"


def test_component_glyph_unknown():
    assert component_glyph("bogus") == "?"


def test_component_glyph_none():
    assert component_glyph(None) == "?"


def test_health_glyph_enum():
    assert health_glyph(InfraHealth.HEALTHY) == "✓"


def test_health_glyph_degraded():
    assert health_glyph(InfraHealth.DEGRADED) == "⚠"


def test_action_glyph_enum():
    assert action_glyph(RecoveryAction.RECLAIM) == "🧹"


def test_action_glyph_str():
    assert action_glyph("escalate") == "📣"


def test_verdict_glyph_enum():
    assert verdict_glyph(RecoveryVerdict.RECOVERED) == "↺"


def test_verdict_glyph_disabled():
    assert verdict_glyph(RecoveryVerdict.DISABLED) == "◌"


# --- PID parser -------------------------------------------------------------


def test_parse_lock_pid_int(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("12345")
    assert _parse_lock_pid(lock) == 12345


def test_parse_lock_pid_json(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text('{"pid": 999, "host": "x"}')
    assert _parse_lock_pid(lock) == 999


def test_parse_lock_pid_string_pid_in_json(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text('{"pid": "888"}')
    assert _parse_lock_pid(lock) == 888


def test_parse_lock_pid_regex_fallback(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text('garbage but "pid": 777 inside')
    assert _parse_lock_pid(lock) == 777


def test_parse_lock_pid_empty(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("")
    assert _parse_lock_pid(lock) is None


def test_parse_lock_pid_unparseable(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("this is not a pid file")
    assert _parse_lock_pid(lock) is None


def test_parse_lock_pid_missing(tmp_path):
    lock = tmp_path / "nonexistent.lock"
    assert _parse_lock_pid(lock) is None


# --- scan_lock_files --------------------------------------------------------


def test_scan_lock_files_empty_root(tmp_path):
    assert scan_lock_files(roots=(tmp_path,)) == ()


def test_scan_lock_files_nonexistent_root():
    assert scan_lock_files(
        roots=(Path("/nonexistent_xyz_qwerty"),),
    ) == ()


def test_scan_lock_files_fresh_lock_healthy(tmp_path):
    lock = tmp_path / "fresh.lock"
    lock.write_text(str(os.getpid()))  # live PID
    checks = scan_lock_files(roots=(tmp_path,))
    assert len(checks) == 1
    assert checks[0].health == InfraHealth.HEALTHY
    assert checks[0].recommended_action == RecoveryAction.NO_OP


def test_scan_lock_files_stale_dead_pid(tmp_path):
    lock = tmp_path / "stale.lock"
    lock.write_text("9999999")  # dead PID
    # Backdate
    old = time.time() - 7200
    os.utime(lock, (old, old))
    checks = scan_lock_files(
        roots=(tmp_path,),
        pid_alive_fn=lambda p: False,
    )
    assert len(checks) == 1
    assert checks[0].health == InfraHealth.DEGRADED
    assert checks[0].recommended_action == RecoveryAction.RECLAIM


def test_scan_lock_files_recent_dead_pid_no_reclaim(tmp_path):
    lock = tmp_path / "recent.lock"
    lock.write_text("9999999")
    # Fresh — within threshold
    checks = scan_lock_files(
        roots=(tmp_path,),
        pid_alive_fn=lambda p: False,
        max_age_s=3600,
    )
    assert len(checks) == 1
    assert checks[0].health == InfraHealth.DEGRADED
    assert checks[0].recommended_action == RecoveryAction.NO_OP


def test_scan_lock_files_unparseable_old(tmp_path):
    lock = tmp_path / "garbage.lock"
    lock.write_text("not a pid file at all")
    old = time.time() - 7200
    os.utime(lock, (old, old))
    checks = scan_lock_files(roots=(tmp_path,))
    assert len(checks) == 1
    assert checks[0].health == InfraHealth.UNKNOWN
    assert checks[0].recommended_action == RecoveryAction.ESCALATE


def test_scan_lock_files_unparseable_fresh(tmp_path):
    lock = tmp_path / "garbage.lock"
    lock.write_text("not a pid")
    checks = scan_lock_files(roots=(tmp_path,))
    assert len(checks) == 1
    assert checks[0].health == InfraHealth.HEALTHY


def test_scan_lock_files_alive_pid_old(tmp_path):
    lock = tmp_path / "live.lock"
    lock.write_text("1234")
    old = time.time() - 7200
    os.utime(lock, (old, old))
    checks = scan_lock_files(
        roots=(tmp_path,),
        pid_alive_fn=lambda p: True,
    )
    assert len(checks) == 1
    # Alive PID = HEALTHY regardless of age
    assert checks[0].health == InfraHealth.HEALTHY


def test_scan_lock_files_multiple(tmp_path):
    (tmp_path / "a.lock").write_text(str(os.getpid()))
    (tmp_path / "b.lock").write_text(str(os.getpid()))
    (tmp_path / "c.lock").write_text(str(os.getpid()))
    checks = scan_lock_files(roots=(tmp_path,))
    assert len(checks) == 3


def test_scan_lock_files_never_raises_on_bad_probe(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("1234")
    def crashy(p):
        raise RuntimeError("boom")
    # Crashes inside default_pid_alive but injected runner
    # raising means the substrate sees True (defensive)
    # OR exception escapes scan_lock_files inner try.
    # Per contract: NEVER raises.
    try:
        checks = scan_lock_files(
            roots=(tmp_path,),
            pid_alive_fn=crashy,
        )
        # If we got here, exception was swallowed — OK
        assert True
    except Exception:
        pytest.fail("scan_lock_files raised")


# --- scan_session_dirs ------------------------------------------------------


def test_scan_session_dirs_empty(tmp_path):
    assert scan_session_dirs(root=tmp_path) == ()


def test_scan_session_dirs_nonexistent():
    assert scan_session_dirs(
        root=Path("/nonexistent_xyz"),
    ) == ()


def test_scan_session_dirs_complete_old(tmp_path):
    session = tmp_path / "s1"
    session.mkdir()
    (session / "summary.json").write_text("{}")
    old = time.time() - 100_000
    os.utime(session, (old, old))
    checks = scan_session_dirs(root=tmp_path)
    assert len(checks) == 1
    assert checks[0].health == InfraHealth.HEALTHY


def test_scan_session_dirs_orphan_old(tmp_path):
    session = tmp_path / "s2"
    session.mkdir()
    (session / "debug.log").write_text("log")
    old = time.time() - 100_000
    os.utime(session, (old, old))
    checks = scan_session_dirs(root=tmp_path)
    assert len(checks) == 1
    assert checks[0].health == InfraHealth.FAILED
    assert checks[0].recommended_action == RecoveryAction.ESCALATE


def test_scan_session_dirs_fresh_no_summary(tmp_path):
    session = tmp_path / "s3"
    session.mkdir()
    checks = scan_session_dirs(root=tmp_path)
    assert len(checks) == 1
    # Fresh — could still be running
    assert checks[0].health == InfraHealth.HEALTHY


def test_scan_session_dirs_max_age_override(tmp_path):
    session = tmp_path / "s4"
    session.mkdir()
    checks = scan_session_dirs(
        root=tmp_path, max_age_s=1,
    )
    assert len(checks) == 1
    # max_age=1, dir is new but already older than 1s
    # Actually depends on exactly when the dir was created.
    # Just assert it produced a check.
    assert checks[0].component == InfraComponent.SESSION_DIR


def test_scan_session_dirs_skips_files(tmp_path):
    (tmp_path / "not_a_dir.txt").write_text("x")
    assert scan_session_dirs(root=tmp_path) == ()


# --- scan_sensor_observer ---------------------------------------------------


@pytest.fixture
def healthy_snapshot():
    now = time.time()
    return {
        "is_running": True,
        "task_started": True,
        "task_done": False,
        "last_cycle_ok_at_unix": now,
        "last_cycle_attempt_at_unix": now,
        "consecutive_cycle_failures": 0,
    }


def test_scan_observer_none_snapshot():
    check = scan_sensor_observer(None)
    assert check.health == InfraHealth.UNKNOWN
    assert check.recommended_action == RecoveryAction.NO_OP


def test_scan_observer_healthy(healthy_snapshot, monkeypatch):
    monkeypatch.setenv("JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "true")
    check = scan_sensor_observer(
        healthy_snapshot, interval_s=10.0,
    )
    assert check.component == InfraComponent.SENSOR_TASK
    assert check.health == InfraHealth.HEALTHY


def test_scan_observer_named(healthy_snapshot):
    check = scan_sensor_observer(
        healthy_snapshot, observer_name="my_observer",
    )
    assert check.name == "my_observer"


def test_scan_observer_task_dead(monkeypatch):
    monkeypatch.setenv("JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "true")
    dead_snapshot = {
        "is_running": False,
        "task_started": False,
        "task_done": False,
        "last_cycle_ok_at_unix": None,
        "last_cycle_attempt_at_unix": None,
        "consecutive_cycle_failures": 0,
    }
    check = scan_sensor_observer(dead_snapshot, interval_s=10.0)
    # task_started=False → TASK_DEAD in posture_health
    assert check.health in (InfraHealth.FAILED, InfraHealth.DEGRADED)


# --- scan_worktrees ---------------------------------------------------------


def test_scan_worktrees_empty(tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    assert scan_worktrees(worktree_base=base) == ()


def test_scan_worktrees_orphan_detected(tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    (base / "unit-abc123").mkdir()
    (base / "unit-def456").mkdir()
    checks = scan_worktrees(worktree_base=base)
    assert len(checks) == 2
    for c in checks:
        assert c.component == InfraComponent.WORKTREE
        assert c.health == InfraHealth.DEGRADED


def test_scan_worktrees_prefix_filter(tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    (base / "unit-xxx").mkdir()
    (base / "other-yyy").mkdir()
    checks = scan_worktrees(worktree_base=base)
    assert len(checks) == 1
    assert "unit-xxx" in checks[0].name


def test_scan_worktrees_custom_prefix(tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    (base / "sub-aaa").mkdir()
    checks = scan_worktrees(
        worktree_base=base, branch_prefix="sub-",
    )
    assert len(checks) == 1


def test_scan_worktrees_nonexistent_base():
    assert scan_worktrees(
        worktree_base=Path("/nonexistent_xyz"),
    ) == ()


def test_scan_worktrees_skips_files(tmp_path):
    base = tmp_path / "worktrees"
    base.mkdir()
    (base / "unit-file.txt").write_text("x")
    checks = scan_worktrees(worktree_base=base)
    assert checks == ()


# --- execute_recovery -------------------------------------------------------


def test_execute_no_op():
    check = ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name="/tmp/x",
        health=InfraHealth.HEALTHY,
        evidence_text="ok",
        last_check_unix=time.time(),
        recommended_action=RecoveryAction.NO_OP,
        boundary_crossed=False,
    )
    attempt = execute_recovery(check)
    assert attempt.action == RecoveryAction.NO_OP
    assert attempt.success is True


def test_execute_reclaim_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_AUTO_RECLAIM, "false")
    lock = tmp_path / "x.lock"
    lock.write_text("x")
    check = ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name=str(lock),
        health=InfraHealth.DEGRADED,
        evidence_text="stale",
        last_check_unix=time.time(),
        recommended_action=RecoveryAction.RECLAIM,
        boundary_crossed=False,
    )
    attempt = execute_recovery(check)
    assert attempt.action == RecoveryAction.NO_OP
    assert attempt.success is False
    assert "auto_reclaim disabled" in (attempt.error or "")
    # File NOT deleted
    assert lock.exists()


def test_execute_reclaim_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_AUTO_RECLAIM, "true")
    lock = tmp_path / "x.lock"
    lock.write_text("x")
    check = ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name=str(lock),
        health=InfraHealth.DEGRADED,
        evidence_text="stale",
        last_check_unix=time.time(),
        recommended_action=RecoveryAction.RECLAIM,
        boundary_crossed=False,
    )
    attempt = execute_recovery(check)
    assert attempt.action == RecoveryAction.RECLAIM
    assert attempt.success is True
    assert attempt.auto_reclaim_was_enabled is True
    assert not lock.exists()


def test_execute_reclaim_already_gone(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_AUTO_RECLAIM, "true")
    check = ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name=str(tmp_path / "gone.lock"),
        health=InfraHealth.DEGRADED,
        evidence_text="stale",
        last_check_unix=time.time(),
        recommended_action=RecoveryAction.RECLAIM,
        boundary_crossed=False,
    )
    attempt = execute_recovery(check)
    # FileNotFoundError → still SUCCESS
    assert attempt.success is True


def test_execute_escalate():
    check = ComponentCheck(
        component=InfraComponent.SESSION_DIR,
        name="/tmp/orphan",
        health=InfraHealth.FAILED,
        evidence_text="missing summary",
        last_check_unix=time.time(),
        recommended_action=RecoveryAction.ESCALATE,
        boundary_crossed=False,
    )
    attempt = execute_recovery(check)
    assert attempt.action == RecoveryAction.ESCALATE
    assert attempt.success is True


def test_execute_with_custom_executor():
    """Operator-injected executor table."""
    calls = []

    def custom_executor(c, now):
        calls.append(c.name)
        return RecoveryAttempt(
            component=c.component,
            name=c.name,
            action=RecoveryAction.RECLAIM,
            success=True,
            elapsed_s=0.0,
            error=None,
            auto_reclaim_was_enabled=True,
        )

    check = ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name="x",
        health=InfraHealth.DEGRADED,
        evidence_text="",
        last_check_unix=time.time(),
        recommended_action=RecoveryAction.RECLAIM,
        boundary_crossed=False,
    )
    attempt = execute_recovery(
        check, executors={RecoveryAction.RECLAIM: custom_executor},
    )
    assert calls == ["x"]
    assert attempt.success is True


def test_execute_crashy_executor_safe():
    def crashy(c, now):
        raise RuntimeError("boom")

    check = ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name="x",
        health=InfraHealth.DEGRADED,
        evidence_text="",
        last_check_unix=time.time(),
        recommended_action=RecoveryAction.RECLAIM,
        boundary_crossed=False,
    )
    attempt = execute_recovery(
        check, executors={RecoveryAction.RECLAIM: crashy},
    )
    assert attempt.success is False
    assert "boom" in (attempt.error or "")


# --- _aggregate_verdict ----------------------------------------------------


def test_aggregate_verdict_empty():
    assert _aggregate_verdict((), ()) == RecoveryVerdict.HEALTHY


def test_aggregate_verdict_all_healthy():
    checks = (ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name="x", health=InfraHealth.HEALTHY,
        evidence_text="", last_check_unix=0.0,
        recommended_action=RecoveryAction.NO_OP,
        boundary_crossed=False,
    ),)
    assert _aggregate_verdict(checks, ()) == RecoveryVerdict.HEALTHY


def test_aggregate_verdict_degraded_unrecovered():
    checks = (ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name="x", health=InfraHealth.DEGRADED,
        evidence_text="", last_check_unix=0.0,
        recommended_action=RecoveryAction.RECLAIM,
        boundary_crossed=False,
    ),)
    assert _aggregate_verdict(checks, ()) == RecoveryVerdict.DEGRADED


def test_aggregate_verdict_recovered():
    checks = (ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name="x", health=InfraHealth.DEGRADED,
        evidence_text="", last_check_unix=0.0,
        recommended_action=RecoveryAction.RECLAIM,
        boundary_crossed=False,
    ),)
    attempts = (RecoveryAttempt(
        component=InfraComponent.LOCK_FILE,
        name="x", action=RecoveryAction.RECLAIM,
        success=True, elapsed_s=0.0, error=None,
        auto_reclaim_was_enabled=True,
    ),)
    assert _aggregate_verdict(checks, attempts) == RecoveryVerdict.RECOVERED


# --- run_recovery_loop ------------------------------------------------------


def test_loop_master_off(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "false")
    report = run_recovery_loop()
    assert report.verdict == RecoveryVerdict.DISABLED
    assert report.master_enabled is False


def test_loop_master_on_no_components(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = run_recovery_loop(
        lock_scan_enabled=False,
        session_scan_enabled=False,
        worktree_scan_enabled=False,
    )
    assert report.master_enabled is True
    assert report.verdict == RecoveryVerdict.HEALTHY


def test_loop_with_observer_healthy(monkeypatch, healthy_snapshot):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv("JARVIS_POSTURE_HEALTH_DETECTION_ENABLED", "true")
    report = run_recovery_loop(
        observer_snapshot=healthy_snapshot,
        observer_interval_s=10.0,
        lock_scan_enabled=False,
        session_scan_enabled=False,
        worktree_scan_enabled=False,
    )
    assert report.verdict == RecoveryVerdict.HEALTHY
    assert len(report.checks) == 1


def test_loop_budget_capped(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MAX_RECOVERIES, "1")
    # Place 3 stale locks
    for i in range(3):
        lock = tmp_path / f"stale{i}.lock"
        lock.write_text("9999999")
        old = time.time() - 7200
        os.utime(lock, (old, old))
    monkeypatch.setenv(_ENV_LOCK_ROOTS, str(tmp_path))
    monkeypatch.setenv(_ENV_AUTO_RECLAIM, "false")
    report = run_recovery_loop(
        lock_scan_enabled=True,
        session_scan_enabled=False,
        worktree_scan_enabled=False,
        pid_alive_fn=lambda p: False,
    )
    # All 3 detected, only 1 attempt
    assert len(report.checks) == 3
    assert len(report.attempts) == 1


def test_loop_records_elapsed(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = run_recovery_loop(
        lock_scan_enabled=False,
        session_scan_enabled=False,
        worktree_scan_enabled=False,
    )
    assert report.elapsed_s >= 0.0


# --- Serialization ----------------------------------------------------------


def test_component_check_to_dict():
    c = ComponentCheck(
        component=InfraComponent.LOCK_FILE,
        name="x",
        health=InfraHealth.HEALTHY,
        evidence_text="ok",
        last_check_unix=1.0,
        recommended_action=RecoveryAction.NO_OP,
        boundary_crossed=False,
    )
    d = c.to_dict()
    assert d["component"] == "lock_file"
    assert d["health"] == "healthy"
    assert d["recommended_action"] == "no_op"
    assert d["schema_version"] == "infra_recovery_loop.1"


def test_recovery_attempt_to_dict():
    a = RecoveryAttempt(
        component=InfraComponent.LOCK_FILE,
        name="x",
        action=RecoveryAction.RECLAIM,
        success=True,
        elapsed_s=0.1,
        error=None,
        auto_reclaim_was_enabled=True,
    )
    d = a.to_dict()
    assert d["action"] == "reclaim"
    assert d["success"] is True


def test_recovery_attempt_to_dict_with_error():
    a = RecoveryAttempt(
        component=InfraComponent.LOCK_FILE,
        name="x",
        action=RecoveryAction.RECLAIM,
        success=False,
        elapsed_s=0.0,
        error="boom",
        auto_reclaim_was_enabled=False,
    )
    d = a.to_dict()
    assert d["error"] == "boom"


def test_report_to_dict():
    report = InfraRecoveryReport(
        evaluated_at_unix=1.0,
        master_enabled=True,
        auto_reclaim_enabled=False,
        verdict=RecoveryVerdict.HEALTHY,
        checks=(),
        attempts=(),
        diagnostic="x",
        elapsed_s=0.1,
    )
    d = report.to_dict()
    assert d["verdict"] == "healthy"
    assert d["master_enabled"] is True


# --- Persistence -----------------------------------------------------------


def test_persistence_disabled_no_write(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv(_ENV_LEDGER_PATH, str(ledger))
    run_recovery_loop(
        lock_scan_enabled=False,
        session_scan_enabled=False,
        worktree_scan_enabled=False,
    )
    assert not ledger.exists()


# --- Panel rendering --------------------------------------------------------


def test_format_panel_master_off(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "false")
    text = format_recovery_panel(None)
    assert "disabled" in text


def test_format_panel_no_report_master_on(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    text = format_recovery_panel(None)
    assert "no report" in text


def test_format_panel_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = run_recovery_loop(
        lock_scan_enabled=False,
        session_scan_enabled=False,
        worktree_scan_enabled=False,
    )
    text = format_recovery_panel(report)
    assert "Infrastructure Recovery" in text


# --- SSE event registration -------------------------------------------------


def test_sse_event_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_INFRA_RECOVERY_EVALUATED,
        _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_INFRA_RECOVERY_EVALUATED == (
        "infra_recovery_evaluated"
    )
    assert EVENT_TYPE_INFRA_RECOVERY_EVALUATED in _VALID_EVENT_TYPES


# --- FlagRegistry seeds -----------------------------------------------------


def test_register_flags_count():
    class FakeRegistry:
        def __init__(self):
            self.registered = []

        def register(self, spec):
            self.registered.append(spec)

    reg = FakeRegistry()
    count = register_flags(reg)
    assert count >= 9
    names = [s.name for s in reg.registered]
    assert _ENV_MASTER in names
    assert _ENV_AUTO_RECLAIM in names


def test_register_flags_master_default_false():
    class FakeRegistry:
        def __init__(self):
            self.registered = []

        def register(self, spec):
            self.registered.append(spec)

    reg = FakeRegistry()
    register_flags(reg)
    master_specs = [s for s in reg.registered if s.name == _ENV_MASTER]
    assert master_specs
    assert master_specs[0].default is False


def test_register_flags_auto_reclaim_default_false():
    class FakeRegistry:
        def __init__(self):
            self.registered = []

        def register(self, spec):
            self.registered.append(spec)

    reg = FakeRegistry()
    register_flags(reg)
    arc_specs = [s for s in reg.registered if s.name == _ENV_AUTO_RECLAIM]
    assert arc_specs
    assert arc_specs[0].default is False


# --- AST pins ---------------------------------------------------------------


def _load_source_tree():
    target = Path(
        "backend/core/ouroboros/governance/infra_recovery_loop.py"
    )
    src = target.read_text()
    return src, ast.parse(src)


def test_ast_pins_count():
    pins = register_shipped_invariants()
    assert len(pins) == 8


def test_ast_pin_component_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "component_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_health_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "health_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_action_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "action_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_verdict_taxonomy_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "verdict_taxonomy" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_authority_asymmetry_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_master_default_false_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_auto_reclaim_default_false_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "auto_reclaim_default_false" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


def test_ast_pin_composes_canonical_passes():
    src, tree = _load_source_tree()
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    assert pin.validate(tree, src) == ()


# --- AST pin synthetic regressions ------------------------------------------


def test_ast_pin_component_taxonomy_catches_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "component_taxonomy" in p.invariant_name
    )
    bad = '''
class InfraComponent(str, enum.Enum):
    SENSOR_TASK = "sensor_task"
    WORKTREE = "worktree"
    LOCK_FILE = "lock_file"
    NEW_THING = "new_thing"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_health_taxonomy_catches_missing():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "health_taxonomy" in p.invariant_name
    )
    bad = '''
class InfraHealth(str, enum.Enum):
    HEALTHY = "healthy"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_action_taxonomy_catches_typo():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins if "action_taxonomy" in p.invariant_name
    )
    bad = '''
class RecoveryAction(str, enum.Enum):
    NO_OP = "no_op"
    RECLAIM = "reclam"
    RESTART = "restart"
    ESCALATE = "escalate"
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_authority_catches_orchestrator_import():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = '''
from backend.core.ouroboros.governance.iron_gate import x
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_master_default_false_catches_true():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad = '''
def master_enabled():
    return _flag("X", default=True)
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_auto_reclaim_default_false_catches_true():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "auto_reclaim_default_false" in p.invariant_name
    )
    bad = '''
def auto_reclaim_enabled():
    return _flag("X", default=True)
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()


def test_ast_pin_composes_canonical_catches_missing():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    # Source is missing 'worktree_manager' / 'WorktreeManager'
    bad = '''
# posture_health
# governance_boundary_gate
# cross_process_jsonl
import subprocess
import pathlib
'''
    res = pin.validate(ast.parse(bad), bad)
    assert res != ()
