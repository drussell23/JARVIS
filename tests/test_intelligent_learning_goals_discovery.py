# [Ouroboros] Modified by Ouroboros (op=op-01a10e6c-) at 2026-10-05 23:39 UTC
# Reason: `backend/autonomy/intelligent_learning_goals_discovery.py` has no corresponding test module. CREATE `tests/test_intellig

from __future__ import annotations
import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.autonomy.intelligent_learning_goals_discovery import (
    GoalSource,
    GoalPriority,
    GoalStatus,
    LearningGoal,
    GoalsDiscoveryConfig,
    GoalSourceAnalyzer,
    FailedQueriesAnalyzer,
    ErrorLogsAnalyzer,
    TrendingTopicsAnalyzer,
    PrimeFeedbackAnalyzer,
    IntelligentLearningGoalsDiscovery,
    get_goals_discovery_async,
    get_goals_discovery,
)


class TestIntelligentLearningGoalsDiscovery:
    def test_import_smoke(self):
        """Verify the module can be imported without errors."""
        assert True  # If we reach here, import succeeded

    def test_learning_goal_to_dict(self):
        """Test LearningGoal.to_dict() method."""
        goal = LearningGoal(
            id="test-id",
            topic="Test Topic",
            description="Test Description",
            source=GoalSource.FAILED_QUERIES,
            priority=GoalPriority.HIGH.value,
            status=GoalStatus.PENDING,
            keywords=["kw1", "kw2"],
            urls=["http://example.com"],
            related_queries=["query1"],
            related_errors=["error1"],
            confidence=0.8,
            created_at=datetime(2023, 1, 1),
            updated_at=datetime(2023, 1, 1),
            completed_at=datetime(2023, 1, 2),
            metadata={"test": "data"}
        )
        
        result = goal.to_dict()
        assert result["id"] == "test-id"
        assert result["topic"] == "Test Topic"
        assert result["description"] == "Test Description"
        assert result["source"] == "failed_queries"
        assert result["priority"] == 8
        assert result["status"] == "pending"
        assert result["keywords"] == ["kw1", "kw2"]
        assert result["urls"] == ["http://example.com"]
        assert result["related_queries"] == ["query1"]
        assert result["related_errors"] == ["error1"]
        assert result["confidence"] == 0.8
        assert result["created_at"] == "2023-01-01T00:00:00"
        assert result["updated_at"] == "2023-01-01T00:00:00"
        assert result["completed_at"] == "2023-01-02T00:00:00"
        assert result["metadata"] == {"test": "data"}

    def test_goals_discovery_config_defaults(self):
        """Test GoalsDiscoveryConfig default values."""
        config = GoalsDiscoveryConfig()
        
        assert config.jarvis_repo == Path(os.getenv("JARVIS_AI_AGENT_PATH", Path.home() / "Documents" / "repos" / "JARVIS-AI-Agent"))
        assert config.jarvis_prime_repo == Path(os.getenv("JARVIS_PRIME_PATH", Path.home() / "Documents" / "repos" / "jarvis-prime"))
        assert config.reactor_core_repo == Path(os.getenv("REACTOR_CORE_PATH", Path.home() / "Documents" / "repos" / "reactor-core"))
        assert config.db_path == Path(os.getenv("JARVIS_LEARNING_GOALS_DB", Path.home() / ".jarvis" / "learning_goals.db"))
        assert config.query_lookback_hours == 72
        assert config.error_lookback_hours == 168
        assert config.min_failure_count == 3
        assert config.max_pending_goals == 50
        assert config.trending_enabled is True
        assert config.trending_refresh_hours == 24
        assert config.focus_categories == ["AI", "ML", "LLM", "Python", "macOS", "automation", "voice", "vision"]

    @pytest.mark.asyncio
    async def test_failed_queries_analyzer_no_logs(self):
        """Test FailedQueriesAnalyzer with no query logs."""
        config = GoalsDiscoveryConfig()
        analyzer = FailedQueriesAnalyzer(config)
        
        with patch.object(analyzer, '_get_query_logs', return_value=[]):
            goals = await analyzer.analyze()
            assert goals == []

    @pytest.mark.asyncio
    async def test_error_logs_analyzer_no_errors(self):
        """Test ErrorLogsAnalyzer with no error logs."""
        config = GoalsDiscoveryConfig()
        analyzer = ErrorLogsAnalyzer(config)
        
        with patch.object(analyzer, '_parse_log_file', return_value=[]):
            goals = await analyzer.analyze()
            assert goals == []

    @pytest.mark.asyncio
    async def test_trending_topics_analyzer_disabled(self):
        """Test TrendingTopicsAnalyzer when trending is disabled."""
        config = GoalsDiscoveryConfig(trending_enabled=False)
        analyzer = TrendingTopicsAnalyzer(config)
        
        goals = await analyzer.analyze()
        assert goals == []

    @pytest.mark.asyncio
    async def test_prime_feedback_analyzer_no_metrics(self):
        """Test PrimeFeedbackAnalyzer with no metrics."""
        config = GoalsDiscoveryConfig()
        analyzer = PrimeFeedbackAnalyzer(config)
        
        with patch.object(analyzer, '_get_prime_metrics', return_value={}):
            goals = await analyzer.analyze()
            assert goals == []

    @pytest.mark.asyncio
    async def test_intelligent_learning_goals_discovery_initialize(self):
        """Test IntelligentLearningGoalsDiscovery initialization."""
        config = GoalsDiscoveryConfig()
        discovery = IntelligentLearningGoalsDiscovery(config)
        
        await discovery.initialize()
        assert discovery._initialized is True

    @pytest.mark.asyncio
    async def test_intelligent_learning_goals_discovery_get_stats(self):
        """Test IntelligentLearningGoalsDiscovery get_stats method."""
        config = GoalsDiscoveryConfig()
        discovery = IntelligentLearningGoalsDiscovery(config)
        
        stats = discovery.get_stats()
        assert 'total_goals' in stats
        assert 'by_status' in stats
        assert 'by_source' in stats
        assert 'avg_priority' in stats

    @pytest.mark.asyncio
    async def test_get_goals_discovery_async(self):
        """Test get_goals_discovery_async function."""
        discovery = await get_goals_discovery_async()
        assert isinstance(discovery, IntelligentLearningGoalsDiscovery)

    @pytest.mark.asyncio
    async def test_get_goals_discovery(self):
        """Test get_goals_discovery function."""
        discovery = get_goals_discovery()
        assert isinstance(discovery, IntelligentLearningGoalsDiscovery)
