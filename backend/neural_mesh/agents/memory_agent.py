"""
JARVIS Neural Mesh - Memory Agent

A production agent responsible for semantic memory operations.
Provides intelligent memory storage, retrieval, and pattern recognition
across the entire Neural Mesh system.

Capabilities:
- store_memory: Store experiences, facts, and procedures
- recall_memory: Query memories with semantic search
- recall_similar: Find similar past situations
- store_experience: Store episodic memories
- find_patterns: Detect patterns across memories
- consolidate: Merge and strengthen related memories
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import (
    AgentMessage,
    KnowledgeType,
    MessagePriority,
    MessageType,
)
from ..knowledge.semantic_memory import (
    SemanticMemory,
    MemoryType,
    MemoryPriority,
    MemoryEntry,
    MemoryQueryResult,
    get_semantic_memory,
)

logger = logging.getLogger(__name__)


class MemoryAgent(BaseNeuralMeshAgent):
    """
    Memory Agent - Manages semantic memory for the Neural Mesh.

    This agent provides intelligent memory services to all other agents:
    - Store and retrieve memories with semantic search
    - Find similar past situations ("What happened last time?")
    - Store procedural knowledge ("How do I do this?")
    - Track patterns across experiences
    - Consolidate and strengthen important memories

    Usage:
        agent = MemoryAgent()
        await coordinator.register_agent(agent)

        # Other agents can request memory operations
        await agent.execute_task({
            "action": "store_memory",
            "content": "Fixed TypeError by adding null check",
            "memory_type": "procedural",
            "data": {"error": "TypeError", "solution": "null check"},
        })

        # Query memories
        results = await agent.execute_task({
            "action": "recall_memory",
            "query": "How to fix TypeError?",
            "limit": 5,
        })
    """

    def __init__(self) -> None:
        """Initialize the Memory Agent."""
        super().__init__(
            agent_name="memory_agent",
            agent_type="core",
            capabilities={
                "store_memory",
                "recall_memory",
                "recall_similar",
                "store_experience",
                "store_procedure",
                "store_fact",
                "find_patterns",
                "consolidate",
                "get_recent",
                "memory_stats",
            },
            version="1.0.0",
        )

        self._memory: Optional[SemanticMemory] = None
        self._store_count = 0
        self._query_count = 0
        self._pattern_cache: Dict[str, List[MemoryQueryResult]] = {}

    async def on_initialize(self) -> None:
        """Initialize agent-specific resources."""
        logger.info("Initializing MemoryAgent")

        # Get semantic memory instance
        self._memory = await get_semantic_memory()

        # Subscribe to memory-related messages
        await self.subscribe(
            MessageType.CUSTOM,
            self._handle_memory_request,
        )

        logger.info(
            f"MemoryAgent initialized with {self._memory.get_stats().total_memories} memories"
        )

    async def on_start(self) -> None:
        """Called when agent starts."""
        logger.info("MemoryAgent started - ready for memory operations")

    async def on_stop(self) -> None:
        """Cleanup when agent stops."""
        logger.info(
            f"MemoryAgent stopping - processed {self._store_count} stores, "
            f"{self._query_count} queries"
        )

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        """
        Execute a memory task.

        Supported actions:
        - store_memory: Store a new memory
        - recall_memory: Query memories
        - recall_similar: Find similar context memories
        - store_experience: Store an episodic experience
        - store_procedure: Store procedural knowledge
        - store_fact: Store a semantic fact
        - find_patterns: Find patterns in memories
        - consolidate: Trigger memory consolidation
        - get_recent: Get recent memories
        - memory_stats: Get memory statistics
        """
        action = payload.get("action", "")

        logger.debug(f"MemoryAgent executing: {action}")

        if action == "store_memory":
            return await self._store_memory(payload)
        elif action == "recall_memory":
            return await self._recall_memory(payload)
        elif action == "recall_similar":
            return await self._recall_similar(payload)
        elif action == "store_experience":
            return await self._store_experience(payload)
        elif action == "store_procedure":
            return await self._store_procedure(payload)
        elif action == "store_fact":
            return await self._store_fact(payload)
        elif action == "find_patterns":
            return await self._find_patterns(payload)
        elif action == "consolidate":
            return await self._consolidate(payload)
        elif action == "get_recent":
            return await self._get_recent(payload)
        elif action == "memory_stats":
            return self._get_stats()
        else:
            raise ValueError(f"Unknown memory action: {action}")

    async def _store_memory(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Store a memory."""
        content = payload.get("content", "")
        memory_type_str = payload.get("memory_type", "semantic")
        priority_str = payload.get("priority", "normal")
        data = payload.get("data", {})
        agent_name = payload.get("agent_name", self.agent_name)
        context_tags = set(payload.get("context_tags", []))
        importance = payload.get("importance", 0.5)
        ttl_seconds = payload.get("ttl_seconds")

        # Parse enums
        memory_type = MemoryType(memory_type_str.lower())
        priority = MemoryPriority[priority_str.upper()]

        entry = await self._memory.store(
            content=content,
            memory_type=memory_type,
            priority=priority,
            data=data,
            agent_name=agent_name,
            context_tags=context_tags,
            importance=importance,
            ttl_seconds=ttl_seconds,
        )

        self._store_count += 1

        # Add to knowledge graph if available
        if self.knowledge_graph:
            await self.add_knowledge(
                knowledge_type=KnowledgeType.OBSERVATION,
                data={
                    "memory_id": entry.id,
                    "memory_type": memory_type.value,
                    "content_preview": content[:100],
                },
                confidence=importance,
            )

        return {
            "status": "stored",
            "memory_id": entry.id,
            "memory_type": entry.memory_type.value,
            "priority": entry.priority.name,
        }

    async def _recall_memory(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Query memories."""
        query = payload.get("query", "")
        memory_types = payload.get("memory_types")
        agent_filter = payload.get("agent_filter")
        min_importance = payload.get("min_importance", 0.0)
        min_relevance = payload.get("min_relevance", 0.0)
        limit = payload.get("limit", 10)

        # Parse memory types
        parsed_types = None
        if memory_types:
            parsed_types = [MemoryType(mt.lower()) for mt in memory_types]

        results = await self._memory.recall(
            query=query,
            memory_types=parsed_types,
            agent_filter=agent_filter,
            min_importance=min_importance,
            min_relevance=min_relevance,
            limit=limit,
        )

        self._query_count += 1

        return {
            "status": "success",
            "query": query,
            "count": len(results),
            "results": [
                {
                    "memory_id": r.entry.id,
                    "content": r.entry.content,
                    "memory_type": r.entry.memory_type.value,
                    "data": r.entry.data,
                    "similarity_score": r.similarity_score,
                    "relevance_score": r.relevance_score,
                    "combined_score": r.combined_score,
                    "source_agent": r.entry.metadata.source_agent,
                    "created_at": r.entry.metadata.created_at.isoformat(),
                }
                for r in results
            ],
        }

    async def _recall_similar(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Find memories from similar context."""
        context = payload.get("context", {})
        memory_types = payload.get("memory_types")
        limit = payload.get("limit", 10)

        parsed_types = None
        if memory_types:
            parsed_types = [MemoryType(mt.lower()) for mt in memory_types]

        results = await self._memory.recall_similar_context(
            context=context,
            memory_types=parsed_types,
            limit=limit,
        )

        self._query_count += 1

        return {
            "status": "success",
            "context": context,
            "count": len(results),
            "results": [
                {
                    "memory_id": r.entry.id,
                    "content": r.entry.content,
                    "memory_type": r.entry.memory_type.value,
                    "data": r.entry.data,
                    "combined_score": r.combined_score,
                }
                for r in results
            ],
        }

    async def _store_experience(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Store an episodic experience."""
        description = payload.get("description", "")
        outcome = payload.get("outcome", "")
        context = payload.get("context", {})
        agent_name = payload.get("agent_name", self.agent_name)
        success = payload.get("success", True)
        lessons_learned = payload.get("lessons_learned", [])

        entry = await self._memory.store_experience(
            description=description,
            outcome=outcome,
            context=context,
            agent_name=agent_name,
            success=success,
            lessons_learned=lessons_learned,
        )

        self._store_count += 1

        return {
            "status": "stored",
            "memory_id": entry.id,
            "memory_type": "episodic",
            "success": success,
        }

    async def _store_procedure(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Store procedural knowledge."""
        task = payload.get("task", "")
        steps = payload.get("steps", [])
        prerequisites = payload.get("prerequisites", [])
        warnings = payload.get("warnings", [])
        agent_name = payload.get("agent_name", self.agent_name)

        entry = await self._memory.store_procedure(
            task=task,
            steps=steps,
            prerequisites=prerequisites,
            warnings=warnings,
            agent_name=agent_name,
        )

        self._store_count += 1

        return {
            "status": "stored",
            "memory_id": entry.id,
            "memory_type": "procedural",
            "task": task,
        }

    async def _store_fact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Store a semantic fact."""
        subject = payload.get("subject", "")
        predicate = payload.get("predicate", "")
        obj = payload.get("object", "")
        confidence = payload.get("confidence", 1.0)
        source = payload.get("source", self.agent_name)

        entry = await self._memory.store_fact(
            subject=subject,
            predicate=predicate,
            obj=obj,
            confidence=confidence,
            source=source,
        )

        self._store_count += 1

        return {
            "status": "stored",
            "memory_id": entry.id,
            "memory_type": "semantic",
            "fact": f"{subject} {predicate} {obj}",
        }

    async def _find_patterns(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Find patterns in memories."""
        pattern_type = payload.get("pattern_type", "")
        pattern_data = payload.get("pattern_data", {})
        limit = payload.get("limit", 10)

        # Check cache
        cache_key = f"{pattern_type}:{str(sorted(pattern_data.items()))}"
        if cache_key in self._pattern_cache:
            results = self._pattern_cache[cache_key]
        else:
            results = await self._memory.recall_by_pattern(
                pattern_type=pattern_type,
                pattern_data=pattern_data,
                limit=limit,
            )
            self._pattern_cache[cache_key] = results

            # Limit cache size
            if len(self._pattern_cache) > 100:
                # Remove oldest entries
                keys = list(self._pattern_cache.keys())
                for key in keys[:50]:
                    del self._pattern_cache[key]

        self._query_count += 1

        return {
            "status": "success",
            "pattern_type": pattern_type,
            "count": len(results),
            "patterns": [
                {
                    "memory_id": r.entry.id,
                    "content": r.entry.content,
                    "data": r.entry.data,
                    "similarity": r.similarity_score,
                }
                for r in results
            ],
        }

    async def _consolidate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Trigger memory consolidation."""
        # Memory consolidation happens automatically, but can be triggered manually
        await self._memory._consolidate_memories()

        return {
            "status": "success",
            "message": "Memory consolidation triggered",
        }

    async def _get_recent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Get recent memories."""
        hours = payload.get("hours", 24.0)
        memory_types = payload.get("memory_types")
        agent_filter = payload.get("agent_filter")
        limit = payload.get("limit", 50)

        parsed_types = None
        if memory_types:
            parsed_types = [MemoryType(mt.lower()) for mt in memory_types]

        entries = await self._memory.recall_recent(
            hours=hours,
            memory_types=parsed_types,
            agent_filter=agent_filter,
            limit=limit,
        )

        return {
            "status": "success",
            "hours": hours,
            "count": len(entries),
            "memories": [
                {
                    "memory_id": e.id,
                    "content": e.content,
                    "memory_type": e.memory_type.value,
                    "created_at": e.metadata.created_at.isoformat(),
                }
                for e in entries
            ],
        }

    def _get_stats(self) -> Dict[str, Any]:
        """Get memory statistics."""
        stats = self._memory.get_stats()
        return {
            "status": "success",
            "total_memories": stats.total_memories,
            "memories_by_type": stats.memories_by_type,
            "memories_by_priority": stats.memories_by_priority,
            "total_queries": stats.total_queries,
            "average_query_time_ms": stats.average_query_time_ms,
            "cache_hit_rate": stats.cache_hit_rate,
            "embedding_model": stats.embedding_model,
            "chromadb_version": stats.chromadb_version,
            "agent_store_count": self._store_count,
            "agent_query_count": self._query_count,
        }

    async def _handle_memory_request(self, message: AgentMessage) -> None:
        """Handle incoming memory requests from other agents."""
        if message.payload.get("type") != "memory_request":
            return

        action = message.payload.get("action")
        data = message.payload.get("data", {})

        try:
            result = await self.execute_task({"action": action, **data})

            # Send response
            await self.publish(
                to_agent=message.from_agent,
                message_type=MessageType.RESPONSE,
                payload={
                    "type": "memory_response",
                    "request_id": message.payload.get("request_id"),
                    "result": result,
                },
            )

            # v238.0: Broadcast memory operation for cross-agent awareness
            try:
                await self.broadcast(
                    message_type=MessageType.KNOWLEDGE_SHARED,
                    payload={
                        "type": "memory_insight",
                        "action": action,
                        "key_count": len(result) if isinstance(result, list) else 1,
                    },
                    priority=MessagePriority.LOW,
                )
            except Exception:
                pass  # Best-effort broadcast
        except Exception as e:
            logger.exception(f"Error handling memory request: {e}")
            await self.publish(
                to_agent=message.from_agent,
                message_type=MessageType.RESPONSE,
                payload={
                    "type": "memory_response",
                    "request_id": message.payload.get("request_id"),
                    "error": str(e),
                },
            )

    # Convenience methods for direct access
    async def remember(
        self,
        content: str,
        memory_type: str = "semantic",
        data: Optional[Dict] = None,
        importance: float = 0.5,
    ) -> str:
        """Quick method to store a memory. Returns memory ID."""
        result = await self.execute_task({
            "action": "store_memory",
            "content": content,
            "memory_type": memory_type,
            "data": data or {},
            "importance": importance,
        })
        return result["memory_id"]

    async def recall(
        self,
        query: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Quick method to recall memories."""
        result = await self.execute_task({
            "action": "recall_memory",
            "query": query,
            "limit": limit,
        })
        return result["results"]

    async def what_happened_last_time(
        self,
        context: Dict[str, Any],
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Answer "What happened last time?" for a given context.

        Args:
            context: Current context (error, file, function, etc.)
            limit: Maximum results

        Returns:
            List of similar past experiences
        """
        result = await self.execute_task({
            "action": "recall_similar",
            "context": context,
            "memory_types": ["episodic"],
            "limit": limit,
        })
        return result["results"]

    async def how_do_i(
        self,
        task: str,
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        Answer "How do I do X?" queries.

        Args:
            task: The task to look up
            limit: Maximum results

        Returns:
            List of procedural memories
        """
        result = await self.execute_task({
            "action": "recall_memory",
            "query": f"how to {task}",
            "memory_types": ["procedural"],
            "limit": limit,
        })
        return result["results"]
