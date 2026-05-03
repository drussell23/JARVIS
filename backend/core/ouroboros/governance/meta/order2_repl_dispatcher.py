"""RR Pass B Slice 6 (module 3) — /order2 REPL dispatcher (CLOSES Pass B).

The operator-facing surface that closes Reverse Russian Doll Pass B.
Composes Slices 1-5 + Slice 6 modules 1+2 into ONE dispatch entrypoint:

  /order2 pending             — list pending entries
  /order2 show <op-id>        — render one entry's full evidence bundle
  /order2 amend <op-id>       — sandboxed-replay against ALL applicable
                                 corpus snapshots, prompt operator for
                                 reason, record AMENDED with replay bundle
  /order2 reject <op-id>      — prompt operator for reason, record REJECTED
  /order2 history [limit]     — list recent entries (newest-first)
  /order2 help                — usage banner (always works, even if
                                 master flag off — discoverability)

This is THE only place in O+V that passes ``operator_authorized=True``
to :func:`replay_executor.execute_replay_under_operator_trigger`. The
amend subcommand is the cage's authority-gating ceremony:

  1. Queue lookup → must be PENDING_REVIEW.
  2. MetaPhaseRunner result must have applicable snapshots.
  3. Sandboxed replay run for EVERY applicable snapshot, all bounded
     by per-call timeout.
  4. At least one PASSED required (mirrors queue's NO_PASSING_REPLAY
     guard; defense in depth — both layers enforce this independently).
  5. Operator name + reason via injected reader callable (test-injectable;
     production uses a stdin prompter).
  6. ``queue.amend(operator_authorized=True via direct queue call)``
     records the AMENDED transition with the full replay bundle as
     evidence — Slice 6 module 2 persists it.

## Authority invariants (Pass B §7.2 + §8)

  * Pure composition + REPL parsing. No subprocess, no env mutation,
    no network. Side effects: queue writes (Slice 6.2's surface),
    replay executor calls (Slice 6.1's surface), structured logging.
  * NO imports of orchestrator / policy / iron_gate / risk_tier_floor
    / change_engine / candidate_generator / gate / semantic_guardian
    / semantic_firewall / scoped_tool_backend.
  * Allowed: stdlib + meta.replay_executor + meta.order2_review_queue
    + meta.shadow_replay (for snapshot type) + meta.meta_phase_runner
    (for evidence-bundle status enum reference).
  * Best-effort throughout — every dispatch returns a structured
    :class:`DispatchResult`; never raises into the REPL caller.

## Default-off

Behind ``JARVIS_ORDER2_REPL_ENABLED`` (default false until Slice 6
graduation). When off, every subcommand EXCEPT ``help`` short-
circuits to ``DispatchStatus.MASTER_OFF``. ``help`` always works
(discoverability per the /help-dispatcher policy adopted across
Pass A graduations).
"""
from __future__ import annotations

import enum
import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import (
    Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple,
)

from backend.core.ouroboros.governance.meta.order2_review_queue import (
    AmendStatus,
    Order2ReviewQueue,
    QueueEntry,
    QueueEntryStatus,
    RejectStatus,
    amendment_requires_operator,
    get_default_queue,
)
from backend.core.ouroboros.governance.meta.replay_executor import (
    DEFAULT_TIMEOUT_S,
    MAX_TIMEOUT_S,
    ReplayExecutionResult,
    ReplayExecutionStatus,
    execute_replay_under_operator_trigger,
)
from backend.core.ouroboros.governance.meta.shadow_replay import (
    ReplayCorpus,
    ReplayLoadStatus,
    ReplaySnapshot,
    get_default_corpus,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Schema stamped into DispatchResult.to_dict for downstream parsers.
DISPATCH_SCHEMA_VERSION: int = 1

# Bound the operator-supplied reason. Mirrors queue's MAX_REASON_CHARS
# so the dispatcher can reject obvious overflows BEFORE round-tripping
# through the queue.
MAX_REASON_CHARS_DISPATCH: int = 1_024

# Bound the history limit subcommand argument.
DEFAULT_HISTORY_LIMIT: int = 20
MAX_HISTORY_LIMIT: int = 500

# Subcommand allowlist — anything else routes to UNKNOWN_SUBCOMMAND.
_VALID_SUBCOMMANDS: Tuple[str, ...] = (
    "pending", "show", "amend", "reject", "history", "help",
)


def is_enabled() -> bool:
    """Master flag — ``JARVIS_ORDER2_REPL_ENABLED`` (default false
    until Slice 6 graduation)."""
    return os.environ.get(
        "JARVIS_ORDER2_REPL_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Status enum + frozen result
# ---------------------------------------------------------------------------


class DispatchStatus(str, enum.Enum):
    """Outcome of one dispatch_order2 call. Pinned so SerpentFlow /
    REPL renderers can map status -> color reliably."""

    OK = "OK"
    """Subcommand executed cleanly. Renderable output in
    :attr:`DispatchResult.output`."""

    MASTER_OFF = "MASTER_OFF"
    """JARVIS_ORDER2_REPL_ENABLED is off. (help subcommand still
    returns OK even when master is off — discoverability.)"""

    UNKNOWN_SUBCOMMAND = "UNKNOWN_SUBCOMMAND"
    """First arg isn't in the subcommand allowlist."""

    MISSING_OP_ID = "MISSING_OP_ID"
    """Subcommand requires an op_id arg but none provided."""

    OP_ID_NOT_FOUND = "OP_ID_NOT_FOUND"
    """No queue entry for the op_id."""

    NOT_PENDING = "NOT_PENDING"
    """Op is in a terminal state (AMENDED / REJECTED / EXPIRED)."""

    NO_APPLICABLE_SNAPSHOTS = "NO_APPLICABLE_SNAPSHOTS"
    """MetaEvaluation has zero applicable_snapshots — cage refuses to
    amend without at least one corpus replay attempt."""

    CORPUS_UNAVAILABLE = "CORPUS_UNAVAILABLE"
    """Slice 4 corpus failed to load. Cage degrades — operator must
    fix the corpus before amending."""

    REPLAY_ALL_FAILED = "REPLAY_ALL_FAILED"
    """Sandboxed replays ran for every applicable snapshot but ZERO
    PASSED. Cage refuses to amend (defense in depth — queue would
    also reject this with NO_PASSING_REPLAY)."""

    REPLAY_AUTHORIZATION_BUG = "REPLAY_AUTHORIZATION_BUG"
    """Defensive: the cage invariant says
    amendment_requires_operator() == True, so the dispatcher MUST
    pass operator_authorized=True. If something flips this we want a
    visible status, not silent NOT_AUTHORIZED."""

    REASON_REQUIRED = "REASON_REQUIRED"
    """Operator supplied empty reason; amend/reject blocked."""

    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
    """Caller supplied empty operator name."""

    QUEUE_REJECTED = "QUEUE_REJECTED"
    """Queue layer (Slice 6.2) refused the amend/reject. Detail
    contains the queue's status."""

    INVALID_ARGS = "INVALID_ARGS"
    """Subcommand args malformed (e.g. history limit not an int)."""

    INTERNAL_ERROR = "INTERNAL_ERROR"
    """Defensive: unexpected exception. Should never fire."""


@dataclass(frozen=True)
class DispatchResult:
    """One REPL dispatch outcome. Frozen so SerpentFlow can stash it
    in event logs without worrying about post-hoc mutation."""

    schema_version: int
    subcommand: str
    status: DispatchStatus
    output: str = ""
    detail: str = ""
    entry: Optional[QueueEntry] = None
    replay_results: Tuple[ReplayExecutionResult, ...] = field(
        default_factory=tuple,
    )

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
            "entry": self.entry.to_dict() if self.entry is not None else None,
            "replay_results": [r.to_dict() for r in self.replay_results],
        }


# ---------------------------------------------------------------------------
# Help text (always available, even master-off)
# ---------------------------------------------------------------------------


_HELP_BANNER = """\
/order2 — Order-2 governance amendment review (RR Pass B)

  /order2 pending                 List pending review entries
  /order2 show <op-id>            Render one entry's full evidence bundle
  /order2 amend <op-id>           Sandboxed replay + record operator approval
  /order2 reject <op-id>          Record operator rejection
  /order2 history [limit]         List recent entries (default {default_limit})
  /order2 help                    This banner

Cage rules:
  * Amendments to Order-2 governance code REQUIRE operator authorization.
    The env JARVIS_ORDER2_MANIFEST_AMENDMENT_REQUIRES_OPERATOR is read
    for audit visibility but cannot be flipped to bypass this gate.
  * Amend requires at least one PASSED sandboxed replay against the
    corpus snapshots before the queue records the AMENDED transition.
  * The sandbox: restricted __builtins__, no import / open / dynamic-code
    primitives; per-snapshot timeout (default {timeout}s, max {max_timeout}s).

Master flag: JARVIS_ORDER2_REPL_ENABLED (default off until graduation).
""".format(
    default_limit=DEFAULT_HISTORY_LIMIT,
    timeout=DEFAULT_TIMEOUT_S,
    max_timeout=MAX_TIMEOUT_S,
)


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _render_entry_summary_line(entry: QueueEntry) -> str:
    eval_data = entry.meta_evaluation or {}
    files = eval_data.get("target_files") or []
    file_summary = (
        f"{len(files)}f" if files else "0f"
    )
    decision = entry.decision
    decision_summary = ""
    if decision is not None:
        decision_summary = f" by={decision.operator}"
    return (
        f"[{entry.status.value:14s}] {entry.op_id:20s} "
        f"phase={eval_data.get('target_phase', '?'):8s} {file_summary}"
        f" at={entry.enqueued_at_iso}{decision_summary}"
    )


def _render_entry_full(entry: QueueEntry) -> str:
    """Operator-readable detail render for /order2 show."""
    eval_data = entry.meta_evaluation or {}
    lines: List[str] = []
    lines.append(f"=== Order-2 review entry: {entry.op_id} ===")
    lines.append(f"  Status:       {entry.status.value}")
    lines.append(f"  Enqueued at:  {entry.enqueued_at_iso}")
    lines.append(f"  Schema:       v{entry.schema_version}")
    lines.append("")
    lines.append("  --- MetaEvaluation ---")
    lines.append(f"  target_phase: {eval_data.get('target_phase', '?')}")
    lines.append(
        f"  status:       {eval_data.get('status', '?')}"
    )
    lines.append(
        f"  manifest_matched: {eval_data.get('manifest_matched', '?')}"
    )
    files = eval_data.get("target_files") or []
    lines.append(f"  target_files ({len(files)}):")
    for f in files[:20]:
        lines.append(f"    - {f}")
    if len(files) > 20:
        lines.append(f"    ... ({len(files) - 20} more)")
    snaps = eval_data.get("applicable_snapshots") or []
    lines.append(f"  applicable_snapshots ({len(snaps)}):")
    for s in snaps[:20]:
        if isinstance(s, dict):
            lines.append(
                f"    - op_id={s.get('op_id')} phase={s.get('phase')} "
                f"tags={s.get('tags', [])}"
            )
    if len(snaps) > 20:
        lines.append(f"    ... ({len(snaps) - 20} more)")
    rationale = (eval_data.get("rationale") or "").strip()
    if rationale:
        lines.append("  rationale:")
        for ln in rationale.splitlines()[:5]:
            lines.append(f"    | {ln}")
    ast_v = eval_data.get("ast_validation") or {}
    if ast_v:
        lines.append("  --- AST validation (Slice 3) ---")
        lines.append(f"  status:  {ast_v.get('status', '?')}")
        lines.append(
            f"  classes: {ast_v.get('classes_inspected', [])}"
        )
        if ast_v.get("reason"):
            lines.append(f"  reason:  {ast_v.get('reason')}")
    if entry.decision is not None:
        d = entry.decision
        lines.append("")
        lines.append("  --- Operator decision ---")
        lines.append(f"  decision:    {d.decision}")
        lines.append(f"  operator:    {d.operator}")
        lines.append(f"  decided_at:  {d.decided_at_iso}")
        lines.append(f"  reason:      {d.reason}")
        if d.replay_results:
            lines.append(f"  replay_results: {len(d.replay_results)} record(s)")
            passed = sum(
                1 for r in d.replay_results
                if isinstance(r, dict) and r.get("status") == "PASSED"
            )
            lines.append(
                f"    passed: {passed} / {len(d.replay_results)}"
            )
    return "\n".join(lines)


def _render_replay_summary(
    results: Sequence[ReplayExecutionResult],
) -> str:
    if not results:
        return "  (no replays attempted)"
    lines = []
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
    summary = ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items())
    )
    lines.append(f"  Total: {len(results)} ({summary})")
    for r in results[:10]:
        det = r.detail or ""
        if len(det) > 100:
            det = det[:97] + "..."
        lines.append(
            f"    snap={r.snapshot_op_id}/{r.snapshot_phase} "
            f"-> {r.status.value} ({r.elapsed_s:.3f}s) {det}"
        )
    if len(results) > 10:
        lines.append(f"    ... ({len(results) - 10} more)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reader protocol
# ---------------------------------------------------------------------------


# A reader callable: called with a prompt string, returns the
# operator's typed response. Tests inject a fake reader. Production
# callers wire in a stdin prompter (NOT in this module — keeps the
# dispatcher pure).
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


def _handle_pending(queue: Order2ReviewQueue) -> DispatchResult:
    pending = queue.list_pending()
    if not pending:
        output = "No pending Order-2 review entries."
    else:
        lines = [f"Pending Order-2 review entries ({len(pending)}):"]
        for entry in sorted(pending,
                            key=lambda e: e.enqueued_at_epoch):
            lines.append("  " + _render_entry_summary_line(entry))
        output = "\n".join(lines)
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="pending",
        status=DispatchStatus.OK,
        output=output,
    )


def _handle_show(
    args: Sequence[str], queue: Order2ReviewQueue,
) -> DispatchResult:
    if len(args) < 1:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="show",
            status=DispatchStatus.MISSING_OP_ID,
            detail="usage: /order2 show <op-id>",
        )
    op_id = args[0].strip()
    entry = queue.get(op_id)
    if entry is None:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="show",
            status=DispatchStatus.OP_ID_NOT_FOUND,
            detail=f"no entry for op_id={op_id!r}",
        )
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="show",
        status=DispatchStatus.OK,
        output=_render_entry_full(entry),
        entry=entry,
    )


def _handle_history(
    args: Sequence[str], queue: Order2ReviewQueue,
) -> DispatchResult:
    limit = DEFAULT_HISTORY_LIMIT
    if args:
        raw = args[0].strip()
        try:
            limit = int(raw)
        except ValueError:
            return DispatchResult(
                schema_version=DISPATCH_SCHEMA_VERSION,
                subcommand="history",
                status=DispatchStatus.INVALID_ARGS,
                detail=f"limit must be an integer, got {raw!r}",
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
    history = queue.list_history(limit=limit)
    if not history:
        output = "No Order-2 review history."
    else:
        lines = [
            f"Order-2 review history ({len(history)} entries, newest-first):",
        ]
        for entry in history:
            lines.append("  " + _render_entry_summary_line(entry))
        output = "\n".join(lines)
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="history",
        status=DispatchStatus.OK,
        output=output,
    )


def _handle_reject(
    args: Sequence[str],
    *,
    operator: str,
    reader: ReaderCallable,
    queue: Order2ReviewQueue,
) -> DispatchResult:
    if not operator.strip():
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="reject",
            status=DispatchStatus.OPERATOR_REQUIRED,
            detail="operator name required",
        )
    if len(args) < 1:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="reject",
            status=DispatchStatus.MISSING_OP_ID,
            detail="usage: /order2 reject <op-id>",
        )
    op_id = args[0].strip()
    entry = queue.get(op_id)
    if entry is None:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="reject",
            status=DispatchStatus.OP_ID_NOT_FOUND,
            detail=f"no entry for op_id={op_id!r}",
        )
    if entry.status is not QueueEntryStatus.PENDING_REVIEW:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="reject",
            status=DispatchStatus.NOT_PENDING,
            detail=f"current status: {entry.status.value}",
            entry=entry,
        )
    try:
        reason = reader(
            f"Reason to reject {op_id} (max {MAX_REASON_CHARS_DISPATCH} chars): "
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="reject",
            status=DispatchStatus.INTERNAL_ERROR,
            detail=f"reader_failed:{type(exc).__name__}:{exc}",
        )
    reason = (reason or "").strip()[:MAX_REASON_CHARS_DISPATCH]
    if not reason:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="reject",
            status=DispatchStatus.REASON_REQUIRED,
            detail="empty reason",
        )
    res = queue.reject(op_id, operator=operator, reason=reason)
    if res.status is not RejectStatus.OK:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="reject",
            status=DispatchStatus.QUEUE_REJECTED,
            detail=f"queue_status={res.status.value}: {res.detail}",
            entry=res.entry,
        )
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="reject",
        status=DispatchStatus.OK,
        output=(f"Rejected {op_id} (operator={operator}). "
                f"Reason: {reason}"),
        entry=res.entry,
    )


async def _handle_amend(
    args: Sequence[str],
    *,
    operator: str,
    reader: ReaderCallable,
    queue: Order2ReviewQueue,
    corpus: ReplayCorpus,
    timeout_s: float,
) -> DispatchResult:
    """The cage's authority-gating ceremony.

    1. Lookup queue entry, must be PENDING_REVIEW.
    2. Verify amendment_requires_operator() returns True (defensive
       — should always be True per Pass B §7.3 invariant).
    3. Find applicable corpus snapshots from MetaEvaluation.
    4. Run sandboxed replay for EACH snapshot.
    5. Operator must supply non-empty reason.
    6. queue.amend() with the full replay-results bundle as evidence.
    """
    if not operator.strip():
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.OPERATOR_REQUIRED,
            detail="operator name required",
        )
    if len(args) < 1:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.MISSING_OP_ID,
            detail="usage: /order2 amend <op-id>",
        )
    op_id = args[0].strip()

    # Defensive: pin the cage invariant locally.
    if not amendment_requires_operator():
        # Should be unreachable — the locked-true invariant says this
        # is always True. Visible status > silent unauthorized call.
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.REPLAY_AUTHORIZATION_BUG,
            detail=(
                "amendment_requires_operator() returned False — cage "
                "invariant violated. Refusing to call replay executor."
            ),
        )

    entry = queue.get(op_id)
    if entry is None:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.OP_ID_NOT_FOUND,
            detail=f"no entry for op_id={op_id!r}",
        )
    if entry.status is not QueueEntryStatus.PENDING_REVIEW:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.NOT_PENDING,
            detail=f"current status: {entry.status.value}",
            entry=entry,
        )

    eval_data = entry.meta_evaluation or {}
    target_phase = str(eval_data.get("target_phase") or "").strip()
    candidate_source = str(eval_data.get("candidate_source") or "")

    if corpus.status is not ReplayLoadStatus.LOADED:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.CORPUS_UNAVAILABLE,
            detail=f"corpus_status={corpus.status.value}",
            entry=entry,
        )

    applicable_snaps_raw = eval_data.get("applicable_snapshots") or []
    if not applicable_snaps_raw:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.NO_APPLICABLE_SNAPSHOTS,
            detail="MetaEvaluation has zero applicable_snapshots",
            entry=entry,
        )

    # Re-resolve applicable snapshots from the live corpus by op_id +
    # phase. (The MetaEvaluation stored only summary metadata; the
    # full snapshot lives in the corpus.)
    snapshots: List[ReplaySnapshot] = []
    for s_meta in applicable_snaps_raw:
        if not isinstance(s_meta, dict):
            continue
        s_op_id = str(s_meta.get("op_id") or "").strip()
        s_phase = str(s_meta.get("phase") or "").strip()
        if not s_op_id or not s_phase:
            continue
        for snap in corpus.snapshots:
            if snap.op_id == s_op_id and snap.phase == s_phase:
                snapshots.append(snap)
                break
    if not snapshots:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.NO_APPLICABLE_SNAPSHOTS,
            detail=(
                "applicable_snapshots referenced by MetaEvaluation "
                "not found in live corpus"
            ),
            entry=entry,
        )

    # Run sandboxed replay against each applicable snapshot. This is
    # THE only place in O+V that passes operator_authorized=True.
    results: List[ReplayExecutionResult] = []
    for snap in snapshots:
        result = await execute_replay_under_operator_trigger(
            candidate_source=candidate_source,
            target_phase=target_phase,
            snapshot=snap,
            op_id=op_id,
            operator_authorized=True,
            timeout_s=timeout_s,
        )
        results.append(result)

    passed_count = sum(
        1 for r in results
        if r.status is ReplayExecutionStatus.PASSED
    )
    if passed_count == 0:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.REPLAY_ALL_FAILED,
            detail=(
                f"replays_run={len(results)} passed=0 "
                "(cage refuses to amend without at least one PASSED)"
            ),
            entry=entry,
            replay_results=tuple(results),
            output=_render_replay_summary(results),
        )

    # Surface the replay summary to the operator BEFORE prompting
    # for the reason — gives them informed consent.
    summary = (
        f"Replay outcome for {op_id}: {passed_count}/{len(results)} PASSED.\n"
        + _render_replay_summary(results)
    )
    try:
        reason = reader(
            f"\n{summary}\n\nReason to AMEND {op_id} "
            f"(max {MAX_REASON_CHARS_DISPATCH} chars): "
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.INTERNAL_ERROR,
            detail=f"reader_failed:{type(exc).__name__}:{exc}",
            replay_results=tuple(results),
        )
    reason = (reason or "").strip()[:MAX_REASON_CHARS_DISPATCH]
    if not reason:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.REASON_REQUIRED,
            detail="empty reason",
            replay_results=tuple(results),
        )

    queue_amend_res = queue.amend(
        op_id,
        operator=operator,
        reason=reason,
        replay_results=[r.to_dict() for r in results],
    )
    if queue_amend_res.status is not AmendStatus.OK:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand="amend",
            status=DispatchStatus.QUEUE_REJECTED,
            detail=(f"queue_status={queue_amend_res.status.value}: "
                    f"{queue_amend_res.detail}"),
            entry=queue_amend_res.entry,
            replay_results=tuple(results),
        )
    logger.info(
        "[Order2REPL] op=%s AMENDED operator=%s "
        "passed_replays=%d/%d reason_chars=%d",
        op_id, operator, passed_count, len(results), len(reason),
    )
    return DispatchResult(
        schema_version=DISPATCH_SCHEMA_VERSION,
        subcommand="amend",
        status=DispatchStatus.OK,
        output=(
            f"Amended {op_id} (operator={operator}, passed_replays="
            f"{passed_count}/{len(results)}). Reason: {reason}"
        ),
        entry=queue_amend_res.entry,
        replay_results=tuple(results),
    )


# ---------------------------------------------------------------------------
# Public dispatch entrypoint
# ---------------------------------------------------------------------------


def parse_argv(line: str) -> List[str]:
    """Parse a raw REPL line into argv tokens via shlex (so quoting
    works for op-ids containing dashes/spaces)."""
    try:
        return shlex.split(line.strip())
    except ValueError:
        # Unbalanced quotes etc. — fall back to whitespace split.
        return line.strip().split()


async def dispatch_order2(
    args: Sequence[str],
    *,
    operator: str = "",
    reader: Optional[ReaderCallable] = None,
    queue: Optional[Order2ReviewQueue] = None,
    corpus: Optional[ReplayCorpus] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> DispatchResult:
    """Dispatch one /order2 invocation. NEVER raises into the REPL
    caller; every failure path returns a structured
    :class:`DispatchResult`.

    ``args`` is the post-parsed argv (subcommand + tail). Use
    :func:`parse_argv` to convert a raw REPL line.

    ``operator`` is the caller-supplied operator name (e.g. from the
    SerpentFlow session config). Required for amend + reject; ignored
    for read-only subcommands.

    ``reader`` is an injectable prompt callable used by amend +
    reject to collect the operator's reason. Tests inject a stub;
    production callers wire in a stdin prompter.
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
                "JARVIS_ORDER2_REPL_ENABLED is off; only /order2 help "
                "is available."
            ),
        )

    if subcmd not in _VALID_SUBCOMMANDS:
        return DispatchResult(
            schema_version=DISPATCH_SCHEMA_VERSION,
            subcommand=subcmd,
            status=DispatchStatus.UNKNOWN_SUBCOMMAND,
            detail=(f"unknown subcommand {subcmd!r}; valid: "
                    f"{', '.join(_VALID_SUBCOMMANDS)}"),
        )

    q = queue if queue is not None else get_default_queue()
    cor = corpus if corpus is not None else get_default_corpus()

    try:
        if subcmd == "pending":
            return _handle_pending(q)
        if subcmd == "show":
            return _handle_show(tail, q)
        if subcmd == "history":
            return _handle_history(tail, q)
        if subcmd == "reject":
            if reader is None:
                return DispatchResult(
                    schema_version=DISPATCH_SCHEMA_VERSION,
                    subcommand="reject",
                    status=DispatchStatus.INTERNAL_ERROR,
                    detail="reader callable required for reject",
                )
            return _handle_reject(
                tail, operator=operator, reader=reader, queue=q,
            )
        if subcmd == "amend":
            if reader is None:
                return DispatchResult(
                    schema_version=DISPATCH_SCHEMA_VERSION,
                    subcommand="amend",
                    status=DispatchStatus.INTERNAL_ERROR,
                    detail="reader callable required for amend",
                )
            # Clamp timeout to safe range (mirrors replay executor's
            # internal clamp; surfacing it earlier is cheap).
            t = float(timeout_s) if timeout_s else DEFAULT_TIMEOUT_S
            if t <= 0.0:
                t = DEFAULT_TIMEOUT_S
            if t > MAX_TIMEOUT_S:
                t = MAX_TIMEOUT_S
            return await _handle_amend(
                tail, operator=operator, reader=reader,
                queue=q, corpus=cor, timeout_s=t,
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[Order2REPL] subcmd=%s INTERNAL_ERROR: %s",
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


# Compatibility re-exports to keep the (unawaited) async helper visible
# in case future callers introspect the awaitable type rather than just
# awaiting it directly.
DispatchAwaitable = Awaitable[DispatchResult]


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DISPATCH_SCHEMA_VERSION",
    "DispatchAwaitable",
    "DispatchResult",
    "DispatchStatus",
    "MAX_HISTORY_LIMIT",
    "MAX_REASON_CHARS_DISPATCH",
    "ReaderCallable",
    "dispatch_order2",
    "is_enabled",
    "parse_argv",
]


# ---------------------------------------------------------------------------
# Pass B Graduation Slice 2 — substrate AST pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta._invariant_helpers import (
        make_pass_b_substrate_invariant,
    )
    inv = make_pass_b_substrate_invariant(
        invariant_name="pass_b_order2_repl_dispatcher_substrate",
        target_file=(
            "backend/core/ouroboros/governance/meta/"
            "order2_repl_dispatcher.py"
        ),
        description=(
            "Pass B Slice 6.3 substrate: is_enabled + "
            "dispatch_order2 + parse_argv + DispatchResult (frozen) "
            "present; no dynamic-code calls. /order2 amend is THE "
            "only caller in O+V that passes operator_authorized=True "
            "to the replay executor -- but execution is independently "
            "gated by JARVIS_REPLAY_EXECUTOR_ENABLED (default-false)."
        ),
        required_funcs=("is_enabled", "dispatch_order2", "parse_argv"),
        required_classes=("DispatchResult",),
        frozen_classes=("DispatchResult",),
    )
    return [inv] if inv is not None else []
