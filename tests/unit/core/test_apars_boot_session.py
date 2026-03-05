"""Tests for APARS boot session UUID binding."""
import json
import re
import textwrap
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
        # Extract the update_apars function heredoc block
        match = re.search(
            r'cat > "\$PROGRESS_FILE" << EOFPROGRESS\n(.*?)\nEOFPROGRESS',
            script,
            re.DOTALL,
        )
        assert match, "Could not find PROGRESS_FILE heredoc in startup script"
        progress_json_template = match.group(1)
        assert '"boot_session_id"' in progress_json_template
        assert "${BOOT_SESSION_ID}" in progress_json_template
