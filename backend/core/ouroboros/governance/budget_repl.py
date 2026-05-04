"""Upgrade 1 Slice 4 — ``/budget`` REPL dispatcher (PRD §31.2).

Operator-facing CLI surface — parallel to :mod:`outcomes_repl`
(M11) and :mod:`failures_repl` (Upgrade 3). Same patterns:
``register_verbs`` for /help auto-discovery, lazy
``epistemic_budget`` import, frozen ``BudgetReplDispatchResult``.

Subcommands:

  * ``/budget``                 — alias for ``/budget status``
  * ``/budget status``          — overview of currently-tracked
    op budgets
  * ``/budget op <op_id>``      — single per-op detail
  * ``/budget config``          — env-knob snapshot
  * ``/budget help``            — usage listing (always
    available; bypasses master-flag gate)

Master gate: :func:`epistemic_budget_enabled`. Auto-discovered by
:func:`help_dispatcher._discover_module_provided_verbs`. NEVER
raises.

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + ``epistemic_budget`` ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor /
    epistemic_budget_executor_hook (executor-hook is the
    authority side; REPL is read-only).
  * Read-only — never mutates tracker state.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Optional

from backend.core.ouroboros.governance.epistemic_budget import (
    EPISTEMIC_BUDGET_SCHEMA_VERSION,
    EpistemicBudget,
    EpistemicBudgetTracker,
    epistemic_budget_enabled,
    epistemic_confidence_drop_threshold,
    epistemic_max_rounds,
    epistemic_sbt_branch_cap,
    epistemic_tracker_ttl_s,
    get_default_tracker,
    get_max_calls_per_probe,
)

logger = logging.getLogger(__name__)


_HELP = (
    "/budget — Bounded Epistemic Loop "
    "(Upgrade 1 / PRD §31.2)\n"
    "\n"
    "Subcommands:\n"
    "  /budget                       alias for /budget status\n"
    "  /budget status                overview of tracked ops\n"
    "  /budget op <op_id>            single per-op detail\n"
    "  /budget config                env-knob snapshot\n"
    "  /budget help                  this text\n"
    "\n"
    "Master flag: JARVIS_EPISTEMIC_BUDGET_ENABLED (graduated\n"
    "Slice 5; flip to false for instant revert)\n"
    "Live HTTP surface: GET /observability/budget[/{op_id}]\n"
    "Live SSE event:    budget_action_taken\n"
)


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetReplDispatchResult:
    """Result of a ``/budget`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/budget`` invocation at all (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Module-level tracker provider — tests inject; production uses
# :func:`get_default_tracker`.
# ---------------------------------------------------------------------------


_default_tracker: Optional[EpistemicBudgetTracker] = None


def set_default_tracker(
    tracker: Optional[EpistemicBudgetTracker],
) -> None:
    global _default_tracker  # noqa: PLW0603
    _default_tracker = tracker


def reset_default_tracker_for_tests() -> None:
    global _default_tracker  # noqa: PLW0603
    _default_tracker = None


def _resolve_tracker(
    explicit: Optional[EpistemicBudgetTracker],
) -> EpistemicBudgetTracker:
    if explicit is not None:
        return explicit
    if _default_tracker is not None:
        return _default_tracker
    return get_default_tracker()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/budget"
        or s == "budget"
        or s.startswith("/budget ")
        or s.startswith("budget ")
    )


def dispatch_budget_command(
    line: str,
    *,
    tracker: Optional[EpistemicBudgetTracker] = None,
) -> BudgetReplDispatchResult:
    """Parse a ``/budget`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return BudgetReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return BudgetReplDispatchResult(
            ok=False,
            text=f"  /budget parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    if head in ("help", "?"):
        return BudgetReplDispatchResult(ok=True, text=_HELP)

    if not epistemic_budget_enabled():
        return BudgetReplDispatchResult(
            ok=False,
            text=(
                "  /budget: EpistemicBudget disabled — set "
                "JARVIS_EPISTEMIC_BUDGET_ENABLED=true"
            ),
        )

    resolved = _resolve_tracker(tracker)

    if head == "status":
        return _render_status(resolved)
    if head == "op":
        if len(args) < 2:
            return BudgetReplDispatchResult(
                ok=False,
                text=(
                    "  /budget op <op_id>: missing op_id "
                    "argument."
                ),
            )
        return _render_op(resolved, args[1])
    if head == "config":
        return _render_config()
    return BudgetReplDispatchResult(
        ok=False,
        text=(
            f"  /budget: unknown subcommand {head!r}. "
            f"Try /budget help."
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _format_budget_one_line(b: EpistemicBudget) -> str:
    op_short = (b.op_id or "")[:12]
    rounds = (
        f"r={b.rounds_consumed}/{b.max_rounds}"
    )
    probes = (
        f"p={b.probe_calls_consumed}/{b.probe_call_cap}"
    )
    branches = (
        f"sbt={b.branch_calls_consumed}/{b.sbt_branch_cap}"
    )
    last_probe = b.last_probe_verdict or "-"
    return (
        f"  {op_short}  route={b.route}  tier={b.risk_tier}  "
        f"{rounds}  {probes}  {branches}  "
        f"last_probe={last_probe}"
    )


def _render_status(
    tracker: EpistemicBudgetTracker,
) -> BudgetReplDispatchResult:
    try:
        budgets = tracker.snapshot_all()
    except Exception:  # noqa: BLE001 — defensive
        budgets = tuple()
    if not budgets:
        return BudgetReplDispatchResult(
            ok=True,
            text=(
                "/budget status — no ops currently tracked.\n"
                f"  schema_version={EPISTEMIC_BUDGET_SCHEMA_VERSION}\n"
                "  master_enabled=true"
            ),
        )
    lines = [
        f"/budget status — {len(budgets)} op(s) tracked",
        f"  schema_version={EPISTEMIC_BUDGET_SCHEMA_VERSION}",
        "",
    ]
    for b in budgets:
        try:
            lines.append(_format_budget_one_line(b))
        except Exception:  # noqa: BLE001 — defensive
            lines.append(f"  <projection_failed for op>")
    return BudgetReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_op(
    tracker: EpistemicBudgetTracker, op_id: str,
) -> BudgetReplDispatchResult:
    try:
        budget = tracker.get(op_id)
    except Exception:  # noqa: BLE001 — defensive
        budget = None
    if budget is None:
        return BudgetReplDispatchResult(
            ok=False,
            text=f"  /budget op: op {op_id!r} not tracked.",
        )
    try:
        proj = budget.to_dict()
    except Exception:  # noqa: BLE001 — defensive
        proj = {}
    lines = [
        f"/budget op {op_id}",
        f"  route                  {proj.get('route')}",
        f"  risk_tier              {proj.get('risk_tier')}",
        (
            f"  rounds                 "
            f"{proj.get('rounds_consumed')}/"
            f"{proj.get('max_rounds')}  "
            f"(remaining={proj.get('rounds_remaining')})"
        ),
        (
            f"  probe_calls            "
            f"{proj.get('probe_calls_consumed')}/"
            f"{proj.get('probe_call_cap')}  "
            f"(remaining={proj.get('probe_calls_remaining')})"
        ),
        (
            f"  branch_calls           "
            f"{proj.get('branch_calls_consumed')}/"
            f"{proj.get('sbt_branch_cap')}  "
            f"(remaining={proj.get('branch_calls_remaining')})"
        ),
        (
            f"  last_probe_verdict     "
            f"{proj.get('last_probe_verdict') or '-'}"
        ),
        (
            f"  last_sbt_verdict       "
            f"{proj.get('last_sbt_verdict') or '-'}"
        ),
        (
            f"  cost_gated_route       "
            f"{proj.get('is_route_cost_gated')}"
        ),
        (
            f"  rounds_exhausted       "
            f"{proj.get('is_rounds_exhausted')}"
        ),
    ]
    traj = proj.get("trajectory") or {}
    lines.append(
        f"  confidence_trajectory  size={traj.get('size')}  "
        f"peak={traj.get('peak')}  "
        f"nadir={traj.get('nadir')}  "
        f"latest={traj.get('latest')}",
    )
    return BudgetReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_config() -> BudgetReplDispatchResult:
    try:
        cfg = {
            "max_rounds": epistemic_max_rounds(),
            "confidence_drop_threshold": (
                epistemic_confidence_drop_threshold()
            ),
            "probe_call_cap": get_max_calls_per_probe(),
            "sbt_branch_cap": epistemic_sbt_branch_cap(),
            "tracker_ttl_s": epistemic_tracker_ttl_s(),
        }
    except Exception:  # noqa: BLE001 — defensive
        cfg = {}
    lines = [
        "/budget config",
        f"  schema_version           {EPISTEMIC_BUDGET_SCHEMA_VERSION}",
        f"  master_enabled           {epistemic_budget_enabled()}",
    ]
    for k, v in sorted(cfg.items()):
        lines.append(f"  {k:<24} {v}")
    return BudgetReplDispatchResult(
        ok=True, text="\n".join(lines),
    )


# ---------------------------------------------------------------------------
# /help auto-discovery
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:
    """Register the ``/budget`` verb. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbSpec,
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0
    try:
        registry.register(VerbSpec(
            name="/budget",
            one_line=(
                "Bounded epistemic budget: per-op tool-round / "
                "probe / SBT consumption + escalation status "
                "(Upgrade 1 / PRD §31.2)."
            ),
            category="observability",
            help_text=_HELP,
        ))
        return 1
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[budget_repl] register_verbs swallowed",
            exc_info=True,
        )
        return 0


__all__ = [
    "BudgetReplDispatchResult",
    "dispatch_budget_command",
    "register_verbs",
    "reset_default_tracker_for_tests",
    "set_default_tracker",
]
