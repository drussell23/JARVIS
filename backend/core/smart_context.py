"""
SmartContextSelector - Surgical Context Retrieval Engine v1.0
==============================================================

"God Mode" Context Intelligence - Beat the 200k token limit by being SMART.

This module implements GraphRAG-powered surgical context extraction that:
1. Queries the Oracle Graph for relevant code entities
2. Uses AST to extract ONLY the relevant functions/classes (not full files)
3. Resolves dependencies (if Function A calls B, include B)
4. Enforces strict token budgets with intelligent prioritization

Why This Beats Raw Context:
- Claude Code: Reads 50 files = 200k tokens = $$$$ + slow + confused
- SmartContext: Extracts 15 functions + deps = 4k tokens = fast + focused + cheap

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │                     SmartContextSelector                            │
    ├─────────────────────────────────────────────────────────────────────┤
    │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐  │
    │  │  RelevanceScorer│  │   ASTChunker    │  │  DependencyResolver │  │
    │  │  ├── Semantic   │  │  ├── Functions  │  │  ├── Call Graph     │  │
    │  │  ├── Structural │  │  ├── Classes    │  │  ├── Import Chain   │  │
    │  │  └── Recency    │  │  └── Methods    │  │  └── Type Deps      │  │
    │  └────────┬────────┘  └────────┬────────┘  └──────────┬──────────┘  │
    │           │                    │                       │            │
    │           ▼                    ▼                       ▼            │
    │  ┌─────────────────────────────────────────────────────────────────┐│
    │  │                    TokenBudgetManager                           ││
    │  │   ├── Priority Queue (relevance score)                          ││
    │  │   ├── Greedy Packing with dependency constraints                ││
    │  │   └── Dynamic truncation for oversized entities                 ││
    │  └─────────────────────────────────────────────────────────────────┘│
    │                              │                                      │
    │                              ▼                                      │
    │  ┌─────────────────────────────────────────────────────────────────┐│
    │  │                    ContextPackage                               ││
    │  │   ├── Selected code chunks with metadata                        ││
    │  │   ├── Dependency graph subset                                   ││
    │  │   ├── Token usage breakdown                                     ││
    │  │   └── Relevance explanations                                    ││
    │  └─────────────────────────────────────────────────────────────────┘│
    └─────────────────────────────────────────────────────────────────────┘

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import heapq
import logging
import os
import re
import sys
import time
import tokenize
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    FrozenSet,
    Generator,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

# Lazy imports for optional dependencies
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    tiktoken = None

try:
    from sentence_transformers import SentenceTransformer
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    SentenceTransformer = None

# Phase 11 P11.1 — surgical AST chunking lifted to a shared module so
# both ``SmartContextSelector`` (this file) AND the upcoming
# ``read_file(target_symbol=...)`` slicing backend (P11.2) consume one
# canonical implementation. Re-exports preserve every existing
# `from backend.core.smart_context import ASTChunker` import path.
from backend.core.ouroboros.governance.ast_slicer import (  # noqa: F401
    ASTChunker,
    ChunkPriority,
    ChunkType,
    CodeChunk,
    RelevanceReason,
    TokenCounterProtocol,
)

logger = logging.getLogger("SmartContext")


# =============================================================================
# CONFIGURATION
# =============================================================================

class SmartContextConfig:
    """Dynamic configuration for smart context selection."""

    # Token budgets
    DEFAULT_MAX_TOKENS = int(os.getenv("SMART_CONTEXT_MAX_TOKENS", "4000"))
    HARD_LIMIT_TOKENS = int(os.getenv("SMART_CONTEXT_HARD_LIMIT", "8000"))
    RESERVE_TOKENS = int(os.getenv("SMART_CONTEXT_RESERVE", "500"))  # For system prompt

    # Relevance scoring weights
    WEIGHT_SEMANTIC = float(os.getenv("SMART_CONTEXT_WEIGHT_SEMANTIC", "0.4"))
    WEIGHT_STRUCTURAL = float(os.getenv("SMART_CONTEXT_WEIGHT_STRUCTURAL", "0.35"))
    WEIGHT_RECENCY = float(os.getenv("SMART_CONTEXT_WEIGHT_RECENCY", "0.15"))
    WEIGHT_COMPLEXITY = float(os.getenv("SMART_CONTEXT_WEIGHT_COMPLEXITY", "0.10"))

    # Dependency resolution
    MAX_DEPENDENCY_DEPTH = int(os.getenv("SMART_CONTEXT_DEP_DEPTH", "3"))
    INCLUDE_CALLERS = os.getenv("SMART_CONTEXT_INCLUDE_CALLERS", "false").lower() == "true"
    INCLUDE_CALLEES = os.getenv("SMART_CONTEXT_INCLUDE_CALLEES", "true").lower() == "true"

    # Chunking settings
    MIN_CHUNK_LINES = int(os.getenv("SMART_CONTEXT_MIN_LINES", "5"))
    MAX_CHUNK_LINES = int(os.getenv("SMART_CONTEXT_MAX_LINES", "200"))
    INCLUDE_DOCSTRINGS = os.getenv("SMART_CONTEXT_INCLUDE_DOCS", "true").lower() == "true"
    INCLUDE_DECORATORS = os.getenv("SMART_CONTEXT_INCLUDE_DECORATORS", "true").lower() == "true"

    # Caching
    CACHE_TTL_SECONDS = int(os.getenv("SMART_CONTEXT_CACHE_TTL", "300"))
    ENABLE_EMBEDDING_CACHE = os.getenv("SMART_CONTEXT_EMBED_CACHE", "true").lower() == "true"

    # Paths
    EMBEDDING_MODEL = os.getenv("SMART_CONTEXT_EMBED_MODEL", "all-MiniLM-L6-v2")


# =============================================================================
# DATA STRUCTURES
# =============================================================================

# ChunkType / RelevanceReason / CodeChunk moved to
# backend.core.ouroboros.governance.ast_slicer (Phase 11 P11.1) and
# re-exported above. ContextPackage stays here — it's a relevance-
# scoring concept, not a chunking primitive.


@dataclass
class ContextPackage:
    """The final packaged context ready for LLM consumption."""

    # Core content
    chunks: List[CodeChunk]
    formatted_context: str                  # Ready-to-use string for LLM

    # Metadata
    total_tokens: int
    max_tokens: int
    file_count: int
    chunk_count: int

    # Breakdown
    token_breakdown: Dict[str, int]         # Per-file token usage
    relevance_breakdown: Dict[str, float]   # Per-chunk relevance

    # Dependencies
    dependency_graph: Dict[str, List[str]]  # chunk_id -> [dependency_ids]
    missing_dependencies: List[str]         # Dependencies that couldn't fit

    # Query info
    original_query: str
    selection_time_ms: float

    def to_prompt_section(self) -> str:
        """Format for inclusion in LLM prompt."""
        return f"""## Relevant Code Context ({self.chunk_count} chunks, {self.total_tokens} tokens)

{self.formatted_context}

---
*Context selected from {self.file_count} files based on relevance to: "{self.original_query[:100]}"*
"""


# ChunkPriority moved to ast_slicer.py (Phase 11 P11.1) and re-exported
# above.


# =============================================================================
# TOKEN COUNTER
# =============================================================================

class TokenCounter:
    """
    High-performance token counter with caching.

    Uses tiktoken for accurate counting, falls back to heuristic.
    """

    _instance: Optional["TokenCounter"] = None
    _lock = asyncio.Lock()

    def __init__(self):
        self._encoder = None
        self._cache: Dict[str, int] = {}
        self._cache_hits = 0
        self._cache_misses = 0

    @classmethod
    async def get_instance(cls) -> "TokenCounter":
        """Get singleton instance."""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
                await cls._instance._initialize()
            return cls._instance

    async def _initialize(self) -> None:
        """Initialize the encoder."""
        if TIKTOKEN_AVAILABLE:
            try:
                # Use cl100k_base (GPT-4/Claude compatible)
                loop = asyncio.get_running_loop()
                self._encoder = await loop.run_in_executor(
                    None, tiktoken.get_encoding, "cl100k_base"
                )
                logger.info("[TokenCounter] Using tiktoken cl100k_base encoder")
            except Exception as e:
                logger.warning(f"[TokenCounter] tiktoken init failed: {e}, using heuristic")
        else:
            logger.info("[TokenCounter] tiktoken not available, using heuristic")

    def count(self, text: str) -> int:
        """Count tokens in text."""
        if not text:
            return 0

        # Check cache
        cache_key = hashlib.md5(text.encode()).hexdigest()[:16]
        if cache_key in self._cache:
            self._cache_hits += 1
            return self._cache[cache_key]

        self._cache_misses += 1

        # Count
        if self._encoder:
            count = len(self._encoder.encode(text))
        else:
            # Heuristic: ~4 chars per token for code
            count = len(text) // 4 + text.count('\n')

        # Cache (limit size)
        if len(self._cache) < 10000:
            self._cache[cache_key] = count

        return count

    def count_many(self, texts: List[str]) -> List[int]:
        """Count tokens for multiple texts efficiently."""
        return [self.count(t) for t in texts]

    @property
    def cache_stats(self) -> Dict[str, int]:
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "size": len(self._cache),
        }


# ASTChunker moved to backend.core.ouroboros.governance.ast_slicer
# (Phase 11 P11.1). Re-exported at top of this module for backward
# compatibility.



# =============================================================================
# RELEVANCE SCORER
# =============================================================================

class RelevanceScorer:
    """
    Scores code chunks for relevance to a query.

    Combines multiple signals:
    - Semantic similarity (embedding-based)
    - Structural relevance (graph distance)
    - Recency (recently modified = more relevant)
    - Complexity (simpler = often more core)
    """

    def __init__(self):
        self._embedder = None
        self._embed_cache: Dict[str, List[float]] = {}

    async def initialize(self) -> None:
        """Initialize embedder if available."""
        if EMBEDDINGS_AVAILABLE and SmartContextConfig.ENABLE_EMBEDDING_CACHE:
            try:
                loop = asyncio.get_running_loop()
                self._embedder = await loop.run_in_executor(
                    None,
                    SentenceTransformer,
                    SmartContextConfig.EMBEDDING_MODEL,
                )
                logger.info(f"[RelevanceScorer] Loaded embedding model: {SmartContextConfig.EMBEDDING_MODEL}")
            except Exception as e:
                logger.warning(f"[RelevanceScorer] Embedding init failed: {e}")

    async def score_chunks(
        self,
        query: str,
        chunks: List[CodeChunk],
        graph_distances: Optional[Dict[str, int]] = None,
    ) -> List[CodeChunk]:
        """
        Score and sort chunks by relevance.

        Args:
            query: The search query/goal
            chunks: Chunks to score
            graph_distances: Optional {chunk_id: distance_from_query_targets}

        Returns:
            Chunks sorted by relevance (highest first)
        """
        if not chunks:
            return []

        # Compute semantic scores
        semantic_scores = await self._compute_semantic_scores(query, chunks)

        # Score each chunk
        for i, chunk in enumerate(chunks):
            # Semantic (0-1)
            semantic = semantic_scores.get(chunk.chunk_id, 0.0)

            # Structural (based on graph distance, 0-1)
            if graph_distances and chunk.chunk_id in graph_distances:
                dist = graph_distances[chunk.chunk_id]
                structural = 1.0 / (1.0 + dist * 0.3)  # Decay with distance
            else:
                structural = 0.5  # Neutral if no graph info

            # Recency (placeholder - would need file mtime)
            recency = 0.5

            # Complexity (lower = more likely to be core logic)
            if chunk.complexity > 0:
                complexity_score = 1.0 / (1.0 + chunk.complexity * 0.1)
            else:
                complexity_score = 0.5

            # Weighted combination
            chunk.relevance_score = (
                SmartContextConfig.WEIGHT_SEMANTIC * semantic +
                SmartContextConfig.WEIGHT_STRUCTURAL * structural +
                SmartContextConfig.WEIGHT_RECENCY * recency +
                SmartContextConfig.WEIGHT_COMPLEXITY * complexity_score
            )

            # Add reasons
            if semantic > 0.5:
                chunk.relevance_reasons.append(RelevanceReason.SEMANTIC)
            if structural > 0.7:
                chunk.relevance_reasons.append(RelevanceReason.STRUCTURAL)

        # Sort by score
        return sorted(chunks, key=lambda c: c.relevance_score, reverse=True)

    async def _compute_semantic_scores(
        self,
        query: str,
        chunks: List[CodeChunk],
    ) -> Dict[str, float]:
        """Compute semantic similarity scores."""
        scores = {}

        if self._embedder is None:
            # Fallback: keyword matching
            query_words = set(query.lower().split())
            for chunk in chunks:
                text = f"{chunk.name} {chunk.docstring or ''} {chunk.source_code[:200]}"
                text_words = set(text.lower().split())
                overlap = len(query_words & text_words)
                scores[chunk.chunk_id] = min(1.0, overlap / max(1, len(query_words)))
            return scores

        # Embedding-based similarity
        try:
            # Get query embedding
            query_embed = self._get_embedding(query)

            # Get chunk embeddings
            for chunk in chunks:
                # Embed name + signature + docstring (not full source)
                text = f"{chunk.name}"
                if chunk.signature:
                    text += f" {chunk.signature}"
                if chunk.docstring:
                    text += f" {chunk.docstring[:200]}"

                chunk_embed = self._get_embedding(text)

                # Cosine similarity
                similarity = self._cosine_similarity(query_embed, chunk_embed)
                scores[chunk.chunk_id] = max(0.0, similarity)

        except Exception as e:
            logger.warning(f"[RelevanceScorer] Embedding error: {e}")

        return scores

    def _get_embedding(self, text: str) -> List[float]:
        """Get embedding with caching."""
        cache_key = hashlib.md5(text.encode()).hexdigest()[:16]

        if cache_key in self._embed_cache:
            return self._embed_cache[cache_key]

        embedding = self._embedder.encode(text).tolist()

        if len(self._embed_cache) < 5000:
            self._embed_cache[cache_key] = embedding

        return embedding

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Compute cosine similarity."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)


# =============================================================================
# DEPENDENCY RESOLVER
# =============================================================================

class DependencyResolver:
    """
    Resolves code dependencies for complete context.

    If you include function A that calls function B, you need B too!
    """

    def __init__(self, ast_chunker: ASTChunker):
        self._chunker = ast_chunker
        self._dependency_cache: Dict[str, Set[str]] = {}

    async def resolve_dependencies(
        self,
        primary_chunks: List[CodeChunk],
        all_chunks: Dict[str, CodeChunk],
        max_depth: int = SmartContextConfig.MAX_DEPENDENCY_DEPTH,
    ) -> List[CodeChunk]:
        """
        Resolve and add dependencies for primary chunks.

        Args:
            primary_chunks: Directly relevant chunks
            all_chunks: All available chunks {chunk_id: chunk}
            max_depth: How many levels of dependencies to follow

        Returns:
            Extended list including dependencies
        """
        result_ids = {c.chunk_id for c in primary_chunks}
        result = list(primary_chunks)

        # BFS to find dependencies
        queue = deque((c, 0) for c in primary_chunks)
        visited = set(result_ids)

        while queue:
            chunk, depth = queue.popleft()

            if depth >= max_depth:
                continue

            # Find callees (functions this calls)
            if SmartContextConfig.INCLUDE_CALLEES:
                for call_name in chunk.calls:
                    # Find matching chunk
                    for chunk_id, candidate in all_chunks.items():
                        if candidate.name == call_name and chunk_id not in visited:
                            visited.add(chunk_id)
                            candidate.relevance_reasons.append(RelevanceReason.DEPENDENCY)
                            candidate.relevance_score = chunk.relevance_score * 0.7  # Decay
                            result.append(candidate)
                            queue.append((candidate, depth + 1))
                            break

            # Find callers (functions that call this)
            if SmartContextConfig.INCLUDE_CALLERS:
                for chunk_id, candidate in all_chunks.items():
                    if chunk.name in candidate.calls and chunk_id not in visited:
                        visited.add(chunk_id)
                        candidate.relevance_reasons.append(RelevanceReason.DEPENDENCY)
                        candidate.relevance_score = chunk.relevance_score * 0.5
                        result.append(candidate)
                        queue.append((candidate, depth + 1))

        return result


# =============================================================================
# TOKEN BUDGET MANAGER
# =============================================================================

class TokenBudgetManager:
    """
    Manages token budget for context selection.

    Uses greedy packing with dependency constraints.
    """

    def __init__(self, token_counter: TokenCounter):
        self._counter = token_counter

    async def pack_within_budget(
        self,
        chunks: List[CodeChunk],
        max_tokens: int,
        dependency_graph: Dict[str, Set[str]],
    ) -> Tuple[List[CodeChunk], List[str]]:
        """
        Pack chunks within token budget, respecting dependencies.

        Args:
            chunks: Sorted by relevance (highest first)
            max_tokens: Maximum tokens allowed
            dependency_graph: {chunk_id: set of required chunk_ids}

        Returns:
            (selected_chunks, missing_dependency_names)
        """
        selected: List[CodeChunk] = []
        selected_ids: Set[str] = set()
        missing: List[str] = []
        current_tokens = 0

        # Priority queue (already sorted by relevance)
        for chunk in chunks:
            # Check if adding this chunk fits
            chunk_tokens = chunk.token_count

            # Check dependencies
            deps = dependency_graph.get(chunk.chunk_id, set())
            dep_tokens = 0
            deps_to_add = []

            for dep_id in deps:
                if dep_id not in selected_ids:
                    # Find the dep chunk
                    dep_chunk = next((c for c in chunks if c.chunk_id == dep_id), None)
                    if dep_chunk:
                        dep_tokens += dep_chunk.token_count
                        deps_to_add.append(dep_chunk)
                    else:
                        missing.append(dep_id)

            total_needed = chunk_tokens + dep_tokens

            if current_tokens + total_needed <= max_tokens:
                # Add dependencies first
                for dep in deps_to_add:
                    if dep.chunk_id not in selected_ids:
                        selected.append(dep)
                        selected_ids.add(dep.chunk_id)
                        current_tokens += dep.token_count

                # Add the chunk
                selected.append(chunk)
                selected_ids.add(chunk.chunk_id)
                current_tokens += chunk_tokens

            elif chunk_tokens <= max_tokens - current_tokens:
                # Chunk fits alone (skip deps)
                selected.append(chunk)
                selected_ids.add(chunk.chunk_id)
                current_tokens += chunk_tokens
                missing.extend(d for d in deps if d not in selected_ids)

            # Stop if we're close to limit
            if current_tokens >= max_tokens * 0.95:
                break

        return selected, missing


# =============================================================================
# CONTEXT FORMATTER
# =============================================================================

class ContextFormatter:
    """Formats selected chunks into LLM-ready context."""

    @staticmethod
    def format_chunks(
        chunks: List[CodeChunk],
        include_metadata: bool = True,
    ) -> str:
        """Format chunks into a readable context string."""
        if not chunks:
            return "# No relevant code found\n"

        # Group by file
        by_file: Dict[Path, List[CodeChunk]] = defaultdict(list)
        for chunk in chunks:
            by_file[chunk.file_path].append(chunk)

        sections = []

        for file_path, file_chunks in sorted(by_file.items()):
            # File header
            section = f"### {file_path.name}\n"
            section += f"# Path: {file_path}\n\n"

            # Sort chunks by line number
            file_chunks.sort(key=lambda c: c.start_line)

            for chunk in file_chunks:
                if include_metadata:
                    section += f"# {chunk.chunk_type.value}: {chunk.name}"
                    section += f" (lines {chunk.start_line}-{chunk.end_line})\n"
                    if chunk.relevance_reasons:
                        reasons = ", ".join(r.value for r in chunk.relevance_reasons)
                        section += f"# Relevance: {chunk.relevance_score:.2f} ({reasons})\n"

                section += chunk.source_code
                section += "\n\n"

            sections.append(section)

        return "\n---\n\n".join(sections)


# =============================================================================
# SMART CONTEXT SELECTOR - The Main Interface
# =============================================================================

class SmartContextSelector:
    """
    Main interface for surgical context retrieval.

    Usage:
        selector = await SmartContextSelector.create()
        context = await selector.get_relevant_context(
            query="Fix the authentication bug in login flow",
            max_tokens=4000,
        )
        print(context.formatted_context)
    """

    def __init__(self):
        self._token_counter: Optional[TokenCounter] = None
        self._ast_chunker: Optional[ASTChunker] = None
        self._relevance_scorer: Optional[RelevanceScorer] = None
        self._dep_resolver: Optional[DependencyResolver] = None
        self._budget_manager: Optional[TokenBudgetManager] = None

        # Oracle integration
        self._oracle = None

        self._initialized = False

    @classmethod
    async def create(cls) -> "SmartContextSelector":
        """Factory method for async initialization."""
        instance = cls()
        await instance.initialize()
        return instance

    async def initialize(self) -> None:
        """Initialize all components."""
        if self._initialized:
            return

        logger.info("[SmartContext] Initializing surgical context engine...")

        # Initialize token counter
        self._token_counter = await TokenCounter.get_instance()

        # Initialize AST chunker
        self._ast_chunker = ASTChunker(self._token_counter)

        # Initialize relevance scorer
        self._relevance_scorer = RelevanceScorer()
        await self._relevance_scorer.initialize()

        # Initialize dependency resolver
        self._dep_resolver = DependencyResolver(self._ast_chunker)

        # Initialize budget manager
        self._budget_manager = TokenBudgetManager(self._token_counter)

        # Try to get Oracle (synchronous factory)
        try:
            from backend.core.ouroboros.oracle import get_oracle
            self._oracle = get_oracle()  # Synchronous - returns TheOracle instance
            if not self._oracle._running:
                await self._oracle.initialize()
            logger.info("[SmartContext] Connected to Oracle graph")
        except Exception as e:
            logger.warning(f"[SmartContext] Oracle not available: {e}")

        self._initialized = True
        logger.info("[SmartContext] Initialization complete")

    async def get_relevant_context(
        self,
        query: str,
        max_tokens: int = SmartContextConfig.DEFAULT_MAX_TOKENS,
        target_files: Optional[List[Path]] = None,
        target_entities: Optional[Set[str]] = None,
        include_dependencies: bool = True,
    ) -> ContextPackage:
        """
        Get surgically selected context for a query.

        Args:
            query: The goal/query to find context for
            max_tokens: Maximum tokens for context
            target_files: Specific files to look in (optional)
            target_entities: Specific functions/classes to include (optional)
            include_dependencies: Whether to resolve and include dependencies

        Returns:
            ContextPackage with selected chunks and formatted context
        """
        start_time = time.time()

        if not self._initialized:
            await self.initialize()

        # Step 1: Find relevant files using Oracle (if available)
        files_to_search = await self._find_relevant_files(query, target_files)

        logger.info(f"[SmartContext] Searching {len(files_to_search)} files for: {query[:50]}...")

        # Step 2: Extract chunks from all files (parallel)
        all_chunks: Dict[str, CodeChunk] = {}
        chunk_tasks = [
            self._ast_chunker.extract_chunks(f, target_entities, include_all=True)
            for f in files_to_search
        ]

        chunk_results = await asyncio.gather(*chunk_tasks, return_exceptions=True)

        for chunks in chunk_results:
            if isinstance(chunks, Exception):
                continue
            for chunk in chunks:
                all_chunks[chunk.chunk_id] = chunk

        logger.info(f"[SmartContext] Extracted {len(all_chunks)} chunks")

        # Step 3: Score chunks for relevance
        scored_chunks = await self._relevance_scorer.score_chunks(
            query, list(all_chunks.values())
        )

        # Step 4: Filter to top candidates
        top_chunks = scored_chunks[:50]  # Top 50 for dependency resolution

        # Step 5: Resolve dependencies
        if include_dependencies:
            top_chunks = await self._dep_resolver.resolve_dependencies(
                top_chunks, all_chunks
            )

        # Step 6: Build dependency graph for packing
        dep_graph: Dict[str, Set[str]] = {}
        for chunk in top_chunks:
            deps = set()
            for call in chunk.calls:
                for cid, c in all_chunks.items():
                    if c.name == call:
                        deps.add(cid)
                        break
            dep_graph[chunk.chunk_id] = deps

        # Step 7: Pack within budget
        effective_budget = max_tokens - SmartContextConfig.RESERVE_TOKENS
        selected, missing = await self._budget_manager.pack_within_budget(
            top_chunks, effective_budget, dep_graph
        )

        # Step 8: Format output
        formatted = ContextFormatter.format_chunks(selected)
        total_tokens = self._token_counter.count(formatted)

        # Build token breakdown
        token_breakdown = {}
        for chunk in selected:
            file_key = str(chunk.file_path.name)
            token_breakdown[file_key] = token_breakdown.get(file_key, 0) + chunk.token_count

        # Build relevance breakdown
        relevance_breakdown = {c.chunk_id: c.relevance_score for c in selected}

        selection_time = (time.time() - start_time) * 1000

        package = ContextPackage(
            chunks=selected,
            formatted_context=formatted,
            total_tokens=total_tokens,
            max_tokens=max_tokens,
            file_count=len(set(c.file_path for c in selected)),
            chunk_count=len(selected),
            token_breakdown=token_breakdown,
            relevance_breakdown=relevance_breakdown,
            dependency_graph={cid: list(deps) for cid, deps in dep_graph.items() if cid in {c.chunk_id for c in selected}},
            missing_dependencies=missing,
            original_query=query,
            selection_time_ms=selection_time,
        )

        logger.info(
            f"[SmartContext] Selected {package.chunk_count} chunks "
            f"({package.total_tokens} tokens) in {selection_time:.1f}ms"
        )

        return package

    async def _find_relevant_files(
        self,
        query: str,
        target_files: Optional[List[Path]] = None,
    ) -> List[Path]:
        """Find files relevant to the query using Oracle or fallback."""
        if target_files:
            return [f for f in target_files if f.exists()]

        # Use Oracle if available
        if self._oracle:
            try:
                # Query oracle for relevant files
                relevant_nodes = await self._oracle.query_relevant_nodes(query, limit=20)
                files = list(set(Path(n.file_path) for n in relevant_nodes if n.file_path))
                if files:
                    return files
            except Exception as e:
                logger.warning(f"[SmartContext] Oracle query failed: {e}")

        # Fallback: search common paths
        search_paths = [
            Path.cwd() / "backend",
            Path.cwd() / "src",
            Path.cwd(),
        ]

        files = []
        for base in search_paths:
            if base.exists():
                for py_file in base.rglob("*.py"):
                    # Skip tests, cache, etc.
                    if any(p in str(py_file) for p in ["__pycache__", ".git", "test_", "_test.py"]):
                        continue
                    files.append(py_file)
                    if len(files) >= 100:
                        break

        return files[:100]  # Limit for performance

    async def get_context_for_file(
        self,
        file_path: Path,
        target_names: Optional[Set[str]] = None,
        max_tokens: int = SmartContextConfig.DEFAULT_MAX_TOKENS,
    ) -> ContextPackage:
        """Get context from a specific file."""
        return await self.get_relevant_context(
            query=f"Extract code from {file_path.name}",
            max_tokens=max_tokens,
            target_files=[file_path],
            target_entities=target_names,
        )

    async def get_blast_radius_context(
        self,
        changed_entity: str,
        max_tokens: int = SmartContextConfig.DEFAULT_MAX_TOKENS,
    ) -> ContextPackage:
        """
        Get context for understanding the blast radius of a change.

        Includes the entity and everything that depends on it.
        """
        return await self.get_relevant_context(
            query=f"What code uses or depends on {changed_entity}?",
            max_tokens=max_tokens,
            include_dependencies=True,
        )


# =============================================================================
# SINGLETON ACCESSOR
# =============================================================================

_smart_context_instance: Optional[SmartContextSelector] = None
_smart_context_lock = asyncio.Lock()


async def get_smart_context() -> SmartContextSelector:
    """Get the global SmartContextSelector instance."""
    global _smart_context_instance

    async with _smart_context_lock:
        if _smart_context_instance is None:
            _smart_context_instance = await SmartContextSelector.create()
        return _smart_context_instance


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    "SmartContextSelector",
    "SmartContextConfig",
    "ContextPackage",
    "CodeChunk",
    "ChunkType",
    "RelevanceReason",
    "get_smart_context",
]
