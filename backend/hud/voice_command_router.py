"""VoiceCommandRouter -- classifies voice commands and routes to the right executor.

Uses Doubleword 35B for fast intent classification, then dispatches to:
  - AppleScriptExecutor (app/navigation -- deterministic, no LLM)
  - VLAExecutor (vision actions -- JarvisCU pipeline)
  - ToolUseOrchestrator (composite -- 397B tool loop)
  - QueryExecutor (questions -- 35B response)

The Swift HUD sends raw commands here. This is the brain's front door.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from backend.hud.applescript_executor import AppleScriptExecutor
from backend.hud.query_executor import QueryExecutor
from backend.hud.tool_use_orchestrator import CommandResult, ToolUseOrchestrator

logger = logging.getLogger(__name__)

_CLASSIFIER_MODEL = os.environ.get("JARVIS_VOICE_ROUTER_MODEL", "Qwen/Qwen3.5-35B-A3B-FP8")

_CLASSIFY_PROMPT = """Given this voice command, classify the intent. Return ONLY a JSON object.

Command: "{command}"

Categories:
- "app_action": open/close/switch/launch an application (e.g., "open chrome", "close Safari", "launch Spotify")
- "navigation": go to a website/URL (e.g., "go to LinkedIn", "open google.com", "search YouTube for music")
- "vision_action": interact with something visible on screen that requires seeing it (e.g., "click the send button", "scroll down", "select the text")
- "composite": multi-step task combining multiple actions (e.g., "open chrome and go to LinkedIn", "send a message on WhatsApp saying hello")
- "code_action": modify code, fix bugs, system development tasks (e.g., "fix the parser bug", "refactor the login module")
- "query": answer a question, provide information, no action needed (e.g., "what time is it", "what's on my screen", "how does the vision loop work")

Return: {{"category": "...", "needs_vision": true/false, "needs_tools": true/false}}"""


class VoiceCommandRouter:
    """Routes voice commands through Ouroboros for intelligent execution."""

    def __init__(self, doubleword: Any, narrate_fn: Optional[Any] = None) -> None:
        self._dw = doubleword
        self._narrate_fn = narrate_fn
        self._applescript = AppleScriptExecutor()
        self._query = QueryExecutor(doubleword)
        self._tool_orchestrator = ToolUseOrchestrator(doubleword, narrate_fn=narrate_fn)

    async def route(self, command: str, screenshot_b64: Optional[str] = None) -> CommandResult:
        """Classify and route a voice command."""
        logger.info("[VoiceRouter] Command: %s", command[:100])

        # Step 1: Classify intent via 35B
        classification = await self._classify(command)
        category = classification.get("category", "composite")
        needs_vision = classification.get("needs_vision", False)
        needs_tools = classification.get("needs_tools", False)

        logger.info("[VoiceRouter] Classified: %s (vision=%s, tools=%s)", category, needs_vision, needs_tools)

        # Step 2: Route to executor
        if category == "app_action":
            return await self._execute_app_action(command)

        if category == "navigation":
            return await self._execute_navigation(command)

        if category == "query":
            return await self._execute_query(command, screenshot_b64)

        if category == "vision_action":
            return await self._execute_vision(command, screenshot_b64)

        if category == "code_action":
            return await self._execute_code_action(command)

        # composite or unknown -> tool-use loop (397B)
        return await self._tool_orchestrator.execute(command, screenshot_b64)

    async def _classify(self, command: str) -> dict:
        """Classify intent via Doubleword 35B."""
        try:
            prompt = _CLASSIFY_PROMPT.format(command=command)
            raw = await self._dw.prompt_only(
                prompt,
                model=_CLASSIFIER_MODEL,
                caller_id="voice_classifier",
                max_tokens=200,
            )
            if not raw:
                return {"category": "composite", "needs_vision": False, "needs_tools": True}

            # Parse JSON
            text = raw.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                json_match = re.search(r'\{[^{}]*\}', text)
                if json_match:
                    return json.loads(json_match.group())

        except Exception as exc:
            logger.warning("[VoiceRouter] Classification failed: %s -- falling back to composite", exc)

        return {"category": "composite", "needs_vision": False, "needs_tools": True}

    async def _execute_app_action(self, command: str) -> CommandResult:
        """Extract app name and open/close it."""
        lower = command.lower()
        # Extract app name from command
        app_match = re.search(
            r"(?:open|launch|start|close|quit)\s+(?:the\s+)?(.+?)(?:\s+app)?$",
            lower,
            re.IGNORECASE,
        )
        app_name = app_match.group(1).strip() if app_match else command

        if "close" in lower or "quit" in lower:
            result = await self._applescript.run_script(
                f'tell application "{self._applescript.discover_app(app_name)}" to quit'
            )
            return CommandResult(
                success=result.success,
                category="app_action",
                steps_completed=1,
                steps_total=1,
                response_text=f"Closed {app_name}." if result.success else f"Couldn't close {app_name}.",
                error=result.error,
            )

        result = await self._applescript.open_app(app_name)
        return CommandResult(
            success=result.success,
            category="app_action",
            steps_completed=1,
            steps_total=1,
            response_text=result.output if result.success else f"Couldn't open {app_name}.",
            error=result.error,
        )

    async def _execute_navigation(self, command: str) -> CommandResult:
        """Extract URL/site and navigate to it."""
        lower = command.lower()
        # Remove "go to", "navigate to", "open" prefix
        site = re.sub(r"^(go\s+to|navigate\s+to|open)\s+", "", lower).strip()
        url = self._applescript.infer_url(site)
        result = await self._applescript.open_url(url)
        return CommandResult(
            success=result.success,
            category="navigation",
            steps_completed=1,
            steps_total=1,
            response_text=result.output if result.success else f"Couldn't navigate to {site}.",
            error=result.error,
        )

    async def _execute_query(self, command: str, screenshot_b64: Optional[str]) -> CommandResult:
        """Answer a question via LLM."""
        answer = await self._query.answer(command)
        return CommandResult(
            success=True,
            category="query",
            steps_completed=1,
            steps_total=1,
            response_text=answer,
            error=None,
        )

    async def _execute_vision(self, command: str, screenshot_b64: Optional[str]) -> CommandResult:
        """Dispatch to VLA pipeline (JarvisCU) for vision-dependent actions."""
        try:
            from backend.vision.jarvis_cu import JarvisCU
            import numpy as np
            from PIL import Image
            import base64
            import io

            cu = JarvisCU()
            frame = None
            if screenshot_b64:
                img = Image.open(io.BytesIO(base64.b64decode(screenshot_b64)))
                frame = np.array(img.convert("RGB"))

            result = await cu.run(command, initial_frame=frame)
            success = result.get("success", False)
            steps = result.get("steps_completed", 0)
            total = result.get("steps_total", 0)
            error = result.get("error")

            return CommandResult(
                success=success,
                category="vision_action",
                steps_completed=steps,
                steps_total=total,
                response_text=f"Completed {steps}/{total} steps." if success else f"Vision task failed: {error}",
                error=error,
            )
        except Exception as exc:
            return CommandResult(
                success=False,
                category="vision_action",
                steps_completed=0,
                steps_total=0,
                response_text=f"Vision system error: {exc}",
                error=str(exc),
            )

    async def _execute_code_action(self, command: str) -> CommandResult:
        """Route to Ouroboros governance pipeline for code tasks."""
        # For now, route through the tool-use orchestrator which can use bash tools
        # Full GovernedLoopService integration is a future enhancement
        return await self._tool_orchestrator.execute(command)
