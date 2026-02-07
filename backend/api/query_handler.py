"""
Query Handler for JARVIS v2.0
==============================

Handles natural language queries with Trinity integration.

ROUTING ARCHITECTURE:
    User Query
         ↓
    UAE Context (if available)
         ↓
    Prime Router (Trinity Integration)
         ↓
    ┌──────────────────────────────────┐
    │  Local JARVIS-Prime (preferred)   │
    │  Cloud Claude (fallback)          │
    └──────────────────────────────────┘
         ↓
    Response with source tracking

v236.0: Adaptive Prompt System
    Static JARVIS_SYSTEM_PROMPT replaced with AdaptivePromptBuilder.
    System prompt, max_tokens, and temperature are now dynamically
    adapted based on QueryComplexity classification:
    - SIMPLE (e.g., "5+5"): 64 tokens, temp 0.0, terse prompt
    - MODERATE: 512 tokens, temp 0.3, concise prompt
    - COMPLEX: 2048 tokens, temp 0.5, thorough prompt
    - ADVANCED/EXPERT: 4096 tokens, temp 0.7, detailed prompt
"""
import logging
import os
from dataclasses import dataclass
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# ADAPTIVE PROMPT SYSTEM (v236.0)
# =============================================================================

@dataclass
class AdaptivePromptParams:
    """Dynamically computed prompt parameters based on query complexity."""
    system_prompt: str
    max_tokens: int
    temperature: float
    complexity_level: str


class AdaptivePromptBuilder:
    """
    v236.0: Builds complexity-adaptive system prompts, max_tokens, and temperature.

    Uses QueryComplexity classification to tailor LLM behavior.
    For SIMPLE queries, the JARVIS identity is intentionally omitted
    to prevent 7B models from generating conversational filler when
    the instruction says "reply with ONLY the answer."
    """

    _JARVIS_IDENTITY = os.getenv(
        "JARVIS_BASE_IDENTITY",
        "You are JARVIS, an advanced AI assistant created by Derek Russell."
    )

    # SIMPLE: No identity prefix — avoids 7B model conflict between
    # "You are JARVIS" (conversational) and "ONLY the number" (terse).
    # Includes few-shot examples to ground the model.
    _BEHAVIOR = {
        "SIMPLE": (
            "Reply with ONLY the direct answer. No preamble, no explanation, "
            "no sign-off, no offers to help further.\n"
            "For math: return ONLY the number.\n"
            "For yes/no: return ONLY yes or no.\n"
            "For definitions: ONE sentence maximum.\n\n"
            "Examples:\n"
            "Q: 5+5\nA: 10\n"
            "Q: Capital of France?\nA: Paris\n"
            "Q: Define gravity\nA: The force that attracts objects with mass toward each other."
        ),
        "MODERATE": (
            "Be concise. 2-3 sentences maximum. "
            "No filler phrases. Get to the point."
        ),
        "COMPLEX": (
            "Provide a clear, structured response. "
            "Be thorough where it adds value but don't pad."
        ),
        "ADVANCED": (
            "Provide detailed analysis with clear structure. "
            "Break down complex topics systematically."
        ),
        "EXPERT": (
            "Provide comprehensive, in-depth analysis. "
            "Use structured reasoning. Consider edge cases."
        ),
    }

    _MAX_TOKENS = {
        "SIMPLE": 64,
        "MODERATE": 512,
        "COMPLEX": 2048,
        "ADVANCED": 4096,
        "EXPERT": 4096,
    }

    # Temperature: 0.0 for deterministic factual, higher for creative
    _TEMPERATURE = {
        "SIMPLE": 0.0,
        "MODERATE": 0.3,
        "COMPLEX": 0.5,
        "ADVANCED": 0.7,
        "EXPERT": 0.7,
    }

    @classmethod
    def build(cls, classified_query=None, is_fallback: bool = False) -> AdaptivePromptParams:
        """
        Build adaptive prompt parameters.

        Args:
            classified_query: ClassifiedQuery from QueryComplexityManager, or None
            is_fallback: If True, use generous defaults matching current behavior
                         (4096 tokens, 0.7 temp) to avoid regression on fallback paths.
        """
        if classified_query is None:
            if is_fallback:
                # Fallback must match CURRENT behavior (4096/0.7) to avoid
                # truncating complex queries when classifier is unavailable
                level_name = "ADVANCED"
            else:
                level_name = "MODERATE"
        else:
            level_name = classified_query.complexity.level.name

        behavior = cls._BEHAVIOR.get(level_name, cls._BEHAVIOR["MODERATE"])

        # SIMPLE: omit identity to avoid 7B model conflict between
        # "You are JARVIS" (conversational) and "ONLY the number" (terse)
        if level_name == "SIMPLE":
            system_prompt = behavior
        else:
            system_prompt = f"{cls._JARVIS_IDENTITY}\n{behavior}"

        params = AdaptivePromptParams(
            system_prompt=system_prompt,
            max_tokens=cls._MAX_TOKENS.get(level_name, 2048),
            temperature=cls._TEMPERATURE.get(level_name, 0.5),
            complexity_level=level_name,
        )

        logger.info(
            f"[ADAPTIVE-PROMPT] level={level_name}, "
            f"max_tokens={params.max_tokens}, temp={params.temperature}"
        )

        return params


# =============================================================================
# QUERY HANDLERS
# =============================================================================

async def handle_query(
    command: str,
    context: Optional[Dict[str, Any]] = None,
    classified_query=None,
) -> Dict[str, Any]:
    """
    Handle a natural language query with Trinity-aware routing.

    This function routes queries through the Prime Router, which will:
    1. Try local JARVIS-Prime first (free, fast, private)
    2. Fall back to cloud Claude if Prime unavailable
    3. Use graceful degradation if both fail

    Args:
        command: The query text
        context: Optional context information
        classified_query: Optional ClassifiedQuery from QueryComplexityManager
                          for adaptive prompt generation (v236.0)

    Returns:
        Dict with success status, response, and routing metadata
    """
    try:
        logger.info(f"[QUERY] Processing query: {command}")

        # Try to get UAE context for enhanced understanding
        uae_context = None
        try:
            from main import app
            uae_engine = getattr(app.state, 'uae_engine', None)
            if uae_engine:
                # Get contextual information from UAE
                uae_context = await uae_engine.get_current_context()
                logger.debug("[QUERY] UAE context available")
        except Exception as e:
            logger.debug(f"[QUERY] UAE context not available: {e}")

        # Build conversation context
        conversation_context = []
        if context and "history" in context:
            conversation_context = context["history"]
        elif uae_context and "conversation_history" in uae_context:
            conversation_context = uae_context.get("conversation_history", [])

        # Route through Prime Router (Trinity integration)
        try:
            # Try relative import first (for backend/ working dir)
            try:
                from core.prime_router import get_prime_router
            except ImportError:
                from backend.core.prime_router import get_prime_router

            router = await get_prime_router()

            # v236.0: Adaptive prompt based on query complexity
            params = AdaptivePromptBuilder.build(classified_query)

            response = await router.generate(
                prompt=command,
                system_prompt=params.system_prompt,
                max_tokens=params.max_tokens,
                temperature=params.temperature,
                context=conversation_context if conversation_context else None,
            )

            logger.info(f"[QUERY] Response from {response.source} (latency: {response.latency_ms:.1f}ms)")

            return {
                "success": True,
                "response": response.content,
                "source": response.source,
                "model": response.model,
                "latency_ms": response.latency_ms,
                "tokens_used": response.tokens_used,
                "fallback_used": response.fallback_used,
                "metadata": response.metadata,
            }

        except ImportError as e:
            logger.warning(f"[QUERY] Prime Router not available: {e}")
            # Fall back to direct UAE if Prime Router not available
            return await _fallback_to_uae(command, context)

    except Exception as e:
        logger.error(f"[QUERY] Error processing query: {e}")
        return {
            "success": False,
            "response": f"Sorry, I encountered an error: {str(e)}",
            "error": str(e),
            "source": "error",
        }


async def _fallback_to_uae(command: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fallback to UAE engine when Prime Router is not available.

    Note (v236.0): UAE engine has its own internal prompting system.
    When UAE processes directly (line ~132), it does NOT use AdaptivePromptBuilder.
    When it falls through to _fallback_to_cloud, generous defaults (4096/0.7) apply.
    """
    try:
        from main import app
        uae_engine = getattr(app.state, 'uae_engine', None)

        if uae_engine:
            logger.info("[QUERY] Using UAE engine fallback")
            response = await uae_engine.process_query(command)

            return {
                "success": True,
                "response": response.get("response", "Query processed successfully"),
                "analysis": response.get("analysis", {}),
                "suggestions": response.get("suggestions", []),
                "source": "uae_engine",
                "fallback_used": True,
            }
        else:
            # Final fallback - direct cloud call
            return await _fallback_to_cloud(command)

    except Exception as e:
        logger.warning(f"[QUERY] UAE fallback failed: {e}")
        return await _fallback_to_cloud(command)


async def _fallback_to_cloud(command: str) -> Dict[str, Any]:
    """
    Final fallback - routes through Prime Router instead of direct Claude API.

    v2.7 FIX: Previously called AsyncAnthropic directly, bypassing the entire
    Prime routing system. Now uses PrimeRouter which has its own fallback chain:
    LOCAL_PRIME → CLOUD_RUN → CLOUD_CLAUDE

    v236.0: Uses is_fallback=True for generous defaults (4096 tokens, 0.7 temp)
    to avoid truncating complex queries that reach this fallback path.
    """
    try:
        # v2.7: Use Prime Router instead of direct Anthropic client
        try:
            from core.prime_router import get_prime_router
        except ImportError:
            from backend.core.prime_router import get_prime_router

        router = await get_prime_router()

        # v236.0: Fallback uses generous defaults — no classified_query available,
        # and we must not truncate complex queries that hit the fallback path.
        params = AdaptivePromptBuilder.build(classified_query=None, is_fallback=True)

        response = await router.generate(
            prompt=command,
            system_prompt=params.system_prompt,
            max_tokens=params.max_tokens,
            temperature=params.temperature,
        )

        # Extract content from response
        content = ""
        if hasattr(response, 'content') and response.content:
            if isinstance(response.content, list):
                content = response.content[0].text if hasattr(response.content[0], 'text') else str(response.content[0])
            else:
                content = str(response.content)
        elif isinstance(response, dict):
            content = response.get('content', response.get('response', ''))
        else:
            content = str(response)

        # Determine source from router
        source = getattr(response, 'source', 'prime_router_fallback')
        if isinstance(response, dict):
            source = response.get('source', 'prime_router_fallback')

        return {
            "success": True,
            "response": content,
            "source": source,
            "fallback_used": True,
            "routed_via_prime": True,  # v2.7: Track Prime routing
        }

    except Exception as e:
        logger.error(f"[QUERY] Prime router fallback failed: {e}")
        return {
            "success": False,
            "response": f"I apologize, but I'm experiencing technical difficulties: {str(e)}",
            "source": "degraded",
            "error": str(e),
        }


async def handle_query_stream(
    command: str,
    context: Optional[Dict[str, Any]] = None,
    classified_query=None,
):
    """
    Handle a streaming query response.

    Yields response chunks as they arrive.

    Args:
        command: The query text
        context: Optional context information
        classified_query: Optional ClassifiedQuery for adaptive prompt (v236.0)
    """
    try:
        try:
            from core.prime_router import get_prime_router
        except ImportError:
            from backend.core.prime_router import get_prime_router

        router = await get_prime_router()

        # v236.0: Adaptive prompt for streaming
        params = AdaptivePromptBuilder.build(classified_query)

        async for chunk in router.generate_stream(
            prompt=command,
            system_prompt=params.system_prompt,
            max_tokens=params.max_tokens,
            temperature=params.temperature,
        ):
            yield chunk

    except Exception as e:
        logger.error(f"[QUERY] Stream error: {e}")
        yield f"Error: {str(e)}"
