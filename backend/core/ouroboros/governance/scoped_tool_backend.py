"""ScopedToolBackend — pre-linguistic tool allowlist enforcement for subagents.

Wraps any :class:`ToolBackend` with a :class:`ScopedToolGate` so that tool
calls outside the subagent's scope return a deterministic
``POLICY_DENIED`` result BEFORE reaching the inner backend. This is the
structural enforcement layer Manifesto §5 (Semantic Firewall) demands
for GENERAL subagents: once a GENERAL dispatch lands with
``allowed_tools=("read_file",)``, any model attempt to call
``bash``/``edit_file``/``write_file`` is refused at the backend boundary.

Why this layer exists despite the global ``GoverningToolPolicy``:

  * The global policy runs AFTER this adapter — it's the second
    refusal in series. If the global policy is ever relaxed, this
    adapter still refuses. Defense in depth via Manifesto §1 Boundary
    Principle.
  * The global policy's decisions depend on broader context
    (``is_read_only``, ``risk_tier``, repo). The subagent scope is a
    pure static allowlist — no reasoning, no context, no bypass
    surface. A prompt-injection attempt in the GENERAL system prompt
    cannot talk its way past this check.
  * The rejection is PRE-LINGUISTIC: the LLM's tool-call JSON is
    rejected based on the ``name`` field alone, before any arguments
    are parsed or any side effects occur. The model sees a
    ``ToolExecStatus.POLICY_DENIED`` result it can reason about, not
    a half-executed state.

Usage::

    from backend.core.ouroboros.governance.scoped_tool_access import (
        ScopedToolGate, ToolScope,
    )
    from backend.core.ouroboros.governance.scoped_tool_backend import (
        ScopedToolBackend,
    )
    scope = ToolScope(
        allowed_tools=frozenset(invocation["allowed_tools"]),
        read_only=(invocation["max_mutations"] == 0),
    )
    gate = ScopedToolGate(scope)
    backend = ScopedToolBackend(inner=real_backend, gate=gate)
    # Drop-in replacement for the inner backend in ToolLoopCoordinator.

Safety invariants:

  * Never mutates inner backend state on rejected calls — the inner
    backend's ``execute_async`` is never even awaited when the gate
    returns False. The rejection is pure allowlist comparison.
  * Never raises on rejection — returns a well-formed ``ToolResult``
    with ``status=POLICY_DENIED``, so the tool loop's normal
    error-handling paths engage cleanly.
  * Delegates ``release_op`` / other optional backend methods
    transparently via ``__getattr__`` — the adapter is invisible to
    callers that use those extension points.
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from backend.core.ouroboros.governance.scoped_tool_access import ScopedToolGate

if TYPE_CHECKING:
    from backend.core.ouroboros.governance.tool_executor import (
        PolicyContext,
        ToolBackend,
        ToolCall,
        ToolResult,
    )

logger = logging.getLogger(__name__)


class ScopedToolBackend:
    """Subagent-scoped allowlist adapter over any ``ToolBackend``.

    Implements the ``ToolBackend`` Protocol so it's a drop-in wherever
    the real backend was used. The adapter adds one gate call per
    tool execution; the overhead is O(1) set membership.
    """

    def __init__(
        self,
        inner: "ToolBackend",
        gate: ScopedToolGate,
    ) -> None:
        """Wrap ``inner`` backend with ``gate``.

        The gate is consulted on every ``execute_async`` call; if it
        refuses, ``inner.execute_async`` is never awaited.
        """
        self._inner = inner
        self._gate = gate

    async def execute_async(
        self,
        call: "ToolCall",
        policy_ctx: "PolicyContext",
        deadline: float,
    ) -> "ToolResult":
        """Enforce the scope, then delegate to the inner backend.

        If the gate refuses, returns a ``ToolResult`` with
        ``status=POLICY_DENIED``, non-empty ``error``, and empty
        ``output``. The caller (``ToolLoopCoordinator``) will surface
        this back to the model via the normal denial-formatting path.
        """
        allowed, reason = self._gate.can_use(call.name)
        if not allowed:
            # Lazy import — avoid circular during module load. All
            # callers that exercise this path have already imported
            # tool_executor, so this is cheap at runtime.
            from backend.core.ouroboros.governance.tool_executor import (
                ToolExecStatus,
                ToolResult,
            )
            logger.info(
                "[ScopedToolBackend] BLOCKED tool=%s reason=%s "
                "op=%s call_id=%s (subagent-scope pre-linguistic gate)",
                call.name, reason, policy_ctx.op_id, policy_ctx.call_id,
            )
            return ToolResult(
                tool_call=call,
                output="",
                error=(
                    f"subagent-scope refusal: tool {call.name!r} is not in "
                    f"the dispatched allowed_tools set — {reason}. The "
                    "subagent boundary rejected this call BEFORE the global "
                    "policy engine; this rejection is non-negotiable."
                ),
                status=ToolExecStatus.POLICY_DENIED,
            )
        return await self._inner.execute_async(call, policy_ctx, deadline)

    def __getattr__(self, name: str) -> Any:
        """Transparent passthrough for optional backend methods
        (``release_op`` and any future extension points) so the adapter
        stays invisible to callers that rely on them.

        ``__getattr__`` fires only when normal attribute lookup fails,
        so the adapter's own methods (``execute_async``, ``_inner``,
        ``_gate``) are NOT passed through — only truly-missing names go
        to the inner backend.
        """
        return getattr(self._inner, name)
