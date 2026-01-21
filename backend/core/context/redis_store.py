"""
Redis-backed Context Store Implementation

This module provides a Redis-based implementation of the ContextStore interface,
supporting distributed deployments, persistence, and clustering. It uses Redis
data structures for efficient storage and querying of context envelopes.

Features:
    - Automatic TTL-based expiration
    - Relevance-based sorting using Redis Sorted Sets
    - Category and tag indexing for fast queries
    - Pub/sub support for distributed notifications
    - Connection pooling and error handling

Example:
    >>> store = RedisContextStore("redis://localhost:6379")
    >>> await store.connect()
    >>> envelope_id = await store.add(envelope)
    >>> retrieved = await store.get(envelope_id)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

try:
    import redis.asyncio as aioredis
    from redis.asyncio import Redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    Redis = Any  # type stub

from backend.core.models.context_envelope import (
    ContextEnvelope,
    ContextPayload,
    ContextMetadata,
    ContextCategory,
    ContextPriority,
    ContextState,
)
from .store_interface import ContextStore, ContextQuery

logger = logging.getLogger(__name__)


class RedisContextStore(ContextStore):
    """
    Redis-backed context store with automatic expiry and pub/sub support.
    
    This implementation uses Redis data structures for efficient storage and
    querying of context envelopes. It provides automatic TTL-based expiration,
    relevance-based sorting, and indexed access by category and tags.
    
    Attributes:
        _redis: The Redis connection instance
        _redis_url: Redis connection URL
        _key_prefix: Prefix for all Redis keys
        _pool_size: Maximum connections in the pool
        _deserializer: Custom payload deserializer function
        _envelope_key: Function to generate envelope keys
        _index_key: Key for the main relevance index
        _category_index: Function to generate category index keys
        _tag_index: Function to generate tag index keys
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        key_prefix: str = "jarvis:context:",
        pool_size: int = 10,
        payload_deserializer: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        """
        Initialize Redis context store.
        
        Args:
            redis_url: Redis connection URL (default: redis://localhost:6379)
            key_prefix: Prefix for all Redis keys (default: jarvis:context:)
            pool_size: Maximum connections in pool (default: 10)
            payload_deserializer: Custom function to deserialize payloads
            
        Raises:
            RuntimeError: If Redis package is not available
        """
        if not REDIS_AVAILABLE:
            raise RuntimeError(
                "Redis support requires 'redis' package. Install with: pip install redis"
            )

        self._redis: Redis | None = None
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._pool_size = pool_size
        self._deserializer = payload_deserializer or self._default_deserializer

        # Redis key patterns
        self._envelope_key = lambda eid: f"{self._key_prefix}env:{eid}"
        self._index_key = f"{self._key_prefix}index:relevance"
        self._category_index = lambda cat: f"{self._key_prefix}idx:cat:{cat}"
        self._tag_index = lambda tag: f"{self._key_prefix}idx:tag:{tag}"

    async def connect(self) -> None:
        """
        Establish Redis connection pool.
        
        Creates an async Redis connection with the configured pool size
        and connection parameters. Must be called before using the store.
        
        Raises:
            redis.ConnectionError: If connection to Redis fails
        """
        if self._redis is None:
            self._redis = await aioredis.from_url(
                self._redis_url,
                max_connections=self._pool_size,
                decode_responses=False,  # We handle JSON encoding
            )
            logger.info(f"Connected to Redis: {self._redis_url}")

    async def disconnect(self) -> None:
        """
        Close Redis connection.
        
        Properly closes the Redis connection pool and cleans up resources.
        Should be called when shutting down the application.
        """
        if self._redis:
            await self._redis.aclose()  # v93.14
            self._redis = None
            logger.info("Disconnected from Redis")

    def _ensure_connected(self) -> Redis:
        """
        Ensure connection is active.
        
        Returns:
            Redis: The active Redis connection instance
            
        Raises:
            RuntimeError: If store is not connected
        """
        if self._redis is None:
            raise RuntimeError("Redis store not connected. Call connect() first.")
        return self._redis

    async def add(self, envelope: ContextEnvelope) -> str:
        """
        Add envelope with automatic TTL and indexing.
        
        Stores the envelope in Redis with automatic expiration based on TTL,
        and updates all relevant indexes for efficient querying.
        
        Args:
            envelope: The context envelope to store
            
        Returns:
            str: The envelope ID that was stored
            
        Raises:
            redis.RedisError: If Redis operation fails
            
        Example:
            >>> envelope = ContextEnvelope(metadata, payload)
            >>> envelope_id = await store.add(envelope)
        """
        redis = self._ensure_connected()
        envelope_id = envelope.metadata.id

        # Serialize envelope
        envelope_data = json.dumps(envelope.to_dict()).encode("utf-8")

        # Pipeline operations for atomicity
        pipe = redis.pipeline()

        # Store envelope with TTL
        envelope_key = self._envelope_key(envelope_id)
        pipe.set(envelope_key, envelope_data, ex=envelope.ttl_seconds)

        # Add to relevance-sorted index
        relevance = envelope.relevance_score()
        pipe.zadd(self._index_key, {envelope_id: relevance})

        # Add to category index
        cat_key = self._category_index(envelope.metadata.category.name)
        pipe.sadd(cat_key, envelope_id)
        pipe.expire(cat_key, envelope.ttl_seconds)

        # Add to tag indexes
        for tag in envelope.metadata.tags:
            tag_key = self._tag_index(tag)
            pipe.sadd(tag_key, envelope_id)
            pipe.expire(tag_key, envelope.ttl_seconds)

        await pipe.execute()

        logger.debug(f"Stored context {envelope_id} in Redis with TTL={envelope.ttl_seconds}s")
        return envelope_id

    async def get(self, envelope_id: str) -> ContextEnvelope | None:
        """
        Retrieve envelope by ID and update access metadata.
        
        Fetches the envelope from Redis, deserializes it, updates access
        tracking, and persists the access update back to Redis.
        
        Args:
            envelope_id: The unique identifier of the envelope
            
        Returns:
            ContextEnvelope | None: The envelope if found, None otherwise
            
        Raises:
            redis.RedisError: If Redis operation fails
            json.JSONDecodeError: If envelope data is corrupted
            
        Example:
            >>> envelope = await store.get("envelope-123")
            >>> if envelope:
            ...     print(f"Found: {envelope.metadata.id}")
        """
        redis = self._ensure_connected()
        envelope_key = self._envelope_key(envelope_id)

        data = await redis.get(envelope_key)
        if not data:
            return None

        # Deserialize
        envelope_dict = json.loads(data.decode("utf-8"))
        envelope = self._deserialize_envelope(envelope_dict)

        # Update access metadata
        envelope.access()
        await self.update(envelope)  # Persist access update

        return envelope

    async def update(self, envelope: ContextEnvelope) -> bool:
        """
        Update envelope and refresh TTL.
        
        Updates an existing envelope in Redis, refreshing its TTL and
        updating all indexes. If the envelope doesn't exist, returns False.
        
        Args:
            envelope: The updated envelope to store
            
        Returns:
            bool: True if envelope was updated, False if not found
            
        Raises:
            redis.RedisError: If Redis operation fails
        """
        redis = self._ensure_connected()
        envelope_id = envelope.metadata.id

        # Check existence
        envelope_key = self._envelope_key(envelope_id)
        if not await redis.exists(envelope_key):
            return False

        # Re-add (updates data and refreshes TTL)
        await self.add(envelope)
        return True

    async def delete(self, envelope_id: str) -> bool:
        """
        Remove envelope and all index references.
        
        Deletes the envelope from Redis and cleans up all associated
        index entries to prevent orphaned references.
        
        Args:
            envelope_id: The unique identifier of the envelope to delete
            
        Returns:
            bool: True if envelope was deleted, False if not found
            
        Raises:
            redis.RedisError: If Redis operation fails
        """
        redis = self._ensure_connected()
        envelope_key = self._envelope_key(envelope_id)

        # Get envelope to clean up indexes
        envelope = await self.get(envelope_id)
        if not envelope:
            return False

        pipe = redis.pipeline()

        # Remove envelope
        pipe.delete(envelope_key)

        # Remove from indexes
        pipe.zrem(self._index_key, envelope_id)
        pipe.srem(self._category_index(envelope.metadata.category.name), envelope_id)
        for tag in envelope.metadata.tags:
            pipe.srem(self._tag_index(tag), envelope_id)

        await pipe.execute()

        logger.debug(f"Deleted context {envelope_id} from Redis")
        return True

    async def query(self, query: ContextQuery) -> list[ContextEnvelope]:
        """
        Execute query by loading all envelopes and filtering.
        
        Note: This implementation loads all envelopes and applies filters
        in memory. For large datasets, consider implementing Lua scripts
        for server-side filtering.
        
        Args:
            query: The query object with filter criteria
            
        Returns:
            list[ContextEnvelope]: List of envelopes matching the query
            
        Raises:
            redis.RedisError: If Redis operation fails
        """
        all_envelopes = await self.get_all()
        return query.apply(all_envelopes)

    async def get_all(self) -> list[ContextEnvelope]:
        """
        Retrieve all envelopes ordered by relevance score.
        
        Fetches all envelope IDs from the relevance index (highest score first)
        and retrieves each envelope, filtering out expired or invalid ones.
        
        Returns:
            list[ContextEnvelope]: All valid envelopes ordered by relevance
            
        Raises:
            redis.RedisError: If Redis operation fails
        """
        redis = self._ensure_connected()

        # Get all IDs from relevance index (highest first)
        envelope_ids = await redis.zrevrange(self._index_key, 0, -1)

        envelopes = []
        for eid in envelope_ids:
            eid_str = eid.decode("utf-8")
            envelope = await self.get(eid_str)
            if envelope and envelope.is_valid():
                envelopes.append(envelope)

        return envelopes

    async def clear_expired(self) -> int:
        """
        Clean up orphaned index entries for expired envelopes.
        
        Redis automatically expires keys via TTL, but index entries may
        become orphaned. This method identifies and removes such entries.
        
        Returns:
            int: Number of orphaned entries removed
            
        Raises:
            redis.RedisError: If Redis operation fails
        """
        redis = self._ensure_connected()

        # Get all envelope IDs from index
        all_ids = await redis.zrange(self._index_key, 0, -1)

        removed = 0
        for eid in all_ids:
            eid_str = eid.decode("utf-8")
            envelope_key = self._envelope_key(eid_str)

            # Check if envelope still exists
            if not await redis.exists(envelope_key):
                # Remove from index
                await redis.zrem(self._index_key, eid_str)
                removed += 1

        if removed > 0:
            logger.debug(f"Cleaned up {removed} orphaned index entries")

        return removed

    async def clear_all(self) -> None:
        """
        Clear all contexts using pattern-based deletion.
        
        Removes all keys matching the store's key prefix pattern.
        This is a destructive operation that cannot be undone.
        
        Raises:
            redis.RedisError: If Redis operation fails
        """
        redis = self._ensure_connected()

        # Find all keys matching prefix
        pattern = f"{self._key_prefix}*"
        cursor = 0

        while True:
            cursor, keys = await redis.scan(cursor, match=pattern, count=100)
            if keys:
                await redis.delete(*keys)
            if cursor == 0:
                break

        logger.info("Cleared all contexts from Redis")

    async def count(self) -> int:
        """
        Get total count of stored envelopes.
        
        Uses the relevance index to efficiently count stored envelopes
        without loading all data.
        
        Returns:
            int: Total number of stored envelopes
            
        Raises:
            redis.RedisError: If Redis operation fails
        """
        redis = self._ensure_connected()
        return await redis.zcard(self._index_key)

    # Optimized queries using Redis indexes

    async def get_by_category(self, category: str) -> list[ContextEnvelope]:
        """
        Fast category-based retrieval using Redis sets.
        
        Uses the category index to efficiently find all envelopes
        in a specific category without scanning all data.
        
        Args:
            category: The category name to search for
            
        Returns:
            list[ContextEnvelope]: All valid envelopes in the category
            
        Raises:
            redis.RedisError: If Redis operation fails
        """
        redis = self._ensure_connected()
        cat_key = self._category_index(category)

        envelope_ids = await redis.smembers(cat_key)
        envelopes = []

        for eid in envelope_ids:
            eid_str = eid.decode("utf-8")
            envelope = await self.get(eid_str)
            if envelope and envelope.is_valid():
                envelopes.append(envelope)

        return envelopes

    async def get_by_tags(self, *tags: str) -> list[ContextEnvelope]:
        """
        Fast tag-based retrieval using Redis set intersection.
        
        Finds envelopes that have ALL specified tags by performing
        a Redis set intersection operation on tag indexes.
        
        Args:
            *tags: Variable number of tag names to match
            
        Returns:
            list[ContextEnvelope]: Envelopes containing all specified tags
            
        Raises:
            redis.RedisError: If Redis operation fails
            
        Example:
            >>> envelopes = await store.get_by_tags("urgent", "user-query")
        """
        redis = self._ensure_connected()

        if not tags:
            return []

        # Redis set intersection
        tag_keys = [self._tag_index(tag) for tag in tags]
        envelope_ids = await redis.sinter(*tag_keys)

        envelopes = []
        for eid in envelope_ids:
            eid_str = eid.decode("utf-8")
            envelope = await self.get(eid_str)
            if envelope and envelope.is_valid():
                envelopes.append(envelope)

        return envelopes

    async def get_top_relevant(self, limit: int = 10) -> list[ContextEnvelope]:
        """
        Get top N envelopes by relevance score using Redis sorted set.
        
        Efficiently retrieves the highest-scoring envelopes without
        loading all data, using the pre-computed relevance index.
        
        Args:
            limit: Maximum number of envelopes to return (default: 10)
            
        Returns:
            list[ContextEnvelope]: Top envelopes ordered by relevance
            
        Raises:
            redis.RedisError: If Redis operation fails
            
        Example:
            >>> top_contexts = await store.get_top_relevant(5)
        """
        redis = self._ensure_connected()

        # Get top IDs from sorted set
        envelope_ids = await redis.zrevrange(self._index_key, 0, limit - 1)

        envelopes = []
        for eid in envelope_ids:
            eid_str = eid.decode("utf-8")
            envelope = await self.get(eid_str)
            if envelope and envelope.is_valid():
                envelopes.append(envelope)

        return envelopes

    # Serialization helpers

    def _deserialize_envelope(self, data: dict[str, Any]) -> ContextEnvelope:
        """
        Reconstruct envelope from serialized dictionary.
        
        Converts a dictionary representation back into a ContextEnvelope
        object, handling all nested objects and type conversions.
        
        Args:
            data: Dictionary containing serialized envelope data
            
        Returns:
            ContextEnvelope: Reconstructed envelope object
            
        Raises:
            KeyError: If required fields are missing
            ValueError: If data format is invalid
        """
        # Reconstruct metadata
        meta_dict = data["metadata"]
        metadata = ContextMetadata(
            id=meta_dict["id"],
            created_at=datetime.fromisoformat(meta_dict["created_at"]),
            category=ContextCategory[meta_dict["category"]],
            priority=ContextPriority[meta_dict["priority"]],
            source=meta_dict["source"],
            tags=tuple(meta_dict["tags"]),
            parent_id=meta_dict.get("parent_id"),
        )

        # Reconstruct payload (use custom deserializer if provided)
        payload = self._deserializer(data["payload"])

        # Reconstruct envelope
        return ContextEnvelope(
            metadata=metadata,
            payload=payload,
            state=ContextState(data["state"]),
            ttl_seconds=data["ttl_seconds"],
            decay_rate=data["decay_rate"],
            access_count=data["access_count"],
            last_accessed=(
                datetime.fromisoformat(data["last_accessed"])
                if data["last_accessed"]
                else None
            ),
            constraints=data["constraints"],
        )

    @staticmethod
    def _default_deserializer(payload_dict: dict[str, Any]) -> dict[str, Any]:
        """
        Default payload deserializer that returns the dictionary as-is.
        
        This is the fallback deserializer used when no custom deserializer
        is provided. It assumes payloads are simple dictionaries.
        
        Args:
            payload_dict: The serialized payload dictionary
            
        Returns:
            dict[str, Any]: The payload dictionary unchanged
        """
        return payload_dict


# Register with factory
from .store_interface import ContextStoreFactory

if REDIS_AVAILABLE:
    ContextStoreFactory.register("redis", RedisContextStore)