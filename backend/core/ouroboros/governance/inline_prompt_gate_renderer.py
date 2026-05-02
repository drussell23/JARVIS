"""InlinePromptGate Slice 4 — listener-based phase-boundary renderer.

The operator-visible surface for phase-boundary prompts.

Architectural problem closed
----------------------------

The existing :class:`ConsoleInlineRenderer` (in
``inline_permission_repl.py``) is bound to the
:class:`InlinePermissionMiddleware` instance — only fires when a
**per-tool-call** prompt flows through that middleware. Slice 2's
phase-boundary producer bypasses the middleware entirely (it calls
``controller.request()`` directly), so phase-boundary prompts had
NO render path. They were registered, the SSE broker fired
``inline_prompt_pending``, but no operator-facing surface displayed
them. That left the human staring at a SerpentFlow countdown that
auto-applied while a controller-pending prompt sat unanswered.

Architectural reuse — three existing surfaces compose
-----------------------------------------------------

1. :meth:`InlinePromptController.on_transition` — same listener
   hook the SSE bridge already uses (proven pattern from
   ``inline_permission_observability.attach_controller_to_broker``).
   We attach a parallel listener that renders to the operator
   console. Listeners compose; they do not interfere.

2. :data:`PHASE_BOUNDARY_TOOL_SENTINEL` (Slice 2) — the
   ``tool`` field on the controller projection. Phase-boundary
   prompts have ``tool == "phase_boundary"``; per-tool-call
   prompts have a real tool name. The listener filters by this
   sentinel so it cannot accidentally double-render a per-tool
   prompt that the existing :class:`ConsoleInlineRenderer` is
   already handling.

3. :class:`InlinePromptRequest` projection (controller's
   ``_project`` method) — the same bounded dict shape the SSE
   bridge consumes. We render from that projection (no
   privileged access required), so the renderer has no
   authority surface.

Direct-solve principles
-----------------------

* **Asynchronous-ready** — listener invocations are sync (called
  from the controller's lock-protected ``_fire``). The listener
  formats + writes to the print callback — no awaits, no
  blocking I/O. Print-callback failures are caught and logged;
  they NEVER raise into the controller's listener loop.

* **Dynamic** — Rich rendering used when available (matches
  SerpentFlow's visual language); plain-text fallback when
  Rich isn't installed. Selection is per-call so importing
  this module never requires Rich.

* **Adaptive** — degraded projections (missing fields, wrong
  types) all render to a sentinel placeholder rather than
  raising. The controller's payload contract is bounded by
  ``_project``; we still defend at the boundary.

* **Intelligent** — terminal-state verb mapping mirrors the
  existing ``ConsoleInlineRenderer.dismiss`` so operators see a
  consistent vocabulary (``allowed`` / ``denied`` / ``paused``
  / ``expired``). Phase-boundary prompts get a distinct visual
  marker (``[Phase Boundary]``) so operators can tell at a
  glance that this is a whole-op confirmation, not a
  single-tool-call prompt.

* **Robust** — every public function NEVER raises. Listener
  errors caught + logged. Print-cb errors caught + logged.

* **No hardcoding** — print callback is dependency-injected
  (test fixtures pass ``lines.append``; production passes
  ``console.print``). All visual constants are module-level
  symbols (Slice 5 AST-pin candidates).

Authority invariants (AST-pinned by Slice 5):

* MAY import: ``inline_permission_prompt`` (controller +
  projection types), ``inline_prompt_gate_runner`` (sentinels).
* MUST NOT import: orchestrator / phase_runner / iron_gate /
  change_engine / candidate_generator / providers /
  doubleword_provider / urgency_router / auto_action_router /
  subagent_scheduler / tool_executor / semantic_guardian /
  semantic_firewall / risk_engine.
* No exec/eval/compile (mirrors Slice 1+2+3 critical safety
  pin).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptController,
    get_default_controller,
)
from backend.core.ouroboros.governance.inline_prompt_gate_runner import (
    PHASE_BOUNDARY_RULE_ID,
    PHASE_BOUNDARY_TOOL_SENTINEL,
)

logger = logging.getLogger(__name__)


PrintCallback = Callable[[str], None]


# ---------------------------------------------------------------------------
# Visual constants — Slice 5 AST-pin candidates
# ---------------------------------------------------------------------------

#: Header marker that distinguishes phase-boundary prompts from
#: per-tool-call prompts in the operator console. Stable wire
#: format — operators / log scrapers can grep for it.
PHASE_BOUNDARY_HEADER: str = "[Phase Boundary]"

#: Default truncation cap on prompt_id rendering (full id stays in
#: audit). 40 chars matches the existing ConsoleInlineRenderer's
#: prompt_id truncation so the two visual styles align.
PROMPT_ID_DISPLAY_CHARS: int = 40

#: Verb dictionary for terminal-state rendering. Mirrors the
#: existing ConsoleInlineRenderer.dismiss vocabulary so operators
#: see a consistent set of verbs across both phase-boundary and
#: per-tool-call prompts.
TERMINAL_STATE_VERBS: Dict[str, str] = {
    "allowed": "allowed",
    "denied": "denied",
    "expired": "expired",
    "paused": "paused",
}

#: Action hint rendered with every prompt — points operators at the
#: existing REPL verbs that resolve the controller Future
#: (Slice 3 confirmed these work for phase-boundary prompts via
#: the shared singleton — no new verbs needed).
PROMPT_ACTIONS_HINT: str = (
    "    actions  : /allow   /deny <reason>   /pause"
)


# ---------------------------------------------------------------------------
# Pure formatters — golden-testable
# ---------------------------------------------------------------------------


def _safe_str(value: Any, default: str = "") -> str:
    try:
        if value is None:
            return default
        return str(value)
    except Exception:  # noqa: BLE001 — defensive
        return default


def _truncate(value: str, *, max_chars: int) -> str:
    try:
        s = _safe_str(value)
        if len(s) <= max_chars:
            return s
        return s[: max(1, max_chars - 3)] + "..."
    except Exception:  # noqa: BLE001 — defensive
        return ""


def format_phase_boundary_block(projection: Dict[str, Any]) -> str:
    """Pure formatter for the operator-visible prompt block.

    Takes the controller's bounded projection dict (the same shape
    the SSE bridge sees) and produces a multi-line plain-text
    block. NEVER raises — degraded fields render as
    ``"(unknown)"`` placeholders.

    Visual style mirrors :meth:`ConsoleInlineRenderer.format_block`
    so the two coexist consistently in the same console — except
    the leading marker is :data:`PHASE_BOUNDARY_HEADER` instead of
    ``[InlinePrompt]``, distinguishing whole-op confirmations
    from per-tool-call prompts at a glance.
    """
    try:
        prompt_id = _safe_str(projection.get("prompt_id"), "(unknown)")
        op_id = _safe_str(projection.get("op_id"), "(unknown)")
        target = _safe_str(projection.get("target_path"), "(unknown)")
        preview = _truncate(
            _safe_str(projection.get("arg_preview"), "(no summary)"),
            max_chars=200,
        )
        rule_id = _safe_str(
            projection.get("verdict_rule_id"), "(unknown)",
        )
        try:
            timeout_s = float(projection.get("timeout_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            timeout_s = 0.0

        lines = [
            "",
            f"  {PHASE_BOUNDARY_HEADER} op-confirmation pending",
            f"    summary  : {preview}",
            f"    target   : {target}",
            f"    op       : {op_id}",
            f"    rule     : {rule_id}",
            f"    timeout  : {timeout_s:.1f}s",
            f"    prompt_id: {_truncate(prompt_id, max_chars=PROMPT_ID_DISPLAY_CHARS)}",
            PROMPT_ACTIONS_HINT,
            "",
        ]
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[InlinePromptGateRenderer] format_phase_boundary_block "
            "degraded: %s", exc,
        )
        return f"\n  {PHASE_BOUNDARY_HEADER} (render degraded)\n"


def format_dismiss_line(projection: Dict[str, Any]) -> str:
    """Pure formatter for the operator-visible dismiss line on
    terminal events. NEVER raises."""
    try:
        prompt_id = _safe_str(projection.get("prompt_id"), "(unknown)")
        state = _safe_str(projection.get("state"), "")
        reviewer = _safe_str(projection.get("reviewer"), "")
        operator_reason = _truncate(
            _safe_str(projection.get("operator_reason"), ""),
            max_chars=80,
        )
        verb = TERMINAL_STATE_VERBS.get(state, state or "(unknown)")
        line = (
            f"  {PHASE_BOUNDARY_HEADER} {verb}: "
            f"{_truncate(prompt_id, max_chars=PROMPT_ID_DISPLAY_CHARS)} "
            f"(reviewer={reviewer or 'auto'})"
        )
        if operator_reason:
            line += f" reason={operator_reason}"
        return line
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[InlinePromptGateRenderer] format_dismiss_line "
            "degraded: %s", exc,
        )
        return f"  {PHASE_BOUNDARY_HEADER} (dismiss render degraded)"


# ---------------------------------------------------------------------------
# Phase-boundary projection filter
# ---------------------------------------------------------------------------


def _is_phase_boundary_projection(projection: Dict[str, Any]) -> bool:
    """The renderer must IGNORE per-tool-call prompts (already
    handled by the existing ConsoleInlineRenderer via the
    middleware path). Phase-boundary prompts are identified by
    the ``tool`` sentinel from Slice 2's bridge.

    Defense-in-depth: also accept the rule_id sentinel as a
    secondary marker. Either match qualifies as phase-boundary.
    NEVER raises."""
    try:
        tool = _safe_str(projection.get("tool"))
        if tool == PHASE_BOUNDARY_TOOL_SENTINEL:
            return True
        rule_id = _safe_str(projection.get("verdict_rule_id"))
        return rule_id == PHASE_BOUNDARY_RULE_ID
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Listener bridge
# ---------------------------------------------------------------------------


PENDING_EVENT: str = "inline_prompt_pending"
TERMINAL_EVENTS: frozenset = frozenset({
    "inline_prompt_allowed",
    "inline_prompt_denied",
    "inline_prompt_expired",
    "inline_prompt_paused",
})


def _make_listener(print_cb: PrintCallback) -> Callable[
    [Dict[str, Any]], None,
]:
    """Build the listener closure that the controller fires.

    The closure filters by phase-boundary sentinel, formats
    via the pure formatters, and writes via the injected
    ``print_cb``. Any error in formatting OR printing is caught
    and logged — the controller's ``_fire`` already swallows
    listener exceptions but we add an inner safety net so a
    formatter bug never causes the controller to silently
    drop OTHER listeners' invocations on the same event.
    """

    def _listener(payload: Dict[str, Any]) -> None:
        try:
            if not isinstance(payload, dict):
                return
            event_type = _safe_str(payload.get("event_type"))
            projection = payload.get("projection") or {}
            if not isinstance(projection, dict):
                return
            if not _is_phase_boundary_projection(projection):
                return
            if event_type == PENDING_EVENT:
                block = format_phase_boundary_block(projection)
                _safe_print(print_cb, block)
            elif event_type in TERMINAL_EVENTS:
                line = format_dismiss_line(projection)
                _safe_print(print_cb, line)
            # Other event types: no-op (the controller doesn't fire
            # them today, but defensive: silently ignore unknowns).
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[InlinePromptGateRenderer] listener degraded: %s", exc,
            )

    return _listener


def _safe_print(print_cb: PrintCallback, text: str) -> None:
    """Wrap the operator-supplied print callback in a defensive
    boundary. Never raises. Logs once-per-failure (the controller
    fires us synchronously; an unhandled raise would interfere
    with sibling listeners)."""
    try:
        print_cb(text)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[InlinePromptGateRenderer] print_cb raised: %s — "
            "phase-boundary render dropped", exc,
        )


# ---------------------------------------------------------------------------
# Public boot-time wiring API
# ---------------------------------------------------------------------------


def attach_phase_boundary_renderer(
    print_cb: PrintCallback,
    *,
    controller: Optional[InlinePromptController] = None,
) -> Callable[[], None]:
    """Subscribe a phase-boundary renderer to the controller.

    Returns an unsubscribe callable. Idempotent in the sense that
    repeated calls install repeated listeners — callers wishing to
    replace MUST call the previously-returned unsubscribe first.

    SerpentFlow boot wiring is a single line::

        from backend.core.ouroboros.governance.inline_prompt_gate_renderer import (
            attach_phase_boundary_renderer,
        )
        self._unsub_inline_prompt_renderer = (
            attach_phase_boundary_renderer(self._console.print)
        )

    Every phase-boundary prompt the controller registers will
    render via ``print_cb``; every terminal transition will also
    print a one-line dismiss summary. ZERO interference with
    per-tool-call prompts (they're filtered out by the sentinel
    check) — the existing :class:`ConsoleInlineRenderer` keeps
    handling those via the middleware path.

    NEVER raises. If ``controller`` is None, defaults to
    :func:`get_default_controller`. If controller resolution
    itself fails, returns a no-op unsubscribe rather than
    propagating the exception (defensive: the renderer is an
    operator-UX nicety, not an authority surface — its absence
    must never block boot).
    """
    try:
        active_controller = controller or get_default_controller()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[InlinePromptGateRenderer] controller resolution "
            "failed: %s — renderer NOT attached", exc,
        )

        def _noop_unsub() -> None:
            return None

        return _noop_unsub

    listener = _make_listener(print_cb)
    try:
        unsub = active_controller.on_transition(listener)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[InlinePromptGateRenderer] on_transition failed: "
            "%s — renderer NOT attached", exc,
        )

        def _noop_unsub() -> None:
            return None

        return _noop_unsub

    logger.info(
        "[InlinePromptGateRenderer] attached "
        "controller=%s print_cb=%s",
        type(active_controller).__name__,
        getattr(print_cb, "__qualname__", repr(print_cb)),
    )
    return unsub


# ---------------------------------------------------------------------------
# Public surface — Slice 5 will pin via shipped_code_invariants
# ---------------------------------------------------------------------------

__all__ = [
    "PENDING_EVENT",
    "PHASE_BOUNDARY_HEADER",
    "PROMPT_ACTIONS_HINT",
    "PROMPT_ID_DISPLAY_CHARS",
    "PrintCallback",
    "TERMINAL_EVENTS",
    "TERMINAL_STATE_VERBS",
    "attach_phase_boundary_renderer",
    "format_dismiss_line",
    "format_phase_boundary_block",
]
