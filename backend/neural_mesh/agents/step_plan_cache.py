"""
JARVIS Neural Mesh — StepPlanCache
===================================

ChromaDB-backed semantic cache for goal-decomposition plans.

When the NativeAppControlAgent decomposes a high-level goal (e.g. "Send Zach
a message on WhatsApp") into atomic UI steps, the resulting plan is stored
here.  On subsequent requests the cache is queried first: if a semantically
similar goal has been seen before, the stored plan is returned without any LLM
call, saving latency and cost.

Design decisions
----------------
* **Cosine similarity** — `hnsw:space=cosine` means distance 0.0 is identical,
  1.0 is orthogonal.  Similarity = max(0, 1 - distance).
* **Lazy collection init with probe** — On first use the embedding function is
  tested with a no-op probe document.  If the embedding model is not available
  (e.g. ONNX download blocked in restricted environments) the collection is
  torn down and `_init_failed` is set so all subsequent operations are no-ops.
  This means the cache degrades to "always miss / never store" rather than
  crashing callers.
* **Explicit embeddings path** — When an embedding function object is available
  we call it directly to obtain vectors, then pass those vectors explicitly to
  `upsert` and `query`.  This avoids ChromaDB calling the embedding function
  internally on every operation (where errors are harder to catch cleanly).
* **Trinity event bus** — A best-effort `plan.cached` event is published on
  every successful store so the Reactor can learn across sessions.
* **Singleton** — `get_step_plan_cache()` returns the process-wide instance.

Configuration (env vars — no hardcoding)
-----------------------------------------
JARVIS_PLAN_CACHE_SIMILARITY    — minimum cosine similarity for a hit  (0.85)
JARVIS_PLAN_CACHE_COLLECTION    — ChromaDB collection name          ("step_plan_cache")
JARVIS_PLAN_CACHE_N_RESULTS     — candidates fetched per query        (3)
JARVIS_PLAN_CACHE_EMBEDDING_FN  — "sentence_transformer" | "default" ("sentence_transformer")
JARVIS_PLAN_CACHE_ST_MODEL      — sentence-transformers model name   ("all-MiniLM-L6-v2")
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env-var helpers
# ---------------------------------------------------------------------------

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Document key helpers
# ---------------------------------------------------------------------------

def _doc_key(app_name: str, goal: str) -> str:
    """Canonical document text that is both embedded and stored."""
    return f"{app_name}: {goal}"


def _doc_id(app_name: str, goal: str) -> str:
    """Stable deterministic ID — upsert is idempotent for the same goal."""
    return hashlib.md5(_doc_key(app_name, goal).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Sentinel for "not yet resolved"
# ---------------------------------------------------------------------------

class _Sentinel:
    """Placeholder distinct from None, meaning 'not yet resolved'."""
    def __repr__(self) -> str:
        return "<_SENTINEL>"


_SENTINEL = _Sentinel()


# ---------------------------------------------------------------------------
# Embedding function resolution
# ---------------------------------------------------------------------------

def _resolve_embedding_function(preference: str) -> Optional[Any]:
    """
    Return a callable embedding function object, or None.

    Priority:
      1. SentenceTransformerEmbeddingFunction when `sentence-transformers` is installed
         and `preference` is "sentence_transformer" (default).
      2. None — caller will pass explicit embeddings using the ChromaDB default.

    Returning None means ChromaDB's default ONNX EF will be used internally,
    which is validated by a probe in `_init_collection`.
    """
    if preference in ("sentence_transformer", "sentence_transformers"):
        try:
            from chromadb.utils.embedding_functions import (  # noqa: PLC0415
                SentenceTransformerEmbeddingFunction,
            )
            model_name = os.getenv(
                "JARVIS_PLAN_CACHE_ST_MODEL", "all-MiniLM-L6-v2"
            )
            ef = SentenceTransformerEmbeddingFunction(model_name=model_name)
            # Probe: actually call the EF to verify the model is loadable
            _test_embed = ef(["probe"])
            logger.debug(
                "[StepPlanCache] Embedding function: SentenceTransformer (model=%s)",
                model_name,
            )
            return ef
        except Exception as exc:
            logger.debug(
                "[StepPlanCache] SentenceTransformer unavailable (%s); "
                "falling back to ChromaDB default ONNX.",
                exc,
            )

    # preference == "default" or sentence-transformers not available
    logger.debug("[StepPlanCache] Embedding function: ChromaDB default (ONNX).")
    return None


# ---------------------------------------------------------------------------
# StepPlanCache
# ---------------------------------------------------------------------------

class StepPlanCache:
    """
    Semantic cache for NativeAppControlAgent goal-decomposition plans.

    Thread/coroutine safety: all ChromaDB calls are synchronous but fast (the
    in-memory EphemeralClient has no I/O).  Public methods are async so callers
    can await them naturally inside the async agent loop.
    """

    def __init__(self) -> None:
        # ChromaDB state — resolved lazily on first access
        self._collection: Optional[Any] = None
        self._ef: Any = _SENTINEL          # embedding function: None | callable | _SENTINEL
        self._init_failed: bool = False    # sticky — skip init retries after failure

        # Configuration
        self._similarity_threshold: float = _env_float(
            "JARVIS_PLAN_CACHE_SIMILARITY", 0.85
        )
        self._collection_name: str = os.getenv(
            "JARVIS_PLAN_CACHE_COLLECTION", "step_plan_cache"
        )
        self._n_results: int = _env_int("JARVIS_PLAN_CACHE_N_RESULTS", 3)
        self._embedding_preference: str = os.getenv(
            "JARVIS_PLAN_CACHE_EMBEDDING_FN", "sentence_transformer"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_cached_plan(
        self, goal: str, app_name: str
    ) -> Optional[List[str]]:
        """
        Semantic lookup for a previously stored decomposition plan.

        Returns:
            Ordered list of atomic step strings on a cache hit, None on miss.
        """
        collection, ef = self._ensure_ready()
        if collection is None:
            return None

        query_text = _doc_key(app_name, goal)
        try:
            query_kwargs: Dict[str, Any] = {
                "n_results": self._n_results,
                "include": ["distances", "metadatas"],
            }
            if ef is not None:
                # Explicit embeddings — bypass ChromaDB's internal EF call
                query_kwargs["query_embeddings"] = ef([query_text])
            else:
                query_kwargs["query_texts"] = [query_text]

            results = collection.query(**query_kwargs)
        except Exception as exc:
            logger.debug("[StepPlanCache] query failed: %s", exc)
            return None

        distances: List[List[float]] = results.get("distances") or []
        metadatas: List[List[Dict[str, Any]]] = results.get("metadatas") or []

        if not distances or not distances[0]:
            return None

        # Candidates are returned nearest-first; pick the best match above threshold
        for dist, meta in zip(distances[0], metadatas[0]):
            # Cosine space: 0 = identical, values in [0, 2] theoretically; typical [0, 1]
            similarity = max(0.0, 1.0 - dist)
            if similarity >= self._similarity_threshold:
                steps_json: str = meta.get("steps_json", "[]")
                try:
                    steps: List[str] = json.loads(steps_json)
                    if steps:
                        logger.debug(
                            "[StepPlanCache] HIT  goal=%r  sim=%.3f  steps=%d",
                            goal[:60],
                            similarity,
                            len(steps),
                        )
                        return steps
                except Exception as parse_exc:
                    logger.debug(
                        "[StepPlanCache] steps_json parse error: %s", parse_exc
                    )
                    continue

        best_sim = max(0.0, 1.0 - distances[0][0]) if distances[0] else 0.0
        logger.debug(
            "[StepPlanCache] MISS  goal=%r  best_sim=%.3f  threshold=%.2f",
            goal[:60],
            best_sim,
            self._similarity_threshold,
        )
        return None

    async def store_plan(
        self, goal: str, app_name: str, steps: List[str]
    ) -> None:
        """
        Persist a successful decomposition plan for future semantic retrieval.

        Upserts by deterministic ID — re-storing the same goal is idempotent.
        Also emits a best-effort `plan.cached` event to the Trinity event bus.
        """
        if not steps:
            return

        collection, ef = self._ensure_ready()
        if collection is None:
            return

        doc_id = _doc_id(app_name, goal)
        doc_text = _doc_key(app_name, goal)
        metadata: Dict[str, Any] = {
            "app_name": app_name,
            "goal": goal,
            "steps_json": json.dumps(steps),
            "steps_count": len(steps),
            "cached_at": time.time(),
        }

        try:
            upsert_kwargs: Dict[str, Any] = {
                "ids": [doc_id],
                "documents": [doc_text],
                "metadatas": [metadata],
            }
            if ef is not None:
                # Explicit embeddings — avoids internal EF call inside ChromaDB
                upsert_kwargs["embeddings"] = ef([doc_text])

            collection.upsert(**upsert_kwargs)
            logger.debug(
                "[StepPlanCache] stored  goal=%r  steps=%d  id=%s",
                goal[:60],
                len(steps),
                doc_id,
            )
        except Exception as exc:
            logger.debug("[StepPlanCache] upsert failed: %s", exc)
            return

        # Best-effort Trinity event bus notification
        await self._emit_plan_cached_event(goal, app_name, steps)

    async def invalidate(self, goal: str, app_name: str) -> bool:
        """
        Remove a specific plan from the cache.

        Returns True if the deletion call succeeded (the entry may or may not
        have existed), False on error.
        """
        collection, _ = self._ensure_ready()
        if collection is None:
            return False

        doc_id = _doc_id(app_name, goal)
        try:
            collection.delete(ids=[doc_id])
            logger.debug(
                "[StepPlanCache] invalidated  goal=%r  id=%s", goal[:60], doc_id
            )
            return True
        except Exception as exc:
            logger.debug("[StepPlanCache] delete failed: %s", exc)
            return False

    def collection_size(self) -> int:
        """Return the number of plans currently stored. Returns 0 on any error."""
        collection, _ = self._ensure_ready()
        if collection is None:
            return 0
        try:
            return collection.count()
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> tuple:
        """
        Lazy-initialise ChromaDB collection and embedding function.

        Returns:
            (collection, ef) where collection is None when init has failed.
        """
        if self._init_failed:
            return None, None
        if self._collection is not None:
            return self._collection, self._ef

        return self._init_collection()

    def _init_collection(self) -> tuple:
        """
        One-shot initialisation.  Sets `_init_failed=True` on permanent failure.

        Strategy:
          1. Resolve embedding function (probe to verify it actually works).
          2. Create EphemeralClient + collection without specifying an EF
             (we pass embeddings explicitly on every operation to avoid ChromaDB
             calling the EF internally where failures are harder to catch).
          3. Probe-upsert a sentinel document; if this fails the collection is
             unusable and we disable the cache.
          4. Probe-delete the sentinel so the collection starts empty.

        Returns:
            (collection, ef) tuple, or (None, None) on failure.
        """
        try:
            import chromadb  # noqa: PLC0415

            # Resolve EF once (probe included inside _resolve_embedding_function)
            if isinstance(self._ef, _Sentinel):
                self._ef = _resolve_embedding_function(self._embedding_preference)

            client = chromadb.EphemeralClient()

            # Create collection WITHOUT an embedding_function arg — we control
            # embedding explicitly, so ChromaDB never calls an EF internally.
            collection = client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            # Probe: verify that upsert + query actually works end-to-end
            probe_id = "__probe__"
            probe_text = "jarvis step plan cache probe"
            probe_upsert_kwargs: Dict[str, Any] = {
                "ids": [probe_id],
                "documents": [probe_text],
                "metadatas": [{"probe": True}],
            }
            if self._ef is not None:
                probe_upsert_kwargs["embeddings"] = self._ef([probe_text])

            collection.upsert(**probe_upsert_kwargs)
            collection.delete(ids=[probe_id])

            self._collection = collection
            logger.info(
                "[StepPlanCache] ChromaDB collection '%s' ready "
                "(similarity_threshold=%.2f, ef=%s)",
                self._collection_name,
                self._similarity_threshold,
                type(self._ef).__name__ if self._ef is not None else "chromadb_internal",
            )
            return self._collection, self._ef

        except Exception as exc:
            logger.warning(
                "[StepPlanCache] Initialisation failed: %s — "
                "plan caching disabled for this session.",
                exc,
            )
            self._init_failed = True
            return None, None

    async def _emit_plan_cached_event(
        self, goal: str, app_name: str, steps: List[str]
    ) -> None:
        """Publish a plan.cached event to the Trinity bus (best-effort, never raises)."""
        try:
            from backend.core.trinity_event_bus import (  # noqa: PLC0415
                get_event_bus_if_exists,
            )

            bus = get_event_bus_if_exists()
            if bus is None:
                return

            await bus.publish_raw(
                topic="plan.cached",
                data={
                    "goal": goal,
                    "app_name": app_name,
                    "steps": steps,
                    "steps_count": len(steps),
                    "timestamp": time.time(),
                },
            )
        except Exception as exc:
            logger.debug("[StepPlanCache] Trinity event publish failed: %s", exc)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_cache_instance: Optional[StepPlanCache] = None


def get_step_plan_cache() -> StepPlanCache:
    """Return (creating if needed) the process-wide StepPlanCache singleton."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = StepPlanCache()
    return _cache_instance
