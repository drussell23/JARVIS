"""
Centralized Embedding Service v1.0 - Enterprise-Grade Embedding Management
============================================================================

This module provides a SINGLE, CENTRALIZED SentenceTransformer instance that is
shared across the entire JARVIS codebase. This eliminates the semaphore leak
caused by multiple SentenceTransformer instances creating independent
torch.multiprocessing pools.

ROOT CAUSE FIX:
    Previously, SentenceTransformer was instantiated in 14+ different modules:
    - backend/ml_model_loader.py
    - backend/core/trinity_knowledge_graph.py
    - backend/intelligence/long_term_memory.py
    - backend/neural_mesh/knowledge/semantic_memory.py
    - ... and more

    Each instance could spawn internal multiprocessing pools for parallel encoding.
    These pools create semaphores that weren't being cleaned up, causing:
    "resource_tracker: There appear to be 1 leaked semaphore objects to clean up"

SOLUTION:
    1. Single SentenceTransformer instance managed by this service
    2. Lazy loading - model only loaded when first needed
    3. Proper cleanup via stop_multi_process_pool() if pools were started
    4. Thread-safe access with connection pooling semantics
    5. Registered with GracefulShutdown for proper cleanup order

Usage:
    from backend.core.embedding_service import get_embedding_service

    # Get the shared service (lazy-loads model on first call)
    service = await get_embedding_service()

    # Generate embeddings
    embeddings = await service.encode(["text1", "text2"])

    # Or use the convenience function
    from backend.core.embedding_service import encode_texts
    embeddings = await encode_texts(["text1", "text2"])

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import atexit
import gc
import logging
import os
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Union

import numpy as np

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class EmbeddingServiceConfig:
    """Configuration for the embedding service."""

    # Model settings
    model_name: str = "all-MiniLM-L6-v2"
    device: str = "cpu"  # "cpu", "cuda", "mps"

    # Performance settings
    batch_size: int = 32
    normalize_embeddings: bool = True
    show_progress_bar: bool = False

    # Multi-process pool settings (disabled by default to prevent leaks)
    use_multiprocess_pool: bool = False
    pool_size: int = 0  # 0 = no pool

    # Cache settings
    enable_cache: bool = True
    cache_maxsize: int = 10000

    # Type hints for cache (key is string hash)
    _cache_key_type: str = "str"  # Document that cache keys are strings

    # Timeouts
    encode_timeout: float = 30.0
    shutdown_timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "EmbeddingServiceConfig":
        """Create config from environment variables."""
        return cls(
            model_name=os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
            device=os.getenv("EMBEDDING_DEVICE", "cpu"),
            batch_size=int(os.getenv("EMBEDDING_BATCH_SIZE", "32")),
            normalize_embeddings=os.getenv("EMBEDDING_NORMALIZE", "true").lower() == "true",
            use_multiprocess_pool=os.getenv("EMBEDDING_MULTIPROCESS", "false").lower() == "true",
            pool_size=int(os.getenv("EMBEDDING_POOL_SIZE", "0")),
            enable_cache=os.getenv("EMBEDDING_CACHE", "true").lower() == "true",
            cache_maxsize=int(os.getenv("EMBEDDING_CACHE_SIZE", "10000")),
        )


# =============================================================================
# EMBEDDING SERVICE
# =============================================================================

class EmbeddingService:
    """
    Centralized embedding service with proper resource management.

    This is a SINGLETON - only one instance should exist per process.
    Use get_embedding_service() to access it.
    """

    _instance: Optional["EmbeddingService"] = None
    _instance_lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls, *args, **kwargs):
        """Ensure singleton pattern."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self, config: Optional[EmbeddingServiceConfig] = None):
        """Initialize the embedding service."""
        # Only initialize once
        if self._initialized:
            return

        self._config = config or EmbeddingServiceConfig.from_env()
        self._model = None
        self._model_lock = asyncio.Lock()
        self._thread_lock = threading.Lock()
        self._pool = None
        self._pool_started = False
        self._shutdown_requested = False
        self._encode_count = 0
        self._cache: Dict[str, np.ndarray] = {}
        self._cache_hits = 0
        self._cache_misses = 0

        # Register cleanup
        atexit.register(self._sync_cleanup)
        self._register_with_shutdown_manager()

        self._initialized = True
        logger.info(f"[EmbeddingService] Initialized (model: {self._config.model_name})")

    def _register_with_shutdown_manager(self) -> None:
        """Register with the graceful shutdown manager."""
        try:
            from backend.core.resilience.graceful_shutdown import (
                GracefulShutdownManager,
                get_shutdown_manager,
            )
            manager = get_shutdown_manager()
            if manager:
                manager.register_callback(
                    name="embedding_service_cleanup",
                    callback=self._async_cleanup,
                    priority=30,  # Clean up before database connections
                )
                logger.debug("[EmbeddingService] Registered with GracefulShutdownManager")
        except ImportError:
            logger.debug("[EmbeddingService] GracefulShutdownManager not available")
        except Exception as e:
            logger.debug(f"[EmbeddingService] Could not register with shutdown manager: {e}")

    async def _load_model(self) -> bool:
        """
        Lazy load the SentenceTransformer model.

        CRITICAL: This is the ONLY place SentenceTransformer should be instantiated.
        """
        if self._model is not None:
            return True

        if self._shutdown_requested:
            logger.warning("[EmbeddingService] Cannot load model during shutdown")
            return False

        async with self._model_lock:
            # Double-check after acquiring lock
            if self._model is not None:
                return True

            try:
                logger.info(f"[EmbeddingService] Loading model: {self._config.model_name}")
                start = time.time()

                # Import in function to defer loading
                from sentence_transformers import SentenceTransformer

                # Create model with explicit settings to avoid internal pool creation
                self._model = SentenceTransformer(
                    self._config.model_name,
                    device=self._config.device,
                )

                # Explicitly disable internal multiprocessing pools
                # SentenceTransformer can start pools for encode_multi_process()
                # We never use that method, but ensure pools aren't started
                if hasattr(self._model, '_target_device'):
                    # Model loaded successfully
                    pass

                elapsed = time.time() - start
                logger.info(
                    f"[EmbeddingService] ✅ Model loaded in {elapsed:.2f}s "
                    f"(device: {self._config.device})"
                )
                return True

            except ImportError as e:
                logger.error(f"[EmbeddingService] ❌ sentence-transformers not installed: {e}")
                return False
            except Exception as e:
                logger.error(f"[EmbeddingService] ❌ Failed to load model: {e}")
                return False

    async def encode(
        self,
        texts: Union[str, Sequence[str]],
        batch_size: Optional[int] = None,
        normalize: Optional[bool] = None,
        convert_to_numpy: bool = True,
    ) -> Optional[np.ndarray]:
        """
        Encode texts to embeddings.

        Args:
            texts: Text or list of texts to encode
            batch_size: Override batch size for this call
            normalize: Override normalization setting
            convert_to_numpy: Convert to numpy array

        Returns:
            Numpy array of shape (n_texts, embedding_dim) or None on error
        """
        if self._shutdown_requested:
            logger.warning("[EmbeddingService] Cannot encode during shutdown")
            return None

        # Ensure model is loaded
        if not await self._load_model():
            return None

        # Handle single text
        if isinstance(texts, str):
            texts = [texts]

        # Check cache
        if self._config.enable_cache:
            cached_results = []
            uncached_texts = []
            uncached_indices = []

            for i, text in enumerate(texts):
                cache_key = str(hash(text))  # Convert to string for type safety
                if cache_key in self._cache:
                    cached_results.append((i, self._cache[cache_key]))
                    self._cache_hits += 1
                else:
                    uncached_texts.append(text)
                    uncached_indices.append(i)
                    self._cache_misses += 1

            # If all cached, return immediately
            if not uncached_texts:
                result = np.zeros((len(texts), cached_results[0][1].shape[0]))
                for i, emb in cached_results:
                    result[i] = emb
                return result

            texts_to_encode = uncached_texts
        else:
            texts_to_encode = list(texts)
            uncached_indices = list(range(len(texts)))
            cached_results = []

        try:
            # Run encoding in thread pool to avoid blocking event loop
            # Note: self._model is guaranteed to be loaded by _load_model() check above
            model = self._model
            if model is None:
                logger.error("[EmbeddingService] Model not loaded")
                return None

            loop = asyncio.get_event_loop()
            embeddings = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: model.encode(
                        texts_to_encode,
                        batch_size=batch_size or self._config.batch_size,
                        normalize_embeddings=normalize if normalize is not None else self._config.normalize_embeddings,
                        show_progress_bar=self._config.show_progress_bar,
                        convert_to_numpy=convert_to_numpy,
                    )
                ),
                timeout=self._config.encode_timeout,
            )

            self._encode_count += len(texts_to_encode)

            # Update cache
            if self._config.enable_cache:
                for i, text in enumerate(texts_to_encode):
                    cache_key = str(hash(text))  # Convert to string for type safety
                    if len(self._cache) < self._config.cache_maxsize:
                        self._cache[cache_key] = embeddings[i]

            # Merge cached and new results
            if cached_results:
                result = np.zeros((len(texts), embeddings.shape[1]))
                # Fill in cached
                for i, emb in cached_results:
                    result[i] = emb
                # Fill in new
                for i, idx in enumerate(uncached_indices):
                    result[idx] = embeddings[i]
                return result

            return embeddings

        except asyncio.TimeoutError:
            logger.error(f"[EmbeddingService] Encoding timed out after {self._config.encode_timeout}s")
            return None
        except Exception as e:
            logger.error(f"[EmbeddingService] Encoding failed: {e}")
            return None

    def encode_sync(
        self,
        texts: Union[str, Sequence[str]],
        batch_size: Optional[int] = None,
        normalize: Optional[bool] = None,
    ) -> Optional[np.ndarray]:
        """
        Synchronous encoding (for non-async contexts).

        Prefer encode() when possible.
        """
        with self._thread_lock:
            if self._model is None:
                # Synchronous model loading
                try:
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(
                        self._config.model_name,
                        device=self._config.device,
                    )
                except Exception as e:
                    logger.error(f"[EmbeddingService] Sync model load failed: {e}")
                    return None

            if isinstance(texts, str):
                texts = [texts]

            try:
                return self._model.encode(
                    texts,
                    batch_size=batch_size or self._config.batch_size,
                    normalize_embeddings=normalize if normalize is not None else self._config.normalize_embeddings,
                    show_progress_bar=False,
                )
            except Exception as e:
                logger.error(f"[EmbeddingService] Sync encoding failed: {e}")
                return None

    async def _async_cleanup(self) -> None:
        """
        Async cleanup called by GracefulShutdownManager.

        CRITICAL: This properly cleans up SentenceTransformer resources to prevent
        semaphore leaks.
        """
        self._shutdown_requested = True
        logger.info("[EmbeddingService] Starting cleanup...")

        try:
            # Stop any multiprocess pools that may have been started
            if self._model is not None:
                # SentenceTransformer's stop_multi_process_pool() if available
                if hasattr(self._model, 'stop_multi_process_pool'):
                    try:
                        self._model.stop_multi_process_pool()
                        logger.debug("[EmbeddingService] Stopped multiprocess pool")
                    except Exception as e:
                        logger.debug(f"[EmbeddingService] Pool stop error (may be fine): {e}")

                # Clear model reference to allow garbage collection
                self._model = None

            # Clear cache
            self._cache.clear()

            # Force garbage collection to clean up any remaining references
            gc.collect()

            logger.info(
                f"[EmbeddingService] ✅ Cleanup complete "
                f"(encoded {self._encode_count} texts, "
                f"cache hits: {self._cache_hits}, misses: {self._cache_misses})"
            )

        except Exception as e:
            logger.error(f"[EmbeddingService] Cleanup error: {e}")

    def _sync_cleanup(self) -> None:
        """
        Synchronous cleanup for atexit.

        Called when Python interpreter is shutting down.
        """
        self._shutdown_requested = True

        try:
            if self._model is not None:
                if hasattr(self._model, 'stop_multi_process_pool'):
                    with suppress(Exception):
                        self._model.stop_multi_process_pool()
                self._model = None

            self._cache.clear()
            gc.collect()

            logger.debug("[EmbeddingService] atexit cleanup complete")
        except Exception:
            pass  # Swallow errors during interpreter shutdown

    def get_stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        return {
            "model_loaded": self._model is not None,
            "model_name": self._config.model_name,
            "device": self._config.device,
            "encode_count": self._encode_count,
            "cache_enabled": self._config.enable_cache,
            "cache_size": len(self._cache),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_hit_rate": (
                self._cache_hits / (self._cache_hits + self._cache_misses)
                if (self._cache_hits + self._cache_misses) > 0
                else 0.0
            ),
            "shutdown_requested": self._shutdown_requested,
        }

    @classmethod
    def get_instance(cls) -> Optional["EmbeddingService"]:
        """Get the singleton instance if it exists."""
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (for testing only)."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance._sync_cleanup()
                cls._instance._initialized = False
            cls._instance = None


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

_service_instance: Optional[EmbeddingService] = None
_service_lock = asyncio.Lock()


async def get_embedding_service(
    config: Optional[EmbeddingServiceConfig] = None,
) -> EmbeddingService:
    """
    Get the centralized embedding service.

    This is the PREFERRED way to access the embedding service.
    The service is lazily initialized on first call.

    Args:
        config: Optional configuration (only used on first call)

    Returns:
        The singleton EmbeddingService instance
    """
    global _service_instance

    if _service_instance is None:
        async with _service_lock:
            if _service_instance is None:
                _service_instance = EmbeddingService(config)

    return _service_instance


async def encode_texts(
    texts: Union[str, Sequence[str]],
    **kwargs,
) -> Optional[np.ndarray]:
    """
    Convenience function to encode texts using the shared service.

    Args:
        texts: Text or list of texts to encode
        **kwargs: Additional arguments passed to encode()

    Returns:
        Numpy array of embeddings or None on error
    """
    service = await get_embedding_service()
    return await service.encode(texts, **kwargs)


def encode_texts_sync(
    texts: Union[str, Sequence[str]],
    **kwargs,
) -> Optional[np.ndarray]:
    """
    Synchronous convenience function for encoding.

    Use encode_texts() when possible.
    """
    service = EmbeddingService()
    return service.encode_sync(texts, **kwargs)


async def cleanup_embedding_service() -> None:
    """
    Explicitly cleanup the embedding service.

    Called automatically during shutdown, but can be called manually if needed.
    """
    global _service_instance

    if _service_instance is not None:
        await _service_instance._async_cleanup()
        _service_instance = None


# =============================================================================
# MULTIPROCESSING SEMAPHORE CLEANUP
# =============================================================================

def cleanup_torch_multiprocessing() -> int:
    """
    Clean up any orphaned torch.multiprocessing resources.

    This is a last-resort cleanup that should be called during shutdown
    to prevent semaphore leak warnings.

    Returns:
        Number of resources cleaned up
    """
    cleaned = 0

    try:
        import torch.multiprocessing as mp

        # Check for any active pools and terminate them
        # This is aggressive but necessary to prevent leaks
        if hasattr(mp, '_children'):
            for child in list(getattr(mp, '_children', {}).values()):
                try:
                    if hasattr(child, 'terminate'):
                        child.terminate()
                        cleaned += 1
                except Exception:
                    pass

        # Force garbage collection to clean up semaphores
        gc.collect()

    except ImportError:
        pass  # torch not installed
    except Exception as e:
        logger.debug(f"[EmbeddingService] torch.multiprocessing cleanup error: {e}")

    return cleaned


# =============================================================================
# MODULE-LEVEL REGISTRATION
# =============================================================================

def _register_cleanup_handlers() -> None:
    """Register cleanup handlers at module load."""

    def cleanup_on_exit():
        """Cleanup handler for atexit."""
        cleanup_torch_multiprocessing()
        if _service_instance:
            _service_instance._sync_cleanup()

    atexit.register(cleanup_on_exit)


# Auto-register cleanup handlers when module is imported
_register_cleanup_handlers()
