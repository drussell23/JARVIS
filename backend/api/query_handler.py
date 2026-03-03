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
import re
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

try:
    from backend.core.runtime_module_resolver import get_main_app
except ImportError:
    from core.runtime_module_resolver import get_main_app

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
    stop_sequences: Optional[List[str]] = None  # v237.0: Stop sequences for generation


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
    # Uses inline prose example to avoid Q:/A: pattern completion.
    _BEHAVIOR = {
        "SIMPLE": (
            "Reply with ONLY the direct answer. No preamble, no explanation, "
            "no sign-off, no offers to help further.\n"
            "For math: return ONLY the number.\n"
            "For yes/no: return ONLY yes or no.\n"
            "For definitions: ONE sentence maximum.\n\n"
            "Example: if asked '5+5', reply '10' — nothing else."
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
        "SIMPLE": 48,
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

    # v237.0: Stop sequences per complexity level.
    # SIMPLE uses stop sequences to catch any residual pattern completion.
    # Note: bare "Q:" omitted — could match inside legitimate content.
    # Only newline-prefixed patterns are safe.
    _STOP_SEQUENCES: Dict[str, Optional[List[str]]] = {
        "SIMPLE": ["\nQ:", "\nQ ", "\n\n"],
        "MODERATE": None,
        "COMPLEX": None,
        "ADVANCED": None,
        "EXPERT": None,
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

        stop_seqs = cls._STOP_SEQUENCES.get(level_name)

        params = AdaptivePromptParams(
            system_prompt=system_prompt,
            max_tokens=cls._MAX_TOKENS.get(level_name, 2048),
            temperature=cls._TEMPERATURE.get(level_name, 0.5),
            complexity_level=level_name,
            stop_sequences=stop_seqs,
        )

        logger.info(
            f"[ADAPTIVE-PROMPT] level={level_name}, "
            f"max_tokens={params.max_tokens}, temp={params.temperature}"
        )

        return params


# =============================================================================
# v240.0: SYMPY MATH SOLVER — DETERMINISTIC INTERCEPT
# =============================================================================

def _try_math_solve(command: str) -> Optional[Dict[str, Any]]:
    """
    v240.0: Try to solve math with sympy before LLM.
    Returns complete response dict if solved, None otherwise.
    Synchronous — sympy is CPU-bound and fast (<50ms).
    """
    try:
        try:
            from context_intelligence.handlers.math_solver import get_math_solver
        except ImportError:
            from backend.context_intelligence.handlers.math_solver import get_math_solver

        solver = get_math_solver()
        result = solver.detect_and_solve(command)
        if result.solved:
            formatted = solver.format_response(result)
            logger.info(
                f"[QUERY] v240.0: Math solved by sympy: "
                f"{result.expression_type.value} -> {str(result.solution)[:60]}"
            )
            return {
                "success": True,
                "response": formatted,
                "source": "local_sympy",
                "model": "sympy",
                "latency_ms": 0,
                "tokens_used": 0,
                "fallback_used": False,
                "math_result": {
                    "type": result.expression_type.value,
                    "steps": result.solution_steps,
                    "variables": result.variables,
                },
            }
    except ImportError:
        logger.debug("[QUERY] Math solver not available")
    except Exception as e:
        logger.debug(f"[QUERY] Math solver error (non-fatal): {e}")
    return None


# =============================================================================
# v241.0: TASK TYPE INFERENCE — J-Prime model routing hints
# Architecture note: J-Prime's TaskClassifier (dynamic_model_registry.py) has
# a more comprehensive classifier, but it runs AFTER the model is loaded. This
# lightweight classifier runs BEFORE the request reaches J-Prime so the GCP
# model swap coordinator can pre-load the right model. Keep in sync with
# GCP_TASK_MODEL_MAPPING in dynamic_model_registry.py.
# =============================================================================

# Programming languages (unambiguous)
_LANG_NAMES = re.compile(
    r'\b(?:python|javascript|typescript|rust|golang|go\s+lang'
    r'|c\+\+|cpp|java(?!script)|ruby|swift|kotlin|scala|haskell'
    r'|elixir|clojure|sql)\b',
    re.IGNORECASE,
)

# Strong code indicators (unambiguous even standalone)
_CODE_STRONG = re.compile(
    r'\b(?:function|def\s+\w|implement|debug|refactor'
    r'|compile|syntax|algorithm|regex|api\s+endpoint'
    r'|pull\s+request|git\s+(?:commit|push|merge))\b'
    r'|```',  # Code fence is always code
    re.IGNORECASE,
)

# Weak code indicators (ambiguous alone — "import the data", "class action")
# R2-2: class\s+\w moved here from _CODE_STRONG to avoid "class action" false positive
_CODE_WEAK = re.compile(
    r'\b(?:import|export|return|async|await|variable|loop|array|object|class\s+\w)\b',
    re.IGNORECASE,
)

_TASK_MATH_KEYWORDS = re.compile(
    r'\b(?:calculate|compute|math|equation|formula|solve|prove|proof|integral'
    r'|derivative|differentiate|integrate|matrix|statistics|probability|percent'
    r'|calculus|algebra|trigonometry|geometry|theorem)\b',
    re.IGNORECASE,
)

# Math word-problem indicators — numbers + question phrasing (no explicit math verb)
_MATH_WORD_PROBLEM = re.compile(
    r'\b(?:how\s+many|how\s+much|how\s+long|how\s+far|how\s+fast)\b',
    re.IGNORECASE,
)
_HAS_NUMBERS = re.compile(r'\d+')

# Creative / writing indicators
_CREATIVE_KEYWORDS = re.compile(
    r'\b(?:write\s+(?:me\s+)?(?:a|an|the)\s+'
    r'|story|poem|essay|fiction|novel|lyrics|haiku|sonnet'
    r'|brainstorm|creative|imagine|fantasy'
    r'|come\s+up\s+with|generate\s+(?:ideas?|names?|titles?)'
    r'|write\s+about)\b',
    re.IGNORECASE,
)

# Summarization indicators
_SUMMARIZE_KEYWORDS = re.compile(
    r'\b(?:summarize|summary|tldr|tl;dr|condense|shorten|brief|recap)\b',
    re.IGNORECASE,
)

# Translation indicators
_TRANSLATE_KEYWORDS = re.compile(
    r'\b(?:translate|translation|how\s+(?:do\s+you\s+)?say\s+.+\s+in)\b',
    re.IGNORECASE,
)

# Greeting patterns — short, non-substantive
_GREETING_PATTERNS = re.compile(
    r'^\s*(?:h(?:ello|i|ey)|good\s+(?:morning|afternoon|evening|night)'
    r'|what\'?s?\s+up|howdy|yo|sup|greetings'
    r'|hi+\s+(?:how\s+are\s+you|there|jarvis)'
    r'|how\s+are\s+you|how\'?s?\s+it\s+going'
    r'|thanks?|thank\s+you)\s*[?!.]*\s*$',
    re.IGNORECASE,
)

# Advanced math indicators — content that signals math_complex regardless of
# what the (space-oriented) complexity classifier says. These topics exceed
# what Qwen2.5-7B handles well and need the Qwen2.5-Math-7B specialist.
_MATH_ADVANCED_CONTENT = re.compile(
    r'\b(?:prove|proof|theorem|lemma|corollary'
    r'|derivative|differentiate|integral|integrate|calculus'
    r'|limit|convergence|divergence|series|sequence'
    r'|eigenvalue|eigenvector|determinant'
    r'|differential\s+equation|partial\s+derivative'
    r'|fourier|laplace|taylor\s+(?:series|expansion))\b',
    re.IGNORECASE,
)


def _infer_task_type(command: str, complexity_level: str) -> str:
    """
    v241.1: Infer J-Prime TaskType from query content + complexity.

    Detection priority:
      1. Greeting (short, pattern-matched — Phi-3.5-mini fast path)
      2. Math (keywords + variable-equation regex + word problems)
      3. Code (2+ indicators to avoid false positives — Issue #5)
      4. Creative (story/brainstorm/poem keywords — Llama-3.1)
      5. Summarize (summarize/tldr keywords — Llama-3.1)
      6. Translate (translate/say-in keywords — Mistral)
      7. Complexity-based fallback (ADVANCED/EXPERT → reason_complex)

    Returns one of the string values from GCP_TASK_MODEL_MAPPING.
    """
    cmd_stripped = command.strip()
    word_count = len(cmd_stripped.split())

    # 1. Greeting detection — must be short and match greeting pattern
    if word_count <= 6 and _GREETING_PATTERNS.search(cmd_stripped):
        return "greeting"

    # 2. Math detection (aligns with v240 math solver patterns)
    has_math_keyword = bool(_TASK_MATH_KEYWORDS.search(command))
    has_variable_eq = bool(re.search(r'\d+\s*[a-zA-Z]\s*[\+\-\*/\^]', command))
    has_word_problem = bool(_MATH_WORD_PROBLEM.search(command)) and bool(_HAS_NUMBERS.search(command))

    if has_math_keyword or has_variable_eq or has_word_problem:
        # Content-based complexity: advanced math topics always route to specialist
        # regardless of what the (space-oriented) complexity classifier says.
        # This catches "calculate the derivative" which the classifier may mark SIMPLE.
        is_advanced_math = bool(_MATH_ADVANCED_CONTENT.search(command))
        if is_advanced_math or has_word_problem or complexity_level in ("COMPLEX", "ADVANCED", "EXPERT"):
            return "math_complex"
        return "math_simple"

    # 3. Code detection — tightened (Issue #5, R2-2)
    has_lang = bool(_LANG_NAMES.search(command))
    has_strong = bool(_CODE_STRONG.search(command))
    has_weak = bool(_CODE_WEAK.search(command))
    code_signals = sum([has_lang, has_strong, has_weak])

    is_code = (
        has_strong                    # Strong indicator alone is enough
        or (has_lang and has_weak)    # Language + weak = code ("import in Python")
        or (code_signals >= 2)        # Any 2+ signals
    )
    if is_code:
        return (
            "code_complex"
            if complexity_level in ("COMPLEX", "ADVANCED", "EXPERT")
            else "code_simple"
        )

    # 4. Creative / writing detection
    if _CREATIVE_KEYWORDS.search(command):
        return "creative_brainstorm" if re.search(
            r'\b(?:brainstorm|ideas?|names?|titles?|come\s+up|generate)\b', command, re.I
        ) else "creative_write"

    # 5. Summarization detection
    if _SUMMARIZE_KEYWORDS.search(command):
        return "summarize"

    # 6. Translation detection
    if _TRANSLATE_KEYWORDS.search(command):
        return "translate"

    # 7. Complexity-based fallback
    if complexity_level == "SIMPLE":
        return "simple_chat"

    if complexity_level in ("ADVANCED", "EXPERT"):
        return "reason_complex"

    return "general_chat"


# =============================================================================
# QUERY HANDLERS
# =============================================================================

async def handle_query(
    command: str,
    context: Optional[Dict[str, Any]] = None,
    classified_query=None,
    deadline: Optional[float] = None,
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
            app = get_main_app(strict=False)
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

            # v240.0: Sympy math solver — intercept equations before LLM.
            math_response = _try_math_solve(command)
            if math_response is not None:
                return math_response

            # v237.0: Build kwargs with stop sequences if present
            gen_kwargs = {}
            if params.stop_sequences:
                gen_kwargs["stop"] = params.stop_sequences

            # v241.0: Task type hint for J-Prime model routing
            if classified_query:
                _task_type = _infer_task_type(command, params.complexity_level)
                gen_kwargs["task_type"] = _task_type
                gen_kwargs["complexity_level"] = params.complexity_level

            response = await router.generate(
                prompt=command,
                system_prompt=params.system_prompt,
                max_tokens=params.max_tokens,
                temperature=params.temperature,
                context=conversation_context if conversation_context else None,
                deadline=deadline,  # v241.0
                **gen_kwargs,
            )

            logger.info(f"[QUERY] Response from {response.source} (latency: {response.latency_ms:.1f}ms)")

            # v237.0: Defensive strip of leaked few-shot patterns.
            # J-Prime handles stop sequences server-side, but cloud fallback
            # and older J-Prime versions may not — strip client-side too.
            content = response.content
            if params.stop_sequences and content:
                for seq in params.stop_sequences:
                    idx = content.find(seq)
                    if idx >= 0:
                        content = content[:idx].rstrip()
                        logger.debug(
                            f"[QUERY] Stripped leaked content at '{seq}' "
                            f"(original len={len(response.content)}, trimmed={len(content)})"
                        )
                        break

            # v238.0: Degenerate response detection.
            # If the model produced only punctuation/whitespace (e.g., "..."),
            # retry once with MODERATE params to get a real answer.
            _stripped_check = re.sub(r'[\s\.\!\?\,\;\:…]+', '', content or '')
            if len(_stripped_check) == 0:
                logger.warning(
                    f"[QUERY] Degenerate response detected: '{content}' "
                    f"(level={params.complexity_level}). Retrying with MODERATE."
                )
                try:
                    retry_params = AdaptivePromptBuilder.build(
                        classified_query=None, is_fallback=False
                    )
                    retry_response = await router.generate(
                        prompt=command,
                        system_prompt=retry_params.system_prompt,
                        max_tokens=retry_params.max_tokens,
                        temperature=retry_params.temperature,
                        context=conversation_context if conversation_context else None,
                        deadline=deadline,  # v241.0
                    )
                    content = retry_response.content
                    logger.info(
                        f"[QUERY] Retry produced: '{(content or '')[:80]}' "
                        f"from {retry_response.source}"
                    )
                except Exception as retry_err:
                    logger.error(f"[QUERY] Retry failed: {retry_err}")
                    # Fall through with original degenerate content —
                    # client-side validation (Change 2) will suppress it

            return {
                "success": True,
                "response": content,
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
        app = get_main_app(strict=False)
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
    # v240.0: Math solver in fallback path too.
    math_response = _try_math_solve(command)
    if math_response is not None:
        return math_response

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

        # v238.0: Log degenerate response from fallback path
        _stripped_fb = re.sub(r'[\s\.\!\?\,\;\:…]+', '', content or '')
        if len(_stripped_fb) == 0:
            logger.warning(
                f"[QUERY] Degenerate fallback response: '{content}'"
            )

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

    v238.0: Note — degenerate detection is not applied to streaming responses
    because chunks arrive incrementally. Client-side validation provides
    defense-in-depth for the streaming path.

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

        # v240.0: Math solver for streaming path.
        math_response = _try_math_solve(command)
        if math_response is not None:
            yield math_response["response"]
            return

        # v237.0: Build kwargs with stop sequences if present
        gen_kwargs = {}
        if params.stop_sequences:
            gen_kwargs["stop"] = params.stop_sequences

        # v241.0: Task type hint for J-Prime model routing
        if classified_query:
            _task_type = _infer_task_type(command, params.complexity_level)
            gen_kwargs["task_type"] = _task_type
            gen_kwargs["complexity_level"] = params.complexity_level

        async for chunk in router.generate_stream(
            prompt=command,
            system_prompt=params.system_prompt,
            max_tokens=params.max_tokens,
            temperature=params.temperature,
            **gen_kwargs,
        ):
            yield chunk

    except Exception as e:
        logger.error(f"[QUERY] Stream error: {e}")
        yield f"Error: {str(e)}"
