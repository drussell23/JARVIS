"""The voice stack's boot warnings of 2026-10-14 20:08, each traced to a wrong answer.

* "'GhostModelProxy' object has no attribute '_lazy_initialized'": the AI
  Loader's proxy was installed AS the speaker verification service, and an
  adapter wrote a marker attribute nothing reads onto it.
* "Model speaker_verification failed to load" (bridge x3 + ERROR, AGI OS
  degraded): the loader returned None when no learning DB was attached yet,
  and the manager materialised None as "ready".
* "Could not import VoiceUnlockIntegration from any path": five import
  attempts, four of which can never work, hid one missing package.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import types

import pytest

from backend.core import ai_loader


def test_a_loader_that_produces_nothing_is_unavailable_not_ready(caplog):
    manager = ai_loader.AsyncModelManager()
    proxy = ai_loader.GhostModelProxy("vad_silero")
    manager._proxies["vad_silero"] = proxy
    with caplog.at_level(logging.INFO, logger="jarvis.ai_loader"):
        asyncio.run(manager._background_load("vad_silero", lambda: None, ai_loader.ModelPriority.NORMAL))
    assert proxy.status == ai_loader.ModelStatus.UNAVAILABLE and not proxy.is_ready
    assert not any("materialized" in r.message or "ready!" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)

    async def call():
        return await proxy.initialize()
    with pytest.raises(ai_loader.ModelUnavailable):
        asyncio.run(call())


def test_the_loader_module_is_one_module_under_both_names():
    sys.path.insert(0, "backend")
    try:
        bare = importlib.import_module("core.ai_loader")
    finally:
        sys.path.remove("backend")
    assert bare is ai_loader and bare.ModelUnavailable is ai_loader.ModelUnavailable


def test_the_service_accessor_never_returns_a_stand_in(monkeypatch):
    from backend.voice import speaker_verification_service as svs
    monkeypatch.setattr(svs, "_global_speaker_service", ai_loader.GhostModelProxy("speaker_verification"))
    monkeypatch.setattr(svs, "_speaker_verification_service", None)
    service = svs.speaker_verification_service_instance()
    assert isinstance(service, svs.SpeakerVerificationService)


def test_an_unavailable_model_is_skipped_without_retries(monkeypatch):
    from backend.neural_mesh import jarvis_bridge as jb
    bridge = jb.JARVISNeuralMeshBridge.__new__(jb.JARVISNeuralMeshBridge)
    bridge._adapters, bridge._failed_agents = {}, {}
    bridge._metrics = jb.BridgeMetrics()
    bridge._config = types.SimpleNamespace(max_retries=3, retry_on_failure=True, retry_delay=0)
    calls = []

    async def factory(**_kw):
        calls.append(1)
        raise ai_loader.ModelUnavailable("speaker_verification is unavailable: no model")

    assert asyncio.run(bridge._try_register_agent("voice_memory", factory, {}, "voice")) is None
    assert calls == [1] and "voice_memory" not in bridge._failed_agents


def test_a_missing_third_party_package_is_one_note_in_the_boot_summary(monkeypatch):
    from backend.core import optional_dependencies as od
    from backend.neural_mesh.adapters import voice_adapter
    noted = []
    monkeypatch.setattr(od, "note_missing", lambda package, feature, **kw: noted.append((package, feature)))
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "backend.voice.voice_unlock_integration":
            raise ModuleNotFoundError("No module named 'keyring'", name="keyring")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(ModuleNotFoundError):
        asyncio.run(voice_adapter.create_voice_unlock_adapter())
    assert noted == [("keyring", "voice unlock")]


def test_a_missing_module_of_our_own_is_not_called_an_extra(monkeypatch):
    from backend.core import optional_dependencies as od
    from backend.neural_mesh.adapters import voice_adapter
    noted = []
    monkeypatch.setattr(od, "note_missing", lambda package, feature, **kw: noted.append(package))
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "backend.voice.voice_unlock_integration":
            raise ModuleNotFoundError("No module named 'voice_unlock.x'", name="voice_unlock.x")
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(ModuleNotFoundError):
        asyncio.run(voice_adapter.create_voice_unlock_adapter())
    assert noted == []
