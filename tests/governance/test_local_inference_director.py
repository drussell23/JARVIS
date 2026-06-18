# tests/governance/test_local_inference_director.py
from __future__ import annotations
import importlib
import pytest

MOD = "backend.core.ouroboros.governance.local_inference_director"


def test_local_prime_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_LOCAL_PRIME_ENABLED", raising=False)
    lid = importlib.import_module(MOD)
    assert lid.local_prime_enabled() is False


def test_local_prime_enable_toggle(monkeypatch):
    lid = importlib.import_module(MOD)
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    assert lid.local_prime_enabled() is True
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    assert lid.local_prime_enabled() is False


def test_config_defaults(monkeypatch):
    for k in ("JARVIS_LOCAL_MODEL_BASE_URL", "JARVIS_LOCAL_MODEL_NAME",
              "JARVIS_LOCAL_MODEL_KEEP_ALIVE_SECONDS", "JARVIS_LOCAL_INFERENCE_TIMEOUT_MS"):
        monkeypatch.delenv(k, raising=False)
    lid = importlib.import_module(MOD)
    cfg = lid.LocalConfig.from_env()
    assert cfg.base_url == "http://127.0.0.1:11434"
    assert cfg.model_name == "qwen2.5-coder:3b"
    assert cfg.keep_alive_seconds == 300
    assert cfg.timeout_ceiling_ms == 120_000


def test_profiler_cold_start_uses_seed():
    from backend.core.ouroboros.governance.local_inference_director import LatencyProfiler, LocalConfig
    cfg = LocalConfig.from_env()
    p = LatencyProfiler(cfg)
    assert p.adaptive_timeout_ms(prompt_tokens=1000) == min(cfg.timeout_seed_ms, cfg.timeout_ceiling_ms)
    assert p.is_warm() is False


def test_profiler_warms_and_scales_with_prompt_size():
    from backend.core.ouroboros.governance.local_inference_director import LatencyProfiler, LocalConfig
    cfg = LocalConfig.from_env()
    p = LatencyProfiler(cfg)
    for _ in range(6):
        p.record(ttft_ms=200.0, total_ms=200.0 + 100 * 10.0, output_tokens=100)
    assert p.is_warm() is True
    t_big = p.adaptive_timeout_ms(prompt_tokens=2000)
    t_small = p.adaptive_timeout_ms(prompt_tokens=200)
    assert t_big > t_small
    assert t_big <= cfg.timeout_ceiling_ms


def test_profiler_never_exceeds_ceiling_even_with_huge_prompt():
    from backend.core.ouroboros.governance.local_inference_director import LatencyProfiler, LocalConfig
    cfg = LocalConfig.from_env()
    p = LatencyProfiler(cfg)
    for _ in range(6):
        p.record(ttft_ms=500.0, total_ms=5000.0, output_tokens=100)
    assert p.adaptive_timeout_ms(prompt_tokens=10_000_000) == cfg.timeout_ceiling_ms


def test_profiler_three_sigma_terminal_lag():
    from backend.core.ouroboros.governance.local_inference_director import LatencyProfiler, LocalConfig
    cfg = LocalConfig.from_env()
    p = LatencyProfiler(cfg)
    for _ in range(6):
        p.record(ttft_ms=200.0, total_ms=1000.0, output_tokens=100)
    assert p.is_terminal_lag(elapsed_ms=1100.0) is False
    assert p.is_terminal_lag(elapsed_ms=50_000.0) is True
    assert p.is_terminal_lag(elapsed_ms=cfg.timeout_ceiling_ms + 1) is True


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p


class _FakeSession:
    def __init__(self, payload):
        self._p = payload
        self.closed = False
        self.posts = []

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return _FakeResp(self._p)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_client_generate_posts_to_openai_compat_with_keep_alive():
    from backend.core.ouroboros.governance.local_inference_director import LocalPrimeClient, LocalConfig
    payload = {"choices": [{"message": {"content": "patched code"}}],
               "usage": {"completion_tokens": 12}}
    fake = _FakeSession(payload)
    cfg = LocalConfig.from_env()
    client = LocalPrimeClient(cfg, session=fake)
    out = await client.complete(system="<sys/>", user="<task/>", prompt_tokens=100)
    assert out.text == "patched code"
    url, kw = fake.posts[-1]
    assert url.endswith("/v1/chat/completions")
    assert kw["json"]["keep_alive"] == cfg.keep_alive_seconds
    assert kw["json"]["model"] == cfg.model_name


@pytest.mark.asyncio
async def test_client_close_releases_session():
    from backend.core.ouroboros.governance.local_inference_director import LocalPrimeClient, LocalConfig
    fake = _FakeSession({"choices": [{"message": {"content": "x"}}]})
    client = LocalPrimeClient(LocalConfig.from_env(), session=fake)
    await client.aclose()
    assert fake.closed is True


def test_structured_prompt_uses_bounded_tags():
    from backend.core.ouroboros.governance.local_inference_director import render_structured_prompt
    s = render_structured_prompt(task="fix bug", constraints=["no new deps"], files={"a.py": "x=1"})
    assert "<task>" in s and "</task>" in s
    assert "<constraints>" in s and "<files>" in s


@pytest.mark.asyncio
async def test_breaker_trips_on_ceiling_breach(monkeypatch):
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalPrimeClient, LocalConfig, LocalLatencyLockup)
    monkeypatch.setenv("JARVIS_LOCAL_INFERENCE_TIMEOUT_MS", "50")  # tiny ceiling

    class _SlowSession:
        closed = False

        def post(self, url, **kw):
            class _R:
                status = 200

                async def __aenter__(self_):
                    import asyncio
                    await asyncio.sleep(0.2)  # 200ms > 50ms ceiling
                    return self_

                async def __aexit__(self_, *a):
                    return False

                async def json(self_):
                    return {"choices": [{"message": {"content": "x"}}]}
            return _R()

        async def close(self):
            self.closed = True

    client = LocalPrimeClient(LocalConfig.from_env(), session=_SlowSession())
    with pytest.raises(LocalLatencyLockup):
        await client.complete_guarded(system="<s/>", user="<u/>", prompt_tokens=10)



@pytest.mark.asyncio
async def test_critical_eviction_unloads_and_gc(monkeypatch):
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalInferenceDirector, LocalConfig, LocalPrimeClient)
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    evicted = {"calls": []}

    class _EvictSession:
        closed = False

        def post(self, url, **kw):
            evicted["calls"].append(kw.get("json", {}))

            class _R:
                status = 200

                async def __aenter__(self_):
                    return self_

                async def __aexit__(self_, *a):
                    return False

                async def json(self_):
                    return {"status": "ok"}
            return _R()

        async def close(self):
            self.closed = True

    client = LocalPrimeClient(LocalConfig.from_env(), session=_EvictSession())
    d = LocalInferenceDirector(LocalConfig.from_env(), client=client)
    gc_calls = {"n": 0}
    monkeypatch.setattr("gc.collect", lambda *a, **k: gc_calls.__setitem__("n", gc_calls["n"] + 1) or 0)
    await d.enforce_memory(PressureLevel.CRITICAL)
    assert any(c.get("keep_alive") == 0 for c in evicted["calls"])  # forced unload
    assert gc_calls["n"] >= 2  # dual-stage gc.collect()


@pytest.mark.asyncio
async def test_generate_returns_primeresponse_with_content():
    from backend.core.ouroboros.governance.local_inference_director import LocalPrimeClient, LocalConfig
    fake = _FakeSession({"choices": [{"message": {"content": "GENERATED"}}],
                         "usage": {"completion_tokens": 7}})
    client = LocalPrimeClient(LocalConfig.from_env(), session=fake)
    resp = await client.generate(prompt="do x", system_prompt="be terse", max_tokens=256, temperature=0.0)
    assert resp.content == "GENERATED"
    assert resp.source == "local_prime"
    assert resp.request_id  # non-empty
    # max_tokens forwarded to the Ollama body
    _, kw = fake.posts[-1]
    assert kw["json"].get("max_tokens") == 256


@pytest.mark.asyncio
async def test_check_health_available_on_200():
    from backend.core.ouroboros.governance.local_inference_director import LocalPrimeClient, LocalConfig

    class _HealthResp:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _HealthSession:
        closed = False
        def get(self, url, **kw): return _HealthResp()
        def post(self, url, **kw): raise AssertionError("health must use GET")
        async def close(self): self.closed = True

    client = LocalPrimeClient(LocalConfig.from_env(), session=_HealthSession())
    status = await client._check_health()
    assert status.name == "AVAILABLE"


def test_build_local_prime_client_off_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "false")
    from backend.core.ouroboros.governance.local_inference_director import build_local_prime_client
    assert build_local_prime_client() is None


def test_build_local_prime_client_on_returns_client(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_PRIME_ENABLED", "true")
    from backend.core.ouroboros.governance.local_inference_director import (
        build_local_prime_client, LocalPrimeClient)
    assert isinstance(build_local_prime_client(), LocalPrimeClient)


@pytest.mark.asyncio
async def test_memory_guard_critical_evicts_and_raises(monkeypatch):
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalInferenceDirector, LocalConfig, LocalPrimeClient, LocalMemoryCritical)
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")
    evicted = {"calls": []}

    class _EvictSession:
        closed = False
        def post(self, url, **kw):
            evicted["calls"].append(kw.get("json", {}))
            class _R:
                status = 200
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): return False
                async def json(self_): return {"status": "ok"}
            return _R()
        async def close(self): self.closed = True

    class _FakeGate:
        def pressure(self): return PressureLevel.CRITICAL

    client = LocalPrimeClient(LocalConfig.from_env(), session=_EvictSession())
    d = LocalInferenceDirector(LocalConfig.from_env(), client=client, gate=_FakeGate())
    with pytest.raises(LocalMemoryCritical):
        await d.memory_guard()
    assert any(c.get("keep_alive") == 0 for c in evicted["calls"])  # evicted before refusing


@pytest.mark.asyncio
async def test_memory_guard_ok_passes(monkeypatch):
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalInferenceDirector, LocalConfig)
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "true")

    class _FakeGate:
        def pressure(self): return PressureLevel.OK

    d = LocalInferenceDirector(LocalConfig.from_env(), client=object(), gate=_FakeGate())
    await d.memory_guard()  # returns None, no raise


@pytest.mark.asyncio
async def test_memory_guard_passthrough_when_gate_disabled(monkeypatch):
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalInferenceDirector, LocalConfig)
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_GATE_ENABLED", "false")

    class _FakeGate:  # even if it would say CRITICAL, disabled gate => pass-through
        def pressure(self): return PressureLevel.CRITICAL

    d = LocalInferenceDirector(LocalConfig.from_env(), client=object(), gate=_FakeGate())
    await d.memory_guard()  # no raise because the gate master switch is OFF


@pytest.mark.asyncio
async def test_generate_consults_governor_and_refuses_at_critical():
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalPrimeClient, LocalConfig, LocalMemoryCritical)

    class _RecordSession:
        closed = False
        def __init__(self): self.posts = []
        def post(self, url, **kw):
            self.posts.append((url, kw))
            class _R:
                status = 200
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): return False
                async def json(self_): return {"choices": [{"message": {"content": "x"}}]}
            return _R()
        async def close(self): self.closed = True

    class _CriticalGov:
        async def memory_guard(self):
            raise LocalMemoryCritical("host CRITICAL")

    sess = _RecordSession()
    client = LocalPrimeClient(LocalConfig.from_env(), session=sess)
    client.attach_governor(_CriticalGov())
    with pytest.raises(LocalMemoryCritical):
        await client.generate(prompt="do x", system_prompt="s", max_tokens=64)
    # refused BEFORE any inference call to Ollama
    assert sess.posts == []


@pytest.mark.asyncio
async def test_generate_calls_guard_then_proceeds_when_ok():
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalPrimeClient, LocalConfig)
    calls = {"guard": 0}

    class _OkGov:
        async def memory_guard(self):
            calls["guard"] += 1  # OK -> returns without raising

    fake_payload = {"choices": [{"message": {"content": "OK"}}], "usage": {"completion_tokens": 3}}

    class _FakeSession:
        closed = False
        def __init__(self): self.posts = []
        def post(self, url, **kw):
            self.posts.append((url, kw))
            class _R:
                status = 200
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): return False
                async def json(self_): return fake_payload
            return _R()
        async def close(self): self.closed = True

    sess = _FakeSession()
    client = LocalPrimeClient(LocalConfig.from_env(), session=sess)
    client.attach_governor(_OkGov())
    resp = await client.generate(prompt="hi", system_prompt="s", max_tokens=16)
    assert resp.content == "OK"
    assert calls["guard"] == 1            # guard consulted
    assert len(sess.posts) == 1           # inference proceeded after guard passed


@pytest.mark.asyncio
async def test_generate_no_governor_is_phase3_behavior():
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalPrimeClient, LocalConfig)
    fake_payload = {"choices": [{"message": {"content": "Z"}}], "usage": {"completion_tokens": 2}}

    class _FakeSession:
        closed = False
        def __init__(self): self.posts = []
        def post(self, url, **kw):
            self.posts.append((url, kw))
            class _R:
                status = 200
                async def __aenter__(self_): return self_
                async def __aexit__(self_, *a): return False
                async def json(self_): return fake_payload
            return _R()
        async def close(self): self.closed = True

    client = LocalPrimeClient(LocalConfig.from_env(), session=_FakeSession())
    # no attach_governor -> governor is None -> unchanged Phase 3 behavior
    resp = await client.generate(prompt="hi", system_prompt="s")
    assert resp.content == "Z"
