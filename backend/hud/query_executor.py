"""QueryExecutor — answers questions via Doubleword 35B without taking action."""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class QueryExecutor:
    """Answers user questions via LLM. No actions, no side effects."""

    def __init__(self, doubleword: Any) -> None:
        self._dw = doubleword

    async def answer(self, question: str, screenshot_description: Optional[str] = None) -> str:
        """Answer a question using Doubleword 35B."""
        prompt = f"Answer this question concisely:\n\n{question}"
        if screenshot_description:
            prompt += f"\n\nContext (what's on screen): {screenshot_description}"

        try:
            response = await self._dw.prompt_only(
                prompt,
                model="Qwen/Qwen3.5-35B-A3B-FP8",
                caller_id="voice_query",
                max_tokens=500,
            )
            return response.strip() if response else "I don't have an answer for that."
        except Exception as exc:
            logger.warning("[QueryExecutor] Failed: %s", exc)
            return "Sorry, I couldn't process that question."
