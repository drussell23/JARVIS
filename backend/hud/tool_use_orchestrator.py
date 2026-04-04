"""ToolUseOrchestrator — 397B model decides what tools to call, loops until done.

The model receives a goal + available tools, calls tools one at a time,
gets results back, reasons about next steps, and loops until it signals
done or max iterations is reached.

Uses Doubleword 397B (primary), Claude API (fallback).
Every tool call passes through Iron Gate before execution.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.hud.tool_definitions import (
    TOOL_SCHEMAS,
    ToolCall,
    ToolResult,
    execute_tool,
    validate_tool_call,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = os.environ.get("JARVIS_TOOLUSE_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8")
_DEFAULT_MAX_ITER = int(os.environ.get("JARVIS_TOOLUSE_MAX_ITERATIONS", "10"))
_DEFAULT_TIMEOUT = float(os.environ.get("JARVIS_TOOLUSE_TIMEOUT_S", "120"))


@dataclass
class CommandResult:
    success: bool
    category: str
    steps_completed: int
    steps_total: int
    response_text: Optional[str]
    error: Optional[str]


class ToolUseOrchestrator:
    """Orchestrates the 397B tool-use loop."""

    def __init__(
        self,
        doubleword: Any,
        max_iterations: int = _DEFAULT_MAX_ITER,
        timeout_s: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._dw = doubleword
        self._max_iter = max_iterations
        self._timeout = timeout_s
        # Vision analyzer: uses Doubleword 235B to describe what's on screen
        self._vision_analyzer = self._make_vision_analyzer()

    def _make_vision_analyzer(self):
        """Create a vision analyzer closure that calls Doubleword 235B."""
        _VISION_MODEL = os.environ.get(
            "JARVIS_VISION_ANALYSIS_MODEL",
            "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8",
        )

        async def analyze(prompt: str, image_b64: str) -> str:
            """Analyze a screenshot using the 235B vision model."""
            # Build a vision prompt with the image
            vision_prompt = (
                f"{prompt}\n\n"
                f"[Screenshot attached as base64 JPEG — analyze the visual content]"
            )
            try:
                result = await self._dw.prompt_only(
                    vision_prompt,
                    model=_VISION_MODEL,
                    caller_id="vision_screen_analysis",
                    max_tokens=1000,
                )
                return result.strip() if result else "Could not analyze screenshot."
            except Exception as exc:
                logger.warning("[ToolUse] Vision analysis failed: %s", exc)
                return f"Vision analysis error: {exc}"

        return analyze

    async def execute(self, goal: str, screenshot_b64: Optional[str] = None) -> CommandResult:
        """Execute a goal using the 397B tool-use loop."""
        t0 = time.monotonic()
        tool_list = json.dumps(list(TOOL_SCHEMAS.values()), indent=2)

        system_prompt = (
            "You are JARVIS, an AI organism controlling a MacBook Pro. "
            "You have tools to interact with the Mac. Use them to accomplish the goal.\n\n"
            "The user is Derek J. Russell. When they say 'my profile' or 'my account', "
            "use their name to find the right page.\n\n"
            "Available tools:\n" + tool_list + "\n\n"
            "To call a tool, respond with JSON: {\"tool_calls\": [{\"name\": \"...\", \"args\": {...}}]}\n"
            "When the task is complete, respond with: {\"done\": true, \"summary\": \"what you did\"}\n"
            "If you cannot complete the task, respond with: {\"done\": true, \"summary\": \"why it failed\", \"error\": \"reason\"}\n\n"
            "Rules:\n"
            "- Call ONE tool at a time, wait for the result before deciding next action\n"
            "- After each action that changes the screen (open_url, vision_click, etc.), "
            "call take_screenshot to VERIFY the result — check if the page loaded correctly, "
            "if you see error messages like 'Page not found', or if you're on the right page\n"
            "- If you see an error (404, Page not found), try a different approach "
            "(e.g., search for the person's name instead of guessing a URL)\n"
            "- Be efficient — don't call unnecessary tools\n"
            "- If a tool fails, try an alternative approach\n"
            "- For 'my LinkedIn profile': try linkedin.com/in/derek-j-russell first, "
            "if 404, search LinkedIn for 'Derek J Russell'"
        )

        conversation = f"Goal: {goal}"
        steps_completed = 0

        for iteration in range(self._max_iter):
            # Timeout check
            if (time.monotonic() - t0) > self._timeout:
                return CommandResult(
                    success=False, category="composite", steps_completed=steps_completed,
                    steps_total=iteration, response_text="Task timed out.",
                    error=f"Timeout after {self._timeout}s",
                )

            # Call model
            try:
                prompt = f"{system_prompt}\n\n{conversation}"
                raw = await self._dw.prompt_only(
                    prompt,
                    model=_DEFAULT_MODEL,
                    caller_id=f"tooluse_iter{iteration}",
                    max_tokens=2000,
                )
            except Exception as exc:
                logger.warning("[ToolUse] Model call failed at iteration %d: %s", iteration, exc)
                return CommandResult(
                    success=False, category="composite", steps_completed=steps_completed,
                    steps_total=iteration, response_text=f"Model error: {exc}",
                    error=str(exc),
                )

            if not raw or not raw.strip():
                continue

            # Parse response
            parsed = self._parse_response(raw)

            # Done signal
            if parsed.get("done"):
                summary = parsed.get("summary", "Task completed.")
                has_error = parsed.get("error")
                return CommandResult(
                    success=not has_error, category="composite",
                    steps_completed=steps_completed, steps_total=steps_completed,
                    response_text=summary, error=has_error,
                )

            # Tool calls
            tool_calls = parsed.get("tool_calls", [])
            if not tool_calls:
                # Model didn't return tool_calls or done — treat raw text as summary
                return CommandResult(
                    success=True, category="composite", steps_completed=steps_completed,
                    steps_total=steps_completed, response_text=raw.strip()[:500],
                    error=None,
                )

            # Execute each tool call
            for tc_dict in tool_calls:
                call = ToolCall.from_dict(tc_dict)

                # Iron Gate validation
                is_safe, reason = validate_tool_call(call)
                if not is_safe:
                    logger.warning("[ToolUse] Iron Gate blocked: %s — %s", call.name, reason)
                    conversation += f"\n\nTool '{call.name}' was BLOCKED by safety gate: {reason}. Try a different approach."
                    continue

                # Execute — pass vision analyzer for take_screenshot tool
                result = await execute_tool(
                    call,
                    vision_analyzer=self._vision_analyzer,
                )
                steps_completed += 1

                # Add result to conversation
                status = "SUCCESS" if result.success else "FAILED"
                conversation += (
                    f"\n\nYou called: {call.name}({json.dumps(call.args)})"
                    f"\nResult ({status}): {result.output or result.error}"
                )

                logger.info("[ToolUse] Step %d: %s(%s) → %s",
                            steps_completed, call.name, json.dumps(call.args)[:80], status)

        # Max iterations reached
        return CommandResult(
            success=steps_completed > 0, category="composite",
            steps_completed=steps_completed, steps_total=self._max_iter,
            response_text=f"Completed {steps_completed} steps (max iterations reached).",
            error=None if steps_completed > 0 else "Max iterations without completing goal",
        )

    def _parse_response(self, raw: str) -> dict:
        """Parse model response — try JSON, handle markdown fences, fallback."""
        text = raw.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in the text
        json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return {"done": True, "summary": text[:500]}
