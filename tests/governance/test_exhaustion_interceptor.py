# tests/governance/test_exhaustion_interceptor.py
from __future__ import annotations
import dataclasses
import pytest


@dataclasses.dataclass
class _Ctx:
    op_id: str = "op1"
    target_files: tuple = ("central.py", "mid.py", "orphan.py")


class _Resp:
    def __init__(self, c): self.content = c


class _Jprime:
    def __init__(self, *, healthy=True, fail=False):
        self._healthy = healthy; self._fail = fail
        self.gen_calls = 0; self.seen_target_files = None
    async def health_probe(self): return self._healthy
    async def generate(self, context, deadline):
        self.gen_calls += 1
        self.seen_target_files = tuple(getattr(context, "target_files", ()))
        if self._fail: raise RuntimeError("local boom")
        return _Resp("LOCAL_ABSORBED")


class _Broker:
    def __init__(self): self.events = []
    def publish(self, **kw): self.events.append(kw)


def test_lastresort_enabled_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_JPRIME_LASTRESORT_ENABLED", raising=False)
    from backend.core.ouroboros.governance.exhaustion_interceptor import lastresort_enabled
    assert lastresort_enabled() is False
    monkeypatch.setenv("JARVIS_JPRIME_LASTRESORT_ENABLED", "true")
    assert lastresort_enabled() is True


def test_should_intercept_only_on_exhaustion_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_LASTRESORT_ENABLED", "true")
    from backend.core.ouroboros.governance.exhaustion_interceptor import should_intercept
    assert should_intercept(RuntimeError("all_providers_exhausted:fallback_failed"), jprime=_Jprime()) is True
    assert should_intercept(RuntimeError("something_else"), jprime=_Jprime()) is False
    assert should_intercept(RuntimeError("all_providers_exhausted"), jprime=None) is False
    monkeypatch.setenv("JARVIS_JPRIME_LASTRESORT_ENABLED", "false")
    assert should_intercept(RuntimeError("all_providers_exhausted"), jprime=_Jprime()) is False


@pytest.mark.asyncio
async def test_local_last_resort_absorbs_and_prunes_and_beacons(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_LASTRESORT_ENABLED", "true")
    from backend.core.ouroboros.governance.exhaustion_interceptor import execute_local_last_resort
    jp = _Jprime(); broker = _Broker()
    ctx = _Ctx()
    # huge per-file tokens force pruning under a tiny ceiling; central.py wins (highest degree)
    file_tokens = {"central.py": 600, "mid.py": 600, "orphan.py": 600}

    class _FakeBackend:
        def nodes_in_file(self, f): return {"central.py": ["c"], "mid.py": ["m"], "orphan.py": ["o"]}.get(f, [])
        def successor_keys(self, k): return {"c": ["x","y","z"], "m": ["x"], "o": []}.get(k, [])
        def predecessor_keys(self, k): return {"c": ["a","b"], "m": [], "o": []}.get(k, [])

    result = await execute_local_last_resort(
        jprime=jp, context=ctx, deadline=None, graph_backend=_FakeBackend(),
        broker=broker, file_tokens=file_tokens, ceiling_tokens=1000, original_exc=RuntimeError("all_providers_exhausted:fallback_failed"))
    assert result.content == "LOCAL_ABSORBED"
    assert jp.gen_calls == 1
    # pruned: orphan.py (degree 0) discarded; central.py kept
    assert "central.py" in jp.seen_target_files
    assert "orphan.py" not in jp.seen_target_files
    # beacon emitted with the right event type + discarded filenames + token differential
    assert len(broker.events) == 1
    ev = broker.events[0]
    assert ev.get("event_type") == "exhaustion_handoff_triggered"
    data = ev.get("data") or {}
    assert "orphan.py" in (data.get("discarded_files") or [])
    assert data.get("tokens_before", 0) >= data.get("tokens_after", 0)


@pytest.mark.asyncio
async def test_local_unhealthy_reraises_original(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_LASTRESORT_ENABLED", "true")
    from backend.core.ouroboros.governance.exhaustion_interceptor import execute_local_last_resort
    jp = _Jprime(healthy=False); orig = RuntimeError("all_providers_exhausted:queue_only")
    with pytest.raises(RuntimeError, match="all_providers_exhausted"):
        await execute_local_last_resort(jprime=jp, context=_Ctx(), deadline=None,
                                        graph_backend=None, broker=None, file_tokens={}, original_exc=orig)
    assert jp.gen_calls == 0  # never generated when local is unhealthy


@pytest.mark.asyncio
async def test_local_generate_failure_reraises_original(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_LASTRESORT_ENABLED", "true")
    from backend.core.ouroboros.governance.exhaustion_interceptor import execute_local_last_resort
    jp = _Jprime(fail=True); orig = RuntimeError("all_providers_exhausted:fallback_failed")
    with pytest.raises(RuntimeError, match="all_providers_exhausted"):
        await execute_local_last_resort(jprime=jp, context=_Ctx(), deadline=None,
                                        graph_backend=None, broker=None, file_tokens={"central.py":1}, original_exc=orig)
