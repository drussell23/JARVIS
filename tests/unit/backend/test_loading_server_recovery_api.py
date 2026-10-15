from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_LOADING_SERVER_PATH = (
    Path(__file__).resolve().parents[3] / "backend" / "loading_server.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "backend_loading_server_main",
    _LOADING_SERVER_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
loading_server_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = loading_server_module
_SPEC.loader.exec_module(loading_server_module)


def _extract_json_payload(http_response: str) -> dict:
    body = http_response.split("\r\n\r\n", 1)[1]
    return json.loads(body)


@pytest.fixture
def loading_server(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(loading_server_module, "ADVANCED_FEATURES_AVAILABLE", False)
    config = loading_server_module.LoadingServerConfig(
        jarvis_home=tmp_path / ".jarvis",
        jarvis_repo=tmp_path,
        prime_repo=tmp_path / "jarvis-prime",
        reactor_repo=tmp_path / "reactor-core",
    )
    (config.jarvis_home / "locks").mkdir(parents=True, exist_ok=True)
    return loading_server_module.LoadingServer(config=config)


@pytest.mark.asyncio
async def test_supervisor_recover_route_returns_recovery_payload(loading_server, monkeypatch):
    async def _fake_request(reason: str, action: str, force: bool):
        assert reason == "unit_test"
        assert action == "restart"
        assert force is True
        return {
            "accepted": True,
            "status": "restart_initiated",
            "attempt_id": "attempt-1",
        }

    monkeypatch.setattr(loading_server, "_request_supervisor_recovery", _fake_request)

    response = await loading_server._route_request(
        "POST",
        "/api/supervisor/recover",
        headers={},
        body=json.dumps(
            {"reason": "unit_test", "action": "restart", "force": True}
        ).encode("utf-8"),
    )
    payload = _extract_json_payload(response)
    assert payload["status"] == "restart_initiated"
    assert payload["accepted"] is True


@pytest.mark.asyncio
async def test_supervisor_recovery_respects_cooldown(loading_server, monkeypatch):
    loading_server._recovery_cooldown_seconds = 60.0
    loading_server._recovery_last_attempt_at = time.monotonic()
    loading_server._recovery_last_attempt_id = "cooldown-attempt"

    async def _unhealthy():
        return False

    monkeypatch.setattr(loading_server, "_check_backend_health_for_recovery", _unhealthy)
    monkeypatch.setattr(loading_server, "_check_kernel_health_for_recovery", _unhealthy)

    result = await loading_server._request_supervisor_recovery(
        reason="unit_test",
        action="restart",
        force=False,
    )

    assert result["accepted"] is False
    assert result["status"] == "cooldown"
    assert result["attempt_id"] == "cooldown-attempt"
    assert result["retry_after_seconds"] > 0


@pytest.mark.asyncio
async def test_supervisor_recovery_spawns_unified_supervisor_restart(
    loading_server, monkeypatch, tmp_path: Path
):
    loading_server._recovery_project_root = tmp_path
    loading_server._recovery_supervisor_script = tmp_path / "unified_supervisor.py"
    loading_server._recovery_supervisor_script.write_text(
        "print('stub supervisor')\n", encoding="utf-8"
    )
    loading_server._recovery_python_executable = "/usr/bin/python3"
    monkeypatch.setattr(loading_server, "_get_recovery_log_path", lambda: tmp_path / "recovery.log")

    async def _unhealthy():
        return False

    monkeypatch.setattr(loading_server, "_check_backend_health_for_recovery", _unhealthy)
    monkeypatch.setattr(loading_server, "_check_kernel_health_for_recovery", _unhealthy)

    captured = {}

    class _PopenStub:
        def __init__(self, cmd, cwd, stdout, stderr, start_new_session, env):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["start_new_session"] = start_new_session
            captured["env"] = env
            self.pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(loading_server_module.subprocess, "Popen", _PopenStub)

    result = await loading_server._request_supervisor_recovery(
        reason="unit_test",
        action="restart",
        force=False,
    )

    assert result["accepted"] is True
    assert result["status"] == "restart_initiated"
    assert result["recovery_pid"] == 4242
    assert captured["cmd"][0] == "/usr/bin/python3"
    assert str(loading_server._recovery_supervisor_script) in captured["cmd"]
    assert "--restart" in captured["cmd"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["start_new_session"] is True


@pytest.mark.asyncio
async def test_supervisor_recovery_status_route_is_available(loading_server, monkeypatch):
    async def _unhealthy():
        return False

    monkeypatch.setattr(loading_server, "_check_backend_health_for_recovery", _unhealthy)
    monkeypatch.setattr(loading_server, "_check_kernel_health_for_recovery", _unhealthy)

    response = await loading_server._route_request(
        "GET",
        "/api/supervisor/recover/status",
        headers={},
        body=None,
    )
    payload = _extract_json_payload(response)

    assert payload["controller"] == "supervisor_recovery"
    assert "recovery_process_running" in payload


@pytest.mark.asyncio
async def test_supervisor_reports_reach_the_page_whole_not_as_components(loading_server):
    """The checklist and the boot's phases are reports relayed as they were
    sent; folded into the components map they never reached the page."""
    timeline = {"phases": [{"name": "preflight", "title": "Preflight", "state": "running"}],
                "percent": 30, "learning": False, "complete": False, "elapsed": 12.0}
    checklist = {"verdict": "ready", "items": []}
    body = json.dumps({"stage": "preflight", "message": "m", "progress": 30,
                       "metadata": {"icon": "spinner", "timeline": timeline, "checklist": checklist}})
    await loading_server._handle_progress_update(body.encode())
    assert "timeline" not in loading_server._components
    assert "checklist" not in loading_server._components
    assert loading_server._reports == {"timeline": timeline, "checklist": checklist}
    # A later message without a report keeps the last one.
    await loading_server._handle_progress_update(json.dumps(
        {"stage": "preflight", "message": "m", "progress": 31, "metadata": {"icon": "spinner"}}).encode())
    assert loading_server._reports["timeline"] == timeline
