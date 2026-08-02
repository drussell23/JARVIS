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

    async def route(self, command: str, screenshot_b64: Optional[str] = None,
                    intent_id: Optional[str] = None) -> CommandResult:
        """Classify and route a voice command.

        WRITE-AHEAD. The raw command is journalled before anything executes, so
        a process death mid-flight leaves the operator's sentence recoverable
        rather than gone. Nothing else in the HUD path checkpoints — the
        Ouroboros FSM's `.ouroboros/checkpoints` resume machinery covers the
        governed loop, which `route()` never enters.

        Classification is journalled as a PURE node: deterministic given the
        same command, no outside effect, so a resumed intent reuses the recorded
        category instead of paying the model again. Execution deliberately is
        NOT — see `intent_journal`: the CU steps are type/click/drag/scroll,
        and replaying one does not recompute a value, it sends a second
        message.
        """
        logger.info("[VoiceRouter] Command: %s", command[:100])
        _journal = None
        try:
            from backend.hud.intent_journal import (
                NodeKind, get_intent_journal,
            )
            _journal = get_intent_journal()
            if intent_id is None:
                intent_id = await _journal.open_intent(
                    command, payload={"has_screenshot": bool(screenshot_b64)})
        except Exception:  # noqa: BLE001 — a journal never blocks a command
            _journal = None

        # Step 1: Classify intent via 35B — PURE, so a resume can reuse it.
        classification = None
        if _journal is not None and intent_id:
            try:
                _v = _journal.resume_plan(intent_id).for_node("classify")
                if _v.result is not None and _v.action.value == "skip":
                    classification = _v.result
                    logger.info("[VoiceRouter] reusing journalled "
                                "classification (no model call)")
            except Exception:  # noqa: BLE001
                classification = None
        if classification is None:
            if _journal is not None and intent_id:
                await _journal.node_started(intent_id, "classify", NodeKind.PURE)
            classification = await self._classify(command)
            if _journal is not None and intent_id:
                await _journal.node_completed(
                    intent_id, "classify", classification, NodeKind.PURE)
        category = classification.get("category", "composite")
        needs_vision = classification.get("needs_vision", False)
        needs_tools = classification.get("needs_tools", False)

        logger.info("[VoiceRouter] Classified: %s (vision=%s, tools=%s)", category, needs_vision, needs_tools)

        # Step 2: Route to executor.
        #
        # The dispatch is EFFECTFUL — every branch below can touch the world —
        # so the journal records that it began and how it ended, and never
        # offers to replay it. An interrupted dispatch resolves to CONFIRM, not
        # to a silent second attempt.
        if _journal is not None and intent_id:
            try:
                await _journal.node_started(intent_id, "dispatch",
                                            NodeKind.EFFECTFUL)
            except Exception:  # noqa: BLE001
                pass
        result: Optional[CommandResult] = None
        try:
            if category == "app_action":
                result = await self._execute_app_action(command)
            elif category == "navigation":
                result = await self._execute_navigation(command)
            elif category == "query":
                result = await self._execute_query(command, screenshot_b64)
            elif category == "vision_action":
                result = await self._execute_vision(command, screenshot_b64)
            elif category == "code_action":
                result = await self._execute_code_action(command)
            else:
                # composite or unknown -> tool-use loop (397B)
                result = await self._tool_orchestrator.execute(
                    command, screenshot_b64)
            return result
        except Exception as exc:  # noqa: BLE001 — record, then propagate
            if _journal is not None and intent_id:
                try:
                    # Record the failure and leave the intent OPEN.
                    #
                    # Closing here would be the natural-looking mistake: an
                    # intent is closed when we have stopped caring about it,
                    # and `unfinished()` is precisely the replay queue. A
                    # first-attempt timeout that closed its own intent would
                    # be unrecoverable — measured, when the end-to-end proof
                    # reported `unfinished intents: 0` after a crash.
                    # Retention bounds how long it stays open.
                    await _journal.node_failed(intent_id, "dispatch",
                                               repr(exc), NodeKind.EFFECTFUL)
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            if _journal is not None and intent_id and result is not None:
                try:
                    await _journal.node_completed(intent_id, "dispatch", None,
                                                  NodeKind.EFFECTFUL)
                    await _journal.close_intent(
                        intent_id, success=bool(getattr(result, "success", True)))
                except Exception:  # noqa: BLE001
                    pass

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
