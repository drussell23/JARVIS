"""InlinePromptGate Slice 2 — async producer / controller bridge.

The thin async layer that translates a Slice 1
:class:`PhaseInlinePromptRequest` into the existing
:class:`InlinePromptController` substrate, awaits the operator's
Future, and returns a :class:`PhaseInlinePromptVerdict`.

Architectural reuse (per the Founding Architect directive)
----------------------------------------------------------

Three existing surfaces compose into the end-to-end loop with
ZERO new SSE wiring required:

1. :class:`InlinePromptController` (``inline_permission_prompt.py``)
   — Future-backed registry, 4 operator actions, timeout→EXPIRED,
   capacity limits, bounded history, singleton via
   :func:`get_default_controller`.

2. :func:`attach_controller_to_broker` (``inline_permission_observability.py``)
   — already subscribes the controller's ``on_transition`` listener
   and publishes ``inline_prompt_{pending,allowed,denied,expired,
   paused}`` to the SSE broker. **Reused as-is** — every prompt this
   producer registers fires SSE events automatically.

3. :func:`compute_phase_inline_verdict` (``inline_prompt_gate.py``)
   — the Slice 1 total mapping function. Reused to convert the
   controller's terminal state into the closed 5-value taxonomy.

The only NEW code in Slice 2 is the orchestrator-shape →
controller-shape bridge function and the async wrapper that owns
the timeout + defensive degradation contract.

Direct-solve principles
-----------------------

* **Asynchronous-ready** — single ``await asyncio.wait_for(...)``
  on the controller's Future. The orchestrator's APPLY-phase
  cancel_check loop can race this via :func:`asyncio.wait` if it
  needs concurrent cancellation semantics; the producer is the
  inner await.

* **Dynamic** — every numeric (timeout) flows from Slice 1's
  env-knob helpers. Caller may override via kwarg. No hardcoded
  magic constants.

* **Adaptive** — degraded paths (controller capacity exhausted,
  state errors, Future cancellation, async timeout, garbage
  outcome) all map to closed-taxonomy verdicts (DISABLED /
  EXPIRED) rather than raises. NEVER propagates exceptions to
  the orchestrator-callable surface.

* **Intelligent** — the bridge synthesizes a sentinel
  :class:`InlineGateVerdict` with ``rule_id="phase_boundary_
  inline_prompt"`` so the existing tool-call gate audit /
  observability paths can DISTINGUISH phase-boundary prompts
  from per-tool-call prompts in the same controller history /
  SSE stream.

* **Robust** — every public function NEVER raises. The producer
  is callable from any async context (orchestrator phase boundary,
  test fixture, REPL handler).

* **No hardcoding** — sentinel constants exposed as module-level
  symbols so Slice 5's AST-pin can assert they're stable +
  Slice 4's REPL bridge can filter prompt history by them.

Authority invariants (AST-pinned by Slice 5):

* MAY import: ``inline_prompt_gate`` (Slice 1 primitive),
  ``inline_permission_prompt`` (controller substrate),
  ``inline_permission`` (verdict shapes).
* MUST NOT import: orchestrator / phase_runners / iron_gate /
  change_engine / candidate_generator / providers / urgency_router
  / auto_action_router / subagent_scheduler / tool_executor /
  semantic_guardian / semantic_firewall / risk_engine. The bridge
  is observability-shaped: it asks the operator and returns a
  verdict; it does not itself decide policy.
* No exec/eval/compile (mirrors Slice 1 critical-safety pin).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from backend.core.ouroboros.governance.inline_permission import (
    InlineDecision,
    InlineGateVerdict,
    RoutePosture,
    UpstreamPolicy,
)
from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptCapacityError,
    InlinePromptController,
    InlinePromptOutcome,
    InlinePromptRequest,
    InlinePromptStateError,
    get_default_controller,
)
from backend.core.ouroboros.governance.inline_prompt_gate import (
    PhaseInlinePromptRequest,
    PhaseInlinePromptVerdict,
    compute_phase_inline_verdict,
    default_prompt_timeout_s,
    inline_prompt_gate_enabled,
    truncate_summary,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentinel constants — stable wire-format for filtering / pinning
# ---------------------------------------------------------------------------

#: Sentinel ``tool`` field for controller-shaped requests originating
#: from a phase-boundary producer. Lets downstream filters (REPL
#: history, IDE prompt list, postmortem audit) distinguish per-op
#: phase-boundary prompts from per-tool-call prompts in the same
#: controller singleton. AST-pinned by Slice 5.
PHASE_BOUNDARY_TOOL_SENTINEL: str = "phase_boundary"

#: Sentinel ``rule_id`` for the synthesized :class:`InlineGateVerdict`.
#: The existing tool-call rule allowlists in ``inline_permission.py``
#: never match this id by construction (no rule registers it), so a
#: phase-boundary verdict cannot be mistaken for a tool-call gate
#: decision in audit. AST-pinned by Slice 5.
PHASE_BOUNDARY_RULE_ID: str = "phase_boundary_inline_prompt"

#: Sentinel ``call_id`` prefix. The full call_id is the prompt_id
#: (which is sha256-derived in Slice 1's :func:`derive_prompt_id`).
PHASE_BOUNDARY_CALL_ID_PREFIX: str = "pb-"

#: Reviewer string written by the producer when the controller's
#: outcome lacks a reviewer (defensive — outcome.reviewer should
#: always populate, but degraded paths may not).
DEFAULT_REVIEWER: str = "phase_boundary_producer"


# ---------------------------------------------------------------------------
# Route-posture mapping
# ---------------------------------------------------------------------------


def _resolve_route_posture(route: str) -> RoutePosture:
    """Map the Slice 1 string route to the controller's enum.
    Defensive: unknown / garbage → INTERACTIVE (the conservative
    default — assume a human is present unless the orchestrator
    explicitly declares otherwise)."""
    try:
        if not isinstance(route, str):
            return RoutePosture.INTERACTIVE
        normalized = route.strip().lower()
        if normalized == "autonomous":
            return RoutePosture.AUTONOMOUS
        return RoutePosture.INTERACTIVE
    except Exception:  # noqa: BLE001 — defensive
        return RoutePosture.INTERACTIVE


def _render_target_path(req: PhaseInlinePromptRequest) -> str:
    """Single-path summary for the controller's per-prompt
    ``target_path`` field. Phase-boundary prompts often touch
    multiple files; the controller's audit only carries one path,
    so we render ``"<first> (+N more)"`` when N>1 and
    ``"(no targets)"`` when empty. The full tuple stays in the
    Slice 1 :class:`PhaseInlinePromptRequest` for audit."""
    try:
        paths = req.target_paths or ()
        if not paths:
            return "(no targets)"
        first = str(paths[0])
        extras = len(paths) - 1
        if extras <= 0:
            return first
        return f"{first} (+{extras} more)"
    except Exception:  # noqa: BLE001 — defensive
        return "(no targets)"


# ---------------------------------------------------------------------------
# Bridge: PhaseInlinePromptRequest → controller-shaped InlinePromptRequest
# ---------------------------------------------------------------------------


def bridge_to_controller_request(
    req: PhaseInlinePromptRequest,
) -> InlinePromptRequest:
    """Adapter: orchestrator-shape → controller-shape.

    The existing :class:`InlinePromptRequest` is tool-call-shaped
    (``tool`` / ``arg_fingerprint`` / ``arg_preview`` /
    ``target_path`` / ``verdict: InlineGateVerdict``). We synthesize
    the tool-call fields with phase-boundary sentinels so the
    controller's bookkeeping + observability + audit still work,
    while remaining structurally distinguishable from per-tool
    prompts.

    NEVER raises. Garbage input → controller request with empty /
    sentinel fields (controller's own validation will surface
    structural errors via :class:`InlinePromptStateError`)."""
    try:
        synthesized_verdict = InlineGateVerdict(
            decision=InlineDecision.ASK,
            rule_id=PHASE_BOUNDARY_RULE_ID,
            reason=(
                req.change_summary
                or "phase_boundary inline prompt"
            )[:200],
        )
        call_id = (
            f"{PHASE_BOUNDARY_CALL_ID_PREFIX}{req.op_id or 'unknown'}"
        )
        arg_preview = truncate_summary(
            req.change_summary, max_chars=200,
        )
        target_path = _render_target_path(req)
        return InlinePromptRequest(
            prompt_id=str(req.prompt_id),
            op_id=str(req.op_id),
            call_id=call_id,
            tool=PHASE_BOUNDARY_TOOL_SENTINEL,
            arg_fingerprint=str(req.change_fingerprint),
            arg_preview=arg_preview,
            target_path=target_path,
            verdict=synthesized_verdict,
            rationale=str(req.rationale or ""),
            route=_resolve_route_posture(req.route),
            upstream_decision=UpstreamPolicy.NO_MATCH,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[InlinePromptGateRunner] bridge degraded: %s — "
            "constructing sentinel request",
            exc,
        )
        return InlinePromptRequest(
            prompt_id=str(getattr(req, "prompt_id", "")) or "ipg-degraded",
            op_id=str(getattr(req, "op_id", "")),
            call_id=PHASE_BOUNDARY_CALL_ID_PREFIX + "degraded",
            tool=PHASE_BOUNDARY_TOOL_SENTINEL,
            arg_fingerprint="",
            arg_preview="",
            target_path="(degraded)",
            verdict=InlineGateVerdict(
                decision=InlineDecision.ASK,
                rule_id=PHASE_BOUNDARY_RULE_ID,
                reason="degraded",
            ),
            rationale="",
            route=RoutePosture.INTERACTIVE,
            upstream_decision=UpstreamPolicy.NO_MATCH,
        )


# ---------------------------------------------------------------------------
# Async producer — the orchestrator-callable surface
# ---------------------------------------------------------------------------


async def request_phase_inline_prompt(
    request: PhaseInlinePromptRequest,
    *,
    controller: Optional[InlinePromptController] = None,
    timeout_s: Optional[float] = None,
    enabled: Optional[bool] = None,
) -> PhaseInlinePromptVerdict:
    """Register a phase-boundary prompt and await the operator's
    answer. NEVER raises — every error path maps to a closed-
    taxonomy verdict.

    Master-flag-off path:
      * Returns ``PhaseInlineVerdict.DISABLED`` immediately — no
        controller call, no SSE emission, no Future allocation.
        The orchestrator can interpret DISABLED as "skip the
        prompt; resume current behavior" (backward-compat path).

    Capacity / state error path:
      * Returns ``PhaseInlineVerdict.DISABLED`` — distinguishable
        from EXPIRED because the prompt never reached an operator.

    Async-cancellation path:
      * If the awaiting coroutine is cancelled (e.g., the
        orchestrator's APPLY-phase cancel_check fires CancelToken
        on the parent task), :class:`asyncio.CancelledError`
        propagates per asyncio convention — this is the ONE
        documented exception case. Callers (orchestrator wire-up
        Slice 4) catch it explicitly. The pending controller
        prompt is left for the controller's own timeout to
        EXPIRE-clean.

    Timeout path:
      * The controller's internal timeout fires
        :class:`InlinePromptOutcome` with ``state=STATE_EXPIRED``
        — this is the natural EXPIRED path. The asyncio
        wait_for is a defense-in-depth secondary timeout (caller-
        controlled) at slightly larger budget.

    Args:
      request: Slice 1 :class:`PhaseInlinePromptRequest`.
      controller: Optional explicit controller (test injection).
        Defaults to :func:`get_default_controller`.
      timeout_s: Optional caller override of the asyncio wait
        ceiling. Defaults to controller's own timeout + 1s grace
        (so the controller's STATE_EXPIRED path always fires
        first under normal conditions).
      enabled: Optional explicit enable override (test injection).
        Defaults to :func:`inline_prompt_gate_enabled`.

    Returns:
      :class:`PhaseInlinePromptVerdict` — terminal verdict with
      Phase C tightening stamp populated by Slice 1's mapping.
    """
    # 1. Master-flag short-circuit (resolved per-call so flips
    #    hot-revert without restart).
    is_enabled = (
        enabled if enabled is not None else inline_prompt_gate_enabled()
    )
    if not is_enabled:
        return compute_phase_inline_verdict(
            prompt_id=str(request.prompt_id),
            op_id=str(request.op_id),
            state=None,
            enabled=False,
        )

    # 2. Resolve controller (singleton by default).
    try:
        active_controller = controller or get_default_controller()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[InlinePromptGateRunner] controller resolution "
            "degraded: %s — DISABLED",
            exc,
        )
        return compute_phase_inline_verdict(
            prompt_id=str(request.prompt_id),
            op_id=str(request.op_id),
            state=None,
            enabled=False,
        )

    # 3. Build controller-shaped request.
    bridged = bridge_to_controller_request(request)

    # 4. Register with controller — handle capacity / state errors
    #    by mapping to DISABLED (defensive; the prompt never
    #    reached an operator).
    try:
        future = active_controller.request(
            bridged,
            timeout_s=request.timeout_s if request.timeout_s > 0 else None,
        )
    except InlinePromptCapacityError as exc:
        logger.warning(
            "[InlinePromptGateRunner] controller at capacity: %s — "
            "DISABLED prompt_id=%s",
            exc, request.prompt_id,
        )
        return compute_phase_inline_verdict(
            prompt_id=str(request.prompt_id),
            op_id=str(request.op_id),
            state=None,
            enabled=False,
        )
    except InlinePromptStateError as exc:
        # Most common cause: deterministic prompt_id collision
        # (re-issue of an idempotent retry). Defensive: treat as
        # DISABLED rather than raise — the orchestrator's retry
        # loop should not see a Python exception.
        logger.warning(
            "[InlinePromptGateRunner] controller state error: %s "
            "— DISABLED prompt_id=%s",
            exc, request.prompt_id,
        )
        return compute_phase_inline_verdict(
            prompt_id=str(request.prompt_id),
            op_id=str(request.op_id),
            state=None,
            enabled=False,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.warning(
            "[InlinePromptGateRunner] controller.request raised "
            "unexpected: %s — DISABLED prompt_id=%s",
            exc, request.prompt_id,
        )
        return compute_phase_inline_verdict(
            prompt_id=str(request.prompt_id),
            op_id=str(request.op_id),
            state=None,
            enabled=False,
        )

    # 5. Resolve effective wait timeout. Defense-in-depth: caller
    #    timeout slightly larger than controller timeout so the
    #    controller's STATE_EXPIRED path fires first under normal
    #    conditions. Falls back to env default + 1s grace.
    effective_wait = (
        timeout_s if timeout_s is not None and timeout_s > 0
        else (request.timeout_s + 1.0 if request.timeout_s > 0
              else default_prompt_timeout_s() + 1.0)
    )

    # 6. Await Future — asyncio cancellation propagates by design.
    outcome: Optional[InlinePromptOutcome] = None
    try:
        outcome = await asyncio.wait_for(future, timeout=effective_wait)
    except asyncio.TimeoutError:
        # Defensive: secondary asyncio timeout fired before the
        # controller's internal timeout. Synthesize an EXPIRED
        # verdict directly. The pending controller prompt will be
        # auto-cleaned by its own _run_timeout task.
        logger.info(
            "[InlinePromptGateRunner] asyncio wait_for fired "
            "before controller timeout (defense-in-depth) "
            "prompt_id=%s wait=%.1fs",
            request.prompt_id, effective_wait,
        )
        return compute_phase_inline_verdict(
            prompt_id=str(request.prompt_id),
            op_id=str(request.op_id),
            state="expired",
            enabled=True,
        )
    except asyncio.CancelledError:
        # Caller-initiated cancellation — propagate per asyncio
        # convention. Orchestrator wire-up (Slice 4) catches.
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.warning(
            "[InlinePromptGateRunner] await future raised "
            "unexpected: %s — DISABLED prompt_id=%s",
            exc, request.prompt_id,
        )
        return compute_phase_inline_verdict(
            prompt_id=str(request.prompt_id),
            op_id=str(request.op_id),
            state=None,
            enabled=False,
        )

    # 7. Map outcome → verdict via Slice 1 total mapping.
    if outcome is None:
        # Should not occur (await success branch always sets
        # outcome), but defensive.
        return compute_phase_inline_verdict(
            prompt_id=str(request.prompt_id),
            op_id=str(request.op_id),
            state=None,
            enabled=False,
        )
    return compute_phase_inline_verdict(
        prompt_id=str(outcome.prompt_id),
        op_id=str(request.op_id),
        state=str(outcome.state) if outcome.state else None,
        elapsed_s=float(outcome.elapsed_s),
        reviewer=str(outcome.reviewer or DEFAULT_REVIEWER),
        operator_reason=str(outcome.operator_reason or ""),
        enabled=True,
    )


# ---------------------------------------------------------------------------
# Public surface — Slice 5 will pin via shipped_code_invariants
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_REVIEWER",
    "PHASE_BOUNDARY_CALL_ID_PREFIX",
    "PHASE_BOUNDARY_RULE_ID",
    "PHASE_BOUNDARY_TOOL_SENTINEL",
    "bridge_to_controller_request",
    "register_shipped_invariants",
    "request_phase_inline_prompt",
]


# ---------------------------------------------------------------------------
# Slice 5 — Module-owned shipped_code_invariants contribution
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Register Slice 2's structural invariants. Discovered
    automatically. Returns :class:`ShippedCodeInvariant` instances."""
    import ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_sentinels_stable(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Phase-boundary sentinel constants must remain at exact
        wire-format values. Renaming any of these silently breaks
        Slice 4's listener filter, the Slice 3 HTTP filter, and
        operator-visible audit. Stamped here to prevent silent
        drift."""
        violations: list = []
        expected = {
            "PHASE_BOUNDARY_TOOL_SENTINEL": "phase_boundary",
            "PHASE_BOUNDARY_RULE_ID": "phase_boundary_inline_prompt",
            "PHASE_BOUNDARY_CALL_ID_PREFIX": "pb-",
            "DEFAULT_REVIEWER": "phase_boundary_producer",
        }
        seen: dict = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name,
            ):
                if node.target.id in expected:
                    if isinstance(node.value, ast.Constant):
                        seen[node.target.id] = node.value.value
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in expected:
                        if isinstance(node.value, ast.Constant):
                            seen[tgt.id] = node.value.value
        for name, expected_value in expected.items():
            if name not in seen:
                violations.append(
                    f"missing sentinel constant {name!r}"
                )
            elif seen[name] != expected_value:
                violations.append(
                    f"sentinel {name!r} drifted: expected "
                    f"{expected_value!r}, got {seen[name]!r}"
                )
        return tuple(violations)

    def _validate_authority_allowlist(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 2 may only import from a small allowlist + the
        registration-contract exemption. Banned: orchestrator-tier
        modules."""
        violations: list = []
        allowed = {
            "backend.core.ouroboros.governance.inline_permission",
            "backend.core.ouroboros.governance.inline_permission_prompt",
            "backend.core.ouroboros.governance.inline_prompt_gate",
        }
        registration_funcs = {
            "register_flags",
            "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                lineno = getattr(node, "lineno", 0)
                if any(s <= lineno <= e for s, e in exempt_ranges):
                    continue
                for ban in banned_substrings:
                    if ban in module:
                        violations.append(
                            f"line {lineno}: BANNED orchestrator-tier "
                            f"substring {ban!r} in {module!r}"
                        )
                if "backend." in module or (
                    "governance" in module and module
                ):
                    if module not in allowed:
                        violations.append(
                            f"line {lineno}: import outside Slice 2 "
                            f"allowlist: {module!r}"
                        )
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"Slice 2 MUST NOT {node.func.id}()"
                        )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/inline_prompt_gate_runner.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="inline_prompt_gate_runner_sentinels_stable",
            target_file=target,
            description=(
                "Phase-boundary sentinel constants "
                "(PHASE_BOUNDARY_TOOL_SENTINEL / PHASE_BOUNDARY_RULE_ID "
                "/ PHASE_BOUNDARY_CALL_ID_PREFIX / DEFAULT_REVIEWER) "
                "remain at exact wire-format values. Drift breaks "
                "Slice 4 listener filter, Slice 3 HTTP filter, audit."
            ),
            validate=_validate_sentinels_stable,
        ),
        ShippedCodeInvariant(
            invariant_name="inline_prompt_gate_runner_authority_allowlist",
            target_file=target,
            description=(
                "Slice 2 producer/bridge imports stay within "
                "{inline_permission, inline_permission_prompt, "
                "inline_prompt_gate} (+ registration-contract "
                "exemption). No orchestrator-tier imports allowed."
            ),
            validate=_validate_authority_allowlist,
        ),
    ]
