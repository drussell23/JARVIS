"""``/proposals`` REPL — §38.11-E operator surface
(PRD v2.68 to v2.69, 2026-05-08).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry``
via §33.3 naming-cage convention.

Subcommands:
  /proposals                         alias for ``panel``
  /proposals panel [N]               last N pending proposals
  /proposals all [N]                  all (incl. terminal) proposals
  /proposals show <proposal-id>      detailed view of one proposal
  /proposals accept <id> [note]      mark ACCEPTED
  /proposals reject <id> [note]      mark REJECTED
  /proposals expire                  sweep stale → EXPIRED
  /proposals status                  master flag + counts
  /proposals help                    this text
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


PROPOSALS_REPL_SCHEMA_VERSION: str = "proposals_repl.1"


_HELP = (
    "/proposals — §38.11-E proactive proposal surface (PRD)\n"
    "\n"
    "Surfaces proposals from the 4 canonical autonomy producers:\n"
    "  - 🔭 CURIOSITY      (proactive_curiosity_reader)\n"
    "  - 🧩 CAPABILITY_GAP (CapabilityGapSensor)\n"
    "  - 💡 OPPORTUNITY    (OpportunityMinerSensor)\n"
    "  - 🏛 ARCHITECTURE   (M10 ArchitectureProposer)\n"
    "\n"
    "Subcommands:\n"
    "  /proposals                     alias for /proposals panel\n"
    "  /proposals panel [N]           last N pending\n"
    "  /proposals all [N]             all (incl. terminal)\n"
    "  /proposals show <id>           detailed view\n"
    "  /proposals accept <id> [note]  mark ACCEPTED\n"
    "  /proposals reject <id> [note]  mark REJECTED\n"
    "  /proposals expire              sweep stale → EXPIRED\n"
    "  /proposals status              master flag + counts\n"
    "  /proposals help                this text\n"
    "\n"
    "Master flag: JARVIS_PROACTIVE_PROPOSAL_ENABLED "
    "(default false).\n"
)


@dataclass(frozen=True)
class ProposalsReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = PROPOSALS_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/proposals"
        or s == "proposals"
        or s.startswith("/proposals ")
        or s.startswith("proposals ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.proactive_proposal_surface import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_proposals_command(
    line: str,
) -> ProposalsReplDispatchResult:
    if not _matches(line):
        return ProposalsReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ProposalsReplDispatchResult(
            ok=False,
            text=f"  /proposals parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "panel")

    if head in ("help", "?"):
        return ProposalsReplDispatchResult(ok=True, text=_HELP)

    if not _master_enabled():
        return ProposalsReplDispatchResult(
            ok=False,
            text=(
                "  /proposals: proactive proposal surface "
                "disabled (default per §33.1). Set "
                "JARVIS_PROACTIVE_PROPOSAL_ENABLED=true."
            ),
        )

    try:
        if head == "panel":
            return _render_panel(
                _parse_int_arg(args, default=8),
                pending_only=True,
            )
        if head == "all":
            return _render_panel(
                _parse_int_arg(args, default=16),
                pending_only=False,
            )
        if head == "show":
            return _render_show(
                args[1] if len(args) >= 2 else "",
            )
        if head == "accept":
            return _decide(
                args, decision="accept",
            )
        if head == "reject":
            return _decide(
                args, decision="reject",
            )
        if head == "expire":
            return _expire_stale()
        if head == "status":
            return _render_status()
        return ProposalsReplDispatchResult(
            ok=False,
            text=(
                f"  /proposals: unknown subcommand "
                f"{head!r}. Try /proposals help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return ProposalsReplDispatchResult(
            ok=False,
            text=(
                f"  /proposals: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _parse_int_arg(args, *, default: int) -> int:
    if len(args) <= 1:
        return default
    try:
        n = int(args[1])
        return max(1, min(n, 64))
    except (TypeError, ValueError):
        return default


def _render_panel(
    limit: int, *, pending_only: bool,
) -> ProposalsReplDispatchResult:
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        format_proposal_panel,
    )
    out = format_proposal_panel(
        limit=limit, pending_only=pending_only,
    )
    if not out:
        scope = "pending" if pending_only else "all"
        return ProposalsReplDispatchResult(
            ok=True,
            text=(
                f"# /proposals — no {scope} proposals "
                f"(producers haven't emitted yet)"
            ),
        )
    return ProposalsReplDispatchResult(ok=True, text=out)


def _render_show(proposal_id: str) -> ProposalsReplDispatchResult:
    if not proposal_id:
        return ProposalsReplDispatchResult(
            ok=False,
            text="  /proposals show: proposal-id required",
        )
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        get_default_ledger,
    )
    p = get_default_ledger().get(proposal_id)
    if p is None:
        return ProposalsReplDispatchResult(
            ok=False,
            text=(
                f"  /proposals show: no proposal with id "
                f"{proposal_id!r}"
            ),
        )
    parts = [
        f"# Proposal {p.proposal_id}",
        f"  kind          : {p.kind.value}",
        f"  signal_source : {p.signal_source}",
        f"  decision      : {p.decision.value}",
        f"  priority_hint : {p.priority_hint:.2f}",
        f"  emitted_at    : {p.emitted_at_unix:.0f}",
        f"  summary       : {p.summary}",
    ]
    if p.rationale:
        parts.append(f"  rationale     : {p.rationale}")
    if p.decided_at_unix is not None:
        parts.append(
            f"  decided_at    : {p.decided_at_unix:.0f}"
        )
    if p.decision_note:
        parts.append(f"  note          : {p.decision_note}")
    return ProposalsReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _decide(
    args, *, decision: str,
) -> ProposalsReplDispatchResult:
    if len(args) < 2:
        return ProposalsReplDispatchResult(
            ok=False,
            text=(
                f"  /proposals {decision}: proposal-id "
                f"required"
            ),
        )
    proposal_id = args[1]
    note = " ".join(args[2:]).strip()
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        accept_proposal, reject_proposal,
    )
    fn = (
        accept_proposal
        if decision == "accept"
        else reject_proposal
    )
    ok = fn(proposal_id, note=note)
    if not ok:
        return ProposalsReplDispatchResult(
            ok=False,
            text=(
                f"  /proposals {decision}: failed (no such "
                f"proposal-id, or already terminal)"
            ),
        )
    label = (
        "ACCEPTED"
        if decision == "accept"
        else "REJECTED"
    )
    return ProposalsReplDispatchResult(
        ok=True,
        text=(
            f"# /proposals {decision} — proposal "
            f"{proposal_id} marked {label}"
            + (f" (note: {note})" if note else "")
        ),
    )


def _expire_stale() -> ProposalsReplDispatchResult:
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        get_default_ledger,
    )
    n = get_default_ledger().expire_stale()
    return ProposalsReplDispatchResult(
        ok=True,
        text=(
            f"# /proposals expire — {n} stale pending "
            f"proposals marked EXPIRED"
        ),
    )


def _render_status() -> ProposalsReplDispatchResult:
    from backend.core.ouroboros.governance.proactive_proposal_surface import (
        ProposalDecision, ProposalKind,
        get_default_ledger, master_enabled,
        panel_enabled, persistence_enabled,
    )
    ledger = get_default_ledger()
    all_p = ledger.all_proposals(limit=512)
    parts = ["# /proposals status"]
    parts.append(f"  master_enabled       : {master_enabled()}")
    parts.append(f"  panel_enabled        : {panel_enabled()}")
    parts.append(
        f"  persistence_enabled  : {persistence_enabled()}"
    )
    parts.append(f"  total proposals      : {len(all_p)}")

    by_kind: dict = {}
    by_decision: dict = {}
    for p in all_p:
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
        by_decision[p.decision] = by_decision.get(p.decision, 0) + 1
    if by_kind:
        parts.append("  by kind:")
        for k in ProposalKind:
            parts.append(f"    {k.value:<14} : {by_kind.get(k, 0)}")
    if by_decision:
        parts.append("  by decision:")
        for d in ProposalDecision:
            parts.append(
                f"    {d.value:<10} : {by_decision.get(d, 0)}"
            )
    return ProposalsReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    try:
        registry.register(
            verb="proposals",
            description=(
                "Proactive proposals — accept/reject "
                "ledger over 4 canonical autonomy producers"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "proposals_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "PROPOSALS_REPL_SCHEMA_VERSION",
    "ProposalsReplDispatchResult",
    "dispatch_proposals_command",
    "register_verbs",
]
