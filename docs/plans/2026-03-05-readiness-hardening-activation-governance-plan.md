# Readiness Hardening + Activation Governance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden GCP readiness semantics (8 items), build schema-enforced service governance (Wave 0), and activate 8 Immune-tier services under promoted-mode contracts (Wave 1).

**Architecture:** Extend-in-place (A+). Extend existing `ComponentDefinition`/`ComponentRegistry` with governance fields. Two-tier validation: legacy=warn, promoted=fail. Phase 1 fixes readiness bugs in `gcp_vm_manager.py`. Phase 2 adds governance types + validation. Phase 3 extracts and registers 8 enterprise services.

**Tech Stack:** Python 3.11, asyncio, dataclasses, pytest, AST-based contract tests

**Design Doc:** `docs/plans/2026-03-05-readiness-hardening-activation-governance-design.md`

---

## Phase 1: Readiness Hardening

### Task 1: HealthVerdict Enum + _ping_health_endpoint Refactor

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:188-196` (add HealthVerdict near existing helpers)
- Modify: `backend/core/gcp_vm_manager.py:8387-8442` (`_ping_health_endpoint`)
- Test: `tests/unit/core/test_health_verdict.py`

**Step 1: Write the failing test**

```python
"""Tests for HealthVerdict enum and _ping_health_endpoint refactor."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import aiohttp


class TestHealthVerdict:
    def test_health_verdict_enum_exists(self):
        from backend.core.gcp_vm_manager import HealthVerdict
        assert hasattr(HealthVerdict, "READY")
        assert hasattr(HealthVerdict, "ALIVE_NOT_READY")
        assert hasattr(HealthVerdict, "UNREACHABLE")
        assert hasattr(HealthVerdict, "UNHEALTHY")

    def test_health_verdict_values_are_strings(self):
        from backend.core.gcp_vm_manager import HealthVerdict
        assert HealthVerdict.READY.value == "ready"
        assert HealthVerdict.ALIVE_NOT_READY.value == "alive_not_ready"
        assert HealthVerdict.UNREACHABLE.value == "unreachable"
        assert HealthVerdict.UNHEALTHY.value == "unhealthy"


class TestPingHealthEndpointVerdict:
    def test_ping_returns_tuple_with_verdict(self):
        """_ping_health_endpoint must return (HealthVerdict, dict), not (bool, dict)."""
        import ast
        from pathlib import Path

        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                # Must reference HealthVerdict, not return bare True/False
                assert "HealthVerdict" in func_src, \
                    "_ping_health_endpoint must return HealthVerdict, not bool"
                # Must NOT have bare 'return True,' or 'return False,' patterns
                assert "return True," not in func_src, \
                    "Must not return raw bool from _ping_health_endpoint"
                assert "return False," not in func_src, \
                    "Must not return raw bool from _ping_health_endpoint"
                break
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py -v`
Expected: FAIL — `HealthVerdict` doesn't exist yet

**Step 3: Write minimal implementation**

Add `HealthVerdict` enum at line ~198 in `gcp_vm_manager.py` (after `_is_apars_current_session`):

```python
class HealthVerdict(Enum):
    """Health check verdict with explicit partial-degradation semantics.

    READY: liveness + readiness both true — service can accept work.
    ALIVE_NOT_READY: liveness true, readiness false — still booting, keep polling.
    UNREACHABLE: no response, timeout, or connection error.
    UNHEALTHY: responded but liveness check failed.
    """
    READY = "ready"
    ALIVE_NOT_READY = "alive_not_ready"
    UNREACHABLE = "unreachable"
    UNHEALTHY = "unhealthy"
```

Refactor `_ping_health_endpoint` (line 8387) to return `Tuple[HealthVerdict, Dict]`:

```python
async def _ping_health_endpoint(
    self, ip: str, port: int, timeout: float = 10.0
) -> Tuple['HealthVerdict', Dict[str, Any]]:
    import aiohttp
    url = f"http://{ip}:{port}/health"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    is_ready = data.get("ready_for_inference", False)
                    if not is_ready and data.get("model_loaded") and data.get("status") == "healthy":
                        is_ready = True
                    if not is_ready and data.get("status") == "healthy":
                        if data.get("phase") == "ready" or data.get("stage") == "ready":
                            is_ready = True
                        elif (data.get("model_load_progress_pct", 0) >= 100
                              and not data.get("model_loading_in_progress", True)):
                            is_ready = True
                    if is_ready:
                        try:
                            from backend.core.protocol_version_gate import validate_health_before_swap
                            _vok, _vreason = validate_health_before_swap("prime:/health", data)
                            if not _vok:
                                logger.warning(
                                    "[GCPVMManager] v276.0: VM health validation warning: %s",
                                    _vreason,
                                )
                        except ImportError:
                            pass
                        return HealthVerdict.READY, data
                    # Liveness: got 200 response with data, but not ready yet
                    return HealthVerdict.ALIVE_NOT_READY, data
                # Non-200 response — unhealthy
                return HealthVerdict.UNHEALTHY, {"status": resp.status}
    except asyncio.TimeoutError:
        return HealthVerdict.UNREACHABLE, {"error": "timeout"}
    except Exception as e:
        return HealthVerdict.UNREACHABLE, {"error": str(e)}
```

Update `_poll_health_until_ready` (line 9692) to use `HealthVerdict`:

```python
# Change line 9720 from:
#   is_ready, health_data = await self._ping_health_endpoint(ip, port, timeout=10.0)
# To:
verdict, health_data = await self._ping_health_endpoint(ip, port, timeout=10.0)

# Change line 9722 from:
#   if is_ready:
# To:
if verdict == HealthVerdict.READY:
```

Also update any other callers of `_ping_health_endpoint` to use `HealthVerdict` instead of bool.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py -v`
Expected: PASS

**Step 5: Run existing tests to verify no regressions**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py tests/contracts/test_readiness_authority.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_health_verdict.py
git commit -m "feat(gcp): add HealthVerdict enum, refactor _ping_health_endpoint to return verdict"
```

---

### Task 2: Process-Epoch Validation

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:188-196` (extend `_is_apars_current_session`)
- Modify: `backend/core/gcp_vm_manager.py:8710` (startup script — add PROCESS_EPOCH)
- Modify: `backend/core/gcp_vm_manager.py:8730-8752` (update_apars — add process_epoch field)
- Modify: `backend/core/gcp_vm_manager.py:9742-9756` (poll — validate process_epoch)
- Test: `tests/unit/core/test_apars_boot_session.py` (extend existing)

**Step 1: Write the failing test**

Add to `tests/unit/core/test_apars_boot_session.py`:

```python
class TestProcessEpochValidation:
    def test_startup_script_contains_process_epoch(self):
        """Startup script must generate a PROCESS_EPOCH."""
        script = _get_golden_startup_script()
        assert "PROCESS_EPOCH=" in script
        assert "process_epoch" in script

    def test_update_apars_includes_process_epoch(self):
        """update_apars JSON template must include process_epoch."""
        script = _get_golden_startup_script()
        match = re.search(
            r'cat > "\$tmp_file" << EOFPROGRESS\n(.*?)\nEOFPROGRESS',
            script,
            re.DOTALL,
        )
        assert match, "Could not find EOFPROGRESS heredoc"
        progress_json = match.group(1)
        assert '"process_epoch"' in progress_json
        assert "${PROCESS_EPOCH}" in progress_json

    def test_is_apars_current_session_validates_epoch(self):
        """Mismatched process_epoch within same boot must return False."""
        from backend.core.gcp_vm_manager import _is_apars_current_session
        # Same boot, same epoch → True
        assert _is_apars_current_session(
            "boot-A", expected="boot-A",
            process_epoch="epoch-1", expected_epoch="epoch-1",
        ) is True
        # Same boot, different epoch → False (stale from crashed process)
        assert _is_apars_current_session(
            "boot-A", expected="boot-A",
            process_epoch="epoch-2", expected_epoch="epoch-1",
        ) is False
        # Different boot → False (regardless of epoch)
        assert _is_apars_current_session(
            "boot-B", expected="boot-A",
            process_epoch="epoch-1", expected_epoch="epoch-1",
        ) is False

    def test_is_apars_current_session_unknown_epoch_accepted(self):
        """Unknown/empty process_epoch accepted for backward compat."""
        from backend.core.gcp_vm_manager import _is_apars_current_session
        assert _is_apars_current_session(
            "boot-A", expected="boot-A",
            process_epoch="", expected_epoch="epoch-1",
        ) is True
        assert _is_apars_current_session(
            "boot-A", expected="boot-A",
            process_epoch=None, expected_epoch="epoch-1",
        ) is True
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py::TestProcessEpochValidation -v`
Expected: FAIL — `_is_apars_current_session` doesn't accept epoch params

**Step 3: Write minimal implementation**

Update `_is_apars_current_session` (line 188):

```python
def _is_apars_current_session(
    session_id: str,
    expected: str,
    process_epoch: Optional[str] = None,
    expected_epoch: Optional[str] = None,
) -> bool:
    """Check if APARS data belongs to current boot session AND process epoch."""
    # Boot session check (backward compat: unknown/empty accepted)
    if not session_id or session_id == "unknown":
        return True
    if session_id != expected:
        return False
    # Process epoch check (backward compat: unknown/empty accepted)
    if not process_epoch or not expected_epoch:
        return True
    return process_epoch == expected_epoch
```

In the startup script (line 8710), add after `BOOT_SESSION_ID`:

```bash
PROCESS_EPOCH=$(python3 -c "import uuid; print(uuid.uuid4().hex[:12])" 2>/dev/null || echo "$$")
```

In `update_apars` heredoc (line 8730-8752), add field:

```json
    "process_epoch": "${PROCESS_EPOCH}",
```

In `_poll_health_until_ready` (line 9742-9756), add epoch tracking:

```python
# After self._current_boot_session_id tracking, add:
self._current_process_epoch = None  # Add to reset at line 9716

# In the APARS processing block, after boot session lock-on:
apars_epoch = apars.get("process_epoch", "")
if self._current_process_epoch:
    if not _is_apars_current_session(
        apars_session, self._current_boot_session_id,
        process_epoch=apars_epoch,
        expected_epoch=self._current_process_epoch,
    ):
        logger.warning(
            f"☁️ [InvincibleNode] Stale APARS data: "
            f"process_epoch={apars_epoch}, expected={self._current_process_epoch}"
        )
        has_real_data = False
else:
    if apars_epoch:
        self._current_process_epoch = apars_epoch
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_apars_boot_session.py
git commit -m "feat(gcp): add process-epoch validation for same-boot stale APARS detection"
```

---

### Task 3: Cross-Repo Contract Check

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:8387-8442` (`_ping_health_endpoint` — check contract_hash)
- Modify: `backend/core/gcp_vm_manager.py:8730-8752` (startup script — compute contract_hash)
- Test: `tests/unit/core/test_health_verdict.py` (extend)

**Step 1: Write the failing test**

Add to `test_health_verdict.py`:

```python
class TestContractHashCheck:
    def test_startup_script_computes_contract_hash(self):
        """Startup script must compute and include contract_hash."""
        from backend.core.gcp_vm_manager import VMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = VMManagerConfig()
        script = mgr._generate_golden_startup_script()
        assert "CONTRACT_HASH=" in script
        assert "contract_hash" in script

    def test_ping_health_logs_contract_mismatch(self):
        """AST check: _ping_health_endpoint must reference contract_hash."""
        import ast
        from pathlib import Path
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "contract_hash" in func_src, \
                    "_ping_health_endpoint must check contract_hash"
                break
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py::TestContractHashCheck -v`
Expected: FAIL

**Step 3: Write minimal implementation**

In startup script, after `PROCESS_EPOCH`, compute hash:

```bash
# Compute contract hash from key package versions
CONTRACT_HASH=$(python3 -c "
import hashlib, importlib.metadata as m
pkgs = sorted(['torch', 'transformers', 'llama-cpp-python', 'fastapi', 'uvicorn'])
versions = []
for p in pkgs:
    try: versions.append(f'{p}={m.version(p)}')
    except: versions.append(f'{p}=unknown')
print(hashlib.sha256('|'.join(versions).encode()).hexdigest()[:16])
" 2>/dev/null || echo "unknown")
```

Add `"contract_hash": "${CONTRACT_HASH}"` to `update_apars` heredoc.

In `_ping_health_endpoint`, after APARS extraction, add contract hash advisory check:

```python
# After is_ready determination, before return:
apars_data = data.get("apars", {})
if isinstance(apars_data, dict):
    remote_hash = apars_data.get("contract_hash", "")
    if remote_hash and hasattr(self, '_expected_contract_hash') and self._expected_contract_hash:
        if remote_hash != self._expected_contract_hash:
            logger.warning(
                "[GCPVMManager] Contract hash mismatch: remote=%s, expected=%s",
                remote_hash, self._expected_contract_hash,
            )
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_health_verdict.py
git commit -m "feat(gcp): add contract_hash for cross-repo version mismatch detection"
```

---

### Task 4: Correlation ID Propagation

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:8387-8442` (`_ping_health_endpoint` — send X-Correlation-ID)
- Modify: `backend/core/gcp_vm_manager.py:9199` (APARS middleware — log correlation ID)
- Test: `tests/unit/core/test_health_verdict.py` (extend)

**Step 1: Write the failing test**

```python
class TestCorrelationIdPropagation:
    def test_ping_health_sends_correlation_header(self):
        """AST check: _ping_health_endpoint must send X-Correlation-ID."""
        import ast
        from pathlib import Path
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "X-Correlation-ID" in func_src or "correlation_id" in func_src, \
                    "_ping_health_endpoint must propagate correlation ID"
                break
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py::TestCorrelationIdPropagation -v`
Expected: FAIL

**Step 3: Write minimal implementation**

In `_ping_health_endpoint`, add correlation ID to request:

```python
import uuid as _uuid
correlation_id = str(_uuid.uuid4())
headers = {"X-Correlation-ID": correlation_id}

async with aiohttp.ClientSession() as session:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), headers=headers) as resp:
        ...
```

In APARS middleware (embedded Python in startup script), extract and log correlation ID:

```python
# In the ASGI middleware __call__, extract from headers:
correlation_id = None
for header_name, header_value in scope.get("headers", []):
    if header_name == b"x-correlation-id":
        correlation_id = header_value.decode()
        break
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_health_verdict.py
git commit -m "feat(gcp): propagate X-Correlation-ID from supervisor health probe to VM"
```

---

### Task 5: Readiness Hysteresis

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:683-686` (`VMManagerConfig` — add hysteresis config)
- Modify: `backend/core/gcp_vm_manager.py:9692-9730` (`_poll_health_until_ready` — add hysteresis)
- Test: `tests/unit/core/test_health_verdict.py` (extend)

**Step 1: Write the failing test**

```python
class TestReadinessHysteresis:
    def test_config_has_hysteresis_fields(self):
        """VMManagerConfig must have hysteresis configuration."""
        from backend.core.gcp_vm_manager import VMManagerConfig
        config = VMManagerConfig()
        assert hasattr(config, "readiness_hysteresis_up")
        assert hasattr(config, "readiness_hysteresis_down")
        assert config.readiness_hysteresis_up >= 2
        assert config.readiness_hysteresis_down >= 1

    def test_hysteresis_default_values(self):
        """Default hysteresis: up=3, down=2."""
        from backend.core.gcp_vm_manager import VMManagerConfig
        config = VMManagerConfig()
        assert config.readiness_hysteresis_up == 3
        assert config.readiness_hysteresis_down == 2
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py::TestReadinessHysteresis -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Add to `VMManagerConfig` after `service_health_timeout` (line ~686):

```python
# Readiness hysteresis: consecutive checks required for state transition
readiness_hysteresis_up: int = field(
    default_factory=lambda: int(os.getenv("GCP_READINESS_HYSTERESIS_UP", "3"))
)
readiness_hysteresis_down: int = field(
    default_factory=lambda: int(os.getenv("GCP_READINESS_HYSTERESIS_DOWN", "2"))
)
```

In `_poll_health_until_ready`, add hysteresis tracking:

```python
# After existing reset variables (line ~9716):
_consecutive_ready = 0
_consecutive_not_ready = 0
_hysteresis_met = False

# Replace the simple verdict check with hysteresis:
if verdict == HealthVerdict.READY:
    _consecutive_ready += 1
    _consecutive_not_ready = 0
    if _consecutive_ready >= self.config.readiness_hysteresis_up:
        _hysteresis_met = True
        if progress_callback:
            try:
                progress_callback(100, "ready", f"VM ready at {ip}")
            except Exception:
                pass
        return True, "ready_for_inference"
    else:
        logger.info(
            f"☁️ [InvincibleNode] Health READY ({_consecutive_ready}/"
            f"{self.config.readiness_hysteresis_up} for hysteresis)"
        )
else:
    _consecutive_not_ready += 1
    _consecutive_ready = 0
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py tests/unit/core/test_apars_boot_session.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_health_verdict.py
git commit -m "feat(gcp): add readiness hysteresis (N-consecutive-healthy debounce)"
```

---

### Task 6: Timeout Policy Tiering

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:683-686` (`VMManagerConfig` — profile support)
- Test: `tests/unit/core/test_health_verdict.py` (extend)

**Step 1: Write the failing test**

```python
class TestTimeoutPolicyTiering:
    def test_timeout_profiles_exist(self):
        """VMManagerConfig must support GCP_TIMEOUT_PROFILE."""
        from backend.core.gcp_vm_manager import VMManagerConfig, TIMEOUT_PROFILES
        assert "dev" in TIMEOUT_PROFILES
        assert "staging" in TIMEOUT_PROFILES
        assert "production" in TIMEOUT_PROFILES
        assert "golden_image" in TIMEOUT_PROFILES

    def test_timeout_profile_values(self):
        from backend.core.gcp_vm_manager import TIMEOUT_PROFILES
        assert TIMEOUT_PROFILES["dev"] == 30.0
        assert TIMEOUT_PROFILES["staging"] == 60.0
        assert TIMEOUT_PROFILES["production"] == 90.0
        assert TIMEOUT_PROFILES["golden_image"] == 120.0

    def test_explicit_timeout_overrides_profile(self):
        """Explicit GCP_SERVICE_HEALTH_TIMEOUT overrides profile."""
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {
            "GCP_TIMEOUT_PROFILE": "dev",
            "GCP_SERVICE_HEALTH_TIMEOUT": "200.0",
        }):
            from backend.core.gcp_vm_manager import VMManagerConfig
            config = VMManagerConfig()
            assert config.service_health_timeout == 200.0
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py::TestTimeoutPolicyTiering -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Add near top of file (after imports):

```python
TIMEOUT_PROFILES: Dict[str, float] = {
    "dev": 30.0,
    "staging": 60.0,
    "production": 90.0,
    "golden_image": 120.0,
}
```

Update `service_health_timeout` in `VMManagerConfig`:

```python
service_health_timeout: float = field(
    default_factory=lambda: float(
        os.getenv(
            "GCP_SERVICE_HEALTH_TIMEOUT",
            str(TIMEOUT_PROFILES.get(
                os.getenv("GCP_TIMEOUT_PROFILE", "production"),
                90.0,
            ))
        )
    )
)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_health_verdict.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_health_verdict.py
git commit -m "feat(gcp): add timeout policy tiering (dev/staging/production/golden_image profiles)"
```

---

### Task 7: Stale Metadata GC

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:8710-8753` (startup script — add GC preamble)
- Test: `tests/unit/core/test_apars_boot_session.py` (extend)

**Step 1: Write the failing test**

```python
class TestStaleMetadataGC:
    def test_startup_script_has_gc_logic(self):
        """Startup script must clean up stale progress files."""
        script = _get_golden_startup_script()
        assert "APARS_FILE_MAX_AGE_S" in script
        assert "stale" in script.lower() or "cleanup" in script.lower() or "gc" in script.lower()

    def test_startup_script_archives_prev(self):
        """Startup script must archive previous progress file."""
        script = _get_golden_startup_script()
        assert ".prev.json" in script or "prev" in script
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py::TestStaleMetadataGC -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Add GC preamble to startup script after `BOOT_SESSION_ID` and `PROCESS_EPOCH` lines (before `update_apars` function):

```bash
# APARS Stale Metadata GC
APARS_FILE_MAX_AGE_S=${APARS_FILE_MAX_AGE_S:-3600}
if [ -f "$PROGRESS_FILE" ]; then
    # Check if stale from previous boot
    _existing_session=$(python3 -c "
import json, sys
try:
    with open('$PROGRESS_FILE') as f:
        d = json.load(f)
    print(d.get('boot_session_id', 'unknown'))
except: print('unknown')
" 2>/dev/null)
    if [ "$_existing_session" != "unknown" ] && [ "$_existing_session" != "$BOOT_SESSION_ID" ]; then
        log "APARS GC: Removing stale progress file from previous boot (session=$_existing_session)"
        rm -f "$PROGRESS_FILE"
    elif [ "$_existing_session" = "$BOOT_SESSION_ID" ]; then
        # Same boot, different process — archive
        log "APARS GC: Archiving previous process progress file"
        mv "$PROGRESS_FILE" "${PROGRESS_FILE%.json}.prev.json" 2>/dev/null || true
    fi
    # Age-based cleanup
    if [ -f "$PROGRESS_FILE" ]; then
        _file_age=$(( $(date +%s) - $(stat -c %Y "$PROGRESS_FILE" 2>/dev/null || stat -f %m "$PROGRESS_FILE" 2>/dev/null || echo 0) ))
        if [ "$_file_age" -gt "$APARS_FILE_MAX_AGE_S" ]; then
            log "APARS GC: Removing aged progress file (${_file_age}s old, max=${APARS_FILE_MAX_AGE_S}s)"
            rm -f "$PROGRESS_FILE"
        fi
    fi
fi
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_apars_boot_session.py
git commit -m "feat(gcp): add APARS stale metadata GC (age-based + boot-session cleanup)"
```

---

### Task 8: Atomic Write Filesystem Boundary Guard

**Files:**
- Modify: `backend/core/gcp_vm_manager.py:8718-8753` (startup script — add mount check)
- Test: `tests/unit/core/test_apars_boot_session.py` (extend)

**Step 1: Write the failing test**

```python
class TestAtomicWriteBoundary:
    def test_startup_script_checks_filesystem_boundary(self):
        """Startup script must verify temp and target are on same mount."""
        script = _get_golden_startup_script()
        # Must check filesystem/mount for atomicity
        assert "df " in script or "mount" in script or "same_fs" in script or "same_mount" in script
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py::TestAtomicWriteBoundary -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Add filesystem boundary check at the start of `update_apars` function:

```bash
update_apars() {
    local phase=$1
    ...
    local tmp_file="${PROGRESS_FILE}.tmp.$$"

    # Verify atomic rename safety (same filesystem)
    local _target_mount=$(df -P "$(dirname "$PROGRESS_FILE")" 2>/dev/null | tail -1 | awk '{print $6}')
    local _tmp_mount=$(df -P "$(dirname "$tmp_file")" 2>/dev/null | tail -1 | awk '{print $6}')
    if [ -n "$_target_mount" ] && [ -n "$_tmp_mount" ] && [ "$_target_mount" != "$_tmp_mount" ]; then
        log "WARNING: APARS temp and target on different mounts ($_tmp_mount vs $_target_mount). Using flock fallback."
        # Fallback: write directly with flock
        flock -w 5 "$PROGRESS_FILE" cat > "$PROGRESS_FILE" << EOFPROGRESS
        ...
        return
    fi

    cat > "$tmp_file" << EOFPROGRESS
    ...
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_apars_boot_session.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_apars_boot_session.py
git commit -m "feat(gcp): add atomic write filesystem boundary guard with flock fallback"
```

---

### Task 9: Phase 1 Exit Gate Tests

**Files:**
- Create: `tests/contracts/test_phase1_exit_gate.py`

**Step 1: Write the exit gate tests**

```python
"""Phase 1 Exit Gate: all readiness hardening invariants must hold."""
import ast
import re
from pathlib import Path


class TestPhase1ExitGate:
    def test_no_progress_readiness_coupling(self):
        """INV-5: total_progress must never determine readiness."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "total_progress" not in func_src

    def test_health_verdict_used_not_bool(self):
        """_ping_health_endpoint must return HealthVerdict, not bool."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "HealthVerdict" in func_src
                assert "return True," not in func_src
                assert "return False," not in func_src

    def test_correlation_id_sent(self):
        """_ping_health_endpoint must send X-Correlation-ID."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "Correlation-ID" in func_src or "correlation_id" in func_src

    def test_process_epoch_in_startup_script(self):
        """Startup script must include process_epoch."""
        from backend.core.gcp_vm_manager import VMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = VMManagerConfig()
        script = mgr._generate_golden_startup_script()
        assert "PROCESS_EPOCH=" in script
        assert '"process_epoch"' in script

    def test_hysteresis_config_exists(self):
        """VMManagerConfig must have hysteresis fields."""
        from backend.core.gcp_vm_manager import VMManagerConfig
        config = VMManagerConfig()
        assert hasattr(config, "readiness_hysteresis_up")
        assert config.readiness_hysteresis_up >= 2

    def test_timeout_profiles_exist(self):
        """Timeout profiles must be defined."""
        from backend.core.gcp_vm_manager import TIMEOUT_PROFILES
        assert len(TIMEOUT_PROFILES) >= 4
```

**Step 2: Run all Phase 1 tests**

Run: `python3 -m pytest tests/contracts/test_phase1_exit_gate.py tests/unit/core/test_health_verdict.py tests/unit/core/test_apars_boot_session.py tests/contracts/test_readiness_authority.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add tests/contracts/test_phase1_exit_gate.py
git commit -m "test(contracts): add Phase 1 readiness hardening exit gate tests"
```

---

## Phase 2: Wave 0 — Governance Infrastructure

### Task 10: Governance Enums and Dataclasses

**Files:**
- Modify: `backend/core/component_registry.py:1-16` (add imports)
- Modify: `backend/core/component_registry.py:58-59` (add new enums after existing ones)
- Test: `tests/unit/core/test_governance_types.py`

**Step 1: Write the failing test**

```python
"""Tests for governance type definitions."""


class TestGovernanceEnums:
    def test_promotion_level(self):
        from backend.core.component_registry import PromotionLevel
        assert PromotionLevel.LEGACY.value == "legacy"
        assert PromotionLevel.PROMOTED.value == "promoted"

    def test_activation_mode(self):
        from backend.core.component_registry import ActivationMode
        assert len(ActivationMode) == 4
        assert hasattr(ActivationMode, "ALWAYS_ON")
        assert hasattr(ActivationMode, "WARM_STANDBY")
        assert hasattr(ActivationMode, "EVENT_DRIVEN")
        assert hasattr(ActivationMode, "BATCH_WINDOW")

    def test_readiness_class(self):
        from backend.core.component_registry import ReadinessClass
        assert len(ReadinessClass) == 3
        assert hasattr(ReadinessClass, "BLOCK_READY")
        assert hasattr(ReadinessClass, "NON_BLOCKING")
        assert hasattr(ReadinessClass, "DEFERRED_AFTER_READY")

    def test_activation_tier_is_int_enum(self):
        from backend.core.component_registry import ActivationTier
        assert ActivationTier.FOUNDATION < ActivationTier.IMMUNE
        assert ActivationTier.IMMUNE < ActivationTier.NERVOUS
        assert ActivationTier.NERVOUS < ActivationTier.METABOLIC
        assert ActivationTier.METABOLIC < ActivationTier.HIGHER
        # IntEnum comparison
        assert ActivationTier.FOUNDATION == 0
        assert ActivationTier.HIGHER == 4

    def test_retry_strategy(self):
        from backend.core.component_registry import RetryStrategy
        assert len(RetryStrategy) == 4

    def test_ownership_mode(self):
        from backend.core.component_registry import OwnershipMode
        assert hasattr(OwnershipMode, "EXCLUSIVE_WRITE")
        assert hasattr(OwnershipMode, "SHARED_READ_ONLY")


class TestGovernanceDataclasses:
    def test_resource_budget_frozen(self):
        from backend.core.component_registry import ResourceBudget
        b = ResourceBudget(max_memory_mb=64, max_cpu_percent=10.0,
                           max_concurrency=4, max_startup_time_s=30.0)
        assert b.max_memory_mb == 64
        import pytest
        with pytest.raises(AttributeError):
            b.max_memory_mb = 128  # frozen

    def test_failure_policy_uses_retry_strategy_enum(self):
        from backend.core.component_registry import FailurePolicy, RetryStrategy
        fp = FailurePolicy(
            retry_strategy=RetryStrategy.EXP_BACKOFF_JITTER,
            max_retries=3, backoff_base_s=1.0, backoff_max_s=30.0,
            circuit_breaker=True, breaker_threshold=5,
            breaker_recovery_s=60.0, quarantine_on_repeated=True,
        )
        assert fp.retry_strategy == RetryStrategy.EXP_BACKOFF_JITTER

    def test_state_domain_frozen(self):
        from backend.core.component_registry import StateDomain, OwnershipMode
        sd = StateDomain(domain="security_policy", ownership_mode=OwnershipMode.EXCLUSIVE_WRITE)
        assert sd.domain == "security_policy"

    def test_observability_contract_defaults(self):
        from backend.core.component_registry import ObservabilityContract
        oc = ObservabilityContract()
        assert oc.schema_version == "1.0"
        assert oc.emit_trace_id is True
        assert "trace_id" in oc.required_log_fields

    def test_health_policy_defaults(self):
        from backend.core.component_registry import HealthPolicy
        hp = HealthPolicy()
        assert hp.supports_liveness is True
        assert hp.hysteresis_window == 3
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_governance_types.py -v`
Expected: FAIL — enums don't exist yet

**Step 3: Write minimal implementation**

Add to `backend/core/component_registry.py` after existing enums (after line 58), before `ComponentDefinition`:

```python
from enum import IntEnum

class PromotionLevel(Enum):
    LEGACY = "legacy"
    PROMOTED = "promoted"

class ActivationMode(Enum):
    ALWAYS_ON = "always_on"
    WARM_STANDBY = "warm_standby"
    EVENT_DRIVEN = "event_driven"
    BATCH_WINDOW = "batch_window"

class ReadinessClass(Enum):
    BLOCK_READY = "block_ready"
    NON_BLOCKING = "non_blocking"
    DEFERRED_AFTER_READY = "deferred_after_ready"

class ActivationTier(IntEnum):
    FOUNDATION = 0
    IMMUNE = 1
    NERVOUS = 2
    METABOLIC = 3
    HIGHER = 4

class RetryStrategy(Enum):
    NONE = "none"
    FIXED_DELAY = "fixed_delay"
    EXP_BACKOFF = "exp_backoff"
    EXP_BACKOFF_JITTER = "exp_backoff_jitter"

class OwnershipMode(Enum):
    EXCLUSIVE_WRITE = "exclusive_write"
    SHARED_READ_ONLY = "shared_read_only"


@dataclass(frozen=True)
class ResourceBudget:
    max_memory_mb: int
    max_cpu_percent: float
    max_concurrency: int
    max_startup_time_s: float

@dataclass(frozen=True)
class FailurePolicy:
    retry_strategy: RetryStrategy
    max_retries: int
    backoff_base_s: float
    backoff_max_s: float
    circuit_breaker: bool
    breaker_threshold: int
    breaker_recovery_s: float
    quarantine_on_repeated: bool

@dataclass(frozen=True)
class StateDomain:
    domain: str
    ownership_mode: OwnershipMode

@dataclass(frozen=True)
class ObservabilityContract:
    schema_version: str = "1.0"
    emit_trace_id: bool = True
    emit_reason_codes: bool = True
    required_log_fields: tuple = (
        "trace_id", "reason_code", "service_name",
        "activation_mode", "readiness_class",
    )
    health_check_interval_s: float = 30.0

@dataclass(frozen=True)
class HealthPolicy:
    supports_liveness: bool = True
    supports_readiness: bool = True
    supports_drain: bool = False
    hysteresis_window: int = 3
    health_check_timeout_s: float = 5.0
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_governance_types.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/component_registry.py tests/unit/core/test_governance_types.py
git commit -m "feat(governance): add governance enums and frozen dataclasses"
```

---

### Task 11: Extend ComponentDefinition with Governance Fields

**Files:**
- Modify: `backend/core/component_registry.py:69-124` (`ComponentDefinition`)
- Test: `tests/unit/core/test_governance_types.py` (extend)

**Step 1: Write the failing test**

```python
class TestComponentDefinitionGovernanceFields:
    def test_legacy_default(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
        )
        defn = ComponentDefinition(
            name="test", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        assert defn.promotion_level == PromotionLevel.LEGACY
        assert defn.activation_mode is None
        assert defn.constructor_pure is False

    def test_promoted_all_fields(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
            ActivationMode, ReadinessClass, ActivationTier, ResourceBudget,
            FailurePolicy, RetryStrategy, StateDomain, OwnershipMode,
            ObservabilityContract, HealthPolicy,
        )
        defn = ComponentDefinition(
            name="test_promoted",
            criticality=Criticality.REQUIRED,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
            activation_mode=ActivationMode.ALWAYS_ON,
            readiness_class=ReadinessClass.BLOCK_READY,
            activation_tier=ActivationTier.IMMUNE,
            resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
            failure_policy_gov=FailurePolicy(
                RetryStrategy.EXP_BACKOFF, 3, 1.0, 30.0, True, 5, 60.0, True,
            ),
            state_domain=StateDomain("test_domain", OwnershipMode.EXCLUSIVE_WRITE),
            observability_contract=ObservabilityContract(),
            health_policy=HealthPolicy(),
            constructor_pure=True,
            contract_version="1.0.0",
        )
        assert defn.promotion_level == PromotionLevel.PROMOTED
        assert defn.activation_tier == ActivationTier.IMMUNE
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_governance_types.py::TestComponentDefinitionGovernanceFields -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Add governance fields to `ComponentDefinition` (after existing fields, before `effective_criticality`):

```python
    # --- Governance fields (required for PROMOTED, optional for LEGACY) ---
    promotion_level: PromotionLevel = PromotionLevel.LEGACY
    activation_mode: Optional[ActivationMode] = None
    readiness_class: Optional[ReadinessClass] = None
    activation_tier: Optional[ActivationTier] = None
    resource_budget: Optional[ResourceBudget] = None
    failure_policy_gov: Optional[FailurePolicy] = None
    state_domain: Optional[StateDomain] = None
    observability_contract: Optional[ObservabilityContract] = None
    health_policy: Optional[HealthPolicy] = None
    constructor_pure: bool = False
    contract_version: Optional[str] = None
    contract_hash: Optional[str] = None

    # --- Kill-switch hierarchy ---
    kill_switch_env: Optional[str] = None
    tier_kill_switch_env: Optional[str] = None

    # --- Cross-tier dependency guard ---
    max_dependency_tier: Optional[int] = None
    cross_tier_dependency_allowlist: Tuple[str, ...] = ()
```

Add `Tuple` to the typing import at the top of the file.

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_governance_types.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/component_registry.py tests/unit/core/test_governance_types.py
git commit -m "feat(governance): extend ComponentDefinition with governance fields"
```

---

### Task 12: Registry Validation — Kill-Switch + Promoted Validation

**Files:**
- Modify: `backend/core/component_registry.py:161-284` (`ComponentRegistry`)
- Test: `tests/contracts/test_governance_validation.py`

**Step 1: Write the failing test**

```python
"""Tests for governance validation in ComponentRegistry."""
import os
import pytest
from unittest.mock import patch


class TestKillSwitchHierarchy:
    def _make_promoted(self, name="test_svc", **overrides):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
            ActivationMode, ReadinessClass, ActivationTier, ResourceBudget,
            FailurePolicy, RetryStrategy, StateDomain, OwnershipMode,
            ObservabilityContract,
        )
        defaults = dict(
            name=name,
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
            activation_mode=ActivationMode.ALWAYS_ON,
            readiness_class=ReadinessClass.NON_BLOCKING,
            activation_tier=ActivationTier.IMMUNE,
            resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
            failure_policy_gov=FailurePolicy(
                RetryStrategy.EXP_BACKOFF, 3, 1.0, 30.0, True, 5, 60.0, True,
            ),
            state_domain=StateDomain(f"{name}_domain", OwnershipMode.EXCLUSIVE_WRITE),
            observability_contract=ObservabilityContract(),
            constructor_pure=True,
            kill_switch_env=f"JARVIS_SVC_{name.upper()}_ENABLED",
            tier_kill_switch_env="JARVIS_TIER_IMMUNE_ENABLED",
        )
        defaults.update(overrides)
        return ComponentDefinition(**defaults)

    def test_global_kill_switch_disables_all(self):
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        reg = ComponentRegistry()
        with patch.dict(os.environ, {"JARVIS_PROMOTED_SERVICES_ENABLED": "false"}):
            defn = self._make_promoted()
            state = reg.register(defn)
            assert state.status == ComponentStatus.DISABLED

    def test_tier_kill_switch_disables_tier(self):
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        reg = ComponentRegistry()
        with patch.dict(os.environ, {"JARVIS_TIER_IMMUNE_ENABLED": "false"}):
            defn = self._make_promoted()
            state = reg.register(defn)
            assert state.status == ComponentStatus.DISABLED

    def test_service_kill_switch_disables_one(self):
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        reg = ComponentRegistry()
        with patch.dict(os.environ, {"JARVIS_SVC_TEST_SVC_ENABLED": "false"}):
            defn = self._make_promoted()
            state = reg.register(defn)
            assert state.status == ComponentStatus.DISABLED

    def test_unset_kill_switches_means_enabled(self):
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        reg = ComponentRegistry()
        env = {
            "JARVIS_PROMOTED_SERVICES_ENABLED": "",
            "JARVIS_TIER_IMMUNE_ENABLED": "",
            "JARVIS_SVC_TEST_SVC_ENABLED": "",
        }
        with patch.dict(os.environ, env, clear=False):
            # Remove the keys entirely
            for k in env:
                os.environ.pop(k, None)
            defn = self._make_promoted()
            state = reg.register(defn)
            assert state.status != ComponentStatus.DISABLED


class TestPromotedValidation:
    def _make_promoted(self, **overrides):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
            ActivationMode, ReadinessClass, ActivationTier, ResourceBudget,
            FailurePolicy, RetryStrategy, StateDomain, OwnershipMode,
            ObservabilityContract,
        )
        defaults = dict(
            name="test_promoted",
            criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
            activation_mode=ActivationMode.ALWAYS_ON,
            readiness_class=ReadinessClass.NON_BLOCKING,
            activation_tier=ActivationTier.IMMUNE,
            resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
            failure_policy_gov=FailurePolicy(
                RetryStrategy.EXP_BACKOFF, 3, 1.0, 30.0, True, 5, 60.0, True,
            ),
            state_domain=StateDomain("test_domain", OwnershipMode.EXCLUSIVE_WRITE),
            observability_contract=ObservabilityContract(),
            constructor_pure=True,
        )
        defaults.update(overrides)
        return ComponentDefinition(**defaults)

    def test_promoted_missing_activation_mode_fails(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted(activation_mode=None)
        with pytest.raises(ValueError, match="activation_mode"):
            reg.register(defn)

    def test_promoted_missing_readiness_class_fails(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted(readiness_class=None)
        with pytest.raises(ValueError, match="readiness_class"):
            reg.register(defn)

    def test_promoted_missing_resource_budget_fails(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted(resource_budget=None)
        with pytest.raises(ValueError, match="resource_budget"):
            reg.register(defn)

    def test_promoted_constructor_not_pure_fails(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted(constructor_pure=False)
        with pytest.raises(ValueError, match="constructor_pure"):
            reg.register(defn)

    def test_promoted_complete_succeeds(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn = self._make_promoted()
        state = reg.register(defn)
        assert state.definition.name == "test_promoted"

    def test_state_domain_conflict_rejected(self):
        from backend.core.component_registry import ComponentRegistry
        reg = ComponentRegistry()
        defn1 = self._make_promoted(name="svc_a", state_domain=StateDomain("shared", OwnershipMode.EXCLUSIVE_WRITE))
        defn2 = self._make_promoted(name="svc_b", state_domain=StateDomain("shared", OwnershipMode.EXCLUSIVE_WRITE))
        reg.register(defn1)
        with pytest.raises(ValueError, match="State domain.*already owned"):
            reg.register(defn2)

    def test_criticality_readiness_conflict(self):
        from backend.core.component_registry import ComponentRegistry, Criticality, ReadinessClass
        reg = ComponentRegistry()
        defn = self._make_promoted(
            criticality=Criticality.REQUIRED,
            readiness_class=ReadinessClass.DEFERRED_AFTER_READY,
        )
        with pytest.raises(ValueError, match="REQUIRED.*DEFERRED"):
            reg.register(defn)

    def test_cross_tier_dependency_rejected(self):
        from backend.core.component_registry import (
            ComponentRegistry, ComponentDefinition, Criticality, ProcessType,
            PromotionLevel, ActivationTier,
        )
        reg = ComponentRegistry()
        # Register a higher-tier component first
        higher = ComponentDefinition(
            name="higher_svc", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            activation_tier=ActivationTier.METABOLIC,
        )
        reg.register(higher)
        # Try to register immune-tier depending on metabolic
        defn = self._make_promoted(
            dependencies=["higher_svc"],
            max_dependency_tier=ActivationTier.FOUNDATION,
        )
        with pytest.raises(ValueError, match="exceeds max_dependency_tier"):
            reg.register(defn)

    def test_cross_tier_allowlist_permits(self):
        from backend.core.component_registry import (
            ComponentRegistry, ComponentDefinition, Criticality, ProcessType,
            ActivationTier,
        )
        reg = ComponentRegistry()
        higher = ComponentDefinition(
            name="higher_svc", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            activation_tier=ActivationTier.METABOLIC,
        )
        reg.register(higher)
        defn = self._make_promoted(
            dependencies=["higher_svc"],
            max_dependency_tier=ActivationTier.FOUNDATION,
            cross_tier_dependency_allowlist=("higher_svc",),
        )
        state = reg.register(defn)
        assert state.definition.name == "test_promoted"


class TestLegacyValidation:
    def test_legacy_warns_but_succeeds(self):
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, ComponentRegistry,
        )
        reg = ComponentRegistry()
        defn = ComponentDefinition(
            name="legacy_svc", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        state = reg.register(defn)
        assert state.definition.name == "legacy_svc"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/contracts/test_governance_validation.py -v`
Expected: FAIL — validation logic doesn't exist

**Step 3: Write minimal implementation**

Add to `ComponentRegistry.__init__`:

```python
self._state_domains: Dict[str, str] = {}  # domain -> component name
self._health_probes: Dict[str, 'HealthProbeSet'] = {}
```

Add validation methods and modify `register()`:

```python
_GLOBAL_KILL_SWITCH = "JARVIS_PROMOTED_SERVICES_ENABLED"

def register(self, definition: ComponentDefinition) -> ComponentState:
    # Kill-switch check
    killed, reason = self._check_kill_switch(definition)
    if killed:
        state = ComponentState(definition=definition)
        state.mark_disabled(reason)
        self._components[definition.name] = state
        logger.info(f"Component {definition.name} disabled by kill-switch: {reason}")
        return state

    # Promotion-level validation
    if definition.promotion_level == PromotionLevel.PROMOTED:
        self._validate_promoted(definition)
    elif definition.promotion_level == PromotionLevel.LEGACY:
        self._warn_missing_governance(definition)

    # --- Existing registration logic ---
    if definition.name in self._components:
        logger.warning(f"Component {definition.name} already registered, updating")
    state = ComponentState(definition=definition)
    self._components[definition.name] = state
    for cap in definition.provides_capabilities:
        if cap in self._capabilities:
            logger.debug(...)
        self._capabilities[cap] = definition.name
    # Track state domain
    if (definition.state_domain and
            definition.state_domain.ownership_mode == OwnershipMode.EXCLUSIVE_WRITE):
        self._state_domains[definition.state_domain.domain] = definition.name
    logger.debug(f"Registered component: {definition.name}")
    return state

def _check_kill_switch(self, defn: ComponentDefinition) -> Tuple[bool, str]:
    if defn.promotion_level == PromotionLevel.PROMOTED:
        global_val = os.environ.get(_GLOBAL_KILL_SWITCH, "true").lower()
        if global_val in ("false", "0", "no", "disabled"):
            return True, "global_kill_switch"
    if defn.tier_kill_switch_env:
        tier_val = os.environ.get(defn.tier_kill_switch_env, "true").lower()
        if tier_val in ("false", "0", "no", "disabled"):
            return True, f"tier_kill_switch:{defn.tier_kill_switch_env}"
    if defn.kill_switch_env:
        svc_val = os.environ.get(defn.kill_switch_env, "true").lower()
        if svc_val in ("false", "0", "no", "disabled"):
            return True, f"service_kill_switch:{defn.kill_switch_env}"
    return False, ""

def _validate_promoted(self, defn: ComponentDefinition) -> None:
    errors = []
    for field_name in ("activation_mode", "readiness_class", "resource_budget",
                       "failure_policy_gov", "state_domain", "observability_contract"):
        if getattr(defn, field_name) is None:
            errors.append(f"Missing required field: {field_name}")
    if not defn.constructor_pure:
        errors.append("constructor_pure must be True for promoted services")
    if defn.state_domain and defn.state_domain.ownership_mode == OwnershipMode.EXCLUSIVE_WRITE:
        existing = self._state_domains.get(defn.state_domain.domain)
        if existing and existing != defn.name:
            errors.append(f"State domain '{defn.state_domain.domain}' already owned by '{existing}'")
    if defn.activation_tier is not None and defn.max_dependency_tier is not None:
        for dep in defn.dependencies:
            dep_name = dep.component if isinstance(dep, Dependency) else dep
            if dep_name in self._components:
                dep_tier = self._components[dep_name].definition.activation_tier
                if dep_tier is not None and dep_tier > defn.max_dependency_tier:
                    if dep_name not in defn.cross_tier_dependency_allowlist:
                        errors.append(
                            f"Dependency '{dep_name}' (tier {dep_tier}) exceeds "
                            f"max_dependency_tier ({defn.max_dependency_tier})"
                        )
    if (defn.criticality == Criticality.REQUIRED and
            defn.readiness_class == ReadinessClass.DEFERRED_AFTER_READY):
        errors.append("REQUIRED criticality cannot be DEFERRED_AFTER_READY")
    if errors:
        raise ValueError(
            f"Promoted registration failed for '{defn.name}': {'; '.join(errors)}"
        )

def _warn_missing_governance(self, defn: ComponentDefinition) -> None:
    missing = []
    for field_name in ("activation_mode", "readiness_class", "resource_budget"):
        if getattr(defn, field_name) is None:
            missing.append(field_name)
    if missing:
        logger.debug(f"Legacy component {defn.name} missing governance fields: {missing}")
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/contracts/test_governance_validation.py tests/unit/core/test_governance_types.py -v`
Expected: All PASS

**Step 5: Run existing component_registry tests**

Run: `python3 -m pytest tests/unit/core/ -k "component_registry or governance" -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add backend/core/component_registry.py tests/contracts/test_governance_validation.py
git commit -m "feat(governance): add kill-switch hierarchy + promoted validation to ComponentRegistry"
```

---

### Task 13: Runtime Health Probe Registration

**Files:**
- Modify: `backend/core/component_registry.py` (add HealthProbeSet + registration)
- Test: `tests/unit/core/test_governance_types.py` (extend)

**Step 1: Write the failing test**

```python
class TestHealthProbeRegistration:
    def test_register_health_probes(self):
        from backend.core.component_registry import (
            ComponentRegistry, ComponentDefinition, Criticality, ProcessType,
            HealthProbeSet,
        )
        reg = ComponentRegistry()
        defn = ComponentDefinition(
            name="test_svc", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
        )
        reg.register(defn)
        probes = HealthProbeSet(liveness=lambda: True)
        reg.register_health_probes("test_svc", probes)
        assert "test_svc" in reg._health_probes

    def test_register_probes_for_unregistered_fails(self):
        from backend.core.component_registry import ComponentRegistry, HealthProbeSet
        reg = ComponentRegistry()
        with pytest.raises(KeyError):
            reg.register_health_probes("nonexistent", HealthProbeSet())
```

**Step 2: Run test, implement, run test, commit**

Add `HealthProbeSet` dataclass and `register_health_probes` method to `ComponentRegistry`.

```python
@dataclass
class HealthProbeSet:
    liveness: Optional[Callable] = None
    readiness: Optional[Callable] = None
    degradation: Optional[Callable] = None
```

```bash
git add backend/core/component_registry.py tests/unit/core/test_governance_types.py
git commit -m "feat(governance): add runtime health probe registration"
```

---

### Task 14: Constructor Purity AST Tests

**Files:**
- Create: `tests/contracts/test_constructor_purity.py`

**Step 1: Write the test**

```python
"""AST-based constructor purity verification for promoted services.

Scans __init__ methods of Wave 1 immune-tier services for forbidden patterns:
- Network calls (socket, requests, aiohttp, urllib)
- File I/O (open, pathlib write)
- Thread/process spawning (threading.Thread, subprocess, asyncio.create_task)
"""
import ast
import re
from pathlib import Path

# Wave 1 services (will be extracted to backend/services/immune/)
IMMUNE_SERVICES = [
    "SecurityPolicyEngine",
    "AnomalyDetector",
    "AuditTrailRecorder",
    "ThreatIntelligenceManager",
    "IncidentResponseCoordinator",
    "ComplianceAuditor",
    "DataClassificationManager",
    "AccessControlManager",
]

FORBIDDEN_INIT_PATTERNS = [
    r"socket\.",
    r"requests\.",
    r"aiohttp\.",
    r"urllib\.",
    r"open\(",
    r"Path\(.*\)\.write",
    r"threading\.Thread",
    r"subprocess\.",
    r"asyncio\.create_task",
    r"asyncio\.ensure_future",
    r"os\.system\(",
    r"os\.popen\(",
]


class TestConstructorPurity:
    def test_immune_service_inits_are_pure(self):
        """All immune-tier service __init__ methods must be side-effect free."""
        src = Path("unified_supervisor.py").read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in IMMUNE_SERVICES:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        init_src = ast.get_source_segment(src, item)
                        if init_src is None:
                            continue
                        for pattern in FORBIDDEN_INIT_PATTERNS:
                            assert not re.search(pattern, init_src), (
                                f"{node.name}.__init__ contains forbidden pattern: {pattern}\n"
                                f"Constructors for promoted services must be side-effect free.\n"
                                f"Move this to initialize() or start()."
                            )
```

**Step 2: Run test**

Run: `python3 -m pytest tests/contracts/test_constructor_purity.py -v`
Expected: PASS (current __init__ methods only set attributes and create locks)

**Step 3: Commit**

```bash
git add tests/contracts/test_constructor_purity.py
git commit -m "test(contracts): add constructor purity AST check for immune-tier services"
```

---

### Task 15: Phase 2 Exit Gate Tests

**Files:**
- Create: `tests/contracts/test_phase2_exit_gate.py`

**Step 1: Write the exit gate tests**

```python
"""Phase 2 Exit Gate: governance infrastructure must enforce all invariants."""
import os
import pytest
from unittest.mock import patch


class TestPhase2ExitGate:
    def test_inv_g1_promoted_requires_complete_contract(self):
        """INV-G1: Promoted services cannot register without complete contracts."""
        from backend.core.component_registry import (
            ComponentDefinition, Criticality, ProcessType, PromotionLevel,
            ComponentRegistry,
        )
        reg = ComponentRegistry()
        defn = ComponentDefinition(
            name="incomplete", criticality=Criticality.OPTIONAL,
            process_type=ProcessType.IN_PROCESS,
            promotion_level=PromotionLevel.PROMOTED,
        )
        with pytest.raises(ValueError):
            reg.register(defn)

    def test_inv_g2_state_domain_conflict(self):
        """INV-G2: One writer per state domain."""
        # Covered by test_governance_validation.py::test_state_domain_conflict_rejected
        pass

    def test_inv_g3_cross_tier_blocked(self):
        """INV-G3: No upward cross-tier dependencies without allowlist."""
        # Covered by test_governance_validation.py::test_cross_tier_dependency_rejected
        pass

    def test_inv_g4_kill_switch_hierarchy(self):
        """INV-G4: Kill-switch hierarchy global > tier > service."""
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        # Covered by test_governance_validation.py kill switch tests
        pass

    def test_inv_g5_constructor_purity(self):
        """INV-G5: Constructor purity for promoted services."""
        # Covered by test_constructor_purity.py
        pass

    def test_inv_g6_required_deferred_conflict(self):
        """INV-G6: REQUIRED criticality cannot be DEFERRED_AFTER_READY."""
        # Covered by test_governance_validation.py::test_criticality_readiness_conflict
        pass

    def test_governance_types_importable(self):
        """All governance types must be importable from component_registry."""
        from backend.core.component_registry import (
            PromotionLevel, ActivationMode, ReadinessClass, ActivationTier,
            RetryStrategy, OwnershipMode, ResourceBudget, FailurePolicy,
            StateDomain, ObservabilityContract, HealthPolicy, HealthProbeSet,
        )
```

**Step 2: Run all Phase 2 tests**

Run: `python3 -m pytest tests/contracts/test_phase2_exit_gate.py tests/contracts/test_governance_validation.py tests/contracts/test_constructor_purity.py tests/unit/core/test_governance_types.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add tests/contracts/test_phase2_exit_gate.py
git commit -m "test(contracts): add Phase 2 governance exit gate tests"
```

---

## Phase 3: Wave 1 — Immune System Activation

### Task 16: Extract Immune-Tier Services to Modules

**Files:**
- Create: `backend/services/__init__.py`
- Create: `backend/services/immune/__init__.py`
- Create: `backend/services/immune/security_policy_engine.py` (extract from `unified_supervisor.py:40917`)
- Create: `backend/services/immune/anomaly_detector.py` (extract from `unified_supervisor.py:42640`)
- Create: `backend/services/immune/audit_trail_recorder.py` (extract from `unified_supervisor.py:34547`)
- Create: `backend/services/immune/threat_intelligence_manager.py` (extract from `unified_supervisor.py:43256`)
- Create: `backend/services/immune/incident_response_coordinator.py` (extract from `unified_supervisor.py:42868`)
- Create: `backend/services/immune/compliance_auditor.py` (extract from `unified_supervisor.py:41364`)
- Create: `backend/services/immune/data_classification_manager.py` (extract from `unified_supervisor.py:41736`)
- Create: `backend/services/immune/access_control_manager.py` (extract from `unified_supervisor.py:42021`)

**Step 1: Extract each class**

For each class:
1. Read the class from `unified_supervisor.py` at the specified line
2. Copy to its own module file under `backend/services/immune/`
3. Add necessary imports (asyncio, logging, dataclasses, typing, etc.)
4. Extract any supporting dataclasses/enums used by that class (they may be defined nearby in unified_supervisor.py)
5. Ensure `__init__` remains side-effect free (no I/O, no network, no threads)
6. Add `initialize()` and `start()` methods if missing
7. Add `stop()` and `health()` methods if missing

**Step 2: Verify imports work**

Run for each:
```bash
python3 -c "from backend.services.immune.security_policy_engine import SecurityPolicyEngine; print('OK')"
```

**Step 3: Run constructor purity test**

Update `test_constructor_purity.py` to also scan the new module files (or update the paths).

Run: `python3 -m pytest tests/contracts/test_constructor_purity.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add backend/services/
git commit -m "refactor(services): extract 8 immune-tier services from unified_supervisor.py"
```

---

### Task 17: Register Immune-Tier Services with Promoted Contracts

**Files:**
- Create: `backend/services/immune/registry.py`
- Test: `tests/unit/services/test_immune_registration.py`

**Step 1: Write the failing test**

```python
"""Tests for immune-tier service registration with full promoted contracts."""
import pytest


class TestImmuneServiceRegistration:
    def test_all_8_services_register_successfully(self):
        """All 8 immune services must register under PROMOTED mode."""
        from backend.core.component_registry import ComponentRegistry
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        assert reg.has("security_policy_engine")
        assert reg.has("anomaly_detector")
        assert reg.has("audit_trail_recorder")
        assert reg.has("threat_intelligence_manager")
        assert reg.has("incident_response_coordinator")
        assert reg.has("compliance_auditor")
        assert reg.has("data_classification_manager")
        assert reg.has("access_control_manager")

    def test_all_are_promoted(self):
        from backend.core.component_registry import ComponentRegistry, PromotionLevel
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for name in ["security_policy_engine", "anomaly_detector", "audit_trail_recorder",
                      "threat_intelligence_manager", "incident_response_coordinator",
                      "compliance_auditor", "data_classification_manager", "access_control_manager"]:
            defn = reg.get(name)
            assert defn.promotion_level == PromotionLevel.PROMOTED, f"{name} must be PROMOTED"

    def test_all_are_immune_tier(self):
        from backend.core.component_registry import ComponentRegistry, ActivationTier
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for name in ["security_policy_engine", "anomaly_detector", "audit_trail_recorder",
                      "threat_intelligence_manager", "incident_response_coordinator",
                      "compliance_auditor", "data_classification_manager", "access_control_manager"]:
            defn = reg.get(name)
            assert defn.activation_tier == ActivationTier.IMMUNE

    def test_no_state_domain_conflicts(self):
        """All 8 services must have unique state domains."""
        from backend.core.component_registry import ComponentRegistry
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)  # Should not raise

    def test_all_constructor_pure(self):
        from backend.core.component_registry import ComponentRegistry
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for state in reg.all_states():
            assert state.definition.constructor_pure is True

    def test_tier_kill_switch_disables_all_immune(self):
        import os
        from unittest.mock import patch
        from backend.core.component_registry import ComponentRegistry, ComponentStatus
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        with patch.dict(os.environ, {"JARVIS_TIER_IMMUNE_ENABLED": "false"}):
            register_immune_services(reg)
            for state in reg.all_states():
                assert state.status == ComponentStatus.DISABLED
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/services/test_immune_registration.py -v`
Expected: FAIL — `register_immune_services` doesn't exist

**Step 3: Write minimal implementation**

Create `backend/services/immune/registry.py`:

```python
"""Registry module for immune-tier (Wave 1) services."""
from backend.core.component_registry import (
    ComponentDefinition, ComponentRegistry, Criticality, ProcessType,
    PromotionLevel, ActivationMode, ReadinessClass, ActivationTier,
    ResourceBudget, FailurePolicy, RetryStrategy, StateDomain,
    OwnershipMode, ObservabilityContract, HealthPolicy,
)

_SHARED_FAILURE_POLICY = FailurePolicy(
    retry_strategy=RetryStrategy.EXP_BACKOFF_JITTER,
    max_retries=3, backoff_base_s=1.0, backoff_max_s=30.0,
    circuit_breaker=True, breaker_threshold=5,
    breaker_recovery_s=60.0, quarantine_on_repeated=True,
)

_SHARED_OBSERVABILITY = ObservabilityContract(
    schema_version="1.0", emit_trace_id=True, emit_reason_codes=True,
    required_log_fields=("trace_id", "reason_code", "service_name",
                         "activation_mode", "readiness_class"),
    health_check_interval_s=30.0,
)

_IMMUNE_COMMON = dict(
    process_type=ProcessType.IN_PROCESS,
    promotion_level=PromotionLevel.PROMOTED,
    activation_tier=ActivationTier.IMMUNE,
    failure_policy_gov=_SHARED_FAILURE_POLICY,
    observability_contract=_SHARED_OBSERVABILITY,
    health_policy=HealthPolicy(supports_drain=True),
    constructor_pure=True,
    contract_version="1.0.0",
    tier_kill_switch_env="JARVIS_TIER_IMMUNE_ENABLED",
    max_dependency_tier=ActivationTier.FOUNDATION,
)

IMMUNE_SERVICE_DEFINITIONS = [
    ComponentDefinition(
        name="security_policy_engine",
        criticality=Criticality.REQUIRED,
        activation_mode=ActivationMode.ALWAYS_ON,
        readiness_class=ReadinessClass.BLOCK_READY,
        resource_budget=ResourceBudget(64, 10.0, 4, 30.0),
        state_domain=StateDomain("security_policy", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_SECURITY_POLICY_ENGINE_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="anomaly_detector",
        criticality=Criticality.DEGRADED_OK,
        activation_mode=ActivationMode.WARM_STANDBY,
        readiness_class=ReadinessClass.NON_BLOCKING,
        resource_budget=ResourceBudget(128, 15.0, 2, 45.0),
        state_domain=StateDomain("anomaly_detection", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_ANOMALY_DETECTOR_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="audit_trail_recorder",
        criticality=Criticality.DEGRADED_OK,
        activation_mode=ActivationMode.ALWAYS_ON,
        readiness_class=ReadinessClass.NON_BLOCKING,
        resource_budget=ResourceBudget(32, 5.0, 8, 20.0),
        state_domain=StateDomain("audit_trail", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_AUDIT_TRAIL_RECORDER_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="threat_intelligence_manager",
        criticality=Criticality.OPTIONAL,
        activation_mode=ActivationMode.EVENT_DRIVEN,
        readiness_class=ReadinessClass.DEFERRED_AFTER_READY,
        resource_budget=ResourceBudget(96, 10.0, 2, 60.0),
        state_domain=StateDomain("threat_intel", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_THREAT_INTELLIGENCE_MANAGER_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="incident_response_coordinator",
        criticality=Criticality.OPTIONAL,
        activation_mode=ActivationMode.EVENT_DRIVEN,
        readiness_class=ReadinessClass.DEFERRED_AFTER_READY,
        resource_budget=ResourceBudget(64, 8.0, 1, 30.0),
        state_domain=StateDomain("incident_response", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_INCIDENT_RESPONSE_COORDINATOR_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="compliance_auditor",
        criticality=Criticality.OPTIONAL,
        activation_mode=ActivationMode.BATCH_WINDOW,
        readiness_class=ReadinessClass.DEFERRED_AFTER_READY,
        resource_budget=ResourceBudget(48, 8.0, 1, 45.0),
        state_domain=StateDomain("compliance_state", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_COMPLIANCE_AUDITOR_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="data_classification_manager",
        criticality=Criticality.DEGRADED_OK,
        activation_mode=ActivationMode.WARM_STANDBY,
        readiness_class=ReadinessClass.NON_BLOCKING,
        resource_budget=ResourceBudget(32, 5.0, 2, 20.0),
        state_domain=StateDomain("data_classification", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_DATA_CLASSIFICATION_MANAGER_ENABLED",
        **_IMMUNE_COMMON,
    ),
    ComponentDefinition(
        name="access_control_manager",
        criticality=Criticality.REQUIRED,
        activation_mode=ActivationMode.ALWAYS_ON,
        readiness_class=ReadinessClass.BLOCK_READY,
        resource_budget=ResourceBudget(48, 8.0, 4, 30.0),
        state_domain=StateDomain("access_control", OwnershipMode.EXCLUSIVE_WRITE),
        kill_switch_env="JARVIS_SVC_ACCESS_CONTROL_MANAGER_ENABLED",
        **_IMMUNE_COMMON,
    ),
]


def register_immune_services(registry: ComponentRegistry) -> None:
    """Register all 8 immune-tier services with full promoted contracts."""
    for defn in IMMUNE_SERVICE_DEFINITIONS:
        registry.register(defn)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/services/test_immune_registration.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/services/ tests/unit/services/
git commit -m "feat(services): register 8 immune-tier services with full promoted contracts"
```

---

### Task 18: Phase 3 Exit Gate + Full Test Suite

**Files:**
- Create: `tests/contracts/test_phase3_exit_gate.py`

**Step 1: Write the exit gate tests**

```python
"""Phase 3 Exit Gate: Wave 1 immune services fully governed and operational."""
import pytest


class TestPhase3ExitGate:
    def test_all_8_services_have_unique_state_domains(self):
        from backend.core.component_registry import ComponentRegistry
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        domains = set()
        for state in reg.all_states():
            if state.definition.state_domain:
                assert state.definition.state_domain.domain not in domains, \
                    f"Duplicate state domain: {state.definition.state_domain.domain}"
                domains.add(state.definition.state_domain.domain)

    def test_no_cross_tier_violations(self):
        from backend.core.component_registry import ComponentRegistry, ActivationTier
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for state in reg.all_states():
            defn = state.definition
            if defn.max_dependency_tier is not None:
                for dep in defn.dependencies:
                    dep_name = dep.component if hasattr(dep, 'component') else dep
                    if reg.has(dep_name):
                        dep_tier = reg.get(dep_name).activation_tier
                        if dep_tier is not None:
                            assert dep_tier <= defn.max_dependency_tier or \
                                dep_name in defn.cross_tier_dependency_allowlist

    def test_immune_services_importable(self):
        """All 8 service classes must be importable from their modules."""
        from backend.services.immune.security_policy_engine import SecurityPolicyEngine
        from backend.services.immune.anomaly_detector import AnomalyDetector
        from backend.services.immune.audit_trail_recorder import AuditTrailRecorder
        from backend.services.immune.threat_intelligence_manager import ThreatIntelligenceManager
        from backend.services.immune.incident_response_coordinator import IncidentResponseCoordinator
        from backend.services.immune.compliance_auditor import ComplianceAuditor
        from backend.services.immune.data_classification_manager import DataClassificationManager
        from backend.services.immune.access_control_manager import AccessControlManager

    def test_always_on_services_have_block_or_nonblocking_readiness(self):
        from backend.core.component_registry import ComponentRegistry, ActivationMode, ReadinessClass
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for state in reg.all_states():
            defn = state.definition
            if defn.activation_mode == ActivationMode.ALWAYS_ON:
                assert defn.readiness_class in (ReadinessClass.BLOCK_READY, ReadinessClass.NON_BLOCKING), \
                    f"ALWAYS_ON service {defn.name} must be BLOCK_READY or NON_BLOCKING"

    def test_event_driven_services_are_deferred(self):
        from backend.core.component_registry import ComponentRegistry, ActivationMode, ReadinessClass
        from backend.services.immune.registry import register_immune_services
        reg = ComponentRegistry()
        register_immune_services(reg)
        for state in reg.all_states():
            defn = state.definition
            if defn.activation_mode == ActivationMode.EVENT_DRIVEN:
                assert defn.readiness_class == ReadinessClass.DEFERRED_AFTER_READY, \
                    f"EVENT_DRIVEN service {defn.name} should be DEFERRED_AFTER_READY"
```

**Step 2: Run full test suite**

Run: `python3 -m pytest tests/contracts/test_phase3_exit_gate.py tests/contracts/test_phase2_exit_gate.py tests/contracts/test_phase1_exit_gate.py tests/contracts/test_governance_validation.py tests/contracts/test_constructor_purity.py tests/unit/core/test_governance_types.py tests/unit/core/test_health_verdict.py tests/unit/core/test_apars_boot_session.py tests/unit/services/test_immune_registration.py tests/contracts/test_readiness_authority.py -v`

Expected: All PASS

**Step 3: Commit**

```bash
git add tests/contracts/test_phase3_exit_gate.py
git commit -m "test(contracts): add Phase 3 Wave 1 exit gate tests"
```

---

### Task 19: Version Bump + Gate Tag

**Step 1: Bump startup script version**

In `backend/core/gcp_vm_manager.py` line 149, change:

```python
_STARTUP_SCRIPT_VERSION = "238.0"  # was "237.0"
```

**Step 2: Update version test**

In `tests/unit/core/test_apars_boot_session.py`, update the version check:

```python
def test_startup_script_version_bumped(self):
    from backend.core.gcp_vm_manager import _STARTUP_SCRIPT_VERSION
    version = float(_STARTUP_SCRIPT_VERSION)
    assert version >= 238.0
```

**Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -k "phase1 or phase2 or phase3 or governance or health_verdict or apars or readiness or constructor_purity or immune" -v`
Expected: All PASS

**Step 4: Commit and tag**

```bash
git add backend/core/gcp_vm_manager.py tests/unit/core/test_apars_boot_session.py
git commit -m "chore: bump startup script version to 238.0 for readiness hardening + governance"
git tag gate-readiness-hardening-activation-governance
```

---

## Summary

| Phase | Tasks | Tests | Key Deliverable |
|-------|-------|-------|-----------------|
| Phase 1: Hardening | Tasks 1-9 | ~20 tests | HealthVerdict, process-epoch, contract hash, correlation ID, hysteresis, timeout profiles, stale GC, atomic FS guard |
| Phase 2: Wave 0 | Tasks 10-15 | ~25 tests | Governance enums/dataclasses, extended ComponentDefinition, promoted validation, kill-switch hierarchy, health probes, constructor purity |
| Phase 3: Wave 1 | Tasks 16-19 | ~15 tests | 8 immune services extracted, registered with full contracts, exit gates passing |
| **Total** | **19 tasks** | **~60 tests** | **Schema-enforced activation governance with 8 live services** |
