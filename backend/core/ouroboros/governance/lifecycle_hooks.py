"""
Lifecycle Hook Engine — Event-Driven Pipeline Interception
==========================================================

Fires events at key pipeline moments and lets registered handlers respond.
Inspired by Claude Code's 20+ hook events, adapted for the Ouroboros
governance pipeline.

Hook Types
----------
- **COMMAND**: Shell script executed via ``asyncio.create_subprocess_exec``.
  JSON payload on stdin; exit 0 = allow, exit 2 = block. Stderr = feedback.
- **PROMPT**: Single LLM call (placeholder — logs but does not call model).
- **FUNCTION**: Python async callable, invoked directly.

Firing Semantics
----------------
Hooks fire in **priority order** (highest first). If any hook returns
``allow=False``, the action is blocked and remaining hooks are skipped
(short-circuit). Timeouts are enforced per-hook via ``JARVIS_HOOK_TIMEOUT_S``
(default 10s).

Fault Isolation
---------------
Individual hook failures (exceptions, timeouts) are logged but never crash
the pipeline. A failing hook is treated as ALLOW to preserve liveness.

Singleton Access
----------------
``get_hook_engine()`` returns the module-level singleton, creating it on
first call.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

logger = logging.getLogger("Ouroboros.LifecycleHooks")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

_HOOK_TIMEOUT_S: float = float(
    os.environ.get("JARVIS_HOOK_TIMEOUT_S", "10")
)


# ---------------------------------------------------------------------------
# Hook Events
# ---------------------------------------------------------------------------


class HookEvent(enum.Enum):
    """Events emitted at key pipeline moments."""

    # Pipeline service lifecycle
    SESSION_START = "session_start"
    SESSION_END = "session_end"

    # Tool execution boundaries
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"

    # Pipeline phase boundaries
    PRE_PHASE = "pre_phase"
    POST_PHASE = "post_phase"

    # File system observation
    FILE_CHANGED = "file_changed"

    # Human-in-the-loop
    PERMISSION_REQUEST = "permission_request"

    # Operation lifecycle
    OP_SUBMITTED = "op_submitted"
    OP_COMPLETED = "op_completed"
    OP_FAILED = "op_failed"

    # Agent lifecycle
    AGENT_SPAWNED = "agent_spawned"
    AGENT_STOPPED = "agent_stopped"

    # Context compaction lifecycle
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"


# ---------------------------------------------------------------------------
# Hook Type
# ---------------------------------------------------------------------------


class HookType(enum.Enum):
    """Mechanism for executing a hook handler."""

    COMMAND = "command"    # Shell script / executable
    PROMPT = "prompt"      # Single LLM call (placeholder)
    FUNCTION = "function"  # Python async callable


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookResult:
    """Outcome of a single hook execution.

    Attributes
    ----------
    allow:
        If ``False``, the triggering action is blocked.
    feedback:
        Human-readable message back to the pipeline (may be empty).
    modified_payload:
        If set, replaces the original payload for downstream hooks
        and the action itself.
    """

    allow: bool = True
    feedback: str = ""
    modified_payload: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class HookHandler(Protocol):
    """Protocol for FUNCTION-type hook handlers."""

    async def handle(
        self, event: HookEvent, payload: Dict[str, Any]
    ) -> HookResult: ...


# ---------------------------------------------------------------------------
# Registration record
# ---------------------------------------------------------------------------


@dataclass
class HookRegistration:
    """A single registered hook.

    Attributes
    ----------
    event:
        Which pipeline event triggers this hook.
    hook_type:
        Execution mechanism (COMMAND, PROMPT, FUNCTION).
    handler:
        The actual handler -- an async callable / ``HookHandler`` instance
        for FUNCTION, a shell command string for COMMAND, or a prompt
        template string for PROMPT.
    matcher:
        Optional regex pattern applied to the JSON-serialised payload.
        If set, the hook only fires when the pattern matches.
    priority:
        Execution order within the same event. **Higher = fires first**.
    """

    event: HookEvent
    hook_type: HookType
    handler: Any
    matcher: Optional[str] = None
    priority: int = 0

    # Pre-compiled regex -- populated lazily
    _compiled_matcher: Optional[re.Pattern[str]] = field(
        default=None, init=False, repr=False, compare=False,
    )

    def matches(self, payload_json: str) -> bool:
        """Return True if the payload satisfies this hook's matcher."""
        if self.matcher is None:
            return True
        if self._compiled_matcher is None:
            try:
                self._compiled_matcher = re.compile(self.matcher)
            except re.error:
                logger.warning(
                    "[LifecycleHooks] Invalid matcher regex %r -- treating as always-match",
                    self.matcher,
                )
                return True
        return self._compiled_matcher.search(payload_json) is not None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class LifecycleHookEngine:
    """Central engine for registering and firing lifecycle hooks.

    Thread-safe for registration; ``fire()`` is async and must be called
    from the event loop.
    """

    def __init__(self) -> None:
        # event -> list of registrations (kept sorted by priority descending)
        self._hooks: Dict[HookEvent, List[HookRegistration]] = {
            evt: [] for evt in HookEvent
        }

    # -- Registration -------------------------------------------------------

    def register(
        self,
        event: HookEvent,
        handler: Any,
        hook_type: HookType = HookType.FUNCTION,
        matcher: Optional[str] = None,
        priority: int = 0,
    ) -> HookRegistration:
        """Register a hook for *event*.

        Returns the created ``HookRegistration`` (useful for later
        ``unregister``).
        """
        reg = HookRegistration(
            event=event,
            hook_type=hook_type,
            handler=handler,
            matcher=matcher,
            priority=priority,
        )
        bucket = self._hooks[event]
        bucket.append(reg)
        # Re-sort: highest priority first
        bucket.sort(key=lambda r: r.priority, reverse=True)
        logger.debug(
            "[LifecycleHooks] Registered %s hook for %s (priority=%d, matcher=%r)",
            hook_type.value, event.value, priority, matcher,
        )
        return reg

    def unregister(self, event: HookEvent, handler: Any) -> bool:
        """Remove *all* registrations for *event* whose handler is *handler*.

        Returns ``True`` if at least one registration was removed.
        """
        bucket = self._hooks[event]
        before = len(bucket)
        self._hooks[event] = [r for r in bucket if r.handler is not handler]
        removed = before - len(self._hooks[event])
        if removed:
            logger.debug(
                "[LifecycleHooks] Unregistered %d hook(s) for %s",
                removed, event.value,
            )
        return removed > 0

    # -- Firing -------------------------------------------------------------

    async def fire(
        self,
        event: HookEvent,
        payload: Dict[str, Any],
    ) -> List[HookResult]:
        """Fire all hooks for *event*, respecting priority order.

        If any hook returns ``allow=False``, remaining hooks are **skipped**
        (short-circuit) and the blocking result is the last element in the
        returned list.

        Payload mutations from ``modified_payload`` are forwarded to
        subsequent hooks in the chain.
        """
        results: List[HookResult] = []
        bucket = self._hooks.get(event, [])
        if not bucket:
            return results

        current_payload = dict(payload)
        payload_json = _safe_json(current_payload)

        for reg in bucket:
            if not reg.matches(payload_json):
                continue

            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    self._execute_hook(reg, event, current_payload),
                    timeout=_HOOK_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - t0
                logger.warning(
                    "[LifecycleHooks] Hook timed out after %.1fs for %s (type=%s)",
                    elapsed, event.value, reg.hook_type.value,
                )
                # Timeout -> fail-open (allow)
                result = HookResult(
                    allow=True,
                    feedback=f"Hook timed out after {elapsed:.1f}s -- allowing",
                )
            except asyncio.CancelledError:
                raise  # Never swallow cancellation
            except Exception as exc:
                logger.warning(
                    "[LifecycleHooks] Hook raised %s for %s -- failing open: %s",
                    type(exc).__name__, event.value, exc,
                    exc_info=True,
                )
                result = HookResult(
                    allow=True,
                    feedback=f"Hook error ({type(exc).__name__}) -- allowing",
                )

            results.append(result)

            # Apply payload mutation if present
            if result.modified_payload is not None:
                current_payload = dict(result.modified_payload)
                payload_json = _safe_json(current_payload)

            # Short-circuit on block
            if not result.allow:
                logger.info(
                    "[LifecycleHooks] Hook BLOCKED %s: %s",
                    event.value, result.feedback,
                )
                break

        return results

    # -- Dispatch per hook type ---------------------------------------------

    async def _execute_hook(
        self,
        reg: HookRegistration,
        event: HookEvent,
        payload: Dict[str, Any],
    ) -> HookResult:
        """Dispatch to the appropriate execution strategy."""
        if reg.hook_type == HookType.FUNCTION:
            return await self._execute_function(reg, event, payload)
        elif reg.hook_type == HookType.COMMAND:
            return await self._execute_command(reg, event, payload)
        elif reg.hook_type == HookType.PROMPT:
            return await self._execute_prompt(reg, event, payload)
        else:
            logger.warning(
                "[LifecycleHooks] Unknown hook type %r -- allowing",
                reg.hook_type,
            )
            return HookResult(allow=True)

    async def _execute_function(
        self,
        reg: HookRegistration,
        event: HookEvent,
        payload: Dict[str, Any],
    ) -> HookResult:
        """Execute a FUNCTION-type hook (async callable or HookHandler)."""
        handler = reg.handler
        if isinstance(handler, HookHandler):
            return await handler.handle(event, payload)
        # Bare async callable: call with (event, payload) and interpret
        raw = await handler(event, payload)
        if isinstance(raw, HookResult):
            return raw
        # If the callable returned a bool, interpret it
        if isinstance(raw, bool):
            return HookResult(allow=raw)
        # Fallback: treat as allow
        return HookResult(allow=True)

    async def _execute_command(
        self,
        reg: HookRegistration,
        event: HookEvent,
        payload: Dict[str, Any],
    ) -> HookResult:
        """Execute a COMMAND-type hook via subprocess.

        Protocol:
        - Payload is written as JSON to stdin.
        - Exit code 0 = allow; exit code 2 = block; anything else = allow
          (fail-open).
        - Feedback is read from stderr.

        Uses ``asyncio.create_subprocess_exec`` with argv splitting
        (no shell=True) to avoid injection.
        """
        command = reg.handler
        if not isinstance(command, str):
            logger.warning(
                "[LifecycleHooks] COMMAND handler is not a string: %r",
                command,
            )
            return HookResult(allow=True)

        stdin_data = _safe_json({
            "event": event.value,
            "payload": payload,
        }).encode("utf-8")

        parts = command.split()
        if not parts:
            return HookResult(allow=True)

        proc = await asyncio.create_subprocess_exec(
            *parts,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=stdin_data)

        feedback = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
        exit_code = proc.returncode or 0

        if exit_code == 0:
            return HookResult(allow=True, feedback=feedback)
        elif exit_code == 2:
            return HookResult(allow=False, feedback=feedback or "Blocked by command hook")
        else:
            # Non-standard exit code -> fail-open
            logger.warning(
                "[LifecycleHooks] Command hook exited with code %d -- allowing",
                exit_code,
            )
            return HookResult(
                allow=True,
                feedback=feedback or f"Command exited with code {exit_code}",
            )

    async def _execute_prompt(
        self,
        reg: HookRegistration,
        event: HookEvent,
        payload: Dict[str, Any],
    ) -> HookResult:
        """Execute a PROMPT-type hook (placeholder -- no model call).

        A real implementation would inject the prompt template and payload
        into a provider call. For now, this logs the intent and allows.
        """
        prompt_template = reg.handler if isinstance(reg.handler, str) else str(reg.handler)
        logger.info(
            "[LifecycleHooks] PROMPT hook for %s (template length=%d) -- "
            "placeholder, allowing without model call",
            event.value, len(prompt_template),
        )
        return HookResult(
            allow=True,
            feedback="Prompt hook placeholder -- no model invoked",
        )

    # -- Introspection ------------------------------------------------------

    def list_hooks(self, event: Optional[HookEvent] = None) -> List[HookRegistration]:
        """Return all registrations, optionally filtered by event."""
        if event is not None:
            return list(self._hooks.get(event, []))
        all_hooks: List[HookRegistration] = []
        for bucket in self._hooks.values():
            all_hooks.extend(bucket)
        return all_hooks

    def clear(self, event: Optional[HookEvent] = None) -> int:
        """Remove all hooks (or all hooks for a specific event).

        Returns the count of removed registrations.
        """
        if event is not None:
            count = len(self._hooks.get(event, []))
            self._hooks[event] = []
            return count
        count = sum(len(b) for b in self._hooks.values())
        for evt in HookEvent:
            self._hooks[evt] = []
        return count


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_engine: Optional[LifecycleHookEngine] = None


def get_hook_engine() -> LifecycleHookEngine:
    """Return the module-level singleton, creating it on first call."""
    global _engine
    if _engine is None:
        _engine = LifecycleHookEngine()
        logger.info("[LifecycleHooks] Engine initialised (timeout=%.1fs)", _HOOK_TIMEOUT_S)
    return _engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_json(obj: Any) -> str:
    """JSON-encode *obj*, falling back to repr on failure."""
    try:
        return json.dumps(obj, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return repr(obj)
