"""Slice 131 Phase 3 — The Asynchronous Semantic Response Cache.

Closes the near-match gap the exact-match ``provider_response_cache`` (Phase 1)
cannot: two prompts expressing the *same intent* in different words hash to
different keys and miss. This layer vectorizes the prompt and serves a cached
response when a recent entry crosses a strict cosine threshold.

**Pure composition — nothing reinvented:**
  * Vectorization: ``semantic_index._embedder_factory`` (the dormant fastembed
    embedder + stdlib-hashing fallback) + ``semantic_index._cosine``. The
    concrete embedding model lives in SemanticIndex; this module embeds NO model
    name (CLAUDE.md no-hardcode mandate — pinned by test).
  * Storage payload + fail-closed git guard: ``provider_response_cache``'s
    ``CachedTrajectory`` (the serializable response) + ``repo_state_digest``
    (any git diff → a different digest → never served).

**Invariants:**
  * Gated ``JARVIS_SEMANTIC_CACHE_ENABLED`` default-FALSE → OFF byte-identical
    (no embedder import, no work).
  * Fully ASYNC + FAIL-CLOSED: the (sync, CPU-bound) embedder runs in an
    executor under ``asyncio.wait_for``; any hang/error/timeout → drop the cache
    attempt → return ``None`` → caller takes the standard generation path.
  * Repo-state fail-closed EVEN on a semantic match: each entry stores the
    ``repo_state_digest`` at write time; a near-match whose digest has drifted is
    skipped (no stale code served).
  * Write-through: a completed novel generation is embedded + pushed immediately
    so the very next cycle can hit it.
  * Bounded LRU (``deque(maxlen=...)``).
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import threading
from collections import deque
from typing import Any, Deque, List, Optional, Sequence

_ENV_MASTER = "JARVIS_SEMANTIC_CACHE_ENABLED"
_ENV_THRESHOLD = "JARVIS_SEMANTIC_CACHE_THRESHOLD"
_ENV_MAX = "JARVIS_SEMANTIC_CACHE_MAX_ENTRIES"
_ENV_TIMEOUT = "JARVIS_SEMANTIC_CACHE_EMBED_TIMEOUT_S"

_DEFAULT_THRESHOLD = 0.95
_DEFAULT_MAX = 256
_DEFAULT_TIMEOUT = 2.0

# An embedder exposes ``embed(texts) -> Optional[List[List[float]]]`` (the
# SemanticIndex contract). Injectable for tests / the hot path.
EmbedderLike = Any


def semantic_cache_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. Re-read each call → hot-revert.
    NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def semantic_threshold() -> float:
    """Strict cosine threshold for a near-match (default 0.95). Clamped [0,1]."""
    try:
        v = float(os.getenv(_ENV_THRESHOLD, _DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        v = _DEFAULT_THRESHOLD
    return max(0.0, min(1.0, v))


def _max_entries() -> int:
    try:
        return max(1, int(os.getenv(_ENV_MAX, _DEFAULT_MAX)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX


def _embed_timeout_s() -> float:
    try:
        return max(0.01, float(os.getenv(_ENV_TIMEOUT, _DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT


def _safe_repo_digest(repo_root: Any) -> str:
    """Compose Phase-1 ``repo_state_digest`` (fail-closed git guard). NEVER raises."""
    if repo_root is None:
        return ""
    try:
        from backend.core.ouroboros.governance.provider_response_cache import (
            repo_state_digest,
        )
        return repo_state_digest(repo_root)
    except Exception:  # noqa: BLE001
        return ""


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Compose SemanticIndex's cosine; fall back to a local computation if the
    import is unavailable. NEVER raises (returns -1.0 on failure → never a hit)."""
    try:
        from backend.core.ouroboros.governance.semantic_index import _cosine as _si_cos
        return float(_si_cos(a, b))
    except Exception:  # noqa: BLE001
        try:
            num = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            if na == 0.0 or nb == 0.0:
                return -1.0
            return num / (na * nb)
        except Exception:  # noqa: BLE001
            return -1.0


@dataclasses.dataclass
class _Entry:
    vector: List[float]
    trajectory: Any           # CachedTrajectory
    repo_digest: str


class SemanticResponseCache:
    """Bounded, async, fail-closed near-match cache over CachedTrajectory."""

    def __init__(
        self,
        *,
        embedder: Optional[EmbedderLike] = None,
        max_entries: Optional[int] = None,
        threshold: Optional[float] = None,
    ) -> None:
        self._embedder = embedder  # None → lazy factory on first use
        self._threshold = threshold
        self._entries: Deque[_Entry] = deque(maxlen=max_entries or _max_entries())
        self._lock = threading.Lock()

    # ── embedder (lazy, composed from SemanticIndex) ────────────────────────
    def _get_embedder(self) -> Optional[EmbedderLike]:
        if self._embedder is None:
            try:
                from backend.core.ouroboros.governance.semantic_index import (
                    _embedder_factory,
                )
                self._embedder = _embedder_factory()
            except Exception:  # noqa: BLE001
                self._embedder = None
        return self._embedder

    async def _embed_one(self, text: str) -> Optional[List[float]]:
        """Vectorize ``text`` in an executor under a hard timeout. FAIL-CLOSED:
        any error/timeout → None (caller falls back to generation)."""
        try:
            emb = self._get_embedder()
            if emb is None:
                return None
            loop = asyncio.get_event_loop()
            vecs = await asyncio.wait_for(
                loop.run_in_executor(None, emb.embed, [text or ""]),
                timeout=_embed_timeout_s(),
            )
            if not vecs or not vecs[0]:
                return None
            return [float(x) for x in vecs[0]]
        except Exception:  # noqa: BLE001 — incl. asyncio.TimeoutError
            return None

    # ── lookup / store ──────────────────────────────────────────────────────
    async def lookup(
        self, prompt: str, repo_root: Any, *, repo_digest: Optional[str] = None,
    ) -> Optional[Any]:
        """Return a cached ``CachedTrajectory`` for a near-match, or None. Gated +
        fail-closed + repo-state-validated. NEVER raises."""
        if not semantic_cache_enabled():
            return None
        try:
            vec = await self._embed_one(prompt)
            if vec is None:
                return None
            cur_digest = repo_digest if repo_digest is not None else _safe_repo_digest(repo_root)
            thr = self._threshold if self._threshold is not None else semantic_threshold()
            with self._lock:
                snapshot = list(self._entries)
            best: Optional[_Entry] = None
            best_sim = -1.0
            for e in snapshot:
                if e.repo_digest != cur_digest:
                    continue  # fail-closed: repo drifted → never serve stale
                sim = _cosine(vec, e.vector)
                if sim > best_sim:
                    best_sim, best = sim, e
            if best is not None and best_sim >= thr:
                return best.trajectory
            return None
        except Exception:  # noqa: BLE001
            return None

    async def store(
        self, prompt: str, trajectory: Any, repo_root: Any,
        *, repo_digest: Optional[str] = None,
    ) -> bool:
        """Write-through: embed ``prompt`` and push ``(vector, trajectory,
        repo_digest)``. Gated + fail-soft (errors → False, never raise)."""
        if not semantic_cache_enabled():
            return False
        try:
            vec = await self._embed_one(prompt)
            if vec is None:
                return False
            cur_digest = repo_digest if repo_digest is not None else _safe_repo_digest(repo_root)
            with self._lock:
                self._entries.append(_Entry(vector=vec, trajectory=trajectory, repo_digest=cur_digest))
            return True
        except Exception:  # noqa: BLE001
            return False

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ── singleton + module-level convenience API (the integration seam) ─────────
_singleton: Optional[SemanticResponseCache] = None
_singleton_lock = threading.Lock()


def get_semantic_cache() -> SemanticResponseCache:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SemanticResponseCache()
    return _singleton


def reset_semantic_cache() -> None:
    """Tests / clean boot."""
    global _singleton
    with _singleton_lock:
        _singleton = None


async def semantic_cache_lookup(prompt: str, repo_root: Any) -> Optional[Any]:
    """Process-wide near-match lookup (the seam the provider calls on exact-miss).
    Gated + fail-closed. NEVER raises."""
    try:
        return await get_semantic_cache().lookup(prompt, repo_root)
    except Exception:  # noqa: BLE001
        return None


async def semantic_cache_store(prompt: str, trajectory: Any, repo_root: Any) -> bool:
    """Process-wide write-through (the provider calls this after a novel
    generation completes). Gated + fail-soft. NEVER raises."""
    try:
        return await get_semantic_cache().store(prompt, trajectory, repo_root)
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "semantic_cache_enabled",
    "semantic_threshold",
    "SemanticResponseCache",
    "get_semantic_cache",
    "reset_semantic_cache",
    "semantic_cache_lookup",
    "semantic_cache_store",
]
