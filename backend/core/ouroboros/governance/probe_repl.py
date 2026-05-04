"""Slice 5b E — ``/probe`` REPL dispatcher.

Operator-facing CLI surface mirroring ``/posture`` (Wave 1 #1) +
``/governor`` + ``/cost`` patterns. Consumes the SAME readers that
the Slice 5b A HTTP routes consume so the data is single-sourced
(see :mod:`confidence_probe_observability`).

Subcommands (delimited by single space):

  * ``/probe``              — alias for ``/probe status``
  * ``/probe status``       — flag state + cadence + SSE event type
  * ``/probe config``       — env-knob snapshot
  * ``/probe allowlist``    — read-only tool allowlist (9-tool
    frozenset surfaced)
  * ``/probe help``         — usage listing (always available, no
    master-flag gate — discoverability)

Master gate: :func:`bridge_enabled` (the same flag the HTTP
``_gate()`` uses). Operators get an explicit DISABLED text rather
than a 503 since this is a console surface.

Auto-discovered by :func:`help_dispatcher._discover_module_provided_-
verbs` via :func:`register_verbs`. NEVER raises out of any public
function — defensive everywhere.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + verification.confidence_probe_* modules ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor.
  * Read-only — never mutates state.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List, Optional

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION,
    bridge_enabled,
    convergence_quorum,
    max_questions,
    max_tool_rounds_per_question,
)
from backend.core.ouroboros.governance.verification.confidence_probe_generator import (  # noqa: E501
    generator_mode,
)
from backend.core.ouroboros.governance.verification.confidence_probe_runner import (  # noqa: E501
    EVENT_TYPE_PROBE_OUTCOME,
    probe_wall_clock_s,
)
from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (  # noqa: E501
    READONLY_TOOL_ALLOWLIST,
    prober_enabled,
)

logger = logging.getLogger(__name__)


_HELP = (
    "/probe — Confidence Probe Loop (Move 5) console surface\n"
    "\n"
    "Subcommands:\n"
    "  /probe                 alias for /probe status\n"
    "  /probe status          flag state + cadence + SSE event\n"
    "  /probe config          env-knob snapshot\n"
    "  /probe allowlist       read-only tool allowlist (9 tools)\n"
    "  /probe help            this text\n"
    "\n"
    "Master flag: JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED\n"
    "Live HTTP surface: GET /observability/probe[/...]\n"
)


# ---------------------------------------------------------------------------
# Frozen result container — propagation-safe across boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeDispatchResult:
    """Result of a ``/probe`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/probe`` invocation at all (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    """Predicate — is this line a ``/probe`` invocation?"""
    s = (line or "").strip()
    if not s:
        return False
    return s == "/probe" or s == "probe" or (
        s.startswith("/probe ") or s.startswith("probe ")
    )


def _master_enabled() -> bool:
    """Wraps :func:`bridge_enabled` so the master gate is
    overridable in tests via env."""
    return bridge_enabled()


def dispatch_probe_command(line: str) -> ProbeDispatchResult:
    """Parse a ``/probe`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return ProbeDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ProbeDispatchResult(
            ok=False, text=f"  /probe parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    if head in ("help", "?"):
        return ProbeDispatchResult(ok=True, text=_HELP)

    if not _master_enabled():
        return ProbeDispatchResult(
            ok=False,
            text=(
                "  /probe: ConfidenceProbeBridge disabled — set "
                "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED=true"
            ),
        )

    if head == "status":
        return _render_status()
    if head == "config":
        return _render_config()
    if head == "allowlist":
        return _render_allowlist()
    return ProbeDispatchResult(
        ok=False,
        text=f"  /probe: unknown subcommand {head!r}. Try /probe help.",
    )


# ---------------------------------------------------------------------------
# Renderers — plain text, defensive everywhere
# ---------------------------------------------------------------------------


def _safe_int(fn) -> str:
    try:
        return str(fn())
    except Exception:  # noqa: BLE001 — defensive
        return "?"


def _safe_float(fn) -> str:
    try:
        return f"{float(fn()):.1f}"
    except Exception:  # noqa: BLE001 — defensive
        return "?"


def _safe_str(fn) -> str:
    try:
        return str(fn())
    except Exception:  # noqa: BLE001 — defensive
        return "?"


def _render_status() -> ProbeDispatchResult:
    lines: List[str] = [
        "/probe — Confidence Probe Loop status",
        "",
        f"  schema_version          {CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION}",  # noqa: E501
        f"  bridge_enabled          {bridge_enabled()}",
        f"  prober_enabled          {prober_enabled()}",
        f"  generator_mode          {_safe_str(lambda: generator_mode().value)}",  # noqa: E501
        f"  max_questions           {_safe_int(max_questions)}",
        f"  convergence_quorum      {_safe_int(convergence_quorum)}",
        f"  max_tool_rounds         {_safe_int(max_tool_rounds_per_question)}",  # noqa: E501
        f"  wall_clock_s            {_safe_float(probe_wall_clock_s)}",
        f"  allowlist_size          {len(READONLY_TOOL_ALLOWLIST)}",
        f"  sse_event_type          {EVENT_TYPE_PROBE_OUTCOME}",
        "",
    ]
    return ProbeDispatchResult(ok=True, text="\n".join(lines))


def _render_config() -> ProbeDispatchResult:
    lines: List[str] = [
        "/probe config — env-knob snapshot",
        "",
        f"  max_questions                 {_safe_int(max_questions)}",  # noqa: E501
        f"  convergence_quorum            {_safe_int(convergence_quorum)}",  # noqa: E501
        f"  max_tool_rounds_per_question  {_safe_int(max_tool_rounds_per_question)}",  # noqa: E501
        f"  wall_clock_s                  {_safe_float(probe_wall_clock_s)}",  # noqa: E501
        f"  generator_mode                {_safe_str(lambda: generator_mode().value)}",  # noqa: E501
        f"  allowlist_size                {len(READONLY_TOOL_ALLOWLIST)}",
        "",
    ]
    return ProbeDispatchResult(ok=True, text="\n".join(lines))


def _render_allowlist() -> ProbeDispatchResult:
    lines: List[str] = [
        f"/probe allowlist — {len(READONLY_TOOL_ALLOWLIST)} read-only tools",  # noqa: E501
        "",
    ]
    for tool in sorted(READONLY_TOOL_ALLOWLIST):
        lines.append(f"  • {tool}")
    lines.append("")
    return ProbeDispatchResult(ok=True, text="\n".join(lines))


# ---------------------------------------------------------------------------
# /help auto-discovery
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/probe`` verb into a :class:`VerbRegistry`.
    Auto-discovered by :func:`help_dispatcher._discover_module_-
    provided_verbs` at first ``get_default_verb_registry`` call.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/probe",
            one_line=(
                "Confidence-probe loop status, config, and "
                "read-only tool allowlist (Move 5)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[probe_repl] register_verbs swallowed", exc_info=True,
        )
        return 0


__all__ = [
    "ProbeDispatchResult",
    "dispatch_probe_command",
    "register_verbs",
]
