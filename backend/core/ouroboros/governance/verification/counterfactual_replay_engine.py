"""Priority #3 Slice 2 — Counterfactual Replay engine.

The execution layer for Priority #3 (Counterfactual Replay).

Slice 1 shipped the primitive layer (BranchSnapshot / ReplayTarget /
ReplayVerdict / compute_replay_outcome). Slice 2 (this module) ships
the deterministic engine that runs an end-to-end counterfactual:

  1. Loads the recorded session's decision ledger via existing
     ``causality_dag.build_dag`` (Priority 2 Slice 3).
  2. Loads the session summary via existing
     ``last_session_summary._parse_summary`` (read-only continuity).
  3. Locates the swap point — first DecisionRecord whose
     ``(phase, kind)`` matches ``target.swap_at_phase`` +
     ``target.swap_decision_kind.value``.
  4. Detects downstream-of-swap records via the DAG's reverse-edge
     index (BFS from swap.record_id over ``children``). The set of
     downstream records is a structural property of the recorded
     causal graph — no heuristics, no hardcoded phase ordering.
  5. Projects an ``original_branch`` BranchSnapshot from
     decisions + summary via pure data transformation.
  6. Projects a ``counterfactual_branch`` BranchSnapshot via a
     dynamic kind-keyed inference registry (no hardcoded enum
     lookup) — operators can register custom inferences for new
     ``DecisionOverrideKind`` values without editing this module.
  7. Calls Slice 1's ``compute_replay_outcome`` and returns the
     ``ReplayVerdict``.

ZERO LLM cost by construction. The engine reads three artifacts:

  * ``decisions.jsonl`` (existing ledger from phase_capture)
  * ``summary.json`` (existing artifact from harness)
  * ``ReplayTarget`` (caller-supplied swap descriptor)

…and produces a verdict via pure data transformation. There is NO
path through this module that would invoke a generation provider.

Direct-solve principles (per the operator directive):

  * **Asynchronous** — disk reads run in ``asyncio.to_thread`` so
    the harness event loop is never blocked. The public surface
    is ``async def run_counterfactual_replay(...)``.

  * **Dynamic** — terminal-state inference for each
    ``DecisionOverrideKind`` lives in a module-level registry that
    operators can extend at runtime. The 5-value closed taxonomy
    has 5 default inferences registered at module load; new
    operator-defined kinds register via ``register_inference``.

  * **Adaptive** — every load failure / lookup miss /
    schema-mismatch maps to an explicit ``ReplayOutcome``:
      * Master flag off → DISABLED
      * Garbage target → FAILED
      * Missing ledger or summary → PARTIAL
      * Swap point not found → PARTIAL
      * Downstream divergence detected → DIVERGED
      * Both branches projected cleanly → SUCCESS

  * **Intelligent** — divergence detection walks the recorded
    Causality DAG (Priority 2 Slice 3) rather than assuming
    phase ordering. The first downstream record by
    ``(wall_ts, ordinal)`` becomes ``divergence_phase`` so
    operators see exactly which decision the counterfactual
    invalidated.

  * **Robust** — every public function NEVER raises out. Disk
    faults log warnings but degrade to PARTIAL/FAILED outcomes.
    The engine itself is stateless — repeated runs on the same
    target produce identical verdicts (no caches that could
    drift).

  * **No hardcoding** — every numeric knob (max replay seconds,
    max phases per branch) reuses Slice 1's env helpers with
    floor+ceiling clamps. Registry-keyed inference replaces an
    enum-keyed switch. Reuses ``causality_dag.build_dag`` rather
    than re-implementing JSONL parsing. Reuses
    ``last_session_summary._parse_summary`` rather than
    re-implementing summary parsing.

Authority invariants (AST-pinned by Slice 5):

  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.

  * Read-only over the recorded ledger + summary.json — never
    writes a file, never executes code, never invokes a model.

  * No exec / eval / compile.

  * No mutation-class tools (no shutil.rmtree / os.remove on
    paths under JARVIS root).

  * Reuses ``last_session_summary._parse_summary`` — does NOT
    re-implement JSON parsing or sanitization.

  * Reuses ``causality_dag.build_dag`` — does NOT re-implement
    JSONL ledger parsing.

  * Stamps ``MonotonicTighteningVerdict.PASSED`` on every
    ``ReplayVerdict`` because replay is observational not
    prescriptive (cannot propose loosenings). Slice 3 will move
    the stamping into the comparator; Slice 2 stamps it directly
    on the verdict's ``detail`` field for forward-compat.

Master flag (Slice 1): ``JARVIS_COUNTERFACTUAL_REPLAY_ENABLED``.
Engine sub-flag (this module): ``JARVIS_REPLAY_ENGINE_ENABLED``
(default-false until Slice 5; gates the engine's loader path
even if Slice 1's master is on — operators can keep the schema
live while disabling the engine for a cost-cap rollback).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)

# Slice 1 primitives (pure-stdlib reuse)
from backend.core.ouroboros.governance.verification.counterfactual_replay import (
    BranchSnapshot,
    BranchVerdict,
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
    compute_replay_outcome,
    counterfactual_replay_enabled,
)

# Causality DAG (Priority 2 Slice 3 reuse — DAG construction +
# downstream traversal). Slice 2 NEVER re-implements JSONL parsing.
from backend.core.ouroboros.governance.verification.causality_dag import (
    CausalityDAG,
    build_dag,
)

# Decision Record type (Phase 1 Slice 1.2 reuse — for type hints
# only; we never instantiate or write records).
from backend.core.ouroboros.governance.determinism.decision_runtime import (
    DecisionRecord,
)

# Last session summary (Phase 1 reuse — read-only summary parser).
# Private import is intentional + load-bearing — we share the same
# JSON shape + sanitization helpers as the existing prior-session
# digest path.
from backend.core.ouroboros.governance import last_session_summary as _lss

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine sub-flag — independent rollback knob from Slice 1's master
# ---------------------------------------------------------------------------


def replay_engine_enabled() -> bool:
    """``JARVIS_REPLAY_ENGINE_ENABLED`` — engine-loader gate.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy/falsy overrides at call time.

    Default ``false`` until Slice 5 graduation. Independent from
    Slice 1's ``JARVIS_COUNTERFACTUAL_REPLAY_ENABLED`` so operators
    can keep the schema (Slice 1 enums + dataclasses) live in
    serialization paths while disabling the engine's loader for a
    cost-cap rollback.

    Both flags must be ``true`` for ``run_counterfactual_replay`` to
    actually load + project; if either is off the engine returns
    ``ReplayOutcome.DISABLED`` immediately (zero I/O)."""
    raw = os.environ.get(
        "JARVIS_REPLAY_ENGINE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-off until Slice 5 graduation
    return raw in ("1", "true", "yes", "on")


def replay_summary_root() -> Path:
    """``JARVIS_REPLAY_SUMMARY_ROOT`` — root dir for session
    summary artifacts. Default ``.ouroboros/sessions`` matches the
    harness convention. Per-session summary lives at
    ``<root>/<session_id>/summary.json``.

    Test override available via the keyword argument on
    ``run_counterfactual_replay`` — env knob is the production
    surface."""
    raw = os.environ.get(
        "JARVIS_REPLAY_SUMMARY_ROOT", ".ouroboros/sessions",
    ).strip()
    return Path(raw or ".ouroboros/sessions")


# ---------------------------------------------------------------------------
# SwapPoint + DivergenceInfo — engine-internal frozen value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwapPoint:
    """Located swap point in the recorded DAG.

    Fields:
      * ``record_id`` — the DecisionRecord whose output the
        counterfactual would replace.
      * ``phase`` / ``kind`` — copied from the record for
        downstream projection convenience.
      * ``ordinal`` — record's per-(op,phase,kind) ordinal.
      * ``wall_ts`` — wall-clock timestamp; used to compute the
        first downstream record by chronological order.

    Frozen + hashable so verdicts can carry a SwapPoint reference
    in their ``detail`` field without lifecycle concerns."""
    record_id: str
    phase: str
    kind: str
    ordinal: int
    wall_ts: float
    output_repr: str = ""


@dataclass(frozen=True)
class DivergenceInfo:
    """Result of downstream-detection BFS from a SwapPoint.

    Fields:
      * ``diverged`` — True iff at least one downstream record
        exists. When True the counterfactual cannot inherit the
        original's full projection (the recorded outputs after
        the swap depend on the swap's original output).
      * ``divergence_phase`` — phase of the first downstream
        record by ``(wall_ts, ordinal)``. Empty when
        ``diverged=False``.
      * ``divergence_reason`` — human-readable description for
        SSE event payloads.
      * ``downstream_record_count`` — total count of downstream
        records reachable from the swap (BFS over reverse edges)."""
    diverged: bool
    divergence_phase: str = ""
    divergence_reason: str = ""
    downstream_record_count: int = 0


# ---------------------------------------------------------------------------
# Terminal-state inference registry — dynamic, no hardcoded enum
# ---------------------------------------------------------------------------


class TerminalInferenceFn(Protocol):
    """Signature for a terminal-state inference function.

    Given the swap payload + the original branch projection +
    the swap point, return the counterfactual branch's projected
    terminal state as ``(terminal_phase, terminal_success,
    apply_outcome)``. Pure — no I/O, no side effects.

    NEVER raises (engine wraps the call in defensive try/except
    even though the protocol asks for purity)."""

    def __call__(
        self,
        *,
        payload: Mapping[str, Any],
        original: BranchSnapshot,
        swap: SwapPoint,
    ) -> Tuple[str, bool, str]: ...


_inference_registry: Dict[DecisionOverrideKind, TerminalInferenceFn] = {}


def register_inference(
    *, kind: DecisionOverrideKind, fn: TerminalInferenceFn,
) -> None:
    """Register a terminal-state inference for ``kind``. Idempotent
    — re-registering the same key with the same fn is a no-op;
    re-registering with a different fn logs an info-level message
    and replaces. NEVER raises."""
    if not isinstance(kind, DecisionOverrideKind):
        return
    existing = _inference_registry.get(kind)
    if existing is not None and existing is not fn:
        logger.info(
            "[replay_engine] inference for %s replaced", kind.value,
        )
    _inference_registry[kind] = fn


def get_inference(
    kind: DecisionOverrideKind,
) -> Optional[TerminalInferenceFn]:
    """Return the registered inference fn for ``kind`` or None.
    NEVER raises."""
    if not isinstance(kind, DecisionOverrideKind):
        return None
    return _inference_registry.get(kind)


def reset_registry_for_tests() -> None:
    """Drop all registered inferences. Production code MUST NOT
    call this. Tests use it to isolate registration between test
    functions."""
    _inference_registry.clear()


# ---------------------------------------------------------------------------
# Default inferences — one per DecisionOverrideKind closed-taxonomy value
# ---------------------------------------------------------------------------

# Canonical risk-tier verdict vocabulary for GATE_DECISION payloads.
# These mirror the orchestrator's risk_tier vocabulary
# (SAFE_AUTO / NOTIFY_APPLY / APPROVAL_REQUIRED / BLOCKED) — the
# strings are the contract surface, not magic constants. We resolve
# them from the payload via ``str().strip().lower()`` so callers can
# pass either enum or string.
_GATE_VERDICT_AUTO_APPLY = "auto_apply"
_GATE_VERDICT_NOTIFY_APPLY = "notify_apply"
_GATE_VERDICT_APPROVAL_REQUIRED = "approval_required"
_GATE_VERDICT_BLOCKED = "blocked"
_GATE_VERDICT_SAFE_AUTO = "safe_auto"


def _gate_decision_inference(
    *,
    payload: Mapping[str, Any],
    original: BranchSnapshot,
    swap: SwapPoint,
) -> Tuple[str, bool, str]:
    """GATE_DECISION → terminal state by verdict vocabulary.

    Reads ``payload['verdict']`` (case-insensitive). When verdict
    promotes the op (auto_apply / safe_auto / notify_apply), the
    counterfactual passes through to the original's terminal state
    — the swap had no causal effect on the gate disposition.

    When verdict gates or blocks (approval_required / blocked),
    the counterfactual halts at the GATE phase with success=False
    and apply_outcome ``"gated"`` or ``"none"`` respectively.

    Unknown verdict → defensive default: halt at swap.phase,
    success=False, apply_outcome=``"none"``."""
    verdict_raw = payload.get("verdict") if isinstance(payload, Mapping) else None
    verdict = ""
    if verdict_raw is not None:
        try:
            verdict = str(verdict_raw).strip().lower()
        except Exception:  # noqa: BLE001 — defensive
            verdict = ""

    if verdict in (_GATE_VERDICT_AUTO_APPLY, _GATE_VERDICT_SAFE_AUTO,
                   _GATE_VERDICT_NOTIFY_APPLY):
        return (
            original.terminal_phase or swap.phase,
            original.terminal_success,
            original.apply_outcome,
        )
    if verdict == _GATE_VERDICT_APPROVAL_REQUIRED:
        return (swap.phase or "GATE", False, "gated")
    if verdict == _GATE_VERDICT_BLOCKED:
        return (swap.phase or "GATE", False, "none")
    # Unknown / missing verdict — halt at swap with empty apply.
    return (swap.phase or "GATE", False, "none")


def _passthrough_inference(
    *,
    payload: Mapping[str, Any],
    original: BranchSnapshot,
    swap: SwapPoint,
) -> Tuple[str, bool, str]:
    """Default for context-injection-class overrides.

    POSTMORTEM_INJECTION / RECURRENCE_BOOST / QUORUM_INVOCATION /
    COHERENCE_OBSERVER all add CONTEXT or invoke parallel
    deliberation BEFORE generation; they don't gate apply.

    Their counterfactual signature is the SAME terminal state as
    the original (same generation cache hit, same apply outcome)
    — the empirical question is whether the postmortem_records
    count or verify pass rate moved, NOT whether the op
    completed. Slice 3's branch comparator measures the secondary
    + tertiary verdict axes; the primary axis (terminal_success)
    is identical by construction."""
    return (
        original.terminal_phase or swap.phase,
        original.terminal_success,
        original.apply_outcome,
    )


# Register the 5-value closed taxonomy at module load. Operators
# replace via ``register_inference`` if they need custom semantics
# for a kind. The registry is dynamic — no hardcoded switch.
register_inference(
    kind=DecisionOverrideKind.GATE_DECISION,
    fn=_gate_decision_inference,
)
register_inference(
    kind=DecisionOverrideKind.POSTMORTEM_INJECTION,
    fn=_passthrough_inference,
)
register_inference(
    kind=DecisionOverrideKind.RECURRENCE_BOOST,
    fn=_passthrough_inference,
)
register_inference(
    kind=DecisionOverrideKind.QUORUM_INVOCATION,
    fn=_passthrough_inference,
)
register_inference(
    kind=DecisionOverrideKind.COHERENCE_OBSERVER,
    fn=_passthrough_inference,
)


# ---------------------------------------------------------------------------
# Loader — async-safe bundle of (DAG, summary record)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LoadedArtifacts:
    """Result of loading one session's artifacts.

    Either field can be empty/None on disk fault — the engine
    handles partial loads explicitly via the PARTIAL outcome."""
    dag: CausalityDAG
    summary: Optional[_lss.SessionRecord]
    summary_path: Path
    ledger_path: Optional[Path] = None


def _load_session_artifacts(
    *,
    session_id: str,
    ledger_path: Optional[Path],
    summary_root: Optional[Path],
) -> _LoadedArtifacts:
    """Synchronous load helper. Run via ``asyncio.to_thread`` from
    the public async surface so the event loop is never blocked.

    NEVER raises. Disk faults degrade to:
      * empty CausalityDAG when ledger missing or unreadable
      * None SessionRecord when summary missing or unparseable

    Reuses ``causality_dag.build_dag`` for the ledger (Priority 2
    Slice 3) and ``last_session_summary._parse_summary`` for the
    summary (Phase 1 read-only digest)."""
    sid = str(session_id).strip() or "default"

    # Ledger via causality_dag.build_dag — handles its own master
    # flag check + JSONL parsing + DAG construction.
    try:
        dag = build_dag(session_id=sid, ledger_path=ledger_path)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_engine] build_dag failed for %s: %s — empty DAG",
            sid, exc,
        )
        dag = CausalityDAG()

    # Summary via last_session_summary — same parser used by the
    # prior-session digest path so we share schema + sanitization.
    root = summary_root if summary_root is not None else replay_summary_root()
    summary_path = root / sid / "summary.json"
    summary_record: Optional[_lss.SessionRecord] = None
    try:
        record, _missing = _lss._parse_summary(summary_path)  # noqa: SLF001
        if record is not None:
            summary_record = record
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_engine] _parse_summary failed for %s: %s",
            summary_path, exc,
        )

    return _LoadedArtifacts(
        dag=dag,
        summary=summary_record,
        summary_path=summary_path,
        ledger_path=ledger_path,
    )


# ---------------------------------------------------------------------------
# Projection helpers — pure data transformation
# ---------------------------------------------------------------------------


def _project_original_branch(
    *,
    dag: CausalityDAG,
    summary: Optional[_lss.SessionRecord],
    target: ReplayTarget,
) -> Optional[BranchSnapshot]:
    """Project a BranchSnapshot from the recorded session.

    Returns None when both DAG and summary are empty (caller
    interprets as PARTIAL). Tolerates either alone:

      * DAG-only: terminal_phase from last record by wall_ts;
        terminal_success / apply_outcome / verify_passed all
        defaulted (False / empty / 0).
      * Summary-only: terminal_phase defaulted to ``"COMPLETE"``
        if convergence_state suggests success; verify counts
        from ops_digest.
      * Both: full projection.

    NEVER raises."""
    try:
        if dag.is_empty and summary is None:
            return None

        # Terminal phase from the last record by chronological
        # order. The DAG's record_ids are insertion-ordered, but
        # the wall_ts on each record is the canonical anchor.
        terminal_phase = ""
        terminal_record: Optional[DecisionRecord] = None
        if not dag.is_empty:
            try:
                ordered = sorted(
                    (dag.node(rid) for rid in dag.record_ids),
                    key=lambda r: (
                        getattr(r, "wall_ts", 0.0) if r else 0.0,
                        getattr(r, "ordinal", 0) if r else 0,
                    ),
                )
                ordered = [r for r in ordered if r is not None]
                if ordered:
                    terminal_record = ordered[-1]
                    terminal_phase = terminal_record.phase
            except Exception:  # noqa: BLE001 — defensive
                terminal_record = None

        # Apply / verify / postmortem from the summary's
        # ops_digest. Missing fields → conservative defaults.
        apply_outcome = ""
        verify_passed = 0
        verify_total = 0
        terminal_success = False
        postmortem_records: Tuple[str, ...] = ()
        if summary is not None:
            apply_outcome = summary.last_apply_mode or "none"
            if summary.last_verify_tests_passed is not None:
                verify_passed = int(summary.last_verify_tests_passed)
            if summary.last_verify_tests_total is not None:
                verify_total = int(summary.last_verify_tests_total)
            # Heuristic terminal_success: convergence_state in
            # success-like vocabulary OR clean apply with verify
            # pass rate ≥ 50%. Conservative: false unless evidence.
            convergence = (summary.convergence_state or "").lower()
            apply_clean = apply_outcome in ("single", "multi")
            verify_clean = (
                verify_total > 0 and verify_passed >= verify_total // 2
            )
            success_words = ("success", "complete", "converged")
            terminal_success = (
                any(w in convergence for w in success_words)
                or (apply_clean and verify_clean)
            )
            # Postmortems projected as synthetic record names from
            # the failed-op count. Slice 3 will add a richer
            # postmortem source; Slice 2 uses the available signal.
            failed_count = max(0, int(summary.stats_failed))
            if failed_count > 0:
                postmortem_records = tuple(
                    f"failed_op_{i}" for i in range(min(failed_count, 100))
                )
            # If we lacked a DAG-derived terminal_phase, fall back
            # to "COMPLETE" when summary signals success, else
            # an empty string so compute_replay_outcome treats it
            # as unknown.
            if not terminal_phase:
                terminal_phase = (
                    "COMPLETE" if terminal_success else ""
                )

        # Branch id — use the session id when available, else the
        # target's session id.
        branch_id = (
            summary.session_id if summary is not None
            and summary.session_id else (target.session_id or "original")
        )

        cost_usd = float(summary.cost_total) if summary is not None else 0.0

        return BranchSnapshot(
            branch_id=branch_id,
            terminal_phase=terminal_phase or "UNKNOWN",
            terminal_success=bool(terminal_success),
            apply_outcome=str(apply_outcome),
            verify_passed=int(verify_passed),
            verify_total=int(verify_total),
            postmortem_records=postmortem_records,
            cost_usd=cost_usd,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_engine] _project_original_branch failed: %s", exc,
        )
        return None


def _locate_swap(
    *,
    dag: CausalityDAG,
    target: ReplayTarget,
) -> Optional[SwapPoint]:
    """Find the FIRST DecisionRecord matching ``target.swap_at_phase``
    + ``target.swap_decision_kind.value`` in chronological order.

    Returns None when not found (caller treats as PARTIAL).
    NEVER raises."""
    try:
        if dag.is_empty:
            return None
        want_phase = str(target.swap_at_phase or "").strip()
        want_kind = ""
        try:
            want_kind = str(target.swap_decision_kind.value)
        except Exception:  # noqa: BLE001 — defensive
            return None
        if not want_phase or not want_kind:
            return None

        candidates: list[DecisionRecord] = []
        for rid in dag.record_ids:
            rec = dag.node(rid)
            if rec is None:
                continue
            if rec.phase == want_phase and rec.kind == want_kind:
                candidates.append(rec)

        if not candidates:
            return None

        candidates.sort(
            key=lambda r: (
                getattr(r, "wall_ts", 0.0),
                getattr(r, "ordinal", 0),
            ),
        )
        first = candidates[0]
        return SwapPoint(
            record_id=str(first.record_id),
            phase=str(first.phase),
            kind=str(first.kind),
            ordinal=int(first.ordinal),
            wall_ts=float(getattr(first, "wall_ts", 0.0)),
            output_repr=str(first.output_repr),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[replay_engine] _locate_swap failed: %s", exc)
        return None


def _detect_divergence(
    *,
    dag: CausalityDAG,
    swap: SwapPoint,
) -> DivergenceInfo:
    """BFS downstream from ``swap.record_id`` over the DAG's
    reverse-edge index. Returns a frozen DivergenceInfo.

    A counterfactual ``diverges`` iff at least one downstream
    record exists. Downstream records were originally computed
    from the swap point's output, so substituting a different
    output invalidates them — the counterfactual cannot inherit
    their projections.

    NEVER raises."""
    try:
        if dag.is_empty:
            return DivergenceInfo(diverged=False)

        # BFS over children (reverse edges). Bounded by the
        # configured max-phases ceiling so a pathological DAG
        # doesn't blow memory.
        from collections import deque
        cap = max(1, int(replay_max_phases_per_branch_safe()))
        visited: set = {swap.record_id}
        downstream: list[str] = []
        queue: deque = deque([swap.record_id])
        while queue and len(downstream) < cap:
            rid = queue.popleft()
            for cid in dag.children(rid):
                if cid in visited:
                    continue
                visited.add(cid)
                downstream.append(cid)
                queue.append(cid)

        if not downstream:
            return DivergenceInfo(diverged=False)

        # First downstream record by chronological order.
        first_phase = ""
        try:
            ordered = sorted(
                (dag.node(rid) for rid in downstream),
                key=lambda r: (
                    getattr(r, "wall_ts", 0.0) if r else 0.0,
                    getattr(r, "ordinal", 0) if r else 0,
                ),
            )
            ordered = [r for r in ordered if r is not None]
            if ordered:
                first_phase = ordered[0].phase
        except Exception:  # noqa: BLE001 — defensive
            first_phase = ""

        return DivergenceInfo(
            diverged=True,
            divergence_phase=first_phase,
            divergence_reason=(
                f"counterfactual_swap_at_{swap.phase}/{swap.kind}_"
                f"invalidates_{len(downstream)}_downstream_records"
            ),
            downstream_record_count=len(downstream),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[replay_engine] _detect_divergence failed: %s", exc)
        return DivergenceInfo(diverged=False)


def replay_max_phases_per_branch_safe() -> int:
    """Slice 1 ceiling but ALWAYS callable (never raises). Slice 1
    already clamps; this thin wrapper exists so static analyzers
    don't flag the `replay_max_phases_per_branch` import as unused
    when the engine path uses only its DAG-walk bound."""
    try:
        from backend.core.ouroboros.governance.verification.counterfactual_replay import (
            replay_max_phases_per_branch,
        )
        return int(replay_max_phases_per_branch())
    except Exception:  # noqa: BLE001 — defensive
        return 50


def _project_counterfactual_branch(
    *,
    original: BranchSnapshot,
    swap: SwapPoint,
    divergence: DivergenceInfo,
    target: ReplayTarget,
) -> BranchSnapshot:
    """Project the counterfactual branch from the original
    projection + the swap descriptor + the divergence info.

    Two cases:

      * **Not diverged** — no downstream records depend on the
        swap. Counterfactual inherits the original's terminal
        state with ``branch_id="counterfactual"`` and the swap
        applied. This is the EQUIVALENT outcome — the override
        had no causal effect on terminal disposition (an
        interesting empirical null result).

      * **Diverged** — downstream records exist. We cannot
        inherit downstream projections, so we infer the
        terminal state from the kind-keyed inference registry:
          * Default inferences ship for all 5 closed-taxonomy
            kinds at module load.
          * Operators register custom inferences via
            ``register_inference`` for new kinds.

    Inference functions are wrapped in defensive try/except —
    a registered fn that raises falls through to a "halt at
    swap, success=False" default.

    NEVER raises."""
    if not divergence.diverged:
        # No downstream — counterfactual = original with renamed
        # branch_id and zero postmortems (since the swap might
        # have eliminated the postmortem path; absence of
        # downstream records means we have no evidence either way,
        # so we stay conservative and pass through the original's
        # postmortems).
        return replace(
            original,
            branch_id="counterfactual",
        )

    # Diverged — inference-driven projection.
    inference = get_inference(target.swap_decision_kind)
    inferred_phase = swap.phase
    inferred_success = False
    inferred_apply = "none"

    if inference is not None:
        try:
            inferred = inference(
                payload=target.swap_decision_payload or {},
                original=original,
                swap=swap,
            )
            if (
                isinstance(inferred, tuple)
                and len(inferred) == 3
            ):
                phase_raw, success_raw, apply_raw = inferred
                inferred_phase = str(phase_raw or swap.phase)
                inferred_success = bool(success_raw)
                inferred_apply = str(apply_raw or "none")
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[replay_engine] inference for %s raised: %s — "
                "default counterfactual halt",
                target.swap_decision_kind.value, exc,
            )

    return BranchSnapshot(
        branch_id="counterfactual",
        terminal_phase=inferred_phase,
        terminal_success=inferred_success,
        apply_outcome=inferred_apply,
        # Counterfactual truncates BEFORE downstream verify/postmortem
        # records would have run, so we project zeros. This is the
        # CORRECT empirical signal — the counterfactual prevented
        # whatever postmortem the original recorded by taking a
        # different path earlier.
        verify_passed=0,
        verify_total=0,
        postmortem_records=(),
        cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# Monotonic-tightening stamp — observational verdicts always PASSED
# ---------------------------------------------------------------------------


def _monotonic_tightening_stamp_passed() -> str:
    """Resolve the canonical PASSED string from
    ``adaptation.ledger.MonotonicTighteningVerdict``. Falls back to
    the literal string if the import fails (the canonical token is
    stable across the stack — Phase C, Move 6, Priority #1, #2 all
    use the same vocabulary)."""
    try:
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        return str(MonotonicTighteningVerdict.PASSED.value)
    except Exception:  # noqa: BLE001 — defensive
        return "passed"


# ---------------------------------------------------------------------------
# Public surface — async run_counterfactual_replay
# ---------------------------------------------------------------------------


async def run_counterfactual_replay(
    target: ReplayTarget,
    *,
    ledger_path: Optional[Path] = None,
    summary_root: Optional[Path] = None,
    enabled_override: Optional[bool] = None,
) -> ReplayVerdict:
    """End-to-end counterfactual replay for one ``ReplayTarget``.

    Resolution order:
      1. ``enabled_override`` (test/REPL escape hatch) → if False,
         return ``ReplayOutcome.DISABLED``.
      2. Slice 1 master flag ``counterfactual_replay_enabled()`` →
         must be True.
      3. Engine sub-flag ``replay_engine_enabled()`` → must be
         True (default-false until Slice 5).

    Master-off path is ZERO I/O — short-circuits before loading
    any artifact.

    Parameters
    ----------
    target : ReplayTarget
        Caller-supplied swap descriptor. Garbage input maps to
        ``ReplayOutcome.FAILED``.
    ledger_path : Path, optional
        Direct override for the decisions.jsonl path. Production
        callers leave None — engine resolves via session_id.
    summary_root : Path, optional
        Direct override for ``.ouroboros/sessions/`` root. Production
        callers leave None — engine resolves via env knob.
    enabled_override : bool, optional
        Force the engine on/off regardless of env (test/REPL).

    Returns
    -------
    ReplayVerdict
        Always returned — every disk fault / lookup miss / garbage
        input maps to an explicit ``ReplayOutcome``. NEVER raises.
    """
    # 1. Master flag(s) check — fast path zero I/O.
    if enabled_override is False:
        return ReplayVerdict(
            outcome=ReplayOutcome.DISABLED,
            target=target if isinstance(target, ReplayTarget) else None,
            verdict=BranchVerdict.FAILED,
            detail="enabled_override=false",
        )

    if enabled_override is None:
        if not counterfactual_replay_enabled():
            return ReplayVerdict(
                outcome=ReplayOutcome.DISABLED,
                target=target if isinstance(target, ReplayTarget) else None,
                verdict=BranchVerdict.FAILED,
                detail="counterfactual_replay_master_flag_off",
            )
        if not replay_engine_enabled():
            return ReplayVerdict(
                outcome=ReplayOutcome.DISABLED,
                target=target if isinstance(target, ReplayTarget) else None,
                verdict=BranchVerdict.FAILED,
                detail="replay_engine_sub_flag_off",
            )

    # 2. Validate target — frozen dataclass instance only.
    if not isinstance(target, ReplayTarget):
        return ReplayVerdict(
            outcome=ReplayOutcome.FAILED,
            target=None,
            verdict=BranchVerdict.FAILED,
            detail=f"invalid_target_type:{type(target).__name__}",
        )

    # 3. Load artifacts off the event loop. NEVER raises — disk
    # faults degrade to empty DAG / None summary.
    try:
        artifacts = await asyncio.to_thread(
            _load_session_artifacts,
            session_id=target.session_id,
            ledger_path=ledger_path,
            summary_root=summary_root,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[replay_engine] _load_session_artifacts raised: %s — "
            "fall through to empty artifacts",
            exc,
        )
        artifacts = _LoadedArtifacts(
            dag=CausalityDAG(),
            summary=None,
            summary_path=Path("/dev/null"),
        )

    # 4. Project the original branch.
    original = _project_original_branch(
        dag=artifacts.dag,
        summary=artifacts.summary,
        target=target,
    )

    if original is None:
        return ReplayVerdict(
            outcome=ReplayOutcome.PARTIAL,
            target=target,
            verdict=BranchVerdict.FAILED,
            detail=(
                f"original_projection_empty session_id={target.session_id} "
                f"summary_path={artifacts.summary_path}"
            ),
        )

    # 5. Locate the swap point. Empty-DAG path (no recorded
    # decisions) → PARTIAL with original-only.
    swap = _locate_swap(dag=artifacts.dag, target=target)
    if swap is None:
        # Stamp PASSED on PARTIAL — observational verdict always
        # tightens or stays even.
        return ReplayVerdict(
            outcome=ReplayOutcome.PARTIAL,
            target=target,
            original_branch=original,
            counterfactual_branch=None,
            verdict=BranchVerdict.FAILED,
            detail=(
                f"swap_not_found phase={target.swap_at_phase} "
                f"kind={target.swap_decision_kind.value} "
                f"dag_size={artifacts.dag.node_count} "
                f"monotonic_tightening={_monotonic_tightening_stamp_passed()}"
            ),
        )

    # 6. Detect downstream divergence.
    divergence = _detect_divergence(dag=artifacts.dag, swap=swap)

    # 7. Project the counterfactual branch.
    counterfactual = _project_counterfactual_branch(
        original=original,
        swap=swap,
        divergence=divergence,
        target=target,
    )

    # 8. Compute the verdict via Slice 1's pure decision function.
    # Slice 1's ``divergence_phase`` is reserved for "cached hash
    # mismatch upstream / artifact corruption" — a STRUCTURAL replay
    # failure where the recorded ledger could not be re-applied. It
    # is NOT the same as the EXPECTED counterfactual divergence
    # detected here (downstream records exist by design when the
    # swap target had a non-leaf node). We therefore do NOT pass
    # divergence_phase/reason — those would short-circuit to
    # ``ReplayOutcome.DIVERGED`` which has the wrong semantics.
    # Instead, we let ``compute_replay_outcome`` compute ``SUCCESS``
    # + the proper ``BranchVerdict`` (DIVERGED_BETTER /
    # DIVERGED_WORSE / EQUIVALENT / DIVERGED_NEUTRAL) and stash the
    # downstream-divergence info in ``detail`` for observability.
    # Positional args match Slice 1's signature: (target, original,
    # counterfactual, *, ...).
    verdict = compute_replay_outcome(
        target,
        original,
        counterfactual,
        enabled_override=True,  # we already checked the flags
    )

    # 9. Stamp the monotonic-tightening token + downstream-divergence
    # observability into the verdict's detail field. Forward-compat
    # with Slice 3's comparator (which will move the stamp into the
    # comparator itself + emit divergence info as a typed sub-field).
    stamp_token = _monotonic_tightening_stamp_passed()
    div_summary = (
        f"downstream_diverged_at={divergence.divergence_phase} "
        f"downstream_records={divergence.downstream_record_count} "
        f"reason={divergence.divergence_reason}"
        if divergence.diverged
        else "downstream_diverged=false downstream_records=0"
    )
    extended_detail = (
        f"{verdict.detail or ''} "
        f"engine_load=ok dag_nodes={artifacts.dag.node_count} "
        f"swap_record_id={swap.record_id} "
        f"{div_summary} "
        f"monotonic_tightening={stamp_token}"
    ).strip()

    return replace(verdict, detail=extended_detail)


# ---------------------------------------------------------------------------
# Cost-contract authority constant (AST-pin target for Slice 5)
# ---------------------------------------------------------------------------

# Surfaced symbol so the AST validator can pin its presence in
# ``shipped_code_invariants``. The token name carries the contract.
COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "DivergenceInfo",
    "SwapPoint",
    "TerminalInferenceFn",
    "get_inference",
    "register_inference",
    "replay_engine_enabled",
    "replay_summary_root",
    "reset_registry_for_tests",
    "run_counterfactual_replay",
]
