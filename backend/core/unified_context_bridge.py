"""
Unified Context Bridge
Integrates Follow-Up Handling with Contextual Awareness Intelligence.

This bridge provides a single interface for:
1. Follow-up question tracking and routing
2. Context-aware command handling
3. Shared context store management
4. Dynamic backend configuration (memory/Redis)
5. Cross-system telemetry and observability
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Protocol
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ============================================================================
# Configuration Models
# ============================================================================

class ContextStoreBackend(str, Enum):
    """Available context store backends."""
    MEMORY = "memory"
    REDIS = "redis"
    HYBRID = "hybrid"  # Memory + Redis persistence


@dataclass
class ContextBridgeConfig:
    """Dynamic configuration for unified context bridge."""

    # Backend selection
    backend: ContextStoreBackend = field(
        default_factory=lambda: ContextStoreBackend(
            os.getenv("CONTEXT_STORE_BACKEND", "memory")
        )
    )

    # Context store settings
    max_contexts: int = field(
        default_factory=lambda: int(os.getenv("MAX_PENDING_CONTEXTS", "100"))
    )
    default_ttl: int = field(
        default_factory=lambda: int(os.getenv("CONTEXT_TTL_SECONDS", "120"))
    )
    cleanup_interval: int = field(
        default_factory=lambda: int(os.getenv("CONTEXT_CLEANUP_INTERVAL", "60"))
    )

    # Follow-up system
    follow_up_enabled: bool = field(
        default_factory=lambda: os.getenv("FOLLOW_UP_ENABLED", "true").lower() == "true"
    )
    min_confidence: float = field(
        default_factory=lambda: float(os.getenv("FOLLOW_UP_MIN_CONFIDENCE", "0.75"))
    )

    # Context intelligence
    context_aware_enabled: bool = field(
        default_factory=lambda: os.getenv("CONTEXT_AWARE_ENABLED", "true").lower() == "true"
    )
    screen_lock_detection: bool = field(
        default_factory=lambda: os.getenv("SCREEN_LOCK_DETECTION", "true").lower() == "true"
    )

    # Redis settings (if backend=redis or hybrid)
    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )
    redis_key_prefix: str = field(
        default_factory=lambda: os.getenv("REDIS_KEY_PREFIX", "jarvis:context:")
    )

    # Telemetry
    telemetry_enabled: bool = field(
        default_factory=lambda: os.getenv("TELEMETRY_ENABLED", "true").lower() == "true"
    )

    # Performance
    enable_caching: bool = field(
        default_factory=lambda: os.getenv("ENABLE_CONTEXT_CACHING", "true").lower() == "true"
    )
    cache_size: int = field(
        default_factory=lambda: int(os.getenv("CONTEXT_CACHE_SIZE", "100"))
    )


# ============================================================================
# Context Store Factory
# ============================================================================

class ContextStoreProtocol(Protocol):
    """Protocol for context store backends."""

    async def add(self, context: Any) -> str:
        """Add context and return ID."""
        ...

    async def get(self, context_id: str) -> Optional[Any]:
        """Retrieve context by ID."""
        ...

    async def get_most_relevant(
        self,
        category: str | None = None,
        limit: int = 1,
    ) -> list[Any]:
        """Get most relevant contexts."""
        ...

    async def clear_expired(self) -> int:
        """Clear expired contexts, return count."""
        ...

    async def start_auto_cleanup(self) -> None:
        """Start background cleanup."""
        ...

    async def stop_auto_cleanup(self) -> None:
        """Stop background cleanup."""
        ...


class ContextStoreFactory:
    """Factory for creating context stores with different backends."""

    @staticmethod
    async def create(config: ContextBridgeConfig) -> ContextStoreProtocol:
        """Create context store based on configuration."""
        backend = config.backend

        if backend == ContextStoreBackend.MEMORY:
            return await ContextStoreFactory._create_memory_store(config)

        elif backend == ContextStoreBackend.REDIS:
            return await ContextStoreFactory._create_redis_store(config)

        elif backend == ContextStoreBackend.HYBRID:
            return await ContextStoreFactory._create_hybrid_store(config)

        else:
            logger.warning(
                f"Unknown backend '{backend}', falling back to memory"
            )
            return await ContextStoreFactory._create_memory_store(config)

    @staticmethod
    async def _create_memory_store(config: ContextBridgeConfig) -> ContextStoreProtocol:
        """Create in-memory context store."""
        from backend.core.context.memory_store import InMemoryContextStore

        store = InMemoryContextStore(
            max_size=config.max_contexts,
            auto_cleanup_interval=config.cleanup_interval,
        )

        # Start auto-cleanup
        if config.cleanup_interval > 0:
            await store.start_auto_cleanup()

        logger.info(
            f"[BRIDGE] Created InMemoryContextStore "
            f"(max={config.max_contexts}, ttl={config.default_ttl}s)"
        )

        return store

    @staticmethod
    async def _create_redis_store(config: ContextBridgeConfig) -> ContextStoreProtocol:
        """Create Redis-backed context store."""
        try:
            from backend.core.context.redis_store import RedisContextStore

            store = RedisContextStore(
                redis_url=config.redis_url,
                key_prefix=config.redis_key_prefix,
            )

            await store.connect()

            logger.info(
                f"[BRIDGE] Created RedisContextStore "
                f"(url={config.redis_url})"
            )

            return store

        except ImportError:
            logger.error(
                "[BRIDGE] Redis backend requested but RedisContextStore not available. "
                "Falling back to memory store."
            )
            return await ContextStoreFactory._create_memory_store(config)
        except Exception as e:
            logger.error(
                f"[BRIDGE] Failed to connect to Redis: {e}. "
                "Falling back to memory store."
            )
            return await ContextStoreFactory._create_memory_store(config)

    @staticmethod
    async def _create_hybrid_store(config: ContextBridgeConfig) -> ContextStoreProtocol:
        """Create hybrid memory + Redis store."""
        try:
            from backend.core.context.hybrid_store import HybridContextStore

            store = HybridContextStore(
                redis_url=config.redis_url,
                key_prefix=config.redis_key_prefix,
                max_memory_size=config.max_contexts,
                default_ttl=config.default_ttl,
            )

            await store.initialize()

            logger.info(
                f"[BRIDGE] Created HybridContextStore "
                f"(memory_max={config.max_contexts}, redis={config.redis_url})"
            )

            return store

        except ImportError:
            logger.error(
                "[BRIDGE] Hybrid backend requested but HybridContextStore not available. "
                "Falling back to memory store."
            )
            return await ContextStoreFactory._create_memory_store(config)


# ============================================================================
# Unified Context Bridge
# ============================================================================

class UnifiedContextBridge:
    """
    Unified bridge between Follow-Up Handling and Context Intelligence.

    This bridge:
    1. Manages shared context store (memory/Redis/hybrid)
    2. Coordinates between async_pipeline and pure_vision_intelligence
    3. Integrates with context_aware_handler for screen lock detection
    4. Provides unified telemetry and observability
    5. Supports dynamic configuration without hardcoding
    """

    def __init__(
        self,
        config: Optional[ContextBridgeConfig] = None,
        context_store: Optional[ContextStoreProtocol] = None,
    ):
        """
        Initialize unified context bridge.

        Args:
            config: Bridge configuration (auto-loaded from env if not provided)
            context_store: Pre-existing context store (optional)
        """
        self.config = config or ContextBridgeConfig()
        self._context_store = context_store
        self._initialized = False

        # Component references (set during integration)
        self.async_pipeline = None
        self.vision_intelligence = None
        self.context_aware_handler = None

        # Telemetry
        self._telemetry = None
        if self.config.telemetry_enabled:
            try:
                from backend.core.telemetry.events import get_telemetry
                self._telemetry = get_telemetry()
            except ImportError:
                logger.warning("[BRIDGE] Telemetry framework not available")

        logger.info(
            f"[BRIDGE] Initialized with config: "
            f"backend={self.config.backend.value}, "
            f"max_contexts={self.config.max_contexts}, "
            f"follow_up={self.config.follow_up_enabled}, "
            f"context_aware={self.config.context_aware_enabled}"
        )

    async def initialize(self) -> None:
        """Initialize the bridge and create context store."""
        if self._initialized:
            logger.warning("[BRIDGE] Already initialized")
            return

        # Create context store if not provided
        if not self._context_store:
            self._context_store = await ContextStoreFactory.create(self.config)

        self._initialized = True
        logger.info("[BRIDGE] Initialization complete")

    async def shutdown(self) -> None:
        """Shutdown the bridge and cleanup resources."""
        if not self._initialized:
            return

        logger.info("[BRIDGE] Shutting down...")

        # Stop auto-cleanup
        if self._context_store:
            try:
                await self._context_store.stop_auto_cleanup()
            except Exception as e:
                logger.error(f"[BRIDGE] Error stopping cleanup: {e}")

        self._initialized = False
        logger.info("[BRIDGE] Shutdown complete")

    @property
    def context_store(self) -> ContextStoreProtocol:
        """Get shared context store."""
        if not self._context_store:
            raise RuntimeError(
                "Context store not initialized. Call initialize() first."
            )
        return self._context_store

    def integrate_async_pipeline(self, pipeline: Any) -> None:
        """
        Integrate with AsyncPipeline.

        Args:
            pipeline: AsyncPipeline instance
        """
        self.async_pipeline = pipeline

        # Share context store
        if hasattr(pipeline, 'context_store'):
            pipeline.context_store = self._context_store
            logger.info("[BRIDGE] Shared context store with AsyncPipeline")

        # Apply follow-up configuration
        if hasattr(pipeline, '_follow_up_enabled'):
            pipeline._follow_up_enabled = self.config.follow_up_enabled

        logger.info("[BRIDGE] Integrated with AsyncPipeline")

    def integrate_vision_intelligence(self, vision: Any) -> None:
        """
        Integrate with PureVisionIntelligence.

        Args:
            vision: PureVisionIntelligence instance
        """
        self.vision_intelligence = vision

        # Share context store
        if hasattr(vision, 'context_store'):
            vision.context_store = self._context_store
            logger.info("[BRIDGE] Shared context store with PureVisionIntelligence")

        logger.info("[BRIDGE] Integrated with PureVisionIntelligence")

    def integrate_context_aware_handler(self, handler: Any) -> None:
        """
        Integrate with ContextAwareCommandHandler.

        Args:
            handler: ContextAwareCommandHandler instance
        """
        self.context_aware_handler = handler

        # Share context store if handler supports it
        if hasattr(handler, 'context_store'):
            handler.context_store = self._context_store
            logger.info("[BRIDGE] Shared context store with ContextAwareCommandHandler")

        logger.info("[BRIDGE] Integrated with ContextAwareCommandHandler")

    async def track_pending_question(
        self,
        question_text: str,
        window_type: str,
        window_id: str,
        space_id: str,
        snapshot_id: str,
        summary: str,
        ocr_text: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        **extras: Any,
    ) -> Optional[str]:
        """
        Track a pending question (unified interface).

        This method delegates to vision_intelligence if available,
        otherwise creates context envelope directly.

        Returns:
            Context ID if successful, None otherwise
        """
        if self.vision_intelligence:
            try:
                context_id = await self.vision_intelligence.track_pending_question(
                    question_text=question_text,
                    window_type=window_type,
                    window_id=window_id,
                    space_id=space_id,
                    snapshot_id=snapshot_id,
                    summary=summary,
                    ocr_text=ocr_text,
                    ttl_seconds=ttl_seconds or self.config.default_ttl,
                    **extras,
                )

                # Telemetry
                if self._telemetry:
                    await self._telemetry.track_context_created(
                        context_id=context_id,
                        category="VISION",
                        window_type=window_type,
                    )

                return context_id

            except Exception as e:
                logger.error(f"[BRIDGE] Failed to track pending question: {e}", exc_info=True)
                return None

        else:
            # Fallback: Create context envelope directly
            logger.debug("[BRIDGE] Vision intelligence not integrated, creating context directly")
            try:
                from backend.core.models.context_envelope import (
                    ContextEnvelope,
                    ContextMetadata,
                    ContextCategory,
                    ContextPriority,
                    VisionContextPayload,
                )

                # Create vision context envelope
                metadata = ContextMetadata(
                    category=ContextCategory.VISION,
                    priority=ContextPriority.HIGH,
                    source="unified_context_bridge",
                    tags=(window_type, "pending_question"),
                )

                payload = VisionContextPayload(
                    window_type=window_type,
                    window_id=window_id,
                    space_id=space_id,
                    snapshot_id=snapshot_id,
                    summary=summary,
                    ocr_text=ocr_text,
                )

                envelope = ContextEnvelope(
                    metadata=metadata,
                    payload=payload,
                    ttl_seconds=ttl_seconds or self.config.default_ttl,
                    decay_rate=0.01,  # 1% per second
                )

                context_id = await self._context_store.add(envelope)
                logger.info(
                    f"[BRIDGE] Tracked pending question directly: '{question_text}' "
                    f"(context_id={context_id}, window={window_type})"
                )

                # Telemetry
                if self._telemetry:
                    try:
                        await self._telemetry.track_event(
                            "follow_up.pending_created",
                            {
                                "context_id": context_id,
                                "window_type": window_type,
                                "question_text": question_text[:100],
                                "ttl_seconds": ttl_seconds or self.config.default_ttl,
                                "has_ocr_text": ocr_text is not None,
                            }
                        )
                    except Exception:
                        pass

                return context_id

            except Exception as e:
                logger.error(f"[BRIDGE] Failed to create context directly: {e}", exc_info=True)
                return None

    async def get_pending_context(
        self,
        category: Optional[str] = None,
    ) -> Optional[Any]:
        """
        Get most relevant pending context.

        Args:
            category: Optional category filter (e.g., "VISION")

        Returns:
            Most relevant context envelope or None
        """
        if not self._context_store:
            return None

        # Build query if category specified
        query = None
        if category:
            from backend.core.context.store_interface import ContextQuery
            from backend.core.models.context_envelope import ContextCategory

            # Convert string to ContextCategory enum
            try:
                cat_enum = ContextCategory[category.upper()]
                query = ContextQuery().with_category(cat_enum)
            except KeyError:
                logger.warning(f"[BRIDGE] Invalid category: {category}")

        contexts = await self._context_store.get_most_relevant(
            query=query,
            limit=1,
        )

        return contexts[0] if contexts else None

    async def clear_expired_contexts(self) -> int:
        """
        Clear expired contexts from store.

        Returns:
            Number of contexts cleared
        """
        if not self._context_store:
            return 0

        count = await self._context_store.clear_expired()

        if self._telemetry and count > 0:
            try:
                await self._telemetry.track_event("follow_up.contexts_expired", {"count": count})
            except Exception:
                pass  # Telemetry is optional

        return count

    def get_stats(self) -> Dict[str, Any]:
        """Get bridge statistics."""
        stats = {
            "initialized": self._initialized,
            "backend": self.config.backend.value,
            "follow_up_enabled": self.config.follow_up_enabled,
            "context_aware_enabled": self.config.context_aware_enabled,
            "max_contexts": self.config.max_contexts,
            "components": {
                "async_pipeline": self.async_pipeline is not None,
                "vision_intelligence": self.vision_intelligence is not None,
                "context_aware_handler": self.context_aware_handler is not None,
            },
        }

        # Add store stats if available
        if self._context_store and hasattr(self._context_store, 'stats'):
            stats["store_stats"] = self._context_store.stats()

        return stats


# ============================================================================
# Global Bridge Instance
# ============================================================================

_global_bridge: Optional[UnifiedContextBridge] = None


def get_context_bridge() -> UnifiedContextBridge:
    """Get or create global context bridge instance."""
    global _global_bridge

    if _global_bridge is None:
        _global_bridge = UnifiedContextBridge()

    return _global_bridge


async def initialize_context_bridge(
    config: Optional[ContextBridgeConfig] = None,
) -> UnifiedContextBridge:
    """
    Initialize global context bridge.

    Args:
        config: Optional configuration (loads from env if not provided)

    Returns:
        Initialized bridge instance
    """
    global _global_bridge

    if _global_bridge is not None:
        logger.warning("[BRIDGE] Global bridge already initialized")
        return _global_bridge

    _global_bridge = UnifiedContextBridge(config=config)
    await _global_bridge.initialize()

    return _global_bridge


async def shutdown_context_bridge() -> None:
    """Shutdown global context bridge."""
    global _global_bridge

    if _global_bridge is not None:
        await _global_bridge.shutdown()
        _global_bridge = None
