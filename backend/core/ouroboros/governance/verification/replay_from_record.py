"""Priority 2 Slice 5 — Replay-from-Record.

Enables ``--rerun-from <record-id>`` on the battle-test CLI. Whereas
``--rerun`` replays an ENTIRE session ledger, ``--rerun-from`` loads
the DAG, locates the target record, restores the session state UP TO
that record, and then resumes execution from that point onward. New
decisions written during the forked run carry
``counterfactual_of=<original_record_id>`` so the DAG correctly
represents the fork.

OPERATOR'S DESIGN CONSTRAINTS:

  * **Asynchronous** — ``prepare_replay_from_record`` is sync (small
    file I/O); the actual re-execution goes through the existing
    async harness.
  * **Dynamic** — target record-id is a runtime argument, not
    config-time.
  * **Adaptive** — gracefully degrades when the target record is
    not found, the DAG can't be built, or the session has no seed.
  * **Intelligent** — leverages the Slice 3 ``CausalityDAG`` +
    ``build_dag()`` to locate the target record and its topological
    predecessors.
  * **Robust** — NEVER raises from any public method. Every failure
    returns a structured ``ReplayFromRecordPlan`` with
    ``is_replayable=False`` + ``failure_reason``.
  * **No hardcoding** — master flag env-tunable, path resolution
    via existing env knobs.
  * **Leverages existing** — reuses ``SessionReplayer.discover``
    for seed/ledger validation, ``build_dag()`` for record location,
    ``DecisionRecord.from_dict`` for JSONL parsing.

Master flag: ``JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED``
(default false — Slice 5; flips ``true`` in Slice 6).

Authority invariants (AST-pinned):
  * Imports ONLY: stdlib + ``determinism.*`` + ``verification.causality_dag``
  * NO imports of: orchestrator, phase_runners, candidate_generator,
    iron_gate, change_engine, policy, semantic_guardian, providers.
  * NEVER raises from any public method.
  * Read-only — never modifies the original ledger.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from backend.core.ouroboros.governance.determinism.decision_runtime import (
    DecisionRecord,
    _ledger_dir,
)
from backend.core.ouroboros.governance.determinism.session_replay import (
    ReplaySessionPlan,
    SessionReplayer,
)
from backend.core.ouroboros.governance.verification.causality_dag import (
    CausalityDAG,
    build_dag,
    dag_query_enabled,
)

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def replay_from_record_enabled() -> bool:
    """``JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED`` (default
    ``true`` — graduated in Priority 2 Slice 6).

    Master flag governing whether ``--rerun-from <record-id>``
    activates the record-level fork primitive. When off
    (hot-revert), the CLI flag is structurally inert. Cost contract
    preservation is structural — the replay path goes through the
    existing orchestrator entry point (no shortcut bypass of the
    §26.6 four-layer defense), pinned by the
    ``dag_replay_cost_contract_preserved`` shipped_code_invariants
    seed."""
    raw = os.environ.get(
        "JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 6 — was false in Slice 5)
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# ReplayFromRecordPlan — frozen result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayFromRecordPlan:
    """Frozen plan for a record-level replay fork.

    ``is_replayable=True`` means the record was found, the session
    has a valid seed, and env vars can be applied. When False,
    ``failure_reason`` explains why."""

    session_id: str
    target_record_id: str
    is_replayable: bool = False
    failure_reason: str = ""
    session_plan: Optional[ReplaySessionPlan] = None
    target_record: Optional[DecisionRecord] = None
    predecessor_count: int = 0
    dag_node_count: int = 0
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Prepare replay-from-record
# ---------------------------------------------------------------------------


def prepare_replay_from_record(
    session_id: str,
    record_id: str,
    *,
    mode: str = "replay",
) -> ReplayFromRecordPlan:
    """Discover session + locate record + prepare fork env.

    Returns a frozen ``ReplayFromRecordPlan``. When
    ``is_replayable=True``, caller can apply env vars via
    ``apply_replay_from_record_env``.

    NEVER raises."""
    try:
        sid = str(session_id or "").strip()
        rid = str(record_id or "").strip()

        if not sid:
            return ReplayFromRecordPlan(
                session_id="",
                target_record_id=rid,
                failure_reason="empty_session_id",
                diagnostics=("session_id is required",),
            )
        if not rid:
            return ReplayFromRecordPlan(
                session_id=sid,
                target_record_id="",
                failure_reason="empty_record_id",
                diagnostics=("record_id is required",),
            )

        if not replay_from_record_enabled():
            return ReplayFromRecordPlan(
                session_id=sid,
                target_record_id=rid,
                failure_reason="disabled",
                diagnostics=(
                    "JARVIS_CAUSALITY_REPLAY_FROM_RECORD_ENABLED=false",
                ),
            )

        if not dag_query_enabled():
            return ReplayFromRecordPlan(
                session_id=sid,
                target_record_id=rid,
                failure_reason="dag_query_disabled",
                diagnostics=(
                    "JARVIS_CAUSALITY_DAG_QUERY_ENABLED=false — "
                    "DAG must be enabled for record-level replay",
                ),
            )

        # Step 1: Discover session (seed + ledger)
        replayer = SessionReplayer()
        session_plan = replayer.discover(sid)

        if not session_plan.is_replayable:
            return ReplayFromRecordPlan(
                session_id=sid,
                target_record_id=rid,
                failure_reason=f"session_not_replayable: "
                               f"{session_plan.failure_reason}",
                session_plan=session_plan,
                diagnostics=session_plan.diagnostics,
            )

        # Step 2: Build DAG + locate record
        dag = build_dag(sid)
        if dag.is_empty:
            return ReplayFromRecordPlan(
                session_id=sid,
                target_record_id=rid,
                failure_reason="dag_empty",
                session_plan=session_plan,
                diagnostics=(
                    f"DAG for session {sid!r} is empty — "
                    f"no records to fork from",
                ),
            )

        target = dag.node(rid)
        if target is None:
            return ReplayFromRecordPlan(
                session_id=sid,
                target_record_id=rid,
                failure_reason="record_not_found",
                session_plan=session_plan,
                dag_node_count=dag.node_count,
                diagnostics=(
                    f"record_id {rid!r} not found in DAG "
                    f"({dag.node_count} total nodes)",
                ),
            )

        # Step 3: Count predecessors (all records topologically
        # before the target)
        topo = dag.topological_order()
        if topo:
            try:
                target_idx = topo.index(rid)
                predecessor_count = target_idx
            except ValueError:
                predecessor_count = 0
        else:
            predecessor_count = 0

        return ReplayFromRecordPlan(
            session_id=sid,
            target_record_id=rid,
            is_replayable=True,
            session_plan=session_plan,
            target_record=target,
            predecessor_count=predecessor_count,
            dag_node_count=dag.node_count,
            diagnostics=(
                f"seed=0x{session_plan.seed:016x}",
                f"dag_nodes={dag.node_count}",
                f"predecessors={predecessor_count}",
                f"target_phase={target.phase}",
                f"target_kind={target.kind}",
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_from_record] prepare failed: %s",
            exc, exc_info=True,
        )
        return ReplayFromRecordPlan(
            session_id=str(session_id or ""),
            target_record_id=str(record_id or ""),
            failure_reason=f"unexpected_error: {exc}",
        )


# ---------------------------------------------------------------------------
# Apply env vars for fork replay
# ---------------------------------------------------------------------------


def apply_replay_from_record_env(
    plan: ReplayFromRecordPlan,
    *,
    mode: str = "replay",
) -> bool:
    """Apply env vars to set up a forked replay from the target
    record. Returns True if env was applied, False if plan is not
    replayable. NEVER raises.

    Sets:
      * Standard replay env vars (via SessionReplayer.apply_env)
      * ``JARVIS_CAUSALITY_FORK_FROM_RECORD_ID`` — the target
      * ``JARVIS_CAUSALITY_FORK_COUNTERFACTUAL_OF`` — same as target
        (new decisions will carry counterfactual_of=<target>)
    """
    try:
        if not plan.is_replayable:
            return False
        if plan.session_plan is None:
            return False

        replayer = SessionReplayer()
        replayer.apply_env(plan.session_plan, mode=mode)

        os.environ["JARVIS_CAUSALITY_FORK_FROM_RECORD_ID"] = (
            plan.target_record_id
        )
        os.environ["JARVIS_CAUSALITY_FORK_COUNTERFACTUAL_OF"] = (
            plan.target_record_id
        )
        return True
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "[replay_from_record] env apply failed: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Render plan summary
# ---------------------------------------------------------------------------


def render_replay_from_record_summary(
    plan: ReplayFromRecordPlan,
) -> str:
    """Human-readable summary.  NEVER raises."""
    try:
        lines = [
            f"[ReplayFromRecord] Session: {plan.session_id}",
            f"  target_record:  {plan.target_record_id}",
            f"  is_replayable:  {plan.is_replayable}",
        ]
        if plan.is_replayable:
            lines.extend([
                f"  dag_nodes:      {plan.dag_node_count}",
                f"  predecessors:   {plan.predecessor_count}",
            ])
        if not plan.is_replayable and plan.failure_reason:
            lines.append(f"  failure:        {plan.failure_reason}")
        if plan.diagnostics:
            lines.append("  diagnostics:")
            for d in plan.diagnostics:
                lines.append(f"    - {d}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return "[ReplayFromRecord] render error"


__all__ = [
    "ReplayFromRecordPlan",
    "apply_replay_from_record_env",
    "prepare_replay_from_record",
    "render_replay_from_record_summary",
    "replay_from_record_enabled",
]
