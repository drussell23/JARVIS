import asyncio
from types import SimpleNamespace

import pytest

from backend.intelligence.unified_model_serving import (
    ModelProvider,
    PrimeLocalClient,
    UnifiedModelServing,
)


@pytest.mark.asyncio
async def test_unload_component_arms_recovery_and_opens_circuit():
    serving = UnifiedModelServing()
    serving._running = True

    local = PrimeLocalClient()
    local._model = object()
    local._loaded = True
    serving._clients[ModelProvider.PRIME_LOCAL] = local

    unloaded = await serving._unload_local_model(
        reason="component_unload",
        arm_recovery=True,
    )

    assert unloaded is True
    assert local._model is None
    assert local._loaded is False
    assert serving._memory_recovery_armed is True
    assert serving.get_local_circuit_state() == "open"


@pytest.mark.asyncio
async def test_memory_recovery_callback_resets_circuit_after_verified_reload(monkeypatch):
    serving = UnifiedModelServing()
    serving._running = True
    serving._memory_recovery_armed = True
    serving._last_local_unload_reason = "component_unload"
    monkeypatch.setenv("JARVIS_LOCAL_RECOVERY_COOLDOWN_SECONDS", "0")

    local = PrimeLocalClient()
    local._model = None
    local._loaded = False
    serving._clients[ModelProvider.PRIME_LOCAL] = local

    calls = {"load": 0, "smoke": 0, "reset": 0, "force": 0}

    async def _load_model(_model_name=None):
        calls["load"] += 1
        local._model = object()
        local._loaded = True
        return True

    async def _smoke_test(*_args, **_kwargs):
        calls["smoke"] += 1
        return True

    real_reset = serving.reset_local_circuit_breaker

    def _reset():
        calls["reset"] += 1
        return real_reset()

    real_force = serving.force_open_local_circuit_breaker

    def _force(*_args, **_kwargs):
        calls["force"] += 1
        return real_force(*_args, **_kwargs)

    monkeypatch.setattr(serving, "load_model", _load_model)
    monkeypatch.setattr(serving, "smoke_test_local_model", _smoke_test)
    monkeypatch.setattr(serving, "reset_local_circuit_breaker", _reset)
    monkeypatch.setattr(serving, "force_open_local_circuit_breaker", _force)

    await serving._handle_memory_recovery(
        SimpleNamespace(value="critical"),
        SimpleNamespace(value="optimal"),
    )

    assert calls["load"] == 1
    assert calls["smoke"] == 1
    assert calls["reset"] == 1
    assert calls["force"] >= 1
    assert serving._memory_recovery_armed is False
    assert serving._memory_recovery_last_success > 0
    handshake = serving.get_local_ready_handshake()
    assert handshake["ready"] is True
    assert handshake["verified"] is True
    assert handshake["circuit_state"] == "closed"


@pytest.mark.asyncio
async def test_memory_recovery_callback_fail_closed_when_smoke_fails(monkeypatch):
    serving = UnifiedModelServing()
    serving._running = True
    serving._memory_recovery_armed = True
    serving._last_local_unload_reason = "component_unload"
    monkeypatch.setenv("JARVIS_LOCAL_RECOVERY_COOLDOWN_SECONDS", "0")

    local = PrimeLocalClient()
    local._model = None
    local._loaded = False
    serving._clients[ModelProvider.PRIME_LOCAL] = local

    unload_calls = []

    async def _load_model(_model_name=None):
        return True

    async def _smoke_test(*_args, **_kwargs):
        return False

    async def _unload(*, reason="unspecified", arm_recovery=None):
        unload_calls.append((reason, arm_recovery))
        return True

    monkeypatch.setattr(serving, "load_model", _load_model)
    monkeypatch.setattr(serving, "smoke_test_local_model", _smoke_test)
    monkeypatch.setattr(serving, "_unload_local_model", _unload)
    monkeypatch.setattr(
        serving,
        "reset_local_circuit_breaker",
        lambda: pytest.fail("circuit reset must not run when smoke fails"),
    )

    await serving._handle_memory_recovery(
        SimpleNamespace(value="critical"),
        SimpleNamespace(value="optimal"),
    )

    assert unload_calls == [("recovery_smoke_failed", True)]
    assert serving._memory_recovery_armed is True


@pytest.mark.asyncio
async def test_recovery_singleflight_joins_concurrent_callers(monkeypatch):
    serving = UnifiedModelServing()
    serving._running = True
    serving._memory_recovery_armed = True
    monkeypatch.setenv("JARVIS_LOCAL_RECOVERY_COOLDOWN_SECONDS", "0")

    local = PrimeLocalClient()
    local._model = None
    local._loaded = False
    serving._clients[ModelProvider.PRIME_LOCAL] = local

    release_load = asyncio.Event()
    calls = {"load": 0, "smoke": 0}

    async def _load_model(_model_name=None):
        calls["load"] += 1
        await release_load.wait()
        local._model = object()
        local._loaded = True
        return True

    async def _smoke_test(*_args, **_kwargs):
        calls["smoke"] += 1
        return True

    monkeypatch.setattr(serving, "load_model", _load_model)
    monkeypatch.setattr(serving, "smoke_test_local_model", _smoke_test)

    t1 = asyncio.create_task(
        serving.recover_local_model_singleflight(
            trigger="test_singleflight_1",
            require_armed=False,
            respect_cooldown=False,
        )
    )
    await asyncio.sleep(0)
    t2 = asyncio.create_task(
        serving.recover_local_model_singleflight(
            trigger="test_singleflight_2",
            require_armed=False,
            respect_cooldown=False,
        )
    )
    await asyncio.sleep(0)
    release_load.set()

    r1 = await t1
    r2 = await t2

    assert calls["load"] == 1
    assert calls["smoke"] == 1
    assert r1["ok"] is True
    assert r2["ok"] is True
    assert r2["singleflight_joined"] is True


@pytest.mark.asyncio
async def test_recovery_emits_background_warmup_hooks(monkeypatch):
    serving = UnifiedModelServing()
    serving._running = True
    serving._memory_recovery_armed = True
    monkeypatch.setenv("JARVIS_LOCAL_RECOVERY_COOLDOWN_SECONDS", "0")

    local = PrimeLocalClient()
    local._model = None
    local._loaded = False
    serving._clients[ModelProvider.PRIME_LOCAL] = local

    async def _load_model(_model_name=None):
        local._model = object()
        local._loaded = True
        return True

    async def _smoke_test(*_args, **_kwargs):
        return True

    events = []

    async def _hook(event):
        events.append(event)

    serving.register_local_warmup_hook(_hook)
    monkeypatch.setattr(serving, "load_model", _load_model)
    monkeypatch.setattr(serving, "smoke_test_local_model", _smoke_test)

    result = await serving.recover_local_model_singleflight(
        trigger="test_hook",
        require_armed=False,
        respect_cooldown=False,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    phases = [evt.get("phase") for evt in events]
    assert result["ok"] is True
    assert "recovery_start" in phases
    assert "recovery_success" in phases
