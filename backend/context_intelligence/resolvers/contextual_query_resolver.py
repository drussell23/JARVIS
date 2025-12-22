#!/usr/bin/env python3
"""
Contextual Query Resolver for JARVIS
====================================

Handles ambiguous and contextual queries with:
- Pronoun resolution ("it", "that", "them")
- Missing space number inference
- Conversation context tracking
- Multi-monitor awareness
- Active space detection via Yabai
- Zero hardcoding, fully dynamic

Author: Derek Russell
Date: 2025-10-17
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from collections import deque
import json

logger = logging.getLogger(__name__)


class ResolutionStrategy(Enum):
    """Strategies for resolving ambiguous queries"""
    USE_ACTIVE_SPACE = "use_active_space"           # Default to current/active space
    USE_LAST_QUERIED = "use_last_queried"           # Use last queried space
    USE_PRIMARY_MONITOR = "use_primary_monitor"      # Use primary monitor's active space
    ASK_FOR_CLARIFICATION = "ask_for_clarification"  # Ask user to specify
    RESOLVE_FROM_CONTEXT = "resolve_from_context"    # Resolve from conversation history
    COMPARE_MULTIPLE = "compare_multiple"            # Compare multiple referenced spaces


class ReferenceType(Enum):
    """Types of contextual references"""
    PRONOUN = "pronoun"              # it, that, them, those
    DEMONSTRATIVE = "demonstrative"   # this, these
    COMPARATIVE = "comparative"       # compare, versus, vs
    IMPLICIT = "implicit"             # What's the error? (no explicit target)
    EXPLICIT = "explicit"             # Space 3, Monitor 2


@dataclass
class ContextualReference:
    """Represents a contextual reference in a query"""
    type: ReferenceType
    original_text: str
    resolved_targets: List[int] = field(default_factory=list)  # Space IDs or Monitor IDs
    confidence: float = 0.0
    resolution_strategy: Optional[ResolutionStrategy] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationTurn:
    """Represents a turn in the conversation"""
    timestamp: datetime
    user_query: str
    spaces_referenced: List[int]
    monitors_referenced: List[int]
    intent: str
    response: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryResolution:
    """Result of query resolution"""
    success: bool
    resolved_spaces: List[int]
    resolved_monitors: List[int]
    strategy_used: ResolutionStrategy
    requires_clarification: bool = False
    clarification_message: Optional[str] = None
    confidence: float = 0.0
    references: List[ContextualReference] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class ContextualQueryResolver:
    """
    Resolves ambiguous and contextual queries dynamically

    Capabilities:
    - Resolves "What's on that screen?" → active space
    - Resolves "Compare them" → last 2 queried spaces
    - Tracks conversation history (last N turns)
    - Integrates with Yabai for active space detection
    - Multi-monitor aware
    - Fully async, zero hardcoding
    """

    def __init__(
        self,
        history_size: int = 10,
        clarification_threshold: float = 0.6
    ):
        """
        Initialize the contextual query resolver

        Args:
            history_size: Number of conversation turns to remember
            clarification_threshold: Confidence threshold below which to ask for clarification
        """
        self.history: deque[ConversationTurn] = deque(maxlen=history_size)
        self.clarification_threshold = clarification_threshold

        # Pronoun patterns (dynamic, no hardcoding)
        self.pronoun_patterns = {
            'singular': re.compile(r'\b(it|that|this|the screen|the space|the monitor|the display)\b', re.I),
            'plural': re.compile(r'\b(them|those|these|both|all of them|the spaces|the monitors)\b', re.I),
            'comparative': re.compile(r'\b(compare|versus|vs|between)\b', re.I),
        }

        # Implicit query patterns
        self.implicit_patterns = {
            'error': re.compile(r'\b(what\'s|show|tell me about|fix|solve|debug)\s+(the\s+)?(error|issue|problem|bug)\b', re.I),
            'ide': re.compile(r'\b(what|which)\s+(ide|editor|app|application)\s+(am i using|is open|is running)\b', re.I),
            'happening': re.compile(r'\bwhat\'s\s+happening\b', re.I),
            'status': re.compile(r'\bwhat\'s\s+(my\s+)?(status|progress|workflow)\b', re.I),
        }

        # Cache for Yabai queries
        self._active_space_cache: Optional[Tuple[int, datetime]] = None
        self._cache_duration = timedelta(seconds=5)

        logger.info("[CONTEXTUAL RESOLVER] Initialized with history_size=%d", history_size)

    async def resolve_query(
        self,
        query: str,
        available_spaces: Optional[List[int]] = None,
        available_monitors: Optional[List[int]] = None
    ) -> QueryResolution:
        """
        Resolve an ambiguous or contextual query

        Args:
            query: The user's query
            available_spaces: List of available space IDs
            available_monitors: List of available monitor IDs

        Returns:
            QueryResolution with resolved targets and strategy
        """
        logger.info(f"[CONTEXTUAL RESOLVER] Resolving query: '{query}'")

        # Step 1: Detect contextual references
        references = await self._detect_references(query)
        logger.debug(f"[CONTEXTUAL RESOLVER] Detected {len(references)} references")

        # Step 2: Resolve references based on type and history
        resolution = await self._resolve_references(
            query,
            references,
            available_spaces,
            available_monitors
        )

        # Step 3: Record this turn in conversation history
        await self._record_turn(query, resolution)

        return resolution

    async def _detect_references(self, query: str) -> List[ContextualReference]:
        """Detect all contextual references in the query"""
        references = []

        # Check for explicit space/monitor numbers
        explicit_refs = await self._detect_explicit_references(query)
        references.extend(explicit_refs)

        # Check for pronouns
        pronoun_refs = await self._detect_pronoun_references(query)
        references.extend(pronoun_refs)

        # Check for implicit references
        implicit_refs = await self._detect_implicit_references(query)
        references.extend(implicit_refs)

        # Check for comparative references
        comparative_refs = await self._detect_comparative_references(query)
        references.extend(comparative_refs)

        return references

    async def _detect_explicit_references(self, query: str) -> List[ContextualReference]:
        """Detect explicit space/monitor references"""
        references = []

        # Match "space 3", "monitor 2", "display 1", etc.
        space_pattern = re.compile(r'\b(space|workspace)\s+(\d+)\b', re.I)
        monitor_pattern = re.compile(r'\b(monitor|display|screen)\s+(\d+)\b', re.I)

        for match in space_pattern.finditer(query):
            space_id = int(match.group(2))
            ref = ContextualReference(
                type=ReferenceType.EXPLICIT,
                original_text=match.group(0),
                resolved_targets=[space_id],
                confidence=1.0,
                metadata={'entity_type': 'space'}
            )
            references.append(ref)
            logger.debug(f"[CONTEXTUAL RESOLVER] Explicit space reference: {space_id}")

        for match in monitor_pattern.finditer(query):
            monitor_id = int(match.group(2))
            ref = ContextualReference(
                type=ReferenceType.EXPLICIT,
                original_text=match.group(0),
                resolved_targets=[monitor_id],
                confidence=1.0,
                metadata={'entity_type': 'monitor'}
            )
            references.append(ref)
            logger.debug(f"[CONTEXTUAL RESOLVER] Explicit monitor reference: {monitor_id}")

        return references

    async def _detect_pronoun_references(self, query: str) -> List[ContextualReference]:
        """Detect pronoun references (it, that, them, etc.)"""
        references = []

        # Singular pronouns
        if self.pronoun_patterns['singular'].search(query):
            ref = ContextualReference(
                type=ReferenceType.PRONOUN,
                original_text=self.pronoun_patterns['singular'].search(query).group(0),
                confidence=0.8,
                metadata={'pronoun_type': 'singular'}
            )
            references.append(ref)
            logger.debug(f"[CONTEXTUAL RESOLVER] Singular pronoun: {ref.original_text}")

        # Plural pronouns
        if self.pronoun_patterns['plural'].search(query):
            ref = ContextualReference(
                type=ReferenceType.PRONOUN,
                original_text=self.pronoun_patterns['plural'].search(query).group(0),
                confidence=0.8,
                metadata={'pronoun_type': 'plural'}
            )
            references.append(ref)
            logger.debug(f"[CONTEXTUAL RESOLVER] Plural pronoun: {ref.original_text}")

        return references

    async def _detect_implicit_references(self, query: str) -> List[ContextualReference]:
        """Detect implicit references (What's the error? What IDE am I using?)"""
        references = []

        for intent_name, pattern in self.implicit_patterns.items():
            if pattern.search(query):
                ref = ContextualReference(
                    type=ReferenceType.IMPLICIT,
                    original_text=pattern.search(query).group(0),
                    confidence=0.7,
                    metadata={'intent': intent_name}
                )
                references.append(ref)
                logger.debug(f"[CONTEXTUAL RESOLVER] Implicit reference ({intent_name}): {ref.original_text}")

        return references

    async def _detect_comparative_references(self, query: str) -> List[ContextualReference]:
        """Detect comparative references (compare them, A vs B)"""
        references = []

        if self.pronoun_patterns['comparative'].search(query):
            ref = ContextualReference(
                type=ReferenceType.COMPARATIVE,
                original_text=self.pronoun_patterns['comparative'].search(query).group(0),
                confidence=0.9,
                metadata={'operation': 'compare'}
            )
            references.append(ref)
            logger.debug(f"[CONTEXTUAL RESOLVER] Comparative reference: {ref.original_text}")

        return references

    async def _resolve_references(
        self,
        query: str,
        references: List[ContextualReference],
        available_spaces: Optional[List[int]],
        available_monitors: Optional[List[int]]
    ) -> QueryResolution:
        """Resolve all references to concrete space/monitor IDs"""

        # If explicit references found, use them directly
        explicit_refs = [r for r in references if r.type == ReferenceType.EXPLICIT]
        if explicit_refs:
            spaces = []
            monitors = []
            for ref in explicit_refs:
                if ref.metadata.get('entity_type') == 'space':
                    spaces.extend(ref.resolved_targets)
                elif ref.metadata.get('entity_type') == 'monitor':
                    monitors.extend(ref.resolved_targets)

            return QueryResolution(
                success=True,
                resolved_spaces=spaces,
                resolved_monitors=monitors,
                strategy_used=ResolutionStrategy.RESOLVE_FROM_CONTEXT,
                confidence=1.0,
                references=references,
                metadata={'explicit_resolution': True}
            )

        # Handle pronoun references
        pronoun_refs = [r for r in references if r.type == ReferenceType.PRONOUN]
        if pronoun_refs:
            return await self._resolve_pronouns(pronoun_refs, query)

        # Handle comparative references
        comparative_refs = [r for r in references if r.type == ReferenceType.COMPARATIVE]
        if comparative_refs:
            return await self._resolve_comparison(query)

        # Handle implicit references
        implicit_refs = [r for r in references if r.type == ReferenceType.IMPLICIT]
        if implicit_refs or not references:
            return await self._resolve_implicit(query, available_spaces, available_monitors)

        # Fallback: no clear resolution
        return await self._request_clarification(query, available_spaces, available_monitors)

    async def _resolve_pronouns(
        self,
        pronoun_refs: List[ContextualReference],
        query: str
    ) -> QueryResolution:
        """Resolve pronoun references from conversation history"""

        # Determine if singular or plural
        is_plural = any(
            ref.metadata.get('pronoun_type') == 'plural'
            for ref in pronoun_refs
        )

        if is_plural:
            # Plural pronoun → return last 2-3 queried spaces
            recent_spaces = await self._get_recent_spaces(count=3)
            if len(recent_spaces) >= 2:
                logger.info(f"[CONTEXTUAL RESOLVER] Resolved plural pronoun to spaces: {recent_spaces}")
                return QueryResolution(
                    success=True,
                    resolved_spaces=recent_spaces,
                    resolved_monitors=[],
                    strategy_used=ResolutionStrategy.USE_LAST_QUERIED,
                    confidence=0.85,
                    references=pronoun_refs,
                    metadata={'pronoun_type': 'plural', 'count': len(recent_spaces)}
                )

        # Singular pronoun → return last queried space
        recent_spaces = await self._get_recent_spaces(count=1)
        if recent_spaces:
            logger.info(f"[CONTEXTUAL RESOLVER] Resolved singular pronoun to space: {recent_spaces[0]}")
            return QueryResolution(
                success=True,
                resolved_spaces=recent_spaces,
                resolved_monitors=[],
                strategy_used=ResolutionStrategy.USE_LAST_QUERIED,
                confidence=0.9,
                references=pronoun_refs,
                metadata={'pronoun_type': 'singular'}
            )

        # No conversation history → ask for clarification
        return QueryResolution(
            success=False,
            resolved_spaces=[],
            resolved_monitors=[],
            strategy_used=ResolutionStrategy.ASK_FOR_CLARIFICATION,
            requires_clarification=True,
            clarification_message="Which space are you referring to? I don't have recent context.",
            confidence=0.3,
            references=pronoun_refs
        )

    async def _resolve_comparison(self, query: str) -> QueryResolution:
        """Resolve comparative queries (compare them, A vs B)"""

        # Get last 2 queried spaces for comparison
        recent_spaces = await self._get_recent_spaces(count=2)

        if len(recent_spaces) == 2:
            logger.info(f"[CONTEXTUAL RESOLVER] Resolved comparison to spaces: {recent_spaces}")
            return QueryResolution(
                success=True,
                resolved_spaces=recent_spaces,
                resolved_monitors=[],
                strategy_used=ResolutionStrategy.COMPARE_MULTIPLE,
                confidence=0.9,
                metadata={'operation': 'compare', 'count': 2}
            )

        # Not enough history
        return QueryResolution(
            success=False,
            resolved_spaces=[],
            resolved_monitors=[],
            strategy_used=ResolutionStrategy.ASK_FOR_CLARIFICATION,
            requires_clarification=True,
            clarification_message=f"Which spaces would you like to compare? (I only have {len(recent_spaces)} in recent history)",
            confidence=0.4
        )

    async def _resolve_implicit(
        self,
        query: str,
        available_spaces: Optional[List[int]],
        available_monitors: Optional[List[int]]
    ) -> QueryResolution:
        """Resolve implicit queries (What's happening? What's the error?)"""

        # Try to get active space from Yabai
        active_space = await self._get_active_space()

        if active_space:
            logger.info(f"[CONTEXTUAL RESOLVER] Resolved implicit query to active space: {active_space}")
            return QueryResolution(
                success=True,
                resolved_spaces=[active_space],
                resolved_monitors=[],
                strategy_used=ResolutionStrategy.USE_ACTIVE_SPACE,
                confidence=0.85,
                metadata={'source': 'yabai_active_space'}
            )

        # Fallback to Space 1
        logger.warning("[CONTEXTUAL RESOLVER] Could not determine active space, defaulting to Space 1")
        return QueryResolution(
            success=True,
            resolved_spaces=[1],
            resolved_monitors=[],
            strategy_used=ResolutionStrategy.USE_ACTIVE_SPACE,
            confidence=0.5,
            metadata={'source': 'default_fallback', 'warning': 'Using Space 1 as fallback'}
        )

    async def _request_clarification(
        self,
        query: str,
        available_spaces: Optional[List[int]],
        available_monitors: Optional[List[int]]
    ) -> QueryResolution:
        """Request clarification from the user"""

        # Build clarification message
        active_space = await self._get_active_space()

        if active_space:
            message = f"Which space? (Currently on Space {active_space})"
        else:
            message = "Which space would you like me to analyze?"

        if available_spaces:
            message += f" Available spaces: {', '.join(map(str, available_spaces))}"

        return QueryResolution(
            success=False,
            resolved_spaces=[],
            resolved_monitors=[],
            strategy_used=ResolutionStrategy.ASK_FOR_CLARIFICATION,
            requires_clarification=True,
            clarification_message=message,
            confidence=0.0
        )

    async def _get_active_space(self) -> Optional[int]:
        """Get the currently active space from Yabai"""

        # Check cache
        if self._active_space_cache:
            space_id, timestamp = self._active_space_cache
            if datetime.now() - timestamp < self._cache_duration:
                logger.debug(f"[CONTEXTUAL RESOLVER] Using cached active space: {space_id}")
                return space_id

        # Query Yabai for active space
        try:
            from vision.yabai_space_detector import get_yabai_detector
            yabai = get_yabai_detector()

            if not yabai.is_available():
                logger.warning("[CONTEXTUAL RESOLVER] Yabai not available")
                return None

            # Get all spaces and find the active one
            spaces = yabai.enumerate_all_spaces(include_display_info=True)
            active_space = next((s for s in spaces if s.get('has-focus', False)), None)

            if active_space:
                space_id = active_space.get('index', active_space.get('id', 1))
                self._active_space_cache = (space_id, datetime.now())
                logger.info(f"[CONTEXTUAL RESOLVER] Active space from Yabai: {space_id}")
                return space_id

        except Exception as e:
            logger.error(f"[CONTEXTUAL RESOLVER] Error getting active space: {e}")

        return None

    async def _get_recent_spaces(self, count: int = 1) -> List[int]:
        """Get recently queried spaces from conversation history"""
        spaces = []

        for turn in reversed(self.history):
            for space_id in turn.spaces_referenced:
                if space_id not in spaces:
                    spaces.append(space_id)
                if len(spaces) >= count:
                    return spaces

        logger.debug(f"[CONTEXTUAL RESOLVER] Found {len(spaces)} recent spaces: {spaces}")
        return spaces

    async def _record_turn(self, query: str, resolution: QueryResolution):
        """Record this conversation turn in history"""
        turn = ConversationTurn(
            timestamp=datetime.now(),
            user_query=query,
            spaces_referenced=resolution.resolved_spaces,
            monitors_referenced=resolution.resolved_monitors,
            intent=resolution.strategy_used.value,
            metadata={
                'confidence': resolution.confidence,
                'required_clarification': resolution.requires_clarification
            }
        )
        self.history.append(turn)
        logger.debug(f"[CONTEXTUAL RESOLVER] Recorded turn: {len(self.history)} total turns in history")

    def get_conversation_history(self, count: Optional[int] = None) -> List[ConversationTurn]:
        """Get conversation history"""
        if count:
            return list(self.history)[-count:]
        return list(self.history)

    def clear_history(self):
        """Clear conversation history"""
        self.history.clear()
        logger.info("[CONTEXTUAL RESOLVER] Conversation history cleared")

    async def get_context_summary(self) -> Dict[str, Any]:
        """Get a summary of current context"""
        active_space = await self._get_active_space()
        recent_spaces = await self._get_recent_spaces(count=3)

        return {
            "active_space": active_space,
            "recent_spaces": recent_spaces,
            "conversation_turns": len(self.history),
            "last_query": self.history[-1].user_query if self.history else None,
            "cache_status": {
                "active_space_cached": self._active_space_cache is not None,
                "cache_age_seconds": (
                    (datetime.now() - self._active_space_cache[1]).total_seconds()
                    if self._active_space_cache
                    else None
                )
            }
        }


# Singleton instance
_contextual_resolver: Optional[ContextualQueryResolver] = None


def get_contextual_resolver() -> ContextualQueryResolver:
    """Get singleton contextual query resolver"""
    global _contextual_resolver
    if _contextual_resolver is None:
        _contextual_resolver = ContextualQueryResolver()
    return _contextual_resolver


# Convenience function for quick resolution
async def resolve_query(query: str, **kwargs) -> QueryResolution:
    """Convenience function to resolve a query"""
    resolver = get_contextual_resolver()
    return await resolver.resolve_query(query, **kwargs)


if __name__ == "__main__":
    # Test the resolver
    async def test_resolver():
        resolver = ContextualQueryResolver()

        print("=" * 70)
        print("Testing Contextual Query Resolver")
        print("=" * 70)

        # Test 1: Explicit reference
        print("\n Test 1: Explicit reference")
        result = await resolver.resolve_query("What's on space 3?")
        print(f"  Query: What's on space 3?")
        print(f"  Resolved: {result.resolved_spaces}")
        print(f"  Strategy: {result.strategy_used.value}")
        print(f"  Confidence: {result.confidence}")

        # Test 2: Implicit reference (should get active space)
        print("\n✅ Test 2: Implicit reference")
        result = await resolver.resolve_query("What's happening?")
        print(f"  Query: What's happening?")
        print(f"  Resolved: {result.resolved_spaces}")
        print(f"  Strategy: {result.strategy_used.value}")

        # Test 3: Pronoun reference (should use last queried)
        print("\n✅ Test 3: Pronoun reference")
        await resolver.resolve_query("What's on space 5?")  # Prime history
        result = await resolver.resolve_query("What about that space?")
        print(f"  Query: What about that space?")
        print(f"  Resolved: {result.resolved_spaces}")
        print(f"  Strategy: {result.strategy_used.value}")

        # Test 4: Comparison
        print("\n✅ Test 4: Comparison")
        await resolver.resolve_query("What's on space 2?")  # Prime history
        result = await resolver.resolve_query("Compare them")
        print(f"  Query: Compare them")
        print(f"  Resolved: {result.resolved_spaces}")
        print(f"  Strategy: {result.strategy_used.value}")

        # Test 5: Context summary
        print("\n✅ Test 5: Context summary")
        summary = await resolver.get_context_summary()
        print(f"  Summary: {json.dumps(summary, indent=2, default=str)}")

        print("\n" + "=" * 70)
        print("✅ All tests complete!")
        print("=" * 70)

    asyncio.run(test_resolver())
