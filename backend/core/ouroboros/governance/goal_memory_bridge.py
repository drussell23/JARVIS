"""GoalMemoryBridge — Connects LongTermMemoryManager to Ouroboros pipeline.

Queries ChromaDB-backed episodic and semantic memory for relevant context
before code generation, and records operation outcomes after completion.
This gives Ouroboros persistent cross-session goal awareness.

Also maintains a **thought log** (`.jarvis/ouroboros_thoughts.jsonl`) that
records the organism's reasoning process in human-readable form. This serves
as the "conversation thread" the user can follow — Ouroboros explaining its
decisions, what it remembers, and what it predicts.

Boundary Principle (Manifesto §4 — The Synthetic Soul):
  Deterministic: Query format, similarity thresholds, prompt injection.
  Agentic: What the provider *does* with the memory context.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_THOUGHT_LOG_PATH = Path(
    os.environ.get("JARVIS_THOUGHT_LOG_PATH", ".jarvis/ouroboros_thoughts.jsonl")
)


class GoalMemoryBridge:
    """Thin facade connecting LongTermMemoryManager to governance pipeline.

    All methods are null-safe: if the memory manager is unavailable, they
    return empty/default values without raising. The pipeline continues
    without goal memory in degraded mode.

    Parameters
    ----------
    memory_manager:
        LongTermMemoryManager instance (from ``get_long_term_memory()``).
        Pass ``None`` for graceful degradation.
    """

    def __init__(self, memory_manager: Any = None) -> None:
        self._memory = memory_manager

    @property
    def is_active(self) -> bool:
        """True if the memory manager is available."""
        return self._memory is not None

    async def get_relevant_context(
        self,
        description: str,
        target_files: Tuple[str, ...],
        limit: int = 5,
    ) -> str:
        """Query memory for context relevant to the current operation.

        Returns a formatted markdown string suitable for injection into
        the ``strategic_memory_prompt`` field of OperationContext.

        Parameters
        ----------
        description:
            Operation description (used as the semantic search query).
        target_files:
            Files being modified (included in query for specificity).
        limit:
            Max memory entries to return.
        """
        if self._memory is None:
            return ""

        try:
            query = f"{description} {' '.join(target_files[:5])}"
            results: List[Dict[str, Any]] = await self._memory.query(
                query=query,
                memory_types=["episodes", "facts", "procedures"],
                limit=limit,
                min_similarity=0.4,
            )

            if not results:
                return ""

            lines = ["## Goal Memory (cross-session context)", ""]
            for r in results:
                doc = r.get("document", "")
                similarity = r.get("similarity", 0)
                mem_type = r.get("type", "unknown")
                if doc:
                    lines.append(
                        f"- [{mem_type}, sim={similarity:.2f}] {doc[:300]}"
                    )

            if len(lines) <= 2:
                return ""

            context = "\n".join(lines)

            # Log thought so user can follow Ouroboros' memory retrieval
            self.log_thought(
                op_id="pre-generate",
                phase="MEMORY_RECALL",
                thought=(
                    f"I found {len(results)} relevant memories for: {description[:100]}. "
                    f"Using past experience to guide this generation."
                ),
                memories_used=len(results),
            )
            logger.info(
                "[GoalMemory] Injecting %d memories (query=%.60s...)",
                len(results), query,
            )
            return context

        except Exception:
            logger.debug("[GoalMemory] Query failed", exc_info=True)
            return ""

    async def record_outcome(
        self,
        op_id: str,
        description: str,
        target_files: Tuple[str, ...],
        success: bool,
        failure_reason: str = "",
    ) -> None:
        """Record an operation outcome for future cross-session learning.

        Parameters
        ----------
        op_id:
            Operation identifier.
        description:
            What the operation was trying to do.
        target_files:
            Files that were modified.
        success:
            Whether the operation succeeded.
        failure_reason:
            If failed, why.
        """
        if self._memory is None:
            return

        try:
            outcome_text = (
                f"Operation {op_id}: {'SUCCESS' if success else 'FAILED'}. "
                f"Goal: {description[:200]}. "
                f"Files: {', '.join(target_files[:5])}."
            )
            if failure_reason:
                outcome_text += f" Failure: {failure_reason[:200]}."

            await self._memory.store(
                content=outcome_text,
                memory_type="episodes",
                metadata={
                    "op_id": op_id,
                    "success": success,
                    "files": list(target_files[:10]),
                },
            )
            logger.debug("[GoalMemory] Recorded outcome for op=%s", op_id)

            # Log thought for user visibility
            self.log_thought(
                op_id=op_id,
                phase="POST_APPLY",
                thought=(
                    f"{'Succeeded' if success else 'Failed'}: {description[:200]}. "
                    f"{'I will remember this for next time.' if success else f'Failure: {failure_reason[:100]}. I will learn from this.'}"
                ),
            )
        except Exception:
            logger.debug("[GoalMemory] Record failed", exc_info=True)

    # ------------------------------------------------------------------
    # Thought Log — visible conversation thread for the user
    # ------------------------------------------------------------------

    def log_thought(
        self,
        op_id: str,
        phase: str,
        thought: str,
        memories_used: int = 0,
    ) -> None:
        """Append a reasoning step to the thought log.

        The thought log at ``.jarvis/ouroboros_thoughts.jsonl`` is a
        human-readable JSONL file that shows Ouroboros' reasoning process
        — what it remembers, what it predicts, what it decides, and why.
        This is the "conversation thread" the user follows.
        """
        entry = {
            "timestamp": time.time(),
            "op_id": op_id,
            "phase": phase,
            "thought": thought,
            "memories_used": memories_used,
        }
        try:
            _THOUGHT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _THOUGHT_LOG_PATH.open("a") as f:
                f.write(json.dumps(entry) + "\n")
            # Also log to stdout so battle test -v shows it
            logger.info("[Ouroboros Thought] [%s] %s", phase, thought)
        except Exception:
            pass  # Thought logging is non-critical
