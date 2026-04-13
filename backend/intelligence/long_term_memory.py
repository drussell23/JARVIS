"""
JARVIS Long-Term Memory & Reasoning Chain System (v2.7)

Provides persistent, cross-session memory with advanced reasoning capabilities:
- Episodic memory (conversation history, events)
- Semantic memory (learned facts, patterns)
- Procedural memory (how-to knowledge, skills)
- Reasoning chains (tracked thinking processes)
- Learning from past successes and failures

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                   LONG-TERM MEMORY                          │
    ├─────────────────────────────────────────────────────────────┤
    │                                                             │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
    │  │  Episodic   │  │  Semantic   │  │    Procedural       │ │
    │  │  (Events)   │  │  (Facts)    │  │    (Skills)         │ │
    │  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘ │
    │         │                │                     │            │
    │         └────────────────┼─────────────────────┘            │
    │                          │                                  │
    │                    ┌─────▼─────┐                           │
    │                    │ ChromaDB  │                           │
    │                    │ (Vectors) │                           │
    │                    └─────┬─────┘                           │
    │                          │                                  │
    │  ┌─────────────────────┐│┌─────────────────────────────┐  │
    │  │  Reasoning Chains   ├┼┤  Persistent Intelligence    │  │
    │  │  (LangGraph State)  │││  (SQLite + Cloud Sync)      │  │
    │  └─────────────────────┘│└─────────────────────────────┘  │
    │                          │                                  │
    └──────────────────────────┼──────────────────────────────────┘
                               │
                         Cross-Session
                         Continuity

Key Features:
- Automatic memory consolidation
- Reasoning chain tracking and replay
- Cross-session learning
- Temporal patterns detection
- Success/failure learning
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class MemoryLayer(Enum):
    """Memory layers with different persistence characteristics."""
    WORKING = "working"  # Current session, fast access (RAM)
    SHORT_TERM = "short_term"  # Recent history, medium access (Cache)
    LONG_TERM = "long_term"  # Persistent, semantic search (ChromaDB)
    ETERNAL = "eternal"  # Critical knowledge, never forgotten (Cloud)


class ReasoningPhase(Enum):
    """Phases of a reasoning chain."""
    PERCEPTION = "perception"  # Gathering information
    ANALYSIS = "analysis"  # Understanding the problem
    HYPOTHESIS = "hypothesis"  # Generating possibilities
    EVALUATION = "evaluation"  # Testing hypotheses
    DECISION = "decision"  # Making a choice
    EXECUTION = "execution"  # Taking action
    REFLECTION = "reflection"  # Learning from outcome


class OutcomeType(Enum):
    """Outcome types for learning."""
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    USER_INTERVENTION = "user_intervention"


@dataclass
class ReasoningStep:
    """A single step in a reasoning chain."""
    step_id: str
    phase: ReasoningPhase
    thought: str
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.5
    alternatives: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    duration_ms: float = 0.0
    memory_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "phase": self.phase.value,
            "thought": self.thought,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "alternatives": self.alternatives,
            "timestamp": self.timestamp.isoformat(),
            "duration_ms": self.duration_ms,
            "memory_refs": self.memory_refs,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReasoningStep":
        return cls(
            step_id=data["step_id"],
            phase=ReasoningPhase(data["phase"]),
            thought=data["thought"],
            evidence=data.get("evidence", []),
            confidence=data.get("confidence", 0.5),
            alternatives=data.get("alternatives", []),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            duration_ms=data.get("duration_ms", 0.0),
            memory_refs=data.get("memory_refs", []),
        )


@dataclass
class ReasoningChain:
    """A complete reasoning chain with outcome tracking."""
    chain_id: str
    query: str
    context: Dict[str, Any]
    steps: List[ReasoningStep] = field(default_factory=list)
    outcome: Optional[OutcomeType] = None
    result: Any = None
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    total_duration_ms: float = 0.0
    tokens_used: int = 0
    model_used: str = ""
    success_factors: List[str] = field(default_factory=list)
    failure_factors: List[str] = field(default_factory=list)

    def add_step(self, step: ReasoningStep) -> None:
        """Add a step to the chain."""
        self.steps.append(step)

    def complete(
        self,
        outcome: OutcomeType,
        result: Any = None,
        success_factors: Optional[List[str]] = None,
        failure_factors: Optional[List[str]] = None,
    ) -> None:
        """Mark the chain as complete."""
        self.outcome = outcome
        self.result = result
        self.completed_at = datetime.now()
        self.total_duration_ms = (
            self.completed_at - self.started_at
        ).total_seconds() * 1000

        if success_factors:
            self.success_factors = success_factors
        if failure_factors:
            self.failure_factors = failure_factors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "query": self.query,
            "context": self.context,
            "steps": [s.to_dict() for s in self.steps],
            "outcome": self.outcome.value if self.outcome else None,
            "result": self.result,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "total_duration_ms": self.total_duration_ms,
            "tokens_used": self.tokens_used,
            "model_used": self.model_used,
            "success_factors": self.success_factors,
            "failure_factors": self.failure_factors,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReasoningChain":
        chain = cls(
            chain_id=data["chain_id"],
            query=data["query"],
            context=data.get("context", {}),
            started_at=datetime.fromisoformat(data["started_at"]),
            tokens_used=data.get("tokens_used", 0),
            model_used=data.get("model_used", ""),
        )
        chain.steps = [ReasoningStep.from_dict(s) for s in data.get("steps", [])]
        chain.outcome = OutcomeType(data["outcome"]) if data.get("outcome") else None
        chain.result = data.get("result")
        chain.completed_at = (
            datetime.fromisoformat(data["completed_at"])
            if data.get("completed_at") else None
        )
        chain.total_duration_ms = data.get("total_duration_ms", 0.0)
        chain.success_factors = data.get("success_factors", [])
        chain.failure_factors = data.get("failure_factors", [])
        return chain


@dataclass
class LearnedPattern:
    """A pattern learned from past reasoning."""
    pattern_id: str
    pattern_type: str  # "success", "failure", "efficiency", "approach"
    description: str
    trigger_conditions: Dict[str, Any]
    recommended_actions: List[str]
    confidence: float = 0.5
    occurrences: int = 1
    last_seen: datetime = field(default_factory=datetime.now)
    chain_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "pattern_type": self.pattern_type,
            "description": self.description,
            "trigger_conditions": self.trigger_conditions,
            "recommended_actions": self.recommended_actions,
            "confidence": self.confidence,
            "occurrences": self.occurrences,
            "last_seen": self.last_seen.isoformat(),
            "chain_refs": self.chain_refs,
        }


class LongTermMemoryManager:
    """
    Unified long-term memory manager for JARVIS.

    Integrates:
    - Semantic memory (ChromaDB)
    - Persistent intelligence (state sync)
    - Reasoning chain tracking
    - Cross-session learning

    Usage:
        manager = await LongTermMemoryManager.create()

        # Store episodic memory
        await manager.store_episode("user_asked_about_weather", {"query": "weather"})

        # Store learned fact
        await manager.store_fact("user_prefers_celsius", True)

        # Start reasoning chain
        chain = await manager.start_reasoning_chain("How to fix the bug?", context)
        chain.add_step(ReasoningStep(...))
        await manager.complete_chain(chain, OutcomeType.SUCCESS)

        # Find similar past reasoning
        similar = await manager.find_similar_reasoning("bug fix", limit=5)

        # Learn from successes
        patterns = await manager.extract_success_patterns()
    """

    # Configuration via environment
    CHROMADB_PATH = os.getenv(
        "JARVIS_CHROMADB_PATH",
        str(Path.home() / ".jarvis" / "memory" / "chromadb")
    )
    EMBEDDING_MODEL = os.getenv(
        "JARVIS_EMBEDDING_MODEL",
        "all-MiniLM-L6-v2"
    )

    # Memory limits
    WORKING_MEMORY_SIZE = int(os.getenv("WORKING_MEMORY_SIZE", "100"))
    SHORT_TERM_TTL_HOURS = int(os.getenv("SHORT_TERM_TTL_HOURS", "24"))
    CONSOLIDATION_INTERVAL_SECONDS = float(
        os.getenv("MEMORY_CONSOLIDATION_INTERVAL", "3600")
    )

    # Reasoning chain settings
    MAX_CHAIN_STEPS = int(os.getenv("MAX_REASONING_STEPS", "50"))
    CHAIN_HISTORY_SIZE = int(os.getenv("REASONING_CHAIN_HISTORY", "1000"))

    def __init__(self) -> None:
        """Initialize the manager. Use create() for async initialization."""
        # ChromaDB client
        self._chromadb: Optional[Any] = None
        self._collections: Dict[str, Any] = {}

        # Embedding model
        self._embedder: Optional[Any] = None

        # Working memory (in-RAM, current session)
        self._working_memory: deque = deque(maxlen=self.WORKING_MEMORY_SIZE)
        self._working_memory_lock = asyncio.Lock()

        # Short-term memory cache
        self._short_term: Dict[str, Tuple[Any, datetime]] = {}
        self._short_term_lock = asyncio.Lock()

        # Reasoning chains
        self._active_chains: Dict[str, ReasoningChain] = {}
        self._chain_history: deque[ReasoningChain] = deque(
            maxlen=self.CHAIN_HISTORY_SIZE
        )

        # Learned patterns
        self._patterns: Dict[str, LearnedPattern] = {}

        # Persistent intelligence integration
        self._persistent: Optional[Any] = None

        # Background tasks
        self._running = False
        self._consolidation_task: Optional[asyncio.Task] = None

        logger.info("[LONG-TERM-MEMORY] Initialized")

    @classmethod
    async def create(cls) -> "LongTermMemoryManager":
        """Create and initialize the manager."""
        manager = cls()
        await manager.initialize()
        return manager

    async def initialize(self) -> None:
        """Initialize all memory systems."""
        # Route the ChromaDB directory through sandbox_fallback so that
        # sandboxed executors (battle test harness running under a
        # read-only host FS) degrade gracefully to
        # ``.ouroboros/state/sandbox_fallback/chromadb/`` instead of
        # crashing with ``readonly database``. When the primary path
        # is writable this is a no-op. Iron Gate stays hot.
        try:
            from backend.core.ouroboros.governance.sandbox_paths import (
                sandbox_fallback,
            )
            self.CHROMADB_PATH = str(sandbox_fallback(Path(self.CHROMADB_PATH)))
        except Exception:
            pass
        try:
            Path(self.CHROMADB_PATH).mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            logger.warning(
                "[LONG-TERM-MEMORY] ChromaDB path %s not writable; "
                "semantic memory will run in degraded mode",
                self.CHROMADB_PATH,
            )

        # Initialize ChromaDB
        await self._init_chromadb()

        # Initialize embedding model
        await self._init_embedder()

        # Initialize persistent intelligence integration
        await self._init_persistent()

        # Load patterns from storage
        await self._load_patterns()

        self._running = True

        # Start consolidation task
        self._consolidation_task = asyncio.create_task(
            self._consolidation_loop(),
            name="memory_consolidation"
        )

        logger.info("[LONG-TERM-MEMORY] Initialization complete")

    async def _init_chromadb(self) -> None:
        """Initialize ChromaDB."""
        try:
            import chromadb
            from chromadb.config import Settings

            self._chromadb = chromadb.PersistentClient(
                path=self.CHROMADB_PATH,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                ),
            )

            # Create collections for different memory types
            collection_configs = [
                ("episodes", "Episodic memories - events and experiences"),
                ("facts", "Semantic memories - facts and knowledge"),
                ("procedures", "Procedural memories - how-to knowledge"),
                ("reasoning", "Reasoning chains and thought processes"),
                ("patterns", "Learned patterns from past experiences"),
            ]

            for name, description in collection_configs:
                self._collections[name] = self._chromadb.get_or_create_collection(
                    name=name,
                    metadata={"description": description},
                )

            logger.info(
                f"[LONG-TERM-MEMORY] ChromaDB initialized with {len(collection_configs)} collections"
            )

        except ImportError:
            logger.warning("[LONG-TERM-MEMORY] ChromaDB not available")
        except Exception as e:
            logger.exception(f"[LONG-TERM-MEMORY] ChromaDB init failed: {e}")

    async def _init_embedder(self) -> None:
        """Initialize embedding model via shared EmbeddingService singleton.

        Uses the centralized EmbeddingService to avoid creating a second
        SentenceTransformer instance. Multiple PyTorch/BLAS thread pools
        cause libmalloc heap corruption on macOS ARM64 (Python 3.9).
        """
        try:
            from backend.core.embedding_service import get_embedding_service

            _svc = await get_embedding_service()
            # Pre-load model so self._embedder.encode() works synchronously.
            # EmbeddingService handles the singleton — only one load ever happens.
            if await _svc._load_model():
                self._embedder = _svc._model  # shared SentenceTransformer instance
                logger.info(
                    "[LONG-TERM-MEMORY] Embedder initialized via shared "
                    "EmbeddingService (%s)",
                    _svc._config.model_name,
                )
            else:
                logger.warning("[LONG-TERM-MEMORY] EmbeddingService model load failed")
        except ImportError:
            # Fallback: direct SentenceTransformer (legacy, non-singleton)
            try:
                from sentence_transformers import SentenceTransformer

                self._embedder = SentenceTransformer(self.EMBEDDING_MODEL)
                logger.info(
                    "[LONG-TERM-MEMORY] Embedder initialized (direct fallback): %s",
                    self.EMBEDDING_MODEL,
                )
            except ImportError:
                logger.warning("[LONG-TERM-MEMORY] SentenceTransformers not available")
        except Exception as e:
            logger.warning(f"[LONG-TERM-MEMORY] Embedder init failed: {e}")

    async def _init_persistent(self) -> None:
        """Initialize persistent intelligence integration."""
        try:
            from backend.core.persistent_intelligence_manager import (
                get_persistent_intelligence,
            )
            self._persistent = await get_persistent_intelligence()
            logger.info("[LONG-TERM-MEMORY] Persistent intelligence connected")
        except ImportError:
            logger.warning("[LONG-TERM-MEMORY] Persistent intelligence not available")
        except Exception as e:
            logger.warning(f"[LONG-TERM-MEMORY] Persistent init failed: {e}")

    async def _load_patterns(self) -> None:
        """Load learned patterns from persistent storage."""
        if not self._persistent:
            return

        try:
            from backend.core.persistent_intelligence_manager import StateCategory

            patterns = await self._persistent.get_by_prefix("pattern:")
            for entry in patterns:
                pattern = LearnedPattern(**entry.value)
                self._patterns[pattern.pattern_id] = pattern

            logger.info(f"[LONG-TERM-MEMORY] Loaded {len(patterns)} patterns")
        except Exception as e:
            logger.warning(f"[LONG-TERM-MEMORY] Pattern loading failed: {e}")

    def _generate_embedding(self, text: str) -> Optional[List[float]]:
        """Generate embedding for text."""
        if not self._embedder:
            return None

        try:
            embedding = self._embedder.encode(text)
            return embedding.tolist()
        except Exception as e:
            logger.warning(f"[LONG-TERM-MEMORY] Embedding failed: {e}")
            return None

    async def store_episode(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        importance: float = 0.5,
    ) -> str:
        """
        Store an episodic memory (event/experience).

        Args:
            content: Description of the episode
            metadata: Additional context
            importance: Importance score (0-1)

        Returns:
            Memory ID
        """
        memory_id = hashlib.sha256(
            f"{content}:{datetime.now().isoformat()}".encode()
        ).hexdigest()[:16]

        # Store in ChromaDB
        if self._collections.get("episodes"):
            embedding = self._generate_embedding(content)

            self._collections["episodes"].add(
                ids=[memory_id],
                embeddings=[embedding] if embedding else None,
                documents=[content],
                metadatas=[{
                    "timestamp": datetime.now().isoformat(),
                    "importance": importance,
                    "layer": MemoryLayer.LONG_TERM.value,
                    **(metadata or {}),
                }],
            )

        # Also add to working memory
        async with self._working_memory_lock:
            self._working_memory.append({
                "id": memory_id,
                "type": "episode",
                "content": content,
                "metadata": metadata,
                "timestamp": datetime.now(),
            })

        logger.debug(f"[LONG-TERM-MEMORY] Stored episode: {memory_id}")
        return memory_id

    async def store_fact(
        self,
        fact: str,
        value: Any,
        source: str = "learned",
        confidence: float = 0.8,
    ) -> str:
        """
        Store a semantic fact.

        Args:
            fact: The fact description
            value: The fact value
            source: Where this fact came from
            confidence: Confidence in the fact (0-1)

        Returns:
            Memory ID
        """
        memory_id = hashlib.sha256(fact.encode()).hexdigest()[:16]

        content = f"{fact}: {json.dumps(value, default=str)}"

        if self._collections.get("facts"):
            embedding = self._generate_embedding(content)

            self._collections["facts"].upsert(
                ids=[memory_id],
                embeddings=[embedding] if embedding else None,
                documents=[content],
                metadatas=[{
                    "fact": fact,
                    "value": json.dumps(value, default=str),
                    "source": source,
                    "confidence": confidence,
                    "timestamp": datetime.now().isoformat(),
                }],
            )

        # Persist if critical
        if confidence > 0.9 and self._persistent:
            try:
                from backend.core.persistent_intelligence_manager import StateCategory
                await self._persistent.set(
                    f"fact:{memory_id}",
                    {"fact": fact, "value": value, "confidence": confidence},
                    StateCategory.LEARNING,
                )
            except Exception:
                pass

        logger.debug(f"[LONG-TERM-MEMORY] Stored fact: {fact}")
        return memory_id

    async def store_procedure(
        self,
        name: str,
        steps: List[str],
        context: Optional[Dict[str, Any]] = None,
        success_rate: float = 1.0,
    ) -> str:
        """
        Store procedural knowledge (how-to).

        Args:
            name: Name of the procedure
            steps: Steps to complete it
            context: When this procedure applies
            success_rate: Historical success rate

        Returns:
            Memory ID
        """
        memory_id = hashlib.sha256(name.encode()).hexdigest()[:16]

        content = f"{name}: " + " -> ".join(steps)

        if self._collections.get("procedures"):
            embedding = self._generate_embedding(content)

            self._collections["procedures"].upsert(
                ids=[memory_id],
                embeddings=[embedding] if embedding else None,
                documents=[content],
                metadatas=[{
                    "name": name,
                    "steps": json.dumps(steps),
                    "context": json.dumps(context or {}),
                    "success_rate": success_rate,
                    "timestamp": datetime.now().isoformat(),
                }],
            )

        logger.debug(f"[LONG-TERM-MEMORY] Stored procedure: {name}")
        return memory_id

    async def query(
        self,
        query: str,
        memory_types: Optional[List[str]] = None,
        limit: int = 10,
        min_similarity: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """
        Query memories using semantic search.

        Args:
            query: Search query
            memory_types: Which collections to search (None = all)
            limit: Max results
            min_similarity: Minimum similarity threshold

        Returns:
            List of matching memories
        """
        results = []

        if not self._chromadb:
            return results

        collections_to_search = memory_types or ["episodes", "facts", "procedures"]

        embedding = self._generate_embedding(query)
        if not embedding:
            return results

        for collection_name in collections_to_search:
            collection = self._collections.get(collection_name)
            if not collection:
                continue

            try:
                query_results = collection.query(
                    query_embeddings=[embedding],
                    n_results=limit,
                    include=["documents", "metadatas", "distances"],
                )

                if query_results and query_results.get("ids"):
                    for i, memory_id in enumerate(query_results["ids"][0]):
                        distance = query_results["distances"][0][i]
                        similarity = 1 - (distance / 2)  # Convert distance to similarity

                        if similarity >= min_similarity:
                            results.append({
                                "id": memory_id,
                                "type": collection_name,
                                "content": query_results["documents"][0][i],
                                "metadata": query_results["metadatas"][0][i],
                                "similarity": similarity,
                            })

            except Exception as e:
                logger.warning(f"[LONG-TERM-MEMORY] Query failed for {collection_name}: {e}")

        # Sort by similarity
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

    async def start_reasoning_chain(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> ReasoningChain:
        """
        Start a new reasoning chain.

        Args:
            query: The problem/question to reason about
            context: Additional context

        Returns:
            New ReasoningChain instance
        """
        chain_id = hashlib.sha256(
            f"{query}:{datetime.now().isoformat()}".encode()
        ).hexdigest()[:16]

        chain = ReasoningChain(
            chain_id=chain_id,
            query=query,
            context=context or {},
        )

        self._active_chains[chain_id] = chain
        logger.debug(f"[LONG-TERM-MEMORY] Started reasoning chain: {chain_id}")

        return chain

    async def add_reasoning_step(
        self,
        chain_id: str,
        phase: ReasoningPhase,
        thought: str,
        evidence: Optional[List[str]] = None,
        confidence: float = 0.5,
    ) -> ReasoningStep:
        """
        Add a step to an active reasoning chain.

        Args:
            chain_id: The chain to add to
            phase: Current reasoning phase
            thought: The thought/observation
            evidence: Supporting evidence
            confidence: Confidence in this step

        Returns:
            The created ReasoningStep
        """
        chain = self._active_chains.get(chain_id)
        if not chain:
            raise ValueError(f"No active chain with ID: {chain_id}")

        if len(chain.steps) >= self.MAX_CHAIN_STEPS:
            raise ValueError(f"Chain exceeded max steps ({self.MAX_CHAIN_STEPS})")

        step = ReasoningStep(
            step_id=f"{chain_id}_{len(chain.steps)}",
            phase=phase,
            thought=thought,
            evidence=evidence or [],
            confidence=confidence,
        )

        chain.add_step(step)
        return step

    async def complete_chain(
        self,
        chain: ReasoningChain,
        outcome: OutcomeType,
        result: Any = None,
        success_factors: Optional[List[str]] = None,
        failure_factors: Optional[List[str]] = None,
    ) -> None:
        """
        Complete a reasoning chain and learn from it.

        Args:
            chain: The chain to complete
            outcome: The outcome type
            result: The result of reasoning
            success_factors: What contributed to success
            failure_factors: What caused failure
        """
        chain.complete(
            outcome=outcome,
            result=result,
            success_factors=success_factors,
            failure_factors=failure_factors,
        )

        # Remove from active
        self._active_chains.pop(chain.chain_id, None)

        # Add to history
        self._chain_history.append(chain)

        # Store in ChromaDB for future reference
        if self._collections.get("reasoning"):
            content = f"Query: {chain.query}\nOutcome: {outcome.value}"
            if chain.steps:
                content += f"\nThoughts: {' -> '.join(s.thought for s in chain.steps[:5])}"

            embedding = self._generate_embedding(content)

            self._collections["reasoning"].add(
                ids=[chain.chain_id],
                embeddings=[embedding] if embedding else None,
                documents=[content],
                metadatas=[{
                    "outcome": outcome.value,
                    "steps_count": len(chain.steps),
                    "duration_ms": chain.total_duration_ms,
                    "timestamp": chain.started_at.isoformat(),
                    "query": chain.query,
                    "success_factors": json.dumps(chain.success_factors),
                    "failure_factors": json.dumps(chain.failure_factors),
                }],
            )

        # Extract patterns for learning
        await self._extract_patterns_from_chain(chain)

        logger.info(
            f"[LONG-TERM-MEMORY] Completed chain {chain.chain_id}: "
            f"{outcome.value} ({len(chain.steps)} steps, {chain.total_duration_ms:.1f}ms)"
        )

    async def find_similar_reasoning(
        self,
        query: str,
        limit: int = 5,
        outcome_filter: Optional[OutcomeType] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find similar past reasoning chains.

        Args:
            query: Query to match against
            limit: Max results
            outcome_filter: Only return chains with this outcome

        Returns:
            List of similar chains with metadata
        """
        results = await self.query(query, memory_types=["reasoning"], limit=limit * 2)

        if outcome_filter:
            results = [
                r for r in results
                if r.get("metadata", {}).get("outcome") == outcome_filter.value
            ]

        return results[:limit]

    async def _extract_patterns_from_chain(self, chain: ReasoningChain) -> None:
        """Extract and store patterns from a completed chain."""
        if not chain.outcome:
            return

        # Success patterns
        if chain.outcome == OutcomeType.SUCCESS and chain.success_factors:
            pattern_id = hashlib.sha256(
                json.dumps(chain.success_factors, sort_keys=True).encode()
            ).hexdigest()[:12]

            if pattern_id in self._patterns:
                # Update existing pattern
                self._patterns[pattern_id].occurrences += 1
                self._patterns[pattern_id].last_seen = datetime.now()
                self._patterns[pattern_id].chain_refs.append(chain.chain_id)
                self._patterns[pattern_id].confidence = min(
                    0.99,
                    self._patterns[pattern_id].confidence + 0.05
                )
            else:
                # Create new pattern
                pattern = LearnedPattern(
                    pattern_id=f"success_{pattern_id}",
                    pattern_type="success",
                    description=f"Success pattern from: {chain.query[:50]}",
                    trigger_conditions=chain.context,
                    recommended_actions=chain.success_factors,
                    confidence=0.6,
                    chain_refs=[chain.chain_id],
                )
                self._patterns[pattern.pattern_id] = pattern

                # Persist
                await self._persist_pattern(pattern)

        # Failure patterns
        if chain.outcome == OutcomeType.FAILURE and chain.failure_factors:
            pattern_id = hashlib.sha256(
                json.dumps(chain.failure_factors, sort_keys=True).encode()
            ).hexdigest()[:12]

            pattern = LearnedPattern(
                pattern_id=f"failure_{pattern_id}",
                pattern_type="failure",
                description=f"Failure pattern from: {chain.query[:50]}",
                trigger_conditions=chain.context,
                recommended_actions=[f"Avoid: {f}" for f in chain.failure_factors],
                confidence=0.6,
                chain_refs=[chain.chain_id],
            )
            self._patterns[pattern.pattern_id] = pattern
            await self._persist_pattern(pattern)

    async def _persist_pattern(self, pattern: LearnedPattern) -> None:
        """Persist a pattern to long-term storage."""
        if not self._persistent:
            return

        try:
            from backend.core.persistent_intelligence_manager import StateCategory
            await self._persistent.set(
                f"pattern:{pattern.pattern_id}",
                pattern.to_dict(),
                StateCategory.LEARNING,
            )
        except Exception as e:
            logger.warning(f"[LONG-TERM-MEMORY] Pattern persist failed: {e}")

    async def get_relevant_patterns(
        self,
        context: Dict[str, Any],
        pattern_type: Optional[str] = None,
        limit: int = 5,
    ) -> List[LearnedPattern]:
        """
        Get patterns relevant to the current context.

        Args:
            context: Current context to match
            pattern_type: Filter by pattern type
            limit: Max results

        Returns:
            List of relevant patterns
        """
        relevant = []

        for pattern in self._patterns.values():
            if pattern_type and pattern.pattern_type != pattern_type:
                continue

            # Simple context matching (could be enhanced with semantic similarity)
            match_score = 0
            for key, value in pattern.trigger_conditions.items():
                if key in context and context[key] == value:
                    match_score += 1

            if match_score > 0 or not pattern.trigger_conditions:
                relevant.append((pattern, match_score * pattern.confidence))

        # Sort by score
        relevant.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in relevant[:limit]]

    async def _consolidation_loop(self) -> None:
        """Background memory consolidation."""
        while self._running:
            try:
                await asyncio.sleep(self.CONSOLIDATION_INTERVAL_SECONDS)
                await self._consolidate_memories()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[LONG-TERM-MEMORY] Consolidation error: {e}")

    async def _consolidate_memories(self) -> None:
        """Consolidate and prune memories."""
        # Move important short-term to long-term
        now = datetime.now()
        ttl = timedelta(hours=self.SHORT_TERM_TTL_HOURS)

        async with self._short_term_lock:
            expired = [
                key for key, (_, timestamp) in self._short_term.items()
                if now - timestamp > ttl
            ]
            for key in expired:
                del self._short_term[key]

        logger.debug(f"[LONG-TERM-MEMORY] Consolidation complete, pruned {len(expired)} entries")

    def get_stats(self) -> Dict[str, Any]:
        """Get memory statistics."""
        return {
            "working_memory_size": len(self._working_memory),
            "short_term_size": len(self._short_term),
            "active_chains": len(self._active_chains),
            "chain_history_size": len(self._chain_history),
            "patterns_count": len(self._patterns),
            "chromadb_available": self._chromadb is not None,
            "embedder_available": self._embedder is not None,
            "persistent_connected": self._persistent is not None,
        }

    async def shutdown(self) -> None:
        """Gracefully shutdown the memory manager."""
        logger.info("[LONG-TERM-MEMORY] Shutting down...")
        self._running = False

        if self._consolidation_task:
            self._consolidation_task.cancel()

        # Final consolidation
        await self._consolidate_memories()

        # Complete any active chains
        for chain in list(self._active_chains.values()):
            chain.complete(
                OutcomeType.TIMEOUT,
                failure_factors=["shutdown"]
            )
            self._chain_history.append(chain)

        logger.info("[LONG-TERM-MEMORY] Shutdown complete")


# Global instance
_manager: Optional[LongTermMemoryManager] = None


async def get_long_term_memory() -> LongTermMemoryManager:
    """Get or create the global LongTermMemoryManager instance."""
    global _manager

    if _manager is None:
        _manager = await LongTermMemoryManager.create()

    return _manager


async def shutdown_long_term_memory() -> None:
    """Shutdown the global manager."""
    global _manager

    if _manager:
        await _manager.shutdown()
        _manager = None
