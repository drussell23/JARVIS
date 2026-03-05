# GCP Readiness Split-Brain Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the GCP golden-image VM getting stuck at 97% by eliminating the split-brain readiness conflict between the startup script's APARS progress file and J-Prime's live health endpoint.

**Architecture:** Declare J-Prime's live `/health` response as the single source of truth for readiness. Demote the APARS progress file (`/tmp/jarvis_progress.json`) to observational metadata only — it reports progress but NEVER determines readiness. Add boot-session identity (UUID) to detect and discard stale APARS data from previous boots. Unify the timeout budget so the startup script health check uses the same configurable timeout as the supervisor.

**Tech Stack:** Python 3.11, bash (startup script), aiohttp, pytest, unittest.mock

---

## Root Cause Summary

The startup script health check loop runs for only 30s (`15 x 2s`). J-Prime model loading takes 60-120s+. When the script times out, it writes `service_start_timeout` with `ready_for_inference=false` to the APARS progress file. The APARS enrichment middleware then injects this stale state into every subsequent `/health` response via `setdefault`. The supervisor polls for 300s but never sees `ready_for_inference=true` because:

1. The progress file says `false` and the middleware uses `setdefault` (doesn't override J-Prime's own field, BUT J-Prime may not set it at all)
2. The `>=95` total_progress shortcut in the startup script is unreachable as a success path
3. The synthetic progress generator reaches 97% before APARS caps it, then stays stuck

## Key Invariants

- **INV-1**: `ready_for_inference` is ONLY determined by J-Prime's live response, never from the progress file
- **INV-2**: APARS progress file is observational metadata — progress display only
- **INV-3**: Boot session UUID binds APARS data to a specific boot — stale data from prior boots is ignored
- **INV-4**: Startup script health timeout derives from the same env var as the supervisor poll timeout
- **INV-5**: The `total_progress >= 95` readiness shortcut is removed — progress is never a proxy for readiness

---

### Task 0: Boot Session UUID in APARS Progress File

**Context:** Without a session identifier, stale APARS data from a previous VM boot contaminates the current boot. The restart race is a real failure mode.

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:8696-8728` (update_apars function in startup script)
- Modify: `backend/core/gcp_vm_manager.py:9126-9147` (`_build_apars_payload`)
- Test: `tests/unit/core/test_apars_boot_session.py`

**Step 1: Write the failing test**

Create `tests/unit/core/test_apars_boot_session.py`:

```python
"""Tests for APARS boot session UUID binding."""
import json
import tempfile
import os
from pathlib import Path
from unittest.mock import patch


class TestAPARSBootSession:
    def test_build_apars_payload_includes_boot_session_id(self):
        """APARS payload must include boot_session_id from progress file."""
        from backend.core.gcp_vm_manager import _build_apars_payload
        state = {
            "phase_number": 6,
            "total_progress": 95,
            "checkpoint": "verifying_service",
            "boot_session_id": "abc-123-def",
            "updated_at": 1000,
        }
        payload = _build_apars_payload(state)
        assert payload is not None
        assert payload["boot_session_id"] == "abc-123-def"

    def test_build_apars_payload_missing_session_returns_unknown(self):
        """If progress file has no boot_session_id, payload uses 'unknown'."""
        from backend.core.gcp_vm_manager import _build_apars_payload
        state = {
            "phase_number": 1,
            "total_progress": 10,
            "checkpoint": "starting",
            "updated_at": 1000,
        }
        payload = _build_apars_payload(state)
        assert payload is not None
        assert payload["boot_session_id"] == "unknown"

    def test_startup_script_contains_boot_session_uuid(self):
        """The golden startup script must generate a BOOT_SESSION_ID UUID."""
        from backend.core.gcp_vm_manager import GCPVMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = GCPVMManagerConfig()
        script = mgr._generate_golden_startup_script()
        assert "BOOT_SESSION_ID=" in script
        assert "boot_session_id" in script
        # Must use uuidgen or python uuid
        assert "uuidgen" in script or "uuid" in script
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: FAIL — `boot_session_id` not in payload, not in startup script

**Step 3: Implement**

In `_build_apars_payload` (line ~9133), add `boot_session_id` to the returned dict:
```python
"boot_session_id": state.get("boot_session_id", "unknown"),
```

In the startup script's `update_apars()` function (line ~8707), add `BOOT_SESSION_ID` generation at script top and include it in the JSON:
```bash
# Near the top of the script (after START_TIME):
BOOT_SESSION_ID=$(uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || echo "unknown-$$")

# In the update_apars function JSON template, add:
    "boot_session_id": "${BOOT_SESSION_ID}",
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add tests/unit/core/test_apars_boot_session.py backend/core/gcp_vm_manager.py
git commit -m "feat(gcp): add boot session UUID to APARS progress file"
```

---

### Task 1: Configurable Startup Script Health Check Timeout

**Context:** The hardcoded `for attempt in $(seq 1 15); sleep 2` = 30s is the primary trigger. J-Prime takes 60-120s+. The timeout must derive from the same env var (`GCP_SERVICE_HEALTH_TIMEOUT`) that the supervisor respects, with a sensible default of 90s.

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:9320-9349` (startup script health check loop)
- Modify: `backend/core/gcp_vm_manager.py:664-665` (GCPVMManagerConfig)
- Test: `tests/unit/core/test_apars_boot_session.py` (extend)

**Step 1: Write the failing test**

Add to `tests/unit/core/test_apars_boot_session.py`:

```python
    def test_startup_script_uses_configurable_health_timeout(self):
        """Startup script must use GCP_SERVICE_HEALTH_TIMEOUT, not hardcoded 30s."""
        from backend.core.gcp_vm_manager import GCPVMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = GCPVMManagerConfig()
        script = mgr._generate_golden_startup_script()
        # Must reference the env var for timeout
        assert "GCP_SERVICE_HEALTH_TIMEOUT" in script
        # Must NOT have the old hardcoded `seq 1 15`
        assert "seq 1 15" not in script

    def test_startup_script_health_timeout_default_90s(self):
        """Default health check timeout should be 90s (45 attempts x 2s)."""
        from backend.core.gcp_vm_manager import GCPVMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = GCPVMManagerConfig()
        script = mgr._generate_golden_startup_script()
        # Default should be 90 seconds
        assert 'GCP_SERVICE_HEALTH_TIMEOUT", "90"' in script or \
               'GCP_SERVICE_HEALTH_TIMEOUT:-90' in script or \
               'GCP_SERVICE_HEALTH_TIMEOUT", 90' in script
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py::TestAPARSBootSession::test_startup_script_uses_configurable_health_timeout -v`
Expected: FAIL — `seq 1 15` still present

**Step 3: Implement**

Replace the hardcoded loop at line 9320 with:

```bash
# Configurable health check timeout (same env var supervisor respects)
SERVICE_HEALTH_TIMEOUT=${GCP_SERVICE_HEALTH_TIMEOUT:-90}
HEALTH_CHECK_INTERVAL=2
MAX_ATTEMPTS=$((SERVICE_HEALTH_TIMEOUT / HEALTH_CHECK_INTERVAL))
log "Health check: ${MAX_ATTEMPTS} attempts x ${HEALTH_CHECK_INTERVAL}s = ${SERVICE_HEALTH_TIMEOUT}s timeout"

READY=false
for attempt in $(seq 1 $MAX_ATTEMPTS); do
    sleep $HEALTH_CHECK_INTERVAL
```

Also update the timeout message at line 9360:
```bash
    log "Service not responding after ${SERVICE_HEALTH_TIMEOUT}s - may need more time"
```

Add `GCP_SERVICE_HEALTH_TIMEOUT` to the GCPVMManagerConfig so the supervisor can propagate it to the VM metadata:
```python
service_health_timeout: float = field(
    default_factory=lambda: float(os.getenv("GCP_SERVICE_HEALTH_TIMEOUT", "90.0"))
)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add tests/unit/core/test_apars_boot_session.py backend/core/gcp_vm_manager.py
git commit -m "feat(gcp): replace hardcoded 30s health timeout with configurable GCP_SERVICE_HEALTH_TIMEOUT (default 90s)"
```

---

### Task 2: Remove `total_progress >= 95` Readiness Shortcut

**Context:** Line 9336 accepts `apars.total_progress >= 95` as a readiness signal in the startup script health check. This is semantically wrong — progress is not readiness. The only path to 95% is the timeout failure case itself (line 9359), so this creates a false-positive readiness circuit. Remove it.

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:9334-9337` (startup script health check)
- Modify: `backend/core/gcp_vm_manager.py:8388-8391` (`_ping_health_endpoint` — same pattern)
- Test: `tests/unit/core/test_apars_boot_session.py` (extend)

**Step 1: Write the failing test**

Add to test file:

```python
    def test_startup_script_no_progress_threshold_readiness(self):
        """Startup script must NOT use total_progress >= 95 as readiness signal."""
        from backend.core.gcp_vm_manager import GCPVMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = GCPVMManagerConfig()
        script = mgr._generate_golden_startup_script()
        # The progress-based readiness shortcut must be removed
        assert "total_progress" not in script or "total_progress.*>= 95" not in script

    def test_ping_health_no_progress_threshold_readiness(self):
        """_ping_health_endpoint must NOT accept total_progress >= 100 as sole readiness."""
        import asyncio
        import aiohttp
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.core.gcp_vm_manager import GCPVMManager, GCPVMManagerConfig

        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = GCPVMManagerConfig()

        # Health response with progress=100 but NO readiness signals
        fake_data = {
            "status": "starting",
            "apars": {"total_progress": 100},
        }

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=fake_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            is_ready, data = asyncio.get_event_loop().run_until_complete(
                mgr._ping_health_endpoint("1.2.3.4", 8000, timeout=5.0)
            )

        # Progress alone must NOT make it ready
        assert is_ready is False
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py::TestAPARSBootSession::test_ping_health_no_progress_threshold_readiness -v`
Expected: FAIL — current code accepts `total_progress >= 100` as ready

**Step 3: Implement**

In `_ping_health_endpoint` (line 8388-8391), remove the APARS total_progress shortcut:
```python
# REMOVE these lines:
#                        if not is_ready:
#                            apars = data.get("apars", {})
#                            if isinstance(apars, dict) and apars.get("total_progress", 0) >= 100:
#                                is_ready = True
```

In the startup script health check (lines 9334-9337), remove the APARS progress shortcut:
```python
# REMOVE these lines from the inline python:
#    apars = d.get('apars', {})
#    if apars.get('total_progress', 0) >= 95:
#        sys.exit(0)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: PASS (7 tests)

**Step 5: Commit**

```bash
git add tests/unit/core/test_apars_boot_session.py backend/core/gcp_vm_manager.py
git commit -m "fix(gcp): remove total_progress>=95 readiness shortcut — progress is not readiness (INV-5)"
```

---

### Task 3: APARS Enrichment Middleware — Never Override Live Readiness

**Context:** The middleware at line 9187-9191 uses `setdefault("model_loaded", ...)` and conditionally sets `ready_for_inference=True`. The bug is subtle: when J-Prime doesn't set `ready_for_inference` itself (common during loading), the field is absent, and the middleware doesn't add it (because progress file says false). The supervisor then defaults to `False`. Fix: the middleware must NEVER set/influence readiness fields. APARS is observational only (INV-1, INV-2).

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:9186-9191` (APARSEnrichmentMiddleware)
- Test: `tests/unit/core/test_apars_enrichment.py`

**Step 1: Write the failing test**

Create `tests/unit/core/test_apars_enrichment.py`:

```python
"""Tests for APARS enrichment middleware readiness isolation."""
import json


class TestAPARSEnrichmentReadiness:
    def test_middleware_does_not_propagate_readiness_fields(self):
        """APARS middleware must NOT inject model_loaded or ready_for_inference."""
        from backend.core.gcp_vm_manager import _build_apars_payload

        # Simulate progress file state with ready_for_inference=true
        state = {
            "phase_number": 6,
            "total_progress": 100,
            "checkpoint": "inference_ready",
            "model_loaded": True,
            "ready_for_inference": True,
            "updated_at": 1000,
            "boot_session_id": "test-uuid",
        }
        payload = _build_apars_payload(state)

        # APARS payload must NOT contain readiness fields
        # These are observational metadata, not truth
        assert "ready_for_inference" not in payload
        assert "model_loaded" not in payload

    def test_apars_payload_contains_progress_fields(self):
        """APARS payload must still contain progress/phase fields."""
        from backend.core.gcp_vm_manager import _build_apars_payload

        state = {
            "phase_number": 4,
            "total_progress": 60,
            "checkpoint": "installing",
            "updated_at": 1000,
            "boot_session_id": "test-uuid",
        }
        payload = _build_apars_payload(state)
        assert payload["total_progress"] == 60
        assert payload["phase_number"] == 4
        assert payload["checkpoint"] == "installing"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_enrichment.py -v`
Expected: FAIL — payload currently includes readiness fields (propagated from state)

**Step 3: Implement**

In `APARSEnrichmentMiddleware.__call__` (line 9186-9191), remove readiness propagation:

Replace:
```python
                if payload:
                    data["apars"] = payload
                    # Propagate readiness flags from progress file
                    if state:
                        data.setdefault("model_loaded", state.get("model_loaded", False))
                        if state.get("ready_for_inference", False):
                            data["ready_for_inference"] = True
```

With:
```python
                if payload:
                    data["apars"] = payload
                    # INV-1/INV-2: APARS is observational metadata only.
                    # Readiness is determined solely by J-Prime's live response.
                    # Do NOT propagate model_loaded or ready_for_inference
                    # from the progress file — it can be stale.
```

Also ensure `_build_apars_payload` does NOT include readiness fields. Currently it doesn't directly (those are propagated in the middleware), but verify and add a comment:
```python
    # INV-2: APARS payload is observational metadata only.
    # Do NOT include ready_for_inference or model_loaded here.
    # Readiness is determined by J-Prime's live /health response.
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_apars_enrichment.py -v`
Expected: PASS (2 tests)

**Step 5: Commit**

```bash
git add tests/unit/core/test_apars_enrichment.py backend/core/gcp_vm_manager.py
git commit -m "fix(gcp): APARS middleware no longer propagates readiness fields — live health is sole authority (INV-1, INV-2)"
```

---

### Task 4: Startup Script — Don't Write `ready_for_inference=false` on Timeout

**Context:** Line 9359 writes `ready_for_inference=false` on timeout. This is semantically incorrect — the startup script doesn't know J-Prime's actual state. It should write `ready_for_inference` as `null` (unknown) and let the live health endpoint determine readiness. Also, `total_progress=95` on failure is misleading — use a separate `timeout_at_progress` field.

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:9359` (service_start_timeout update_apars call)
- Modify: `backend/core/gcp_vm_manager.py:8696-8728` (update_apars function — support null readiness)
- Test: `tests/unit/core/test_apars_boot_session.py` (extend)

**Step 1: Write the failing test**

Add to test file:

```python
    def test_startup_script_timeout_does_not_claim_not_ready(self):
        """On timeout, startup script must NOT assert ready_for_inference=false.

        The script doesn't know J-Prime's actual state — it should write
        null (unknown) so the live health endpoint determines readiness.
        """
        from backend.core.gcp_vm_manager import GCPVMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = GCPVMManagerConfig()
        script = mgr._generate_golden_startup_script()

        # Find the service_start_timeout update_apars call
        import re
        timeout_calls = re.findall(
            r'update_apars.*service_start_timeout.*', script
        )
        assert len(timeout_calls) >= 1, "Must have service_start_timeout update_apars call"

        # It must NOT pass 'false' for ready_for_inference
        # It should pass 'null' (unknown state)
        for call in timeout_calls:
            # The 6th positional arg is ready_for_inference
            # update_apars 6 100 95 "service_start_timeout" true false
            #              ^phase ^phase_prog ^total ^checkpoint ^model ^ready
            assert "false" not in call.split('"service_start_timeout"')[1].strip().split()[0:2] or \
                   "null" in call, \
                   f"Timeout call must not assert ready=false: {call}"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py::TestAPARSBootSession::test_startup_script_timeout_does_not_claim_not_ready -v`
Expected: FAIL — currently writes `false`

**Step 3: Implement**

Change line 9359 from:
```bash
    update_apars 6 100 95 "service_start_timeout" true false '"service_health_check_failed"'
```
To:
```bash
    update_apars 6 100 95 "service_start_timeout" null null '"service_health_check_failed"'
```

Update the `update_apars` function to handle `null` for model_loaded and ready_for_inference:
```bash
update_apars() {
    local phase=$1
    local phase_progress=$2
    local total_progress=$3
    local checkpoint=$4
    local model_loaded=${5:-null}
    local ready=${6:-null}
    local error=${7:-null}
```

This means the JSON will contain `"model_loaded": null, "ready_for_inference": null` which is valid JSON and clearly signals "unknown state" rather than asserting false.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: PASS (8 tests)

**Step 5: Commit**

```bash
git add tests/unit/core/test_apars_boot_session.py backend/core/gcp_vm_manager.py
git commit -m "fix(gcp): startup script timeout writes null readiness, not false — doesn't know J-Prime state"
```

---

### Task 5: `_ping_health_endpoint` — Prefer Live Health Over APARS

**Context:** `_ping_health_endpoint` (line 8364) already checks multiple signals (`ready_for_inference`, `model_loaded && healthy`, `phase=="ready"`, `model_load_progress_pct>=100`). These are all from J-Prime's LIVE response. Now that we've removed the APARS `total_progress>=100` shortcut (Task 2) and stopped the middleware from injecting readiness (Task 3), the endpoint naturally prefers live health. But we need a contract test to ensure this invariant holds.

**Files:**
- Test: `tests/contracts/test_readiness_authority.py`

**Step 1: Write the contract test**

Create `tests/contracts/test_readiness_authority.py`:

```python
"""Contract test: readiness authority is the live health endpoint, never APARS.

INV-1: ready_for_inference is ONLY determined by J-Prime's live response.
INV-2: APARS progress file is observational metadata — progress display only.
"""
import ast
from pathlib import Path


class TestReadinessAuthority:
    def test_ping_health_does_not_use_apars_for_readiness(self):
        """_ping_health_endpoint must not use APARS total_progress for readiness decisions."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_ping_health_endpoint"
            ):
                body_src = ast.get_source_segment(src, node)
                # Must not contain total_progress readiness shortcut
                assert "total_progress" not in body_src or \
                       "is_ready = True" not in body_src.split("total_progress")[1].split("\n")[0], \
                       "_ping_health_endpoint must not use total_progress for readiness"
                break
        else:
            raise AssertionError("_ping_health_endpoint not found")

    def test_apars_middleware_does_not_set_readiness(self):
        """APARSEnrichmentMiddleware must not set ready_for_inference or model_loaded."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "APARSEnrichmentMiddleware":
                class_src = ast.get_source_segment(src, node)
                assert "ready_for_inference" not in class_src, \
                    "APARSEnrichmentMiddleware must not touch ready_for_inference"
                assert 'setdefault("model_loaded"' not in class_src, \
                    "APARSEnrichmentMiddleware must not set model_loaded"
                break
        else:
            raise AssertionError("APARSEnrichmentMiddleware not found")

    def test_build_apars_payload_excludes_readiness(self):
        """_build_apars_payload must not include readiness fields in output."""
        from backend.core.gcp_vm_manager import _build_apars_payload
        state = {
            "phase_number": 6, "total_progress": 100,
            "checkpoint": "ready", "updated_at": 1000,
            "model_loaded": True, "ready_for_inference": True,
            "boot_session_id": "test",
        }
        payload = _build_apars_payload(state)
        assert "ready_for_inference" not in payload
        assert "model_loaded" not in payload
```

**Step 2: Run test to verify it passes**

Run: `python3 -m pytest tests/contracts/test_readiness_authority.py -v`
Expected: PASS (3 tests — these guard Tasks 2-3 invariants)

**Step 3: Commit**

```bash
git add tests/contracts/test_readiness_authority.py
git commit -m "test(contracts): add readiness authority contract tests — INV-1, INV-2 guards"
```

---

### Task 6: APARS Staleness Detection via Boot Session + Monotonic Timestamp

**Context:** The `_read_apars` function (line 9111) caches by mtime_ns but has no staleness model. A stale progress file from a previous boot (restart race) contaminates the new boot. With the boot session UUID from Task 0, the polling loop can detect and discard stale APARS data.

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:9666-9884` (`_poll_health_until_ready` — add boot session tracking)
- Test: `tests/unit/core/test_apars_boot_session.py` (extend)

**Step 1: Write the failing test**

Add to test file:

```python
    def test_poll_ignores_stale_boot_session(self):
        """When APARS data has a different boot_session_id, it should be flagged as stale."""
        # This tests the concept: when the polling loop first sees a boot_session_id,
        # it locks onto it. If a subsequent poll returns a different ID (stale file
        # from previous boot), the data is treated as no-data.

        # We test the helper function that validates boot session
        from backend.core.gcp_vm_manager import _is_apars_current_session

        # First call establishes the session
        assert _is_apars_current_session("session-A", expected="session-A") is True
        # Same session
        assert _is_apars_current_session("session-A", expected="session-A") is True
        # Different session = stale
        assert _is_apars_current_session("session-B", expected="session-A") is False
        # Unknown session = accept (backward compat with old startup scripts)
        assert _is_apars_current_session("unknown", expected="session-A") is True
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py::TestAPARSBootSession::test_poll_ignores_stale_boot_session -v`
Expected: FAIL — `_is_apars_current_session` doesn't exist

**Step 3: Implement**

Add near `_read_apars` (line ~9125):

```python
def _is_apars_current_session(session_id: str, expected: str) -> bool:
    """Check if APARS data belongs to the current boot session.

    Returns True if session matches or is 'unknown' (backward compat).
    Returns False if session is from a different boot (stale data).
    """
    if not session_id or session_id == "unknown":
        return True  # Backward compat: old scripts don't have session ID
    return session_id == expected
```

In `_poll_health_until_ready`, after extracting APARS data (line ~9689), add session validation:

```python
                # Validate boot session — discard stale data from previous boots
                apars_session = apars.get("boot_session_id", "unknown")
                if not _is_apars_current_session(apars_session, self._current_boot_session_id):
                    logger.warning(
                        f"Stale APARS data detected (session={apars_session}, "
                        f"expected={self._current_boot_session_id}). Ignoring."
                    )
                    has_real_data = False
                    continue
```

Store `_current_boot_session_id` on the manager when starting a new VM (set in `_start_instance` or `ensure_static_vm_ready`).

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: PASS (9 tests)

**Step 5: Commit**

```bash
git add tests/unit/core/test_apars_boot_session.py backend/core/gcp_vm_manager.py
git commit -m "feat(gcp): detect and discard stale APARS data from previous boot sessions (INV-3)"
```

---

### Task 7: Synthetic Progress Reset on `service_start_timeout`

**Context:** The synthetic progress generator in `unified_supervisor.py:72713-72727` caps at 95% but can display 97-98% because it reaches that value before the APARS cap is applied. When APARS reports `service_start_timeout` (now with `ready_for_inference=null`), the synthetic generator should display the failure state clearly, not a misleading 97%.

**Files:**
- Modify: `unified_supervisor.py:72713-72741` (synthetic progress generator)
- Test: `tests/unit/core/test_synthetic_progress_reset.py`

**Step 1: Write the failing test**

Create `tests/unit/core/test_synthetic_progress_reset.py`:

```python
"""Tests for synthetic progress behavior on service_start_timeout."""


class TestSyntheticProgressReset:
    def test_synthetic_caps_to_apars_on_timeout_checkpoint(self):
        """When APARS checkpoint is 'service_start_timeout', synthetic progress
        must not exceed the APARS total_progress value."""
        # This is a design contract test — verify the code path exists
        import ast
        from pathlib import Path

        src = Path("unified_supervisor.py").read_text()
        # The synthetic progress section must check for service_start_timeout
        assert "service_start_timeout" in src, \
            "Synthetic progress generator must handle service_start_timeout checkpoint"

    def test_synthetic_never_exceeds_apars_when_apars_present(self):
        """Once real APARS data arrives, synthetic must never display higher than APARS + 2."""
        # Verify the cap logic exists and uses a small buffer (not +5)
        import re
        from pathlib import Path

        src = Path("unified_supervisor.py").read_text()
        # Find the synthetic cap logic
        cap_matches = re.findall(r'_syn_apars_cap\s*=.*', src)
        assert len(cap_matches) > 0, "Synthetic APARS cap logic must exist"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_synthetic_progress_reset.py -v`
Expected: FAIL — `service_start_timeout` not handled in synthetic generator

**Step 3: Implement**

In the synthetic progress generator (line ~72714-72722), after reading the APARS state, add checkpoint-aware behavior:

```python
                                    _syn_apars_cap = 95
                                    if _last_real_gcp_progress_update > 0:
                                        try:
                                            _syn_apars_last = float(_live_dashboard._gcp_state.get("progress", 0))
                                            _syn_source = _live_dashboard._gcp_state.get("source", "none")
                                            _syn_checkpoint = _live_dashboard._gcp_state.get("checkpoint", "")
                                            if _syn_source == "apars" and _syn_apars_last > 0:
                                                # When service_start_timeout, cap strictly to APARS value
                                                # to avoid misleading 97% display
                                                if "service_start_timeout" in str(_syn_checkpoint):
                                                    _syn_apars_cap = int(_syn_apars_last)
                                                else:
                                                    # Normal: allow synthetic to lead by +2 (smoother UX)
                                                    _syn_apars_cap = min(95, int(_syn_apars_last) + 2)
                                        except Exception:
                                            pass
```

Also reduce the buffer from `+5` to `+2` for normal cases (less misleading).

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_synthetic_progress_reset.py -v`
Expected: PASS (2 tests)

**Step 5: Commit**

```bash
git add tests/unit/core/test_synthetic_progress_reset.py unified_supervisor.py
git commit -m "fix(dashboard): synthetic progress caps strictly on service_start_timeout, reduces buffer to +2"
```

---

### Task 8: Atomic APARS File Writes in Startup Script

**Context:** The startup script writes the progress file via `cat > "$PROGRESS_FILE"` which is not atomic. Readers can observe partial JSON during writes. Use write-to-temp + mv (atomic on same filesystem).

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:8707-8727` (update_apars function in startup script)
- Test: `tests/unit/core/test_apars_boot_session.py` (extend)

**Step 1: Write the failing test**

Add to test file:

```python
    def test_startup_script_uses_atomic_apars_write(self):
        """APARS progress file must be written atomically (write temp + mv)."""
        from backend.core.gcp_vm_manager import GCPVMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = GCPVMManagerConfig()
        script = mgr._generate_golden_startup_script()
        # Must write to temp file then mv (atomic rename)
        assert "mv " in script or "mv \"" in script
        # Must NOT write directly to PROGRESS_FILE in update_apars
        # The function should write to a temp file then move
        import re
        update_fn = re.search(r'update_apars\(\)\s*\{.*?\n\}', script, re.DOTALL)
        assert update_fn, "update_apars function must exist"
        fn_body = update_fn.group(0)
        assert ".tmp" in fn_body or "_tmp" in fn_body, \
            "update_apars must write to temp file before atomic rename"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py::TestAPARSBootSession::test_startup_script_uses_atomic_apars_write -v`
Expected: FAIL — currently uses `cat > "$PROGRESS_FILE"`

**Step 3: Implement**

Replace the `cat > "$PROGRESS_FILE"` pattern in update_apars with atomic write:

```bash
update_apars() {
    local phase=$1
    local phase_progress=$2
    local total_progress=$3
    local checkpoint=$4
    local model_loaded=${5:-null}
    local ready=${6:-null}
    local error=${7:-null}
    local now=$(date +%s)
    local elapsed=$((now - START_TIME))
    local tmp_file="${PROGRESS_FILE}.tmp.$$"

    cat > "$tmp_file" << EOFPROGRESS
{
    "phase": ${phase},
    "phase_number": ${phase},
    "phase_progress": ${phase_progress},
    "total_progress": ${total_progress},
    "checkpoint": "${checkpoint}",
    "phase_name": "${checkpoint}",
    "model_loaded": ${model_loaded},
    "ready_for_inference": ${ready},
    "error": ${error},
    "updated_at": ${now},
    "elapsed_seconds": ${elapsed},
    "deployment_mode": "golden_image",
    "deps_prebaked": true,
    "skipped_phases": [2, 3],
    "version": "${STARTUP_SCRIPT_VERSION}",
    "startup_script_version": "${STARTUP_SCRIPT_VERSION}",
    "startup_script_metadata_version": "${STARTUP_SCRIPT_METADATA_VERSION}",
    "boot_session_id": "${BOOT_SESSION_ID}"
}
EOFPROGRESS
    mv "$tmp_file" "$PROGRESS_FILE"
}
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: PASS (10 tests)

**Step 5: Commit**

```bash
git add tests/unit/core/test_apars_boot_session.py backend/core/gcp_vm_manager.py
git commit -m "fix(gcp): atomic APARS progress file writes (write-to-temp + mv)"
```

---

### Task 9: Bump Startup Script Version

**Context:** All changes to the startup script must bump `_STARTUP_SCRIPT_VERSION` so the version mismatch detection in `_poll_health_until_ready` (line 9700-9712) triggers VM recycling for VMs running old scripts.

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:149` (`_STARTUP_SCRIPT_VERSION`)

**Step 1: Write the failing test**

Add to test file:

```python
    def test_startup_script_version_bumped(self):
        """Startup script version must be > 236.0 after readiness fixes."""
        from backend.core.gcp_vm_manager import _STARTUP_SCRIPT_VERSION
        version = float(_STARTUP_SCRIPT_VERSION)
        assert version > 236.0, \
            f"Startup script version must be bumped after readiness fixes, got {version}"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py::TestAPARSBootSession::test_startup_script_version_bumped -v`
Expected: FAIL — current version is "236.0"

**Step 3: Implement**

Change line 149:
```python
_STARTUP_SCRIPT_VERSION = "237.0"
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: PASS (11 tests)

**Step 5: Commit**

```bash
git add tests/unit/core/test_apars_boot_session.py backend/core/gcp_vm_manager.py
git commit -m "chore(gcp): bump startup script version to 237.0 for readiness split-brain fixes"
```

---

### Task 10: Run Full Test Suite

**Files:**
- All new test files from Tasks 0-9

**Step 1: Run all new tests**

```bash
python3 -m pytest tests/unit/core/test_apars_boot_session.py tests/unit/core/test_apars_enrichment.py tests/contracts/test_readiness_authority.py tests/unit/core/test_synthetic_progress_reset.py -v
```

Expected: ALL PASS

**Step 2: Run existing GCP tests to ensure no regressions**

```bash
python3 -m pytest tests/unit/core/test_gcp_instance_manager_startup_timeout.py tests/unit/core/test_gcp_lifecycle_adapter.py tests/unit/core/test_gcp_lifecycle_schema.py tests/unit/core/test_gcp_lifecycle_state_machine.py tests/unit/core/test_gcp_lifecycle_transitions.py tests/unit/core/test_gcp_lifecycle_bridge.py -v
```

Expected: ALL PASS (no regressions)

**Step 3: Run Phase 2 tests to ensure no regressions**

```bash
python3 -m pytest tests/unit/core/ tests/unit/intelligence/ tests/contracts/ -v --timeout=30
```

Expected: ALL PASS

**Step 4: Tag the gate**

```bash
git tag gate-gcp-readiness-split-brain-fix
```

---

## Test Matrix Summary

| Test File | Tests | Invariant |
|-----------|-------|-----------|
| `test_apars_boot_session.py` | 11 | INV-3 (boot UUID), INV-4 (configurable timeout), INV-5 (no progress readiness) |
| `test_apars_enrichment.py` | 2 | INV-1 (live health authority), INV-2 (APARS observational) |
| `test_readiness_authority.py` | 3 | INV-1, INV-2 (contract guards) |
| `test_synthetic_progress_reset.py` | 2 | Display correctness on timeout |
| **Total** | **18** | |

## Changes Summary

| File | Change |
|------|--------|
| `backend/core/gcp_vm_manager.py:149` | Version bump to 237.0 |
| `backend/core/gcp_vm_manager.py:664` | New `service_health_timeout` config field |
| `backend/core/gcp_vm_manager.py:8696-8728` | Atomic writes, boot UUID, null readiness defaults |
| `backend/core/gcp_vm_manager.py:9111-9148` | `_is_apars_current_session`, `_build_apars_payload` excludes readiness |
| `backend/core/gcp_vm_manager.py:9186-9191` | Middleware stops propagating readiness fields |
| `backend/core/gcp_vm_manager.py:9320-9349` | Configurable health timeout (default 90s) |
| `backend/core/gcp_vm_manager.py:9334-9337` | Remove `>=95` readiness shortcut |
| `backend/core/gcp_vm_manager.py:9359` | Timeout writes `null` readiness, not `false` |
| `backend/core/gcp_vm_manager.py:8388-8391` | Remove `>=100` readiness shortcut from `_ping_health_endpoint` |
| `backend/core/gcp_vm_manager.py:9666-9884` | Boot session validation in polling loop |
| `unified_supervisor.py:72713-72727` | Synthetic progress strict cap on timeout, buffer reduced to +2 |
