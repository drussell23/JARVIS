"""Claude API fallback for Ouroboros cognitive layer.

Used when Doubleword 397B is unavailable (no API key, timeout, API error).
Same prompt structure, async call via the ``anthropic`` SDK.

Design principles
-----------------
- Zero hardcoding: model, max_tokens, and system message are parameterised.
- Graceful degradation: returns ``None`` on any failure so callers can
  fall back further or proceed with Tier 0 only.
- Async-native: uses ``anthropic.AsyncAnthropic`` for non-blocking calls.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Default model used when caller does not specify one.
_DEFAULT_MODEL: str = "claude-sonnet-4-20250514"

# System message priming the model for JSON-structured code analysis.
_SYSTEM_MESSAGE: str = (
    "You are a code analysis assistant for the Trinity AI ecosystem. "
    "Return valid JSON matching the requested schema. No markdown wrapping."
)


async def claude_inference(
    prompt: str,
    caller_id: str = "ouroboros_fallback",
    response_format: Optional[dict] = None,
    max_tokens: int = 8000,
    model: str = _DEFAULT_MODEL,
) -> Optional[str]:
    """Call Claude API as fallback inference.

    Parameters
    ----------
    prompt:
        The full prompt text to send as the user message.
    caller_id:
        Logical identifier of the calling subsystem (for logging).
    response_format:
        Reserved for future use (JSON schema hint). Currently unused by the
        Anthropic messages API but accepted for call-site symmetry with the
        Doubleword ``prompt_only()`` signature.
    max_tokens:
        Maximum number of tokens in the response.
    model:
        Anthropic model identifier.

    Returns
    -------
    Optional[str]
        The response text, or ``None`` if the call failed for any reason.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("[claude_fallback] ANTHROPIC_API_KEY not set — skipping")
        return None

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)

        messages = [{"role": "user", "content": prompt}]

        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_MESSAGE,
            messages=messages,
        )

        # Extract text content from response blocks.
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        if not text:
            logger.warning(
                "[claude_fallback] Empty response from Claude (caller=%s)", caller_id
            )
            return None

        logger.info(
            "[claude_fallback] Claude response received (%d chars, caller=%s)",
            len(text),
            caller_id,
        )
        return text

    except Exception as exc:
        logger.warning("[claude_fallback] Claude API call failed: %s", exc)
        return None
