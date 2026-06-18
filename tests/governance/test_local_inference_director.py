# tests/governance/test_local_inference_director.py
from __future__ import annotations
import importlib

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
