"""
Intelligent Caching System for Memory-Managed Components
Optimizes performance by caching frequently used results
"""

import asyncio
import logging
from typing import Dict, Any, Optional, List, Tuple, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, field
import hashlib
import json
from collections import OrderedDict, defaultdict
from memory.memory_manager import M1MemoryManager

logger = logging.getLogger(__name__)

@dataclass
class CacheEntry:
    """Represents a cached result"""

    key: str
    value: Any
    size_bytes: int
    created_at: datetime
    last_accessed: datetime
    access_count: int = 0
    ttl_seconds: Optional[int] = None

    def is_expired(self) -> bool:
        """Check if cache entry has expired"""
        if self.ttl_seconds is None:
            return False
        return datetime.now() > self.created_at + timedelta(seconds=self.ttl_seconds)

    def access(self):
        """Update access statistics"""
        self.last_accessed = datetime.now()
        self.access_count += 1

class LRUCache:
    """
    Least Recently Used cache with memory constraints
    """

    def __init__(self, max_size_mb: int = 100, max_entries: int = 1000):
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.max_entries = max_entries
        self.cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.total_size = 0
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache"""
        async with self._lock:
            if key in self.cache:
                entry = self.cache[key]

                # Check expiration
                if entry.is_expired():
                    await self._remove(key)
                    return None

                # Update access and move to end (most recently used)
                entry.access()
                self.cache.move_to_end(key)
                return entry.value

            return None

    async def set(
        self, key: str, value: Any, ttl_seconds: Optional[int] = None
    ) -> bool:
        """Set value in cache"""
        async with self._lock:
            # Calculate size
            try:
                value_str = json.dumps(value, default=str)
                size_bytes = len(value_str.encode("utf-8"))
            except Exception:
                size_bytes = 1024  # Default size for non-serializable objects

            # Check if we need to evict entries
            while (
                self.total_size + size_bytes > self.max_size_bytes
                or len(self.cache) >= self.max_entries
            ) and len(self.cache) > 0:
                await self._evict_lru()

            # Add new entry
            entry = CacheEntry(
                key=key,
                value=value,
                size_bytes=size_bytes,
                created_at=datetime.now(),
                last_accessed=datetime.now(),
                ttl_seconds=ttl_seconds,
            )

            # Remove old entry if exists
            if key in self.cache:
                await self._remove(key)

            self.cache[key] = entry
            self.total_size += size_bytes

            return True

    async def _remove(self, key: str):
        """Remove entry from cache"""
        if key in self.cache:
            entry = self.cache.pop(key)
            self.total_size -= entry.size_bytes

    async def _evict_lru(self):
        """Evict least recently used entry"""
        if self.cache:
            key, entry = self.cache.popitem(last=False)
            self.total_size -= entry.size_bytes
            logger.debug(f"Evicted cache entry: {key}")

    async def clear(self):
        """Clear all cache entries"""
        async with self._lock:
            self.cache.clear()
            self.total_size = 0

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        if not self.cache:
            return {"entries": 0, "size_mb": 0, "hit_rate": 0, "avg_access_count": 0}

        total_accesses = sum(e.access_count for e in self.cache.values())

        return {
            "entries": len(self.cache),
            "size_mb": self.total_size / (1024 * 1024),
            "max_size_mb": self.max_size_bytes / (1024 * 1024),
            "utilization": self.total_size / self.max_size_bytes,
            "total_accesses": total_accesses,
            "avg_access_count": total_accesses / len(self.cache) if self.cache else 0,
        }

class IntelligentCache:
    """
    Intelligent caching system that adapts based on memory availability
    and usage patterns
    """

    def __init__(
        self,
        memory_manager: M1MemoryManager,
        base_cache_size_mb: int = 50,
        max_cache_size_mb: int = 200,
    ):
        self.memory_manager = memory_manager
        self.base_cache_size_mb = base_cache_size_mb
        self.max_cache_size_mb = max_cache_size_mb

        # Different caches for different purposes
        self.nlp_cache = LRUCache(max_size_mb=30)
        self.rag_cache = LRUCache(max_size_mb=50)
        self.response_cache = LRUCache(max_size_mb=20)

        # Usage patterns
        self.cache_hits: defaultdict = defaultdict(int)
        self.cache_misses: defaultdict = defaultdict(int)

        # Adaptive sizing
        self._last_resize = datetime.now()
        self._resize_interval = timedelta(minutes=5)

    def _generate_key(self, category: str, *args) -> str:
        """Generate cache key from category and arguments"""
        content = f"{category}:{':'.join(str(arg) for arg in args)}"
        return hashlib.md5(content.encode()).hexdigest()

    async def get_nlp_analysis(self, text: str) -> Optional[Any]:
        """Get cached NLP analysis"""
        key = self._generate_key("nlp", text)
        result = await self.nlp_cache.get(key)

        if result:
            self.cache_hits["nlp"] += 1
        else:
            self.cache_misses["nlp"] += 1

        return result

    async def set_nlp_analysis(self, text: str, analysis: Any):
        """Cache NLP analysis"""
        key = self._generate_key("nlp", text)
        await self.nlp_cache.set(key, analysis, ttl_seconds=3600)  # 1 hour TTL

    async def get_rag_search(self, query: str, k: int = 5) -> Optional[List[Dict]]:
        """Get cached RAG search results"""
        key = self._generate_key("rag_search", query, k)
        result = await self.rag_cache.get(key)

        if result:
            self.cache_hits["rag"] += 1
        else:
            self.cache_misses["rag"] += 1

        return result

    async def set_rag_search(self, query: str, results: List[Dict], k: int = 5):
        """Cache RAG search results"""
        key = self._generate_key("rag_search", query, k)
        await self.rag_cache.set(key, results, ttl_seconds=1800)  # 30 min TTL

    async def get_response(
        self, user_input: str, context_hash: str = ""
    ) -> Optional[str]:
        """Get cached response"""
        key = self._generate_key("response", user_input, context_hash)
        result = await self.response_cache.get(key)

        if result:
            self.cache_hits["response"] += 1
        else:
            self.cache_misses["response"] += 1

        return result

    async def set_response(
        self, user_input: str, response: str, context_hash: str = ""
    ):
        """Cache response"""
        key = self._generate_key("response", user_input, context_hash)
        await self.response_cache.set(key, response, ttl_seconds=600)  # 10 min TTL

    async def adapt_cache_sizes(self):
        """Adapt cache sizes based on memory availability"""
        # Check if it's time to resize
        if datetime.now() - self._last_resize < self._resize_interval:
            return

        # Get memory status
        snapshot = await self.memory_manager.get_memory_snapshot()

        # Calculate target cache sizes based on available memory
        available_gb = snapshot.available / (1024**3)

        if available_gb > 4:
            # Plenty of memory - use larger caches
            target_multiplier = 1.5
        elif available_gb > 2:
            # Normal memory - use standard caches
            target_multiplier = 1.0
        elif available_gb > 1:
            # Low memory - reduce cache sizes
            target_multiplier = 0.7
        else:
            # Very low memory - minimal caches
            target_multiplier = 0.3

        # Apply multiplier to cache sizes
        nlp_size = int(30 * target_multiplier)
        rag_size = int(50 * target_multiplier)
        response_size = int(20 * target_multiplier)

        # Update cache sizes
        self.nlp_cache.max_size_bytes = nlp_size * 1024 * 1024
        self.rag_cache.max_size_bytes = rag_size * 1024 * 1024
        self.response_cache.max_size_bytes = response_size * 1024 * 1024

        self._last_resize = datetime.now()

        logger.info(
            f"Adapted cache sizes - NLP: {nlp_size}MB, RAG: {rag_size}MB, "
            f"Response: {response_size}MB (multiplier: {target_multiplier})"
        )

    async def clear_old_entries(self):
        """Clear expired entries from all caches"""
        # This happens automatically on access, but we can be proactive
        for cache in [self.nlp_cache, self.rag_cache, self.response_cache]:
            expired_keys = []
            for key, entry in cache.cache.items():
                if entry.is_expired():
                    expired_keys.append(key)

            for key in expired_keys:
                await cache._remove(key)

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive cache statistics"""
        total_hits = sum(self.cache_hits.values())
        total_misses = sum(self.cache_misses.values())
        hit_rate = (
            total_hits / (total_hits + total_misses)
            if (total_hits + total_misses) > 0
            else 0
        )

        return {
            "overall": {
                "hit_rate": hit_rate,
                "total_hits": total_hits,
                "total_misses": total_misses,
            },
            "by_type": {
                "nlp": {
                    "hits": self.cache_hits["nlp"],
                    "misses": self.cache_misses["nlp"],
                    "stats": self.nlp_cache.get_stats(),
                },
                "rag": {
                    "hits": self.cache_hits["rag"],
                    "misses": self.cache_misses["rag"],
                    "stats": self.rag_cache.get_stats(),
                },
                "response": {
                    "hits": self.cache_hits["response"],
                    "misses": self.cache_misses["response"],
                    "stats": self.response_cache.get_stats(),
                },
            },
        }

    async def cleanup(self):
        """Cleanup all caches"""
        await self.nlp_cache.clear()
        await self.rag_cache.clear()
        await self.response_cache.clear()

# Decorator for automatic caching
def cached(cache_type: str, ttl_seconds: int = 3600):
    """
    Decorator to automatically cache function results
    """

    def decorator(func: Callable):
        async def wrapper(self, *args, **kwargs):
            # Check if we have an intelligent cache
            if hasattr(self, "_cache") and isinstance(self._cache, IntelligentCache):
                # Generate cache key
                cache_key = self._cache._generate_key(
                    f"{cache_type}:{func.__name__}", *args, *sorted(kwargs.items())
                )

                # Try to get from cache
                cache = getattr(self._cache, f"{cache_type}_cache", None)
                if cache:
                    result = await cache.get(cache_key)
                    if result is not None:
                        return result

                # Execute function
                result = await func(self, *args, **kwargs)

                # Cache result
                if cache and result is not None:
                    await cache.set(cache_key, result, ttl_seconds=ttl_seconds)

                return result
            else:
                # No cache available, just execute
                return await func(self, *args, **kwargs)

        return wrapper

    return decorator

