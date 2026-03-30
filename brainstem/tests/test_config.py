import os
import pytest
from unittest.mock import patch


def test_config_loads_from_env():
    env = {
        "JARVIS_VERCEL_URL": "https://jarvis.vercel.app",
        "JARVIS_DEVICE_ID": "mac-m1-derek",
        "JARVIS_DEVICE_SECRET": "a" * 64,
    }
    with patch.dict(os.environ, env, clear=False):
        from brainstem.config import BrainstemConfig
        cfg = BrainstemConfig.from_env()
        assert cfg.vercel_url == "https://jarvis.vercel.app"
        assert cfg.device_id == "mac-m1-derek"
        assert cfg.device_secret == "a" * 64
        assert cfg.device_type == "mac"


def test_config_raises_on_missing_url():
    env = {"JARVIS_DEVICE_ID": "mac-m1-derek", "JARVIS_DEVICE_SECRET": "a" * 64}
    with patch.dict(os.environ, env, clear=True):
        from brainstem.config import BrainstemConfig
        with pytest.raises(ValueError, match="JARVIS_VERCEL_URL"):
            BrainstemConfig.from_env()


def test_config_raises_on_missing_secret():
    env = {"JARVIS_VERCEL_URL": "https://jarvis.vercel.app", "JARVIS_DEVICE_ID": "mac-m1-derek"}
    with patch.dict(os.environ, env, clear=True):
        from brainstem.config import BrainstemConfig
        with pytest.raises(ValueError, match="JARVIS_DEVICE_SECRET"):
            BrainstemConfig.from_env()
