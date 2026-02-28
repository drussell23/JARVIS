from __future__ import annotations

import pytest
from aiohttp import web

from backend.core.cross_repo_contract_enforcer import (
    ContractCheckResult,
    ContractDriftMonitor,
    ContractTarget,
    CrossRepoContractEnforcer,
)


async def _start_test_server(app: web.Application):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets  # type: ignore[attr-defined]
    port = sockets[0].getsockname()[1] if sockets else 0
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_contract_enforcer_accepts_native_handshake():
    async def health(_request):
        return web.json_response(
            {
                "status": "healthy",
                "ready_for_inference": True,
                "protocol_version": "1.1.0",
                "capabilities": ["inference"],
            }
        )

    async def handshake(_request):
        return web.json_response(
            {
                "accepted": True,
                "component_instance_id": "prime-1",
                "api_version": "1.1.0",
                "capabilities": ["inference"],
                "health_schema_hash": "abcd1234",
            }
        )

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/lifecycle/handshake", handshake)
    runner, endpoint = await _start_test_server(app)

    try:
        enforcer = CrossRepoContractEnforcer(
            supervisor_instance_id="kernel-test",
            local_protocol_version="1.2.0",
            request_timeout_s=3.0,
        )
        target = ContractTarget(
            name="jarvis_prime",
            endpoint=endpoint,
            health_schema_key="prime:/health",
            min_api_version="1.0.0",
            max_api_version="1.9.9",
            required_capabilities=("inference",),
            require_handshake=True,
            allow_legacy_handshake=False,
        )
        result = (await enforcer.check_many([target]))["jarvis_prime"]
        assert result.ok
        assert result.handshake_mode == "native"
        assert result.api_version == "1.1.0"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_contract_enforcer_blocks_missing_handshake_when_legacy_disallowed():
    async def health(_request):
        return web.json_response(
            {
                "status": "healthy",
                "ready_for_inference": True,
                "protocol_version": "1.1.0",
                "capabilities": ["inference"],
            }
        )

    app = web.Application()
    app.router.add_get("/health", health)
    runner, endpoint = await _start_test_server(app)

    try:
        enforcer = CrossRepoContractEnforcer(
            supervisor_instance_id="kernel-test",
            local_protocol_version="1.2.0",
            request_timeout_s=3.0,
        )
        target = ContractTarget(
            name="jarvis_prime",
            endpoint=endpoint,
            health_schema_key="prime:/health",
            min_api_version="1.0.0",
            max_api_version="1.9.9",
            required_capabilities=("inference",),
            require_handshake=True,
            allow_legacy_handshake=False,
        )
        result = (await enforcer.check_many([target]))["jarvis_prime"]
        assert not result.ok
        assert "handshake_endpoint_missing" in result.reason
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_contract_enforcer_allows_legacy_when_enabled():
    async def health(_request):
        return web.json_response(
            {
                "status": "healthy",
                "ready_for_inference": True,
                "protocol_version": "1.1.0",
                "capabilities": ["inference"],
            }
        )

    app = web.Application()
    app.router.add_get("/health", health)
    runner, endpoint = await _start_test_server(app)

    try:
        enforcer = CrossRepoContractEnforcer(
            supervisor_instance_id="kernel-test",
            local_protocol_version="1.2.0",
            request_timeout_s=3.0,
        )
        target = ContractTarget(
            name="jarvis_prime",
            endpoint=endpoint,
            health_schema_key="prime:/health",
            min_api_version="1.0.0",
            max_api_version="1.9.9",
            required_capabilities=("inference",),
            require_handshake=True,
            allow_legacy_handshake=True,
        )
        result = (await enforcer.check_many([target]))["jarvis_prime"]
        assert result.ok
        assert result.handshake_mode == "legacy"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_contract_enforcer_blocks_version_outside_window():
    async def health(_request):
        return web.json_response(
            {
                "status": "healthy",
                "ready_for_inference": True,
                "protocol_version": "2.0.0",
                "capabilities": ["inference"],
            }
        )

    async def handshake(_request):
        return web.json_response(
            {
                "accepted": True,
                "component_instance_id": "prime-1",
                "api_version": "2.0.0",
                "capabilities": ["inference"],
                "health_schema_hash": "abcd1234",
            }
        )

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/lifecycle/handshake", handshake)
    runner, endpoint = await _start_test_server(app)

    try:
        enforcer = CrossRepoContractEnforcer(
            supervisor_instance_id="kernel-test",
            local_protocol_version="1.2.0",
            request_timeout_s=3.0,
        )
        target = ContractTarget(
            name="jarvis_prime",
            endpoint=endpoint,
            health_schema_key="prime:/health",
            min_api_version="1.0.0",
            max_api_version="1.9.9",
            required_capabilities=("inference",),
            require_handshake=True,
            allow_legacy_handshake=False,
        )
        result = (await enforcer.check_many([target]))["jarvis_prime"]
        assert not result.ok
        assert "outside [1.0.0, 1.9.9]" in result.reason
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_contract_enforcer_accepts_component_version_without_local_major_match():
    async def health(_request):
        return web.json_response(
            {
                "status": "healthy",
                "ready_for_inference": True,
                "protocol_version": "152",
                "capabilities": ["training"],
            }
        )

    async def handshake(_request):
        return web.json_response(
            {
                "accepted": True,
                "component_instance_id": "reactor-1",
                "api_version": "152",
                "capabilities": ["training"],
                "health_schema_hash": "abcd1234",
            }
        )

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/lifecycle/handshake", handshake)
    runner, endpoint = await _start_test_server(app)

    try:
        enforcer = CrossRepoContractEnforcer(
            supervisor_instance_id="kernel-test",
            local_protocol_version="0.0.0",
            request_timeout_s=3.0,
        )
        target = ContractTarget(
            name="reactor_core",
            endpoint=endpoint,
            health_schema_key="/health",
            min_api_version="0.0.0",
            max_api_version="9999.9999.9999",
            required_capabilities=("training",),
            require_handshake=True,
            allow_legacy_handshake=False,
            required=False,
        )
        result = (await enforcer.check_many([target]))["reactor_core"]
        assert result.ok
        assert result.reason == "contract_ok"
        assert result.api_version == "152"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_contract_enforcer_requires_workspace_action_semantic_contract():
    async def health(_request):
        return web.json_response(
            {
                "status": "healthy",
                "ready_for_inference": True,
                "protocol_version": "1.1.0",
                "capabilities": ["inference"],
                "semantic_contracts": {
                    "workspace_action": {
                        "version": "workspace_action.v1",
                        "schema_hash": "bad_hash_value",
                    }
                },
            }
        )

    async def handshake(_request):
        return web.json_response(
            {
                "accepted": True,
                "component_instance_id": "prime-1",
                "api_version": "1.1.0",
                "capabilities": ["inference"],
                "health_schema_hash": "abcd1234",
                "metadata": {
                    "semantic_contracts": {
                        "workspace_action": {
                            "version": "workspace_action.v1",
                            "schema_hash": "bad_hash_value",
                        }
                    }
                },
            }
        )

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/lifecycle/handshake", handshake)
    runner, endpoint = await _start_test_server(app)

    try:
        enforcer = CrossRepoContractEnforcer(
            supervisor_instance_id="kernel-test",
            local_protocol_version="1.2.0",
            request_timeout_s=3.0,
        )
        target = ContractTarget(
            name="jarvis_prime",
            endpoint=endpoint,
            health_schema_key="prime:/health",
            min_api_version="1.0.0",
            max_api_version="1.9.9",
            required_capabilities=("inference",),
            require_handshake=True,
            allow_legacy_handshake=False,
            workspace_action_contract_required=True,
            workspace_action_contract_version="workspace_action.v1",
        )
        result = (await enforcer.check_many([target]))["jarvis_prime"]
        assert not result.ok
        assert "semantic_contract_violation" in result.reason
        assert any("schema_hash_mismatch" in v for v in result.semantic_contract_violations)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_contract_enforcer_accepts_matching_workspace_action_semantic_contract():
    enforcer = CrossRepoContractEnforcer(
        supervisor_instance_id="kernel-test",
        local_protocol_version="1.2.0",
        request_timeout_s=3.0,
    )
    expected_hash = enforcer._workspace_action_schema_hash()

    async def health(_request):
        return web.json_response(
            {
                "status": "healthy",
                "ready_for_inference": True,
                "protocol_version": "1.1.0",
                "capabilities": ["inference"],
                "semantic_contracts": {
                    "workspace_action": {
                        "version": "workspace_action.v1",
                        "schema_hash": expected_hash,
                    }
                },
            }
        )

    async def handshake(_request):
        return web.json_response(
            {
                "accepted": True,
                "component_instance_id": "prime-1",
                "api_version": "1.1.0",
                "capabilities": ["inference"],
                "health_schema_hash": "abcd1234",
                "metadata": {
                    "semantic_contracts": {
                        "workspace_action": {
                            "version": "workspace_action.v1",
                            "schema_hash": expected_hash,
                        }
                    }
                },
            }
        )

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/lifecycle/handshake", handshake)
    runner, endpoint = await _start_test_server(app)

    try:
        target = ContractTarget(
            name="jarvis_prime",
            endpoint=endpoint,
            health_schema_key="prime:/health",
            min_api_version="1.0.0",
            max_api_version="1.9.9",
            required_capabilities=("inference",),
            require_handshake=True,
            allow_legacy_handshake=False,
            workspace_action_contract_required=True,
            workspace_action_contract_version="workspace_action.v1",
        )
        result = (await enforcer.check_many([target]))["jarvis_prime"]
        assert result.ok
        assert result.reason == "contract_ok"
    finally:
        await runner.cleanup()


def test_contract_drift_monitor_hysteresis():
    monitor = ContractDriftMonitor(failure_threshold=2, recovery_threshold=2)
    target = ContractTarget(
        name="jarvis_prime",
        endpoint="http://localhost:8001",
        health_schema_key="prime:/health",
        min_api_version="1.0.0",
        max_api_version="1.9.9",
        required_capabilities=("inference",),
    )

    fail = ContractCheckResult(target=target, ok=False, reason="schema_violation")
    ok = ContractCheckResult(target=target, ok=True, reason="contract_ok")

    assert monitor.update({"jarvis_prime": fail}) == []
    degraded = monitor.update({"jarvis_prime": fail})
    assert len(degraded) == 1
    assert degraded[0].to_state == "degraded"

    assert monitor.update({"jarvis_prime": ok}) == []
    recovered = monitor.update({"jarvis_prime": ok})
    assert len(recovered) == 1
    assert recovered[0].to_state == "healthy"
