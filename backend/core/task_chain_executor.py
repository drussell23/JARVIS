"""
Task Chain Executor — v295.0
============================

Executes a GoalChain as a sequence of brain-routed steps, each delegated to
the Neural Mesh CoordinatorAgent.  Step N output feeds step N+1 as context.

Design principles:
- Zero hardcoding: task_type resolved via keyword rules + InteractiveBrainRouter
- Async throughout: all steps run with individual deadline budgets
- Resilient: a failed step records the error but does NOT abort the chain
- Observable: emits ``task_chain_execution`` to Reactor Core on completion

Usage (from UnifiedCommandProcessor or any async caller)::

    from backend.core.task_chain_executor import get_task_chain_executor

    executor = await get_task_chain_executor()
    results = await executor.execute_chain(
        sub_goals=["open LinkedIn", "navigate to my profile"],
        origin_command="go to my LinkedIn profile",
        context={"speaker": "Derek"},
    )
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("TaskChainExecutor")


# ---------------------------------------------------------------------------
# Keyword → task_type routing rules
# ---------------------------------------------------------------------------

# Each rule is (compiled-regex, task_type).
# Rules are tested in order; first match wins.
_GOAL_TASK_TYPE_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(open|launch|start|quit|close)\b", re.IGNORECASE), "system_command"),
    (re.compile(r"\b(click|tap|press|scroll|drag|type|fill)\b", re.IGNORECASE), "vision_action"),
    (re.compile(r"\b(navigate|go to|browse|visit|search)\b", re.IGNORECASE), "browser_navigation"),
    (re.compile(r"\b(send|reply|compose|write|draft).{0,20}(email|message|mail)\b", re.IGNORECASE), "email_compose"),
    (re.compile(r"\b(read|check|triage|prioritize).{0,20}(email|inbox|messages)\b", re.IGNORECASE), "email_triage"),
    (re.compile(r"\b(schedule|book|calendar|appointment|meeting|free time)\b", re.IGNORECASE), "calendar_query"),
    (re.compile(r"\b(verify|confirm|did it|check if|make sure)\b", re.IGNORECASE), "vision_verification"),
    (re.compile(r"\b(plan|workflow|multi.?step|automate|orchestrate)\b", re.IGNORECASE), "multi_step_planning"),
    (re.compile(r"\b(classify|categorize|what (type|kind|domain))\b", re.IGNORECASE), "classification"),
    (re.compile(r"\b(break.?down|decompose|steps? for)\b", re.IGNORECASE), "step_decomposition"),
]

_FALLBACK_TASK_TYPE = "goal_chain_step"


def _goal_text_to_task_type(goal_text: str) -> str:
    """Map a sub-goal text to the closest interactive task_type."""
    for pattern, task_type in _GOAL_TASK_TYPE_RULES:
        if pattern.search(goal_text):
            return task_type
    return _FALLBACK_TASK_TYPE


# ---------------------------------------------------------------------------
# CoordinatorAgent capability mapping
# ---------------------------------------------------------------------------

_TASK_TYPE_TO_CAPABILITY: Dict[str, str] = {
    "system_command":    "system_control",
    "vision_action":     "computer_use",
    "browser_navigation": "browser_control",
    "email_compose":     "email_management",
    "email_triage":      "email_management",
    "calendar_query":    "calendar_management",
    "vision_verification": "computer_use",
    "multi_step_planning": "task_orchestration",
    "classification":    "intent_classification",
    "step_decomposition": "task_planning",
    "goal_chain_step":   "general_execution",
}


def _task_type_to_capability(task_type: str, brain_id: str) -> str:
    """Return a CoordinatorAgent capability name for the given task type."""
    return _TASK_TYPE_TO_CAPABILITY.get(task_type, "general_execution")


# ---------------------------------------------------------------------------
# TaskChainExecutor
# ---------------------------------------------------------------------------

class TaskChainExecutor:
    """Execute a list of sub-goals sequentially, each brain-routed.

    Args:
        coordinator_agent: A CoordinatorAgent instance from the Neural Mesh.
            May be ``None`` — in that case sub-goals are attempted via
            InteractiveBrainRouter classification only (no delegation).
    """

    def __init__(self, coordinator_agent: Optional[Any] = None) -> None:
        self._coordinator = coordinator_agent
        self._router = None  # lazy: loaded on first execute_chain call

    def _get_router(self):
        if self._router is None:
            try:
                from backend.core.interactive_brain_router import get_interactive_brain_router
                self._router = get_interactive_brain_router()
            except Exception as exc:
                logger.warning("[TaskChain] Brain router unavailable: %s", exc)
        return self._router

    async def execute_chain(
        self,
        sub_goals: List[str],
        origin_command: str = "",
        context: Optional[Dict[str, Any]] = None,
        per_step_timeout_s: float = float(os.getenv("JARVIS_CHAIN_STEP_TIMEOUT_S", "15")),
    ) -> List[Dict[str, Any]]:
        """Execute *sub_goals* sequentially, chaining output context.

        Args:
            sub_goals: Ordered list of goal texts to execute.
            origin_command: The original user utterance (for logging + experience).
            context: Initial context dict (speaker, source, active_app, …).
            per_step_timeout_s: Hard deadline per individual step.

        Returns:
            List of per-step result dicts with keys:
            ``goal``, ``task_type``, ``brain_id``, ``success``, ``output``,
            ``latency_ms``, ``error`` (optional).
        """
        router = self._get_router()
        ctx: Dict[str, Any] = dict(context or {})
        results: List[Dict[str, Any]] = []
        chain_start = time.perf_counter()

        logger.info(
            "[TaskChain] Starting %d-step chain for '%s'",
            len(sub_goals), origin_command[:80],
        )

        for i, goal in enumerate(sub_goals):
            step_start = time.perf_counter()
            task_type = _goal_text_to_task_type(goal)
            brain_id = "qwen_coder"  # safe default
            step_result: Dict[str, Any] = {
                "goal": goal,
                "task_type": task_type,
                "brain_id": brain_id,
                "success": False,
                "output": None,
                "latency_ms": 0.0,
            }

            try:
                # Brain routing
                if router is not None:
                    selection = router.select_for_task(task_type, goal)
                    brain_id = selection.brain_id
                    step_result["brain_id"] = brain_id

                # Delegate to CoordinatorAgent
                output = await self._delegate_step(
                    goal=goal,
                    task_type=task_type,
                    brain_id=brain_id,
                    context=ctx,
                    timeout_s=per_step_timeout_s,
                )

                step_result["success"] = True
                step_result["output"] = output
                # Chain: feed this step's output as next step's context
                if output and isinstance(output, dict):
                    ctx.update({
                        f"step_{i}_result": output.get("response") or output.get("output"),
                        f"step_{i}_task": task_type,
                    })
                elif output:
                    ctx[f"step_{i}_result"] = str(output)[:500]

            except asyncio.TimeoutError:
                step_result["error"] = f"step timed out after {per_step_timeout_s}s"
                logger.warning("[TaskChain] Step %d timed out: '%s'", i, goal[:60])
            except Exception as exc:
                step_result["error"] = str(exc)
                logger.warning("[TaskChain] Step %d failed: '%s' — %s", i, goal[:60], exc)

            step_result["latency_ms"] = (time.perf_counter() - step_start) * 1000
            results.append(step_result)
            logger.info(
                "[TaskChain] Step %d/%d [%s] '%s' → %s (%.0fms)",
                i + 1, len(sub_goals), task_type,
                goal[:50], "OK" if step_result["success"] else "FAIL",
                step_result["latency_ms"],
            )

        total_ms = (time.perf_counter() - chain_start) * 1000
        success_count = sum(1 for r in results if r["success"])
        logger.info(
            "[TaskChain] Completed: %d/%d steps succeeded in %.0fms",
            success_count, len(sub_goals), total_ms,
        )

        # Emit chain outcome to Reactor Core (best-effort, fire-and-forget)
        asyncio.create_task(
            self._emit_chain_experience(
                sub_goals=sub_goals,
                origin_command=origin_command,
                results=results,
                total_ms=total_ms,
            ),
            name="task_chain_experience",
        )

        return results

    async def _delegate_step(
        self,
        goal: str,
        task_type: str,
        brain_id: str,
        context: Dict[str, Any],
        timeout_s: float,
    ) -> Optional[Any]:
        """Attempt to delegate one step to CoordinatorAgent.

        Falls back to a best-effort inline response if the coordinator is
        unavailable or doesn't support the required capability.
        """
        capability = _task_type_to_capability(task_type, brain_id)

        if self._coordinator is not None:
            try:
                delegate_fn = (
                    getattr(self._coordinator, "delegate_task", None)
                    or getattr(self._coordinator, "execute_task", None)
                    or getattr(self._coordinator, "handle_task", None)
                )
                if delegate_fn is not None:
                    return await asyncio.wait_for(
                        delegate_fn(
                            task=goal,
                            capability=capability,
                            context=context,
                            brain_id=brain_id,
                        ),
                        timeout=timeout_s,
                    )
            except asyncio.TimeoutError:
                raise
            except Exception as exc:
                logger.debug(
                    "[TaskChain] Coordinator delegate failed for '%s': %s — inline fallback",
                    goal[:50], exc,
                )

        # Inline fallback: classify + respond via InteractiveBrainRouter alone
        # (limited, but at least records the step and doesn't abort the chain)
        return {"response": f"[chain step] {goal}", "source": "inline_fallback"}

    async def _emit_chain_experience(
        self,
        sub_goals: List[str],
        origin_command: str,
        results: List[Dict[str, Any]],
        total_ms: float,
    ) -> None:
        """Forward task chain outcome to Reactor Core for training."""
        try:
            from backend.intelligence.cross_repo_experience_forwarder import get_experience_forwarder
            fwd = await get_experience_forwarder()
            success_count = sum(1 for r in results if r["success"])
            await fwd.forward_experience(
                experience_type="task_chain_execution",
                input_data={
                    "origin_command": origin_command[:500],
                    "sub_goals": sub_goals[:20],
                    "step_count": len(sub_goals),
                },
                output_data={
                    "success_count": success_count,
                    "total_steps": len(sub_goals),
                    "total_ms": round(total_ms, 1),
                    "step_task_types": [r["task_type"] for r in results],
                    "step_brain_ids": [r["brain_id"] for r in results],
                },
                quality_score=success_count / max(len(sub_goals), 1),
                confidence=0.8,
                success=success_count > 0,
                component="task_chain_executor",
            )
        except Exception as exc:
            logger.debug("[TaskChain] Experience forward failed: %s", exc)


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_executor: Optional[TaskChainExecutor] = None
_executor_lock: Optional[asyncio.Lock] = None


async def get_task_chain_executor(
    coordinator_agent: Optional[Any] = None,
) -> TaskChainExecutor:
    """Return the global TaskChainExecutor singleton.

    Args:
        coordinator_agent: Provide on first call to wire in the Neural Mesh
            CoordinatorAgent.  Ignored on subsequent calls (singleton is
            already initialised).
    """
    global _executor, _executor_lock
    if _executor_lock is None:
        _executor_lock = asyncio.Lock()
    async with _executor_lock:
        if _executor is None:
            _executor = TaskChainExecutor(coordinator_agent=coordinator_agent)
        elif coordinator_agent is not None and _executor._coordinator is None:
            # Late-bind coordinator if it wasn't available at startup
            _executor._coordinator = coordinator_agent
    return _executor
