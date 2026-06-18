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
