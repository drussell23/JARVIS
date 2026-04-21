"""ScopedToolBackend — pre-linguistic tool allowlist enforcement for subagents.

Wraps any :class:`ToolBackend` with a :class:`ScopedToolGate` so that tool
calls outside the subagent's scope return a deterministic
``POLICY_DENIED`` result BEFORE reaching the inner backend. This is the
structural enforcement layer Manifesto §5 (Semantic Firewall) demands
for GENERAL subagents: once a GENERAL dispatch lands with
``allowed_tools=("read_file",)``, any model attempt to call
``bash``/``edit_file``/``write_file`` is refused at the backend boundary.

Two independent gates layer here:

  * **Type gate** — ``ScopedToolGate`` vs. ``allowed_tools``. A tool
    not in the static allowlist is denied regardless of state.
  * **Count gate** — mutation budget vs. ``max_mutations``. Every
    call to a tool in ``_MUTATION_TOOLS`` consumes one slot. Once
    the budget is exhausted, subsequent mutation calls return
    ``POLICY_DENIED`` even though their *type* is allowlisted. This
    is the Phase C Slice 1b graduation follow-through (Ticket 8) —
    Slice 1b proved models respect ``max_mutations`` cooperatively;
    this layer turns that into a structural guarantee.

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
    backend = ScopedToolBackend(
        inner=real_backend,
        gate=gate,
        max_mutations=invocation["max_mutations"],
    )
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
import time
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from backend.core.ouroboros.governance.scoped_tool_access import (
    _MUTATION_TOOLS,
    ScopedToolGate,
)

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

    Carries a per-instance mutation counter that enforces
    ``max_mutations`` structurally: every call to a tool in
    ``_MUTATION_TOOLS`` consumes one slot, and the adapter denies
    subsequent mutation calls once the budget is exhausted. The
    counter is instance-local — a fresh ``ScopedToolBackend`` is
    constructed per GENERAL dispatch, so there is no cross-op leak.
    """

    def __init__(
        self,
        inner: "ToolBackend",
        gate: ScopedToolGate,
        *,
        max_mutations: int = 0,
        state_mirror: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Wrap ``inner`` backend with ``gate`` and a mutation budget.

        Parameters
        ----------
        inner:
            The real ``ToolBackend`` the adapter delegates to when a
            call passes both the type gate and the count gate.
        gate:
            ``ScopedToolGate`` enforcing the static allowlist /
            denylist / read-only layers.
        max_mutations:
            Maximum number of calls to tools in ``_MUTATION_TOOLS``
            permitted over the lifetime of this adapter. ``0`` (the
            default) forbids all mutations; callers that want
            read-only semantics typically also set ``gate`` with
            ``read_only=True`` — both mechanisms layered give
            defense-in-depth.
        state_mirror:
            Optional shared-state dict the adapter pushes live
            counters into on every ``execute_async``. The executor's
            hard-kill wrapper reads this dict to build a partial
            exec_trace on cancellation (Epoch 2 / Ticket 9). The
            mirror is authoritative only for the executor's
            post-cancel snapshot — the adapter's own properties are
            always authoritative for in-process readers. Fields
            populated: ``tool_calls_made``, ``tool_names``,
            ``mutations_count``, ``max_mutations``,
            ``mutation_records``, ``call_records``,
            ``last_activity_mono``.
        """
        self._inner = inner
        self._gate = gate
        self._max_mutations = max(0, int(max_mutations))
        self._mutations_count = 0
        # Per-mutation audit trail — each authorized mutation appends
        # ``(tool_name, call_id, t_authorized_mono)``. Captures WHICH
        # mutations happened under the budget, not just HOW MANY.
        self._mutation_records: List[Tuple[str, str, float]] = []
        # Every execute_async attempt (authorized or denied) appends
        # ``(tool_name, call_id, status, t_mono)`` — status ∈
        # {"authorized", "type_denied", "count_denied"}. Operators grep
        # this on hard_kill to reconstruct the full authorization log.
        self._call_records: List[Tuple[str, str, str, float]] = []
        self._state_mirror = state_mirror
        # Seed the mirror with our static config so readers see a
        # well-formed dict even before the first tool call.
        self._sync_state_mirror()

    # --- properties -----------------------------------------------------

    @property
    def mutations_count(self) -> int:
        """Number of mutation-tool calls this adapter has authorized.

        Exposed for exec_trace bookkeeping — the driver reads this on
        the hard_kill path so partial mutation records survive timeout
        (Ticket 9 / Epoch 2).
        """
        return self._mutations_count

    @property
    def max_mutations(self) -> int:
        """Configured mutation budget (immutable after construction)."""
        return self._max_mutations

    @property
    def mutation_records(self) -> Tuple[Tuple[str, str, float], ...]:
        """Immutable snapshot of authorized mutation records.

        Each entry is ``(tool_name, call_id, t_authorized_mono)`` —
        the timestamp is the ``time.monotonic()`` value at
        authorization, not inner-call success. Returned as a tuple
        to prevent accidental mutation via the snapshot.
        """
        return tuple(self._mutation_records)

    @property
    def call_records(self) -> Tuple[Tuple[str, str, str, float], ...]:
        """Immutable snapshot of every authorization decision.

        Each entry is ``(tool_name, call_id, status, t_mono)`` with
        ``status`` ∈ ``{"authorized", "type_denied",
        "count_denied"}``. Captures the full audit trail including
        refused calls — critical for detecting adversarial models
        that probe the cage.
        """
        return tuple(self._call_records)

    @property
    def tool_names(self) -> Tuple[str, ...]:
        """Unique tool names seen in authorized calls (insertion order)."""
        seen: Dict[str, None] = {}
        for tool_name, _call_id, status, _ts in self._call_records:
            if status == "authorized" and tool_name not in seen:
                seen[tool_name] = None
        return tuple(seen.keys())

    # --- internals ------------------------------------------------------

    def _sync_state_mirror(self) -> None:
        """Push the current adapter state into ``self._state_mirror``
        if one was provided. Idempotent — safe to call from every
        execute_async. Uses a fresh list / tuple each time so
        concurrent readers see a consistent snapshot."""
        if self._state_mirror is None:
            return
        self._state_mirror["tool_calls_made"] = sum(
            1 for _n, _cid, status, _ts in self._call_records
            if status == "authorized"
        )
        self._state_mirror["tool_names"] = list(self.tool_names)
        self._state_mirror["mutations_count"] = self._mutations_count
        self._state_mirror["max_mutations"] = self._max_mutations
        self._state_mirror["mutation_records"] = [
            {"tool": t, "call_id": c, "t_mono": ts}
            for t, c, ts in self._mutation_records
        ]
        self._state_mirror["call_records"] = [
            {"tool": n, "call_id": c, "status": s, "t_mono": ts}
            for n, c, s, ts in self._call_records
        ]
        self._state_mirror["last_activity_mono"] = (
            self._call_records[-1][3] if self._call_records else 0.0
        )

    async def execute_async(
        self,
        call: "ToolCall",
        policy_ctx: "PolicyContext",
        deadline: float,
    ) -> "ToolResult":
        """Enforce the scope, then delegate to the inner backend.

        Enforcement order (first-rejection-wins):
          1. Type gate — ``ScopedToolGate.can_use(call.name)``. Rejects
             tools outside the static allowlist, explicitly denied
             tools, and mutation tools under a ``read_only`` scope.
          2. Count gate — if ``call.name`` is in ``_MUTATION_TOOLS``
             and ``self._mutations_count >= self._max_mutations``,
             rejects the call as ``POLICY_DENIED`` *before* delegating.
             The slot is consumed at the point of authorization, not at
             inner-call success, so a model cannot retry a failing
             mutation to eventually burn extra slots.

        On any rejection, returns a ``ToolResult`` with
        ``status=POLICY_DENIED``, non-empty ``error``, and empty
        ``output``. The caller (``ToolLoopCoordinator``) will surface
        this back to the model via the normal denial-formatting path.
        """
        t_now = time.monotonic()
        # Layer 1: type gate (allowlist / denylist / read-only).
        allowed, reason = self._gate.can_use(call.name)
        if not allowed:
            # Lazy import — avoid circular during module load. All
            # callers that exercise this path have already imported
            # tool_executor, so this is cheap at runtime.
            from backend.core.ouroboros.governance.tool_executor import (
                ToolExecStatus,
                ToolResult,
            )
            self._call_records.append(
                (call.name, policy_ctx.call_id, "type_denied", t_now),
            )
            self._sync_state_mirror()
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

        # Layer 2: count gate (mutation budget).
        if call.name in _MUTATION_TOOLS:
            if self._mutations_count >= self._max_mutations:
                from backend.core.ouroboros.governance.tool_executor import (
                    ToolExecStatus,
                    ToolResult,
                )
                self._call_records.append(
                    (call.name, policy_ctx.call_id, "count_denied", t_now),
                )
                self._sync_state_mirror()
                logger.info(
                    "[ScopedToolBackend] BLOCKED tool=%s reason=mutation_budget_exhausted "
                    "mutations_count=%d max_mutations=%d op=%s call_id=%s "
                    "(subagent max_mutations COUNT gate)",
                    call.name, self._mutations_count, self._max_mutations,
                    policy_ctx.op_id, policy_ctx.call_id,
                )
                return ToolResult(
                    tool_call=call,
                    output="",
                    error=(
                        f"subagent max_mutations budget exhausted: "
                        f"{self._mutations_count}/{self._max_mutations} mutation "
                        f"slots consumed — tool {call.name!r} refused at the "
                        "subagent boundary. This is the structural COUNT gate "
                        "(distinct from the allowlist TYPE gate); the budget "
                        "is set at dispatch and cannot be extended mid-op. "
                        "This rejection is non-negotiable."
                    ),
                    status=ToolExecStatus.POLICY_DENIED,
                )
            # Authorize: consume the slot BEFORE delegating. A failure
            # inside the inner backend still burns the slot — this is
            # deliberate. It prevents a model from retrying a failing
            # mutation to eventually exceed the budget.
            self._mutations_count += 1
            self._mutation_records.append(
                (call.name, policy_ctx.call_id, t_now),
            )

        # Record the authorized call BEFORE delegating so if the inner
        # backend hangs or raises, the audit trail still reflects that
        # this call was authorized. Epoch 2: this is the single
        # authoritative record the hard_kill path reads from.
        self._call_records.append(
            (call.name, policy_ctx.call_id, "authorized", t_now),
        )
        self._sync_state_mirror()

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
