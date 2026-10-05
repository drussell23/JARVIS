# [Ouroboros] Modified by Ouroboros (op=op-01a10974-) at 2026-10-05 00:31 UTC
# Reason: `backend/autonomy/unified_data_flywheel.py` has no corresponding test module. CREATE `tests/test_unified_data_flywheel.p

from __future__ import annotations
import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
from backend.autonomy.unified_data_flywheel import (
    FlywheelStage,
    DataSourceType,
    FlywheelConfig,
    FlywheelProgress,
    FlywheelResult,
    UnifiedDataFlywheel,
    get_data_flywheel,
    run_flywheel_cycle,
    get_flywheel_status,
    run_schema_migrations_sync,
    run_schema_migrations_async,
)

class TestImportSmoke:
    """Test that the module imports correctly and exposes expected symbols."""

    def test_import_module(self):
        """Verify the module can be imported without errors."""
        import backend.autonomy.unified_data_flywheel as flywheel_module
        assert hasattr(flywheel_module, 'UnifiedDataFlywheel')
        assert hasattr(flywheel_module, 'FlywheelConfig')
        assert hasattr(flywheel_module, 'FlywheelProgress')
        assert hasattr(flywheel_module, 'FlywheelResult')
        assert hasattr(flywheel_module, 'FlywheelStage')
        assert hasattr(flywheel_module, 'DataSourceType')

    def test_import_functions(self):
        """Verify key functions are importable."""
        import backend.autonomy.unified_data_flywheel as flywheel_module
        assert callable(flywheel_module.get_data_flywheel)
        assert callable(flywheel_module.run_flywheel_cycle)
        assert callable(flywheel_module.get_flywheel_status)
        assert callable(flywheel_module.run_schema_migrations_sync)
        assert callable(flywheel_module.run_schema_migrations_async)


class TestFlywheelConfig:
    """Test FlywheelConfig initialization and defaults."""

    def test_config_defaults(self):
        """Verify default configuration values."""
        config = FlywheelConfig()
        assert isinstance(config.jarvis_repo, Path)
        assert isinstance(config.jarvis_prime_repo, Path)
        assert isinstance(config.reactor_core_repo, Path)
        assert isinstance(config.training_db_path, Path)
        assert config.training_db_enabled is True
        assert config.experience_lookback_hours == 24
        assert config.min_experiences_for_training == 100
        assert config.min_web_examples_for_training == 50
        assert config.auto_train_enabled is True
        assert config.training_cooldown_hours == 24
        assert isinstance(config.scout_topics, list)
        assert len(config.scout_topics) > 0
        assert config.scout_max_pages_per_topic == 10
        assert config.base_model == "meta-llama/Llama-3.2-3B"
        assert config.output_name == "jarvis-prime"
        assert config.quantization == "Q4_K_M"
        assert isinstance(config.gcs_bucket, str)

    def test_config_custom_values(self):
        """Verify custom configuration values are respected."""
        custom_config = FlywheelConfig(
            jarvis_repo=Path("/custom/jarvis"),
            jarvis_prime_repo=Path("/custom/prime"),
            reactor_core_repo=Path("/custom/reactor"),
            training_db_enabled=False,
            training_db_path=Path("/custom/db"),
            experience_lookback_hours=48,
            min_experiences_for_training=200,
            auto_train_enabled=False,
            scout_max_pages_per_topic=5,
            base_model="custom/model",
            output_name="custom-output",
            quantization="Q5_K_M",
            gcs_bucket="gs://custom-bucket"
        )
        assert custom_config.jarvis_repo == Path("/custom/jarvis")
        assert custom_config.jarvis_prime_repo == Path("/custom/prime")
        assert custom_config.reactor_core_repo == Path("/custom/reactor")
        assert custom_config.training_db_enabled is False
        assert custom_config.training_db_path == Path("/custom/db")
        assert custom_config.experience_lookback_hours == 48
        assert custom_config.min_experiences_for_training == 200
        assert custom_config.auto_train_enabled is False
        assert custom_config.scout_max_pages_per_topic == 5
        assert custom_config.base_model == "custom/model"
        assert custom_config.output_name == "custom-output"
        assert custom_config.quantization == "Q5_K_M"
        assert custom_config.gcs_bucket == "gs://custom-bucket"


class TestFlywheelProgress:
    """Test FlywheelProgress functionality."""

    def test_progress_duration_seconds(self):
        """Verify duration calculation works correctly."""
        progress = FlywheelProgress()
        assert progress.duration_seconds == 0.0

        # Set start time
        import datetime
        now = datetime.datetime.now()
        progress.started_at = now
        assert progress.duration_seconds >= 0.0

    def test_progress_to_dict(self):
        """Verify to_dict method works correctly."""
        progress = FlywheelProgress(
            stage=FlywheelStage.COLLECTING_EXPERIENCES,
            experiences_collected=10,
            web_pages_scraped=5,
            dataset_examples=20,
            training_epochs=3,
            current_loss=0.5,
            best_loss=0.3,
            model_size_mb=100.0,
            deployed_local=True,
            errors=["test error"]
        )
        result = progress.to_dict()
        assert isinstance(result, dict)
        assert result["stage"] == "collecting_experiences"
        assert result["experiences_collected"] == 10
        assert result["web_pages_scraped"] == 5
        assert result["dataset_examples"] == 20
        assert result["training_epochs"] == 3
        assert result["current_loss"] == 0.5
        assert result["best_loss"] == 0.3
        assert result["model_size_mb"] == 100.0
        assert result["deployed_local"] is True
        assert "test error" in result["errors"]


class TestUnifiedDataFlywheel:
    """Test main UnifiedDataFlywheel class functionality."""

    @pytest.fixture
    def mock_config(self):
        return FlywheelConfig(
            jarvis_repo=Path("/tmp/jarvis"),
            jarvis_prime_repo=Path("/tmp/prime"),
            reactor_core_repo=Path("/tmp/reactor"),
            training_db_path=Path("/tmp/test.db"),
            training_db_enabled=True,
        )

    @pytest.fixture
    def flywheel(self, mock_config):
        return UnifiedDataFlywheel(config=mock_config)

    def test_flywheel_initialization(self, flywheel, mock_config):
        """Verify flywheel initializes correctly."""
        assert flywheel.config == mock_config
        assert flywheel._training_db_conn is None
        assert flywheel._running is False
        assert flywheel._progress.stage == FlywheelStage.IDLE

    def test_get_data_flywheel_singleton(self):
        """Verify get_data_flywheel returns the same instance."""
        flywheel1 = get_data_flywheel()
        flywheel2 = get_data_flywheel()
        assert flywheel1 is flywheel2

    def test_is_running(self, flywheel):
        """Verify is_running method works correctly."""
        assert flywheel.is_running is False

    def test_progress(self, flywheel):
        """Verify progress method works correctly."""
        progress = flywheel.progress
        assert isinstance(progress, FlywheelProgress)
        assert progress.stage == FlywheelStage.IDLE

    def test_register_progress_callback(self, flywheel):
        """Verify register_progress_callback works correctly."""
        callback = MagicMock()
        flywheel.register_progress_callback(callback)
        # Just verify no exception is raised
        assert True

    @pytest.mark.asyncio
    async def test_run_schema_migrations_sync(self, mock_config):
        """Test sync schema migrations function."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            config = FlywheelConfig(training_db_path=db_path, training_db_enabled=True)
            flywheel = UnifiedDataFlywheel(config=config)
            
            # Mock the database connection
            with patch('sqlite3.connect') as mock_connect:
                mock_conn = MagicMock()
                mock_connect.return_value = mock_conn
                
                result = run_schema_migrations_sync(mock_conn, mock_conn.cursor())
                assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_run_schema_migrations_async(self, mock_config):
        """Test async schema migrations function."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            config = FlywheelConfig(training_db_path=db_path, training_db_enabled=True)
            flywheel = UnifiedDataFlywheel(config=config)
            
            # Mock the database connection
            with patch('aiosqlite.connect') as mock_connect:
                mock_conn = AsyncMock()
                mock_connect.return_value.__aenter__.return_value = mock_conn
                
                result = await run_schema_migrations_async(mock_conn)
                assert isinstance(result, bool)

class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_flywheel_status_functions(self):
        """Test get_flywheel_status function."""
        # Mock the flywheel to avoid actual initialization
        with patch('backend.autonomy.unified_data_flywheel.get_data_flywheel') as mock_get_flywheel:
            mock_flywheel = MagicMock()
            mock_flywheel.is_running = False
            mock_flywheel.progress = FlywheelProgress()
            mock_get_flywheel.return_value = mock_flywheel
            
            status = await get_flywheel_status()
            assert isinstance(status, dict)
            assert 'progress' in status
            assert 'running' in status
            # Verify the stage is accessible through progress
            assert 'stage' in status['progress']

    @pytest.mark.asyncio
    async def test_run_flywheel_cycle(self):
        """Test run_flywheel_cycle function."""
        # This should not raise an exception
        result = await run_flywheel_cycle()
        assert isinstance(result, FlywheelResult)
