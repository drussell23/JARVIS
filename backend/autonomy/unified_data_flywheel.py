"""
Unified Data Flywheel Orchestrator
===================================

Connects JARVIS-AI-Agent, JARVIS-Prime, and reactor-core into a
continuous self-improving learning loop.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                     JARVIS UNIFIED DATA FLYWHEEL                         │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                        DATA SOURCES                                  ││
    │  │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  ││
    │  │  │  JARVISConnector │  │  SafeScout       │  │  ChromaDB Memory │  ││
    │  │  │  (Experiences)   │  │  (Web Docs)      │  │  (Context)       │  ││
    │  │  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  ││
    │  └───────────┼─────────────────────┼─────────────────────┼────────────┘│
    │              │                     │                     │              │
    │              └─────────────────────┼─────────────────────┘              │
    │                                    ▼                                    │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                      DATASET BUILDER                                ││
    │  │  - PII Anonymization                                                ││
    │  │  - Format Conversion (ChatML/Alpaca)                                ││
    │  │  - Quality Filtering                                                ││
    │  │  - Deduplication                                                    ││
    │  └───────────────────────────────┬─────────────────────────────────────┘│
    │                                  ▼                                      │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                      ASYNC TRAINER                                   ││
    │  │  - LoRA/QLoRA Fine-tuning                                           ││
    │  │  - Memory-Efficient Training                                        ││
    │  │  - Progress Tracking                                                ││
    │  └───────────────────────────────┬─────────────────────────────────────┘│
    │                                  ▼                                      │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                      GGUF EXPORTER                                   ││
    │  │  - Quantization (Q4_K_M, Q5_K_M, Q8_0)                              ││
    │  │  - Optimization for llama.cpp                                       ││
    │  └───────────────────────────────┬─────────────────────────────────────┘│
    │                                  ▼                                      │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                  REACTOR-CORE WATCHER                                ││
    │  │  - Auto-deploy to local JARVIS-Prime                                ││
    │  │  - Upload to GCS for Cloud Run                                      ││
    │  │  - Hot-swap notification                                            ││
    │  └───────────────────────────────┬─────────────────────────────────────┘│
    │                                  ▼                                      │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                    OBSERVABILITY HUB                                 ││
    │  │  - Langfuse Tracing                                                 ││
    │  │  - Helicone Cost Tracking                                           ││
    │  │  - Performance Metrics                                              ││
    │  └─────────────────────────────────────────────────────────────────────┘│
    └─────────────────────────────────────────────────────────────────────────┘

Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Awaitable, Tuple

logger = logging.getLogger(__name__)

# v9.4: Try to import aiosqlite for async database operations
try:
    import aiosqlite
    AIOSQLITE_AVAILABLE = True
except ImportError:
    AIOSQLITE_AVAILABLE = False
    logger.info("[Flywheel] aiosqlite not available, using sync sqlite3")


# =============================================================================
# Configuration
# =============================================================================

class FlywheelStage(Enum):
    """Stages of the data flywheel."""
    IDLE = "idle"
    COLLECTING_EXPERIENCES = "collecting_experiences"
    COLLECTING_WEB_DATA = "collecting_web_data"
    BUILDING_DATASET = "building_dataset"
    TRAINING = "training"
    EXPORTING_GGUF = "exporting_gguf"
    DEPLOYING = "deploying"
    COMPLETED = "completed"
    FAILED = "failed"


class DataSourceType(Enum):
    """Types of data sources."""
    JARVIS_EXPERIENCES = "jarvis_experiences"
    WEB_DOCUMENTATION = "web_documentation"
    CHROMADB_MEMORY = "chromadb_memory"
    MANUAL_UPLOAD = "manual_upload"


@dataclass
class FlywheelConfig:
    """Configuration for the unified data flywheel."""

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

    # SQLite Training Database (v9.0)
    training_db_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TRAINING_DB_ENABLED", "true").lower() == "true"
    )
    training_db_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_TRAINING_DB",
            Path.home() / "Documents" / "repos" / "JARVIS-AI-Agent" / "data" / "training_db" / "jarvis_training.db"
        ))
    )

    # Data collection settings
    experience_lookback_hours: int = 24
    min_experiences_for_training: int = 100
    min_web_examples_for_training: int = 50

    # Training triggers
    auto_train_enabled: bool = field(
        default_factory=lambda: os.getenv("FLYWHEEL_AUTO_TRAIN", "true").lower() == "true"
    )
    training_cooldown_hours: int = 24  # Minimum hours between auto-trainings

    # Scout settings
    scout_topics: List[str] = field(default_factory=lambda: [
        "Python asyncio best practices",
        "macOS automation AppleScript",
        "Voice authentication security",
        "LLM fine-tuning techniques",
        "LangChain agent patterns",
        "FastAPI async patterns",
    ])
    scout_max_pages_per_topic: int = 10

    # Training settings
    base_model: str = "meta-llama/Llama-3.2-3B"
    output_name: str = "jarvis-prime"
    quantization: str = "Q4_K_M"

    # GCS settings
    gcs_bucket: str = field(
        default_factory=lambda: os.getenv(
            "JARVIS_MODELS_GCS_BUCKET",
            "gs://jarvis-473803-deployments/models"
        )
    )


@dataclass
class FlywheelProgress:
    """Progress tracking for flywheel execution."""
    stage: FlywheelStage = FlywheelStage.IDLE
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Data collection stats
    experiences_collected: int = 0
    corrections_found: int = 0
    web_pages_scraped: int = 0
    examples_synthesized: int = 0

    # Dataset stats
    dataset_examples: int = 0
    dataset_size_mb: float = 0.0

    # Training stats
    training_epochs: int = 0
    current_epoch: int = 0
    current_loss: float = 0.0
    best_loss: float = float('inf')

    # Deployment stats
    model_size_mb: float = 0.0
    deployed_local: bool = False
    deployed_cloud: bool = False

    # Errors
    errors: List[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "experiences_collected": self.experiences_collected,
            "corrections_found": self.corrections_found,
            "web_pages_scraped": self.web_pages_scraped,
            "examples_synthesized": self.examples_synthesized,
            "dataset_examples": self.dataset_examples,
            "dataset_size_mb": self.dataset_size_mb,
            "training_epochs": self.training_epochs,
            "current_epoch": self.current_epoch,
            "current_loss": self.current_loss,
            "best_loss": self.best_loss,
            "model_size_mb": self.model_size_mb,
            "deployed_local": self.deployed_local,
            "deployed_cloud": self.deployed_cloud,
            "errors": self.errors[-10:],
        }


@dataclass
class FlywheelResult:
    """Result of a flywheel execution."""
    success: bool
    run_id: str
    progress: FlywheelProgress
    model_path: Optional[str] = None
    gcs_path: Optional[str] = None
    error: Optional[str] = None


# =============================================================================
# v10.0: Intelligent Schema Migration System
# =============================================================================
# ROOT CAUSE FIX: Database schema mismatch between sync and async initialization
#
# PROBLEM:
# - Sync database init (sqlite3) created tables with OLD schema
# - Async database init (aiosqlite) expected NEW schema with more columns
# - CREATE TABLE IF NOT EXISTS doesn't update existing tables
# - Result: "no such column: description" errors
#
# SOLUTION:
# - Single source of truth for schema (UNIFIED_SCHEMA)
# - Schema version tracking (flywheel_schema_version table)
# - Automatic migration on database open
# - Column existence checks before ALTER TABLE
# - Works for both sync (sqlite3) and async (aiosqlite) connections
# =============================================================================

# Current schema version - increment when adding migrations
SCHEMA_VERSION = 2

# Unified schema definition - single source of truth
UNIFIED_SCHEMA = """
    -- Schema version tracking table
    CREATE TABLE IF NOT EXISTS flywheel_schema_version (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL DEFAULT 1,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- Experiences table: Records of JARVIS interactions
    CREATE TABLE IF NOT EXISTS experiences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        source TEXT NOT NULL,
        input_text TEXT NOT NULL,
        output_text TEXT NOT NULL,
        context TEXT,
        quality_score REAL DEFAULT 0.5,
        feedback TEXT,
        correction TEXT,
        used_in_training INTEGER DEFAULT 0,
        training_run_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    -- Scraped content table: Web documentation
    CREATE TABLE IF NOT EXISTS scraped_content (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE NOT NULL,
        title TEXT,
        content TEXT NOT NULL,
        content_type TEXT DEFAULT 'documentation',
        topic TEXT,
        language TEXT DEFAULT 'en',
        quality_score REAL DEFAULT 0.5,
        word_count INTEGER,
        code_blocks INTEGER DEFAULT 0,
        scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        used_in_training INTEGER DEFAULT 0,
        training_run_id INTEGER
    );

    -- Learning goals table: Topics to learn
    CREATE TABLE IF NOT EXISTS learning_goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT UNIQUE NOT NULL,
        description TEXT,
        priority INTEGER DEFAULT 5,
        source TEXT DEFAULT 'auto',
        urls TEXT,
        keywords TEXT,
        discovered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        started_at DATETIME,
        completed INTEGER DEFAULT 0,
        completed_at DATETIME,
        experiences_count INTEGER DEFAULT 0,
        pages_scraped INTEGER DEFAULT 0
    );

    -- Training runs table: Training history
    CREATE TABLE IF NOT EXISTS training_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at DATETIME NOT NULL,
        completed_at DATETIME,
        status TEXT DEFAULT 'running',
        experiences_used INTEGER DEFAULT 0,
        pages_used INTEGER DEFAULT 0,
        training_steps INTEGER DEFAULT 0,
        training_epochs INTEGER DEFAULT 0,
        learning_rate REAL,
        batch_size INTEGER,
        final_loss REAL,
        final_eval_loss REAL,
        model_path TEXT,
        gguf_path TEXT,
        gguf_size_mb REAL,
        gguf_checksum TEXT,
        gcs_path TEXT,
        deployed_to_local INTEGER DEFAULT 0,
        deployed_to_cloud INTEGER DEFAULT 0,
        error TEXT,
        error_traceback TEXT,
        config_json TEXT
    );

    -- Model deployments table
    CREATE TABLE IF NOT EXISTS model_deployments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        training_run_id INTEGER,
        model_name TEXT NOT NULL,
        model_version TEXT,
        model_path TEXT NOT NULL,
        gguf_path TEXT,
        checksum TEXT,
        model_size_mb REAL,
        deployed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        deployed_to TEXT,
        gcs_path TEXT,
        active INTEGER DEFAULT 1,
        notes TEXT
    );

    -- Create indexes for performance
    CREATE INDEX IF NOT EXISTS idx_experiences_timestamp ON experiences(timestamp);
    CREATE INDEX IF NOT EXISTS idx_experiences_used ON experiences(used_in_training);
    CREATE INDEX IF NOT EXISTS idx_scraped_url ON scraped_content(url);
    CREATE INDEX IF NOT EXISTS idx_goals_priority ON learning_goals(priority DESC);
    CREATE INDEX IF NOT EXISTS idx_training_status ON training_runs(status);
"""

# Schema migrations - each migration adds columns/indexes that may be missing
# Format: (version, [(table, column, definition), ...])
SCHEMA_MIGRATIONS = {
    # Migration from version 1 to 2: Add missing columns from sync schema
    2: [
        # experiences table additions
        ("experiences", "feedback", "TEXT"),
        ("experiences", "correction", "TEXT"),
        # scraped_content table additions
        ("scraped_content", "content_type", "TEXT DEFAULT 'documentation'"),
        ("scraped_content", "language", "TEXT DEFAULT 'en'"),
        ("scraped_content", "word_count", "INTEGER"),
        ("scraped_content", "code_blocks", "INTEGER DEFAULT 0"),
        ("scraped_content", "training_run_id", "INTEGER"),
        # learning_goals table additions
        ("learning_goals", "description", "TEXT"),
        ("learning_goals", "keywords", "TEXT"),
        ("learning_goals", "started_at", "DATETIME"),
        ("learning_goals", "completed_at", "DATETIME"),
        ("learning_goals", "experiences_count", "INTEGER DEFAULT 0"),
        ("learning_goals", "pages_scraped", "INTEGER DEFAULT 0"),
        # training_runs table additions
        ("training_runs", "training_epochs", "INTEGER DEFAULT 0"),
        ("training_runs", "learning_rate", "REAL"),
        ("training_runs", "batch_size", "INTEGER"),
        ("training_runs", "final_eval_loss", "REAL"),
        ("training_runs", "gguf_size_mb", "REAL"),
        ("training_runs", "gguf_checksum", "TEXT"),
        ("training_runs", "deployed_to_local", "INTEGER DEFAULT 0"),
        ("training_runs", "deployed_to_cloud", "INTEGER DEFAULT 0"),
        ("training_runs", "error_traceback", "TEXT"),
        ("training_runs", "config_json", "TEXT"),
    ],
}


def _get_table_columns_sync(cursor, table_name: str) -> set:
    """Get existing columns for a table (sync version)."""
    try:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}
    except Exception:
        return set()


def _get_current_schema_version_sync(cursor) -> int:
    """Get current schema version (sync version)."""
    try:
        cursor.execute("SELECT version FROM flywheel_schema_version WHERE id = 1")
        row = cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _set_schema_version_sync(cursor, version: int) -> None:
    """Set schema version (sync version)."""
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO flywheel_schema_version (id, version, updated_at)
            VALUES (1, ?, CURRENT_TIMESTAMP)
        """, (version,))
    except Exception as e:
        logger.warning(f"[Flywheel] Failed to set schema version: {e}")


def run_schema_migrations_sync(conn, cursor) -> bool:
    """
    Run schema migrations on a sync SQLite connection.

    Returns:
        True if migrations were successful
    """
    try:
        # First, create base schema (tables that don't exist)
        cursor.executescript(UNIFIED_SCHEMA)
        conn.commit()

        # Get current version
        current_version = _get_current_schema_version_sync(cursor)
        logger.debug(f"[Flywheel] Current schema version: {current_version}")

        if current_version >= SCHEMA_VERSION:
            logger.debug("[Flywheel] Schema is up to date")
            return True

        # Run migrations
        migrations_run = 0
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            if version not in SCHEMA_MIGRATIONS:
                continue

            logger.info(f"[Flywheel] Running migration to version {version}...")

            for table, column, definition in SCHEMA_MIGRATIONS[version]:
                existing_columns = _get_table_columns_sync(cursor, table)

                if column not in existing_columns:
                    try:
                        alter_sql = f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        cursor.execute(alter_sql)
                        logger.debug(f"[Flywheel] Added column {table}.{column}")
                        migrations_run += 1
                    except Exception as e:
                        # Column might already exist or table doesn't exist
                        logger.debug(f"[Flywheel] Migration skipped {table}.{column}: {e}")

            conn.commit()

        # Update schema version
        _set_schema_version_sync(cursor, SCHEMA_VERSION)
        conn.commit()

        if migrations_run > 0:
            logger.info(f"[Flywheel] ✅ Schema migrated to version {SCHEMA_VERSION} ({migrations_run} columns added)")
        else:
            logger.debug(f"[Flywheel] Schema updated to version {SCHEMA_VERSION} (no new columns needed)")

        return True

    except Exception as e:
        logger.error(f"[Flywheel] Schema migration failed: {e}")
        return False


async def _get_table_columns_async(conn, table_name: str) -> set:
    """Get existing columns for a table (async version)."""
    try:
        async with conn.execute(f"PRAGMA table_info({table_name})") as cursor:
            rows = await cursor.fetchall()
            return {row[1] for row in rows}
    except Exception:
        return set()


async def _get_current_schema_version_async(conn) -> int:
    """Get current schema version (async version)."""
    try:
        async with conn.execute("SELECT version FROM flywheel_schema_version WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


async def _set_schema_version_async(conn, version: int) -> None:
    """Set schema version (async version)."""
    try:
        await conn.execute("""
            INSERT OR REPLACE INTO flywheel_schema_version (id, version, updated_at)
            VALUES (1, ?, CURRENT_TIMESTAMP)
        """, (version,))
    except Exception as e:
        logger.warning(f"[Flywheel] Failed to set schema version: {e}")


async def run_schema_migrations_async(conn) -> bool:
    """
    Run schema migrations on an async aiosqlite connection.

    Returns:
        True if migrations were successful
    """
    try:
        # First, create base schema (tables that don't exist)
        await conn.executescript(UNIFIED_SCHEMA)
        await conn.commit()

        # Get current version
        current_version = await _get_current_schema_version_async(conn)
        logger.debug(f"[Flywheel] Current schema version: {current_version}")

        if current_version >= SCHEMA_VERSION:
            logger.debug("[Flywheel] Schema is up to date")
            return True

        # Run migrations
        migrations_run = 0
        for version in range(current_version + 1, SCHEMA_VERSION + 1):
            if version not in SCHEMA_MIGRATIONS:
                continue

            logger.info(f"[Flywheel] Running migration to version {version}...")

            for table, column, definition in SCHEMA_MIGRATIONS[version]:
                existing_columns = await _get_table_columns_async(conn, table)

                if column not in existing_columns:
                    try:
                        alter_sql = f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        await conn.execute(alter_sql)
                        logger.debug(f"[Flywheel] Added column {table}.{column}")
                        migrations_run += 1
                    except Exception as e:
                        # Column might already exist or table doesn't exist
                        logger.debug(f"[Flywheel] Migration skipped {table}.{column}: {e}")

            await conn.commit()

        # Update schema version
        await _set_schema_version_async(conn, SCHEMA_VERSION)
        await conn.commit()

        if migrations_run > 0:
            logger.info(f"[Flywheel] ✅ Schema migrated to version {SCHEMA_VERSION} ({migrations_run} columns added)")
        else:
            logger.debug(f"[Flywheel] Schema updated to version {SCHEMA_VERSION} (no new columns needed)")

        return True

    except Exception as e:
        logger.error(f"[Flywheel] Schema migration failed: {e}")
        return False


# =============================================================================
# Unified Data Flywheel Orchestrator
# =============================================================================

class UnifiedDataFlywheel:
    """
    Orchestrates the complete data flywheel across all JARVIS repositories.

    Connects:
    - JARVIS-AI-Agent (experience telemetry, observability)
    - reactor-core (Scout scraping, training, GGUF export)
    - JARVIS-Prime (model deployment, inference)
    """

    def __init__(self, config: Optional[FlywheelConfig] = None):
        self.config = config or FlywheelConfig()
        self._progress = FlywheelProgress()
        self._running = False
        self._cancelled = False
        self._progress_callbacks: List[Callable[[FlywheelProgress], None]] = []

        # Components (lazy loaded)
        self._jarvis_connector = None
        self._scout = None
        self._trainer = None
        self._watcher = None
        self._observability = None

        # v9.0: SQLite Training Database
        self._training_db = None
        self._training_db_conn = None

        # v9.4: Async SQLite connection pool
        self._async_db_conn: Optional[aiosqlite.Connection] = None if AIOSQLITE_AVAILABLE else None
        self._db_lock = asyncio.Lock()

        # Last training timestamp
        self._last_training: Optional[datetime] = None

        logger.info("[UnifiedDataFlywheel] Initialized")

    @property
    def progress(self) -> FlywheelProgress:
        """Get current progress."""
        return self._progress

    @property
    def is_running(self) -> bool:
        """Check if flywheel is currently running."""
        return self._running

    def register_progress_callback(
        self,
        callback: Callable[[FlywheelProgress], None]
    ) -> None:
        """Register a callback for progress updates."""
        self._progress_callbacks.append(callback)

    def _notify_progress(self) -> None:
        """Notify all progress callbacks."""
        for callback in self._progress_callbacks:
            try:
                callback(self._progress)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    def _update_stage(self, stage: FlywheelStage) -> None:
        """Update current stage."""
        self._progress.stage = stage
        self._notify_progress()
        logger.info(f"[Flywheel] Stage: {stage.value}")

    async def _init_components(self) -> None:
        """Initialize all components lazily."""
        # Add reactor-core to path if not already
        reactor_core_path = str(self.config.reactor_core_repo)
        if reactor_core_path not in sys.path:
            sys.path.insert(0, reactor_core_path)

        # Import reactor-core components
        try:
            from reactor_core.integration.jarvis_connector import (
                JARVISConnector,
                JARVISConnectorConfig,
            )
            self._jarvis_connector = JARVISConnector(
                JARVISConnectorConfig(
                    jarvis_repo_path=self.config.jarvis_repo,
                    lookback_hours=self.config.experience_lookback_hours,
                )
            )
            logger.info("[Flywheel] JARVISConnector initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] JARVISConnector not available: {e}")

        try:
            from reactor_core.scout.safe_scout_orchestrator import (
                SafeScoutOrchestrator,
                ScoutConfig,
            )
            self._scout = SafeScoutOrchestrator(
                ScoutConfig(
                    work_dir=self.config.reactor_core_repo / "work",
                    max_pages_per_topic=self.config.scout_max_pages_per_topic,
                )
            )
            logger.info("[Flywheel] SafeScoutOrchestrator initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] SafeScoutOrchestrator not available: {e}")

        try:
            from reactor_core.training.trainer import AsyncTrainer, TrainingConfig
            self._trainer_class = AsyncTrainer
            self._training_config_class = TrainingConfig
            logger.info("[Flywheel] AsyncTrainer initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] AsyncTrainer not available: {e}")
            self._trainer_class = None

        # Import local components
        try:
            from backend.autonomy.reactor_core_watcher import get_reactor_core_watcher
            self._watcher = get_reactor_core_watcher()
            logger.info("[Flywheel] ReactorCoreWatcher initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] ReactorCoreWatcher not available: {e}")

        try:
            from backend.observability import get_observability_hub
            self._observability = get_observability_hub()
            logger.info("[Flywheel] ObservabilityHub initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] ObservabilityHub not available: {e}")

        # v9.0: Initialize SQLite Training Database
        if self.config.training_db_enabled:
            await self._init_training_database()

    async def _init_training_database(self) -> None:
        """
        v10.0: Initialize SQLite training database with intelligent schema migration.

        The training database stores:
        - Experiences from JARVIS interactions
        - Scraped web content
        - Learning goals and topics
        - Training run history
        - Model deployment records

        v10.0 IMPROVEMENTS:
        - Uses unified schema definition (single source of truth)
        - Automatic schema migration for existing databases
        - Column existence checks before ALTER TABLE
        - Schema version tracking for reliable upgrades
        """
        import sqlite3

        try:
            # Ensure directory exists
            db_path = Path(self.config.training_db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)

            # Connect to database
            self._training_db_conn = sqlite3.connect(str(db_path))
            cursor = self._training_db_conn.cursor()

            # v10.0: Run schema migrations (creates tables + adds missing columns)
            migration_success = run_schema_migrations_sync(
                self._training_db_conn,
                cursor
            )

            if not migration_success:
                logger.warning("[Flywheel] Schema migration had issues, database may be incomplete")

            self._training_db = db_path

            # Get stats for logging
            cursor.execute("SELECT COUNT(*) FROM experiences")
            exp_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM scraped_content")
            scraped_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM learning_goals WHERE completed = 0")
            goals_count = cursor.fetchone()[0]

            # Get schema version
            schema_version = _get_current_schema_version_sync(cursor)

            logger.info(
                f"[Flywheel] SQLite Training Database initialized: {db_path} "
                f"(schema v{schema_version}, experiences: {exp_count}, "
                f"scraped: {scraped_count}, pending goals: {goals_count})"
            )

        except Exception as e:
            logger.error(f"[Flywheel] Failed to initialize training database: {e}")
            self._training_db = None
            self._training_db_conn = None

    def add_experience(
        self,
        source: str,
        input_text: str,
        output_text: str,
        context: Optional[Dict[str, Any]] = None,
        quality_score: float = 0.5
    ) -> Optional[int]:
        """
        Add an experience to the training database.

        Args:
            source: Source of the experience (voice, text, api, automation)
            input_text: User's input
            output_text: JARVIS's response
            context: Additional context (JSON serializable)
            quality_score: Quality score (0.0 to 1.0)

        Returns:
            Experience ID if successful, None otherwise
        """
        if not self._training_db_conn:
            logger.warning("[Flywheel] Training database not initialized")
            return None

        try:
            import time
            cursor = self._training_db_conn.cursor()

            context_json = json.dumps(context) if context else None

            cursor.execute("""
                INSERT INTO experiences (timestamp, source, input_text, output_text, context, quality_score)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (time.time(), source, input_text, output_text, context_json, quality_score))

            self._training_db_conn.commit()
            experience_id = cursor.lastrowid

            logger.debug(f"[Flywheel] Added experience {experience_id} from {source}")
            return experience_id

        except Exception as e:
            logger.error(f"[Flywheel] Failed to add experience: {e}")
            return None

    def add_scraped_content(
        self,
        url: str,
        title: str,
        content: str,
        topic: Optional[str] = None,
        quality_score: float = 0.5
    ) -> Optional[int]:
        """
        Add scraped web content to the training database.

        Args:
            url: Source URL
            title: Page title
            content: Processed text content
            topic: Associated learning topic
            quality_score: Quality score (0.0 to 1.0)

        Returns:
            Content ID if successful, None otherwise
        """
        if not self._training_db_conn:
            logger.warning("[Flywheel] Training database not initialized")
            return None

        try:
            cursor = self._training_db_conn.cursor()

            cursor.execute("""
                INSERT OR REPLACE INTO scraped_content (url, title, content, topic, quality_score, scraped_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (url, title, content, topic, quality_score))

            self._training_db_conn.commit()
            content_id = cursor.lastrowid

            logger.debug(f"[Flywheel] Added scraped content {content_id} from {url}")
            return content_id

        except Exception as e:
            logger.error(f"[Flywheel] Failed to add scraped content: {e}")
            return None

    def add_learning_goal(
        self,
        topic: str,
        priority: int = 5,
        source: str = "auto",
        urls: Optional[List[str]] = None
    ) -> bool:
        """
        Add a learning goal to the training database.

        Args:
            topic: Topic name
            priority: Priority (1-10)
            source: Source of the goal (auto, user, trending)
            urls: URLs to scrape for this topic

        Returns:
            True if added successfully
        """
        if not self._training_db_conn:
            logger.warning("[Flywheel] Training database not initialized")
            return False

        try:
            cursor = self._training_db_conn.cursor()

            urls_str = ",".join(urls) if urls else None

            cursor.execute("""
                INSERT OR IGNORE INTO learning_goals (topic, priority, source, urls)
                VALUES (?, ?, ?, ?)
            """, (topic, priority, source, urls_str))

            self._training_db_conn.commit()
            return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"[Flywheel] Failed to add learning goal: {e}")
            return False

    def get_training_db_stats(self) -> Dict[str, Any]:
        """Get statistics from the training database."""
        if not self._training_db_conn:
            return {"error": "Training database not initialized"}

        try:
            cursor = self._training_db_conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM experiences")
            total_experiences = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM experiences WHERE used_in_training = 0")
            unused_experiences = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM scraped_content")
            total_scraped = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM learning_goals WHERE completed = 0")
            pending_goals = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM training_runs WHERE status = 'completed'")
            completed_runs = cursor.fetchone()[0]

            return {
                "total_experiences": total_experiences,
                "unused_experiences": unused_experiences,
                "total_scraped": total_scraped,
                "pending_goals": pending_goals,
                "completed_training_runs": completed_runs,
                "db_path": str(self._training_db) if self._training_db else None,
            }

        except Exception as e:
            logger.error(f"[Flywheel] Failed to get training DB stats: {e}")
            return {"error": str(e)}

    # =========================================================================
    # v9.4: Async SQLite Methods for Enhanced Performance
    # =========================================================================

    async def _get_async_db(self) -> Optional["aiosqlite.Connection"]:
        """
        Get or create async database connection with intelligent schema migration.

        v10.0 IMPROVEMENTS:
        - Uses unified schema definition (single source of truth)
        - Automatic schema migration for existing databases
        - Column existence checks before ALTER TABLE
        - Schema version tracking for reliable upgrades
        """
        if not AIOSQLITE_AVAILABLE:
            return None

        async with self._db_lock:
            if self._async_db_conn is None:
                db_path = Path(self.config.training_db_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)

                self._async_db_conn = await aiosqlite.connect(str(db_path))
                self._async_db_conn.row_factory = aiosqlite.Row

                # v10.0: Run schema migrations (creates tables + adds missing columns)
                migration_success = await run_schema_migrations_async(self._async_db_conn)

                if not migration_success:
                    logger.warning("[Flywheel] Async schema migration had issues")

                # Create additional async-only tables and indexes
                # (these are extras that may not be in the core schema)
                await self._async_db_conn.executescript("""
                    CREATE TABLE IF NOT EXISTS intelligence_insights (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        insight_type TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        source_systems TEXT,
                        confidence REAL DEFAULT 0.5,
                        description TEXT NOT NULL,
                        data_json TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        acted_upon INTEGER DEFAULT 0,
                        outcome TEXT
                    );

                    CREATE INDEX IF NOT EXISTS idx_experiences_quality ON experiences(quality_score DESC);
                    CREATE INDEX IF NOT EXISTS idx_scraped_topic ON scraped_content(topic);
                    CREATE INDEX IF NOT EXISTS idx_scraped_quality ON scraped_content(quality_score DESC);
                    CREATE INDEX IF NOT EXISTS idx_goals_completed ON learning_goals(completed);
                    CREATE INDEX IF NOT EXISTS idx_deployments_active ON model_deployments(active);
                """)
                await self._async_db_conn.commit()

                # Get schema version for logging
                schema_version = await _get_current_schema_version_async(self._async_db_conn)
                logger.info(f"[Flywheel] Async SQLite connection established: {db_path} (schema v{schema_version})")

            return self._async_db_conn

    async def add_experience_async(
        self,
        source: str,
        input_text: str,
        output_text: str,
        context: Optional[Dict[str, Any]] = None,
        quality_score: float = 0.5,
        feedback: Optional[str] = None,
        correction: Optional[str] = None
    ) -> Optional[int]:
        """
        v9.4: Add an experience to the training database asynchronously.

        Args:
            source: Source of the experience (voice, text, api, automation)
            input_text: User's input
            output_text: JARVIS's response
            context: Additional context (JSON serializable)
            quality_score: Quality score (0.0 to 1.0)
            feedback: User feedback (positive, negative, corrected)
            correction: If feedback is 'corrected', the correct response

        Returns:
            Experience ID if successful, None otherwise
        """
        conn = await self._get_async_db()
        if not conn:
            # Fallback to sync method
            return self.add_experience(source, input_text, output_text, context, quality_score)

        try:
            context_json = json.dumps(context) if context else None

            async with conn.execute("""
                INSERT INTO experiences (timestamp, source, input_text, output_text, context, quality_score, feedback, correction)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (time.time(), source, input_text, output_text, context_json, quality_score, feedback, correction)) as cursor:
                experience_id = cursor.lastrowid

            await conn.commit()
            logger.debug(f"[Flywheel] Added async experience {experience_id} from {source}")
            return experience_id

        except Exception as e:
            logger.error(f"[Flywheel] Failed to add async experience: {e}")
            return None

    async def get_unused_experiences_async(
        self,
        limit: int = 1000,
        min_quality: float = 0.3,
        sources: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        v9.4: Get experiences not yet used in training (async).

        Args:
            limit: Maximum number of experiences to return
            min_quality: Minimum quality score
            sources: Filter by source types (optional)

        Returns:
            List of experience dictionaries
        """
        conn = await self._get_async_db()
        if not conn:
            return []

        try:
            query = """
                SELECT id, timestamp, source, input_text, output_text, context, quality_score, feedback, correction
                FROM experiences
                WHERE used_in_training = 0 AND quality_score >= ?
            """
            params: List[Any] = [min_quality]

            if sources:
                placeholders = ",".join("?" * len(sources))
                query += f" AND source IN ({placeholders})"
                params.extend(sources)

            query += " ORDER BY quality_score DESC, timestamp DESC LIMIT ?"
            params.append(limit)

            experiences = []
            async with conn.execute(query, params) as cursor:
                async for row in cursor:
                    experiences.append({
                        "id": row[0],
                        "timestamp": row[1],
                        "source": row[2],
                        "input": row[3],
                        "output": row[4],
                        "context": json.loads(row[5]) if row[5] else None,
                        "quality": row[6],
                        "feedback": row[7],
                        "correction": row[8],
                    })

            return experiences

        except Exception as e:
            logger.error(f"[Flywheel] Failed to get unused experiences: {e}")
            return []

    async def mark_experiences_used_async(
        self,
        experience_ids: List[int],
        training_run_id: Optional[int] = None
    ) -> bool:
        """
        v9.4: Mark experiences as used in training (async).

        Args:
            experience_ids: List of experience IDs to mark
            training_run_id: Optional training run ID to associate

        Returns:
            True if successful
        """
        if not experience_ids:
            return True

        conn = await self._get_async_db()
        if not conn:
            return False

        try:
            placeholders = ",".join("?" * len(experience_ids))
            if training_run_id:
                await conn.execute(f"""
                    UPDATE experiences SET used_in_training = 1, training_run_id = ?
                    WHERE id IN ({placeholders})
                """, [training_run_id] + experience_ids)
            else:
                await conn.execute(f"""
                    UPDATE experiences SET used_in_training = 1
                    WHERE id IN ({placeholders})
                """, experience_ids)

            await conn.commit()
            logger.debug(f"[Flywheel] Marked {len(experience_ids)} experiences as used")
            return True

        except Exception as e:
            logger.error(f"[Flywheel] Failed to mark experiences used: {e}")
            return False

    async def get_pending_learning_goals_async(
        self,
        limit: int = 10,
        min_priority: int = 1
    ) -> List[Dict[str, Any]]:
        """
        v9.4: Get pending learning goals sorted by priority (async).

        Args:
            limit: Maximum number of goals to return
            min_priority: Minimum priority (1-10)

        Returns:
            List of learning goal dictionaries
        """
        conn = await self._get_async_db()
        if not conn:
            return []

        try:
            goals = []
            async with conn.execute("""
                SELECT id, topic, description, priority, source, urls, keywords, discovered_at, experiences_count, pages_scraped
                FROM learning_goals
                WHERE completed = 0 AND priority >= ?
                ORDER BY priority DESC, discovered_at DESC
                LIMIT ?
            """, (min_priority, limit)) as cursor:
                async for row in cursor:
                    goals.append({
                        "id": row[0],
                        "topic": row[1],
                        "description": row[2],
                        "priority": row[3],
                        "source": row[4],
                        "urls": row[5].split(",") if row[5] else [],
                        "keywords": row[6].split(",") if row[6] else [],
                        "discovered_at": row[7],
                        "experiences_count": row[8],
                        "pages_scraped": row[9],
                    })

            return goals

        except Exception as e:
            logger.error(f"[Flywheel] Failed to get pending goals: {e}")
            return []

    async def start_training_run_async(
        self,
        config: Optional[Dict[str, Any]] = None
    ) -> Optional[int]:
        """
        v9.4: Record the start of a training run (async).

        Args:
            config: Training configuration to store

        Returns:
            Training run ID if successful
        """
        conn = await self._get_async_db()
        if not conn:
            return None

        try:
            config_json = json.dumps(config) if config else None

            async with conn.execute("""
                INSERT INTO training_runs (started_at, status, config_json)
                VALUES (CURRENT_TIMESTAMP, 'running', ?)
            """, (config_json,)) as cursor:
                run_id = cursor.lastrowid

            await conn.commit()
            logger.info(f"[Flywheel] Started training run {run_id}")
            return run_id

        except Exception as e:
            logger.error(f"[Flywheel] Failed to start training run: {e}")
            return None

    async def complete_training_run_async(
        self,
        run_id: int,
        status: str,
        experiences_used: int = 0,
        pages_used: int = 0,
        training_steps: int = 0,
        training_epochs: int = 0,
        final_loss: Optional[float] = None,
        model_path: Optional[str] = None,
        gguf_path: Optional[str] = None,
        gguf_size_mb: Optional[float] = None,
        gcs_path: Optional[str] = None,
        deployed_local: bool = False,
        deployed_cloud: bool = False,
        error: Optional[str] = None
    ) -> bool:
        """
        v9.4: Record the completion of a training run (async).

        Args:
            run_id: Training run ID
            status: Final status (completed, failed, cancelled)
            experiences_used: Number of experiences used
            pages_used: Number of scraped pages used
            training_steps: Total training steps
            training_epochs: Total training epochs
            final_loss: Final training loss
            model_path: Path to trained model
            gguf_path: Path to GGUF export
            gguf_size_mb: GGUF file size in MB
            gcs_path: GCS upload path
            deployed_local: Whether deployed locally
            deployed_cloud: Whether deployed to cloud
            error: Error message if failed

        Returns:
            True if successful
        """
        conn = await self._get_async_db()
        if not conn:
            return False

        try:
            await conn.execute("""
                UPDATE training_runs SET
                    completed_at = CURRENT_TIMESTAMP,
                    status = ?,
                    experiences_used = ?,
                    pages_used = ?,
                    training_steps = ?,
                    training_epochs = ?,
                    final_loss = ?,
                    model_path = ?,
                    gguf_path = ?,
                    gguf_size_mb = ?,
                    gcs_path = ?,
                    deployed_to_local = ?,
                    deployed_to_cloud = ?,
                    error = ?
                WHERE id = ?
            """, (
                status, experiences_used, pages_used, training_steps, training_epochs,
                final_loss, model_path, gguf_path, gguf_size_mb, gcs_path,
                1 if deployed_local else 0, 1 if deployed_cloud else 0, error, run_id
            ))

            await conn.commit()
            logger.info(f"[Flywheel] Completed training run {run_id}: {status}")
            return True

        except Exception as e:
            logger.error(f"[Flywheel] Failed to complete training run: {e}")
            return False

    async def get_training_stats_async(self) -> Dict[str, Any]:
        """
        v9.4: Get comprehensive training database statistics (async).

        Returns:
            Dictionary of statistics
        """
        conn = await self._get_async_db()
        if not conn:
            return self.get_training_db_stats()

        try:
            stats = {}

            # Experience stats
            async with conn.execute("SELECT COUNT(*) FROM experiences") as cursor:
                stats["total_experiences"] = (await cursor.fetchone())[0]

            async with conn.execute("SELECT COUNT(*) FROM experiences WHERE used_in_training = 0") as cursor:
                stats["unused_experiences"] = (await cursor.fetchone())[0]

            async with conn.execute("SELECT AVG(quality_score) FROM experiences") as cursor:
                result = await cursor.fetchone()
                stats["avg_experience_quality"] = round(result[0] or 0, 3)

            # Scraped content stats
            async with conn.execute("SELECT COUNT(*) FROM scraped_content") as cursor:
                stats["total_scraped_pages"] = (await cursor.fetchone())[0]

            async with conn.execute("SELECT COUNT(*) FROM scraped_content WHERE used_in_training = 0") as cursor:
                stats["unused_scraped_pages"] = (await cursor.fetchone())[0]

            async with conn.execute("SELECT SUM(word_count) FROM scraped_content") as cursor:
                result = await cursor.fetchone()
                stats["total_scraped_words"] = result[0] or 0

            # Learning goals stats
            async with conn.execute("SELECT COUNT(*) FROM learning_goals") as cursor:
                stats["total_learning_goals"] = (await cursor.fetchone())[0]

            async with conn.execute("SELECT COUNT(*) FROM learning_goals WHERE completed = 0") as cursor:
                stats["pending_learning_goals"] = (await cursor.fetchone())[0]

            async with conn.execute("SELECT COUNT(*) FROM learning_goals WHERE completed = 1") as cursor:
                stats["completed_learning_goals"] = (await cursor.fetchone())[0]

            # Training runs stats
            async with conn.execute("SELECT COUNT(*) FROM training_runs") as cursor:
                stats["total_training_runs"] = (await cursor.fetchone())[0]

            async with conn.execute("SELECT COUNT(*) FROM training_runs WHERE status = 'completed'") as cursor:
                stats["successful_training_runs"] = (await cursor.fetchone())[0]

            async with conn.execute("SELECT MIN(final_loss) FROM training_runs WHERE final_loss IS NOT NULL") as cursor:
                result = await cursor.fetchone()
                stats["best_training_loss"] = round(result[0] or 0, 4) if result[0] else None

            # Model deployment stats
            async with conn.execute("SELECT COUNT(*) FROM model_deployments") as cursor:
                stats["total_deployments"] = (await cursor.fetchone())[0]

            async with conn.execute("SELECT COUNT(*) FROM model_deployments WHERE active = 1") as cursor:
                stats["active_deployments"] = (await cursor.fetchone())[0]

            # Recent activity
            async with conn.execute("""
                SELECT COUNT(*) FROM experiences
                WHERE timestamp > ?
            """, (time.time() - 86400,)) as cursor:
                stats["experiences_last_24h"] = (await cursor.fetchone())[0]

            async with conn.execute("""
                SELECT COUNT(*) FROM scraped_content
                WHERE scraped_at > datetime('now', '-24 hours')
            """) as cursor:
                stats["pages_scraped_last_24h"] = (await cursor.fetchone())[0]

            stats["db_path"] = str(self.config.training_db_path)
            stats["async_enabled"] = True

            return stats

        except Exception as e:
            logger.error(f"[Flywheel] Failed to get async training stats: {e}")
            return {"error": str(e)}

    async def query_experiences_async(
        self,
        query: str,
        params: Optional[Tuple] = None
    ) -> List[Dict[str, Any]]:
        """
        v9.4: Execute a custom query on the experiences table (async).

        Args:
            query: SQL query (SELECT only)
            params: Query parameters

        Returns:
            List of result rows as dictionaries
        """
        if not query.strip().upper().startswith("SELECT"):
            logger.error("[Flywheel] Only SELECT queries are allowed")
            return []

        conn = await self._get_async_db()
        if not conn:
            return []

        try:
            results = []
            async with conn.execute(query, params or ()) as cursor:
                columns = [desc[0] for desc in cursor.description]
                async for row in cursor:
                    results.append(dict(zip(columns, row)))

            return results

        except Exception as e:
            logger.error(f"[Flywheel] Query failed: {e}")
            return []

    async def add_intelligence_insight_async(
        self,
        insight_type: str,
        topic: str,
        description: str,
        source_systems: Optional[List[str]] = None,
        confidence: float = 0.5,
        data: Optional[Dict[str, Any]] = None
    ) -> Optional[int]:
        """
        v9.4: Add an intelligence insight from CAI system (async).

        Args:
            insight_type: Type of insight (pattern, recommendation, anomaly)
            topic: Related topic
            description: Insight description
            source_systems: Contributing systems (uae, sai, mas, cai)
            confidence: Confidence score (0.0 to 1.0)
            data: Additional data

        Returns:
            Insight ID if successful
        """
        conn = await self._get_async_db()
        if not conn:
            return None

        try:
            sources_str = ",".join(source_systems) if source_systems else None
            data_json = json.dumps(data) if data else None

            async with conn.execute("""
                INSERT INTO intelligence_insights (insight_type, topic, source_systems, confidence, description, data_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (insight_type, topic, sources_str, confidence, description, data_json)) as cursor:
                insight_id = cursor.lastrowid

            await conn.commit()
            logger.debug(f"[Flywheel] Added intelligence insight {insight_id}")
            return insight_id

        except Exception as e:
            logger.error(f"[Flywheel] Failed to add intelligence insight: {e}")
            return None

    async def close_async_db(self) -> None:
        """v9.4: Close the async database connection."""
        async with self._db_lock:
            if self._async_db_conn:
                await self._async_db_conn.close()
                self._async_db_conn = None
                logger.info("[Flywheel] Async SQLite connection closed")

    async def _cleanup_components(self) -> None:
        """Cleanup all components."""
        if self._scout:
            try:
                await self._scout.cancel()
            except Exception:
                pass

        if self._watcher:
            try:
                await self._watcher.stop()
            except Exception:
                pass

    async def run_full_cycle(
        self,
        include_web_scraping: bool = True,
        include_training: bool = True,
        force: bool = False,
    ) -> FlywheelResult:
        """
        Run a full data flywheel cycle.

        Args:
            include_web_scraping: Whether to scrape web documentation
            include_training: Whether to run training after data collection
            force: Force training even if cooldown hasn't elapsed

        Returns:
            FlywheelResult with execution details
        """
        import uuid
        run_id = f"flywheel_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        if self._running:
            return FlywheelResult(
                success=False,
                run_id=run_id,
                progress=self._progress,
                error="Flywheel already running",
            )

        # Check cooldown
        if (
            not force
            and self._last_training
            and (datetime.now() - self._last_training).total_seconds()
            < self.config.training_cooldown_hours * 3600
        ):
            hours_remaining = (
                self.config.training_cooldown_hours
                - (datetime.now() - self._last_training).total_seconds() / 3600
            )
            return FlywheelResult(
                success=False,
                run_id=run_id,
                progress=self._progress,
                error=f"Training cooldown: {hours_remaining:.1f}h remaining",
            )

        self._running = True
        self._cancelled = False
        self._progress = FlywheelProgress(started_at=datetime.now())

        try:
            # Initialize components
            await self._init_components()

            # Start watcher for auto-deployment
            if self._watcher:
                await self._watcher.start()

            # Phase 1: Collect experiences from JARVIS
            self._update_stage(FlywheelStage.COLLECTING_EXPERIENCES)
            experiences = await self._collect_experiences()

            if self._cancelled:
                return self._create_cancelled_result(run_id)

            # Phase 2: Collect web documentation (optional)
            web_examples = []
            if include_web_scraping and self._scout:
                self._update_stage(FlywheelStage.COLLECTING_WEB_DATA)
                web_examples = await self._collect_web_data()

            if self._cancelled:
                return self._create_cancelled_result(run_id)

            # Phase 3: Build dataset
            self._update_stage(FlywheelStage.BUILDING_DATASET)
            dataset_path = await self._build_dataset(experiences, web_examples)

            if not dataset_path:
                self._update_stage(FlywheelStage.FAILED)
                return FlywheelResult(
                    success=False,
                    run_id=run_id,
                    progress=self._progress,
                    error="Failed to build dataset",
                )

            if self._cancelled:
                return self._create_cancelled_result(run_id)

            # Phase 4: Train model (optional)
            model_path = None
            if include_training and self._trainer_class:
                # Check if we have enough data
                if (
                    self._progress.experiences_collected >= self.config.min_experiences_for_training
                    or self._progress.examples_synthesized >= self.config.min_web_examples_for_training
                ):
                    self._update_stage(FlywheelStage.TRAINING)
                    model_path = await self._train_model(dataset_path)

                    if model_path:
                        self._last_training = datetime.now()
                else:
                    logger.info(
                        f"[Flywheel] Insufficient data for training: "
                        f"{self._progress.experiences_collected} experiences, "
                        f"{self._progress.examples_synthesized} web examples"
                    )

            if self._cancelled:
                return self._create_cancelled_result(run_id)

            # Phase 5: Export to GGUF and deploy
            gcs_path = None
            if model_path:
                self._update_stage(FlywheelStage.EXPORTING_GGUF)
                gguf_path = await self._export_gguf(model_path)

                if gguf_path:
                    self._update_stage(FlywheelStage.DEPLOYING)
                    gcs_path = await self._deploy_model(gguf_path)

            # Complete
            self._update_stage(FlywheelStage.COMPLETED)
            self._progress.completed_at = datetime.now()

            # Record to observability
            if self._observability:
                await self._record_flywheel_run(run_id)

            return FlywheelResult(
                success=True,
                run_id=run_id,
                progress=self._progress,
                model_path=model_path,
                gcs_path=gcs_path,
            )

        except Exception as e:
            logger.exception(f"[Flywheel] Error: {e}")
            self._progress.errors.append(str(e))
            self._update_stage(FlywheelStage.FAILED)
            return FlywheelResult(
                success=False,
                run_id=run_id,
                progress=self._progress,
                error=str(e),
            )
        finally:
            self._running = False
            await self._cleanup_components()

    def _create_cancelled_result(self, run_id: str) -> FlywheelResult:
        """Create a cancelled result."""
        self._progress.completed_at = datetime.now()
        return FlywheelResult(
            success=False,
            run_id=run_id,
            progress=self._progress,
            error="Cancelled by user",
        )

    async def cancel(self) -> None:
        """Cancel the current flywheel execution."""
        self._cancelled = True
        if self._scout:
            await self._scout.cancel()
        logger.info("[Flywheel] Cancellation requested")

    # =========================================================================
    # Phase Implementations
    # =========================================================================

    async def _collect_experiences(self) -> List[Dict[str, Any]]:
        """Collect experiences from JARVIS-AI-Agent logs."""
        if not self._jarvis_connector:
            logger.warning("[Flywheel] JARVISConnector not available")
            return []

        try:
            # Get successful interactions
            interactions = await self._jarvis_connector.get_successful_interactions(
                min_confidence=0.85,
            )
            self._progress.experiences_collected = len(interactions)

            # Get corrections (valuable for training)
            corrections = await self._jarvis_connector.get_corrections()
            self._progress.corrections_found = len(corrections)

            # Convert to training format
            examples = []
            for event in interactions + corrections:
                if event.user_input and event.jarvis_response:
                    examples.append({
                        "instruction": event.user_input,
                        "output": event.jarvis_response,
                        "context": event.system_context,
                        "is_correction": event.is_correction,
                        "confidence": event.confidence,
                    })

            logger.info(
                f"[Flywheel] Collected {len(interactions)} experiences, "
                f"{len(corrections)} corrections"
            )
            return examples

        except Exception as e:
            logger.error(f"[Flywheel] Experience collection failed: {e}")
            self._progress.errors.append(f"Experience collection: {e}")
            return []

    async def _collect_web_data(self) -> List[Dict[str, Any]]:
        """Collect documentation from web scraping."""
        if not self._scout:
            logger.warning("[Flywheel] Scout not available")
            return []

        try:
            # Add topics to queue
            for topic in self.config.scout_topics:
                await self._scout.add_topic(topic)

            # Run scraping
            results = await self._scout.run()

            self._progress.web_pages_scraped = results.pages_fetched
            self._progress.examples_synthesized = results.examples_synthesized

            # Get synthesized examples
            examples = []
            output_dir = self.config.reactor_core_repo / "work" / "output"
            if output_dir.exists():
                for json_file in output_dir.glob("*.json"):
                    try:
                        data = json.loads(json_file.read_text())
                        if isinstance(data, list):
                            examples.extend(data)
                        elif isinstance(data, dict) and "pairs" in data:
                            examples.extend(data["pairs"])
                    except Exception:
                        continue

            logger.info(
                f"[Flywheel] Scraped {results.pages_fetched} pages, "
                f"synthesized {len(examples)} examples"
            )
            return examples

        except Exception as e:
            logger.error(f"[Flywheel] Web scraping failed: {e}")
            self._progress.errors.append(f"Web scraping: {e}")
            return []

    async def _build_dataset(
        self,
        experiences: List[Dict[str, Any]],
        web_examples: List[Dict[str, Any]],
    ) -> Optional[Path]:
        """Build training dataset from collected data."""
        all_examples = experiences + web_examples

        if not all_examples:
            logger.warning("[Flywheel] No examples to build dataset from")
            return None

        # Output path
        output_dir = self.config.reactor_core_repo / "datasets"
        output_dir.mkdir(parents=True, exist_ok=True)

        dataset_path = output_dir / f"jarvis_dataset_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

        # Convert to ChatML format and write
        with open(dataset_path, "w") as f:
            for example in all_examples:
                # Build ChatML conversation
                messages = []

                # Add system message if context exists
                if example.get("context"):
                    messages.append({
                        "role": "system",
                        "content": f"You are JARVIS, an intelligent AI assistant. Context: {example['context']}"
                    })
                else:
                    messages.append({
                        "role": "system",
                        "content": "You are JARVIS, an intelligent AI assistant."
                    })

                # Add user message
                messages.append({
                    "role": "user",
                    "content": example.get("instruction", example.get("input", ""))
                })

                # Add assistant response
                messages.append({
                    "role": "assistant",
                    "content": example.get("output", example.get("response", ""))
                })

                # Write as JSONL
                f.write(json.dumps({"messages": messages}) + "\n")

        # Update stats
        self._progress.dataset_examples = len(all_examples)
        self._progress.dataset_size_mb = dataset_path.stat().st_size / (1024 * 1024)

        logger.info(
            f"[Flywheel] Built dataset: {len(all_examples)} examples, "
            f"{self._progress.dataset_size_mb:.2f}MB"
        )
        return dataset_path

    async def _train_model(self, dataset_path: Path) -> Optional[str]:
        """Train the model using reactor-core's AsyncTrainer."""
        if not self._trainer_class:
            logger.warning("[Flywheel] AsyncTrainer not available")
            return None

        try:
            # Configure training
            config = self._training_config_class(
                model_name=self.config.base_model,
                num_epochs=3,
                batch_size=4,
                learning_rate=2e-5,
                use_lora=True,
                use_qlora=True,
                lora_rank=64,
            )

            # Create trainer
            trainer = self._trainer_class(config)

            # Register progress callback
            def on_progress(state):
                self._progress.current_epoch = state.get("epoch", 0)
                self._progress.current_loss = state.get("loss", 0.0)
                if state.get("loss", float('inf')) < self._progress.best_loss:
                    self._progress.best_loss = state["loss"]
                self._notify_progress()

            trainer.register_callback(on_progress)

            # Set epochs
            self._progress.training_epochs = config.num_epochs

            # Output directory
            output_dir = self.config.reactor_core_repo / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            output_name = f"{self.config.output_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

            # Run training
            result = await trainer.train(
                dataset_path=str(dataset_path),
                output_dir=str(output_dir),
                output_name=output_name,
            )

            if result.success:
                logger.info(f"[Flywheel] Training completed: {result.model_path}")
                return result.model_path
            else:
                logger.error(f"[Flywheel] Training failed: {result.error}")
                self._progress.errors.append(f"Training: {result.error}")
                return None

        except Exception as e:
            logger.error(f"[Flywheel] Training error: {e}")
            self._progress.errors.append(f"Training: {e}")
            return None

    async def _export_gguf(self, model_path: str) -> Optional[Path]:
        """Export trained model to GGUF format."""
        try:
            from reactor_core.export.gguf_exporter import GGUFExporter, ExportConfig

            exporter = GGUFExporter(
                ExportConfig(quantization=self.config.quantization)
            )

            output_dir = self.config.reactor_core_repo / "output"
            output_name = Path(model_path).stem

            result = await exporter.export(
                model_path=model_path,
                output_dir=str(output_dir),
                output_name=output_name,
            )

            if result.success:
                gguf_path = Path(result.gguf_path)
                self._progress.model_size_mb = gguf_path.stat().st_size / (1024 * 1024)
                logger.info(f"[Flywheel] GGUF exported: {gguf_path}")
                return gguf_path
            else:
                logger.error(f"[Flywheel] GGUF export failed: {result.error}")
                self._progress.errors.append(f"GGUF export: {result.error}")
                return None

        except ImportError:
            logger.warning("[Flywheel] GGUFExporter not available, skipping export")
            return None
        except Exception as e:
            logger.error(f"[Flywheel] GGUF export error: {e}")
            self._progress.errors.append(f"GGUF export: {e}")
            return None

    async def _deploy_model(self, gguf_path: Path) -> Optional[str]:
        """Deploy GGUF model using ReactorCoreWatcher."""
        if not self._watcher:
            logger.warning("[Flywheel] ReactorCoreWatcher not available")
            return None

        try:
            result = await self._watcher.deploy_model(gguf_path)

            self._progress.deployed_local = result.local_deployed
            self._progress.deployed_cloud = result.gcs_uploaded

            if result.success:
                logger.info(
                    f"[Flywheel] Model deployed: "
                    f"local={result.local_deployed}, cloud={result.gcs_uploaded}"
                )
                return result.gcs_path
            else:
                logger.error(f"[Flywheel] Deployment failed: {result.error}")
                self._progress.errors.append(f"Deployment: {result.error}")
                return None

        except Exception as e:
            logger.error(f"[Flywheel] Deployment error: {e}")
            self._progress.errors.append(f"Deployment: {e}")
            return None

    async def _record_flywheel_run(self, run_id: str) -> None:
        """Record flywheel run to observability hub."""
        if not self._observability:
            return

        try:
            await self._observability.record_event(
                event_type="flywheel_run",
                metadata={
                    "run_id": run_id,
                    "progress": self._progress.to_dict(),
                }
            )
        except Exception as e:
            logger.warning(f"[Flywheel] Failed to record to observability: {e}")

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    async def collect_only(self) -> FlywheelResult:
        """Run data collection only (no training)."""
        return await self.run_full_cycle(
            include_web_scraping=True,
            include_training=False,
        )

    async def train_from_experiences_only(self) -> FlywheelResult:
        """Train from experiences only (no web scraping)."""
        return await self.run_full_cycle(
            include_web_scraping=False,
            include_training=True,
        )

    async def quick_retrain(self) -> FlywheelResult:
        """Quick retrain using only recent experiences."""
        # Temporarily reduce lookback
        original_lookback = self.config.experience_lookback_hours
        self.config.experience_lookback_hours = 6

        try:
            return await self.run_full_cycle(
                include_web_scraping=False,
                include_training=True,
                force=True,
            )
        finally:
            self.config.experience_lookback_hours = original_lookback


# =============================================================================
# Singleton Access
# =============================================================================

_flywheel_instance: Optional[UnifiedDataFlywheel] = None


def get_data_flywheel() -> UnifiedDataFlywheel:
    """Get the global UnifiedDataFlywheel instance."""
    global _flywheel_instance
    if _flywheel_instance is None:
        _flywheel_instance = UnifiedDataFlywheel()
    return _flywheel_instance


async def run_flywheel_cycle(
    include_web_scraping: bool = True,
    include_training: bool = True,
    force: bool = False,
) -> FlywheelResult:
    """Run a data flywheel cycle using the global instance."""
    flywheel = get_data_flywheel()
    return await flywheel.run_full_cycle(
        include_web_scraping=include_web_scraping,
        include_training=include_training,
        force=force,
    )


async def get_flywheel_status() -> Dict[str, Any]:
    """Get current flywheel status."""
    flywheel = get_data_flywheel()
    return {
        "running": flywheel.is_running,
        "progress": flywheel.progress.to_dict(),
    }
