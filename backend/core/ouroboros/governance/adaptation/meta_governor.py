"""RR Pass C Slice 6 (CLOSES Pass C) — MetaAdaptationGovernor + /adapt REPL.

Per `memory/project_reverse_russian_doll_pass_c.md` §10:

  > `adaptation/meta_governor.py` — the single component that:
  >   * Coordinates the 5 adaptive surfaces (Slices 2-5; Slice 1
  >     is substrate)
  >   * Enforces §4.1 monotonic-tightening before any proposal
  >     reaches the AdaptationLedger
  >   * Provides the `/adapt` REPL surface (§4.5)
  >   * Provides the `/observability/adaptations` GET endpoints
  >   * Wires SSE event emission for the 4 adaptation event types
  >   * Owns the weekly background analyzer scheduling (§4.3)

This module CLOSES Pass C by shipping the operator-facing
coordination surface. Mirror of Pass B's order2_repl_dispatcher
pattern: dispatch one /adapt invocation, never raises into the
caller, structured DispatchResult per outcome.

## What's in scope this PR

  * `/adapt {pending,show,approve,reject,history,stats,help}` REPL
    dispatcher — the load-bearing operator surface.
  * Stats aggregator: per-surface pending/approved/rejected counts.
  * Render helpers: pending list, full proposal detail, history,
    stats summary.
  * Auto-stamps the operator on approve/reject (delegates to
    Slice 1 substrate's `approve()` / `reject()`).
  * Composition with all 5 adaptive surfaces preserved through
    the existing surface-validator registry.

## What's deferred to follow-up

Same split-pattern as Pass B's Slice 4 (gate_runner wiring) and
Slice 6.3 (REPL closes the cage but observability + scheduling
are independent). Tracked here, not landing this PR:

  * `register_adaptation_routes(app)` — the `/observability/adaptations`
    GET endpoints (mirror of P5/P4 metrics-observability pattern).
  * SSE event emission for `adaptation_proposed` / `_approved` /
    `_rejected` / `_applied` (4 event types per §4.5).
  * Weekly background analyzer scheduling (§4.3) — currently
    mining pipelines run when the operator calls them; the
    scheduled-analyzer wrapper is its own concern.
  * Integration of approved adaptations into actual gate state
    (`.jarvis/adapted_*.yaml` writers per surface — each surface's
    activation path per §6.3 / §7.3 / §8.4 / §9.3).

The REPL is the cage's structural close: operators can review
+ approve/reject the full mining-output stream from all 5 surfaces.
The "actual gate state mutation on approve" wiring is the natural
follow-up arc (mirrors Pass B's "/order2 amend" → replay executor
split).

## Authority surface

  * Pure composition + REPL parsing. No subprocess, no env
    mutation, no network.
  * Reads from + writes to AdaptationLedger ONLY (the substrate
    handles all persistence + validation).
  * NO imports of orchestrator / policy / iron_gate /
    risk_tier_floor / change_engine / candidate_generator / gate /
    semantic_guardian / semantic_firewall / scoped_tool_backend.
  * Allowed: stdlib + `adaptation.ledger` (substrate). The 4
    adaptive surface modules (Slices 2-5) are NOT imported here —
    each registered its own surface validator at import time;
    this module just consumes the AdaptationLedger surface.
  * Best-effort throughout — every dispatch returns a structured
    :class:`DispatchResult`; never raises into the REPL caller.

## Default-off

`JARVIS_ADAPT_REPL_ENABLED` (default false). When off, every
subcommand EXCEPT `help` short-circuits to MASTER_OFF. `help`
always works (discoverability per the policy adopted across
Pass A graduations).
"""
from __future__ import annotations

import enum
import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, List, Optional, Sequence, Tuple,
)

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    DecisionStatus,
    OperatorDecisionStatus,
    get_default_ledger,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Schema version stamped into DispatchResult.to_dict for downstream
# parsers (mirrors Pass B's order2_repl_dispatcher pattern).
DISPATCH_SCHEMA_VERSION: int = 1

# Bound the operator-supplied reason. Mirrors Pass B's
# MAX_REASON_CHARS_DISPATCH.
MAX_REASON_CHARS_DISPATCH: int = 1_024

# Default + max history limits for the `/adapt history` subcommand.
DEFAULT_HISTORY_LIMIT: int = 20
MAX_HISTORY_LIMIT: int = 500

# Subcommand allowlist — anything else routes to UNKNOWN_SUBCOMMAND.
_VALID_SUBCOMMANDS: Tuple[str, ...] = (
    "pending", "show", "approve", "reject", "history", "stats", "help",
)


def is_enabled() -> bool:
    """Master flag — ``JARVIS_ADAPT_REPL_ENABLED`` (default ``true`` —
    graduated in Move 1 Pass C cadence 2026-04-29).

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy hot-reverts."""
    raw = os.environ.get(
        "JARVIS_ADAPT_REPL_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Move 1 Pass C cadence)
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Status enum + frozen result
# ---------------------------------------------------------------------------


class DispatchStatus(str, enum.Enum):
    """Outcome of one dispatch_adapt call."""

    OK = "OK"
    """Subcommand executed cleanly. Renderable output in
    :attr:`DispatchResult.output`."""

    MASTER_OFF = "MASTER_OFF"
    """JARVIS_ADAPT_REPL_ENABLED is off. (help bypasses this.)"""

    UNKNOWN_SUBCOMMAND = "UNKNOWN_SUBCOMMAND"
    """First arg isn't in the subcommand allowlist."""

    MISSING_PROPOSAL_ID = "MISSING_PROPOSAL_ID"
    """show/approve/reject require a proposal_id arg; none provided."""

    PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
    """No proposal in the ledger for the given proposal_id."""

    NOT_PENDING = "NOT_PENDING"
    """Proposal is in a terminal state (approved/rejected)."""

    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
    """Caller supplied empty operator name on approve/reject."""

    REASON_REQUIRED = "REASON_REQUIRED"
    """Operator supplied empty reason via reader (approve/reject)."""

    LEDGER_REJECTED = "LEDGER_REJECTED"
    """Ledger layer (Slice 1) refused the approve/reject. Detail
    contains the ledger's status."""

    LEDGER_DISABLED = "LEDGER_DISABLED"
    """Ledger master flag (JARVIS_ADAPTATION_LEDGER_ENABLED) is
    off. Even if the REPL is on, no proposals can be read or
    decided. Pass C is fully dark."""

    INVALID_ARGS = "INVALID_ARGS"
    """Subcommand args malformed (e.g. history limit not int)."""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Defensive: unexpected exception. Should never fire."""


@dataclass(frozen=True)
class DispatchResult:
    """One REPL dispatch outcome. Frozen so log consumers can stash
    it without worrying about post-hoc mutation."""

    schema_version: int
    subcommand: str
    status: DispatchStatus
    output: str = ""
    detail: str = ""
    proposal: Optional[AdaptationProposal] = None
    proposals: Tuple[AdaptationProposal, ...] = field(default_factory=tuple)
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is DispatchStatus.OK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "subcommand": self.subcommand,
            "status": self.status.value,
            "output": self.output,
            "detail": self.detail,
            "proposal": (
                self.proposal.to_dict() if self.proposal is not None else None
            ),
            "proposals": [p.to_dict() for p in self.proposals],
            "stats": dict(self.stats),
        }


# ---------------------------------------------------------------------------
# Help banner
# ---------------------------------------------------------------------------


_HELP_BANNER = """\
/adapt — Adaptive Anti-Venom proposal review (RR Pass C)

  /adapt pending                       List pending proposals
  /adapt show <proposal-id>            Render one proposal's full evidence
  /adapt approve <proposal-id>         Apply a proposal (gate state mutates)
  /adapt reject <proposal-id>          Decline a proposal (terminal)
  /adapt history [limit] [--surface N] List recent proposals (any state)
  /adapt stats                         Per-surface pending/approved/rejected
  /adapt help                          This banner

Cage rules:
  * Pass C cannot LOOSEN. Every proposal that reaches this REPL has
    already passed the substrate's monotonic-tightening invariant.
  * Approving a proposal makes the adaptation live (writes
    .jarvis/adapted_<surface>.yaml at the wired surface; gate state
    reads it on next boot).
  * To LOOSEN an applied adaptation, use Pass B's /order2 amend REPL
    (loosening is an Order-2 governance change).

Master flag: JARVIS_ADAPT_REPL_ENABLED (default false until graduation).
"""


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _render_proposal_summary_line(p: AdaptationProposal) -> str:
    """One-line summary for /adapt pending and /adapt history."""
    decision_marker = ""
    if p.operator_decision is OperatorDecisionStatus.APPROVED:
        decision_marker = (
            f" by={p.operator_decision_by or '?'} APPLIED"
        )
    elif p.operator_decision is OperatorDecisionStatus.REJECTED:
        decision_marker = (
            f" by={p.operator_decision_by or '?'} REJECTED"
        )
    return (
        f"[{p.operator_decision.value:9s}] {p.proposal_id:38s} "
        f"surface={p.surface.value:42s} kind={p.proposal_kind:18s} "
        f"obs={p.evidence.observation_count:3d}"
        f"{decision_marker}"
    )


def _render_proposal_full(p: AdaptationProposal) -> str:
    """Operator-readable detail render for /adapt show."""
    lines: List[str] = []
    lines.append(f"=== Adaptation proposal: {p.proposal_id} ===")
    lines.append(f"  Surface:    {p.surface.value}")
    lines.append(f"  Kind:       {p.proposal_kind}")
    lines.append(f"  Status:     {p.operator_decision.value}")
    lines.append(f"  Verdict:    {p.monotonic_tightening_verdict.value}")
    lines.append(f"  Proposed:   {p.proposed_at}")
    lines.append(f"  Schema:     v{p.schema_version}")
    lines.append("")
    lines.append("  --- Evidence ---")
    lines.append(f"  Window:           {p.evidence.window_days}d")
    lines.append(f"  Observation count: {p.evidence.observation_count}")
    if p.evidence.source_event_ids:
        ids = list(p.evidence.source_event_ids[:10])
        more = len(p.evidence.source_event_ids) - len(ids)
        suffix = f" (+{more} more)" if more > 0 else ""
        lines.append(f"  Source events:     {', '.join(ids)}{suffix}")
    if p.evidence.summary:
        lines.append("  Summary:")
        for ln in p.evidence.summary.splitlines()[:10]:
            lines.append(f"    | {ln}")
    lines.append("")
    lines.append("  --- State transition ---")
    lines.append(f"  Current hash:  {p.current_state_hash}")
    lines.append(f"  Proposed hash: {p.proposed_state_hash}")
    if p.operator_decision is not OperatorDecisionStatus.PENDING:
        lines.append("")
        lines.append("  --- Operator decision ---")
        lines.append(
            f"  Decision:    {p.operator_decision.value} "
            f"by {p.operator_decision_by or '?'}"
        )
        lines.append(f"  Decided at:  {p.operator_decision_at}")
        if p.applied_at:
            lines.append(f"  Applied at:  {p.applied_at}")
    return "\n".join(lines)


def _render_pending_list(pending: Sequence[AdaptationProposal]) -> str:
    if not pending:
        return "No pending adaptation proposals."
    lines = [f"Pending adaptation proposals ({len(pending)}):"]
    for p in sorted(pending, key=lambda x: x.proposed_at_epoch):
        lines.append("  " + _render_proposal_summary_line(p))
    return "\n".join(lines)


def _render_history_list(
    history: Sequence[AdaptationProposal],
    surface: Optional[AdaptationSurface] = None,
) -> str:
    if not history:
        if surface is not None:
            return (
                f"No history for surface {surface.value!r}."
            )
        return "No adaptation history."
    suffix = f" (surface={surface.value})" if surface is not None else ""
    lines = [
        f"Adaptation history ({len(history)} entries, newest-first){suffix}:",
    ]
    for p in history:
        lines.append("  " + _render_proposal_summary_line(p))
    return "\n".join(lines)


def compute_stats(
    ledger: AdaptationLedger,
) -> Dict[str, Any]:
    """Aggregate pending/approved/rejected counts per surface +
    overall totals. Pure read; never mutates the ledger.

    Returns a dict shaped:
      {
        "totals": {"pending": int, "approved": int, "rejected": int},
        "per_surface": {
          "<surface_value>": {"pending": int, "approved": int, "rejected": int},
          ...
        },
      }
    """
    totals = {"pending": 0, "approved": 0, "rejected": 0}
    per_surface: Dict[str, Dict[str, int]] = {}
    # Use a large limit to walk the full append-only log; the
    # latest-record-per-proposal-id reduction happens inside
    # ledger.history()'s single-pass.
    history = ledger.history(limit=10_000)
    # Reduce to latest record per proposal_id (substrate's history
    # returns ALL records sorted by epoch desc; we take first-seen
    # per proposal_id since that's the latest by epoch).
    seen: set = set()
    for p in history:
        if p.proposal_id in seen:
            continue
        seen.add(p.proposal_id)
        surface_key = p.surface.value
        per_surface.setdefault(
            surface_key,
            {"pending": 0, "approved": 0, "rejected": 0},
        )
        decision_key = p.operator_decision.value
        if decision_key in totals:
            totals[decision_key] += 1
            per_surface[surface_key][decision_key] += 1
    return {
        "totals": totals,
        "per_surface": per_surface,
    }


def _render_stats(stats: Dict[str, Any]) -> str:
    totals = stats.get("totals", {})
    per_surface = stats.get("per_surface", {})
    lines = ["Adaptation proposal stats:"]
    lines.append(
        f"  Total: pending={totals.get('pending', 0)}, "
        f"approved={totals.get('approved', 0)}, "
        f"rejected={totals.get('rejected', 0)}"
    )
    if per_surface:
        lines.append("  By surface:")
        for surface_key in sorted(per_surface):
            counts = per_surface[surface_key]
            lines.append(
                f"    {surface_key:42s}  "
                f"pending={counts['pending']:3d}  "
                f"approved={counts['approved']:3d}  "
                f"rejected={counts['rejected']:3d}"
            )
    else:
        lines.append("  No proposals on any surface yet.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reader protocol (for approve/reject reason prompts)
# ---------------------------------------------------------------------------


ReaderCallable = Callable[[str], str]


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _handle_help() -> DispatchResult:
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="help",
        status=DispatchStatus.OK,
        output=_HELP_BANNER,
    )


def _handle_pending(ledger: AdaptationLedger) -> DispatchResult:
    pending = ledger.list_pending()
    if not pending and not ledger._read_all():
        # Distinguish "ledger off" from "ledger on, no proposals"
        # by checking if the substrate returned ANY records. If
        # both pending and full history are empty AND the ledger
        # exists, return OK with the empty marker. If the ledger
        # is master-off, list_pending() returns ()  but we want a
        # specific status — check via the substrate's is_enabled.
        # Cleaner: just return OK with "no pending" — the master-
        # off case is caught by an explicit ledger.is_enabled
        # check at the dispatch entry point.
        pass
    output = _render_pending_list(pending)
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="pending",
        status=DispatchStatus.OK,
        output=output,
        proposals=tuple(pending),
    )


def _handle_show(
    args: Sequence[str], ledger: AdaptationLedger,
) -> DispatchResult:
    if len(args) < 1:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="show",
            status=DispatchStatus.MISSING_PROPOSAL_ID,
            detail="usage: /adapt show <proposal-id>",
        )
    proposal_id = args[0].strip()
    p = ledger.get(proposal_id)
    if p is None:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="show",
            status=DispatchStatus.PROPOSAL_NOT_FOUND,
            detail=f"no proposal for proposal_id={proposal_id!r}",
        )
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="show",
        status=DispatchStatus.OK,
        output=_render_proposal_full(p),
        proposal=p,
    )


def _handle_history(
    args: Sequence[str], ledger: AdaptationLedger,
) -> DispatchResult:
    limit = DEFAULT_HISTORY_LIMIT
    surface: Optional[AdaptationSurface] = None
    # Parse args: [limit] [--surface NAME]
    tail = list(args)
    if "--surface" in tail:
        idx = tail.index("--surface")
        if idx + 1 >= len(tail):
            return DispatchResult(
                schema_version=DISPATCH_SCHEMA_VERSION,
                subcommand="history",
                status=DispatchStatus.INVALID_ARGS,
                detail="--surface requires a surface name",
            )
        surface_arg = tail[idx + 1]
        try:
            surface = AdaptationSurface(surface_arg)
        except ValueError:
            return DispatchResult(
                schema_version=DISPATCH_SCHEMA_VERSION,
                subcommand="history",
                status=DispatchStatus.INVALID_ARGS,
                detail=(
                    f"unknown surface {surface_arg!r}; valid: "
                    f"{[s.value for s in AdaptationSurface]}"
                ),
            )
        # Strip the --surface NAME pair from tail
        tail = tail[:idx] + tail[idx + 2:]
    if tail:
        try:
            limit = int(tail[0])
        except ValueError:
            return DispatchResult(
                schema_version=DISPATCH_SCHEMA_VERSION,
                subcommand="history",
                status=DispatchStatus.INVALID_ARGS,
                detail=f"limit must be an integer, got {tail[0]!r}",
            )
        if limit <= 0:
            return DispatchResult(
                schema_version=DISPATCH_SCHEMA_VERSION,
                subcommand="history",
                status=DispatchStatus.INVALID_ARGS,
                detail=f"limit must be > 0, got {limit}",
            )
        if limit > MAX_HISTORY_LIMIT:
            limit = MAX_HISTORY_LIMIT

    history = ledger.history(surface=surface, limit=limit)
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="history",
        status=DispatchStatus.OK,
        output=_render_history_list(history, surface=surface),
        proposals=tuple(history),
    )


def _handle_stats(ledger: AdaptationLedger) -> DispatchResult:
    stats = compute_stats(ledger)
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="stats",
        status=DispatchStatus.OK,
        output=_render_stats(stats),
        stats=stats,
    )


def _handle_decision(
    args: Sequence[str],
    *,
    operator: str,
    reader: ReaderCallable,
    ledger: AdaptationLedger,
    decision_kind: str,  # "approve" or "reject"
) -> DispatchResult:
    if not operator.strip():
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=decision_kind,
            status=DispatchStatus.OPERATOR_REQUIRED,
            detail="operator name required",
        )
    if len(args) < 1:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=decision_kind,
            status=DispatchStatus.MISSING_PROPOSAL_ID,
            detail=f"usage: /adapt {decision_kind} <proposal-id>",
        )
    proposal_id = args[0].strip()
    existing = ledger.get(proposal_id)
    if existing is None:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=decision_kind,
            status=DispatchStatus.PROPOSAL_NOT_FOUND,
            detail=f"no proposal for proposal_id={proposal_id!r}",
        )
    if existing.operator_decision is not OperatorDecisionStatus.PENDING:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=decision_kind,
            status=DispatchStatus.NOT_PENDING,
            detail=(
                f"current status: {existing.operator_decision.value}"
            ),
            proposal=existing,
        )

    # Prompt operator for reason (audit trail). Both approve and
    # reject require it — defense against accidental clicks.
    try:
        reason = reader(
            f"Reason to {decision_kind} {proposal_id} "
            f"(max {MAX_REASON_CHARS_DISPATCH} chars): "
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=decision_kind,
            status=DispatchStatus.INTERNAL_ERROR,
            detail=f"reader_failed:{type(exc).__name__}:{exc}",
        )
    reason = (reason or "").strip()[:MAX_REASON_CHARS_DISPATCH]
    if not reason:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=decision_kind,
            status=DispatchStatus.REASON_REQUIRED,
            detail="empty reason",
        )

    # Note: the substrate's approve()/reject() does NOT take a
    # reason parameter (the Slice 1 contract). The reason is logged
    # by the REPL here for operator-side audit. Future improvement:
    # extend the substrate's contract to thread the reason into the
    # AdaptationProposal record. Tracked as Pass C v1.1 follow-up.
    if decision_kind == "approve":
        res = ledger.approve(proposal_id, operator=operator)
    else:
        res = ledger.reject(proposal_id, operator=operator)

    if res.status is not DecisionStatus.OK:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=decision_kind,
            status=DispatchStatus.LEDGER_REJECTED,
            detail=(
                f"ledger_status={res.status.value}: {res.detail}"
            ),
            proposal=res.proposal,
        )

    # Item #2 (2026-04-26): on APPROVE, materialize the proposal's
    # proposed_state_payload into the live gate's adapted YAML via
    # the YAML writer. Best-effort — writer failures DO NOT roll
    # back the ledger approval (the audit trail of the approval
    # decision must persist regardless).
    yaml_writer_summary = ""
    if (
        decision_kind == "approve"
        and res.proposal is not None
    ):
        try:
            from backend.core.ouroboros.governance.adaptation.yaml_writer import (  # noqa: E501
                write_proposal_to_yaml,
            )
            write_result = write_proposal_to_yaml(res.proposal)
            yaml_writer_summary = (
                f" yaml_write_status={write_result.status.value}"
            )
            if not (write_result.is_ok or write_result.is_skipped):
                logger.warning(
                    "[MetaAdaptationGovernor] yaml_writer FAILED for "
                    "proposal_id=%s: status=%s detail=%s",
                    proposal_id, write_result.status.value,
                    write_result.detail,
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[MetaAdaptationGovernor] yaml_writer raised %s for "
                "proposal_id=%s — ledger approval preserved",
                exc, proposal_id,
            )
            yaml_writer_summary = (
                f" yaml_write_status=raised:{type(exc).__name__}"
            )

    logger.info(
        "[MetaAdaptationGovernor] %sd proposal_id=%s operator=%s "
        "reason_chars=%d%s",
        decision_kind, proposal_id, operator, len(reason),
        yaml_writer_summary,
    )
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand=decision_kind,
        status=DispatchStatus.OK,
        output=(
            f"{decision_kind.capitalize()}d {proposal_id} "
            f"(operator={operator}). Reason: {reason}"
            + yaml_writer_summary
        ),
        proposal=res.proposal,
    )


# ---------------------------------------------------------------------------
# Public dispatch entrypoint
# ---------------------------------------------------------------------------


def parse_argv(line: str) -> List[str]:
    """Parse a raw REPL line into argv tokens via shlex."""
    try:
        return shlex.split(line.strip())
    except ValueError:
        return line.strip().split()


def dispatch_adapt(
    args: Sequence[str],
    *,
    operator: str = "",
    reader: Optional[ReaderCallable] = None,
    ledger: Optional[AdaptationLedger] = None,
) -> DispatchResult:
    """Dispatch one /adapt invocation. NEVER raises into the REPL
    caller; every failure path returns a structured DispatchResult.

    `args` is the post-parsed argv (subcommand + tail). Use
    :func:`parse_argv` to convert a raw REPL line.

    `operator` is the caller-supplied operator name. Required for
    approve/reject; ignored for read-only subcommands.

    `reader` is an injectable prompt callable for collecting
    operator reasons on approve/reject. Tests inject a stub.
    """
    args_list = list(args or ())
    if not args_list:
        return _handle_help()
    subcmd = args_list[0].strip().lower()
    tail = args_list[1:]

    # help always works (discoverability) — bypasses master flag.
    if subcmd == "help":
        return _handle_help()

    if not is_enabled():
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.MASTER_OFF,
            detail=(
                "JARVIS_ADAPT_REPL_ENABLED is off; only /adapt help "
                "is available."
            ),
        )

    if subcmd not in _VALID_SUBCOMMANDS:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.UNKNOWN_SUBCOMMAND,
            detail=(
                f"unknown subcommand {subcmd!r}; valid: "
                f"{', '.join(_VALID_SUBCOMMANDS)}"
            ),
        )

    led = ledger if ledger is not None else get_default_ledger()

    # Substrate master-flag check — if the ledger itself is off,
    # all read methods return empty + write methods return DISABLED.
    # Surface this explicitly for read-side clarity.
    from backend.core.ouroboros.governance.adaptation.ledger import (
        is_enabled as _ledger_is_enabled,
    )
    if not _ledger_is_enabled():
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.LEDGER_DISABLED,
            detail=(
                "JARVIS_ADAPTATION_LEDGER_ENABLED is off; all Pass C "
                "is dark. Set the ledger flag first."
            ),
        )

    try:
        if subcmd == "pending":
            return _handle_pending(led)
        if subcmd == "show":
            return _handle_show(tail, led)
        if subcmd == "history":
            return _handle_history(tail, led)
        if subcmd == "stats":
            return _handle_stats(led)
        if subcmd in ("approve", "reject"):
            if reader is None:
                return DispatchResult(
                    schema_version=DISPATCH_SCHEMA_VERSION,
                    subcommand=subcmd,
                    status=DispatchStatus.INTERNAL_ERROR,
                    detail=f"reader callable required for {subcmd}",
                )
            return _handle_decision(
                tail,
                operator=operator,
                reader=reader,
                ledger=led,
                decision_kind=subcmd,
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[MetaAdaptationGovernor] subcmd=%s INTERNAL_ERROR: %s",
            subcmd, exc,
        )
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.INTERNAL_ERROR,
            detail=f"exception:{type(exc).__name__}:{exc!s}",
        )

    # Unreachable — every subcmd in _VALID_SUBCOMMANDS is handled.
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand=subcmd,
        status=DispatchStatus.INTERNAL_ERROR,
        detail="unhandled subcommand fallthrough (should be unreachable)",
    )


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DISPATCH_SCHEMA_VERSION",
    "DispatchResult",
    "DispatchStatus",
    "MAX_HISTORY_LIMIT",
    "MAX_REASON_CHARS_DISPATCH",
    "ReaderCallable",
    "compute_stats",
    "dispatch_adapt",
    "is_enabled",
    "parse_argv",
]
