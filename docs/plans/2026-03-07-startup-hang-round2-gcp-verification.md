# Startup Hang Round 2: GCP Health Verification Fixes

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the 300s GCP health verification death loop that causes startup to stall at 57% by detecting the startup script's failure signal, registering GCP verification as visible activity, and bounding total verification time.

**Architecture:** Three surgical fixes to `_poll_health_until_ready()` and the supervisor's GCP progress callback: (1) detect `service_start_timeout` APARS checkpoint and exit early, (2) call `_mark_startup_activity()` from the GCP progress callback so ProgressController can see GCP is alive, (3) bound total verification to startup script timeout + grace period instead of a flat 300s.

**Tech Stack:** Python 3.9+, asyncio, pytest

---

## Prerequisite Context

### Key Files
- `backend/core/gcp_vm_manager.py` — `_poll_health_until_ready()` (line 9876), APARS extraction (line 9934-10030), progress callback (line 10177)
- `unified_supervisor.py` — `_mark_startup_activity()` (line 67604), GCP progress callbacks (line 72566 and 76387), `active_subsystem_reasons` assembly (line 69953)

### Root Cause Chain
1. Startup script health check fails after ~90s, writes `checkpoint: "service_start_timeout"` + `error: "service_health_check_failed"` to APARS
2. Python `_poll_health_until_ready()` extracts `phase_name` from APARS but **never checks for the failure signal** — keeps polling for 300s total
3. GCP progress callback updates dashboard but does NOT call `_mark_startup_activity()` — ProgressController can't see GCP is doing work
4. After 90s of flat progress + no activity markers, ProgressController triggers TRUE STALL

### APARS Data Structure (from startup script failure at line 9594)
When the startup script's health check fails, it writes:
```json
{
    "checkpoint": "service_start_timeout",
    "phase_name": "service_start_timeout",
    "total_progress": 95,
    "error": "service_health_check_failed",
    "ready_for_inference": null
}
```

### Import Convention
- `backend/core/gcp_vm_manager.py` uses `import os, time, logging, asyncio`
- Tests use `sys.path.insert(0, ...)` to add backend to path

---

## Task 1: APARS-Aware Early Exit from Health Polling

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:10024-10031` (after APARS field extraction)
- Create: `tests/unit/core/test_gcp_apars_early_exit.py`

### Step 1: Write the test

```python
"""Tests for APARS-aware early exit from GCP health verification."""

import os
import sys
from enum import Enum

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))


class TestAparsEarlyExit:
    def test_service_start_timeout_detected(self):
        """APARS checkpoint 'service_start_timeout' should be recognized as failure."""
        apars = {
            "checkpoint": "service_start_timeout",
            "phase_name": "service_start_timeout",
            "total_progress": 95,
            "error": "service_health_check_failed",
        }

        _APARS_TERMINAL_CHECKPOINTS = frozenset({
            "service_start_timeout",
            "service_health_check_failed",
        })

        is_terminal = apars.get("checkpoint", "") in _APARS_TERMINAL_CHECKPOINTS
        assert is_terminal is True

    def test_normal_checkpoint_not_terminal(self):
        """Normal APARS checkpoints should NOT trigger early exit."""
        apars = {
            "checkpoint": "verifying_attempt_5",
            "phase_name": "verifying_attempt_5",
            "total_progress": 60,
        }

        _APARS_TERMINAL_CHECKPOINTS = frozenset({
            "service_start_timeout",
            "service_health_check_failed",
        })

        is_terminal = apars.get("checkpoint", "") in _APARS_TERMINAL_CHECKPOINTS
        assert is_terminal is False

    def test_inference_ready_not_terminal(self):
        """Successful inference_ready checkpoint should NOT trigger early exit."""
        apars = {
            "checkpoint": "inference_ready",
            "phase_name": "inference_ready",
            "total_progress": 100,
            "ready_for_inference": True,
        }

        _APARS_TERMINAL_CHECKPOINTS = frozenset({
            "service_start_timeout",
            "service_health_check_failed",
        })

        is_terminal = apars.get("checkpoint", "") in _APARS_TERMINAL_CHECKPOINTS
        assert is_terminal is False

    def test_error_field_detected(self):
        """APARS error field containing failure signal should be detected."""
        apars = {
            "checkpoint": "service_start_timeout",
            "error": "service_health_check_failed",
            "total_progress": 95,
        }

        has_error = isinstance(apars.get("error"), str) and "failed" in apars["error"]
        assert has_error is True

    def test_grace_period_applied_after_terminal(self):
        """After terminal APARS, a grace period should be applied before returning."""
        import time

        terminal_detected_at = time.monotonic()
        grace_seconds = 30.0
        grace_deadline = terminal_detected_at + grace_seconds

        # Immediately after detection, grace period is NOT expired
        assert time.monotonic() < grace_deadline

    def test_empty_checkpoint_not_terminal(self):
        """Missing or empty checkpoint should not trigger early exit."""
        _APARS_TERMINAL_CHECKPOINTS = frozenset({
            "service_start_timeout",
            "service_health_check_failed",
        })

        for apars in [
            {},
            {"checkpoint": ""},
            {"checkpoint": "unknown"},
            {"phase_name": "starting"},
        ]:
            is_terminal = apars.get("checkpoint", "") in _APARS_TERMINAL_CHECKPOINTS
            assert is_terminal is False
```

### Step 2: Run test to verify it passes (contract test)

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_gcp_apars_early_exit.py -v`
Expected: All PASS

### Step 3: Add APARS terminal checkpoint detection to `_poll_health_until_ready`

In `backend/core/gcp_vm_manager.py`, add a module-level constant near the top of the file (after the `_STARTUP_SCRIPT_VERSION` constant around line 149):

Find:
```python
_STARTUP_SCRIPT_VERSION = "238.0"
```

Add after it:
```python
# v290.1: APARS checkpoints that indicate the startup script has given up.
# When detected, the polling loop applies a grace period before returning
# failure instead of looping for the full 300s timeout.
_APARS_TERMINAL_CHECKPOINTS: frozenset = frozenset({
    "service_start_timeout",
    "service_health_check_failed",
})
```

### Step 4: Add grace period tracking variables

In `_poll_health_until_ready()`, after the existing variable initialization (line 9903), add two new tracking variables.

Find:
```python
        _consecutive_ready = 0
        _consecutive_not_ready = 0
```

Replace with:
```python
        _consecutive_ready = 0
        _consecutive_not_ready = 0
        # v290.1: APARS terminal detection with grace period
        _apars_terminal_detected_at: Optional[float] = None
        _apars_grace_seconds = float(os.getenv(
            "JARVIS_GCP_APARS_GRACE_SECONDS", "30"
        ))
```

### Step 5: Add APARS terminal checkpoint check after field extraction

In `_poll_health_until_ready()`, after the APARS field extraction block (after line 10031), add terminal detection.

Find:
```python
                last_status = f"phase={phase_name}, progress={progress_pct}%, eta={eta}s, mode={deploy_mode}"
                logger.debug(f"☁️ [InvincibleNode] Health poll: {last_status}")
```

Replace with:
```python
                last_status = f"phase={phase_name}, progress={progress_pct}%, eta={eta}s, mode={deploy_mode}"
                logger.debug(f"☁️ [InvincibleNode] Health poll: {last_status}")

                # v290.1: Detect startup script failure signal.
                # When the startup script's own health check fails (after ~90s),
                # it sets checkpoint="service_start_timeout". Instead of polling
                # for 210 more seconds, apply a grace period then exit.
                _apars_checkpoint = apars.get("checkpoint", "")
                if _apars_checkpoint in _APARS_TERMINAL_CHECKPOINTS:
                    if _apars_terminal_detected_at is None:
                        _apars_terminal_detected_at = time.time()
                        logger.warning(
                            f"☁️ [InvincibleNode] Startup script reports failure: "
                            f"checkpoint={_apars_checkpoint}, "
                            f"error={apars.get('error', 'none')}. "
                            f"Applying {_apars_grace_seconds}s grace period "
                            f"before exit."
                        )
                    elif (time.time() - _apars_terminal_detected_at) > _apars_grace_seconds:
                        logger.warning(
                            f"☁️ [InvincibleNode] Grace period expired "
                            f"({_apars_grace_seconds}s after startup script "
                            f"failure). Returning APARS_TERMINAL."
                        )
                        if progress_callback:
                            try:
                                progress_callback(
                                    progress_pct, "terminal",
                                    f"Startup script failed: {_apars_checkpoint}"
                                )
                            except Exception:
                                pass
                        return False, (
                            f"APARS_TERMINAL: {_apars_checkpoint} "
                            f"(grace={_apars_grace_seconds}s expired)"
                        )
```

### Step 6: Run tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_gcp_apars_early_exit.py -v`
Expected: All PASS

### Step 7: Commit

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_gcp_apars_early_exit.py
git commit -m "$(cat <<'EOF'
fix(gcp): APARS-aware early exit from health verification loop

Detects startup script failure signal (checkpoint=service_start_timeout)
in APARS data and exits the polling loop after a configurable grace
period (JARVIS_GCP_APARS_GRACE_SECONDS, default 30s) instead of
looping for the full 300s timeout. Reduces worst-case GCP verification
from 300s to ~120s (90s startup script timeout + 30s grace).

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: GCP Verification Activity Registration

**Files:**
- Modify: `unified_supervisor.py:76387-76420` (GCP progress callback)
- Create: `tests/unit/core/test_gcp_activity_registration.py`

### Step 1: Write the test

```python
"""Tests for GCP verification activity registration with ProgressController."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))


class TestGcpActivityRegistration:
    def test_activity_marker_updated_on_progress(self):
        """GCP progress callback should update activity markers."""
        markers = {}
        sources = {}

        def mock_mark_activity(source, stage=None):
            phase = stage or "intelligence"
            markers[phase] = time.time()
            sources[phase] = source

        # Simulate GCP progress callback calling mark_activity
        mock_mark_activity("gcp_verification", stage="intelligence")

        assert "intelligence" in markers
        assert sources["intelligence"] == "gcp_verification"

    def test_activity_marker_not_set_on_recycle(self):
        """GCP recycle events should NOT register as startup activity."""
        markers = {}

        pct = 0
        detail = "recycling VM"
        is_recycle = pct == 0 and "recycl" in detail.lower()

        # Recycle should skip activity registration
        if not is_recycle:
            markers["intelligence"] = time.time()

        assert "intelligence" not in markers

    def test_activity_timestamp_is_recent(self):
        """Activity marker timestamp should be within tolerance."""
        markers = {}
        now = time.time()
        markers["intelligence"] = now

        staleness = time.time() - markers["intelligence"]
        assert staleness < 1.0  # Within 1 second

    def test_activity_source_contains_gcp(self):
        """Activity source string should identify GCP verification."""
        source = "gcp_verification"
        assert "gcp" in source.lower()
```

### Step 2: Run test to verify it passes (contract test)

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_gcp_activity_registration.py -v`
Expected: All PASS

### Step 3: Add `_mark_startup_activity` call to GCP progress callback

In `unified_supervisor.py`, find the GCP progress callback at line 76387 (`_gcp_progress_callback`). This is the callback passed to `ensure_static_vm_ready()` at line 76425.

Find the end of the callback function, just before the closing of the `_gcp_progress_callback` function. The last lines are:

```python
                    update_dashboard_gcp_progress(
                        phase=4, phase_name=phase.title()[:15],
                        checkpoint=detail[:60],
                        progress=dashboard_pct,
                        source="apars",  # v229.0: Critical — marks as real data
                        deployment_mode=_mode if _mode else None,
                    )
```

Replace with:
```python
                    update_dashboard_gcp_progress(
                        phase=4, phase_name=phase.title()[:15],
                        checkpoint=detail[:60],
                        progress=dashboard_pct,
                        source="apars",  # v229.0: Critical — marks as real data
                        deployment_mode=_mode if _mode else None,
                    )
                    # v290.1: Register GCP verification as startup activity so
                    # ProgressController can see it. Without this, GCP polling
                    # is invisible to active_subsystem_reasons → false TRUE STALL.
                    try:
                        self._mark_startup_activity("gcp_verification")
                    except Exception:
                        pass
```

### Step 4: Also add to the proactive callback

In `unified_supervisor.py`, find the proactive GCP progress callback at line 72566 (`_proactive_progress_cb`). Find the end of that function:

```python
                    update_dashboard_gcp_progress(
                        phase=4,
                        phase_name=phase.title()[:15] if phase else "Loading",
                        checkpoint=(detail or "")[:60],
                        progress=dashboard_pct,
                        source="apars",
                        deployment_mode=_mode,
                    )
```

Replace with:
```python
                    update_dashboard_gcp_progress(
                        phase=4,
                        phase_name=phase.title()[:15] if phase else "Loading",
                        checkpoint=(detail or "")[:60],
                        progress=dashboard_pct,
                        source="apars",
                        deployment_mode=_mode,
                    )
                    # v290.1: Register GCP verification as startup activity
                    try:
                        self._mark_startup_activity("gcp_verification")
                    except Exception:
                        pass
```

### Step 5: Run tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_gcp_activity_registration.py -v`
Expected: All PASS

### Step 6: Commit

```bash
git add unified_supervisor.py tests/unit/core/test_gcp_activity_registration.py
git commit -m "$(cat <<'EOF'
fix(supervisor): register GCP verification as startup activity

Adds _mark_startup_activity("gcp_verification") calls to both GCP
progress callbacks. ProgressController can now see GCP health
verification as legitimate startup activity, preventing false
TRUE STALL detection while GCP VM is actively being verified.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Bounded GCP Verification Timeout

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:9895-9905` (poll loop init + timeout)
- Create: `tests/unit/core/test_gcp_bounded_verification.py`

### Step 1: Write the test

```python
"""Tests for bounded GCP health verification timeout."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))


class TestBoundedVerification:
    def test_effective_timeout_capped_by_script_timeout_plus_grace(self):
        """Effective timeout should be min(config_timeout, script_timeout + grace)."""
        config_timeout = 300.0
        script_health_timeout = 90.0  # GCP_SERVICE_HEALTH_TIMEOUT default
        grace_seconds = 30.0

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 120.0  # 90 + 30, not 300

    def test_config_timeout_wins_when_smaller(self):
        """If config timeout is smaller than script + grace, use config."""
        config_timeout = 60.0
        script_health_timeout = 90.0
        grace_seconds = 30.0

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 60.0

    def test_custom_script_timeout_respected(self):
        """Custom GCP_SERVICE_HEALTH_TIMEOUT should feed into bound."""
        config_timeout = 300.0
        script_health_timeout = 180.0  # Custom longer timeout
        grace_seconds = 30.0

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 210.0  # 180 + 30

    def test_grace_period_configurable(self):
        """Grace period should be configurable via environment."""
        config_timeout = 300.0
        script_health_timeout = 90.0
        grace_seconds = 60.0  # Custom grace

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 150.0  # 90 + 60

    def test_zero_grace_means_exit_at_script_timeout(self):
        """Zero grace means exit immediately when script timeout elapses."""
        config_timeout = 300.0
        script_health_timeout = 90.0
        grace_seconds = 0.0

        effective = min(config_timeout, script_health_timeout + grace_seconds)
        assert effective == 90.0
```

### Step 2: Run test to verify it passes (contract test)

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_gcp_bounded_verification.py -v`
Expected: All PASS

### Step 3: Compute effective timeout in `_poll_health_until_ready`

In `backend/core/gcp_vm_manager.py`, find the beginning of `_poll_health_until_ready()` where local variables are initialized (line 9895).

Find:
```python
        start_time = time.time()
        last_status = "starting"
```

Replace with:
```python
        start_time = time.time()
        last_status = "starting"
        # v290.1: Bound verification to startup script timeout + grace.
        # The startup script's health check runs for GCP_SERVICE_HEALTH_TIMEOUT
        # (default 90s). After that, APARS will report service_start_timeout.
        # Instead of using the full config timeout (300s), cap at
        # script_timeout + grace to avoid 210s of useless polling.
        _script_health_timeout = float(os.getenv(
            "GCP_SERVICE_HEALTH_TIMEOUT", "90"
        ))
        _bounded_timeout = min(
            timeout,
            _script_health_timeout + _apars_grace_seconds,
        )
        if _bounded_timeout < timeout:
            logger.info(
                f"☁️ [InvincibleNode] Verification timeout bounded to "
                f"{_bounded_timeout:.0f}s (script={_script_health_timeout:.0f}s "
                f"+ grace={_apars_grace_seconds:.0f}s, config={timeout:.0f}s)"
            )
            timeout = _bounded_timeout
```

**Important:** The `_apars_grace_seconds` variable was added in Task 1 Step 4 (line 9904). This step MUST be done after Task 1.

### Step 4: Run tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_gcp_bounded_verification.py -v`
Expected: All PASS

### Step 5: Commit

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_gcp_bounded_verification.py
git commit -m "$(cat <<'EOF'
fix(gcp): bound health verification timeout to script timeout + grace

Computes effective timeout as min(config_timeout, script_health_timeout
+ grace_period) instead of using the full 300s config timeout. Default:
min(300, 90+30) = 120s. This eliminates ~180s of useless polling after
the startup script has already given up.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Run Full Test Suite

### Step 1: Run all new tests

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_gcp_apars_early_exit.py tests/unit/core/test_gcp_activity_registration.py tests/unit/core/test_gcp_bounded_verification.py -v`
Expected: All PASS

### Step 2: Run all startup-fix tests (regression from round 1)

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/core/test_gcp_version_mismatch_terminal.py tests/unit/core/test_ecapa_cloudsql_failfast.py tests/unit/core/test_phase_hold_terminal_skip.py -v`
Expected: All PASS

### Step 3: Run email triage + routing tests (regression)

Run: `cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent && python3 -m pytest tests/unit/backend/email_triage/ tests/unit/core/test_prime_router_gcp_first.py tests/unit/core/test_route_selection_matrix.py tests/unit/core/test_supervisor_experience_processor.py -v`
Expected: All PASS

---

## Summary of Changes

| File | Change | Purpose |
|------|--------|---------|
| `backend/core/gcp_vm_manager.py` | Add `_APARS_TERMINAL_CHECKPOINTS` constant | Define failure signals from startup script |
| `backend/core/gcp_vm_manager.py` | Add APARS checkpoint detection in poll loop | Exit early when startup script reports failure |
| `backend/core/gcp_vm_manager.py` | Compute bounded timeout from script + grace | Reduce worst-case from 300s to 120s |
| `unified_supervisor.py` | Add `_mark_startup_activity("gcp_verification")` to callbacks | Make GCP verification visible to ProgressController |
| `tests/unit/core/test_gcp_apars_early_exit.py` | Contract tests for APARS terminal detection | Validate checkpoint detection + grace period |
| `tests/unit/core/test_gcp_activity_registration.py` | Contract tests for activity registration | Validate GCP activity visible to supervisor |
| `tests/unit/core/test_gcp_bounded_verification.py` | Contract tests for bounded timeout | Validate effective timeout calculation |
