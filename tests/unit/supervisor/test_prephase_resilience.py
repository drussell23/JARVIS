from __future__ import annotations


def test_cloud_only_fallback_never_escalates_to_local_full(monkeypatch):
    import unified_supervisor as us

    monkeypatch.setenv("JARVIS_CRITICAL_THRESHOLD_GB", "2.0")
    monkeypatch.setenv("JARVIS_OPTIMIZE_THRESHOLD_GB", "4.0")
    monkeypatch.setenv("JARVIS_PLANNED_ML_GB", "4.6")
    monkeypatch.setenv("JARVIS_MINIMAL_THRESHOLD_GB", "1.0")

    assert us._resolve_local_startup_mode_on_cloud_unavailable("cloud_only", 16.0) == "local_optimized"
    assert us._resolve_local_startup_mode_on_cloud_unavailable("cloud_only", 0.4) == "minimal"
    assert us._resolve_local_startup_mode_on_cloud_unavailable("cloud_only", None) == "sequential"


def test_cloud_first_fallback_is_memory_aware(monkeypatch):
    import unified_supervisor as us

    monkeypatch.setenv("JARVIS_CRITICAL_THRESHOLD_GB", "2.0")
    monkeypatch.setenv("JARVIS_OPTIMIZE_THRESHOLD_GB", "4.0")
    monkeypatch.setenv("JARVIS_PLANNED_ML_GB", "4.6")
    monkeypatch.setenv("JARVIS_MINIMAL_THRESHOLD_GB", "1.0")

    assert us._resolve_local_startup_mode_on_cloud_unavailable("cloud_first", 16.0) == "local_full"
    assert us._resolve_local_startup_mode_on_cloud_unavailable("cloud_first", 6.0) == "sequential"
    assert us._resolve_local_startup_mode_on_cloud_unavailable("cloud_first", None) == "local_optimized"


def test_startup_watchdog_invalid_env_values_fall_back_to_defaults(monkeypatch):
    import unified_supervisor as us

    class _Logger:
        def info(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def debug(self, *_args, **_kwargs):
            pass

    monkeypatch.setenv("JARVIS_DMS_ENABLED", "not-a-bool")
    monkeypatch.setenv("JARVIS_DMS_STALL_THRESHOLD", "bad-float")
    monkeypatch.setenv("JARVIS_DMS_CHECK_INTERVAL", "bad-float")
    monkeypatch.setenv("JARVIS_DMS_RECOVERY_MODE", "unknown-mode")
    monkeypatch.setenv("JARVIS_DMS_ESCALATION_COOLDOWN", "NaN???")
    monkeypatch.setenv("JARVIS_DMS_STALL_ESCALATION_COOLDOWN", "NaN???")
    monkeypatch.setenv("JARVIS_DMS_PROGRESS_WINDOW", "NaN???")
    monkeypatch.setenv("GCP_VM_STARTUP_TIMEOUT", "NaN???")

    watchdog = us.StartupWatchdog(logger=_Logger())

    assert watchdog.enabled is True
    assert watchdog._stall_threshold == 60.0
    assert watchdog._check_interval == 5.0
    assert watchdog._recovery_mode == "graduated"
    assert watchdog._escalation_cooldown == 60.0
    assert watchdog._stall_escalation_cooldown == 15.0
    assert watchdog._progress_advancing_window == 120.0


def test_startup_watchdog_reads_the_measured_percentage_not_the_callers_guess():
    # Live boot 2026-10-14 08:04: phase call sites passed literals (86 at
    # agi_os) while the broadcast passed the measured value (57), and the
    # watchdog logged "Progress regression: 84% -> 57%" three times.
    import unified_supervisor as us

    warnings = []

    class _Logger:
        def info(self, *_a, **_k):
            pass

        def debug(self, *_a, **_k):
            pass

        def warning(self, msg, *_a, **_k):
            warnings.append(msg)

    measured = [57]
    watchdog = us.StartupWatchdog(logger=_Logger(), progress_source=lambda: measured[0])
    watchdog.update_phase("agi_os", 86)        # a call site's literal
    watchdog.update_phase("agi_os", 57)        # the broadcast
    measured[0] = 58
    watchdog.update_phase("agi_os", 89)
    assert watchdog._last_progress == 58
    assert not [w for w in warnings if "regression" in w or "jump" in w]

    # No measure (no timeline, or after completion): the caller's value stands.
    bare = us.StartupWatchdog(logger=_Logger(), progress_source=lambda: None)
    bare.update_phase("frontend", 93)
    assert bare._last_progress == 93
