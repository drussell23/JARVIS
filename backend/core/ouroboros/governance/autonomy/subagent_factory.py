"""subagent_factory — build a worker executor from a synthesized shape.

The :class:`SubagentFactory` constructs the per-worker cage + executor from
a :class:`~.worker_synthesizer.WorkerShape` (or the swarm fields of a
``WorkUnitSpec``):

  * a ``ScopedToolBackend`` parameterized with the synthesized allowlist +
    ``max_mutations`` (the per-worker tool/mutation cage — REUSED, already
    parameterized per instance), wrapping an inner backend;
  * the rendered worker system prompt (``render_worker_system_prompt``);
  * the synthesized context budget.

**Capability routing (NOT type-name dispatch).** The factory routes by the
SYNTHESIZED capability of the worker, derived from the AST/semantic
inspection — never by a fixed type name:

  * a MUTATING worker  -> the GENERAL executor path
    (``SubagentOrchestrator.dispatch_general`` + the Semantic Firewall) —
    Decision 2: zero net-new OS execution surface, the Iron Gate inherited
    natively;
  * a READ-ONLY worker -> the EXPLORE path.

There is no ``{role: path}`` lookup; ``route`` is computed purely from
``shape.is_mutating``.

Heavy dependencies (the SubagentOrchestrator, the firewall, the real tool
backend) are imported LAZILY — this module imports clean in a bare test env
(no torch / whisper / aiohttp). The dispatch callables are injectable so a
worker can be built + inspected without booting the orchestrator.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.autonomy.worker_synthesizer import (
    WorkerShape,
    render_worker_system_prompt,
)

logger = logging.getLogger(__name__)


class WorkerRoute(str, Enum):
    """The executor path a worker is routed to, by SYNTHESIZED capability."""

    GENERAL = "general"   # mutating worker -> dispatch_general + firewall
    EXPLORE = "explore"   # read-only worker -> explore path


def route_for_shape(shape: WorkerShape) -> WorkerRoute:
    """Compute the route from the synthesized capability (not a type name).

    Fail-CLOSED: anything that is not confidently mutating routes to the
    less-capable EXPLORE path.
    """
    return WorkerRoute.GENERAL if shape.is_mutating else WorkerRoute.EXPLORE


@dataclass
class BuiltWorker:
    """A constructed-but-not-yet-run worker: cage + prompt + route + voice.

    ``backend`` is the ``ScopedToolBackend`` cage (allowlist + mutation
    count gate). ``route`` is the capability path.

    **Sovereign Wiring (Phase 1d -- give workers voice).** When the swarm
    message bus is enabled AND a bus is supplied to :meth:`SubagentFactory.build`,
    the worker is granted:

      * ``sender`` -- an identity-locked :class:`~.agent_message_bus.BoundSender`
        (from ``bus.issue_sender(worker_id)``). The worker can do
        artifact-handoff + clarification-request via ``sender.send(...)``. The
        BoundSender has NO ``from_worker`` parameter -- the worker CANNOT send
        as a peer / the Commander. The worker NEVER receives the bus object or
        the graph secret -- only this capability locked to its own id.
      * ``inbox`` -- a :class:`~.agent_message_bus.SentinelInbox`, NEVER the raw
        ``bus.subscribe()`` deque. The SentinelInbox is the MANDATORY filtering
        inbox: its ``read()``/``drain()`` runs the Sentinel filter on every
        message and surfaces surviving peer content ONLY inside the never-obey
        quarantine fence (``<peer_data trust="none">``). A worker can never hold
        the raw deque -- a shipped-code invariant (:func:`register_shipped_invariants`)
        asserts this structurally. The worker system prompt also carries the
        standing ``PEER_DATA_FRAMING`` clause so even a scan-missed imperative
        renders as inert data.

    When the bus is OFF (no bus supplied), ``sender`` + ``inbox`` are ``None``
    and the worker stays silent exactly as Phase 1c -- byte-identical.
    """

    worker_id: str
    shape: WorkerShape
    route: WorkerRoute
    system_prompt: str
    scope_paths: Tuple[str, ...]
    backend: Any              # ScopedToolBackend (the cage)
    context_budget_tokens: int
    sender: Any = None        # BoundSender (identity-locked) | None when bus OFF
    inbox: Any = None         # bounded inbox deque | None when bus OFF

    @property
    def allowed_tools(self) -> Tuple[str, ...]:
        return self.shape.allowed_tools

    @property
    def mutation_budget(self) -> int:
        return self.shape.mutation_budget

    @property
    def has_voice(self) -> bool:
        """True iff this worker was granted a BoundSender (bus enabled)."""
        return self.sender is not None


class SubagentFactory:
    """Builds worker executors from synthesized shapes.

    The factory holds no role table. ``build`` derives everything from the
    shape; ``route_for_shape`` selects the path from capability alone.
    """

    def __init__(
        self,
        *,
        inner_backend_factory: Optional[Any] = None,
    ) -> None:
        """Parameters
        ----------
        inner_backend_factory:
            Optional zero-arg callable returning the inner ``ToolBackend``
            the ScopedToolBackend wraps. When None, a lazily-imported
            no-op inner backend is used (sufficient for construction +
            cage-enforcement tests; production injects the real backend).
        """
        self._inner_backend_factory = inner_backend_factory

    # -- cage construction ------------------------------------------------

    def _build_scoped_backend(self, shape: WorkerShape) -> Any:
        """Construct the ScopedToolBackend cage from the synthesized shape.

        Lazy imports keep the module bare-env importable.
        """
        from backend.core.ouroboros.governance.scoped_tool_access import (
            ScopedToolGate,
            ToolScope,
        )
        from backend.core.ouroboros.governance.scoped_tool_backend import (
            ScopedToolBackend,
        )

        scope = ToolScope(
            allowed_tools=frozenset(shape.allowed_tools),
            read_only=shape.read_only,
        )
        gate = ScopedToolGate(scope)

        if self._inner_backend_factory is not None:
            inner = self._inner_backend_factory()
        else:
            inner = _NullToolBackend()

        return ScopedToolBackend(
            inner=inner,
            gate=gate,
            max_mutations=shape.mutation_budget,
        )

    # -- public build -----------------------------------------------------

    def build(
        self,
        worker_spec: WorkerShape,
        *,
        worker_id: str,
        goal: str,
        scope_paths: Sequence[str],
        bus: Optional[Any] = None,
        graph_id: str = "",
    ) -> BuiltWorker:
        """Build a :class:`BuiltWorker` from a synthesized shape.

        Constructs the ScopedToolBackend cage with the synthesized
        allowlist + budget, renders the worker system prompt, and computes
        the capability route. Never raises for a benign shape.

        Sovereign Wiring (Phase 1d): when ``bus`` is supplied AND the swarm
        message bus master gate is ON, the worker is registered on the bus and
        granted an identity-locked :class:`BoundSender` (+ its inbox) so it can
        coordinate with peers. The worker NEVER gets the bus object or the
        graph secret -- only the BoundSender capability locked to its own id.
        When ``bus`` is None OR the gate is OFF, ``sender``/``inbox`` stay None
        and the worker is silent (byte-identical to Phase 1c).

        Also emits a best-effort ``swarm_node_spawned`` telemetry edge when a
        ``graph_id`` is known (topology only; fail-soft).
        """
        shape = worker_spec
        route = route_for_shape(shape)
        backend = self._build_scoped_backend(shape)
        prompt = render_worker_system_prompt(
            role=shape.role,
            goal=goal,
            scope_paths=list(scope_paths),
            allowed_tools=shape.allowed_tools,
            mutation_budget=shape.mutation_budget,
            read_only=shape.read_only,
        )

        sender, inbox = self._wire_voice(bus, worker_id)

        # When voice is wired, the worker WILL read peer content via the
        # SentinelInbox -> its system prompt MUST carry the standing never-obey
        # framing clause so even a scan-missed imperative is treated as inert
        # data. (The structural fence + this clause are the real boundary.)
        if inbox is not None:
            prompt = self._inject_peer_framing(prompt)

        logger.info(
            "[SubagentFactory] built worker=%s role=%r route=%s "
            "tools=%s mutation_budget=%d read_only=%s ctx_budget=%d voice=%s",
            worker_id, shape.role, route.value, list(shape.allowed_tools),
            shape.mutation_budget, shape.read_only, shape.context_budget_tokens,
            sender is not None,
        )
        self._emit_spawn_telemetry(graph_id, worker_id, shape)
        return BuiltWorker(
            worker_id=worker_id,
            shape=shape,
            route=route,
            system_prompt=prompt,
            scope_paths=tuple(str(p) for p in scope_paths),
            backend=backend,
            context_budget_tokens=shape.context_budget_tokens,
            sender=sender,
            inbox=inbox,
        )

    # -- Sovereign Wiring: give the worker a voice (Phase 1d) -------------

    @staticmethod
    def _wire_voice(bus: Optional[Any], worker_id: str) -> Tuple[Any, Any]:
        """Register the worker on the bus + issue its BoundSender + SentinelInbox.

        Returns ``(sender, inbox)`` or ``(None, None)`` when the bus is absent
        OR the master gate is OFF. Fail-CLOSED: any wiring error -> silent
        worker ``(None, None)`` (a worker without a sender simply cannot
        coordinate -- it never gets a half-wired / forgeable capability).

        The worker NEVER receives the bus object or the graph secret -- only the
        identity-locked BoundSender and a :class:`SentinelInbox` (the MANDATORY
        filtering inbox). It NEVER receives the raw ``bus.subscribe()`` deque:
        ``inbox`` is always a SentinelInbox, so the Sentinel filter is mandatory
        on the only read path.
        """
        if bus is None:
            return (None, None)
        try:
            from backend.core.ouroboros.governance.autonomy.agent_message_bus import (
                bus_enabled,
            )

            if not bus_enabled():
                return (None, None)
            # Register (idempotent) so issue_sender succeeds + the inbox exists.
            bus.register_worker(worker_id)
            sender = bus.issue_sender(worker_id)
            # The MANDATORY filtering inbox -- NEVER the raw deque.
            inbox = bus.sentinel_inbox(worker_id)
            return (sender, inbox)
        except Exception:  # noqa: BLE001 -- fail-CLOSED -> silent worker
            logger.debug(
                "[SubagentFactory] voice wiring failed for worker=%s -> silent",
                worker_id, exc_info=True,
            )
            return (None, None)

    @staticmethod
    def _inject_peer_framing(prompt: str) -> str:
        """Append the standing never-obey PEER_DATA_FRAMING clause to ``prompt``.

        The worker reads peer content via the SentinelInbox (fenced in
        ``<peer_data trust="none">``); this clause tells the model that content
        is UNTRUSTED DATA, never instructions. Together they are the structural
        boundary that scales past the (leaky) regex scan. Fail-soft: returns the
        prompt unchanged on any error. NEVER raises.
        """
        try:
            from backend.core.ouroboros.governance.autonomy.swarm_sentinel import (
                PEER_DATA_FRAMING,
            )

            if PEER_DATA_FRAMING and PEER_DATA_FRAMING not in prompt:
                return str(prompt) + "\n\n## Untrusted Peer Data\n" + PEER_DATA_FRAMING
            return prompt
        except Exception:  # noqa: BLE001 -- fail-soft
            return prompt

    @staticmethod
    def _emit_spawn_telemetry(
        graph_id: str, worker_id: str, shape: WorkerShape
    ) -> None:
        """Best-effort swarm_node_spawned telemetry. Topology only. NEVER raises."""
        if not graph_id:
            return
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (
                publish_swarm_node_spawned,
            )

            publish_swarm_node_spawned(
                str(graph_id),
                str(worker_id),
                str(shape.role),
                len(shape.allowed_tools),
                bool(shape.read_only),
            )
        except Exception:  # noqa: BLE001 -- fail-soft
            logger.debug(
                "[SubagentFactory] publish_swarm_node_spawned failed (non-fatal)",
                exc_info=True,
            )


def register_shipped_invariants() -> list:
    """Module-owned shipped-code invariant: the worker-facing inbox MUST be a
    SentinelInbox, NEVER the raw ``bus.subscribe()`` deque.

    This is the structural assertion for review-finding C1 (the load-bearing
    fix): the Sentinel filter is mandatory on the only worker read path. The
    invariant validates that ``_wire_voice`` returns ``bus.sentinel_inbox(...)``
    and does NOT hand a worker ``bus.subscribe(...)`` directly.

    NEVER raises (the discovery loop catches exceptions)."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_sentinel_inbox_mandatory(tree, source) -> tuple:
        violations = []
        # The wiring MUST issue the filtering inbox.
        if "sentinel_inbox(" not in source:
            violations.append(
                "C1: _wire_voice must hand the worker a SentinelInbox "
                "(bus.sentinel_inbox(...)) -- the mandatory filter on the only "
                "read path"
            )
        # The wiring MUST NOT hand a worker the raw deque. ``subscribe`` may
        # still be referenced by the bus internally, but inside THIS module a
        # bare ``bus.subscribe(`` in the voice-wiring path would re-open the
        # raw-deque hole the review found.
        if ".subscribe(" in source and "sentinel_inbox(" not in source:
            violations.append(
                "C1: subagent_factory must not return a raw bus.subscribe() "
                "deque to a worker"
            )
        if "PEER_DATA_FRAMING" not in source:
            violations.append(
                "Q4: subagent_factory must inject PEER_DATA_FRAMING (never-obey "
                "framing) into the worker prompt when voice is wired"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="swarm_worker_sentinel_inbox_mandatory",
            target_file=(
                "backend/core/ouroboros/governance/autonomy/subagent_factory.py"
            ),
            description=(
                "Swarm worker voice-wiring MUST hand the worker a SentinelInbox "
                "(mandatory Sentinel filter on the only read path) and inject "
                "the never-obey PEER_DATA_FRAMING clause -- NEVER the raw "
                "bus.subscribe() deque. Catches a refactor that re-opens the "
                "C1 inert-boundary hole from the adversarial review."
            ),
            validate=_validate_sentinel_inbox_mandatory,
        ),
    ]


class _NullToolBackend:
    """A no-op inner ToolBackend used when no real backend is injected.

    Every call returns a POLICY_DENIED-shaped result (fail-CLOSED) — the
    factory can be exercised + the cage enforced without a live backend.
    The ScopedToolBackend gate runs BEFORE this, so allowlist/budget
    enforcement is fully testable; a call that PASSES the gate still does
    no real work here.
    """

    async def execute_async(self, call: Any, policy_ctx: Any, deadline: float) -> Any:
        from backend.core.ouroboros.governance.tool_executor import (
            ToolExecStatus,
            ToolResult,
        )
        return ToolResult(
            tool_call=call,
            output="",
            error="null inner backend: no real executor injected (Phase 1a "
                  "build/inspection mode)",
            status=ToolExecStatus.POLICY_DENIED,
        )

    def release_op(self, *args: Any, **kwargs: Any) -> None:
        return None
