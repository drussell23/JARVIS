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
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================


def _parse_int_list_env(var_name: str, default_values: List[int]) -> List[int]:
    """Parse an integer list environment variable while preserving order."""
    raw_value = os.getenv(var_name, "").strip()
    items = raw_value.split(",") if raw_value else [str(v) for v in default_values]
    parsed: List[int] = []
    seen: set = set()
    for item in items:
        token = item.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            continue
        if value <= 0:
            continue
        if value in seen:
            continue
        parsed.append(value)
        seen.add(value)
    return parsed or list(default_values)


def _parse_path_list_env(var_name: str, default_paths: List[str]) -> List[str]:
    """Parse comma-separated endpoint paths and normalize to absolute-style paths."""
    raw_value = os.getenv(var_name, "").strip()
    items = raw_value.split(",") if raw_value else list(default_paths)
    normalized: List[str] = []
    seen: set = set()
    for item in items:
        path = item.strip()
        if not path:
            continue
        if "://" in path:
            # Ignore full URLs here; this list is path-only.
            continue
        if not path.startswith("/"):
            path = f"/{path}"
        if path in seen:
            continue
        normalized.append(path)
        seen.add(path)
    return normalized or list(default_paths)


def _unique_ints(values: List[int]) -> List[int]:
    """Deduplicate integer list while preserving order."""
    result: List[int] = []
    seen: set = set()
    for value in values:
        if value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def _adaptive_timeout(base_timeout: float, observed_latency: Optional[float]) -> float:
    """
    Derive a dynamic timeout from observed network latency.

    Keeps a deterministic floor while adapting upward when a link is slower than normal.
    """
    if observed_latency is None:
        return base_timeout
    # 4x recent latency, clamped to avoid runaway waits.
    dynamic_timeout = max(base_timeout, observed_latency * 4.0)
    return min(dynamic_timeout, base_timeout * 6.0)

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
    prime_port_candidates: List[int] = field(
        default_factory=lambda: _parse_int_list_env(
            "JARVIS_PRIME_PORT_CANDIDATES",
            [
                int(os.getenv("JARVIS_PRIME_PORT", "8002")),
                8000,
                8001,
                8002,
            ],
        )
    )
    prime_websocket_enabled: bool = field(
        default_factory=lambda: os.getenv("PRIME_WEBSOCKET_ENABLED", "true").lower() == "true"
    )
    prime_websocket_paths: List[str] = field(
        default_factory=lambda: _parse_path_list_env(
            "JARVIS_PRIME_WEBSOCKET_PATHS",
            ["/ws/events"],
        )
    )
    prime_health_paths: List[str] = field(
        default_factory=lambda: _parse_path_list_env(
            "JARVIS_PRIME_HEALTH_PATHS",
            ["/health"],
        )
    )
    prime_event_poll_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_EVENT_POLL_INTERVAL", "5.0"))
    )
    prime_event_probe_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_EVENT_PROBE_TIMEOUT", "3.0"))
    )
    prime_transport_reprobe_interval: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_TRANSPORT_REPROBE_INTERVAL", "30.0"))
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

    def _prime_port_candidates(self) -> List[int]:
        """Ordered, de-duplicated Prime ports (preferred first)."""
        return _unique_ints([self.config.prime_port] + list(self.config.prime_port_candidates))

    def _prime_health_url_candidates(self) -> List[Tuple[int, str, str]]:
        """Generate Prime health endpoint candidates (port + path)."""
        candidates: List[Tuple[int, str, str]] = []
        for port in self._prime_port_candidates():
            for path in self.config.prime_health_paths:
                url = f"http://{self.config.prime_host}:{port}{path}"
                candidates.append((port, path, url))
        return candidates

    async def _resolve_live_prime_port(self) -> int:
        """
        Probe configured Prime candidates and return the first healthy/reachable port.

        Falls back to configured `prime_port` if no candidate responds.
        """
        try:
            import aiohttp
        except ImportError:
            return self.config.prime_port

        timeout_s = max(1.0, self.config.prime_event_probe_timeout)
        adaptive_timeout_s = _adaptive_timeout(timeout_s, None)
        timeout = aiohttp.ClientTimeout(total=adaptive_timeout_s)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for port, _path, url in self._prime_health_url_candidates():
                try:
                    async with session.get(url) as resp:
                        # 200 (ready) and 503 (starting) both indicate a live service.
                        if resp.status in (200, 503):
                            if port != self.config.prime_port:
                                logger.info(
                                    "[ReactorCore] Prime endpoint drift detected; "
                                    f"switching from configured port {self.config.prime_port} to live port {port}"
                                )
                            return port
                except Exception:
                    continue

        return self.config.prime_port

    async def _poll_prime_health_event(self) -> Optional[Dict[str, Any]]:
        """Poll Prime health endpoints and emit a synthetic status event."""
        try:
            import aiohttp
        except ImportError:
            return None

        timeout_s = max(1.0, self.config.prime_event_probe_timeout)
        timeout = aiohttp.ClientTimeout(total=_adaptive_timeout(timeout_s, None))

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for port, path, url in self._prime_health_url_candidates():
                try:
                    async with session.get(url) as resp:
                        if resp.status >= 500:
                            continue

                        payload: Any
                        try:
                            payload = await resp.json(content_type=None)
                        except Exception:
                            payload = {"raw": await resp.text()}

                        if not isinstance(payload, dict):
                            payload = {"value": payload}

                        return {
                            "event_type": "status_poll",
                            "data": {
                                **payload,
                                "_prime_host": self.config.prime_host,
                                "_prime_port": port,
                                "_prime_health_path": path,
                                "_prime_http_status": resp.status,
                            },
                            "timestamp": datetime.now().isoformat(),
                        }
                except Exception:
                    continue

        return None

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

            live_port = await self._resolve_live_prime_port()
            self.config.prime_port = live_port

            prime_cfg = PrimeConnectorConfig(
                host=self.config.prime_host,
                port=self.config.prime_port,
                enable_websocket=self.config.prime_websocket_enabled,
            )
            # Backwards-compatible override for connectors that still use a single WS path.
            if hasattr(prime_cfg, "websocket_path") and self.config.prime_websocket_paths:
                setattr(prime_cfg, "websocket_path", self.config.prime_websocket_paths[0])

            self._prime_connector = PrimeConnector(
                prime_cfg
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
        if self._prime_connector:
            try:
                async with self._prime_connector:
                    async for event in self._prime_connector.stream_events():
                        yield {
                            "event_type": getattr(event, "event_type", "unknown"),
                            "data": getattr(event, "data", {}),
                            "timestamp": datetime.now().isoformat(),
                        }
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "[ReactorCore] Prime event streaming via WebSocket failed; "
                    f"falling back to health polling: {e}"
                )
        else:
            logger.warning(
                "[ReactorCore] Prime connector unavailable; using health-poll fallback stream"
            )

        # Deterministic fallback stream: always emits status snapshots while Prime is reachable.
        poll_interval = max(0.5, self.config.prime_event_poll_interval)
        failures = 0
        while True:
            try:
                event = await self._poll_prime_health_event()
                if event is not None:
                    failures = 0
                    yield event
                else:
                    failures += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                if failures <= 3 or failures % 10 == 0:
                    logger.debug(f"[ReactorCore] Prime health stream poll failed: {e}")

            backoff = min(poll_interval * (1.0 + (0.25 * min(failures, 8))), poll_interval * 4.0)
            await asyncio.sleep(backoff)

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
        │  ┌──────────────────┐     ┌──────────────────┐                 │
        │  │  JARVIS-Prime    │────►│  Event Translator │                │
        │  │  (Model Events)  │     │  (Prime→AgentMsg) │                │
        │  └──────────────────┘     └────────┬─────────┘                 │
        │                                     │                          │
        │                          ┌──────────▼──────────┐               │
        │                          │  Neural Mesh Bus    │               │
        │                          │  (Pub/Sub System)   │               │
        │                          └──────────┬──────────┘               │
        │                                     │                          │
        │  ┌──────────────────────────────────┼──────────────────────┐   │
        │  │                                  ▼                      │   │
        │  │  ┌────────────┐  ┌────────────┐  ┌────────────┐         │   │
        │  │  │  Memory    │  │  Pattern   │  │  Health    │         │   │
        │  │  │  Agent     │  │  Agent     │  │  Monitor   │         │   │
        │  │  └────────────┘  └────────────┘  └────────────┘         │   │
        │  │              Neural Mesh Subscribers                    │   │
        │  └─────────────────────────────────────────────────────────┘   │
        └────────────────────────────────────────────────────────────────┘
    """

    def __init__(self, config: Optional[ReactorCoreConfig] = None):
        self.config = config or ReactorCoreConfig()
        self._initialized = False
        self._communication_bus = None
        self._prime_client = None
        self._event_stream_task: Optional[asyncio.Task] = None
        self._callbacks: List[Callable] = []
        self._prime_probe_latency_ema: Optional[float] = None
        self._ws_candidate_index: int = 0
        self._health_candidate_index: int = 0
        self._last_successful_ws_url: Optional[str] = None
        self._last_successful_health_url: Optional[str] = None
        self._current_transport: str = "websocket" if self.config.prime_websocket_enabled else "polling"

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
        """
        Initialize connection to Neural Mesh communication bus.

        v93.5: Enhanced with multiple import path attempts and graceful fallback.
        The communication bus may be imported from different paths depending on
        how the module is run (as package vs script).
        """
        import_attempts = [
            # Try backend-prefixed import first (when running from JARVIS-AI-Agent root)
            ("backend.neural_mesh.communication.agent_communication_bus", "get_communication_bus"),
            # Try direct import (when backend is in PYTHONPATH)
            ("neural_mesh.communication.agent_communication_bus", "get_communication_bus"),
            # Try relative from current location
            ("backend.neural_mesh", "get_communication_bus"),
        ]

        for module_path, func_name in import_attempts:
            try:
                import importlib
                module = importlib.import_module(module_path)
                get_bus_func = getattr(module, func_name, None)

                if get_bus_func:
                    self._communication_bus = await get_bus_func()
                    logger.info(f"[PrimeNeuralMesh] ✓ Connected to Neural Mesh communication bus (via {module_path})")
                    return

            except ImportError as e:
                logger.debug(f"[PrimeNeuralMesh] Import attempt failed for {module_path}: {e}")
                continue
            except Exception as e:
                logger.debug(f"[PrimeNeuralMesh] Bus init failed for {module_path}: {e}")
                continue

        # v93.5: All import attempts failed - create fallback in-memory bus
        logger.warning("[PrimeNeuralMesh] Communication bus not available via any import path, using fallback")
        self._communication_bus = await self._create_fallback_bus()

    async def _create_fallback_bus(self) -> "FallbackCommunicationBus":
        """
        v93.5: Create a lightweight fallback communication bus.

        This provides basic pub/sub functionality when the main Neural Mesh
        communication bus is not available. Allows cross-repo communication
        to continue in degraded mode.
        """
        return FallbackCommunicationBus()

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

    def _ordered_prime_ports(self) -> List[int]:
        return _unique_ints([self.config.prime_port] + list(self.config.prime_port_candidates))

    def _ws_url_base_candidates(self) -> List[str]:
        scheme = "wss" if os.getenv("JARVIS_PRIME_SSL", "false").lower() == "true" else "ws"
        candidates: List[str] = []
        for port in self._ordered_prime_ports():
            for path in self.config.prime_websocket_paths:
                candidates.append(f"{scheme}://{self.config.prime_host}:{port}{path}")
        return candidates

    def _health_url_base_candidates(self) -> List[str]:
        scheme = "https" if os.getenv("JARVIS_PRIME_SSL", "false").lower() == "true" else "http"
        candidates: List[str] = []
        for port in self._ordered_prime_ports():
            for path in self.config.prime_health_paths:
                candidates.append(f"{scheme}://{self.config.prime_host}:{port}{path}")
        return candidates

    def _rotated_candidates(self, base: List[str], start_index: int) -> List[str]:
        if not base:
            return []
        idx = start_index % len(base)
        return base[idx:] + base[:idx]

    def _observe_probe_latency(self, latency_seconds: float) -> None:
        if latency_seconds <= 0:
            return
        alpha = 0.30
        if self._prime_probe_latency_ema is None:
            self._prime_probe_latency_ema = latency_seconds
            return
        self._prime_probe_latency_ema = (
            (1.0 - alpha) * self._prime_probe_latency_ema
            + alpha * latency_seconds
        )

    def _probe_timeout_seconds(self) -> float:
        base = max(1.0, self.config.prime_event_probe_timeout)
        return _adaptive_timeout(base, self._prime_probe_latency_ema)

    @staticmethod
    def _is_endpoint_contract_error(exc: Exception) -> bool:
        """Return True when an error indicates a missing/forbidden websocket endpoint."""
        msg = str(exc).lower()
        exc_type = type(exc).__name__
        endpoint_tokens = ("404", "403", "not found", "forbidden")
        return (
            any(token in msg for token in endpoint_tokens)
            or exc_type in ("InvalidStatusCode", "InvalidStatus")
        )

    async def _connect_prime_websocket(self, timeout_seconds: float):
        """Try websocket candidates in priority order and return (ws, url)."""
        import websockets

        base_candidates = self._ws_url_base_candidates()
        last_error: Optional[Exception] = None
        for url in self._rotated_candidates(base_candidates, self._ws_candidate_index):
            started_at = asyncio.get_event_loop().time()
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(
                        url,
                        ping_interval=20,
                        ping_timeout=10,
                        close_timeout=5,
                    ),
                    timeout=timeout_seconds,
                )
                self._observe_probe_latency(asyncio.get_event_loop().time() - started_at)
                # Keep stable path first after success.
                self._last_successful_ws_url = url
                return ws, url
            except Exception as exc:
                last_error = exc
                if self._is_endpoint_contract_error(exc):
                    self._ws_candidate_index += 1
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("Prime websocket endpoint unavailable for all configured candidates")

    async def _stream_prime_events(self) -> None:
        """
        Background task that streams Prime events to Neural Mesh.

        Uses adaptive endpoint negotiation:
        - Rotates across configured Prime ports + websocket paths
        - Falls back to health polling when websocket contracts are unavailable
        - Periodically re-probes websocket transport from polling mode
        """
        retry_count = 0
        base_delay = 2.0
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        startup_window = 120.0
        polling_mode = not self.config.prime_websocket_enabled
        polling_started_at: Optional[float] = loop.time() if polling_mode else None

        while True:
            elapsed = loop.time() - start_time
            in_startup = elapsed < startup_window

            try:
                if polling_mode:
                    ok = await self._poll_prime_status()
                    if ok:
                        retry_count = 0
                    else:
                        retry_count += 1

                    if (
                        self.config.prime_websocket_enabled
                        and polling_started_at is not None
                        and (loop.time() - polling_started_at) >= self.config.prime_transport_reprobe_interval
                    ):
                        polling_mode = False
                        polling_started_at = None
                        retry_count = 0
                        continue

                    poll_delay = max(0.5, self.config.prime_event_poll_interval)
                    backoff = min(poll_delay * (1.0 + 0.20 * min(retry_count, 8)), poll_delay * 4.0)
                    await asyncio.sleep(backoff)
                    continue

                ws_timeout = _adaptive_timeout(
                    float(os.getenv("TIMEOUT_PRIME_WS_CONNECTION", "30.0")),
                    self._prime_probe_latency_ema,
                )
                ws, prime_url = await self._connect_prime_websocket(timeout_seconds=ws_timeout)
                async with ws:
                    logger.info(f"[PrimeNeuralMesh] ✓ Connected to Prime WebSocket: {prime_url}")
                    retry_count = 0
                    self._current_transport = "websocket"

                    async for message in ws:
                        try:
                            import json
                            event = json.loads(message)
                            await self._handle_prime_event(event)
                        except json.JSONDecodeError:
                            continue

            except ImportError:
                logger.warning("[PrimeNeuralMesh] websockets library not available; using health polling")
                polling_mode = True
                polling_started_at = loop.time()
                self._current_transport = "polling"

            except asyncio.CancelledError:
                logger.info("[PrimeNeuralMesh] Event stream cancelled")
                break

            except Exception as exc:
                retry_count += 1
                if self._is_endpoint_contract_error(exc):
                    # Move to next path/port candidate and degrade to polling until reprobe window.
                    self._ws_candidate_index += 1
                    polling_mode = True
                    polling_started_at = loop.time()
                    self._current_transport = "polling"
                    if retry_count <= 3:
                        logger.info(
                            "[PrimeNeuralMesh] Prime websocket endpoint contract not available; "
                            "switching to health polling and will reprobe"
                        )
                    await self._poll_prime_status()
                    await asyncio.sleep(max(1.0, self.config.prime_event_poll_interval))
                    continue

                # Handle graceful websocket closures without hard fallback.
                exc_type_name = type(exc).__name__
                exc_module = type(exc).__module__
                if exc_type_name in ("ConnectionClosed", "ConnectionClosedError", "ConnectionClosedOK") and "websockets" in exc_module:
                    delay = min(base_delay * (1.2 ** min(retry_count - 1, 5)), 15.0)
                    if retry_count == 1 or retry_count % 5 == 0:
                        logger.debug(f"[PrimeNeuralMesh] WebSocket reconnect attempt {retry_count}")
                    await asyncio.sleep(delay)
                    continue

                if retry_count <= 3 or retry_count % 10 == 0:
                    logger.warning(f"[PrimeNeuralMesh] Event stream error: {exc}")

                if in_startup and retry_count >= 5:
                    polling_mode = True
                    polling_started_at = loop.time()
                    self._current_transport = "polling"
                    await self._poll_prime_status()
                    await asyncio.sleep(max(1.0, self.config.prime_event_poll_interval))
                    continue

                delay = min(base_delay * (1.5 ** min(retry_count - 1, 6)), 45.0)
                await asyncio.sleep(delay)

    async def _poll_prime_status(self) -> bool:
        """Fallback: Poll Prime status via REST when WebSocket unavailable."""
        try:
            import aiohttp
        except ImportError:
            return False

        candidates = self._rotated_candidates(
            self._health_url_base_candidates(),
            self._health_candidate_index,
        )
        if not candidates:
            return False

        timeout_seconds = self._probe_timeout_seconds()
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for offset, prime_health_url in enumerate(candidates):
                started_at = asyncio.get_event_loop().time()
                try:
                    async with session.get(prime_health_url) as resp:
                        if resp.status >= 500:
                            continue
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            data = {"raw": await resp.text()}
                        if not isinstance(data, dict):
                            data = {"value": data}

                        self._observe_probe_latency(asyncio.get_event_loop().time() - started_at)
                        self._last_successful_health_url = prime_health_url
                        self._health_candidate_index += offset
                        await self._handle_prime_event(
                            {
                                "event_type": "status_poll",
                                "data": {
                                    **data,
                                    "_prime_health_url": prime_health_url,
                                    "_prime_http_status": resp.status,
                                },
                            }
                        )
                        logger.debug("[PrimeNeuralMesh] REST poll successful")
                        return True
                except Exception:
                    continue

        return False

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
            "transport_mode": self._current_transport,
            "event_stream_active": (
                self._event_stream_task is not None
                and not self._event_stream_task.done()
            ),
            "registered_callbacks": len(self._callbacks),
            "last_successful_ws_url": self._last_successful_ws_url,
            "last_successful_health_url": self._last_successful_health_url,
            "prime_probe_latency_ema_ms": (
                round(self._prime_probe_latency_ema * 1000.0, 2)
                if self._prime_probe_latency_ema is not None
                else None
            ),
            "prime_ws_candidates": len(self._ws_url_base_candidates()),
            "prime_health_candidates": len(self._health_url_base_candidates()),
        }


# =============================================================================
# Fallback Communication Bus (v93.5)
# =============================================================================

class FallbackCommunicationBus:
    """
    v93.5: Lightweight fallback communication bus for cross-repo messaging.

    Provides basic pub/sub functionality when the main Neural Mesh
    communication bus is not available. This ensures the system can
    still operate in degraded mode without the full neural mesh.

    Features:
    - In-memory message queue
    - Basic topic-based pub/sub
    - Async-safe operations
    - Message history for debugging
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._message_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._history: List[Dict[str, Any]] = []
        self._max_history = 100
        self._running = False
        self._processor_task: Optional[asyncio.Task] = None
        logger.info("[FallbackBus] Initialized (degraded mode)")

    async def start(self) -> None:
        """Start the message processor."""
        if self._running:
            return
        self._running = True
        self._processor_task = asyncio.create_task(self._process_messages())
        logger.debug("[FallbackBus] Started message processor")

    async def stop(self) -> None:
        """Stop the message processor."""
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        logger.debug("[FallbackBus] Stopped")

    async def publish(self, message: Any) -> bool:
        """
        Publish a message to subscribers.

        Args:
            message: Message object with 'to_agent' or topic info

        Returns:
            True if message was queued successfully
        """
        try:
            # Auto-start if not running
            if not self._running:
                await self.start()

            # Extract topic from message
            if hasattr(message, 'to_agent'):
                topic = message.to_agent
            elif hasattr(message, 'message_type'):
                topic = str(message.message_type)
            elif isinstance(message, dict):
                topic = message.get('to_agent', message.get('topic', 'broadcast'))
            else:
                topic = 'broadcast'

            # Queue the message
            await asyncio.wait_for(
                self._message_queue.put({'topic': topic, 'message': message}),
                timeout=1.0
            )

            # Track history
            self._history.append({
                'topic': topic,
                'timestamp': datetime.now().isoformat(),
                'type': str(type(message).__name__),
            })
            if len(self._history) > self._max_history:
                self._history.pop(0)

            return True

        except asyncio.TimeoutError:
            logger.warning("[FallbackBus] Message queue full, dropping message")
            return False
        except Exception as e:
            logger.error(f"[FallbackBus] Publish failed: {e}")
            return False

    async def subscribe(self, topic: str, callback: Callable) -> bool:
        """
        Subscribe to a topic.

        Args:
            topic: Topic to subscribe to (or 'broadcast' for all)
            callback: Async callback function to handle messages

        Returns:
            True if subscribed successfully
        """
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(callback)
        logger.debug(f"[FallbackBus] Subscribed to topic: {topic}")
        return True

    async def unsubscribe(self, topic: str, callback: Callable) -> bool:
        """Unsubscribe from a topic."""
        if topic in self._subscribers and callback in self._subscribers[topic]:
            self._subscribers[topic].remove(callback)
            return True
        return False

    async def _process_messages(self) -> None:
        """Background task to deliver messages to subscribers."""
        while self._running:
            try:
                item = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=1.0
                )

                topic = item['topic']
                message = item['message']

                # Deliver to specific topic subscribers
                if topic in self._subscribers:
                    for callback in self._subscribers[topic]:
                        try:
                            if asyncio.iscoroutinefunction(callback):
                                await callback(message)
                            else:
                                callback(message)
                        except Exception as e:
                            logger.warning(f"[FallbackBus] Callback error: {e}")

                # Deliver to broadcast subscribers
                if 'broadcast' in self._subscribers:
                    for callback in self._subscribers['broadcast']:
                        try:
                            if asyncio.iscoroutinefunction(callback):
                                await callback(message)
                            else:
                                callback(message)
                        except Exception as e:
                            logger.warning(f"[FallbackBus] Broadcast callback error: {e}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FallbackBus] Process error: {e}")
                await asyncio.sleep(0.1)

    def get_stats(self) -> Dict[str, Any]:
        """Get bus statistics."""
        return {
            'running': self._running,
            'queue_size': self._message_queue.qsize(),
            'subscribers': {k: len(v) for k, v in self._subscribers.items()},
            'history_size': len(self._history),
            'mode': 'fallback',
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
