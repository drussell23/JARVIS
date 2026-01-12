"""
Intelligent Learning Goals Discovery System
============================================

Autonomously discovers learning goals for JARVIS by analyzing:
- User queries that failed or had low confidence
- Error logs revealing knowledge gaps
- Trending topics in tech/AI space
- Model performance metrics from JARVIS Prime
- Knowledge base coverage analysis

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │              INTELLIGENT LEARNING GOALS DISCOVERY                        │
    │                                                                          │
    │  ┌────────────────────────────────────────────────────────────────────┐ │
    │  │                      DATA SOURCES                                   │ │
    │  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌────────────┐│ │
    │  │  │ Query Logs   │ │ Error Logs   │ │ Trending API │ │ Prime      ││ │
    │  │  │ (JARVIS)     │ │ (Gaps)       │ │ (External)   │ │ Feedback   ││ │
    │  │  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────┬─────┘│ │
    │  └─────────┼────────────────┼────────────────┼────────────────┼──────┘ │
    │            └────────────────┴────────────────┴────────────────┘        │
    │                                     │                                   │
    │                                     ▼                                   │
    │  ┌────────────────────────────────────────────────────────────────────┐ │
    │  │                    GOAL PRIORITIZATION ENGINE                       │ │
    │  │  • Relevance scoring                                               │ │
    │  │  • Urgency detection                                               │ │
    │  │  • Duplicate detection                                             │ │
    │  │  • Cross-repo coordination                                         │ │
    │  └────────────────────────────────────────────────────────────────────┘ │
    │                                     │                                   │
    │                                     ▼                                   │
    │  ┌────────────────────────────────────────────────────────────────────┐ │
    │  │                    LEARNING GOALS QUEUE                             │ │
    │  │  [priority=10] [priority=8] [priority=6] [priority=4]              │ │
    │  └────────────────────────────────────────────────────────────────────┘ │
    └─────────────────────────────────────────────────────────────────────────┘

Integration Points:
- JARVIS-AI-Agent: Query logs, error analysis, ChromaDB memory
- JARVIS-Prime: Model performance metrics, inference feedback
- Reactor-Core: Scout system, web documentation synthesis

Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# Enums and Data Models
# =============================================================================

class GoalSource(Enum):
    """Sources for learning goals."""
    FAILED_QUERIES = "failed_queries"
    ERROR_LOGS = "error_logs"
    TRENDING_TOPICS = "trending_topics"
    PRIME_FEEDBACK = "prime_feedback"
    KNOWLEDGE_GAPS = "knowledge_gaps"
    USER_REQUEST = "user_request"
    SCHEDULED_REFRESH = "scheduled_refresh"


class GoalPriority(Enum):
    """Priority levels for learning goals."""
    CRITICAL = 10  # User-blocking issues
    HIGH = 8       # Frequent failures
    MEDIUM = 5     # Nice to have
    LOW = 3        # Background learning
    MINIMAL = 1    # Optional enrichment


class GoalStatus(Enum):
    """Status of a learning goal."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class LearningGoal:
    """A discovered learning goal."""
    id: str
    topic: str
    description: str
    source: GoalSource
    priority: int = GoalPriority.MEDIUM.value
    status: GoalStatus = GoalStatus.PENDING
    keywords: List[str] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)
    related_queries: List[str] = field(default_factory=list)
    related_errors: List[str] = field(default_factory=list)
    confidence: float = 0.5
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "description": self.description,
            "source": self.source.value,
            "priority": self.priority,
            "status": self.status.value,
            "keywords": self.keywords,
            "urls": self.urls,
            "related_queries": self.related_queries,
            "related_errors": self.related_errors,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "metadata": self.metadata,
        }


@dataclass
class GoalsDiscoveryConfig:
    """Configuration for the goals discovery system."""
    # Repository paths
    jarvis_repo: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_AI_AGENT_PATH",
            Path.home() / "Documents" / "repos" / "JARVIS-AI-Agent"
        ))
    )
    jarvis_prime_repo: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_PRIME_PATH",
            Path.home() / "Documents" / "repos" / "jarvis-prime"
        ))
    )
    reactor_core_repo: Path = field(
        default_factory=lambda: Path(os.getenv(
            "REACTOR_CORE_PATH",
            Path.home() / "Documents" / "repos" / "reactor-core"
        ))
    )

    # Database path
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_LEARNING_GOALS_DB",
            Path.home() / ".jarvis" / "learning_goals.db"
        ))
    )

    # Discovery settings
    query_lookback_hours: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_QUERY_LOOKBACK_HOURS", "72"))
    )
    error_lookback_hours: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_ERROR_LOOKBACK_HOURS", "168"))
    )
    min_failure_count: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MIN_FAILURE_COUNT", "3"))
    )
    max_pending_goals: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MAX_PENDING_GOALS", "50"))
    )

    # Trending topics
    trending_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TRENDING_ENABLED", "true").lower() == "true"
    )
    trending_refresh_hours: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_TRENDING_REFRESH_HOURS", "24"))
    )

    # Topic categories for focused learning
    focus_categories: List[str] = field(
        default_factory=lambda: os.getenv(
            "JARVIS_FOCUS_CATEGORIES",
            "AI,ML,LLM,Python,macOS,automation,voice,vision"
        ).split(",")
    )


# =============================================================================
# Goal Source Analyzers (Pluggable Architecture)
# =============================================================================

class GoalSourceAnalyzer(ABC):
    """Base class for goal source analyzers."""

    @abstractmethod
    async def analyze(self) -> List[LearningGoal]:
        """Analyze the source and return discovered goals."""
        pass

    @abstractmethod
    def get_source_type(self) -> GoalSource:
        """Return the source type for this analyzer."""
        pass


class FailedQueriesAnalyzer(GoalSourceAnalyzer):
    """
    Analyzes failed JARVIS queries to discover knowledge gaps.

    Connects to JARVIS ChromaDB and query logs to find:
    - Queries with low confidence responses
    - Queries that resulted in errors
    - Repeated similar queries (user frustration signals)
    """

    def __init__(self, config: GoalsDiscoveryConfig):
        self.config = config
        self._chromadb_client = None

    def get_source_type(self) -> GoalSource:
        return GoalSource.FAILED_QUERIES

    async def analyze(self) -> List[LearningGoal]:
        goals = []

        try:
            # Try to connect to JARVIS query logs
            query_logs = await self._get_query_logs()

            # Group queries by topic/intent
            topic_clusters = self._cluster_queries(query_logs)

            for topic, queries in topic_clusters.items():
                failure_count = sum(1 for q in queries if q.get("failed", False))
                avg_confidence = sum(q.get("confidence", 0) for q in queries) / len(queries) if queries else 0

                if failure_count >= self.config.min_failure_count or avg_confidence < 0.5:
                    priority = self._calculate_priority(failure_count, avg_confidence, len(queries))

                    goal = LearningGoal(
                        id=f"query_{topic.lower().replace(' ', '_')}_{int(time.time())}",
                        topic=topic,
                        description=f"Improve JARVIS responses for '{topic}' queries (failure rate: {failure_count}/{len(queries)})",
                        source=GoalSource.FAILED_QUERIES,
                        priority=priority,
                        keywords=self._extract_keywords(queries),
                        related_queries=[q.get("text", "") for q in queries[:5]],
                        confidence=1.0 - avg_confidence,
                        metadata={
                            "failure_count": failure_count,
                            "avg_confidence": avg_confidence,
                            "query_count": len(queries),
                        }
                    )
                    goals.append(goal)

            logger.info(f"[GoalsDiscovery] FailedQueries: Found {len(goals)} learning goals")

        except Exception as e:
            logger.warning(f"[GoalsDiscovery] FailedQueries analysis failed: {e}")

        return goals

    async def _get_query_logs(self) -> List[Dict[str, Any]]:
        """Get query logs from JARVIS database."""
        logs = []

        try:
            # Try SQLite logs first
            log_db_path = self.config.jarvis_repo / "data" / "training_db" / "jarvis_training.db"
            if log_db_path.exists():
                conn = sqlite3.connect(str(log_db_path))
                cursor = conn.cursor()

                cutoff = datetime.now() - timedelta(hours=self.config.query_lookback_hours)

                # Check if query_logs table exists
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='query_logs'")
                if cursor.fetchone():
                    cursor.execute("""
                        SELECT query_text, confidence, success, created_at
                        FROM query_logs
                        WHERE created_at > ?
                        ORDER BY created_at DESC
                        LIMIT 1000
                    """, (cutoff.isoformat(),))

                    for row in cursor.fetchall():
                        logs.append({
                            "text": row[0],
                            "confidence": row[1] or 0.0,
                            "failed": not row[2],
                            "timestamp": row[3],
                        })

                # Also check experiences table
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='experiences'")
                if cursor.fetchone():
                    cursor.execute("""
                        SELECT user_input, assistant_response, created_at
                        FROM experiences
                        WHERE created_at > ?
                        ORDER BY created_at DESC
                        LIMIT 500
                    """, (cutoff.isoformat(),))

                    for row in cursor.fetchall():
                        # Infer failure from empty or error responses
                        response = row[1] or ""
                        is_failed = (
                            "error" in response.lower() or
                            "sorry" in response.lower() or
                            "don't know" in response.lower() or
                            len(response) < 20
                        )

                        logs.append({
                            "text": row[0],
                            "confidence": 0.3 if is_failed else 0.7,
                            "failed": is_failed,
                            "timestamp": row[2],
                        })

                conn.close()

        except Exception as e:
            logger.debug(f"[GoalsDiscovery] Query logs retrieval failed: {e}")

        return logs

    def _cluster_queries(self, queries: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Cluster queries by topic/intent."""
        clusters: Dict[str, List[Dict[str, Any]]] = {}

        # Simple keyword-based clustering
        for query in queries:
            text = query.get("text", "").lower()
            topic = self._extract_topic(text)

            if topic:
                if topic not in clusters:
                    clusters[topic] = []
                clusters[topic].append(query)

        return clusters

    def _extract_topic(self, text: str) -> Optional[str]:
        """Extract main topic from query text."""
        # Common patterns for topic extraction
        patterns = [
            r"(?:how to|what is|explain|help with|show me)\s+(.+?)(?:\?|$)",
            r"(?:search for|find|look up)\s+(.+?)(?:\?|$)",
            r"(?:create|make|build|write)\s+(?:a|an)?\s*(.+?)(?:\?|$)",
        ]

        text = text.strip()

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                topic = match.group(1).strip()
                # Clean up the topic
                topic = re.sub(r'\b(the|a|an|my|your)\b', '', topic).strip()
                if len(topic) > 3:
                    return topic.title()

        # Fallback: use first 5 significant words
        words = [w for w in text.split() if len(w) > 3][:5]
        if words:
            return " ".join(words).title()

        return None

    def _extract_keywords(self, queries: List[Dict[str, Any]]) -> List[str]:
        """Extract keywords from queries."""
        keywords = set()

        for query in queries:
            text = query.get("text", "").lower()
            # Extract words longer than 3 characters
            words = re.findall(r'\b\w{4,}\b', text)
            keywords.update(words[:5])

        return list(keywords)[:10]

    def _calculate_priority(self, failure_count: int, avg_confidence: float, total_queries: int) -> int:
        """Calculate priority based on failure metrics."""
        if failure_count >= 10 or avg_confidence < 0.3:
            return GoalPriority.CRITICAL.value
        elif failure_count >= 5 or avg_confidence < 0.5:
            return GoalPriority.HIGH.value
        elif failure_count >= 3:
            return GoalPriority.MEDIUM.value
        else:
            return GoalPriority.LOW.value


class ErrorLogsAnalyzer(GoalSourceAnalyzer):
    """
    Analyzes JARVIS error logs to discover knowledge gaps.

    Looks for:
    - Import errors (missing dependencies)
    - API errors (integration issues)
    - Repeated exceptions (systematic problems)
    """

    def __init__(self, config: GoalsDiscoveryConfig):
        self.config = config

    def get_source_type(self) -> GoalSource:
        return GoalSource.ERROR_LOGS

    async def analyze(self) -> List[LearningGoal]:
        goals = []

        # Safety limits
        MAX_LOG_FILES = 20  # Max log files to process
        TOTAL_ANALYZE_TIMEOUT = 30.0  # Total timeout for all analysis

        try:
            async def _do_analysis():
                # Find log files
                log_dirs = [
                    self.config.jarvis_repo / "backend" / "logs",
                    self.config.jarvis_repo / "logs",
                    Path.home() / ".jarvis" / "logs",
                ]

                error_patterns: Dict[str, List[Dict[str, Any]]] = {}
                files_processed = 0

                for log_dir in log_dirs:
                    if log_dir.exists():
                        # Get recent log files sorted by modification time
                        log_files = sorted(
                            log_dir.glob("*.log"),
                            key=lambda p: p.stat().st_mtime if p.exists() else 0,
                            reverse=True  # Most recent first
                        )[:MAX_LOG_FILES - files_processed]

                        for log_file in log_files:
                            if files_processed >= MAX_LOG_FILES:
                                break
                            files_processed += 1

                            errors = await self._parse_log_file(log_file)
                            for error in errors:
                                pattern = self._extract_error_pattern(error)
                                if pattern:
                                    if pattern not in error_patterns:
                                        error_patterns[pattern] = []
                                    error_patterns[pattern].append(error)
                return error_patterns

            # Run with overall timeout
            error_patterns = await asyncio.wait_for(_do_analysis(), timeout=TOTAL_ANALYZE_TIMEOUT)

            # Create goals from error patterns
            for pattern, errors in error_patterns.items():
                if len(errors) >= self.config.min_failure_count:
                    topic = self._pattern_to_topic(pattern)
                    priority = self._calculate_priority(len(errors))

                    goal = LearningGoal(
                        id=f"error_{hash(pattern) % 10000}_{int(time.time())}",
                        topic=topic,
                        description=f"Fix recurring error: {pattern[:100]}",
                        source=GoalSource.ERROR_LOGS,
                        priority=priority,
                        keywords=self._extract_keywords_from_errors(errors),
                        related_errors=[e.get("message", "")[:200] for e in errors[:5]],
                        confidence=min(1.0, len(errors) / 10),
                        metadata={
                            "error_count": len(errors),
                            "pattern": pattern,
                            "first_seen": errors[0].get("timestamp", ""),
                            "last_seen": errors[-1].get("timestamp", ""),
                        }
                    )
                    goals.append(goal)

            logger.info(f"[GoalsDiscovery] ErrorLogs: Found {len(goals)} learning goals")

        except Exception as e:
            logger.warning(f"[GoalsDiscovery] ErrorLogs analysis failed: {e}")

        return goals

    async def _parse_log_file(self, log_file: Path) -> List[Dict[str, Any]]:
        """Parse a log file for errors with robust timeout and size limits."""
        errors = []

        # Constants for safety limits
        MAX_FILE_SIZE_MB = 50  # Skip files larger than 50MB
        MAX_LINES_PER_FILE = 10000  # Max lines to read
        MAX_ERRORS_PER_FILE = 100  # Max errors to collect per file
        PARSE_TIMEOUT_SECS = 5.0  # Timeout per file

        try:
            # Safety: Skip files that are too large
            file_size_mb = log_file.stat().st_size / (1024 * 1024)
            if file_size_mb > MAX_FILE_SIZE_MB:
                logger.debug(f"[GoalsDiscovery] Skipping large log file: {log_file} ({file_size_mb:.1f}MB)")
                return errors

            cutoff = datetime.now() - timedelta(hours=self.config.error_lookback_hours)

            # Run file parsing in executor with timeout to prevent blocking
            def _sync_parse():
                parsed_errors = []
                lines_read = 0
                try:
                    with open(log_file, 'r', errors='ignore') as f:
                        for line in f:
                            lines_read += 1
                            if lines_read > MAX_LINES_PER_FILE:
                                break
                            if len(parsed_errors) >= MAX_ERRORS_PER_FILE:
                                break

                            if any(level in line for level in ["ERROR", "CRITICAL", "EXCEPTION"]):
                                # Try to parse timestamp
                                timestamp_match = re.search(
                                    r'(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2})',
                                    line
                                )

                                if timestamp_match:
                                    try:
                                        ts = datetime.fromisoformat(
                                            timestamp_match.group(1).replace(' ', 'T')
                                        )
                                        if ts < cutoff:
                                            continue
                                    except ValueError:
                                        pass

                                parsed_errors.append({
                                    "message": line.strip()[:500],  # Limit message length
                                    "file": str(log_file),
                                    "timestamp": timestamp_match.group(1) if timestamp_match else "",
                                })
                except Exception as e:
                    logger.debug(f"[GoalsDiscovery] Error parsing {log_file}: {e}")
                return parsed_errors

            # Execute with timeout
            loop = asyncio.get_event_loop()
            errors = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_parse),
                timeout=PARSE_TIMEOUT_SECS
            )

        except asyncio.TimeoutError:
            logger.debug(f"[GoalsDiscovery] Timeout parsing {log_file}")
        except Exception as e:
            logger.debug(f"[GoalsDiscovery] Failed to parse {log_file}: {e}")

        return errors

    def _extract_error_pattern(self, error: Dict[str, Any]) -> Optional[str]:
        """Extract a normalizable pattern from an error."""
        message = error.get("message", "")

        # Common error patterns
        patterns = [
            (r"ImportError: No module named '([^']+)'", r"ImportError: \1"),
            (r"ModuleNotFoundError: No module named '([^']+)'", r"ModuleNotFound: \1"),
            (r"AttributeError: '([^']+)' object has no attribute '([^']+)'", r"AttributeError: \1.\2"),
            (r"KeyError: '([^']+)'", r"KeyError: \1"),
            (r"TypeError: (.+?) expected", r"TypeError: \1"),
            (r"ValueError: (.+)", r"ValueError"),
            (r"TimeoutError", r"TimeoutError"),
            (r"ConnectionError", r"ConnectionError"),
        ]

        for pattern, replacement in patterns:
            match = re.search(pattern, message)
            if match:
                return re.sub(pattern, replacement, message)

        # Generic pattern extraction
        if "Error" in message or "Exception" in message:
            # Extract the error type
            error_match = re.search(r'\b(\w+(?:Error|Exception))\b', message)
            if error_match:
                return error_match.group(1)

        return None

    def _pattern_to_topic(self, pattern: str) -> str:
        """Convert an error pattern to a learning topic."""
        if "ImportError" in pattern or "ModuleNotFound" in pattern:
            module = re.search(r': (\w+)', pattern)
            if module:
                return f"Install/Configure {module.group(1)}"
            return "Python Dependencies"

        if "AttributeError" in pattern:
            return "Python Object Attributes"

        if "TimeoutError" in pattern:
            return "Async/Timeout Handling"

        if "ConnectionError" in pattern:
            return "Network/API Connections"

        return f"Error Handling: {pattern[:50]}"

    def _extract_keywords_from_errors(self, errors: List[Dict[str, Any]]) -> List[str]:
        """Extract keywords from error messages."""
        keywords = set()

        for error in errors:
            message = error.get("message", "").lower()
            # Extract module names, class names, method names
            words = re.findall(r'\b[a-z][a-z_]*[a-z]\b', message)
            keywords.update(w for w in words if len(w) > 3)

        return list(keywords)[:10]

    def _calculate_priority(self, error_count: int) -> int:
        """Calculate priority based on error frequency."""
        if error_count >= 20:
            return GoalPriority.CRITICAL.value
        elif error_count >= 10:
            return GoalPriority.HIGH.value
        elif error_count >= 5:
            return GoalPriority.MEDIUM.value
        else:
            return GoalPriority.LOW.value


class TrendingTopicsAnalyzer(GoalSourceAnalyzer):
    """
    Analyzes trending topics in tech/AI space for proactive learning.

    Sources:
    - GitHub trending repositories
    - HackerNews top stories
    - Tech RSS feeds
    """

    def __init__(self, config: GoalsDiscoveryConfig):
        self.config = config
        self._last_refresh: Optional[datetime] = None
        self._cached_topics: List[Dict[str, Any]] = []

    def get_source_type(self) -> GoalSource:
        return GoalSource.TRENDING_TOPICS

    async def analyze(self) -> List[LearningGoal]:
        goals = []

        if not self.config.trending_enabled:
            return goals

        try:
            # Check cache freshness
            if self._should_refresh():
                await self._refresh_trending()

            # Create goals from trending topics
            for topic_data in self._cached_topics:
                if self._is_relevant_topic(topic_data):
                    goal = LearningGoal(
                        id=f"trending_{topic_data['id']}_{int(time.time())}",
                        topic=topic_data["topic"],
                        description=topic_data.get("description", f"Learn about {topic_data['topic']}"),
                        source=GoalSource.TRENDING_TOPICS,
                        priority=GoalPriority.LOW.value,  # Lower priority for trending
                        keywords=topic_data.get("keywords", []),
                        urls=topic_data.get("urls", []),
                        confidence=topic_data.get("score", 0.5),
                        metadata={
                            "source": topic_data.get("source", "unknown"),
                            "score": topic_data.get("score", 0),
                        }
                    )
                    goals.append(goal)

            logger.info(f"[GoalsDiscovery] TrendingTopics: Found {len(goals)} learning goals")

        except Exception as e:
            logger.warning(f"[GoalsDiscovery] TrendingTopics analysis failed: {e}")

        return goals

    def _should_refresh(self) -> bool:
        """Check if we should refresh trending topics."""
        if self._last_refresh is None:
            return True

        refresh_delta = timedelta(hours=self.config.trending_refresh_hours)
        return datetime.now() - self._last_refresh > refresh_delta

    async def _refresh_trending(self) -> None:
        """Refresh trending topics from external sources."""
        self._cached_topics = []

        # Add some default tech topics for initial bootstrapping
        default_topics = [
            {
                "id": "llm_agents",
                "topic": "LLM Agents and Tool Use",
                "description": "Learn about building autonomous AI agents with tool use capabilities",
                "keywords": ["llm", "agents", "tool-use", "anthropic", "openai"],
                "source": "default",
                "score": 0.8,
            },
            {
                "id": "rag_systems",
                "topic": "RAG Systems",
                "description": "Learn about Retrieval Augmented Generation for knowledge-grounded responses",
                "keywords": ["rag", "retrieval", "embeddings", "chromadb", "vector"],
                "source": "default",
                "score": 0.7,
            },
            {
                "id": "voice_ai",
                "topic": "Voice AI and Real-time Audio",
                "description": "Learn about voice assistants, speech recognition, and real-time audio processing",
                "keywords": ["voice", "speech", "whisper", "tts", "realtime"],
                "source": "default",
                "score": 0.6,
            },
            {
                "id": "macos_automation",
                "topic": "macOS Automation",
                "description": "Learn about automating macOS using AppleScript, Shortcuts, and system APIs",
                "keywords": ["macos", "applescript", "automation", "accessibility"],
                "source": "default",
                "score": 0.5,
            },
        ]

        self._cached_topics.extend(default_topics)
        self._last_refresh = datetime.now()

        logger.debug(f"[GoalsDiscovery] Refreshed trending topics: {len(self._cached_topics)} topics")

    def _is_relevant_topic(self, topic_data: Dict[str, Any]) -> bool:
        """Check if a topic is relevant to JARVIS focus areas."""
        topic = topic_data.get("topic", "").lower()
        keywords = [k.lower() for k in topic_data.get("keywords", [])]

        focus_categories = [c.lower() for c in self.config.focus_categories]

        # Check if topic matches focus categories
        for category in focus_categories:
            if category in topic or any(category in kw for kw in keywords):
                return True

        return False


class PrimeFeedbackAnalyzer(GoalSourceAnalyzer):
    """
    Analyzes JARVIS Prime model performance to discover improvement areas.

    Connects to JARVIS Prime metrics to find:
    - Low-confidence responses
    - High latency queries
    - Categories with poor performance
    """

    def __init__(self, config: GoalsDiscoveryConfig):
        self.config = config

    def get_source_type(self) -> GoalSource:
        return GoalSource.PRIME_FEEDBACK

    async def analyze(self) -> List[LearningGoal]:
        goals = []

        try:
            # Try to connect to JARVIS Prime metrics
            metrics = await self._get_prime_metrics()

            if metrics:
                # Analyze performance by category
                for category, stats in metrics.get("categories", {}).items():
                    if stats.get("avg_confidence", 1.0) < 0.6:
                        goal = LearningGoal(
                            id=f"prime_{category}_{int(time.time())}",
                            topic=f"Improve {category} responses",
                            description=f"JARVIS Prime shows low confidence ({stats['avg_confidence']:.1%}) for {category} queries",
                            source=GoalSource.PRIME_FEEDBACK,
                            priority=GoalPriority.HIGH.value,
                            keywords=[category.lower()],
                            confidence=1.0 - stats.get("avg_confidence", 0),
                            metadata={
                                "category": category,
                                "stats": stats,
                            }
                        )
                        goals.append(goal)

            logger.info(f"[GoalsDiscovery] PrimeFeedback: Found {len(goals)} learning goals")

        except Exception as e:
            logger.warning(f"[GoalsDiscovery] PrimeFeedback analysis failed: {e}")

        return goals

    async def _get_prime_metrics(self) -> Optional[Dict[str, Any]]:
        """Get performance metrics from JARVIS Prime."""
        try:
            # Try to read metrics file
            metrics_file = self.config.jarvis_prime_repo / "data" / "metrics" / "performance.json"
            if metrics_file.exists():
                with open(metrics_file, 'r') as f:
                    return json.load(f)

            # Try to connect to Prime API
            prime_url = os.getenv("JARVIS_PRIME_URL", "http://localhost:8000")  # v89.0: Fixed to 8000
            # TODO: Implement API call when Prime metrics endpoint is available

        except Exception as e:
            logger.debug(f"[GoalsDiscovery] Prime metrics unavailable: {e}")

        return None


# =============================================================================
# Main Goals Discovery Engine
# =============================================================================

class IntelligentLearningGoalsDiscovery:
    """
    Main engine for discovering and managing learning goals.

    Coordinates multiple analyzers to discover goals from various sources,
    prioritizes them, and manages the goal lifecycle.
    """

    def __init__(self, config: Optional[GoalsDiscoveryConfig] = None):
        self.config = config or GoalsDiscoveryConfig()
        self._analyzers: List[GoalSourceAnalyzer] = []
        self._goals: Dict[str, LearningGoal] = {}
        self._db_conn: Optional[sqlite3.Connection] = None
        self._initialized = False
        self._lock = asyncio.Lock()

        # Register default analyzers
        self._register_default_analyzers()

        logger.info("[GoalsDiscovery] Initialized")

    def _register_default_analyzers(self) -> None:
        """Register default goal source analyzers."""
        self._analyzers = [
            FailedQueriesAnalyzer(self.config),
            ErrorLogsAnalyzer(self.config),
            TrendingTopicsAnalyzer(self.config),
            PrimeFeedbackAnalyzer(self.config),
        ]

    async def initialize(self) -> None:
        """Initialize the goals discovery system."""
        if self._initialized:
            return

        async with self._lock:
            try:
                # Create database directory
                self.config.db_path.parent.mkdir(parents=True, exist_ok=True)

                # Initialize database
                self._db_conn = sqlite3.connect(str(self.config.db_path))
                self._create_tables()

                # Load existing goals
                await self._load_goals_from_db()

                self._initialized = True
                logger.info(f"[GoalsDiscovery] Initialized with {len(self._goals)} existing goals")

            except Exception as e:
                logger.error(f"[GoalsDiscovery] Initialization failed: {e}")
                raise

    def _create_tables(self) -> None:
        """Create database tables."""
        if not self._db_conn:
            return

        cursor = self._db_conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS learning_goals (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                description TEXT,
                source TEXT NOT NULL,
                priority INTEGER DEFAULT 5,
                status TEXT DEFAULT 'pending',
                keywords TEXT,  -- JSON array
                urls TEXT,  -- JSON array
                related_queries TEXT,  -- JSON array
                related_errors TEXT,  -- JSON array
                confidence REAL DEFAULT 0.5,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT,
                metadata TEXT  -- JSON object
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_goals_status ON learning_goals(status)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_goals_priority ON learning_goals(priority DESC)
        """)

        self._db_conn.commit()

    async def _load_goals_from_db(self) -> None:
        """Load existing goals from database."""
        if not self._db_conn:
            return

        cursor = self._db_conn.cursor()
        cursor.execute("""
            SELECT id, topic, description, source, priority, status,
                   keywords, urls, related_queries, related_errors,
                   confidence, created_at, updated_at, completed_at, metadata
            FROM learning_goals
            WHERE status IN ('pending', 'in_progress')
        """)

        for row in cursor.fetchall():
            try:
                goal = LearningGoal(
                    id=row[0],
                    topic=row[1],
                    description=row[2] or "",
                    source=GoalSource(row[3]),
                    priority=row[4],
                    status=GoalStatus(row[5]),
                    keywords=json.loads(row[6]) if row[6] else [],
                    urls=json.loads(row[7]) if row[7] else [],
                    related_queries=json.loads(row[8]) if row[8] else [],
                    related_errors=json.loads(row[9]) if row[9] else [],
                    confidence=row[10] or 0.5,
                    created_at=datetime.fromisoformat(row[11]) if row[11] else datetime.now(),
                    updated_at=datetime.fromisoformat(row[12]) if row[12] else datetime.now(),
                    completed_at=datetime.fromisoformat(row[13]) if row[13] else None,
                    metadata=json.loads(row[14]) if row[14] else {},
                )
                self._goals[goal.id] = goal

            except Exception as e:
                logger.warning(f"[GoalsDiscovery] Failed to load goal: {e}")

    async def discover_goals(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Discover new learning goals from all sources.

        Args:
            limit: Maximum number of new goals to return

        Returns:
            List of discovered goals as dictionaries
        """
        if not self._initialized:
            await self.initialize()

        async with self._lock:
            discovered = []

            # Run all analyzers in parallel
            tasks = [analyzer.analyze() for analyzer in self._analyzers]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(f"[GoalsDiscovery] Analyzer {i} failed: {result}")
                    continue

                for goal in result:
                    # Check for duplicates
                    if not self._is_duplicate(goal):
                        # Add to collection
                        self._goals[goal.id] = goal
                        # Save to database
                        self._save_goal_to_db(goal)
                        discovered.append(goal.to_dict())

            # Sort by priority and return top N
            discovered.sort(key=lambda g: g["priority"], reverse=True)
            discovered = discovered[:limit]

            # Respect max pending goals limit
            pending_count = sum(1 for g in self._goals.values() if g.status == GoalStatus.PENDING)
            if pending_count > self.config.max_pending_goals:
                # Cancel lowest priority goals
                self._prune_low_priority_goals()

            logger.info(f"[GoalsDiscovery] Discovered {len(discovered)} new goals")

            return discovered

    def _is_duplicate(self, goal: LearningGoal) -> bool:
        """Check if a goal is a duplicate of an existing one."""
        for existing in self._goals.values():
            # Same topic
            if existing.topic.lower() == goal.topic.lower():
                return True

            # High keyword overlap
            existing_kw = set(k.lower() for k in existing.keywords)
            new_kw = set(k.lower() for k in goal.keywords)
            if existing_kw and new_kw:
                overlap = len(existing_kw & new_kw) / len(existing_kw | new_kw)
                if overlap > 0.7:
                    return True

        return False

    def _save_goal_to_db(self, goal: LearningGoal) -> None:
        """Save a goal to the database."""
        if not self._db_conn:
            return

        try:
            cursor = self._db_conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO learning_goals
                (id, topic, description, source, priority, status,
                 keywords, urls, related_queries, related_errors,
                 confidence, created_at, updated_at, completed_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                goal.id,
                goal.topic,
                goal.description,
                goal.source.value,
                goal.priority,
                goal.status.value,
                json.dumps(goal.keywords),
                json.dumps(goal.urls),
                json.dumps(goal.related_queries),
                json.dumps(goal.related_errors),
                goal.confidence,
                goal.created_at.isoformat(),
                goal.updated_at.isoformat(),
                goal.completed_at.isoformat() if goal.completed_at else None,
                json.dumps(goal.metadata),
            ))
            self._db_conn.commit()

        except Exception as e:
            logger.warning(f"[GoalsDiscovery] Failed to save goal: {e}")

    def _prune_low_priority_goals(self) -> None:
        """Remove lowest priority pending goals when over limit."""
        pending = [g for g in self._goals.values() if g.status == GoalStatus.PENDING]
        pending.sort(key=lambda g: g.priority)

        # Remove lowest priority goals
        to_remove = len(pending) - self.config.max_pending_goals
        for goal in pending[:to_remove]:
            goal.status = GoalStatus.CANCELLED
            goal.updated_at = datetime.now()
            self._save_goal_to_db(goal)
            del self._goals[goal.id]

    async def get_pending_goals(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get pending goals sorted by priority."""
        if not self._initialized:
            await self.initialize()

        pending = [
            g.to_dict() for g in self._goals.values()
            if g.status == GoalStatus.PENDING
        ]

        pending.sort(key=lambda g: g["priority"], reverse=True)
        return pending[:limit]

    async def mark_goal_completed(self, goal_id: str) -> bool:
        """Mark a goal as completed."""
        if goal_id in self._goals:
            goal = self._goals[goal_id]
            goal.status = GoalStatus.COMPLETED
            goal.completed_at = datetime.now()
            goal.updated_at = datetime.now()
            self._save_goal_to_db(goal)
            return True

        return False

    async def mark_goal_failed(self, goal_id: str, reason: str = "") -> bool:
        """Mark a goal as failed."""
        if goal_id in self._goals:
            goal = self._goals[goal_id]
            goal.status = GoalStatus.FAILED
            goal.updated_at = datetime.now()
            goal.metadata["failure_reason"] = reason
            self._save_goal_to_db(goal)
            return True

        return False

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about learning goals."""
        status_counts = {}
        source_counts = {}
        total_priority = 0

        for goal in self._goals.values():
            status_counts[goal.status.value] = status_counts.get(goal.status.value, 0) + 1
            source_counts[goal.source.value] = source_counts.get(goal.source.value, 0) + 1
            total_priority += goal.priority

        return {
            "total_goals": len(self._goals),
            "by_status": status_counts,
            "by_source": source_counts,
            "avg_priority": total_priority / len(self._goals) if self._goals else 0,
        }


# =============================================================================
# Global Instance
# =============================================================================

_goals_discovery_instance: Optional[IntelligentLearningGoalsDiscovery] = None
_goals_discovery_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_goals_discovery_async() -> IntelligentLearningGoalsDiscovery:
    """Get or create the global GoalsDiscovery instance (async)."""
    global _goals_discovery_instance

    async with _goals_discovery_lock:
        if _goals_discovery_instance is None:
            _goals_discovery_instance = IntelligentLearningGoalsDiscovery()
            await _goals_discovery_instance.initialize()

        return _goals_discovery_instance


def get_goals_discovery() -> IntelligentLearningGoalsDiscovery:
    """Get the global GoalsDiscovery instance (sync - for import compatibility)."""
    global _goals_discovery_instance

    if _goals_discovery_instance is None:
        _goals_discovery_instance = IntelligentLearningGoalsDiscovery()
        # Don't initialize here - let caller do async init

    return _goals_discovery_instance


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "IntelligentLearningGoalsDiscovery",
    "GoalsDiscoveryConfig",
    "LearningGoal",
    "GoalSource",
    "GoalPriority",
    "GoalStatus",
    "get_goals_discovery",
    "get_goals_discovery_async",
]
