"""
In-Memory Context Store Implementation

This module provides a high-performance, thread-safe in-memory context store
implementation with LRU (Least Recently Used) eviction policy. It's designed
for single-instance deployments where external dependencies should be minimized.

The store maintains contexts in memory using an OrderedDict for efficient
LRU operations and provides automatic cleanup of expired contexts through
a background task.

Example:
    >>> store = InMemoryContextStore(max_size=500, auto_cleanup_interval=30)
    >>> await store.start_auto_cleanup()
    >>> envelope_id = await store.add(context_envelope)
    >>> retrieved = await store.get(envelope_id)
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from datetime import datetime
from typing import Any

from backend.core.models.context_envelope import ContextEnvelope, ContextPayload, ContextState
from backend.core.async_safety import TimeoutConfig, get_shutdown_event
from .store_interface import ContextStore, ContextQuery

logger = logging.getLogger(__name__)


class InMemoryContextStore(ContextStore):
    """
    Thread-safe in-memory context store with LRU eviction.
    
    This implementation stores context envelopes in memory using an OrderedDict
    to maintain insertion/access order for LRU eviction. It provides automatic
    cleanup of expired contexts and is suitable for single-instance deployments.
    
    Attributes:
        _store: OrderedDict storing context envelopes by ID
        _max_size: Maximum number of contexts to store
        _lock: Async lock for thread safety
        _cleanup_interval: Seconds between automatic cleanup runs
        _cleanup_task: Background task for periodic cleanup
    """

    def __init__(self, max_size: int = 1000, auto_cleanup_interval: int = 60):
        """
        Initialize the in-memory context store.
        
        Args:
            max_size: Maximum number of contexts to store before LRU eviction.
                     Defaults to 1000.
            auto_cleanup_interval: Seconds between automatic cleanup of expired
                                 contexts. Defaults to 60.
        """
        self._store: OrderedDict[str, ContextEnvelope] = OrderedDict()
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._cleanup_interval = auto_cleanup_interval
        self._cleanup_task: asyncio.Task | None = None

    async def start_auto_cleanup(self) -> None:
        """
        Start the background cleanup task.
        
        Creates and starts an asyncio task that periodically removes expired
        contexts from the store. Safe to call multiple times - will not create
        duplicate tasks.
        
        Example:
            >>> store = InMemoryContextStore()
            >>> await store.start_auto_cleanup()
        """
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._auto_cleanup_loop())
            logger.info("Started auto-cleanup task")

    async def stop_auto_cleanup(self) -> None:
        """
        Stop the background cleanup task.
        
        Cancels the cleanup task and waits for it to complete gracefully.
        Safe to call even if no cleanup task is running.
        
        Example:
            >>> await store.stop_auto_cleanup()
        """
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("Stopped auto-cleanup task")

    async def _auto_cleanup_loop(self) -> None:
        """
        Background loop for periodic cleanup of expired contexts.

        Runs continuously until cancelled or shutdown, sleeping for the cleanup interval
        between runs. Logs any cleanup activity and handles exceptions gracefully.

        Raises:
            asyncio.CancelledError: When the task is cancelled during shutdown.
        """
        shutdown_event = get_shutdown_event()
        max_iterations = int(os.getenv("CONTEXT_CLEANUP_MAX_ITERATIONS", "0")) or None
        iteration = 0

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("Context auto-cleanup stopped via shutdown event")
                break

            # Check max iterations (for testing/safety)
            if max_iterations and iteration >= max_iterations:
                logger.info(f"Context auto-cleanup reached max iterations ({max_iterations})")
                break

            iteration += 1

            try:
                await asyncio.sleep(self._cleanup_interval)
                # Add timeout protection for cleanup operation
                removed = await asyncio.wait_for(
                    self.clear_expired(),
                    timeout=TimeoutConfig.CLEANUP
                )
                if removed > 0:
                    logger.debug(f"Auto-cleanup removed {removed} expired contexts")
            except asyncio.TimeoutError:
                logger.warning(f"Context auto-cleanup timed out after {TimeoutConfig.CLEANUP}s")
            except asyncio.CancelledError:
                logger.info("Context auto-cleanup cancelled")
                break
            except Exception as e:
                logger.error(f"Auto-cleanup error: {e}", exc_info=True)

    async def add(self, envelope: ContextEnvelope) -> str:
        """
        Add a context envelope to the store with LRU eviction if needed.
        
        If the store is at capacity and the envelope is new, the least recently
        used envelope will be evicted. The new envelope is marked as most recently
        used.
        
        Args:
            envelope: The context envelope to add to the store.
            
        Returns:
            The ID of the added envelope.
            
        Example:
            >>> envelope = ContextEnvelope(...)
            >>> envelope_id = await store.add(envelope)
            >>> print(f"Added context: {envelope_id}")
        """
        async with self._lock:
            envelope_id = envelope.metadata.id

            # Evict oldest if at capacity
            if len(self._store) >= self._max_size and envelope_id not in self._store:
                evicted_id, _ = self._store.popitem(last=False)
                logger.debug(f"Evicted context {evicted_id} (LRU)")

            self._store[envelope_id] = envelope
            self._store.move_to_end(envelope_id)  # Mark as most recent

            logger.debug(
                f"Added context {envelope_id} "
                f"(category={envelope.metadata.category.name}, "
                f"priority={envelope.metadata.priority.name})"
            )

            return envelope_id

    async def get(self, envelope_id: str) -> ContextEnvelope | None:
        """
        Retrieve a context envelope and refresh its LRU position.
        
        When an envelope is retrieved, it's moved to the end of the LRU order
        (marked as most recently used) and its access metadata is updated.
        
        Args:
            envelope_id: The ID of the envelope to retrieve.
            
        Returns:
            The context envelope if found, None otherwise.
            
        Example:
            >>> envelope = await store.get("context-123")
            >>> if envelope:
            ...     print(f"Retrieved: {envelope.metadata.id}")
        """
        async with self._lock:
            envelope = self._store.get(envelope_id)
            if envelope:
                self._store.move_to_end(envelope_id)  # LRU refresh
                envelope.access()  # Update access metadata
            return envelope

    async def update(self, envelope: ContextEnvelope) -> bool:
        """
        Update an existing context envelope.
        
        Replaces the existing envelope with the provided one and marks it as
        most recently used. The envelope must already exist in the store.
        
        Args:
            envelope: The updated context envelope.
            
        Returns:
            True if the envelope was updated, False if it doesn't exist.
            
        Example:
            >>> success = await store.update(modified_envelope)
            >>> if success:
            ...     print("Envelope updated successfully")
        """
        async with self._lock:
            envelope_id = envelope.metadata.id
            if envelope_id in self._store:
                self._store[envelope_id] = envelope
                self._store.move_to_end(envelope_id)
                return True
            return False

    async def delete(self, envelope_id: str) -> bool:
        """
        Remove a context envelope from the store.
        
        Args:
            envelope_id: The ID of the envelope to remove.
            
        Returns:
            True if the envelope was deleted, False if it doesn't exist.
            
        Example:
            >>> deleted = await store.delete("context-123")
            >>> if deleted:
            ...     print("Context deleted successfully")
        """
        async with self._lock:
            if envelope_id in self._store:
                del self._store[envelope_id]
                logger.debug(f"Deleted context {envelope_id}")
                return True
            return False

    async def query(self, query: ContextQuery) -> list[ContextEnvelope]:
        """
        Execute a query on all contexts in the store.
        
        Applies the query's filters and sorting to all stored contexts.
        The query processing is done outside the lock to minimize lock time.
        
        Args:
            query: The context query to execute.
            
        Returns:
            List of context envelopes matching the query criteria.
            
        Example:
            >>> query = ContextQuery().filter_by_category("CONVERSATION")
            >>> results = await store.query(query)
            >>> print(f"Found {len(results)} conversation contexts")
        """
        async with self._lock:
            contexts = list(self._store.values())

        # Apply query filters/sorting outside lock
        return query.apply(contexts)

    async def get_all(self) -> list[ContextEnvelope]:
        """
        Retrieve all context envelopes in the store.
        
        Returns:
            List of all context envelopes, ordered from most recent to oldest.
            
        Example:
            >>> all_contexts = await store.get_all()
            >>> print(f"Store contains {len(all_contexts)} contexts")
        """
        async with self._lock:
            return list(reversed(self._store.values()))

    async def clear_expired(self) -> int:
        """
        Remove expired and invalidated contexts from the store.
        
        Checks all contexts for expiration or invalidated state and removes them.
        Also tracks the cleanup activity via telemetry if available.
        
        Returns:
            The number of contexts that were removed.
            
        Example:
            >>> removed_count = await store.clear_expired()
            >>> print(f"Cleaned up {removed_count} expired contexts")
        """
        now = datetime.utcnow()
        to_remove = []

        async with self._lock:
            for envelope_id, envelope in self._store.items():
                if envelope.is_expired(now) or envelope.state == ContextState.INVALIDATED:
                    to_remove.append(envelope_id)

            for envelope_id in to_remove:
                del self._store[envelope_id]

        if to_remove:
            logger.debug(f"Cleared {len(to_remove)} expired/invalidated contexts")

            # Telemetry: Track expired contexts
            try:
                from backend.core.telemetry.events import get_telemetry
                telemetry = get_telemetry()
                await telemetry.track_event(
                    "follow_up.contexts_expired",
                    {"count": len(to_remove)}
                )
            except Exception:
                pass  # Telemetry is optional

        return len(to_remove)

    async def clear_all(self) -> None:
        """
        Clear all contexts from the store.
        
        Removes all stored context envelopes. This operation cannot be undone.
        
        Example:
            >>> await store.clear_all()
            >>> print("All contexts cleared")
        """
        async with self._lock:
            self._store.clear()
        logger.info("Cleared all contexts from store")

    async def count(self) -> int:
        """
        Get the current number of contexts in the store.
        
        Returns:
            The number of context envelopes currently stored.
            
        Example:
            >>> current_count = await store.count()
            >>> print(f"Store contains {current_count} contexts")
        """
        async with self._lock:
            return len(self._store)

    # Additional optimized methods

    async def get_by_tags(self, *tags: str) -> list[ContextEnvelope]:
        """
        Fast retrieval of contexts by tags.
        
        Returns all valid contexts that have at least one of the specified tags.
        
        Args:
            *tags: Variable number of tag strings to search for.
            
        Returns:
            List of context envelopes that match any of the specified tags
            and are in a valid state.
            
        Example:
            >>> contexts = await store.get_by_tags("urgent", "customer")
            >>> print(f"Found {len(contexts)} contexts with urgent or customer tags")
        """
        tag_set = set(tags)
        async with self._lock:
            return [
                env
                for env in self._store.values()
                if tag_set.intersection(env.metadata.tags) and env.is_valid()
            ]

    async def get_by_category(self, category: str) -> list[ContextEnvelope]:
        """
        Fast retrieval of contexts by category.
        
        Returns all valid contexts that belong to the specified category.
        
        Args:
            category: The category name to search for (case-insensitive).
            
        Returns:
            List of context envelopes in the specified category that are valid.
            
        Example:
            >>> conversations = await store.get_by_category("conversation")
            >>> print(f"Found {len(conversations)} conversation contexts")
        """
        async with self._lock:
            return [
                env
                for env in self._store.values()
                if env.metadata.category.name == category.upper() and env.is_valid()
            ]

    def __len__(self) -> int:
        """
        Get the synchronous length of the store for debugging.
        
        Note: This method is not thread-safe and should only be used for
        debugging purposes when you're certain no concurrent access is occurring.
        
        Returns:
            The number of contexts currently in the store.
        """
        return len(self._store)

    async def get_stats(self) -> dict[str, Any]:
        """
        Get comprehensive statistics about the store.
        
        Provides detailed information about the current state of the store
        including counts by state and category, utilization, and average
        relevance scores.
        
        Returns:
            Dictionary containing store statistics with the following keys:
            - total: Total number of contexts
            - max_size: Maximum capacity of the store
            - utilization: Percentage of capacity used (0.0 to 1.0)
            - by_state: Count of contexts by state
            - by_category: Count of contexts by category
            - avg_relevance: Average relevance score across all contexts
            
        Example:
            >>> stats = await store.get_stats()
            >>> print(f"Store utilization: {stats['utilization']:.1%}")
            >>> print(f"Average relevance: {stats['avg_relevance']:.2f}")
        """
        async with self._lock:
            total = len(self._store)
            by_state = {}
            by_category = {}
            avg_relevance = 0.0

            for env in self._store.values():
                # Count by state
                state_name = env.state.name
                by_state[state_name] = by_state.get(state_name, 0) + 1

                # Count by category
                cat_name = env.metadata.category.name
                by_category[cat_name] = by_category.get(cat_name, 0) + 1

                # Sum relevance
                avg_relevance += env.relevance_score()

            if total > 0:
                avg_relevance /= total

            return {
                "total": total,
                "max_size": self._max_size,
                "utilization": total / self._max_size if self._max_size > 0 else 0,
                "by_state": by_state,
                "by_category": by_category,
                "avg_relevance": avg_relevance,
            }


# Register with factory
from .store_interface import ContextStoreFactory

ContextStoreFactory.register("memory", InMemoryContextStore)