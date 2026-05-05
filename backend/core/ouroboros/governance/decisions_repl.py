"""Upgrade 2 (PRD §31.3) Slice 3 — ``/decisions`` REPL
dispatcher.

Operator-facing CLI surface — parallel to :mod:`budget_repl`
(Upgrade 1) / :mod:`curiosity_repl` (M9) / :mod:`outcomes_repl`
(M11). Same patterns: ``register_verbs`` for ``/help`` auto-
discovery, lazy substrate import, frozen
``DecisionsReplDispatchResult``.

Subcommands:

  * ``/decisions``                 — alias for
    ``/decisions recent``
  * ``/decisions recent [N]``      — most-recent N records
    across all sessions (default 20, max 200)
  * ``/decisions session <id> [N]`` — last N records of one
    session
  * ``/decisions sessions``         — list available session ids
    sorted by mtime descending
  * ``/decisions kind <K>``         — records whose kind matches
    K (filter applied to most-recent across all sessions)
  * ``/decisions count [<id>]``     — kind histogram (per-session
    or aggregate across all sessions)
  * ``/decisions help``             — usage listing (always
    available; bypasses master-flag gate)

Master gate: :func:`decision_runtime.ledger_enabled`. Auto-
discovered by :func:`help_dispatcher._discover_module_provided_-
verbs`. NEVER raises.

Authority invariants (AST-pinned at Slice 5):

  * Imports stdlib + ``determinism.decisions_reader`` +
    ``determinism.decision_kinds`` +
    ``determinism.decision_runtime`` (master flag) ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor /
    sensor_governor / strategic_direction.
  * **READ-ONLY** — no subcommand mutates the ledger. The
    AST-pinned invariant at Slice 5 enforces no
    ``record(`` / ``write(`` / ``delete(`` calls in source.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


_HELP = (
    "/decisions — DecisionRecord ledger "
    "(Upgrade 2 / PRD §31.3)\n"
    "\n"
    "Subcommands:\n"
    "  /decisions                       alias for "
    "/decisions recent\n"
    "  /decisions recent [N]            top-N most-recent "
    "records (default 20, max 200)\n"
    "  /decisions session <id> [N]      last N records of "
    "one session (default 50)\n"
    "  /decisions sessions              list available "
    "session ids (newest first)\n"
    "  /decisions kind <K>              records of kind K "
    "(see /decisions help for vocab)\n"
    "  /decisions count [<id>]          kind histogram "
    "(per-session or aggregate)\n"
    "  /decisions help                  this text\n"
    "\n"
    "DecisionKind vocabulary: route_selection, gate_pass, "
    "gate_fail, validator_pass,\n"
    "  validator_fail, risk_escalation, probe_trigger, "
    "sbt_trigger, auto_action_proposal,\n"
    "  approval_request, phase_transition, disabled\n"
    "\n"
    "Master flag: JARVIS_DETERMINISM_LEDGER_ENABLED "
    "(graduated default-TRUE Phase 1 Slice 1.5)\n"
    "Live HTTP surface: GET /observability/decisions"
    "[/session/{id}]\n"
    "Live SSE event:    decision_drift_detected\n"
    "Replay CLI:        scripts/replay_determinism.py "
    "--session <id>\n"
)


_DEFAULT_RECENT_LIMIT: int = 20
_MAX_RECENT_LIMIT: int = 200
_DEFAULT_SESSION_LIMIT: int = 50


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionsReplDispatchResult:
    """Result of a ``/decisions`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/decisions`` invocation (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Master flag — defers to existing decision_runtime.ledger_enabled
# ---------------------------------------------------------------------------


def _master_enabled() -> bool:
    """Defers to the existing
    :func:`decision_runtime.ledger_enabled` (no parallel flag).
    NEVER raises — falls through to False on any import
    failure."""
    try:
        from backend.core.ouroboros.governance.determinism.decision_runtime import (  # noqa: E501
            ledger_enabled,
        )
        return bool(ledger_enabled())
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/decisions"
        or s == "decisions"
        or s.startswith("/decisions ")
        or s.startswith("decisions ")
    )


def _parse_limit(args, *, default, ceiling):
    """Parse limit from the ``args[1]`` slot. Falls through to
    default on parse failure / out-of-bounds."""
    if len(args) < 2:
        return default
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > ceiling:
            return ceiling
        return n
    except (TypeError, ValueError):
        return default


def dispatch_decisions_command(
    line: str,
) -> DecisionsReplDispatchResult:
    """Parse a ``/decisions`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return DecisionsReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return DecisionsReplDispatchResult(
            ok=False,
            text=f"  /decisions parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "recent")

    if head in ("help", "?"):
        return DecisionsReplDispatchResult(ok=True, text=_HELP)

    if not _master_enabled():
        return DecisionsReplDispatchResult(
            ok=False,
            text=(
                "  /decisions: DecisionRecord ledger disabled "
                "— set JARVIS_DETERMINISM_LEDGER_ENABLED=true"
            ),
        )

    if head == "recent":
        return _render_recent(
            _parse_limit(
                args,
                default=_DEFAULT_RECENT_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "session":
        if len(args) < 2:
            return DecisionsReplDispatchResult(
                ok=False,
                text=(
                    "  /decisions session <id>: missing "
                    "session_id argument."
                ),
            )
        # Limit slot is args[2] (after <id>) for session subcmd
        limit = _DEFAULT_SESSION_LIMIT
        if len(args) >= 3:
            try:
                v = int(args[2])
                if v >= 1:
                    limit = min(v, _MAX_RECENT_LIMIT)
            except (TypeError, ValueError):
                pass
        return _render_session(args[1], limit)
    if head == "sessions":
        return _render_sessions()
    if head == "kind":
        if len(args) < 2:
            return DecisionsReplDispatchResult(
                ok=False,
                text=(
                    "  /decisions kind <K>: missing kind "
                    "argument."
                ),
            )
        return _render_kind(
            args[1],
            _parse_limit(
                args[1:],  # consume kind so [N] sits at args[2]
                default=_DEFAULT_RECENT_LIMIT,
                ceiling=_MAX_RECENT_LIMIT,
            ),
        )
    if head == "count":
        if len(args) >= 2:
            return _render_count_per_session(args[1])
        return _render_count_aggregate()
    return DecisionsReplDispatchResult(
        ok=False,
        text=(
            f"  /decisions: unknown subcommand {head!r}. "
            f"Try /decisions help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_record_one_line(
    session_id: str, rec: dict,
) -> str:
    rid = (rec.get("record_id", "") or "")[:18]
    op = (rec.get("op_id", "") or "")[:18]
    phase = (rec.get("phase", "") or "")[:10]
    kind = (rec.get("kind", "") or "")[:24]
    return (
        f"  {session_id[:18]:<18}  {rid:<18}  op={op:<18}  "
        f"phase={phase:<10}  kind={kind}"
    )


def _render_recent(limit: int) -> DecisionsReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            recent_records_across_sessions,
        )
        recent = recent_records_across_sessions(limit=limit)
    except Exception:  # noqa: BLE001 — defensive
        recent = ()
    if not recent:
        return DecisionsReplDispatchResult(
            ok=True,
            text=(
                "/decisions recent — no records found.\n"
                "  hint: ledger lives at "
                ".jarvis/determinism/<session>/decisions.jsonl"
            ),
        )
    lines = [
        f"/decisions recent — {len(recent)} record(s) "
        f"across most-recent sessions",
        "",
    ]
    for sid, rec in recent:
        try:
            lines.append(_format_record_one_line(sid, rec))
        except Exception:  # noqa: BLE001 — defensive
            lines.append("  <projection_failed>")
    return DecisionsReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_session(
    session_id: str, limit: int,
) -> DecisionsReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            read_records_for_session,
        )
        result = read_records_for_session(
            session_id, limit=limit,
        )
    except Exception:  # noqa: BLE001 — defensive
        return DecisionsReplDispatchResult(
            ok=False,
            text=(
                f"  /decisions session: read failed for "
                f"{session_id!r}"
            ),
        )
    if (
        not result.records
        and result.total_records_in_file == 0
    ):
        diag = (
            "; ".join(result.diagnostics)
            if result.diagnostics
            else "no records"
        )
        return DecisionsReplDispatchResult(
            ok=False,
            text=(
                f"  /decisions session: {session_id!r} "
                f"unreadable ({diag})"
            ),
        )
    lines = [
        f"/decisions session {session_id} — "
        f"{len(result.records)}/{result.total_records_in_file} "
        f"records",
        "",
    ]
    for rec in result.records:
        try:
            lines.append(
                _format_record_one_line(session_id, rec),
            )
        except Exception:  # noqa: BLE001 — defensive
            lines.append("  <projection_failed>")
    return DecisionsReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_sessions() -> DecisionsReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            list_available_sessions,
        )
        sessions = list_available_sessions()
    except Exception:  # noqa: BLE001 — defensive
        sessions = ()
    if not sessions:
        return DecisionsReplDispatchResult(
            ok=True,
            text=(
                "/decisions sessions — no sessions found "
                "under .jarvis/determinism/"
            ),
        )
    lines = [
        f"/decisions sessions — {len(sessions)} session(s)",
        "",
    ]
    for s in sessions:
        lines.append(
            f"  {s.session_id:<32}  "
            f"records~{s.record_count_estimate:<5}  "
            f"size={s.file_size_bytes // 1024:>5}KB",
        )
    return DecisionsReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_kind(
    kind: str, limit: int,
) -> DecisionsReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            recent_records_across_sessions,
        )
        results = recent_records_across_sessions(
            limit=limit, kind_filter=kind,
        )
    except Exception:  # noqa: BLE001 — defensive
        results = ()
    if not results:
        return DecisionsReplDispatchResult(
            ok=True,
            text=(
                f"/decisions kind {kind} — no matching "
                f"records"
            ),
        )
    lines = [
        f"/decisions kind {kind} — {len(results)} record(s)",
        "",
    ]
    for sid, rec in results:
        lines.append(_format_record_one_line(sid, rec))
    return DecisionsReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_count_per_session(
    session_id: str,
) -> DecisionsReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            aggregate_kinds_for_session,
        )
        agg = aggregate_kinds_for_session(session_id)
    except Exception:  # noqa: BLE001 — defensive
        agg = ()
    if not agg:
        return DecisionsReplDispatchResult(
            ok=True,
            text=(
                f"/decisions count {session_id} — no "
                f"records / unreadable"
            ),
        )
    total = sum(e.count for e in agg)
    lines = [
        f"/decisions count {session_id} — {total} "
        f"total record(s) across {len(agg)} kind(s)",
        "",
    ]
    for entry in agg:
        lines.append(
            f"  {entry.kind:<28}  {entry.count}",
        )
    return DecisionsReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_count_aggregate() -> DecisionsReplDispatchResult:
    """Cross-session histogram — sum counts across all
    available sessions."""
    try:
        from backend.core.ouroboros.governance.determinism.decisions_reader import (  # noqa: E501
            aggregate_kinds_for_session,
            list_available_sessions,
        )
        sessions = list_available_sessions(limit=100)
        kind_counts = {}
        for s in sessions:
            for e in aggregate_kinds_for_session(s.session_id):
                kind_counts[e.kind] = (
                    kind_counts.get(e.kind, 0) + e.count
                )
    except Exception:  # noqa: BLE001 — defensive
        kind_counts = {}
        sessions = ()
    if not kind_counts:
        return DecisionsReplDispatchResult(
            ok=True,
            text=(
                "/decisions count — no records across any "
                "session"
            ),
        )
    total = sum(kind_counts.values())
    lines = [
        f"/decisions count — {total} record(s) across "
        f"{len(sessions)} session(s) / {len(kind_counts)} "
        f"kind(s)",
        "",
    ]
    for kind, count in sorted(
        kind_counts.items(), key=lambda x: (-x[1], x[0]),
    ):
        lines.append(f"  {kind:<28}  {count}")
    return DecisionsReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# /help auto-discovery
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/decisions`` verb. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/decisions",
            one_line=(
                "DecisionRecord ledger: recent / per-session / "
                "kind histogram queries (Upgrade 2 / PRD §31.3)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[decisions_repl] register_verbs swallowed",
            exc_info=True,
        )
        return 0


__all__ = [
    "DecisionsReplDispatchResult",
    "dispatch_decisions_command",
    "register_verbs",
]
