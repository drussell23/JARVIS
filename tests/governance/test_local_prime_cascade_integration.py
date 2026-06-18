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
