"""M10 ArchitectureProposer (PRD §32.4) Slice 5 — ``/m10`` REPL
dispatcher.

Operator-facing CLI surface — parallel to :mod:`decisions_repl`
(Upgrade 2) / :mod:`curiosity_repl` (M9). Same patterns:
``register_verbs`` for ``/help`` auto-discovery, lazy substrate
import, frozen :class:`M10ReplDispatchResult`.

Subcommands:

  * ``/m10``                — alias for ``/m10 pending``
  * ``/m10 pending [N]``    — proposals awaiting approval/merge
  * ``/m10 show <id>``      — most-recent state for one proposal
  * ``/m10 history [N]``    — most-recent N ledger rows
    (default 20, max 200)
  * ``/m10 stats``          — phase histogram across all
    proposals
  * ``/m10 help``           — usage listing (always available;
    bypasses master-flag gate)

Master gate: :func:`primitives.m10_arch_proposer_enabled`
(JARVIS_M10_ARCH_PROPOSER_ENABLED — default-FALSE per
§30.5.2 operator binding).

Authority invariants (AST-pinned at Slice 5):

  * Imports stdlib + ``m10.proposal_store`` + ``m10.primitives``
    ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor /
    sensor_governor / strategic_direction /
    graduation_orchestrator / m10.proposal_synthesizer /
    m10.lifecycle / m10.unhandled_pattern_miner.
  * **READ-ONLY** — no subcommand mutates the ledger. AST-pin
    enforces no ``append_proposal`` / ``write`` / ``delete``
    calls in source.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


_HELP = (
    "/m10 — ArchitectureProposer ledger "
    "(M10 / PRD §32.4)\n"
    "\n"
    "Subcommands:\n"
    "  /m10                       alias for /m10 pending\n"
    "  /m10 pending [N]           proposals in awaiting_approval"
    " / awaiting_merge (default 20)\n"
    "  /m10 show <id>             most-recent state for one "
    "proposal\n"
    "  /m10 history [N]           most-recent N ledger rows "
    "(default 20, max 200)\n"
    "  /m10 stats                 phase histogram across all "
    "proposals\n"
    "  /m10 fire                  operator-initiated mining "
    "cycle (Slice 1; persists DETECTING records)\n"
    "  /m10 sweep                 (Slice 3) poll pending PRs; "
    "transition merged/closed to graduated/rejected\n"
    "  /m10 expire                (Slice 3) expire stale "
    "AWAITING_APPROVAL proposals (>"
    "JARVIS_M10_APPROVAL_TIMEOUT_S)\n"
    "  /m10 help                  this text\n"
    "\n"
    "Phases: detecting, synthesizing, validating, committing, "
    "pushing, awaiting_approval,\n"
    "  awaiting_merge, graduated, failed, decided_skip, "
    "rejected, expired, push_failed, ...\n"
    "\n"
    "Kinds: new_sensor, new_phase, new_observer, "
    "new_flag_family, disabled\n"
    "\n"
    "Master flag: JARVIS_M10_ARCH_PROPOSER_ENABLED "
    "(default-FALSE — operator-pinned per §30.5.2)\n"
    "Live HTTP surface: GET /observability/m10[/proposal/{id}]\n"
    "Live SSE event:    m10_proposal_emitted\n"
)


_DEFAULT_HISTORY_LIMIT: int = 20
_MAX_HISTORY_LIMIT: int = 200
_DEFAULT_PENDING_LIMIT: int = 20


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class M10ReplDispatchResult:
    """Result of a ``/m10`` dispatch. Frozen for safe propagation.
    ``matched=False`` signals the line wasn't a ``/m10``
    invocation (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Master flag — defers to existing primitives.m10_arch_proposer_enabled
# ---------------------------------------------------------------------------


def _master_enabled() -> bool:
    """Defers to the existing
    :func:`primitives.m10_arch_proposer_enabled` (no parallel
    flag). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.m10.primitives import (
            m10_arch_proposer_enabled,
        )
        return bool(m10_arch_proposer_enabled())
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
        s == "/m10"
        or s == "m10"
        or s.startswith("/m10 ")
        or s.startswith("m10 ")
    )


def _parse_limit(args, *, default, ceiling):
    """Parse limit from ``args[1]``. Falls through to default
    on parse failure / out-of-bounds."""
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


def dispatch_m10_command(line: str) -> M10ReplDispatchResult:
    """Parse a ``/m10`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return M10ReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return M10ReplDispatchResult(
            ok=False,
            text=f"  /m10 parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "pending")

    if head in ("help", "?"):
        return M10ReplDispatchResult(ok=True, text=_HELP)

    if not _master_enabled():
        return M10ReplDispatchResult(
            ok=False,
            text=(
                "  /m10: M10 ArchitectureProposer disabled — "
                "set JARVIS_M10_ARCH_PROPOSER_ENABLED=true "
                "(default-FALSE per §30.5.2 — requires 30+ "
                "proposal-acceptance audit before graduation)"
            ),
        )

    if head == "pending":
        return _render_pending(
            _parse_limit(
                args,
                default=_DEFAULT_PENDING_LIMIT,
                ceiling=_MAX_HISTORY_LIMIT,
            ),
        )
    if head == "show":
        if len(args) < 2:
            return M10ReplDispatchResult(
                ok=False,
                text=(
                    "  /m10 show <id>: missing proposal_id "
                    "argument."
                ),
            )
        return _render_show(args[1])
    if head == "history":
        return _render_history(
            _parse_limit(
                args,
                default=_DEFAULT_HISTORY_LIMIT,
                ceiling=_MAX_HISTORY_LIMIT,
            ),
        )
    if head == "stats":
        return _render_stats()
    if head == "fire":
        return _render_fire()
    if head == "sweep":
        return _render_sweep()
    if head == "expire":
        return _render_expire()
    return M10ReplDispatchResult(
        ok=False,
        text=(
            f"  /m10: unknown subcommand {head!r}. Try "
            f"/m10 help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_proposal_one_line(rec) -> str:
    pid = (rec.proposal_id or "")[:24]
    kind = (rec.kind or "")[:18]
    phase = (rec.phase or "")[:18]
    pin = (rec.proposed_ast_pin_name or "")[:24]
    return (
        f"  {pid:<24}  kind={kind:<18}  "
        f"phase={phase:<18}  pin={pin}"
    )


def _render_pending(limit: int) -> M10ReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (
            list_pending_proposals,
        )
        rows = list_pending_proposals(limit=limit)
    except Exception:  # noqa: BLE001 — defensive
        rows = ()
    if not rows:
        return M10ReplDispatchResult(
            ok=True,
            text=(
                "/m10 pending — no proposals awaiting "
                "approval/merge.\n"
                "  hint: ledger lives at "
                ".jarvis/m10/proposals.jsonl"
            ),
        )
    lines = [
        f"/m10 pending — {len(rows)} proposal(s) awaiting "
        f"operator action",
        "",
    ]
    for r in rows:
        lines.append(_format_proposal_one_line(r))
        if r.pr_url:
            lines.append(f"    PR: {r.pr_url}")
    return M10ReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_show(proposal_id: str) -> M10ReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (
            find_proposal_by_id,
        )
        found = find_proposal_by_id(proposal_id)
    except Exception:  # noqa: BLE001 — defensive
        return M10ReplDispatchResult(
            ok=False,
            text=(
                f"  /m10 show: read failed for "
                f"{proposal_id!r}"
            ),
        )
    if found is None:
        return M10ReplDispatchResult(
            ok=False,
            text=(
                f"  /m10 show: proposal {proposal_id!r} not "
                f"found in ledger"
            ),
        )
    lines = [
        f"/m10 show {proposal_id}",
        "",
        f"  kind:                 {found.kind}",
        f"  phase:                {found.phase}",
        f"  pattern_signature:    {found.pattern_signature}",
        f"  proposed_module_path: {found.proposed_module_path}",
        f"  proposed_class_name:  {found.proposed_class_name}",
        (
            f"  proposed_ast_pin:     "
            f"{found.proposed_ast_pin_name}"
        ),
        f"  consensus_signature:  {found.consensus_signature}",
        f"  cost_usd:             {found.cost_usd:.4f}",
        f"  pr_branch:            {found.pr_branch}",
        f"  pr_url:               {found.pr_url}",
        f"  failure_reason:       {found.failure_reason}",
        (
            f"  last_updated_unix:    "
            f"{found.last_updated_at_unix:.1f}"
        ),
    ]
    if found.detection_evidence:
        lines.append("")
        lines.append("  detection_evidence:")
        for e in found.detection_evidence[:20]:
            lines.append(f"    - {e}")
    return M10ReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_history(limit: int) -> M10ReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (
            read_all_proposals,
        )
        rows = read_all_proposals(limit=limit)
    except Exception:  # noqa: BLE001 — defensive
        rows = ()
    if not rows:
        return M10ReplDispatchResult(
            ok=True,
            text=(
                "/m10 history — no proposals in ledger.\n"
                "  hint: ledger lives at "
                ".jarvis/m10/proposals.jsonl"
            ),
        )
    lines = [
        f"/m10 history — {len(rows)} most-recent row(s)",
        "",
    ]
    for r in rows:
        lines.append(_format_proposal_one_line(r))
    return M10ReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_fire() -> M10ReplDispatchResult:
    """Operator-initiated mining cycle.

    Composes the producer-bridge. When
    ``JARVIS_M10_BRIDGE_FULL_LIFECYCLE_ENABLED=true`` (Slice 2),
    routes through ``fire_full_lifecycle_cycle_sync`` which
    runs mine → synthesize → advance. Otherwise (Slice 1
    behavior), routes through ``fire_mining_cycle_sync`` which
    only persists DETECTING records.

    NEVER raises — the bridge wraps everything in structured
    results."""
    try:
        from backend.core.ouroboros.governance.m10.m10_producer_bridge import (  # noqa: E501
            fire_full_lifecycle_cycle_sync,
            fire_mining_cycle_sync,
            full_lifecycle_enabled,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return M10ReplDispatchResult(
            ok=False,
            text=(
                f"  /m10 fire: bridge import failed: "
                f"{type(exc).__name__}"
            ),
        )

    if full_lifecycle_enabled():
        return _render_full_lifecycle_fire(
            fire_full_lifecycle_cycle_sync(),
        )
    return _render_mining_only_fire(fire_mining_cycle_sync())


def _render_mining_only_fire(result: Any) -> M10ReplDispatchResult:
    """Slice 1 renderer — DETECTING records only."""
    lines = [
        f"/m10 fire — outcome={result.outcome} (Slice 1: mining only)",
        f"  ok:                      {result.ok}",
        f"  proposals_emitted_count: {result.proposals_emitted_count}",
        f"  rows_stored:             {result.rows_stored}",
        f"  elapsed_s:               {result.elapsed_s:.3f}",
    ]
    if result.proposal_ids:
        lines.append("  proposal_ids:")
        for pid in result.proposal_ids[:10]:
            lines.append(f"    {pid}")
        if len(result.proposal_ids) > 10:
            lines.append(
                f"    ... ({len(result.proposal_ids) - 10} more)"
            )
    if result.diagnostic:
        lines.append(f"  diagnostic: {result.diagnostic[:256]}")
    if not result.ok or result.outcome == "error":
        lines.append("")
        lines.append(
            "  hint: master JARVIS_M10_ARCH_PROPOSER_ENABLED "
            "(default-FALSE); enable Slice 2 with "
            "JARVIS_M10_BRIDGE_FULL_LIFECYCLE_ENABLED=true"
        )
    return M10ReplDispatchResult(
        ok=result.ok or result.outcome == "disabled",
        text="\n".join(lines),
    )


def _render_full_lifecycle_fire(
    result: Any,
) -> M10ReplDispatchResult:
    """Slice 2 renderer — synth + lifecycle advance per record."""
    lines = [
        f"/m10 fire — outcome={result.outcome} (Slice 2: full lifecycle)",
        f"  ok:        {result.ok}",
        f"  elapsed_s: {result.elapsed_s:.3f}",
    ]
    mr = result.mining_result
    if mr is not None:
        lines.append(
            f"  mining:    outcome={mr.outcome} "
            f"emitted={mr.proposals_emitted_count} "
            f"stored={mr.rows_stored}"
        )
    if result.advanced_proposals:
        lines.append("  advanced:")
        for adv in result.advanced_proposals[:10]:
            phase = adv.final_phase or "?"
            pr_suffix = f" pr={adv.pr_url}" if adv.pr_url else ""
            lines.append(
                f"    {adv.proposal_id}  "
                f"synth={adv.synth_verdict}  "
                f"final={phase}"
                f"{pr_suffix}"
            )
        if len(result.advanced_proposals) > 10:
            lines.append(
                f"    ... ({len(result.advanced_proposals) - 10} more)"
            )
    if result.diagnostic:
        lines.append(f"  diagnostic: {result.diagnostic[:256]}")
    return M10ReplDispatchResult(
        ok=bool(result.ok),
        text="\n".join(lines),
    )


def _render_sweep() -> M10ReplDispatchResult:
    """Slice 3 — sweep pending PRs for merge / closure."""
    try:
        from backend.core.ouroboros.governance.m10.cadence_runner import (  # noqa: E501
            sweep_pending_for_merge_sync,
        )
    except Exception as exc:  # noqa: BLE001
        return M10ReplDispatchResult(
            ok=False,
            text=(
                f"  /m10 sweep: cadence_runner import failed: "
                f"{type(exc).__name__}"
            ),
        )
    result = sweep_pending_for_merge_sync()
    return _render_sweep_result("sweep", result)


def _render_expire() -> M10ReplDispatchResult:
    """Slice 3 — expire stale AWAITING_APPROVAL proposals."""
    try:
        from backend.core.ouroboros.governance.m10.cadence_runner import (  # noqa: E501
            expire_stale_pending_sync,
        )
    except Exception as exc:  # noqa: BLE001
        return M10ReplDispatchResult(
            ok=False,
            text=(
                f"  /m10 expire: cadence_runner import failed: "
                f"{type(exc).__name__}"
            ),
        )
    result = expire_stale_pending_sync()
    return _render_sweep_result("expire", result)


def _render_sweep_result(
    verb: str, result: Any,
) -> M10ReplDispatchResult:
    lines = [
        f"/m10 {verb} — ok={result.ok}",
        f"  inspected: {result.inspected_count}",
        f"  elapsed_s: {result.elapsed_s:.3f}",
    ]
    if result.transitions:
        lines.append(f"  transitions ({len(result.transitions)}):")
        for t in result.transitions[:10]:
            lines.append(
                f"    {t.proposal_id}  "
                f"{t.from_phase} → {t.to_phase}  "
                f"({t.reason})"
            )
        if len(result.transitions) > 10:
            lines.append(
                f"    ... ({len(result.transitions) - 10} more)"
            )
    if result.diagnostic:
        lines.append(f"  diagnostic: {result.diagnostic[:256]}")
    return M10ReplDispatchResult(
        ok=bool(result.ok),
        text="\n".join(lines),
    )


def _render_stats() -> M10ReplDispatchResult:
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (
            aggregate_phase_histogram,
        )
        hist = aggregate_phase_histogram()
    except Exception:  # noqa: BLE001 — defensive
        hist = {}
    if not hist:
        return M10ReplDispatchResult(
            ok=True,
            text="/m10 stats — no proposals in ledger.",
        )
    total = sum(hist.values())
    lines = [
        f"/m10 stats — {total} proposal(s) across "
        f"{len(hist)} phase(s)",
        "",
    ]
    for phase, count in sorted(
        hist.items(), key=lambda x: (-x[1], x[0]),
    ):
        lines.append(f"  {phase:<24}  {count}")
    return M10ReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# /help auto-discovery
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/m10`` verb. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/m10",
            one_line=(
                "ArchitectureProposer ledger: pending / show / "
                "history / stats queries (M10 / PRD §32.4)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[m10_repl] register_verbs swallowed",
            exc_info=True,
        )
        return 0


__all__ = [
    "M10ReplDispatchResult",
    "dispatch_m10_command",
    "register_verbs",
]
