import re

import pytest

from backend.core.gcp_vm_manager import GCPVMManager


def _get_golden_startup_script() -> str:
    # Bypass __init__ to avoid GCP client setup in tests.
    manager = GCPVMManager.__new__(GCPVMManager)
    return manager._generate_golden_startup_script()


def test_startup_script_exports_apars_file():
    script = _get_golden_startup_script()
    assert "JARVIS_APARS_FILE" in script, "startup script should export APARS file path"


def test_startup_script_has_safe_env_loader():
    script = _get_golden_startup_script()
    assert "SAFE_ENV_LOAD" in script, "startup script should include safe .env loader"


def test_startup_script_exposes_script_version():
    script = _get_golden_startup_script()
    assert re.search(r"startup_script_version", script), "APARS should expose startup script version"


@pytest.mark.asyncio
async def test_poll_health_detects_script_version_mismatch():
    manager = GCPVMManager.__new__(GCPVMManager)

    async def _fake_ping(ip, port, timeout=10.0):
        return False, {
            "apars": {
                "phase_name": "starting",
                "total_progress": 12,
                "startup_script_version": "200.0",
                "startup_script_metadata_version": "235.1",
            }
        }

    manager._ping_health_endpoint = _fake_ping

    success, status = await manager._poll_health_until_ready(
        "1.2.3.4",
        8000,
        timeout=0.05,
        poll_interval=0.01,
    )

    assert success is False
    assert "SCRIPT_VERSION_MISMATCH" in status
