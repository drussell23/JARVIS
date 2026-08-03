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


def _humanise(capability: str) -> str:
    """A capability name as a person would say it. NEVER raises.

    `video.start_streaming` → "start streaming". Derived from the name rather
    than looked up, for the same reason the reflex derives its lexicon: a
    table of spoken forms is a table that goes stale the first time somebody
    adds a capability without reading this file.
    """
    try:
        bare = str(capability or "").split(".")[-1]
        return " ".join(bare.replace("_", " ").split()) or "that"
    except Exception:  # noqa: BLE001
        return "that"


def _read_controller_result(result: Any, human: str) -> tuple:
    """(succeeded, what to say) from whatever a capability returned.

    NEVER raises. `MacOSController` methods answer `(bool, str)` and the
    string is already a sentence written for a person — "🔒 Locking the screen
    now, Derek. See you soon." Synthesising our own narration on top of that
    would be a second voice for the same event, and the two would eventually
    disagree about what happened.
    """
    try:
        if isinstance(result, tuple) and len(result) == 2:
            ok, msg = result
            text = str(msg or "").strip()
            if isinstance(ok, bool):
                return ok, (text or (f"Done — {human}." if ok
                                     else f"I couldn't {human}."))
        if isinstance(result, str) and result.strip():
            return True, result.strip()[:400]
        if result is False:
            return False, f"I couldn't {human}."
        return True, f"Done — {human}."
    except Exception:  # noqa: BLE001
        return True, f"Done — {human}."


def _why_the_model_could_not_answer(detail: str) -> str:
    """Turn a provider fault into a sentence. NEVER raises.

    The operator asked for their screen to be locked and JARVIS said, out
    loud, through a speech synthesiser:

        "Model error: session_budget_preflight_refused:soak_circuit_tripped:
         soak_cost_cap_exceeded:$238111.9488>=$2.0000:on_boot: provider=
         doubleword est=$0.1000 > session_remaining=$0.0000"

    Absolute observability means every autonomous decision is VISIBLE, and a
    diagnostic read aloud to a person is not visibility — it is the log
    escaping onto the one surface that cannot skim. The full string stays in
    `error` for the log; this is what gets spoken.

    Classified by SHAPE rather than by matching a provider's exact wording, so
    a reworded refusal from a new provider still lands in the right sentence.
    """
    try:
        d = (detail or "").lower()
        if "budget" in d or "cost_cap" in d or "circuit" in d or "spend" in d:
            return ("I've hit my own spending limit, so I can't think that "
                    "one through right now.")
        if ("403" in d or "entitlement" in d or "unauthor" in d
                or "api key" in d or "credential" in d):
            return "I'm not authorised to reach my language model right now."
        if "timeout" in d or "timed out" in d:
            return "My language model took too long to answer."
        if ("connect" in d or "unreachable" in d or "network" in d
                or "outage" in d or "502" in d or "503" in d):
            return "I can't reach my language model right now."
        return "My language model isn't answering right now."
    except Exception:  # noqa: BLE001
        return "My language model isn't answering right now."


def _near_miss_offer(reflex: Any) -> str:
    """What the reflex ALMOST understood, phrased as an offer. "" if nothing.

    NEVER raises. When the cortex is unreachable, "I don't know" and "I think
    you meant lock screen but I wasn't sure enough to act" are very different
    answers, and the second one lets the operator fix it in one word. The
    reflex still does not act on it — this only says what it saw.
    """
    try:
        if reflex is None or getattr(reflex, "resolved", False):
            return ""
        cap = str(getattr(reflex, "capability", "") or "")
        outcome = str(getattr(reflex, "outcome", "") or "")
        if not cap:
            return ""
        if outcome in ("low_confidence", "ambiguous"):
            return f" Did you mean {_humanise(cap)}?"
        if outcome == "needs_args":
            return (f" I can {_humanise(cap)}, but I need you to tell me "
                    f"what to run it on.")
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _make_failure_speakable(result: Any, reflex: Any) -> Any:
    """Rewrite a failed result's spoken half. NEVER raises.

    Applied HERE rather than inside `ToolUseOrchestrator` on purpose: the
    orchestrator has other callers and its `error` string is a diagnostic that
    logs and tests depend on. Only this path ends at a speech synthesiser, so
    only this path translates. `error` is left exactly as it was.
    """
    try:
        if result is None or getattr(result, "success", False):
            return result
        if getattr(result, "pending", False):
            return result
        # NEVER touch a result the reflex produced. Measured: the capability
        # path answered "I couldn't ask anyone to approve lock screen, so I
        # didn't do it" — true, specific, and actionable — and this function
        # saw the word "provider" in `no approval provider available` and
        # replaced it with "My language model isn't answering right now",
        # which is a plain falsehood; no model was involved in that failure at
        # all. A translator that can overwrite a true sentence with a false
        # one is worse than the diagnostic it was written to replace.
        #
        # The gate is the PATH, not a better keyword list: the capability path
        # already writes sentences for people, so it needs no translation, and
        # anything this function did to one could only be damage.
        if reflex is not None and getattr(reflex, "resolved", False):
            return result
        detail = str(getattr(result, "error", "")
                     or getattr(result, "response_text", "") or "")
        # Only rewrite when the failure was the MODEL being unavailable. A
        # genuine task failure ("couldn't find Chrome") is already a sentence
        # a person can act on, and replacing it would lose the only useful
        # thing in it.
        markers = ("model error", "preflight", "provider", "circuit", "budget",
                   "timeout", "connect", "unauthor", "403", "502", "503",
                   "entitlement", "outage")
        if not any(m in detail.lower() for m in markers):
            return result
        result.response_text = (_why_the_model_could_not_answer(detail)
                                + _near_miss_offer(reflex))
        logger.warning("[VoiceRouter] model path unavailable — spoken: %s "
                       "| detail: %s", result.response_text, detail[:200])
        return result
    except Exception:  # noqa: BLE001
        return result


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
        reflex = None
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

        # Step 0: THE REFLEX. Deterministic, ~0.1ms, zero LLM, zero I/O.
        #
        # This runs BEFORE classification rather than as its fallback, and the
        # order is the whole point. As a fallback it would only ever run after
        # a model call had already been attempted — so a sentence naming a
        # capability outright would still cost two round-trips when the model
        # is up, and would still be at the mercy of the model being up at all.
        #
        # It fires only when it is certain, and every uncertain outcome falls
        # through to exactly the path that ran before, so nothing it declines
        # is lost. See `capability_reflex` for the four separate refusals.
        reflex = self._reflex(command)
        if reflex is not None and reflex.resolved:
            logger.info("[VoiceRouter] REFLEX resolved '%s' -> %s in %.2fms "
                        "(no model call) — %s", command[:60],
                        reflex.capability, reflex.elapsed_ms, reflex.reason)

        # Step 1: Classify intent via 35B — PURE, so a resume can reuse it.
        classification = None
        if reflex is not None and reflex.resolved:
            # Skip the classifier entirely. Asking a model to categorise a
            # sentence we have already resolved to a named capability is a
            # round-trip whose answer we would discard.
            classification = {"category": "system_action",
                              "needs_vision": False, "needs_tools": False}
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
            if reflex is not None and reflex.resolved:
                result = await self._execute_capability(reflex)
            elif category == "app_action":
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
            return _make_failure_speakable(result, reflex)
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

    # ── The reflex arc ──────────────────────────────────────────────────

    def _reflex(self, command: str) -> Optional[Any]:
        """Resolve the sentence to a named capability, or None. NEVER raises.

        Wrapped rather than called inline so that a fault in the reflex costs
        the model round-trip it was meant to save, and nothing else. A reflex
        that can take the voice path down with it is worse than no reflex.
        """
        try:
            from backend.system_control.capability_reflex import (
                get_capability_reflex,
            )
            out = get_capability_reflex().resolve(command)
            if not out.resolved:
                logger.debug("[VoiceRouter] reflex declined (%s): %s",
                             out.outcome, out.reason)
            return out
        except Exception:  # noqa: BLE001
            logger.debug("[VoiceRouter] reflex unavailable", exc_info=True)
            return None

    async def _execute_capability(self, reflex: Any) -> CommandResult:
        """Run a reflex-resolved capability through the consent boundary.

        Goes through `CapabilityRouter.route`, never around it. The reflex
        decided WHICH capability; the registry still decides whether it needs
        the Iron Gate, the router still mints the nonce and suspends, and the
        operator still sees the Touch ID prompt. Arriving at the gate without
        a model does not mean arriving past it.

        Called with NO arguments, always — `capability_reflex` refuses to
        resolve anything whose signature has a required parameter, so an empty
        call here is a guarantee rather than an omission.
        """
        try:
            from backend.system_control.capability_router import (
                Outcome, get_capability_router,
            )
            routed = await get_capability_router().route(
                reflex.capability, {}, op_id=f"reflex:{reflex.capability}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[VoiceRouter] capability route failed: %s", exc)
            return CommandResult(
                success=False, category="system_action", steps_completed=0,
                steps_total=1, error=str(exc),
                response_text=f"I couldn't run {_humanise(reflex.capability)}.")

        human = _humanise(reflex.capability)
        outcome = routed.outcome

        if outcome == Outcome.EXECUTED.value:
            # HONOUR THE RETURN VALUE. `_execute` reports EXECUTED when the
            # call did not RAISE — but `lock_screen` answers
            # `(False, "Unable to lock screen (all methods failed)")` without
            # raising anything. Reading only the router's outcome would report
            # a locked screen to an operator looking at an unlocked one, which
            # is the one failure mode worse than not acting.
            ok, spoken = _read_controller_result(routed.result, human)
            return CommandResult(
                success=ok, category="system_action",
                steps_completed=1 if ok else 0, steps_total=1,
                response_text=spoken, error=None if ok else spoken)

        if outcome == Outcome.SUSPENDED.value:
            logger.info("[VoiceRouter] '%s' awaiting operator consent "
                        "(request=%s)", reflex.capability,
                        (routed.request_id or "")[:12])
            return CommandResult(
                success=False, pending=True, category="system_action",
                steps_completed=0, steps_total=1, error=None,
                response_text=f"I need your approval to {human}. "
                              f"Check the prompt on screen.")

        if outcome == Outcome.DENIED.value:
            # Two very different situations wearing one outcome, and an
            # operator needs opposite follow-ups: they said no, or nothing
            # could ask them. The router already writes the distinction into
            # `detail`; dropping it here would make a disconnected HUD look
            # like a refusal the operator does not remember giving.
            unasked = "no approval provider" in (routed.detail or "").lower()
            return CommandResult(
                success=False, category="system_action", steps_completed=0,
                steps_total=1, error=routed.detail or "denied",
                response_text=(
                    f"I couldn't ask anyone to approve {human}, so I didn't "
                    f"do it." if unasked
                    else f"You declined {human}, so I left it alone."))

        if outcome == Outcome.EXPIRED.value:
            return CommandResult(
                success=False, category="system_action", steps_completed=0,
                steps_total=1, error=routed.detail or "expired",
                response_text=f"The approval for {human} timed out.")

        return CommandResult(
            success=False, category="system_action", steps_completed=0,
            steps_total=1, error=routed.detail or outcome,
            response_text=f"I couldn't {human}: {routed.detail or outcome}.")

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
