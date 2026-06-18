from __future__ import annotations

import pytest


def test_lockup_maps_to_primary_degraded():
    from backend.core.ouroboros.governance.local_inference_director import LocalLatencyLockup
    from backend.core.ouroboros.governance.candidate_generator import classify_local_failure
    verdict = classify_local_failure(LocalLatencyLockup("timeout"))
    assert verdict.degrade is True
    assert verdict.target_state == "PRIMARY_DEGRADED"
    assert verdict.cascade_upstream is True


def test_normal_exception_does_not_degrade():
    from backend.core.ouroboros.governance.candidate_generator import classify_local_failure
    verdict = classify_local_failure(ValueError("schema"))
    assert verdict.degrade is False
    assert verdict.target_state is None
    assert verdict.cascade_upstream is False


@pytest.mark.asyncio
async def test_killswitch_off_makes_no_ollama_call(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    from backend.core.ouroboros.governance.local_inference_director import build_local_prime_client
    # OFF -> no client is ever constructed, so no endpoint contact is possible.
    assert build_local_prime_client() is None


@pytest.mark.asyncio
async def test_director_stop_closes_session_no_leak(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalConfig, LocalPrimeClient, LocalInferenceDirector)

    class _S:
        closed = False

        async def close(self):
            self.closed = True

    fake = _S()
    client = LocalPrimeClient(LocalConfig.from_env(), session=fake)
    d = LocalInferenceDirector(LocalConfig.from_env(), client=client)
    await d.stop()
    assert client._session is None   # released
    assert fake.closed is True       # zero hanging FDs


def test_memory_critical_maps_to_primary_degraded():
    from backend.core.ouroboros.governance.local_inference_director import LocalMemoryCritical
    from backend.core.ouroboros.governance.candidate_generator import classify_local_failure
    verdict = classify_local_failure(LocalMemoryCritical("host CRITICAL"))
    assert verdict.degrade is True
    assert verdict.target_state == "PRIMARY_DEGRADED"
    assert verdict.cascade_upstream is True


def test_memory_critical_failure_class_attr():
    from backend.core.ouroboros.governance.local_inference_director import LocalMemoryCritical
    assert LocalMemoryCritical("x").failure_class == "local_memory_critical"
    assert isinstance(LocalMemoryCritical("x"), RuntimeError)


@pytest.mark.asyncio
async def test_end_to_end_client_with_real_director_refuses_at_critical(monkeypatch):
    """Full chain: attach real LocalInferenceDirector(gate=CRITICAL) -> client.generate
    consults memory_guard -> evicts model + raises LocalMemoryCritical (no inference)."""
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalConfig, LocalPrimeClient, LocalInferenceDirector, LocalMemoryCritical)
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")

    class _Session:
        closed = False
        def __init__(self): self.posts = []
        def post(self, url, **kw):
            self.posts.append((url, kw))
            class _R:
                status = 200
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): return False
                async def json(self_): return {"choices": [{"message": {"content": "x"}}],
                                               "status": "ok"}
            return _R()
        async def close(self): self.closed = True

    class _CriticalGate:
        def pressure(self): return PressureLevel.CRITICAL

    sess = _Session()
    client = LocalPrimeClient(LocalConfig.from_env(), session=sess)
    director = LocalInferenceDirector(LocalConfig.from_env(), client=client, gate=_CriticalGate())
    client.attach_governor(director)

    with pytest.raises(LocalMemoryCritical):
        await client.generate(prompt="do x", system_prompt="s", max_tokens=64)
    # the ONLY post allowed is the eviction unload (keep_alive:0); NO chat/completions inference
    assert all("/v1/chat/completions" not in url for url, _ in sess.posts)
    assert any(kw.get("json", {}).get("keep_alive") == 0 for _, kw in sess.posts)


@pytest.mark.asyncio
async def test_killswitch_off_no_governor_no_guard(monkeypatch):
    """With the local tier OFF, build_local_prime_client() is None, so nothing is
    injected, no governor is attached, and memory_guard is never reachable --
    byte-identical to pre-Phase-3.1 behavior."""
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    from backend.core.ouroboros.governance.local_inference_director import build_local_prime_client
    assert build_local_prime_client() is None
