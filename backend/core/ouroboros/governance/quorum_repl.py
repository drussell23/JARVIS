"""Slice 5b E — ``/quorum`` REPL dispatcher.

Operator-facing CLI surface mirroring ``/probe`` + ``/coherence``
(Slice 5b E companions). Consumes the SAME readers that the
Slice 5b C HTTP routes consume (see
:mod:`generative_quorum_observability`).

Subcommands:

  * ``/quorum``               — alias for ``/quorum status``
  * ``/quorum status``        — schemas + flags + config + history
    size + recent stability score
  * ``/quorum config``        — full env-knob snapshot
  * ``/quorum history [N]``   — last N StampedQuorumRun records
    (default 20)
  * ``/quorum stats [N]``     — derived adaptive insights over the
    last N runs (default :func:`quorum_recent_stats_window`)
  * ``/quorum outcomes``      — closed enum vocabulary
  * ``/quorum help``          — usage listing (always available)

Master gate: :func:`quorum_enabled` (Slice 1 primitive flag).
Auto-discovered by :func:`help_dispatcher._discover_module_provided_-
verbs`. NEVER raises.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + verification.generative_quorum* modules ONLY.
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

from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
    ConsensusOutcome,
    GENERATIVE_QUORUM_SCHEMA_VERSION,
    agreement_threshold,
    quorum_enabled,
    quorum_k,
)
from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
    QuorumActionMapping,
    quorum_gate_enabled,
)
from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
    compute_recent_quorum_stats,
    quorum_history_max_records,
    quorum_observer_enabled,
    quorum_recent_stats_window,
    read_quorum_history,
)
from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
    EVENT_TYPE_QUORUM_OUTCOME,
)

logger = logging.getLogger(__name__)


_HELP = (
    "/quorum — Generative Quorum (Move 6) console surface\n"
    "\n"
    "Subcommands:\n"
    "  /quorum                 alias for /quorum status\n"
    "  /quorum status          flags + config + recent stability\n"
    "  /quorum config          env-knob snapshot\n"
    "  /quorum history [N]     last N consensus runs (default 20)\n"
    "  /quorum stats [N]       derived insights over last N runs\n"
    "  /quorum outcomes        closed enum vocabulary\n"
    "  /quorum help            this text\n"
    "\n"
    "Master flag: JARVIS_GENERATIVE_QUORUM_ENABLED\n"
    "Live HTTP surface: GET /observability/quorum[/...]\n"
)

_DEFAULT_LIMIT: int = 20
_MAX_LIMIT: int = 200


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuorumDispatchResult:
    """Result of a ``/quorum`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/quorum`` invocation at all."""

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
    return s == "/quorum" or s == "quorum" or (
        s.startswith("/quorum ") or s.startswith("quorum ")
    )


def _parse_limit(args: List[str]) -> Optional[int]:
    """Parse optional [N] argument. ``None`` means caller should
    use the underlying default (``quorum_recent_stats_window`` or
    ``_DEFAULT_LIMIT``)."""
    if len(args) < 2:
        return None
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > _MAX_LIMIT:
            return _MAX_LIMIT
        return n
    except (TypeError, ValueError):
        return None


def dispatch_quorum_command(line: str) -> QuorumDispatchResult:
    """Parse a ``/quorum`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return QuorumDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return QuorumDispatchResult(
            ok=False, text=f"  /quorum parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    if head in ("help", "?"):
        return QuorumDispatchResult(ok=True, text=_HELP)

    if not quorum_enabled():
        return QuorumDispatchResult(
            ok=False,
            text=(
                "  /quorum: GenerativeQuorum disabled — set "
                "JARVIS_GENERATIVE_QUORUM_ENABLED=true"
            ),
        )

    if head == "status":
        return _render_status()
    if head == "config":
        return _render_config()
    if head == "history":
        return _render_history(_parse_limit(args) or _DEFAULT_LIMIT)
    if head == "stats":
        return _render_stats(_parse_limit(args))
    if head == "outcomes":
        return _render_outcomes()
    return QuorumDispatchResult(
        ok=False,
        text=(
            f"  /quorum: unknown subcommand {head!r}. "
            f"Try /quorum help."
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


def _safe_history_size() -> int:
    try:
        return len(
            read_quorum_history(
                limit=quorum_history_max_records(),
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0


def _render_status() -> QuorumDispatchResult:
    stats = _safe_call(compute_recent_quorum_stats)
    sample_size = (
        getattr(stats, "sample_size", 0)
        if stats is not None else 0
    )
    stability_score = (
        getattr(stats, "stability_score", 0.0)
        if stats is not None else 0.0
    )
    actionable_score = (
        getattr(stats, "actionable_score", 0.0)
        if stats is not None else 0.0
    )
    lines: List[str] = [
        "/quorum — Generative Quorum status",
        "",
        f"  schema_version          {GENERATIVE_QUORUM_SCHEMA_VERSION}",  # noqa: E501
        f"  quorum_enabled          {quorum_enabled()}",
        f"  quorum_gate_enabled     {quorum_gate_enabled()}",
        f"  quorum_observer_enabled {quorum_observer_enabled()}",
        f"  k                       {_safe_call(quorum_k)}",
        f"  agreement_threshold     {_safe_call(agreement_threshold)}",  # noqa: E501
        f"  history_max_records     {_safe_call(quorum_history_max_records)}",  # noqa: E501
        f"  history_size            {_safe_history_size()}",
        "",
        f"  Recent (n={sample_size}):",
        f"    stability_score        {stability_score:.3f}  "
        f"(unanimous CONSENSUS fraction)",
        f"    actionable_score       {actionable_score:.3f}  "
        f"(CONSENSUS+MAJORITY fraction)",
        "",
        f"  sse_event_type          {EVENT_TYPE_QUORUM_OUTCOME}",
        "",
    ]
    return QuorumDispatchResult(ok=True, text="\n".join(lines))


def _render_config() -> QuorumDispatchResult:
    lines: List[str] = [
        "/quorum config — env-knob snapshot",
        "",
        f"  k                            {_safe_call(quorum_k)}",
        f"  agreement_threshold          {_safe_call(agreement_threshold)}",  # noqa: E501
        f"  history_max_records          {_safe_call(quorum_history_max_records)}",  # noqa: E501
        f"  recent_stats_window          {_safe_call(quorum_recent_stats_window)}",  # noqa: E501
        f"  consensus_outcomes           {[o.value for o in ConsensusOutcome]}",  # noqa: E501
        f"  action_mappings              {[a.value for a in QuorumActionMapping]}",  # noqa: E501
        "",
    ]
    return QuorumDispatchResult(ok=True, text="\n".join(lines))


def _render_history(limit: int) -> QuorumDispatchResult:
    try:
        history = read_quorum_history(limit=limit)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[quorum_repl] _render_history raised: %s", exc,
        )
        history = ()
    lines: List[str] = [
        f"/quorum history — last {limit} consensus runs "
        f"({len(history)} found)",
        "",
    ]
    if not history:
        lines.append("  (no runs yet)")
        lines.append("")
        return QuorumDispatchResult(ok=True, text="\n".join(lines))
    for stamped in history[-limit:]:
        try:
            run = stamped.run
            verdict = (
                run.get("verdict")
                if isinstance(run, dict) else None
            )
            outcome = (
                str(verdict.get("outcome"))
                if isinstance(verdict, dict) else "?"
            )
            agree = (
                verdict.get("agreement_count")
                if isinstance(verdict, dict) else "?"
            )
            total = (
                verdict.get("total_rolls")
                if isinstance(verdict, dict) else "?"
            )
            elapsed = (
                run.get("elapsed_seconds", 0.0)
                if isinstance(run, dict) else 0.0
            )
            op_id = (stamped.op_id or "")[:24]
            lines.append(
                f"  • {outcome:<22} {agree}/{total}  "
                f"elapsed={elapsed:>5.2f}s  op={op_id}"
            )
        except Exception:  # noqa: BLE001 — defensive
            lines.append("  • (corrupt run — skipped)")
    lines.append("")
    return QuorumDispatchResult(ok=True, text="\n".join(lines))


def _render_stats(limit: Optional[int]) -> QuorumDispatchResult:
    try:
        stats = compute_recent_quorum_stats(limit=limit)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[quorum_repl] _render_stats raised: %s", exc,
        )
        stats = compute_recent_quorum_stats(limit=0)
    lines: List[str] = [
        f"/quorum stats — derived insights "
        f"(sample_size={stats.sample_size})",
        "",
    ]
    if stats.sample_size == 0:
        lines.append("  (no runs yet — observer needs evidence)")
        lines.append("")
        return QuorumDispatchResult(ok=True, text="\n".join(lines))
    lines.extend([
        f"  stability_score             {stats.stability_score:.3f}",  # noqa: E501
        f"  actionable_score            {stats.actionable_score:.3f}",  # noqa: E501
        f"  avg_elapsed_seconds         {stats.avg_elapsed_seconds:.3f}",  # noqa: E501
        f"  avg_agreement_count         {stats.avg_agreement_count:.2f}",  # noqa: E501
        f"  avg_distinct_signatures     {stats.avg_distinct_signatures:.2f}",  # noqa: E501
        f"  avg_failed_roll_fraction    {stats.avg_failed_roll_fraction:.3f}",  # noqa: E501
        "",
        "  Outcome distribution:",
    ])
    for outcome, count in sorted(
        stats.outcome_distribution.items(),
    ):
        lines.append(
            f"    {outcome:<24} {count}"
        )
    if stats.most_recent_outcome is not None:
        lines.extend([
            "",
            f"  most_recent_outcome         {stats.most_recent_outcome}",  # noqa: E501
            f"  most_recent_op_id           {stats.most_recent_op_id}",  # noqa: E501
            f"  most_recent_signature       {stats.most_recent_signature}",  # noqa: E501
        ])
    lines.append("")
    return QuorumDispatchResult(ok=True, text="\n".join(lines))


def _render_outcomes() -> QuorumDispatchResult:
    lines: List[str] = [
        "/quorum outcomes — closed enum vocabulary",
        "",
        "  ConsensusOutcome:",
    ]
    for o in ConsensusOutcome:
        lines.append(f"    • {o.value}")
    lines.append("")
    lines.append("  QuorumActionMapping:")
    for a in QuorumActionMapping:
        lines.append(f"    • {a.value}")
    lines.extend([
        "",
        f"  sse_event_type              {EVENT_TYPE_QUORUM_OUTCOME}",
        "",
    ])
    return QuorumDispatchResult(ok=True, text="\n".join(lines))


# ---------------------------------------------------------------------------
# /help auto-discovery
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/quorum`` verb. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/quorum",
            one_line=(
                "Generative-quorum status, run history, and "
                "derived stability score (Move 6)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[quorum_repl] register_verbs swallowed",
            exc_info=True,
        )
        return 0


__all__ = [
    "QuorumDispatchResult",
    "dispatch_quorum_command",
    "register_verbs",
]
