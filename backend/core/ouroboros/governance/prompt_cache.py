"""
Prompt Caching Layer
====================

Reuses system prompt prefixes across operations to cut token costs.

The system prompt (base instructions + repo context + skill context) is
typically identical across operations within a short window.  By caching
the assembled prompt text keyed on a SHA-256 hash we avoid redundant
construction and — more importantly — enable providers that support
server-side prompt caching to reuse the same prefix.

Cache entries are evicted by TTL (default 1 hour) and by capacity
(default 50 entries, LRU on eviction).

Environment variables
---------------------
``JARVIS_PROMPT_CACHE_TTL_S``
    Time-to-live for cached entries in seconds (default ``3600``).
``JARVIS_PROMPT_CACHE_MAX_ENTRIES``
    Maximum number of cached entries before eviction (default ``50``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.PromptCache")

# ---------------------------------------------------------------------------
# Environment defaults
# ---------------------------------------------------------------------------

_DEFAULT_TTL_S: float = float(os.environ.get("JARVIS_PROMPT_CACHE_TTL_S", "3600"))
_DEFAULT_MAX_ENTRIES: int = int(os.environ.get("JARVIS_PROMPT_CACHE_MAX_ENTRIES", "50"))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """A single cached prompt fragment."""

    key: str
    content: str
    token_estimate: int  # rough token count (len(content) // 4)
    created_at: float = field(default_factory=time.monotonic)
    last_used_at: float = field(default_factory=time.monotonic)
    hit_count: int = 0
    ttl_s: float = _DEFAULT_TTL_S

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > self.ttl_s


# ---------------------------------------------------------------------------
# PromptCache
# ---------------------------------------------------------------------------


class PromptCache:
    """In-memory LRU prompt cache with TTL eviction.

    Thread-safe via a reentrant lock.  Designed for use from both the
    async governance pipeline and synchronous callers.

    Usage::

        cache = get_prompt_cache()
        key = cache._make_key(system_prompt, context_prefix)
        hit = cache.get(key)
        if hit is None:
            prompt = build_expensive_prompt(...)
            cache.put(key, prompt)
    """

    def __init__(
        self,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        ttl_s: float = _DEFAULT_TTL_S,
    ) -> None:
        self._max_entries = max(1, max_entries)
        self._ttl_s = ttl_s
        self._entries: Dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self._hits: int = 0
        self._misses: int = 0
        logger.info(
            "PromptCache initialised (max_entries=%d, ttl_s=%.0f)",
            self._max_entries,
            self._ttl_s,
        )

    # -- public API ---------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        """Return cached content if present and not expired, else ``None``.

        On hit: bumps ``hit_count`` and ``last_used_at``.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired:
                del self._entries[key]
                self._misses += 1
                logger.debug("cache EXPIRED key=%s", key[:16])
                return None
            entry.hit_count += 1
            entry.last_used_at = time.monotonic()
            self._hits += 1
            logger.debug("cache HIT key=%s (hits=%d)", key[:16], entry.hit_count)
            return entry.content

    def put(self, key: str, content: str) -> None:
        """Store *content* under *key*, evicting the oldest entry if at capacity."""
        with self._lock:
            # If key already exists, update in-place.
            if key in self._entries:
                existing = self._entries[key]
                existing.content = content
                existing.token_estimate = len(content) // 4
                existing.last_used_at = time.monotonic()
                return

            # Evict expired entries first — may free enough space.
            self._evict_expired()

            # If still at capacity, evict the least-recently-used entry.
            while len(self._entries) >= self._max_entries:
                lru_key = min(
                    self._entries,
                    key=lambda k: self._entries[k].last_used_at,
                )
                logger.debug("cache EVICT key=%s (LRU)", lru_key[:16])
                del self._entries[lru_key]

            entry = CacheEntry(
                key=key,
                content=content,
                token_estimate=len(content) // 4,
                ttl_s=self._ttl_s,
            )
            self._entries[key] = entry
            logger.debug(
                "cache PUT key=%s (tokens~%d, entries=%d)",
                key[:16],
                entry.token_estimate,
                len(self._entries),
            )

    def invalidate(self, key: str) -> bool:
        """Remove a specific entry.  Returns ``True`` if it existed."""
        with self._lock:
            if key in self._entries:
                del self._entries[key]
                logger.debug("cache INVALIDATE key=%s", key[:16])
                return True
            return False

    def clear(self) -> int:
        """Remove all entries.  Returns the count removed."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            logger.info("cache CLEAR removed %d entries", count)
            return count

    def stats(self) -> Dict[str, Any]:
        """Return cache statistics."""
        with self._lock:
            total = self._hits + self._misses
            total_tokens_saved = sum(
                e.token_estimate * e.hit_count for e in self._entries.values()
            )
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (self._hits / total) if total > 0 else 0.0,
                "entries": len(self._entries),
                "max_entries": self._max_entries,
                "ttl_s": self._ttl_s,
                "estimated_tokens_saved": total_tokens_saved,
            }

    # -- internal -----------------------------------------------------------

    def _evict_expired(self) -> int:
        """Remove entries that have exceeded their TTL.  Returns count removed."""
        expired_keys = [k for k, v in self._entries.items() if v.is_expired]
        for k in expired_keys:
            del self._entries[k]
        if expired_keys:
            logger.debug("cache evicted %d expired entries", len(expired_keys))
        return len(expired_keys)

    @staticmethod
    def _make_key(system_prompt: str, context_prefix: str) -> str:
        """Compute a deterministic cache key from prompt components.

        Uses SHA-256 so that key length is fixed regardless of prompt size.
        """
        raw = system_prompt + "\x00" + context_prefix
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# CachedPromptBuilder
# ---------------------------------------------------------------------------


class CachedPromptBuilder:
    """Wraps generation prompt construction with a caching layer.

    The *system prompt* portion (base instructions + repo context + skill
    context) is the cacheable prefix.  The *operation context* and *file
    context* are per-request and never cached.

    This ordering maximises server-side prompt caching for providers that
    support it (Anthropic, OpenAI).
    """

    def __init__(self, cache: PromptCache) -> None:
        self._cache = cache

    def build_system_prompt(
        self,
        base_instructions: str,
        repo_context: str,
        skill_context: str,
    ) -> Tuple[str, bool]:
        """Build or retrieve the cached system prompt.

        Returns ``(prompt_text, was_cached)``.
        """
        cache_key = self._cache._make_key(
            base_instructions,
            repo_context + "\x00" + skill_context,
        )

        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("system prompt cache HIT (key=%s)", cache_key[:16])
            return cached, True

        # Assemble the full system prompt — cache-friendly ordering.
        parts = [base_instructions]
        if repo_context:
            parts.append("\n\n## Repository Context\n" + repo_context)
        if skill_context:
            parts.append("\n\n## Skill Context\n" + skill_context)
        prompt = "\n".join(parts)

        self._cache.put(cache_key, prompt)
        logger.debug("system prompt cache MISS — built & cached (key=%s)", cache_key[:16])
        return prompt, False

    @staticmethod
    def build_generation_prompt(
        system: str,
        operation_context: str,
        file_context: str,
    ) -> str:
        """Assemble the complete generation prompt.

        The *system* portion is the cacheable prefix.  *operation_context*
        and *file_context* are per-request and appended after.
        """
        sections = [system]
        if operation_context:
            sections.append("\n\n## Operation Context\n" + operation_context)
        if file_context:
            sections.append("\n\n## File Context\n" + file_context)
        return "\n".join(sections)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_singleton: Optional[PromptCache] = None
_singleton_lock = threading.Lock()


def get_prompt_cache() -> PromptCache:
    """Return the process-wide ``PromptCache`` singleton."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PromptCache(
                max_entries=_DEFAULT_MAX_ENTRIES,
                ttl_s=_DEFAULT_TTL_S,
            )
        return _singleton
