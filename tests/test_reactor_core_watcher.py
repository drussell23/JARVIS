# [Ouroboros] Modified by Ouroboros (op=op-01a10939-) at 2026-10-04 23:27 UTC
# Reason: `backend/autonomy/reactor_core_watcher.py` has no corresponding test module. CREATE `tests/test_reactor_core_watcher.py`

from __future__ import annotations
import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from backend.autonomy.reactor_core_watcher import (
    DeploymentResult,
    ModelValidator,
    ReactorCoreConfig,
    ReactorCoreWatcher,
    get_reactor_core_watcher,
    start_reactor_core_watcher,
    stop_reactor_core_watcher,
)


# Test fixtures and helpers

def _make_mock_config(**overrides) -> ReactorCoreConfig:
    """Create a mock ReactorCoreConfig with default values."""
    defaults = {
        "watch_dir": Path("/tmp/watch"),
        "local_models_dir": Path("/tmp/models"),
        "gcs_bucket": "test-bucket",
        "upload_to_gcs": True,
        "deploy_local": True,
        "watch_patterns": ["*.gguf"],
        "debounce_seconds": 1.0,
        "min_model_size_bytes": 100 * 1024 * 1024,
        "auto_activate": True,
        "jarvis_prime_local_url": "http://localhost:8002",
        "jarvis_prime_cloud_url": "https://cloud.run.app",
        "smoke_test_enabled": True,
    }
    defaults.update(overrides)
    return ReactorCoreConfig(**defaults)


def _make_mock_deployment_result(**overrides) -> DeploymentResult:
    """Create a mock DeploymentResult with default values."""
    defaults = {
        "success": True,
        "model_name": "test_model.gguf",
        "model_path": "/tmp/test_model.gguf",
        "model_size_mb": 100.0,
        "checksum": "abc123def456",
    }
    defaults.update(overrides)
    return DeploymentResult(**defaults)


def _make_mock_watcher(config: Optional[ReactorCoreConfig] = None) -> ReactorCoreWatcher:
    """Create a mock ReactorCoreWatcher instance."""
    return ReactorCoreWatcher(config or _make_mock_config())


# Import smoke test

def test_import_smoke_test():
    """Test that all modules can be imported without errors."""
    # This should not raise any ImportError
    from backend.autonomy.reactor_core_watcher import (
        ReactorCoreConfig,
        DeploymentResult,
        ModelValidator,
        ReactorCoreWatcher,
        get_reactor_core_watcher,
        start_reactor_core_watcher,
        stop_reactor_core_watcher,
    )

    # Verify that the classes are accessible
    assert ReactorCoreConfig
    assert DeploymentResult
    assert ModelValidator
    assert ReactorCoreWatcher
    assert get_reactor_core_watcher
    assert start_reactor_core_watcher
    assert stop_reactor_core_watcher


# Test public functions and edge cases

def test_reactor_core_config_defaults():
    """Test that ReactorCoreConfig has correct default values."""
    config = ReactorCoreConfig()
    
    assert config.watch_dir == Path.home() / "Documents" / "repos" / "reactor-core" / "output"
    assert config.local_models_dir == Path.home() / "Documents" / "repos" / "jarvis-prime" / "models"
    assert config.gcs_bucket == "gs://jarvis-473803-deployments/models"
    assert config.upload_to_gcs is True
    assert config.deploy_local is True
    assert config.watch_patterns == ["*.gguf", "*.bin"]
    assert config.debounce_seconds == 5.0
    assert config.min_model_size_bytes == 100 * 1024 * 1024
    assert config.auto_activate is True
    assert config.jarvis_prime_local_url == "http://127.0.0.1:8002"
    assert config.jarvis_prime_cloud_url == "https://jarvis-prime-dev-888774109345.us-central1.run.app"
    assert config.smoke_test_enabled is True


def test_reactor_core_config_custom_values():
    """Test that ReactorCoreConfig accepts custom values."""
    custom_config = _make_mock_config(
        watch_dir=Path("/custom/watch"),
        gcs_bucket="custom-bucket",
        upload_to_gcs=False,
        smoke_test_enabled=False,
    )
    
    assert custom_config.watch_dir == Path("/custom/watch")
    assert custom_config.gcs_bucket == "custom-bucket"
    assert custom_config.upload_to_gcs is False
    assert custom_config.smoke_test_enabled is False


def test_model_validator_validate_valid_file(tmp_path):
    """Test that ModelValidator.validate correctly identifies valid GGUF files."""
    # Create a mock GGUF file with proper magic bytes and size (at least 100MB for validation)
    model_path = tmp_path / "valid_model.gguf"
    with open(model_path, "wb") as f:
        f.write(b"GGUF" + b"\x00" * (100 * 1024 * 1024))  # GGUF magic bytes + 100MB padding
    
    is_valid, error = ModelValidator.validate(model_path)
    assert is_valid is True
    assert error == "Valid"


def test_model_validator_validate_invalid_file(tmp_path):
    """Test that ModelValidator.validate correctly identifies invalid files."""
    # Create a file without GGUF magic bytes but with sufficient size
    model_path = tmp_path / "invalid_model.bin"
    with open(model_path, "wb") as f:
        f.write(b"\x00" * (100 * 1024 * 1024 + 100))  # Large file without GGUF magic
    
    is_valid, error = ModelValidator.validate(model_path)
    assert is_valid is False
    assert "Invalid GGUF magic bytes" in error


def test_model_validator_validate_small_file(tmp_path):
    """Test that ModelValidator.validate correctly identifies files below minimum size."""
    # Create a file smaller than minimum size (100MB)
    model_path = tmp_path / "small_model.gguf"
    with open(model_path, "wb") as f:
        f.write(b"GGUF")  # Only magic bytes
    
    is_valid, error = ModelValidator.validate(model_path, min_size_bytes=100 * 1024 * 1024)
    assert is_valid is False
    assert "File too small" in error


def test_model_validator_compute_checksum(tmp_path):
    """Test that ModelValidator.compute_checksum computes correct checksum."""
    model_path = tmp_path / "checksum_test.gguf"
    with open(model_path, "wb") as f:
        f.write(b"GGUF" + b"test content")
    
    checksum = ModelValidator.compute_checksum(model_path)
    assert isinstance(checksum, str)
    assert len(checksum) == 16  # First 16 chars


def test_reactor_core_watcher_init():
    """Test that ReactorCoreWatcher initializes correctly."""
    config = _make_mock_config()
    watcher = ReactorCoreWatcher(config)
    
    assert watcher.config == config
    assert watcher._running is False
    assert watcher._watch_task is None
    assert watcher._pending_files == {}
    assert watcher._deployed_checksums == set()
    assert watcher._deploy_callbacks == []
    assert watcher._http_client is None


def test_reactor_core_watcher_register_callback():
    """Test that ReactorCoreWatcher.register_callback works correctly."""
    watcher = _make_mock_watcher()
    
    callback = AsyncMock()
    watcher.register_callback(callback)
    
    assert len(watcher._deploy_callbacks) == 1
    assert watcher._deploy_callbacks[0] == callback


def test_reactor_core_watcher_start_already_running():
    """Test that ReactorCoreWatcher.start handles already running case."""
    watcher = _make_mock_watcher()
    
    # Mock the start process to simulate already running
    watcher._running = True
    
    with patch("backend.autonomy.reactor_core_watcher.logger") as mock_logger:
        asyncio.run(watcher.start())
        mock_logger.warning.assert_called_once_with("[ReactorCoreWatcher] Already running")


def test_reactor_core_watcher_stop():
    """Test that ReactorCoreWatcher.stop works correctly."""
    watcher = _make_mock_watcher()
    
    # Mock a running watcher
    watcher._running = True
    
    # Create an event loop for the test
    async def run_stop():
        # Create a mock task
        watcher._watch_task = asyncio.create_task(asyncio.sleep(0.1))
        with patch("backend.autonomy.reactor_core_watcher.logger"):
            await watcher.stop()
            assert watcher._running is False
            assert watcher._watch_task is None
    
    # Run the async function in an event loop
    asyncio.run(run_stop())


def test_reactor_core_watcher_deploy_model_invalid_file(tmp_path):
    """Test that ReactorCoreWatcher.deploy_model handles invalid files correctly."""
    watcher = _make_mock_watcher()
    
    # Create an invalid file (no GGUF magic but with sufficient size)
    model_path = tmp_path / "invalid.gguf"
    with open(model_path, "wb") as f:
        f.write(b"\x00" * (100 * 1024 * 1024 + 100))  # Large file without GGUF magic
    
    result = asyncio.run(watcher.deploy_model(model_path))
    assert result.success is False
    assert "Invalid GGUF magic bytes" in result.error


def test_reactor_core_watcher_manual_deploy_file_not_found():
    """Test that ReactorCoreWatcher.manual_deploy handles missing files correctly."""
    watcher = _make_mock_watcher()
    
    result = asyncio.run(watcher.manual_deploy("/nonexistent/file.gguf"))
    assert result.success is False
    assert "File not found" in result.error


def test_reactor_core_watcher_get_reactor_core_watcher():
    """Test that get_reactor_core_watcher returns correct instance."""
    # Reset the global instance
    from backend.autonomy.reactor_core_watcher import _watcher_instance
    _watcher_instance = None
    
    watcher = get_reactor_core_watcher()
    assert isinstance(watcher, ReactorCoreWatcher)
    assert watcher is get_reactor_core_watcher()  # Should return same instance


def test_reactor_core_watcher_start_reactor_core_watcher():
    """Test that start_reactor_core_watcher works correctly."""
    # Reset the global instance
    from backend.autonomy.reactor_core_watcher import _watcher_instance
    _watcher_instance = None
    
    watcher = asyncio.run(start_reactor_core_watcher())
    assert isinstance(watcher, ReactorCoreWatcher)
    assert watcher is get_reactor_core_watcher()


def test_reactor_core_watcher_stop_reactor_core_watcher():
    """Test that stop_reactor_core_watcher works correctly."""
    # Reset the global instance
    from backend.autonomy.reactor_core_watcher import _watcher_instance
    _watcher_instance = None
    
    # Start and then stop
    watcher = asyncio.run(start_reactor_core_watcher())
    asyncio.run(stop_reactor_core_watcher())
    assert _watcher_instance is None