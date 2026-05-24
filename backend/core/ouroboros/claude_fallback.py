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
        # Slice 2B-ii — route through Aegis Provider Bridge (transport
        # swap + per-call lease when JARVIS_AEGIS_ENABLED; byte-identical
        # legacy AsyncAnthropic when disabled).
        from backend.core.ouroboros.governance.aegis_provider_bridge import (
            acquire_call_lease as _aegis_acquire_call_lease,
            make_async_anthropic_client as _aegis_make_anthropic,
            merge_lease_header as _aegis_merge_lease,
        )

        client = _aegis_make_anthropic(api_key=api_key)

        messages = [{"role": "user", "content": prompt}]

        # Per-call Aegis lease (synthetic op_id — this entry point
        # predates the OperationContext threading).
        _aegis_lease = await _aegis_acquire_call_lease(
            op_id=f"claude-fallback:{caller_id}",
            route="standard",
            estimated_cost_usd=0.01,
        )
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_MESSAGE,
            messages=messages,
            extra_headers=_aegis_merge_lease(None, _aegis_lease),
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
