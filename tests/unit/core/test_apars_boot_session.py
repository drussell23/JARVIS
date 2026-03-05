"""Tests for APARS boot session UUID binding and health check configuration."""
import ast
import json
import re
import textwrap
from pathlib import Path
from unittest.mock import patch


def _get_golden_startup_script():
    """Helper to generate the golden startup script from a bare GCPVMManager."""
    from backend.core.gcp_vm_manager import VMManagerConfig, GCPVMManager
    mgr = GCPVMManager.__new__(GCPVMManager)
    mgr.config = VMManagerConfig()
    return mgr._generate_golden_startup_script()


def _extract_embedded_build_apars_payload(script: str):
    """Extract the _build_apars_payload function from the embedded APARS launcher Python code."""
    # The function is embedded inside a heredoc that writes a Python launcher script.
    # Extract the Python code between 'EOFLAUNCHER' markers.
    match = re.search(
        r"cat > \"\$APARS_LAUNCHER\" << 'EOFLAUNCHER'\n(.*?)\nEOFLAUNCHER",
        script,
        re.DOTALL,
    )
    assert match, "Could not find APARS launcher heredoc in startup script"
    launcher_code = match.group(1)

    # Execute the launcher code in an isolated namespace to extract the function
    namespace = {"__builtins__": __builtins__}
    # We need time module available
    import time
    namespace["time"] = time
    # Only exec the imports and the function definition we need
    # Extract just _build_apars_payload and its imports
    exec(
        "import os, sys, json, time\n"
        + _extract_function(launcher_code, "_build_apars_payload"),
        namespace,
    )
    return namespace["_build_apars_payload"]


def _extract_function(code: str, func_name: str) -> str:
    """Extract a function definition from source code by name."""
    lines = code.split("\n")
    func_lines = []
    capturing = False
    indent = None
    for line in lines:
        if line.strip().startswith(f"def {func_name}("):
            capturing = True
            indent = len(line) - len(line.lstrip())
            func_lines.append(line)
        elif capturing:
            if line.strip() == "" or line.strip().startswith("#"):
                func_lines.append(line)
            elif len(line) - len(line.lstrip()) > indent:
                func_lines.append(line)
            else:
                break
    return "\n".join(func_lines)


class TestAPARSBootSession:
    def test_build_apars_payload_includes_boot_session_id(self):
        """APARS payload must include boot_session_id from progress file."""
        script = _get_golden_startup_script()
        _build_apars_payload = _extract_embedded_build_apars_payload(script)

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
        script = _get_golden_startup_script()
        _build_apars_payload = _extract_embedded_build_apars_payload(script)

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
        script = _get_golden_startup_script()
        assert "BOOT_SESSION_ID=" in script
        assert "boot_session_id" in script
        # Must use uuidgen or python uuid
        assert "uuidgen" in script or "uuid" in script

    def test_update_apars_bash_function_includes_boot_session_id(self):
        """The update_apars bash function must write boot_session_id to the progress JSON."""
        script = _get_golden_startup_script()
        # Extract the update_apars heredoc block (writes to tmp_file now)
        match = re.search(
            r'cat > "\$tmp_file" << EOFPROGRESS\n(.*?)\nEOFPROGRESS',
            script,
            re.DOTALL,
        )
        assert match, "Could not find EOFPROGRESS heredoc in startup script"
        progress_json_template = match.group(1)
        assert '"boot_session_id"' in progress_json_template
        assert "${BOOT_SESSION_ID}" in progress_json_template


class TestAPARSSessionValidation:
    """Tests for the module-level _is_apars_current_session helper."""

    def test_is_apars_current_session_matching(self):
        """Matching session IDs should return True."""
        from backend.core.gcp_vm_manager import _is_apars_current_session
        assert _is_apars_current_session("session-A", expected="session-A") is True

    def test_is_apars_current_session_mismatched(self):
        """Different session IDs should return False (stale data)."""
        from backend.core.gcp_vm_manager import _is_apars_current_session
        assert _is_apars_current_session("session-B", expected="session-A") is False

    def test_is_apars_current_session_unknown_accepted(self):
        """'unknown' session ID should be accepted (backward compat)."""
        from backend.core.gcp_vm_manager import _is_apars_current_session
        assert _is_apars_current_session("unknown", expected="session-A") is True

    def test_is_apars_current_session_empty_accepted(self):
        """Empty session ID should be accepted (backward compat)."""
        from backend.core.gcp_vm_manager import _is_apars_current_session
        assert _is_apars_current_session("", expected="session-A") is True

    def test_is_apars_current_session_none_accepted(self):
        """None session ID should be accepted (backward compat)."""
        from backend.core.gcp_vm_manager import _is_apars_current_session
        assert _is_apars_current_session(None, expected="session-A") is True


class TestConfigurableHealthTimeout:
    def test_startup_script_uses_configurable_health_timeout(self):
        """Startup script must use GCP_SERVICE_HEALTH_TIMEOUT, not hardcoded 30s."""
        script = _get_golden_startup_script()
        assert "GCP_SERVICE_HEALTH_TIMEOUT" in script
        assert "seq 1 15" not in script

    def test_startup_script_health_timeout_default_90s(self):
        """Default health check timeout should be 90s."""
        script = _get_golden_startup_script()
        assert "GCP_SERVICE_HEALTH_TIMEOUT:-90" in script

    def test_startup_script_no_progress_threshold_readiness(self):
        """Startup script must NOT use total_progress >= 95 as readiness signal."""
        script = _get_golden_startup_script()
        # Extract the health check Python inline code
        inline_py = re.findall(r'python3 -c "(.*?)"', script, re.DOTALL)
        for code in inline_py:
            assert "total_progress" not in code, \
                "Inline Python health check must not use total_progress for readiness"

    def test_ping_health_no_progress_threshold_readiness(self):
        """_ping_health_endpoint must NOT accept total_progress as readiness."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "total_progress" not in func_src, \
                    "_ping_health_endpoint must not use total_progress for readiness"
                break

    def test_startup_script_timeout_uses_null_readiness(self):
        """On timeout, startup script must write null readiness, not false."""
        script = _get_golden_startup_script()
        timeout_calls = re.findall(r'update_apars.*service_start_timeout.*', script)
        assert len(timeout_calls) >= 1
        for call in timeout_calls:
            assert "null" in call, f"Timeout must use null readiness: {call}"
            # Should NOT have 'false' after the checkpoint name
            parts_after_checkpoint = call.split('"service_start_timeout"')[1] if '"service_start_timeout"' in call else call.split("service_start_timeout")[1]
            # The two args after checkpoint should be null null, not true false
            assert "false" not in parts_after_checkpoint.split('"')[0], \
                f"Must not assert ready=false on timeout: {call}"


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

    def test_build_apars_payload_includes_process_epoch(self):
        """APARS payload must include process_epoch from progress file."""
        script = _get_golden_startup_script()
        _build_apars_payload = _extract_embedded_build_apars_payload(script)
        state = {
            "phase_number": 6,
            "total_progress": 95,
            "checkpoint": "verifying_service",
            "boot_session_id": "abc-123-def",
            "process_epoch": "a1b2c3d4e5f6",
            "updated_at": 1000,
        }
        payload = _build_apars_payload(state)
        assert payload is not None
        assert payload.get("process_epoch") == "a1b2c3d4e5f6"


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


class TestAtomicWritesAndVersion:
    def test_startup_script_uses_atomic_apars_write(self):
        """APARS progress file must be written atomically (write temp + mv)."""
        script = _get_golden_startup_script()
        # The update_apars function must write to temp then mv
        assert ".tmp." in script, "update_apars must write to temp file"
        assert 'mv "$tmp_file" "$PROGRESS_FILE"' in script, \
            "update_apars must atomically rename temp to PROGRESS_FILE"
        # Must NOT write directly to PROGRESS_FILE
        assert 'cat > "$PROGRESS_FILE"' not in script, \
            "Must not write directly to PROGRESS_FILE (use atomic temp+mv)"

    def test_startup_script_version_bumped(self):
        """Startup script version must be >= 237.0 after readiness fixes."""
        from backend.core.gcp_vm_manager import _STARTUP_SCRIPT_VERSION
        version = float(_STARTUP_SCRIPT_VERSION)
        assert version >= 237.0, \
            f"Startup script version must be >= 237.0, got {version}"


class TestAtomicWriteBoundary:
    def test_startup_script_checks_filesystem_boundary(self):
        """Startup script must verify temp and target are on same mount."""
        script = _get_golden_startup_script()
        # Must check filesystem/mount for atomicity
        assert "df " in script or "mount" in script or "same_fs" in script or "same_mount" in script
