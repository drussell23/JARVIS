"""Slice 5b E — ``/coherence`` REPL dispatcher.

Operator-facing CLI surface mirroring ``/probe`` (Slice 5b E
companion). Consumes the SAME readers that the Slice 5b B HTTP
routes consume (see :mod:`coherence_observability`).

Subcommands:

  * ``/coherence``              — alias for ``/coherence status``
  * ``/coherence status``       — schemas + flags + budgets + cadence
    + observer counter snapshot
  * ``/coherence config``       — full env-knob snapshot
  * ``/coherence audits [N]``   — last N BehavioralDriftVerdict
    history records (default 20)
  * ``/coherence advisories [N]`` — last N CoherenceAdvisory
    records (default 20)
  * ``/coherence stats``        — alias for ``status`` focused on
    the live observer counter snapshot
  * ``/coherence help``         — usage listing (always available)

Master gate: :func:`coherence_auditor_enabled`. Auto-discovered by
:func:`help_dispatcher._discover_module_provided_verbs`. NEVER
raises.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + verification.coherence_* modules ONLY.
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
from typing import Any, List

from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    coherence_action_bridge_enabled,
    read_coherence_advisories,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
    COHERENCE_AUDITOR_SCHEMA_VERSION,
    BehavioralDriftKind,
    budget_confidence_rise_pct,
    budget_posture_locked_hours,
    budget_recurrence_count,
    budget_route_drift_pct,
    coherence_auditor_enabled,
    halflife_days,
)
from backend.core.ouroboros.governance.verification.coherence_observer import (  # noqa: E501
    EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED,
    cadence_hours_default,
    cadence_hours_harden,
    cadence_hours_maintain,
    get_default_observer,
    observer_enabled,
)
from backend.core.ouroboros.governance.verification.coherence_window_store import (  # noqa: E501
    read_drift_audit,
)

logger = logging.getLogger(__name__)


_HELP = (
    "/coherence — Coherence Auditor (Priority #1) console surface\n"
    "\n"
    "Subcommands:\n"
    "  /coherence                   alias for /coherence status\n"
    "  /coherence status            flags + budgets + observer counters\n"
    "  /coherence config            env-knob snapshot\n"
    "  /coherence audits [N]        last N drift verdicts (default 20)\n"
    "  /coherence advisories [N]    last N tightening advisories (default 20)\n"
    "  /coherence stats             alias for status (live counters)\n"
    "  /coherence help              this text\n"
    "\n"
    "Master flag: JARVIS_COHERENCE_AUDITOR_ENABLED\n"
    "Live HTTP surface: GET /observability/coherence[/...]\n"
)

_DEFAULT_LIMIT: int = 20
_MAX_LIMIT: int = 200


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoherenceDispatchResult:
    """Result of a ``/coherence`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/coherence`` invocation at all."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return s == "/coherence" or s == "coherence" or (
        s.startswith("/coherence ") or s.startswith("coherence ")
    )


def _parse_limit(args: List[str]) -> int:
    if len(args) < 2:
        return _DEFAULT_LIMIT
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > _MAX_LIMIT:
            return _MAX_LIMIT
        return n
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT


def dispatch_coherence_command(
    line: str,
) -> CoherenceDispatchResult:
    """Parse a ``/coherence`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return CoherenceDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return CoherenceDispatchResult(
            ok=False, text=f"  /coherence parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    if head in ("help", "?"):
        return CoherenceDispatchResult(ok=True, text=_HELP)

    if not coherence_auditor_enabled():
        return CoherenceDispatchResult(
            ok=False,
            text=(
                "  /coherence: CoherenceAuditor disabled — set "
                "JARVIS_COHERENCE_AUDITOR_ENABLED=true"
            ),
        )

    if head in ("status", "stats"):
        return _render_status()
    if head == "config":
        return _render_config()
    if head == "audits":
        return _render_audits(_parse_limit(args))
    if head == "advisories":
        return _render_advisories(_parse_limit(args))
    return CoherenceDispatchResult(
        ok=False,
        text=(
            f"  /coherence: unknown subcommand {head!r}. "
            f"Try /coherence help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _safe_call(fn) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — defensive
        return None


def _render_status() -> CoherenceDispatchResult:
    snap = _safe_call(lambda: get_default_observer().snapshot())
    if not isinstance(snap, dict):
        snap = {}
    lines: List[str] = [
        "/coherence — Coherence Auditor status",
        "",
        f"  schema_version              {COHERENCE_AUDITOR_SCHEMA_VERSION}",  # noqa: E501
        f"  auditor_enabled             {coherence_auditor_enabled()}",  # noqa: E501
        f"  observer_enabled            {observer_enabled()}",
        f"  action_bridge_enabled       {coherence_action_bridge_enabled()}",  # noqa: E501
        "",
        "  Budgets:",
        f"    route_drift_pct           {_safe_call(budget_route_drift_pct)}",  # noqa: E501
        f"    posture_locked_hours      {_safe_call(budget_posture_locked_hours)}",  # noqa: E501
        f"    recurrence_count          {_safe_call(budget_recurrence_count)}",  # noqa: E501
        f"    confidence_rise_pct       {_safe_call(budget_confidence_rise_pct)}",  # noqa: E501
        f"    halflife_days             {_safe_call(halflife_days)}",
        "",
        "  Observer counters:",
        f"    cycles_total              {snap.get('cycles_total', '?')}",
        f"    cycles_coherent           {snap.get('cycles_coherent', '?')}",  # noqa: E501
        f"    cycles_drift_emitted      {snap.get('cycles_drift_emitted', '?')}",  # noqa: E501
        f"    cycles_drift_deduped      {snap.get('cycles_drift_deduped', '?')}",  # noqa: E501
        f"    cycles_insufficient       {snap.get('cycles_insufficient', '?')}",  # noqa: E501
        f"    cycles_failed             {snap.get('cycles_failed', '?')}",
        f"    consecutive_failures      {snap.get('consecutive_failures', '?')}",  # noqa: E501
        "",
        f"  sse_event_type              {EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED}",  # noqa: E501
        "",
    ]
    return CoherenceDispatchResult(ok=True, text="\n".join(lines))


def _render_config() -> CoherenceDispatchResult:
    lines: List[str] = [
        "/coherence config — env-knob snapshot",
        "",
        f"  budget_route_drift_pct        {_safe_call(budget_route_drift_pct)}",  # noqa: E501
        f"  budget_posture_locked_hours   {_safe_call(budget_posture_locked_hours)}",  # noqa: E501
        f"  budget_recurrence_count       {_safe_call(budget_recurrence_count)}",  # noqa: E501
        f"  budget_confidence_rise_pct    {_safe_call(budget_confidence_rise_pct)}",  # noqa: E501
        f"  halflife_days                 {_safe_call(halflife_days)}",
        f"  cadence_hours_default         {_safe_call(cadence_hours_default)}",  # noqa: E501
        f"  cadence_hours_harden          {_safe_call(cadence_hours_harden)}",  # noqa: E501
        f"  cadence_hours_maintain        {_safe_call(cadence_hours_maintain)}",  # noqa: E501
        f"  drift_kinds                   {[k.value for k in BehavioralDriftKind]}",  # noqa: E501
        "",
    ]
    return CoherenceDispatchResult(ok=True, text="\n".join(lines))


def _render_audits(limit: int) -> CoherenceDispatchResult:
    try:
        result = read_drift_audit(limit=limit)
        verdicts = result.verdicts
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[coherence_repl] _render_audits raised: %s", exc,
        )
        verdicts = ()
    lines: List[str] = [
        f"/coherence audits — last {limit} drift verdicts "
        f"({len(verdicts)} found)",
        "",
    ]
    if not verdicts:
        lines.append("  (no verdicts yet)")
        lines.append("")
        return CoherenceDispatchResult(
            ok=True, text="\n".join(lines),
        )
    for v in verdicts[-limit:]:
        try:
            outcome = v.outcome.value
            severity = v.largest_severity.value
            sig = (v.drift_signature or "")[:16]
            n_findings = len(v.findings)
            lines.append(
                f"  • {outcome:<22} severity={severity:<8} "
                f"findings={n_findings} sig={sig}"
            )
        except Exception:  # noqa: BLE001 — defensive
            lines.append("  • (corrupt verdict — skipped)")
    lines.append("")
    return CoherenceDispatchResult(ok=True, text="\n".join(lines))


def _render_advisories(limit: int) -> CoherenceDispatchResult:
    try:
        advisories = read_coherence_advisories(limit=limit)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[coherence_repl] _render_advisories raised: %s", exc,
        )
        advisories = ()
    lines: List[str] = [
        f"/coherence advisories — last {limit} tightening "
        f"advisories ({len(advisories)} found)",
        "",
    ]
    if not advisories:
        lines.append("  (no advisories yet)")
        lines.append("")
        return CoherenceDispatchResult(
            ok=True, text="\n".join(lines),
        )
    for a in advisories[-limit:]:
        try:
            kind = a.drift_kind.value
            action = a.action.value
            sig = (a.advisory_id or "")[:16]
            lines.append(
                f"  • {kind:<28} action={action:<24} id={sig}"
            )
        except Exception:  # noqa: BLE001 — defensive
            lines.append("  • (corrupt advisory — skipped)")
    lines.append("")
    return CoherenceDispatchResult(ok=True, text="\n".join(lines))


# ---------------------------------------------------------------------------
# /help auto-discovery
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/coherence`` verb. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/coherence",
            one_line=(
                "Coherence-auditor flags, budgets, drift verdicts "
                "+ tightening advisories (Priority #1)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[coherence_repl] register_verbs swallowed",
            exc_info=True,
        )
        return 0


__all__ = [
    "CoherenceDispatchResult",
    "dispatch_coherence_command",
    "register_verbs",
]
