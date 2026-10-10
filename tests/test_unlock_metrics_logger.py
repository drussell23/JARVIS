# [Ouroboros] Modified by Ouroboros (op=op-01a12342-) at 2026-10-10 00:44 UTC
# Reason: `backend/voice_unlock/unlock_metrics_logger.py` has no corresponding test module. CREATE `tests/test_unlock_metrics_logg

from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.voice_unlock.unlock_metrics_logger import (
    StageMetrics,
    UnlockMetricsLogger,
    get_metrics_logger,
)


def _make_stage(
    stage_name: str = "test_stage",
    success: bool | None = True,
    duration_ms: float = 100.0,
    confidence_score: float | None = 0.8,
    **kwargs,
) -> StageMetrics:
    stage = StageMetrics(
        stage_name=stage_name,
        started_at=time.time(),
        success=success,
        duration_ms=duration_ms,
        confidence_score=confidence_score,
        **kwargs,
    )
    stage.ended_at = stage.started_at + (duration_ms / 1000.0)
    return stage


def _make_biometrics(
    speaker_confidence: float = 0.85,
    threshold: float = 0.35,
) -> dict:
    return {
        "speaker_confidence": speaker_confidence,
        "threshold": threshold,
    }


def _make_performance(
    total_duration_ms: float = 500.0,
) -> dict:
    return {
        "total_duration_ms": total_duration_ms,
    }


def _make_quality_indicators() -> dict:
    return {
        "signal_to_noise_ratio": 25.0,
        "audio_clarity": 0.9,
    }


class TestUnlockMetricsLogger:
    """Tests for the UnlockMetricsLogger core functionality."""

    @pytest.fixture
    def logger(self, tmp_path):
        """Provide a fresh UnlockMetricsLogger instance with temporary log dir."""
        return UnlockMetricsLogger(log_dir=str(tmp_path))

    @pytest.fixture
    def mock_database(self):
        """Mock the metrics database to avoid actual I/O."""
        with patch("voice_unlock.metrics_database.get_metrics_database") as mock:
            db = MagicMock()
            mock.return_value = db
            yield db

    async def test_create_stage(self, logger: UnlockMetricsLogger):
        """Test that create_stage creates a StageMetrics object with correct fields."""
        stage = logger.create_stage("audio_preparation")
        assert stage.stage_name == "audio_preparation"
        assert stage.started_at is not None
        assert stage.module_path is not None
        assert stage.function_name is not None
        assert isinstance(stage, StageMetrics)

    async def test_create_stage_with_metadata(self, logger: UnlockMetricsLogger):
        """Test that create_stage accepts and stores metadata correctly."""
        stage = logger.create_stage("transcription", model="whisper-tiny")
        assert stage.metadata["model"] == "whisper-tiny"

    async def test_stage_complete(self, logger: UnlockMetricsLogger):
        """Test that complete() sets ended_at, duration_ms, and success correctly."""
        stage = logger.create_stage("test_stage")
        await asyncio.sleep(0.01)  # Ensure some time passes
        stage.complete(True, algorithm_used="test-algo")
        assert stage.ended_at is not None
        assert stage.duration_ms is not None
        assert stage.success is True
        assert stage.algorithm_used == "test-algo"



    async def test_log_unlock_attempt_empty_stages(self, logger: UnlockMetricsLogger, mock_database):
        """Test logging with empty stages list."""
        await logger.log_unlock_attempt(
            success=True,
            speaker_name="test_user",
            transcribed_text="unlock command",
            stages=[],
            biometrics=_make_biometrics(),
            performance=_make_performance(),
            quality_indicators=_make_quality_indicators(),
        )

        # Verify database was called even with empty stages
        mock_database.store_unlock_attempt.assert_called()

    async def test_get_metrics_logger_singleton(self):
        """Test that get_metrics_logger returns the same instance on multiple calls."""
        logger1 = get_metrics_logger()
        logger2 = get_metrics_logger()
        assert logger1 is logger2

    async def test_get_today_stats(self, logger: UnlockMetricsLogger):
        """Test that get_today_stats returns expected structure."""
        stats = await logger.get_today_stats()
        assert isinstance(stats, dict)
        assert "date" in stats
        assert "total_attempts" in stats
        assert "successful" in stats
        assert "failed" in stats
        assert "success_rate" in stats
        assert "avg_confidence" in stats
        assert "avg_latency" in stats
        assert "confidence_trend" in stats


    async def test_calculate_confidence_trend_empty_history(self, logger: UnlockMetricsLogger):
        """Test confidence trend calculation with empty history."""
        result = logger._calculate_confidence_trend([], 0.8)
        assert isinstance(result, dict)
        assert "avg_last_10" in result
        assert "avg_last_30" in result
        assert "direction" in result
        assert "volatility" in result
        assert "best" in result
        assert "worst" in result
        assert "percentile" in result

    async def test_calculate_confidence_trend_with_history(self, logger: UnlockMetricsLogger):
        """Test confidence trend calculation with existing history."""
        history = [0.5, 0.6, 0.7, 0.8]
        result = logger._calculate_confidence_trend(history, 0.9)
        assert isinstance(result, dict)
        assert "avg_last_10" in result
        assert "avg_last_30" in result
        assert "direction" in result
        assert "volatility" in result
        assert "best" in result
        assert "worst" in result
        assert "percentile" in result

    async def test_get_confidence_history(self, logger: UnlockMetricsLogger):
        """Test confidence history retrieval."""
        # Mock entries to return some data
        mock_entries = [
            {
                "speaker_name": "test_user",
                "biometrics": {"speaker_confidence": 0.8}
            },
            {
                "speaker_name": "other_user",
                "biometrics": {"speaker_confidence": 0.7}
            }
        ]
        with patch.object(logger, "_load_entries", return_value=mock_entries):
            history = await logger._get_confidence_history("test_user")
            assert len(history) == 1
            assert history[0] == 0.8

    async def test_get_confidence_history_no_matches(self, logger: UnlockMetricsLogger):
        """Test confidence history retrieval with no matching speaker."""
        mock_entries = [
            {
                "speaker_name": "other_user",
                "biometrics": {"speaker_confidence": 0.7}
            }
        ]
        with patch.object(logger, "_load_entries", return_value=mock_entries):
            history = await logger._get_confidence_history("test_user")
            assert len(history) == 0

    async def test_get_daily_log_file(self, logger: UnlockMetricsLogger):
        """Test that _get_daily_log_file returns correct path with today's date."""
        log_file = logger._get_daily_log_file()
        assert log_file.name.startswith("unlock_metrics_")
        assert log_file.name.endswith(".json")
        # Check that it contains today's date
        today = datetime.now().strftime("%Y-%m-%d")
        assert today in log_file.name

    async def test_create_logger_with_custom_dir(self, tmp_path):
        """Test logger initialization with custom log directory."""
        custom_dir = tmp_path / "custom_logs"
        logger = UnlockMetricsLogger(log_dir=str(custom_dir))
        assert logger.log_dir == custom_dir
        assert custom_dir.exists()

    async def test_create_logger_with_none_dir(self, tmp_path):
        """Test logger initialization with None log directory (default behavior)."""
        # Mock home directory to avoid actual filesystem access
        with patch("pathlib.Path.home", return_value=tmp_path):
            logger = UnlockMetricsLogger(log_dir=None)
            expected_path = tmp_path / ".jarvis" / "logs" / "unlock_metrics"
            assert logger.log_dir == expected_path
            assert expected_path.exists()
