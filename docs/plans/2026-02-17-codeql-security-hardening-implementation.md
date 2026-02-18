# CodeQL Security Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Resolve the true-positive security vulnerabilities from CodeQL scanning and add CI gates to prevent regressions.

**Architecture:** Surgical fixes to 26 true-positive security issues (19 file permission, 7 log injection), plus CodeQL config tuning to suppress ~4,000+ false positives from lazy-loading `__getattr__` and cosmetic issues. Centralized sanitization utility extracted from existing code.

**Tech Stack:** Python 3, CodeQL, bandit, pre-commit, GitHub Actions

---

### Task 1: CodeQL Configuration — Exclude Deprecated Files

**Files:**
- Modify: `.github/workflows/codeql-analysis.yml:39-47`

**Step 1: Edit the CodeQL paths-ignore config**

In `.github/workflows/codeql-analysis.yml`, replace the existing `config` block (lines 39-47) with:

```yaml
          config: |
            paths-ignore:
              - venv
              - node_modules
              - build
              - dist
              - .git
              - "**/*.min.js"
              - "**/*.bundle.js"
              - "_deprecated_*"
              - "temporarily_lower_threshold.py"
              - "check_chirp_voices.py"
              - "setup_tts_voices.py"
              - "test_connector.py"
              - "test_password_typing.py"
              - "test_jarvis_websocket.html"
              - "test_jarvis_audio.html"
              - "test_actual_coordinates.py"
              - "test_pyautogui_direct.py"
              - "test_terminal_capture.py"
              - "test_multispace_vision.py"
              - "backend/tests/archive/**"
              - "backend/UNIFIED_ARCHITECTURE_EXAMPLE.py"
              - "backend/debug_*"
              - "backend/verify_*"
              - "backend/start_backend_debug.py"
              - "backend/setup_claude_api.py"
              - "backend/test_cloud_sql.py"
              - "backend/test_vision_websocket.html"
              - "backend/apply_advanced_whatsapp_fix.py"
              - "backend/enable_parallel_startup.py"
              - "backend/update_main_for_parallel.py"
              - "backend/migrate_to_rust_performance.py"
              - "frontend/public/test-audio.html"
              - "frontend/public/debug-audio.html"
```

**Step 2: Verify YAML is valid**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/codeql-analysis.yml'))" && echo "Valid YAML"`

Expected: `Valid YAML`

**Step 3: Commit**

```bash
git add .github/workflows/codeql-analysis.yml
git commit -m "security: exclude deprecated and test utility files from CodeQL scanning

Deprecated files, test HTML fixtures, and debug/setup scripts generate
false-positive security alerts. They are not production code."
```

---

### Task 2: Create Centralized Log Sanitization Utility

**Files:**
- Create: `backend/core/secure_logging.py`
- Test: `tests/unit/core/test_secure_logging.py`

**Step 1: Write the failing test**

Create `tests/unit/core/test_secure_logging.py`:

```python
"""Tests for centralized log sanitization (CWE-117 prevention)."""

import pytest
from backend.core.secure_logging import sanitize_for_log, mask_sensitive


class TestSanitizeForLog:
    """Test CWE-117 log injection prevention."""

    def test_strips_newlines(self):
        assert "\n" not in sanitize_for_log("hello\nworld")

    def test_strips_carriage_return(self):
        assert "\r" not in sanitize_for_log("hello\rworld")

    def test_strips_null_bytes(self):
        assert "\x00" not in sanitize_for_log("hello\x00world")

    def test_strips_ansi_escape(self):
        assert "\x1b" not in sanitize_for_log("hello\x1b[31mred\x1b[0m")

    def test_truncates_to_max_len(self):
        result = sanitize_for_log("a" * 500, max_len=100)
        assert len(result) == 100

    def test_default_max_len(self):
        result = sanitize_for_log("a" * 500)
        assert len(result) == 200

    def test_preserves_safe_content(self):
        assert sanitize_for_log("hello world 123") == "hello world 123"

    def test_handles_non_string(self):
        assert sanitize_for_log(42) == "42"
        assert sanitize_for_log(None) == "None"

    def test_handles_empty_string(self):
        assert sanitize_for_log("") == ""


class TestMaskSensitive:
    """Test CWE-532 sensitive data masking."""

    def test_masks_long_value(self):
        result = mask_sensitive("sk-ant-api03-xxxxxxxxxxxx")
        assert result == "sk-a****"

    def test_masks_short_value(self):
        result = mask_sensitive("abc")
        assert result == "****"

    def test_custom_prefix_length(self):
        result = mask_sensitive("abcdefgh", visible_prefix=6)
        assert result == "abcdef****"

    def test_handles_non_string(self):
        result = mask_sensitive(12345)
        assert result == "1234****"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/core/test_secure_logging.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.core.secure_logging'`

**Step 3: Write minimal implementation**

Create `backend/core/secure_logging.py`:

```python
"""
Centralized log sanitization — CWE-117 (Log Injection) and CWE-532 (Clear-Text Logging).

Consolidates existing patterns from:
- backend/main.py:_sanitize_log() — control character stripping
- backend/neural_mesh/agents/google_workspace_agent.py:_sanitize_for_logging() — PII redaction

Usage:
    from backend.core.secure_logging import sanitize_for_log, mask_sensitive

    logger.info(f"User action: {sanitize_for_log(user_input)}")
    logger.debug(f"Using key: {mask_sensitive(api_key)}")
"""

import re

_CONTROL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')


def sanitize_for_log(val, max_len: int = 200) -> str:
    """Strip control characters and limit length to prevent log injection (CWE-117).

    Removes: null bytes, newlines, carriage returns, tabs, ANSI escapes,
    and all other control characters (0x00-0x1f, 0x7f).

    Args:
        val: Value to sanitize (coerced to str).
        max_len: Maximum output length. Defaults to 200.

    Returns:
        Sanitized string safe for log output.
    """
    return _CONTROL_CHAR_RE.sub('', str(val))[:max_len]


def mask_sensitive(val, visible_prefix: int = 4) -> str:
    """Mask sensitive values, showing only first N characters (CWE-532 prevention).

    Args:
        val: Sensitive value to mask (coerced to str).
        visible_prefix: Number of leading characters to show. Defaults to 4.

    Returns:
        Masked string (e.g., "sk-a****").
    """
    s = str(val)
    if len(s) <= visible_prefix:
        return '****'
    return s[:visible_prefix] + '****'
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/core/test_secure_logging.py -v`

Expected: All 12 tests PASS

**Step 5: Commit**

```bash
git add backend/core/secure_logging.py tests/unit/core/test_secure_logging.py
git commit -m "feat: add centralized log sanitization utility (CWE-117, CWE-532)

Extracts and consolidates _sanitize_log() from main.py into a shared
module. Provides sanitize_for_log() for control char stripping and
mask_sensitive() for credential masking."
```

---

### Task 3: Fix Critical File Permissions — Supervisor Sockets (0o666 → 0o600)

**Files:**
- Modify: `backend/core/supervisor_singleton.py` (lines ~1212, ~2821, ~2896)

**Step 1: Fix unix socket permissions**

In `backend/core/supervisor_singleton.py`, change all three occurrences of `0o666` to `0o600`:

Line ~1212:
```python
# BEFORE:
os.chmod(str(self._socket_path), 0o666)
# AFTER:
os.chmod(str(self._socket_path), 0o600)
```

Line ~2821:
```python
# BEFORE:
os.chmod(str(SUPERVISOR_IPC_SOCKET), 0o666)
# AFTER:
os.chmod(str(SUPERVISOR_IPC_SOCKET), 0o600)
```

Line ~2896:
```python
# BEFORE:
os.chmod(str(SUPERVISOR_IPC_SOCKET), 0o666)
# AFTER:
os.chmod(str(SUPERVISOR_IPC_SOCKET), 0o600)
```

**Step 2: Verify no runtime breakage**

Run: `python3 -c "from backend.core.supervisor_singleton import SupervisorSingleton; print('Import OK')"`

Expected: `Import OK` (no syntax errors)

**Step 3: Commit**

```bash
git add backend/core/supervisor_singleton.py
git commit -m "security: restrict IPC socket permissions from 0o666 to 0o600 (CWE-732)

Unix sockets were world-readable and world-writable. Restrict to
owner-only to prevent unauthorized IPC access."
```

---

### Task 4: Fix High-Priority File Permissions — Lock Files and Epoch Fencing

**Files:**
- Modify: `backend/core/epoch_fencing.py` (line ~427)
- Modify: `backend/core/robust_file_lock.py` (line ~252)
- Modify: `backend/core/trinity_integrator.py` (line ~3830)
- Modify: `backend/core/coding_council/advanced/atomic_command_queue.py` (line ~521)

**Step 1: Fix all lock file permissions**

`backend/core/epoch_fencing.py` line ~427:
```python
# BEFORE:
fd = os.open(str(self._epoch_file), os.O_RDWR | os.O_CREAT, 0o644)
# AFTER:
fd = os.open(str(self._epoch_file), os.O_RDWR | os.O_CREAT, 0o600)
```

`backend/core/robust_file_lock.py` line ~252:
```python
# BEFORE:
return os.open(str(self._lock_file), os.O_RDWR | os.O_CREAT, 0o644)
# AFTER:
return os.open(str(self._lock_file), os.O_RDWR | os.O_CREAT, 0o600)
```

`backend/core/trinity_integrator.py` line ~3830:
```python
# BEFORE:
fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
# AFTER:
fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
```

`backend/core/coding_council/advanced/atomic_command_queue.py` line ~521:
```python
# BEFORE:
fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o666)
# AFTER:
fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
```

**Step 2: Verify imports**

Run: `python3 -c "from backend.core.epoch_fencing import *; from backend.core.robust_file_lock import *; print('OK')"`

Expected: `OK`

**Step 3: Commit**

```bash
git add backend/core/epoch_fencing.py backend/core/robust_file_lock.py backend/core/trinity_integrator.py backend/core/coding_council/advanced/atomic_command_queue.py
git commit -m "security: restrict lock file permissions to 0o600 (CWE-732)

Lock files for epoch fencing, distributed locks, trinity IPC, and
atomic command queue were world-readable (0o644) or world-writable
(0o666). Restrict to owner-only."
```

---

### Task 5: Fix Medium-Priority File Permissions — Executable Scripts

**Files:**
- Modify: `unified_supervisor.py` (line ~160)
- Modify: `start_system.py` (line ~14790)
- Modify: `backend/core/safe_fd.py` (line ~582)
- Modify: `backend/vision/rust_self_healer.py` (lines ~660, ~1023)
- Modify: `backend/macos_helper/launchd/service_manager.py` (line ~281)
- Modify: `backend/core/coding_council/advanced/atomic_locking.py` (lines ~257, ~297)
- Modify: `backend/core/coding_council/async_tools/file_locker.py` (line ~453)

**Step 1: Fix all 0o755 → 0o700 and remaining 0o644 → 0o600**

For each file, change `0o755` to `0o700` (owner-execute only) and `0o644` to `0o600`:

`unified_supervisor.py` line ~160:
```python
# BEFORE:
_early_os.chmod(_script_path, 0o755)
# AFTER:
_early_os.chmod(_script_path, 0o700)
```

`start_system.py` line ~14790:
```python
# BEFORE:
os.chmod(script, 0o755)
# AFTER:
os.chmod(script, 0o700)
```

`backend/vision/rust_self_healer.py` line ~660:
```python
# BEFORE:
os.chmod(installer_path, 0o755)
# AFTER:
os.chmod(installer_path, 0o700)
```

`backend/vision/rust_self_healer.py` line ~1023:
```python
# BEFORE:
os.chmod(file_path, 0o755)
# AFTER:
os.chmod(file_path, 0o700)
```

`backend/macos_helper/launchd/service_manager.py` line ~281:
```python
# BEFORE:
os.chmod(self.plist_path, 0o644)
# AFTER:
os.chmod(self.plist_path, 0o600)
```

Also fix any remaining permission issues in:
- `backend/core/safe_fd.py` (~582)
- `backend/core/coding_council/advanced/atomic_locking.py` (~257, ~297)
- `backend/core/coding_council/async_tools/file_locker.py` (~453)

Each follows the same pattern: `0o644` → `0o600`, `0o755` → `0o700`.

**Step 2: Commit**

```bash
git add unified_supervisor.py start_system.py backend/vision/rust_self_healer.py backend/macos_helper/launchd/service_manager.py backend/core/safe_fd.py backend/core/coding_council/advanced/atomic_locking.py backend/core/coding_council/async_tools/file_locker.py
git commit -m "security: restrict script and config file permissions (CWE-732)

Executable scripts changed from 0o755 (world-executable) to 0o700
(owner-only). Config/data files changed from 0o644 (world-readable)
to 0o600 (owner-only)."
```

---

### Task 6: Fix Log Injection in API Routes

**Files:**
- Modify: `backend/api/agentic_api.py` (lines ~330, ~418)
- Modify: `backend/api/ml_audio_api.py` (lines ~1324, ~1448)
- Modify: `backend/api/broadcast_router.py` (line ~164)
- Modify: `backend/api/network_recovery_api.py` (line ~56)
- Modify: `backend/api/audio_error_fallback.py` (line ~30)

**Step 1: Add import to each file**

At the top of each file, add:
```python
from backend.core.secure_logging import sanitize_for_log
```

**Step 2: Fix each log statement**

`backend/api/agentic_api.py` line ~330:
```python
# BEFORE:
logger.info(f"[AgenticAPI] Execute goal: {body.goal[:50]}...")
# AFTER:
logger.info(f"[AgenticAPI] Execute goal: {sanitize_for_log(body.goal, 50)}...")
```

`backend/api/agentic_api.py` line ~418:
```python
# BEFORE:
logger.info(f"[AgenticAPI] Route command: {body.command[:50]}...")
# AFTER:
logger.info(f"[AgenticAPI] Route command: {sanitize_for_log(body.command, 50)}...")
```

`backend/api/ml_audio_api.py` line ~1324:
```python
# BEFORE:
logger.info(f"ML Audio prediction from {client_id} - format: {data.format}, size: {len(data.audio_data) if data.audio_data else 0}")
# AFTER:
logger.info(f"ML Audio prediction from {client_id} - format: {sanitize_for_log(data.format, 32)}, size: {len(data.audio_data) if data.audio_data else 0}")
```

`backend/api/ml_audio_api.py` line ~1448:
```python
# BEFORE:
logger.info(f"Audio telemetry: {request.event} - {request.data}")
# AFTER:
logger.info(f"Audio telemetry: {sanitize_for_log(request.event, 64)} - {sanitize_for_log(str(request.data), 100)}")
```

`backend/api/broadcast_router.py` line ~164:
```python
# BEFORE:
logger.info(f"📡 Broadcast to {success_count}/{len(connections)} clients: {message.get('type', 'unknown')}")
# AFTER:
logger.info(f"📡 Broadcast to {success_count}/{len(connections)} clients: {sanitize_for_log(message.get('type', 'unknown'), 64)}")
```

`backend/api/network_recovery_api.py` line ~56:
```python
# BEFORE:
logger.info(f"Network diagnosis requested for error: {request.error}")
# AFTER:
logger.info(f"Network diagnosis requested for error: {sanitize_for_log(request.error, 100)}")
```

`backend/api/audio_error_fallback.py` line ~30:
```python
# BEFORE:
logger.warning(f"Audio error reported: {error.error_type} - {error.message}")
# AFTER:
logger.warning(f"Audio error reported: {sanitize_for_log(error.error_type, 64)} - {sanitize_for_log(error.message, 100)}")
```

**Step 3: Verify imports**

Run: `python3 -c "from backend.api.agentic_api import *; from backend.api.ml_audio_api import *; print('OK')"`

Expected: `OK`

**Step 4: Commit**

```bash
git add backend/api/agentic_api.py backend/api/ml_audio_api.py backend/api/broadcast_router.py backend/api/network_recovery_api.py backend/api/audio_error_fallback.py
git commit -m "security: sanitize user-controlled input in API route logs (CWE-117)

Apply sanitize_for_log() to all log statements that include user-
controlled request data (goals, commands, formats, error messages).
Prevents log injection via newlines, ANSI escapes, and control chars."
```

---

### Task 7: Add Pre-commit Security Hooks

**Files:**
- Create: `.pre-commit-config.yaml`

**Step 1: Create pre-commit config**

Create `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/PyCQA/bandit
    rev: '1.8.3'
    hooks:
      - id: bandit
        args: ['--severity-level', 'medium', '--confidence-level', 'medium', '-q']
        exclude: ^(_deprecated_|tests/|backend/tests/|temporarily_|check_chirp|setup_tts|test_connector|test_password)
        types: [python]
```

**Step 2: Verify config syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.pre-commit-config.yaml')); print('Valid')"`

Expected: `Valid`

**Step 3: Commit**

```bash
git add .pre-commit-config.yaml
git commit -m "ci: add pre-commit hook with bandit for Python security linting

Catches security issues (CWE-117 log injection, CWE-732 file perms,
CWE-327 weak crypto) before commit. Excludes deprecated and test files."
```

---

### Task 8: Fix Loading Server Log Injection (High-Volume Source)

**Files:**
- Modify: `loading_server.py` (multiple log injection sites)

**Step 1: Identify the pattern in loading_server.py**

`loading_server.py` has ~15 log injection alerts. Read the flagged lines and determine which log user-controlled values from HTTP requests vs internal state. Apply `sanitize_for_log()` to each user-controlled value.

Add import at top:
```python
from backend.core.secure_logging import sanitize_for_log
```

Fix each flagged line following the same pattern as Task 6.

**Step 2: Commit**

```bash
git add loading_server.py
git commit -m "security: sanitize user input in loading server logs (CWE-117)"
```

---

### Task 9: Fix Voice Orchestrator Log Injection

**Files:**
- Modify: `backend/core/supervisor/unified_voice_orchestrator.py` (lines ~567-641)

**Step 1: Sanitize voice text in logs**

Add import:
```python
from backend.core.secure_logging import sanitize_for_log
```

Fix each instance where `text` (user voice input) is logged:

```python
# BEFORE:
logger.debug(f"🔇 Voice disabled, skipping: {text[:50]}...")
# AFTER:
logger.debug(f"🔇 Voice disabled, skipping: {sanitize_for_log(text, 50)}...")
```

Apply the same pattern to all ~6 instances in lines 567-641.

**Step 2: Commit**

```bash
git add backend/core/supervisor/unified_voice_orchestrator.py
git commit -m "security: sanitize voice text in orchestrator logs (CWE-117)

Voice commands logged with truncation but no control char sanitization.
Prevents log injection via spoken commands with embedded control sequences."
```

---

### Task 10: Fix Remaining Log Injection Sites

**Files:**
- Modify: `backend/main.py` (remaining log injection lines not using `_sanitize_log`)
- Modify: `backend/core/coding_council/integration.py` (lines ~2438, ~3156)
- Modify: `backend/memory/experience_recorder.py` (lines ~326, ~342)

**Step 1: For each file, add import and fix**

Add `from backend.core.secure_logging import sanitize_for_log` and apply to user-controlled log values.

**Step 2: Commit**

```bash
git add backend/main.py backend/core/coding_council/integration.py backend/memory/experience_recorder.py
git commit -m "security: sanitize remaining log injection sites (CWE-117)"
```

---

### Task 11: Verify Alert Reduction

**Step 1: Push changes and wait for CodeQL scan**

After all commits are pushed, the next CodeQL scan (or manually triggered) should show significant alert reduction.

**Step 2: Check remaining alerts**

Run: `gh api "repos/drussell23/JARVIS/code-scanning/alerts?state=open&per_page=1" -i 2>/dev/null | grep -i link`

Expected: Significant reduction from 5,117. The remaining alerts should be primarily:
- `py/unused-import` (~3,500) — cosmetic, separate effort
- `py/undefined-export` (~311) — false positives from `__getattr__` lazy loading
- `py/multiple-definition` (~100) — Phase 2 correctness work
- Other code quality issues — Phase 2

---

## Phase 2: Code Correctness (Separate Plan)

After Phase 1 security hardening is complete and verified, create a follow-up plan for:
1. `py/uninitialized-local-variable` (23 alerts)
2. `py/call/wrong-named-class-argument` (31 alerts)
3. `py/unsafe-cyclic-import` (12 alerts)
4. `py/illegal-raise` (9 alerts)
5. `py/multiple-definition` (101 alerts)
6. `py/unreachable-statement` (46 alerts)
