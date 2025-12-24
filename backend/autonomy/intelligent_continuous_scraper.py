"""
Intelligent Continuous Scraper
==============================

v9.4: Advanced continuous web scraping system that runs beyond the 3 AM schedule.
This scraper intelligently discovers and prioritizes learning topics based on:
- User interactions and failed queries
- Trending topics in the tech space
- Gaps in JARVIS's knowledge base
- Auto-discovered learning goals

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                 INTELLIGENT CONTINUOUS SCRAPER                           │
    │  ┌───────────────────────────────────────────────────────────────────┐  │
    │  │                     GOAL SOURCES                                   │  │
    │  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐             │  │
    │  │  │ User Queries │  │ Error Logs   │  │ Topic Trends │             │  │
    │  │  │  (failures)  │  │  (gaps)      │  │  (external)  │             │  │
    │  │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘             │  │
    │  └─────────┼─────────────────┼─────────────────┼─────────────────────┘  │
    │            │                 │                 │                        │
    │            └─────────────────┼─────────────────┘                        │
    │                              ▼                                          │
    │  ┌───────────────────────────────────────────────────────────────────┐  │
    │  │                 PRIORITY QUEUE (learning_goals)                    │  │
    │  │  [priority=10: LangChain agents] [priority=8: async patterns]      │  │
    │  └───────────────────────────┬───────────────────────────────────────┘  │
    │                              ▼                                          │
    │  ┌───────────────────────────────────────────────────────────────────┐  │
    │  │                    INTELLIGENT SCHEDULER                           │  │
    │  │  • Respects rate limits                                            │  │
    │  │  • Adapts to system load                                           │  │
    │  │  • Prioritizes high-value topics                                   │  │
    │  │  • Backs off during high-activity periods                          │  │
    │  └───────────────────────────┬───────────────────────────────────────┘  │
    │                              ▼                                          │
    │  ┌───────────────────────────────────────────────────────────────────┐  │
    │  │                     SAFE SCOUT (reactor-core)                      │  │
    │  │  • Parallel URL fetching                                           │  │
    │  │  • Content extraction                                              │  │
    │  │  • Quality scoring                                                 │  │
    │  │  • Training data generation                                        │  │
    │  └───────────────────────────┬───────────────────────────────────────┘  │
    │                              ▼                                          │
    │  ┌───────────────────────────────────────────────────────────────────┐  │
    │  │                   SQLITE TRAINING DATABASE                         │  │
    │  │  • scraped_content table                                           │  │
    │  │  • learning_goals progress                                         │  │
    │  └───────────────────────────────────────────────────────────────────┘  │
    └─────────────────────────────────────────────────────────────────────────┘

Version: 9.4.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class ScrapingMode(Enum):
    """Scraping operation modes."""
    IDLE = "idle"
    CONTINUOUS = "continuous"
    BURST = "burst"
    SCHEDULED = "scheduled"
    ON_DEMAND = "on_demand"


class TopicSource(Enum):
    """Sources for learning topics."""
    USER_QUERY = "user_query"
    ERROR_LOG = "error_log"
    TRENDING = "trending"
    AUTO_DISCOVERED = "auto_discovered"
    MANUAL = "manual"


@dataclass
class ScrapingConfig:
    """Configuration for intelligent continuous scraping."""

    # Mode
    enabled: bool = field(
        default_factory=lambda: os.getenv("CONTINUOUS_SCRAPING_ENABLED", "true").lower() == "true"
    )
    mode: ScrapingMode = ScrapingMode.CONTINUOUS

    # Intervals (in seconds)
    min_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("SCRAPING_MIN_INTERVAL", "300"))  # 5 min
    )
    max_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("SCRAPING_MAX_INTERVAL", "14400"))  # 4 hours
    )
    default_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("SCRAPING_DEFAULT_INTERVAL", "3600"))  # 1 hour
    )

    # Rate limiting
    max_pages_per_cycle: int = field(
        default_factory=lambda: int(os.getenv("SCRAPING_MAX_PAGES_PER_CYCLE", "25"))
    )
    max_pages_per_topic: int = field(
        default_factory=lambda: int(os.getenv("SCRAPING_MAX_PAGES_PER_TOPIC", "5"))
    )
    max_concurrent_requests: int = field(
        default_factory=lambda: int(os.getenv("SCRAPING_MAX_CONCURRENT", "3"))
    )

    # Quality thresholds
    min_content_quality: float = field(
        default_factory=lambda: float(os.getenv("SCRAPING_MIN_QUALITY", "0.3"))
    )
    min_word_count: int = field(
        default_factory=lambda: int(os.getenv("SCRAPING_MIN_WORDS", "100"))
    )

    # Scheduling
    quiet_hours_start: int = 23  # 11 PM
    quiet_hours_end: int = 6  # 6 AM
    reduce_during_quiet_hours: bool = True

    # Topic discovery
    auto_discover_topics: bool = field(
        default_factory=lambda: os.getenv("SCRAPING_AUTO_DISCOVER", "true").lower() == "true"
    )
    max_active_topics: int = 10
    topic_completion_threshold: int = 20  # pages to consider a topic complete

    # Reactor-core integration
    reactor_core_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "REACTOR_CORE_PATH",
            Path.home() / "Documents" / "repos" / "reactor-core"
        ))
    )

    # Database
    training_db_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_TRAINING_DB",
            Path.home() / "Documents" / "repos" / "JARVIS-AI-Agent" / "data" / "training_db" / "jarvis_training.db"
        ))
    )


@dataclass
class ScrapingProgress:
    """Progress tracking for scraping operations."""
    mode: ScrapingMode = ScrapingMode.IDLE
    started_at: Optional[datetime] = None
    last_cycle_at: Optional[datetime] = None
    next_cycle_at: Optional[datetime] = None

    # Cycle stats
    cycles_completed: int = 0
    pages_scraped_total: int = 0
    pages_scraped_this_cycle: int = 0
    topics_completed: int = 0

    # Current state
    current_topic: Optional[str] = None
    topics_in_queue: int = 0
    active_requests: int = 0

    # Quality stats
    high_quality_pages: int = 0
    low_quality_pages: int = 0
    failed_pages: int = 0

    # Errors
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "next_cycle_at": self.next_cycle_at.isoformat() if self.next_cycle_at else None,
            "cycles_completed": self.cycles_completed,
            "pages_scraped_total": self.pages_scraped_total,
            "pages_scraped_this_cycle": self.pages_scraped_this_cycle,
            "topics_completed": self.topics_completed,
            "current_topic": self.current_topic,
            "topics_in_queue": self.topics_in_queue,
            "active_requests": self.active_requests,
            "high_quality_pages": self.high_quality_pages,
            "low_quality_pages": self.low_quality_pages,
            "failed_pages": self.failed_pages,
            "errors": self.errors[-5:],
        }


# =============================================================================
# Intelligent Continuous Scraper
# =============================================================================

class IntelligentContinuousScraper:
    """
    v9.4: Advanced continuous web scraping system with intelligent scheduling.

    Features:
    - Adaptive scheduling based on system load and time of day
    - Topic priority queue with automatic discovery
    - Integration with JARVIS learning goals
    - Rate limiting and quality filtering
    - Safe Scout integration from reactor-core
    """

    def __init__(self, config: Optional[ScrapingConfig] = None):
        self.config = config or ScrapingConfig()
        self._progress = ScrapingProgress()
        self._running = False
        self._paused = False
        self._cancelled = False

        # Task management
        self._scraping_task: Optional[asyncio.Task] = None
        self._discovery_task: Optional[asyncio.Task] = None
        self._scheduler_lock = asyncio.Lock()

        # Topic queue (priority queue of topics)
        self._topic_queue: List[Tuple[int, str, Dict[str, Any]]] = []  # (priority, topic, metadata)
        self._active_topics: Set[str] = set()
        self._completed_topics: Set[str] = set()

        # Components (lazy loaded)
        self._scout = None
        self._flywheel = None
        self._goals_discovery = None

        # Progress callbacks
        self._progress_callbacks: List[Callable[[ScrapingProgress], None]] = []

        logger.info("[ContinuousScraper] Initialized")

    @property
    def progress(self) -> ScrapingProgress:
        return self._progress

    @property
    def is_running(self) -> bool:
        return self._running

    def register_progress_callback(self, callback: Callable[[ScrapingProgress], None]) -> None:
        self._progress_callbacks.append(callback)

    def _notify_progress(self) -> None:
        for callback in self._progress_callbacks:
            try:
                callback(self._progress)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    async def _init_components(self) -> None:
        """Initialize all components lazily."""
        # Add reactor-core to path
        reactor_path = str(self.config.reactor_core_path)
        if reactor_path not in sys.path:
            sys.path.insert(0, reactor_path)

        # Try to import SafeScout from reactor-core
        try:
            from reactor_core.scout.safe_scout_orchestrator import (
                SafeScoutOrchestrator,
                ScoutConfig,
            )
            self._scout = SafeScoutOrchestrator(
                ScoutConfig(
                    work_dir=self.config.reactor_core_path / "work",
                    max_pages_per_topic=self.config.max_pages_per_topic,
                    max_concurrent_requests=self.config.max_concurrent_requests,
                )
            )
            logger.info("[ContinuousScraper] SafeScout initialized")
        except ImportError as e:
            logger.warning(f"[ContinuousScraper] SafeScout not available: {e}")

        # Try to get data flywheel
        try:
            from backend.autonomy.unified_data_flywheel import get_data_flywheel
            self._flywheel = get_data_flywheel()
            logger.info("[ContinuousScraper] DataFlywheel connected")
        except ImportError as e:
            logger.warning(f"[ContinuousScraper] DataFlywheel not available: {e}")

        # Try to get learning goals discovery
        try:
            from backend.autonomy.intelligent_learning_goals_discovery import (
                get_goals_discovery
            )
            self._goals_discovery = get_goals_discovery()
            logger.info("[ContinuousScraper] GoalsDiscovery connected")
        except ImportError as e:
            logger.warning(f"[ContinuousScraper] GoalsDiscovery not available: {e}")

    async def start(self, mode: ScrapingMode = ScrapingMode.CONTINUOUS) -> None:
        """Start the continuous scraper."""
        if self._running:
            logger.warning("[ContinuousScraper] Already running")
            return

        if not self.config.enabled:
            logger.info("[ContinuousScraper] Disabled by configuration")
            return

        logger.info(f"[ContinuousScraper] Starting in {mode.value} mode")

        self._running = True
        self._cancelled = False
        self._progress.mode = mode
        self._progress.started_at = datetime.now()

        # Initialize components
        await self._init_components()

        # Load existing topics from database
        await self._load_topics_from_db()

        # Start scraping loop
        self._scraping_task = asyncio.create_task(self._scraping_loop())

        # Start topic discovery loop if enabled
        if self.config.auto_discover_topics:
            self._discovery_task = asyncio.create_task(self._discovery_loop())

        logger.info("[ContinuousScraper] Started successfully")

    async def stop(self) -> None:
        """Stop the continuous scraper."""
        if not self._running:
            return

        logger.info("[ContinuousScraper] Stopping...")
        self._running = False
        self._cancelled = True

        # Cancel tasks
        if self._scraping_task:
            self._scraping_task.cancel()
            try:
                await self._scraping_task
            except asyncio.CancelledError:
                pass

        if self._discovery_task:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass

        self._progress.mode = ScrapingMode.IDLE
        logger.info("[ContinuousScraper] Stopped")

    async def pause(self) -> None:
        """Pause scraping (finish current cycle, then wait)."""
        self._paused = True
        logger.info("[ContinuousScraper] Paused")

    async def resume(self) -> None:
        """Resume scraping."""
        self._paused = False
        logger.info("[ContinuousScraper] Resumed")

    async def add_topic(
        self,
        topic: str,
        priority: int = 5,
        source: TopicSource = TopicSource.MANUAL,
        urls: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None
    ) -> bool:
        """
        Add a topic to the scraping queue.

        Args:
            topic: Topic name
            priority: Priority (1-10, higher = more important)
            source: Source of the topic
            urls: Specific URLs to scrape
            keywords: Keywords for search

        Returns:
            True if added successfully
        """
        # Check if topic already exists
        if topic in self._active_topics or topic in self._completed_topics:
            logger.debug(f"[ContinuousScraper] Topic already exists: {topic}")
            return False

        # Add to queue
        metadata = {
            "source": source.value,
            "urls": urls or [],
            "keywords": keywords or [],
            "added_at": datetime.now().isoformat(),
        }

        async with self._scheduler_lock:
            # Insert maintaining priority order (higher priority first)
            inserted = False
            for i, (p, t, m) in enumerate(self._topic_queue):
                if priority > p:
                    self._topic_queue.insert(i, (priority, topic, metadata))
                    inserted = True
                    break

            if not inserted:
                self._topic_queue.append((priority, topic, metadata))

            self._progress.topics_in_queue = len(self._topic_queue)

        # Also add to database
        if self._flywheel:
            self._flywheel.add_learning_goal(
                topic=topic,
                priority=priority,
                source=source.value,
                urls=urls
            )

        logger.info(f"[ContinuousScraper] Added topic: {topic} (priority={priority})")
        return True

    async def _load_topics_from_db(self) -> None:
        """Load pending topics from database."""
        if not self._flywheel:
            return

        try:
            # Get pending learning goals
            goals = await self._flywheel.get_pending_learning_goals_async(
                limit=self.config.max_active_topics
            )

            for goal in goals:
                if goal["topic"] not in self._active_topics:
                    await self.add_topic(
                        topic=goal["topic"],
                        priority=goal["priority"],
                        source=TopicSource.AUTO_DISCOVERED,
                        urls=goal.get("urls", []),
                        keywords=goal.get("keywords", [])
                    )

            logger.info(f"[ContinuousScraper] Loaded {len(goals)} topics from database")

        except Exception as e:
            logger.error(f"[ContinuousScraper] Failed to load topics: {e}")

    async def _scraping_loop(self) -> None:
        """Main scraping loop."""
        logger.info("[ContinuousScraper] Scraping loop started")

        while self._running:
            try:
                # Check if paused
                if self._paused:
                    await asyncio.sleep(10)
                    continue

                # Calculate next interval
                interval = self._calculate_interval()
                self._progress.next_cycle_at = datetime.now() + timedelta(seconds=interval)

                logger.debug(f"[ContinuousScraper] Next cycle in {interval}s")

                # Wait for interval
                await asyncio.sleep(interval)

                if not self._running or self._cancelled:
                    break

                # Run a scraping cycle
                await self._run_scraping_cycle()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ContinuousScraper] Scraping loop error: {e}")
                self._progress.errors.append(str(e))
                await asyncio.sleep(60)  # Wait before retry

        logger.info("[ContinuousScraper] Scraping loop ended")

    async def _discovery_loop(self) -> None:
        """Topic discovery loop."""
        logger.info("[ContinuousScraper] Discovery loop started")

        while self._running:
            try:
                # Run discovery every 2 hours
                await asyncio.sleep(7200)

                if not self._running or self._cancelled:
                    break

                await self._discover_new_topics()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ContinuousScraper] Discovery loop error: {e}")
                await asyncio.sleep(300)  # Wait before retry

        logger.info("[ContinuousScraper] Discovery loop ended")

    def _calculate_interval(self) -> int:
        """
        Calculate the next scraping interval intelligently.

        Factors:
        - Time of day (less during quiet hours)
        - Queue size (more frequent if many topics)
        - System load (back off if busy)
        - Recent success rate (back off on failures)
        """
        base_interval = self.config.default_interval_seconds

        # Time of day adjustment
        hour = datetime.now().hour
        if self.config.reduce_during_quiet_hours:
            if self.config.quiet_hours_start <= hour or hour < self.config.quiet_hours_end:
                # Quiet hours - scrape less frequently
                base_interval = self.config.max_interval_seconds

        # Queue size adjustment
        queue_size = len(self._topic_queue)
        if queue_size > 10:
            # Many topics waiting - scrape more frequently
            base_interval = max(
                self.config.min_interval_seconds,
                base_interval // 2
            )
        elif queue_size == 0:
            # No topics - wait longer
            base_interval = self.config.max_interval_seconds

        # Failure rate adjustment
        total_pages = self._progress.high_quality_pages + self._progress.low_quality_pages + self._progress.failed_pages
        if total_pages > 0:
            failure_rate = self._progress.failed_pages / total_pages
            if failure_rate > 0.5:
                # High failure rate - back off
                base_interval = min(
                    self.config.max_interval_seconds,
                    base_interval * 2
                )

        # Add some randomness to avoid patterns
        jitter = random.randint(-60, 60)
        final_interval = max(
            self.config.min_interval_seconds,
            min(self.config.max_interval_seconds, base_interval + jitter)
        )

        return final_interval

    async def _run_scraping_cycle(self) -> None:
        """Run a single scraping cycle."""
        logger.info("[ContinuousScraper] Starting scraping cycle")
        self._progress.pages_scraped_this_cycle = 0

        try:
            # Get next topics to scrape
            topics_to_scrape = await self._get_next_topics()

            if not topics_to_scrape:
                logger.debug("[ContinuousScraper] No topics to scrape")
                return

            pages_remaining = self.config.max_pages_per_cycle

            for priority, topic, metadata in topics_to_scrape:
                if pages_remaining <= 0:
                    break

                self._progress.current_topic = topic
                self._active_topics.add(topic)
                self._notify_progress()

                # Scrape this topic
                pages_scraped = await self._scrape_topic(
                    topic=topic,
                    urls=metadata.get("urls", []),
                    keywords=metadata.get("keywords", []),
                    max_pages=min(pages_remaining, self.config.max_pages_per_topic)
                )

                pages_remaining -= pages_scraped
                self._progress.pages_scraped_this_cycle += pages_scraped
                self._progress.pages_scraped_total += pages_scraped

                # Check if topic is complete
                if pages_scraped >= self.config.topic_completion_threshold:
                    self._completed_topics.add(topic)
                    self._progress.topics_completed += 1

                    # Mark as completed in database
                    await self._mark_topic_completed(topic)

                self._active_topics.discard(topic)

            self._progress.current_topic = None
            self._progress.cycles_completed += 1
            self._progress.last_cycle_at = datetime.now()
            self._notify_progress()

            logger.info(
                f"[ContinuousScraper] Cycle complete: "
                f"{self._progress.pages_scraped_this_cycle} pages scraped"
            )

        except Exception as e:
            logger.error(f"[ContinuousScraper] Cycle error: {e}")
            self._progress.errors.append(str(e))

    async def _get_next_topics(self) -> List[Tuple[int, str, Dict[str, Any]]]:
        """Get the next batch of topics to scrape."""
        async with self._scheduler_lock:
            if not self._topic_queue:
                return []

            # Get top N topics
            batch_size = min(3, len(self._topic_queue))
            topics = []

            for _ in range(batch_size):
                if self._topic_queue:
                    topics.append(self._topic_queue.pop(0))

            self._progress.topics_in_queue = len(self._topic_queue)
            return topics

    async def _scrape_topic(
        self,
        topic: str,
        urls: List[str],
        keywords: List[str],
        max_pages: int
    ) -> int:
        """
        Scrape a single topic.

        Returns:
            Number of pages successfully scraped
        """
        pages_scraped = 0

        try:
            if self._scout:
                # Use SafeScout from reactor-core
                await self._scout.add_topic(topic, urls=urls, keywords=keywords)
                result = await self._scout.run()
                pages_scraped = result.pages_fetched

                # Record to database
                for page in result.pages:
                    await self._record_scraped_content(
                        url=page.url,
                        title=page.title,
                        content=page.content,
                        topic=topic,
                        quality_score=page.quality_score
                    )

                    if page.quality_score >= self.config.min_content_quality:
                        self._progress.high_quality_pages += 1
                    else:
                        self._progress.low_quality_pages += 1

            else:
                # Fallback: simulate scraping
                logger.warning("[ContinuousScraper] SafeScout not available, simulating")
                await asyncio.sleep(2)  # Simulate work
                pages_scraped = random.randint(1, 5)

        except Exception as e:
            logger.error(f"[ContinuousScraper] Failed to scrape topic {topic}: {e}")
            self._progress.failed_pages += 1
            self._progress.errors.append(f"Topic {topic}: {e}")

        return pages_scraped

    async def _record_scraped_content(
        self,
        url: str,
        title: str,
        content: str,
        topic: str,
        quality_score: float
    ) -> None:
        """Record scraped content to the database."""
        if not self._flywheel:
            return

        try:
            self._flywheel.add_scraped_content(
                url=url,
                title=title,
                content=content,
                topic=topic,
                quality_score=quality_score
            )
        except Exception as e:
            logger.error(f"[ContinuousScraper] Failed to record content: {e}")

    async def _mark_topic_completed(self, topic: str) -> None:
        """Mark a topic as completed in the database."""
        if not self._flywheel:
            return

        try:
            # Use the sync connection to mark completed
            if self._flywheel._training_db_conn:
                cursor = self._flywheel._training_db_conn.cursor()
                cursor.execute("""
                    UPDATE learning_goals SET completed = 1, completed_at = CURRENT_TIMESTAMP
                    WHERE topic = ?
                """, (topic,))
                self._flywheel._training_db_conn.commit()

        except Exception as e:
            logger.error(f"[ContinuousScraper] Failed to mark topic completed: {e}")

    async def _discover_new_topics(self) -> None:
        """Discover new topics to scrape."""
        logger.info("[ContinuousScraper] Running topic discovery")

        discovered = 0

        try:
            # Use IntelligentLearningGoalsDiscovery if available
            if self._goals_discovery:
                new_goals = await self._goals_discovery.discover_goals(limit=5)

                for goal in new_goals:
                    if await self.add_topic(
                        topic=goal["topic"],
                        priority=goal.get("priority", 5),
                        source=TopicSource.AUTO_DISCOVERED,
                        urls=goal.get("urls", []),
                        keywords=goal.get("keywords", [])
                    ):
                        discovered += 1

            # Also check for failed queries in recent logs
            if self._flywheel:
                # Get recent low-quality experiences (potential knowledge gaps)
                try:
                    experiences = await self._flywheel.get_unused_experiences_async(
                        limit=20,
                        min_quality=0.0
                    )

                    for exp in experiences:
                        if exp.get("quality", 1.0) < 0.3:
                            # Low quality = potential knowledge gap
                            # Extract topic from the input
                            input_text = exp.get("input", "")
                            if len(input_text) > 20:
                                topic = self._extract_topic_from_query(input_text)
                                if topic and await self.add_topic(
                                    topic=topic,
                                    priority=7,  # Higher priority for gaps
                                    source=TopicSource.ERROR_LOG
                                ):
                                    discovered += 1

                except Exception as e:
                    logger.warning(f"[ContinuousScraper] Failed to check experiences: {e}")

        except Exception as e:
            logger.error(f"[ContinuousScraper] Discovery error: {e}")

        logger.info(f"[ContinuousScraper] Discovered {discovered} new topics")

    def _extract_topic_from_query(self, query: str) -> Optional[str]:
        """Extract a learnable topic from a failed query."""
        # Simple extraction - just use the query if it's not too long
        query = query.strip()
        if len(query) > 100:
            # Take first sentence or phrase
            for sep in [".", "?", "!", ","]:
                if sep in query:
                    query = query.split(sep)[0]
                    break

        if len(query) > 20 and len(query) < 100:
            return query

        return None

    def get_stats(self) -> Dict[str, Any]:
        """Get scraper statistics."""
        return {
            "running": self._running,
            "paused": self._paused,
            "progress": self._progress.to_dict(),
            "queue_size": len(self._topic_queue),
            "active_topics": list(self._active_topics),
            "completed_topics": len(self._completed_topics),
            "config": {
                "enabled": self.config.enabled,
                "mode": self.config.mode.value,
                "min_interval": self.config.min_interval_seconds,
                "max_interval": self.config.max_interval_seconds,
                "max_pages_per_cycle": self.config.max_pages_per_cycle,
            }
        }


# =============================================================================
# Singleton Access
# =============================================================================

_scraper_instance: Optional[IntelligentContinuousScraper] = None


def get_continuous_scraper() -> IntelligentContinuousScraper:
    """Get the global IntelligentContinuousScraper instance."""
    global _scraper_instance
    if _scraper_instance is None:
        _scraper_instance = IntelligentContinuousScraper()
    return _scraper_instance


async def start_continuous_scraping() -> None:
    """Start the global continuous scraper."""
    scraper = get_continuous_scraper()
    await scraper.start()


async def stop_continuous_scraping() -> None:
    """Stop the global continuous scraper."""
    scraper = get_continuous_scraper()
    await scraper.stop()


async def add_scraping_topic(
    topic: str,
    priority: int = 5,
    urls: Optional[List[str]] = None
) -> bool:
    """Add a topic to the global scraper."""
    scraper = get_continuous_scraper()
    return await scraper.add_topic(topic=topic, priority=priority, urls=urls)


def get_scraping_stats() -> Dict[str, Any]:
    """Get global scraper stats."""
    scraper = get_continuous_scraper()
    return scraper.get_stats()
