"""
Reactor-Core Integration Module v1.0.0
======================================

Connects JARVIS-AI-Agent with reactor-core for:
- Experience ingestion via JARVISConnector
- Web documentation scraping via SafeScoutOrchestrator
- JARVIS-Prime integration via PrimeConnector
- Training pipeline orchestration

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    JARVIS ↔ Reactor-Core Integration                    │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                         │
    │  ┌─────────────────────────────────────────────────────────────────┐   │
    │  │                     JARVIS-AI-Agent                              │   │
    │  │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐       │   │
    │  │  │ Voice Handler │  │ Agentic Runner│  │ Data Flywheel │       │   │
    │  │  └───────┬───────┘  └───────┬───────┘  └───────┬───────┘       │   │
    │  └──────────┼──────────────────┼──────────────────┼────────────────┘   │
    │             │                  │                  │                    │
    │             └──────────────────┼──────────────────┘                    │
    │                                │                                       │
    │                    ┌───────────▼───────────┐                           │
    │                    │  ReactorCoreIntegration │                          │
    │                    │  (This Module)         │                          │
    │                    └───────────┬───────────┘                           │
    │                                │                                       │
    │  ┌─────────────────────────────┼─────────────────────────────────────┐ │
    │  │                     reactor-core                                   │ │
    │  │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐         │ │
    │  │  │JARVISConnector│  │  SafeScout    │  │PrimeConnector │         │ │
    │  │  │(Experience)   │  │  (Web Docs)   │  │ (Real-time)   │         │ │
    │  │  └───────────────┘  └───────────────┘  └───────────────┘         │ │
    │  └───────────────────────────────────────────────────────────────────┘ │
    │                                │                                       │
    │                    ┌───────────▼───────────┐                           │
    │                    │  Training Pipeline    │                           │
    │                    │  (GGUF → GCS → Prime) │                           │
    │                    └───────────────────────┘                           │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, AsyncIterator

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class ReactorCoreConfig:
    """Configuration for reactor-core integration."""

    # Repository paths
    reactor_core_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "REACTOR_CORE_PATH",
            Path.home() / "Documents" / "repos" / "reactor-core"
        ))
    )
    jarvis_prime_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_PRIME_PATH",
            Path.home() / "Documents" / "repos" / "jarvis-prime"
        ))
    )

    # JARVIS Connector settings
    jarvis_connector_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_CONNECTOR_ENABLED", "true").lower() == "true"
    )
    experience_lookback_hours: int = field(
        default_factory=lambda: int(os.getenv("EXPERIENCE_LOOKBACK_HOURS", "168"))  # 1 week
    )
    enable_file_watching: bool = field(
        default_factory=lambda: os.getenv("JARVIS_FILE_WATCHING", "true").lower() == "true"
    )

    # Safe Scout settings
    scout_enabled: bool = field(
        default_factory=lambda: os.getenv("SCOUT_ENABLED", "true").lower() == "true"
    )
    scout_max_topics: int = field(
        default_factory=lambda: int(os.getenv("SCOUT_MAX_TOPICS", "50"))
    )
    scout_max_pages_per_topic: int = field(
        default_factory=lambda: int(os.getenv("SCOUT_MAX_PAGES", "10"))
    )
    scout_concurrency: int = field(
        default_factory=lambda: int(os.getenv("SCOUT_CONCURRENCY", "5"))
    )
    scout_use_docker: bool = field(
        default_factory=lambda: os.getenv("SCOUT_USE_DOCKER", "true").lower() == "true"
    )

    # Prime Connector settings
    prime_connector_enabled: bool = field(
        default_factory=lambda: os.getenv("PRIME_CONNECTOR_ENABLED", "true").lower() == "true"
    )
    prime_host: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_HOST", "localhost")
    )
    prime_port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8002"))
    )
    prime_websocket_enabled: bool = field(
        default_factory=lambda: os.getenv("PRIME_WEBSOCKET_ENABLED", "true").lower() == "true"
    )

    # Training pipeline settings
    training_enabled: bool = field(
        default_factory=lambda: os.getenv("REACTOR_TRAINING_ENABLED", "true").lower() == "true"
    )
    training_base_model: str = field(
        default_factory=lambda: os.getenv("TRAINING_BASE_MODEL", "meta-llama/Llama-3.2-3B")
    )
    training_quantization: str = field(
        default_factory=lambda: os.getenv("TRAINING_QUANTIZATION", "Q4_K_M")
    )


# =============================================================================
# Reactor-Core Integration
# =============================================================================

class ReactorCoreIntegration:
    """
    Unified integration point for reactor-core components.

    Provides lazy loading and async access to:
    - JARVISConnector: Experience ingestion from JARVIS logs
    - SafeScoutOrchestrator: Web documentation scraping
    - PrimeConnector: Real-time JARVIS-Prime integration
    - NightShiftPipeline: Training orchestration
    """

    def __init__(self, config: Optional[ReactorCoreConfig] = None):
        self.config = config or ReactorCoreConfig()
        self._initialized = False

        # Components (lazy loaded)
        self._jarvis_connector = None
        self._scout = None
        self._prime_connector = None
        self._pipeline = None

        # State
        self._event_callbacks: List[Callable] = []
        self._streaming_task: Optional[asyncio.Task] = None

        logger.info("[ReactorCore] Integration initialized")

    async def initialize(self) -> bool:
        """Initialize all available reactor-core components."""
        if self._initialized:
            return True

        logger.info("[ReactorCore] Initializing components...")

        # Add reactor-core to path
        reactor_path = str(self.config.reactor_core_path)
        if reactor_path not in sys.path:
            sys.path.insert(0, reactor_path)
            logger.debug(f"[ReactorCore] Added to path: {reactor_path}")

        # Initialize JARVIS Connector
        if self.config.jarvis_connector_enabled:
            await self._init_jarvis_connector()

        # Initialize Safe Scout
        if self.config.scout_enabled:
            await self._init_scout()

        # Initialize Prime Connector
        if self.config.prime_connector_enabled:
            await self._init_prime_connector()

        self._initialized = True
        logger.info("[ReactorCore] Initialization complete")
        return True

    async def _init_jarvis_connector(self) -> None:
        """Initialize JARVIS Connector for experience ingestion."""
        try:
            from reactor_core.integration.jarvis_connector import (
                JARVISConnector,
                JARVISConnectorConfig,
            )

            jarvis_repo = Path(__file__).parent.parent.parent  # JARVIS-AI-Agent root

            self._jarvis_connector = JARVISConnector(
                JARVISConnectorConfig(
                    jarvis_repo_path=jarvis_repo,
                    lookback_hours=self.config.experience_lookback_hours,
                    enable_file_watching=self.config.enable_file_watching,
                )
            )
            logger.info("[ReactorCore] ✓ JARVISConnector initialized")

        except ImportError as e:
            logger.warning(f"[ReactorCore] JARVISConnector not available: {e}")
        except Exception as e:
            logger.error(f"[ReactorCore] JARVISConnector init failed: {e}")

    async def _init_scout(self) -> None:
        """Initialize Safe Scout for web documentation scraping."""
        try:
            from reactor_core.scout.safe_scout_orchestrator import (
                SafeScoutOrchestrator,
                ScoutConfig,
            )

            self._scout = SafeScoutOrchestrator(
                ScoutConfig(
                    work_dir=self.config.reactor_core_path / "work",
                    max_topics=self.config.scout_max_topics,
                    max_pages_per_topic=self.config.scout_max_pages_per_topic,
                    url_concurrency=self.config.scout_concurrency,
                    use_docker=self.config.scout_use_docker,
                )
            )
            logger.info("[ReactorCore] ✓ SafeScoutOrchestrator initialized")

        except ImportError as e:
            logger.warning(f"[ReactorCore] SafeScout not available: {e}")
        except Exception as e:
            logger.error(f"[ReactorCore] SafeScout init failed: {e}")

    async def _init_prime_connector(self) -> None:
        """Initialize Prime Connector for JARVIS-Prime integration."""
        try:
            from reactor_core.integration.prime_connector import (
                PrimeConnector,
                PrimeConnectorConfig,
            )

            self._prime_connector = PrimeConnector(
                PrimeConnectorConfig(
                    host=self.config.prime_host,
                    port=self.config.prime_port,
                    enable_websocket=self.config.prime_websocket_enabled,
                )
            )
            logger.info("[ReactorCore] ✓ PrimeConnector initialized")

        except ImportError as e:
            logger.warning(f"[ReactorCore] PrimeConnector not available: {e}")
        except Exception as e:
            logger.error(f"[ReactorCore] PrimeConnector init failed: {e}")

    # =========================================================================
    # Experience Collection
    # =========================================================================

    async def get_recent_experiences(
        self,
        hours: int = 24,
        limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Get recent experiences from JARVIS logs.

        Args:
            hours: How many hours back to look
            limit: Maximum number of experiences

        Returns:
            List of experience dictionaries
        """
        if not self._jarvis_connector:
            logger.warning("[ReactorCore] JARVIS Connector not available")
            return []

        try:
            since = datetime.now() - timedelta(hours=hours)
            events = await self._jarvis_connector.get_events(
                since=since,
                limit=limit
            )

            experiences = []
            for event in events:
                experiences.append({
                    "timestamp": event.timestamp.isoformat() if hasattr(event, 'timestamp') else None,
                    "event_type": getattr(event, 'event_type', 'unknown'),
                    "user_input": getattr(event, 'user_input', ''),
                    "response": getattr(event, 'response', ''),
                    "metadata": getattr(event, 'metadata', {}),
                })

            logger.info(f"[ReactorCore] Retrieved {len(experiences)} experiences")
            return experiences

        except Exception as e:
            logger.error(f"[ReactorCore] Experience retrieval failed: {e}")
            return []

    async def get_corrections(self, hours: int = 168) -> List[Dict[str, Any]]:
        """
        Get correction events (user fixing JARVIS mistakes).

        Args:
            hours: How many hours back to look

        Returns:
            List of correction dictionaries
        """
        if not self._jarvis_connector:
            return []

        try:
            corrections = await self._jarvis_connector.get_corrections(hours=hours)
            return [
                {
                    "original": getattr(c, 'original_response', ''),
                    "corrected": getattr(c, 'corrected_response', ''),
                    "correction_type": getattr(c, 'correction_type', 'unknown'),
                    "timestamp": getattr(c, 'timestamp', None),
                }
                for c in corrections
            ]
        except Exception as e:
            logger.error(f"[ReactorCore] Correction retrieval failed: {e}")
            return []

    async def stream_experiences(self) -> AsyncIterator[Dict[str, Any]]:
        """
        Stream experiences in real-time from JARVIS logs.

        Yields:
            Experience dictionaries as they occur
        """
        if not self._jarvis_connector:
            logger.warning("[ReactorCore] JARVIS Connector not available for streaming")
            return

        try:
            async for event in self._jarvis_connector.stream_events():
                yield {
                    "timestamp": getattr(event, 'timestamp', datetime.now()).isoformat(),
                    "event_type": getattr(event, 'event_type', 'unknown'),
                    "user_input": getattr(event, 'user_input', ''),
                    "response": getattr(event, 'response', ''),
                }
        except Exception as e:
            logger.error(f"[ReactorCore] Experience streaming failed: {e}")

    # =========================================================================
    # Web Documentation Scraping
    # =========================================================================

    async def scrape_topics(
        self,
        topics: List[str],
        max_pages: Optional[int] = None,
        progress_callback: Optional[Callable] = None
    ) -> Dict[str, Any]:
        """
        Scrape web documentation for given topics.

        Args:
            topics: List of topics to scrape
            max_pages: Override max pages per topic
            progress_callback: Callback for progress updates

        Returns:
            Scraping results with statistics
        """
        if not self._scout:
            logger.warning("[ReactorCore] Safe Scout not available")
            return {"error": "Scout not available", "topics": topics}

        try:
            # Add topics
            for topic in topics:
                self._scout.add_topic(topic)

            # Add progress callback if provided
            if progress_callback:
                self._scout.add_progress_callback(progress_callback)

            # Run scraping
            logger.info(f"[ReactorCore] Starting scrape for {len(topics)} topics")
            progress = await self._scout.run()

            # Get statistics
            stats = self._scout.get_statistics()

            return {
                "success": True,
                "topics_processed": len(topics),
                "pages_scraped": stats.get("pages_fetched", 0),
                "examples_synthesized": stats.get("examples_synthesized", 0),
                "duration_seconds": stats.get("duration_seconds", 0),
            }

        except Exception as e:
            logger.error(f"[ReactorCore] Scraping failed: {e}")
            return {"error": str(e), "topics": topics}

    async def add_scraping_topic(self, topic: str, urls: Optional[List[str]] = None) -> bool:
        """
        Add a topic for future scraping.

        Args:
            topic: Topic name/description
            urls: Optional specific URLs to scrape

        Returns:
            True if added successfully
        """
        if not self._scout:
            return False

        try:
            self._scout.add_topic(topic)
            if urls:
                for url in urls:
                    self._scout.add_url(url)
            return True
        except Exception as e:
            logger.error(f"[ReactorCore] Failed to add topic: {e}")
            return False

    # =========================================================================
    # JARVIS-Prime Integration
    # =========================================================================

    async def check_prime_health(self) -> Dict[str, Any]:
        """
        Check JARVIS-Prime health status.

        Returns:
            Health status dictionary
        """
        if not self._prime_connector:
            return {"status": "unavailable", "error": "Prime Connector not initialized"}

        try:
            health = await self._prime_connector.check_health()
            return {
                "status": "healthy" if health else "unhealthy",
                "host": self.config.prime_host,
                "port": self.config.prime_port,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def get_prime_interactions(
        self,
        hours: int = 24,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get recent interactions from JARVIS-Prime.

        Args:
            hours: How many hours back to look
            limit: Maximum number of interactions

        Returns:
            List of interaction dictionaries
        """
        if not self._prime_connector:
            return []

        try:
            interactions = await self._prime_connector.get_recent_interactions(
                hours=hours,
                limit=limit
            )
            return interactions
        except Exception as e:
            logger.error(f"[ReactorCore] Prime interaction retrieval failed: {e}")
            return []

    async def stream_prime_events(self) -> AsyncIterator[Dict[str, Any]]:
        """
        Stream real-time events from JARVIS-Prime via WebSocket.

        Yields:
            Event dictionaries as they occur
        """
        if not self._prime_connector:
            return

        try:
            async with self._prime_connector:
                async for event in self._prime_connector.stream_events():
                    yield {
                        "event_type": getattr(event, 'event_type', 'unknown'),
                        "data": getattr(event, 'data', {}),
                        "timestamp": datetime.now().isoformat(),
                    }
        except Exception as e:
            logger.error(f"[ReactorCore] Prime event streaming failed: {e}")

    def register_prime_callback(self, callback: Callable) -> None:
        """Register a callback for Prime events."""
        if self._prime_connector:
            self._prime_connector.add_event_callback(callback)
            self._event_callbacks.append(callback)

    # =========================================================================
    # Training Pipeline
    # =========================================================================

    async def run_training_pipeline(
        self,
        sources: Optional[List[str]] = None,
        stop_after: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Run the full Night Shift training pipeline.

        Args:
            sources: Data sources to use (scout, jarvis, prime)
            stop_after: Stop after specific stage

        Returns:
            Pipeline execution results
        """
        if not self.config.training_enabled:
            return {"error": "Training disabled"}

        try:
            from reactor_core.orchestration.pipeline import (
                NightShiftPipeline,
                PipelineConfig,
                DataSource,
            )

            # Configure pipeline
            enabled_sources = set()
            if sources:
                source_map = {
                    "scout": DataSource.SCOUT,
                    "jarvis": DataSource.JARVIS_EXPERIENCE,
                    "prime": DataSource.PRIME_INTERACTION,
                }
                for s in sources:
                    if s in source_map:
                        enabled_sources.add(source_map[s])
            else:
                enabled_sources = {DataSource.SCOUT, DataSource.JARVIS_EXPERIENCE}

            config = PipelineConfig(
                enabled_sources=enabled_sources,
                base_model=self.config.training_base_model,
            )

            self._pipeline = NightShiftPipeline(config)
            result = await self._pipeline.run()

            return {
                "success": result.success if hasattr(result, 'success') else True,
                "stages_completed": getattr(result, 'stages_completed', []),
                "model_path": getattr(result, 'model_path', None),
            }

        except ImportError as e:
            logger.warning(f"[ReactorCore] Training pipeline not available: {e}")
            return {"error": "Pipeline not available"}
        except Exception as e:
            logger.error(f"[ReactorCore] Training pipeline failed: {e}")
            return {"error": str(e)}

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def shutdown(self) -> None:
        """Gracefully shutdown all components."""
        logger.info("[ReactorCore] Shutting down...")

        # Cancel streaming task
        if self._streaming_task:
            self._streaming_task.cancel()
            try:
                await self._streaming_task
            except asyncio.CancelledError:
                pass

        # Close Prime Connector
        if self._prime_connector:
            try:
                await self._prime_connector.close()
            except Exception:
                pass

        # Cancel Scout if running
        if self._scout:
            try:
                await self._scout.cancel()
            except Exception:
                pass

        self._initialized = False
        logger.info("[ReactorCore] Shutdown complete")

    def get_status(self) -> Dict[str, Any]:
        """Get integration status."""
        return {
            "initialized": self._initialized,
            "components": {
                "jarvis_connector": self._jarvis_connector is not None,
                "scout": self._scout is not None,
                "prime_connector": self._prime_connector is not None,
                "pipeline": self._pipeline is not None,
            },
            "config": {
                "reactor_core_path": str(self.config.reactor_core_path),
                "training_enabled": self.config.training_enabled,
                "scout_enabled": self.config.scout_enabled,
            }
        }


# =============================================================================
# JARVIS-Prime Neural Mesh Bridge
# =============================================================================

class PrimeNeuralMeshBridge:
    """
    Bridge connecting JARVIS-Prime events to the Neural Mesh.

    This bridge:
    - Listens to JARVIS-Prime model events (hot-swap, routing, telemetry)
    - Translates events to AgentMessage format
    - Publishes to the Neural Mesh communication bus
    - Enables distributed intelligence across the JARVIS ecosystem

    Architecture:
        ┌────────────────────────────────────────────────────────────────┐
        │                  JARVIS-Prime Neural Mesh Bridge               │
        ├────────────────────────────────────────────────────────────────┤
        │                                                                │
        │  ┌──────────────────┐     ┌──────────────────┐                │
        │  │  JARVIS-Prime    │────►│  Event Translator │                │
        │  │  (Model Events)  │     │  (Prime→AgentMsg) │                │
        │  └──────────────────┘     └────────┬─────────┘                │
        │                                     │                          │
        │                          ┌──────────▼──────────┐              │
        │                          │  Neural Mesh Bus    │              │
        │                          │  (Pub/Sub System)   │              │
        │                          └──────────┬──────────┘              │
        │                                     │                          │
        │  ┌──────────────────────────────────┼──────────────────────┐  │
        │  │                                  ▼                       │  │
        │  │  ┌────────────┐  ┌────────────┐  ┌────────────┐        │  │
        │  │  │  Memory    │  │  Pattern   │  │  Health    │        │  │
        │  │  │  Agent     │  │  Agent     │  │  Monitor   │        │  │
        │  │  └────────────┘  └────────────┘  └────────────┘        │  │
        │  │              Neural Mesh Subscribers                    │  │
        │  └─────────────────────────────────────────────────────────┘  │
        └────────────────────────────────────────────────────────────────┘
    """

    def __init__(self, config: Optional[ReactorCoreConfig] = None):
        self.config = config or ReactorCoreConfig()
        self._initialized = False
        self._communication_bus = None
        self._prime_client = None
        self._event_stream_task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable] = []

        logger.info("[PrimeNeuralMesh] Bridge initialized")

    async def initialize(self) -> bool:
        """Initialize the Prime Neural Mesh bridge."""
        if self._initialized:
            return True

        logger.info("[PrimeNeuralMesh] Initializing bridge...")

        # Initialize communication bus connection
        await self._init_communication_bus()

        # Initialize JARVIS-Prime client
        await self._init_prime_client()

        self._initialized = True
        logger.info("[PrimeNeuralMesh] ✓ Bridge initialized")
        return True

    async def _init_communication_bus(self) -> None:
        """Initialize connection to Neural Mesh communication bus."""
        try:
            from neural_mesh.communication.agent_communication_bus import (
                get_communication_bus,
            )
            self._communication_bus = await get_communication_bus()
            logger.info("[PrimeNeuralMesh] ✓ Connected to Neural Mesh communication bus")
        except ImportError:
            logger.warning("[PrimeNeuralMesh] Communication bus not available")
        except Exception as e:
            logger.error(f"[PrimeNeuralMesh] Communication bus init failed: {e}")

    async def _init_prime_client(self) -> None:
        """Initialize JARVIS-Prime client connection."""
        try:
            from core.jarvis_prime_client import get_jarvis_prime_client
            # get_jarvis_prime_client() is a sync function that returns a singleton
            self._prime_client = get_jarvis_prime_client()
            logger.info("[PrimeNeuralMesh] ✓ Connected to JARVIS-Prime client")
        except ImportError:
            logger.warning("[PrimeNeuralMesh] JARVIS-Prime client not available")
        except Exception as e:
            logger.error(f"[PrimeNeuralMesh] Prime client init failed: {e}")

    async def start_event_stream(self) -> None:
        """Start streaming events from JARVIS-Prime to Neural Mesh."""
        if not self._initialized:
            await self.initialize()

        if self._event_stream_task and not self._event_stream_task.done():
            return

        self._event_stream_task = asyncio.create_task(self._stream_prime_events())
        logger.info("[PrimeNeuralMesh] Event stream started")

    async def _stream_prime_events(self) -> None:
        """
        Background task that streams Prime events to Neural Mesh.

        v8.0 - Non-Blocking Startup Edition:
        - During startup phase, uses short delays (2-5s) to avoid blocking
        - Switches to normal delays after initial startup window (120s)
        - Graceful degradation: if WebSocket unavailable, uses REST polling
        - Never blocks main JARVIS startup
        """
        retry_count = 0
        base_delay = 2.0  # Start with 2s, not 10s
        start_time = asyncio.get_event_loop().time()
        startup_window = 120.0  # First 2 minutes use short delays

        ws_connection_timeout = float(os.getenv("TIMEOUT_PRIME_WS_CONNECTION", "30.0"))
        while True:
            try:
                # Check if we're still in startup window
                elapsed = asyncio.get_event_loop().time() - start_time
                in_startup = elapsed < startup_window

                # Connect to Prime WebSocket for real-time events
                prime_url = f"ws://{self.config.prime_host}:{self.config.prime_port}/ws/events"

                import websockets
                async with asyncio.timeout(ws_connection_timeout):
                    ws = await websockets.connect(
                        prime_url,
                        ping_interval=20,
                        ping_timeout=10,
                        close_timeout=5,
                    )
                async with ws:
                    logger.info(f"[PrimeNeuralMesh] ✓ Connected to Prime WebSocket: {prime_url}")
                    retry_count = 0  # Reset on successful connection

                    async for message in ws:
                        try:
                            import json
                            event = json.loads(message)
                            await self._handle_prime_event(event)
                        except json.JSONDecodeError:
                            continue

            except ImportError:
                logger.warning("[PrimeNeuralMesh] websockets library not available")
                break

            except ConnectionRefusedError:
                retry_count += 1
                # Short delays during startup, longer after
                if in_startup:
                    delay = min(base_delay * (1.5 ** min(retry_count - 1, 3)), 10.0)  # Max 10s during startup
                else:
                    delay = min(base_delay * (2 ** min(retry_count, 6)), 120.0)  # Max 2min normally

                if retry_count <= 3 or retry_count % 10 == 0:  # Log first 3, then every 10th
                    logger.debug(f"[PrimeNeuralMesh] Prime not available (attempt {retry_count}), retry in {delay:.1f}s")
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                logger.info("[PrimeNeuralMesh] Event stream cancelled")
                break

            except Exception as e:
                error_msg = str(e)
                retry_count += 1

                if "404" in error_msg:
                    # WebSocket endpoint not available - Prime may not have this endpoint
                    # Use very short delays during startup to not block
                    if in_startup:
                        delay = min(5.0 * retry_count, 15.0)  # 5s, 10s, 15s during startup
                        max_startup_retries = 5
                        if retry_count <= max_startup_retries:
                            if retry_count == 1:
                                logger.info("[PrimeNeuralMesh] Waiting for Prime WebSocket (background)...")
                            await asyncio.sleep(delay)
                        else:
                            # During startup, just switch to polling and don't block
                            logger.info("[PrimeNeuralMesh] Prime WebSocket not ready - using REST polling (non-blocking)")
                            asyncio.create_task(self._poll_prime_status())  # Fire and forget
                            retry_count = 0
                            await asyncio.sleep(30)  # Wait 30s before trying WebSocket again
                    else:
                        # After startup, use normal retry logic
                        delay = min(base_delay * (2 ** min(retry_count - 1, 5)), 60.0)
                        max_retries = int(os.getenv("PRIME_WEBSOCKET_MAX_RETRIES", "15"))

                        if retry_count <= max_retries:
                            if retry_count == 1 or retry_count % 5 == 0:
                                logger.info(f"[PrimeNeuralMesh] WebSocket retry {retry_count}/{max_retries} in {delay:.1f}s")
                            await asyncio.sleep(delay)
                        else:
                            logger.info("[PrimeNeuralMesh] Falling back to REST polling")
                            await self._poll_prime_status()
                            retry_count = 0
                            await asyncio.sleep(60)
                else:
                    # Other errors
                    if retry_count <= 3:
                        logger.warning(f"[PrimeNeuralMesh] Event stream error: {e}")
                    delay = min(5.0 * (1.5 ** min(retry_count - 1, 4)), 30.0)
                    await asyncio.sleep(delay)

    async def _poll_prime_status(self) -> None:
        """Fallback: Poll Prime status via REST when WebSocket unavailable."""
        try:
            import aiohttp

            prime_health_url = f"http://{self.config.prime_host}:{self.config.prime_port}/health"

            async with aiohttp.ClientSession() as session:
                async with session.get(prime_health_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Emit as event
                        await self._handle_prime_event({
                            "event_type": "status_poll",
                            "data": data,
                        })
                        logger.debug("[PrimeNeuralMesh] REST poll successful")
        except Exception as e:
            logger.debug(f"[PrimeNeuralMesh] REST poll failed: {e}")

    async def _handle_prime_event(self, event: Dict[str, Any]) -> None:
        """Handle an event from JARVIS-Prime."""
        event_type = event.get("event_type", "unknown")

        # Translate to AgentMessage
        agent_message = await self._translate_to_agent_message(event)

        # Publish to Neural Mesh
        if self._communication_bus and agent_message:
            await self._communication_bus.publish(agent_message)
            logger.debug(f"[PrimeNeuralMesh] Published {event_type} event to Neural Mesh")

        # Notify registered callbacks
        for callback in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                logger.warning(f"[PrimeNeuralMesh] Callback error: {e}")

    async def _translate_to_agent_message(self, event: Dict[str, Any]) -> Optional[Any]:
        """Translate JARVIS-Prime event to Neural Mesh AgentMessage."""
        try:
            from neural_mesh.data_models import (
                AgentMessage,
                MessageType,
                MessagePriority,
            )

            event_type = event.get("event_type", "unknown")

            # Map Prime events to Neural Mesh message types
            type_mapping = {
                "model_loaded": MessageType.SYSTEM_STARTUP,
                "model_swap": MessageType.SYSTEM_CONFIG_CHANGED,
                "swap_complete": MessageType.SYSTEM_CONFIG_CHANGED,
                "inference_request": MessageType.REQUEST,
                "inference_complete": MessageType.RESPONSE,
                "routing_decision": MessageType.CUSTOM,
                "telemetry": MessageType.CUSTOM,
                "health_check": MessageType.AGENT_HEALTH_CHECK,
                "error": MessageType.ERROR_DETECTED,
            }

            # Determine priority based on event type
            priority_mapping = {
                "error": MessagePriority.CRITICAL,
                "model_swap": MessagePriority.HIGH,
                "swap_complete": MessagePriority.HIGH,
                "inference_request": MessagePriority.NORMAL,
                "telemetry": MessagePriority.LOW,
            }

            message_type = type_mapping.get(event_type, MessageType.CUSTOM)
            priority = priority_mapping.get(event_type, MessagePriority.NORMAL)

            return AgentMessage(
                from_agent="jarvis_prime",
                to_agent="broadcast",
                message_type=message_type,
                payload={
                    "prime_event_type": event_type,
                    "data": event.get("data", {}),
                    "timestamp": event.get("timestamp", datetime.now().isoformat()),
                    "source": "jarvis_prime",
                },
                priority=priority,
                metadata={
                    "origin": "prime_neural_mesh_bridge",
                    "event_version": "1.0",
                }
            )

        except ImportError:
            logger.warning("[PrimeNeuralMesh] Neural Mesh data models not available")
            return None
        except Exception as e:
            logger.error(f"[PrimeNeuralMesh] Message translation failed: {e}")
            return None

    async def publish_to_prime(self, command: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Send a command to JARVIS-Prime from the Neural Mesh.

        Args:
            command: Command type (inference, status, reload, etc.)
            data: Command data

        Returns:
            Response from JARVIS-Prime
        """
        if not self._prime_client:
            logger.warning("[PrimeNeuralMesh] Prime client not available")
            return None

        try:
            if command == "inference":
                return await self._prime_client.complete(
                    messages=data.get("messages", []),
                    temperature=data.get("temperature", 0.7),
                )
            elif command == "status":
                return await self._prime_client.get_status()
            else:
                logger.warning(f"[PrimeNeuralMesh] Unknown command: {command}")
                return None

        except Exception as e:
            logger.error(f"[PrimeNeuralMesh] Prime command failed: {e}")
            return None

    def register_callback(self, callback: Callable) -> None:
        """Register a callback for Prime events."""
        self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable) -> None:
        """Unregister a callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def shutdown(self) -> None:
        """Shutdown the bridge."""
        logger.info("[PrimeNeuralMesh] Shutting down...")

        if self._event_stream_task:
            self._event_stream_task.cancel()
            try:
                await self._event_stream_task
            except asyncio.CancelledError:
                pass

        self._callbacks.clear()
        self._initialized = False
        logger.info("[PrimeNeuralMesh] Shutdown complete")

    def get_status(self) -> Dict[str, Any]:
        """Get bridge status."""
        return {
            "initialized": self._initialized,
            "communication_bus_connected": self._communication_bus is not None,
            "prime_client_connected": self._prime_client is not None,
            "event_stream_active": (
                self._event_stream_task is not None
                and not self._event_stream_task.done()
            ),
            "registered_callbacks": len(self._callbacks),
        }


# =============================================================================
# Singleton Access
# =============================================================================

_integration_instance: Optional[ReactorCoreIntegration] = None
_prime_mesh_bridge: Optional[PrimeNeuralMeshBridge] = None


def get_reactor_core_integration() -> ReactorCoreIntegration:
    """Get the global ReactorCoreIntegration instance."""
    global _integration_instance
    if _integration_instance is None:
        _integration_instance = ReactorCoreIntegration()
    return _integration_instance


def get_prime_neural_mesh_bridge() -> PrimeNeuralMeshBridge:
    """Get the global PrimeNeuralMeshBridge instance."""
    global _prime_mesh_bridge
    if _prime_mesh_bridge is None:
        _prime_mesh_bridge = PrimeNeuralMeshBridge()
    return _prime_mesh_bridge


async def initialize_reactor_core() -> bool:
    """Initialize the global reactor-core integration."""
    integration = get_reactor_core_integration()
    return await integration.initialize()


async def initialize_prime_neural_mesh() -> bool:
    """Initialize the Prime Neural Mesh bridge."""
    bridge = get_prime_neural_mesh_bridge()
    success = await bridge.initialize()
    if success:
        await bridge.start_event_stream()
    return success


async def shutdown_reactor_core() -> None:
    """Shutdown the global reactor-core integration."""
    global _integration_instance, _prime_mesh_bridge

    if _prime_mesh_bridge:
        await _prime_mesh_bridge.shutdown()
        _prime_mesh_bridge = None

    if _integration_instance:
        await _integration_instance.shutdown()
        _integration_instance = None
