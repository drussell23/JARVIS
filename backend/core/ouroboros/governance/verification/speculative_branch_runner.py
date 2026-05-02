"""Priority #4 Slice 2 — Speculative Branch Tree async runner.

The execution layer for SBT. Slice 1 shipped pure data + closed-taxonomy
decision functions. Slice 2 (this module) ships the async runner that:

  1. Spawns N parallel branches at the root (level 0) using
     ``asyncio.create_task`` + ``asyncio.as_completed`` — same pattern
     as ``confidence_probe_runner.run_probe_loop``.

  2. Collects ``BranchEvidence`` from each branch via an injectable
     ``BranchProber`` Protocol. Production wiring filters Venom's
     ``tool_executor`` to ``READONLY_TOOL_ALLOWLIST`` (9-tool
     frozenset reused from ``readonly_evidence_prober``); tests
     inject capturing fakes; default is ``_NullBranchProber``.

  3. Computes the level-0 verdict via Slice 1's
     ``compute_tree_verdict``. If verdict is ``CONVERGED`` /
     ``INCONCLUSIVE`` / ``TRUNCATED`` / ``FAILED`` → return
     immediately (no tie-breaker spawn).

  4. **If DIVERGED and depth permits** → spawn ONE tie-breaker level
     of ``max_breadth`` branches. Each tie-breaker branch receives
     the level-0 aggregated evidence as ``prior_evidence`` so the
     prober can ask sharper follow-up questions ("level 0 disagreed
     on X — verify via Y"). Same parallel-with-early-stop pattern.

  5. Re-computes verdict on the union of all branches. Caps at
     ``effective_max_depth`` (env-tunable via Slice 1). When the cap
     is hit and verdict is still DIVERGED, returns DIVERGED — the
     ambiguity is genuine; operators see this as escalation signal.

  6. Wall-clock cap enforced via ``asyncio.wait_for`` at the gather
     boundary. Wall-cap hit → cancel pending + return TRUNCATED.

  7. Diminishing-returns dedup: if K consecutive branches at the
     same level produce the same fingerprint AND
     ``threshold * level_count <= same_fp_count`` → cancel remaining
     pending branches at that level (no new information).

ZERO-LLM cost on the convergence + projection path (the only LLM
costs are inside the BRANCH execution path, bounded structurally by
``max_depth × max_breadth × per-tool budget``). The runner itself
only orchestrates + scores.

Direct-solve principles:

  * **Asynchronous** — every branch runs in an ``asyncio.Task``;
    sync prober calls wrap in ``asyncio.to_thread``. The wall-clock
    cap is enforced at the gather boundary.

  * **Dynamic** — every cap (depth, breadth, wall-time, dim-returns)
    is env-tunable via Slice 1's helpers. NO hardcoded magic
    constants.

  * **Adaptive** — degraded inputs map to typed outcomes:
      - master flag off → TreeVerdict.FAILED with detail
      - garbage target → TreeVerdict.FAILED
      - prober raises → BranchOutcome.FAILED (per-branch isolated)
      - branch task wall-cap hit → BranchOutcome.TIMEOUT
      - tree wall-cap hit → TreeVerdict.TRUNCATED

  * **Intelligent** — tie-breaker level only spawns on DIVERGED.
    CONVERGED at level 0 means the ambiguity resolved cheaply (the
    common case); operators get the answer without spending the
    deeper budget.

  * **Robust** — every public function NEVER raises out. Per-branch
    cancellation propagates cleanly via ``_cancel_pending`` (same
    pattern as confidence_probe_runner).

  * **No hardcoding** — branch IDs deterministically constructed
    from ``(decision_id, level, position)``; tool allowlist reused
    from `readonly_evidence_prober.READONLY_TOOL_ALLOWLIST`; cap
    helpers from Slice 1.

Authority invariants (AST-pinned by Slice 5):

  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.

  * Read-only contract enforced via ``READONLY_TOOL_ALLOWLIST``
    reuse (Move 5 reuse — no re-implementation of the 9-tool
    frozenset).

  * No exec / eval / compile (mirrors Slice 1 + Move 6 + Priority
    #1/#2/#3 critical safety pin).

  * Reuses Slice 1's primitives + decision functions — does NOT
    re-implement convergence logic.

Master flag (Slice 1): ``JARVIS_SBT_ENABLED``. Engine sub-flag (this
module): ``JARVIS_SBT_RUNNER_ENABLED`` (default-false until Slice 5;
gates the runner's loader path even if Slice 1's master is on —
operators can keep the schema live while disabling the runner for a
cost-cap rollback).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import replace
from typing import (
    Any,
    List,
    Optional,
    Protocol,
    Tuple,
)

# Slice 1 primitives (pure-stdlib reuse).
from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchEvidence,
    BranchOutcome,
    BranchResult,
    BranchTreeTarget,
    TreeVerdict,
    TreeVerdictResult,
    canonical_evidence_fingerprint,
    compute_tree_outcome,
    compute_tree_verdict,
    sbt_diminishing_returns_threshold,
    sbt_enabled,
)

# Move 5 reuse — the canonical read-only tool allowlist + per-tool
# allowlist check. NEVER re-implements the 9-tool frozenset.
from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (
    READONLY_TOOL_ALLOWLIST,
    is_tool_allowlisted,
)

logger = logging.getLogger(__name__)


SBT_RUNNER_SCHEMA_VERSION: str = "speculative_branch_runner.1"


# ---------------------------------------------------------------------------
# Sub-flag — independent rollback knob from Slice 1's master
# ---------------------------------------------------------------------------


def sbt_runner_enabled() -> bool:
    """``JARVIS_SBT_RUNNER_ENABLED`` — runner-loader gate.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy/falsy overrides at call time.

    Default ``false`` until Slice 5 graduation. Independent from
    Slice 1's ``JARVIS_SBT_ENABLED`` so operators can keep the schema
    live in serialization paths while disabling the runner's loader
    for a cost-cap rollback.

    Both flags must be ``true`` for ``run_speculative_tree`` to
    actually fire branches; if either is off the runner returns
    ``TreeVerdict.FAILED`` immediately (zero I/O / LLM cost)."""
    raw = os.environ.get(
        "JARVIS_SBT_RUNNER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-off until Slice 5 graduation
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# BranchProber Protocol — per-branch evidence-collection surface
# ---------------------------------------------------------------------------


class BranchProber(Protocol):
    """Per-branch read-only evidence collector.

    Production wiring filters Venom's ``tool_executor`` to
    ``READONLY_TOOL_ALLOWLIST``; tests inject capturing fakes;
    default is ``_NullBranchProber``.

    Implementations MUST NOT raise — but if they do, the runner
    catches at the boundary and converts to ``BranchOutcome.FAILED``
    with empty evidence. Implementations MUST NOT call mutation
    tools (defense in depth — runner ALSO checks per-evidence
    ``source_tool`` against the allowlist).

    The protocol is sync to mirror ``ReadonlyToolBackend`` (the
    runner wraps each call in ``asyncio.to_thread`` so the event
    loop is never blocked). Async implementations work too — Python
    duck-types this away."""

    def probe_branch(
        self,
        *,
        target: BranchTreeTarget,
        branch_id: str,
        depth: int,
        prior_evidence: Tuple[BranchEvidence, ...] = (),
    ) -> Tuple[BranchEvidence, ...]:
        """Run one branch's read-only probes; return collected
        evidence as a frozen tuple. Empty tuple → branch produced
        no evidence (caller treats as PARTIAL).

        ``prior_evidence`` carries aggregated evidence from earlier
        levels (only non-empty on level ≥ 1 tie-breaker spawns).
        Production probers should use this to ask sharper
        follow-ups ("level 0 disagreed on X — verify via Y").

        NEVER raises (defensive contract is implementation-owned;
        runner catches anyway)."""
        ...  # Protocol


class _NullBranchProber:
    """Default safe-fallback prober — returns empty evidence on
    every call. Used when caller doesn't supply a prober AND the
    sub-flag is on (which would only happen in a misconfigured
    environment; production always supplies a prober).

    Empty-evidence branches are typed as PARTIAL by the runner's
    classification logic — convergence won't fire on PARTIAL alone,
    so the tree returns INCONCLUSIVE. Safer than asserting the
    caller MUST supply a prober (which would raise)."""

    def probe_branch(
        self,
        *,
        target: BranchTreeTarget,
        branch_id: str,
        depth: int,
        prior_evidence: Tuple[BranchEvidence, ...] = (),
    ) -> Tuple[BranchEvidence, ...]:
        return ()


_DEFAULT_PROBER: BranchProber = _NullBranchProber()


# ---------------------------------------------------------------------------
# Per-branch task wrapper
# ---------------------------------------------------------------------------


def _build_branch_id(
    target: BranchTreeTarget, level: int, position: int,
) -> str:
    """Deterministic branch ID from tree position. Same target +
    same level/position → same branch_id, enabling replay."""
    decision = (
        str(target.decision_id) if isinstance(target, BranchTreeTarget)
        else "unknown"
    )
    return f"{decision}.L{level}.P{position}"


def _classify_evidence_outcome(
    evidence: Tuple[BranchEvidence, ...],
) -> BranchOutcome:
    """Map an evidence tuple to a BranchOutcome:
      * empty evidence → PARTIAL (branch ran but produced nothing)
      * any evidence with confidence > 0 → SUCCESS
      * all evidence with confidence == 0 → PARTIAL

    NEVER raises."""
    try:
        if not evidence:
            return BranchOutcome.PARTIAL
        if any(e.confidence > 0.0 for e in evidence):
            return BranchOutcome.SUCCESS
        return BranchOutcome.PARTIAL
    except Exception:  # noqa: BLE001 — defensive
        return BranchOutcome.PARTIAL


def _filter_evidence_to_allowlist(
    evidence: Tuple[BranchEvidence, ...],
) -> Tuple[BranchEvidence, ...]:
    """Defense-in-depth: drop any evidence whose ``source_tool`` is
    NOT in ``READONLY_TOOL_ALLOWLIST``. Production probers should
    already filter; this is the runner's belt-and-suspenders check.

    Empty source_tool is allowed — some evidence kinds (e.g.,
    TYPE_INFERENCE from AST analysis) don't have a tool name.

    NEVER raises."""
    try:
        result: List[BranchEvidence] = []
        for ev in evidence:
            tool = str(ev.source_tool or "").strip()
            if tool == "" or is_tool_allowlisted(tool):
                result.append(ev)
            else:
                logger.debug(
                    "[sbt_runner] dropping non-allowlist evidence "
                    "with source_tool=%r",
                    tool,
                )
        return tuple(result)
    except Exception:  # noqa: BLE001 — defensive
        return ()


async def _run_one_branch(
    *,
    prober: BranchProber,
    target: BranchTreeTarget,
    level: int,
    position: int,
    prior_evidence: Tuple[BranchEvidence, ...],
    per_branch_wall_seconds: float,
) -> BranchResult:
    """Execute one branch. Wraps the sync prober call in
    ``asyncio.to_thread`` and ``asyncio.wait_for``. NEVER raises.

    Wall-cap hit → BranchOutcome.TIMEOUT with whatever partial
    evidence the prober produced (None if nothing returned).
    Prober raise → BranchOutcome.FAILED with error_detail.
    Cancellation → re-raise (runner cleanup awaits)."""
    branch_id = _build_branch_id(target, level, position)
    started_mono = time.monotonic()
    try:
        evidence = await asyncio.wait_for(
            asyncio.to_thread(
                prober.probe_branch,
                target=target,
                branch_id=branch_id,
                depth=level,
                prior_evidence=prior_evidence,
            ),
            timeout=per_branch_wall_seconds,
        )
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - started_mono) * 1000.0
        return BranchResult(
            branch_id=branch_id,
            outcome=BranchOutcome.TIMEOUT,
            evidence=(),
            elapsed_ms=elapsed_ms,
            depth=level,
            fingerprint="",
            error_detail=(
                f"branch_wall_cap_exceeded_after_{per_branch_wall_seconds:.1f}s"
            ),
        )
    except asyncio.CancelledError:
        # Propagate cancellation up to the gather boundary; runner
        # will collect this branch's CancelledError + record empty.
        raise
    except Exception as exc:  # noqa: BLE001 — defensive
        elapsed_ms = (time.monotonic() - started_mono) * 1000.0
        logger.debug(
            "[sbt_runner] branch %s raised: %s", branch_id, exc,
        )
        return BranchResult(
            branch_id=branch_id,
            outcome=BranchOutcome.FAILED,
            evidence=(),
            elapsed_ms=elapsed_ms,
            depth=level,
            fingerprint="",
            error_detail=f"{type(exc).__name__}:{exc}",
        )

    elapsed_ms = (time.monotonic() - started_mono) * 1000.0

    # Defense-in-depth: drop non-allowlist evidence.
    if not isinstance(evidence, tuple):
        try:
            evidence = tuple(evidence) if evidence else ()
        except (TypeError, ValueError):
            evidence = ()

    safe_evidence = _filter_evidence_to_allowlist(evidence)
    fp = canonical_evidence_fingerprint(safe_evidence)
    outcome = _classify_evidence_outcome(safe_evidence)

    return BranchResult(
        branch_id=branch_id,
        outcome=outcome,
        evidence=safe_evidence,
        elapsed_ms=elapsed_ms,
        depth=level,
        fingerprint=fp,
    )


# ---------------------------------------------------------------------------
# Cancellation helper — same pattern as confidence_probe_runner
# ---------------------------------------------------------------------------


async def _cancel_pending(
    tasks: List[asyncio.Task],
) -> None:
    """Cancel all unfinished tasks + await cleanup. NEVER raises."""
    for t in tasks:
        if not t.done():
            t.cancel()
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:  # noqa: BLE001 — defensive
        pass


# ---------------------------------------------------------------------------
# Per-level execution — spawn N branches in parallel with early stop
# ---------------------------------------------------------------------------


async def _run_one_level(
    *,
    prober: BranchProber,
    target: BranchTreeTarget,
    level: int,
    breadth: int,
    prior_evidence: Tuple[BranchEvidence, ...],
    level_budget_seconds: float,
    per_branch_wall_seconds: float,
    diminishing_returns_threshold: float,
) -> List[BranchResult]:
    """Spawn ``breadth`` parallel branches at ``level``. Collect
    results via ``asyncio.as_completed``.

    Diminishing-returns early-stop: if ≥ threshold fraction of
    completed branches share the same fingerprint, cancel pending
    + return current results. Saves cost on the easy case where
    consensus emerges immediately.

    Wall-cap on the level (level_budget_seconds): cancel pending +
    return current results when exceeded.

    NEVER raises. Returns whatever results were collected (may be
    less than ``breadth`` if early-stop fired)."""
    if breadth < 1:
        return []

    started_mono = time.monotonic()
    tasks: List[asyncio.Task] = []
    for position in range(breadth):
        coro = _run_one_branch(
            prober=prober,
            target=target,
            level=level,
            position=position,
            prior_evidence=prior_evidence,
            per_branch_wall_seconds=per_branch_wall_seconds,
        )
        tasks.append(asyncio.create_task(coro))

    results: List[BranchResult] = []
    fingerprint_counts: dict[str, int] = {}

    try:
        for completed in asyncio.as_completed(tasks):
            # Wall-cap check before awaiting next completion.
            elapsed = time.monotonic() - started_mono
            if elapsed >= level_budget_seconds:
                logger.debug(
                    "[sbt_runner] level %d wall cap %.2fs hit "
                    "after %.2fs — cancelling pending",
                    level, level_budget_seconds, elapsed,
                )
                break

            try:
                # Bound await on this single completion to the
                # remaining level budget (defense in depth).
                remaining = max(
                    0.1, level_budget_seconds - elapsed,
                )
                result = await asyncio.wait_for(
                    completed, timeout=remaining,
                )
            except asyncio.TimeoutError:
                # The whole level timed out waiting for this single
                # task — break + cleanup.
                break
            except asyncio.CancelledError:
                # Single task cancelled — record nothing for it
                # (cleanup will pick up the rest).
                continue
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[sbt_runner] level %d as_completed exc: %s",
                    level, exc,
                )
                continue

            if isinstance(result, BranchResult):
                results.append(result)
                if result.fingerprint:
                    fingerprint_counts[result.fingerprint] = (
                        fingerprint_counts.get(result.fingerprint, 0)
                        + 1
                    )

            # Diminishing-returns early-stop. Only meaningful once
            # we have at least 2 results (you can't have a "majority"
            # of 1).
            if len(results) >= 2 and fingerprint_counts:
                top_fp_count = max(fingerprint_counts.values())
                fraction = top_fp_count / len(results)
                if fraction >= diminishing_returns_threshold:
                    logger.debug(
                        "[sbt_runner] level %d diminishing returns "
                        "fired: %d/%d branches share fp (>= %.2f)",
                        level, top_fp_count, len(results),
                        diminishing_returns_threshold,
                    )
                    break
    finally:
        await _cancel_pending(tasks)

    return results


# ---------------------------------------------------------------------------
# Aggregate prior evidence for tie-breaker level
# ---------------------------------------------------------------------------


def _aggregate_prior_evidence(
    branches: List[BranchResult],
    *,
    cap: int = 8,
) -> Tuple[BranchEvidence, ...]:
    """Bundle high-confidence evidence from earlier levels for the
    tie-breaker prober. Bounded by ``cap`` items so the prior
    payload doesn't grow unbounded across deep trees.

    Selection rule: take up to ``cap`` evidence items, preferring
    higher confidence. Stable sort by ``(-confidence, kind, hash)``
    so the same input produces the same prior.

    NEVER raises."""
    try:
        all_evidence: List[BranchEvidence] = []
        for b in branches:
            if not isinstance(b, BranchResult):
                continue
            if b.outcome is not BranchOutcome.SUCCESS:
                continue
            all_evidence.extend(b.evidence)
        if not all_evidence:
            return ()
        # Sort by (-confidence, kind, hash) for stable + high-first.
        ordered = sorted(
            all_evidence,
            key=lambda e: (
                -float(e.confidence),
                str(e.kind.value),
                str(e.content_hash),
            ),
        )
        return tuple(ordered[: max(1, int(cap))])
    except Exception:  # noqa: BLE001 — defensive
        return ()


# ---------------------------------------------------------------------------
# Public surface — run_speculative_tree
# ---------------------------------------------------------------------------


async def run_speculative_tree(
    target: BranchTreeTarget,
    *,
    prober: Optional[BranchProber] = None,
    enabled_override: Optional[bool] = None,
) -> TreeVerdictResult:
    """End-to-end speculative tree runner.

    Resolution order:
      1. ``enabled_override is False`` → DISABLED
      2. ``not sbt_enabled()`` (when override is None) → FAILED
      3. ``not sbt_runner_enabled()`` (when override is None) → FAILED
      4. Garbage target → FAILED
      5. Spawn level 0 (max_breadth branches in parallel)
      6. Compute level-0 verdict via Slice 1's compute_tree_verdict
      7. If DIVERGED AND level < max_depth → spawn one tie-breaker
         level with prior_evidence aggregated from level 0
      8. Re-compute on union; cap at max_depth
      9. Return TreeVerdictResult via Slice 1's compute_tree_outcome

    Wall-cap budget is split across levels: each level gets a
    fraction of the total budget proportional to the depth. Level
    0 gets the largest share; tie-breaker levels get progressively
    less so a deeply-divergent tree still finishes within the
    overall cap.

    NEVER raises out. Wall-cap hit mid-execution → TRUNCATED with
    whatever results were collected."""
    # 1. Flag resolution.
    if enabled_override is False:
        return TreeVerdictResult(
            outcome=TreeVerdict.FAILED,
            target=(
                target if isinstance(target, BranchTreeTarget) else None
            ),
            detail="enabled_override=false",
        )
    if enabled_override is None:
        if not sbt_enabled():
            return TreeVerdictResult(
                outcome=TreeVerdict.FAILED,
                target=(
                    target if isinstance(target, BranchTreeTarget)
                    else None
                ),
                detail="sbt_master_flag_off",
            )
        if not sbt_runner_enabled():
            return TreeVerdictResult(
                outcome=TreeVerdict.FAILED,
                target=(
                    target if isinstance(target, BranchTreeTarget)
                    else None
                ),
                detail="sbt_runner_sub_flag_off",
            )

    # 2. Validate target.
    if not isinstance(target, BranchTreeTarget):
        return TreeVerdictResult(
            outcome=TreeVerdict.FAILED,
            detail=f"target_not_BranchTreeTarget:{type(target).__name__}",
        )

    resolved_prober = prober if prober is not None else _DEFAULT_PROBER

    # 3. Resolve effective caps from target + env.
    max_depth = target.effective_max_depth()
    max_breadth = target.effective_max_breadth()
    total_wall = target.effective_max_wall_seconds()
    dim_threshold = sbt_diminishing_returns_threshold()

    # Per-level budget split: level 0 gets 50%, each tie-breaker
    # level gets a share of the remaining budget. Cumulative levels
    # bounded by max_depth.
    level_budgets = _allocate_level_budgets(total_wall, max_depth)
    per_branch_wall_floor = max(1.0, total_wall / max(1, max_depth * max_breadth))

    # 4. Run the tree.
    started_mono = time.monotonic()
    all_branches: List[BranchResult] = []
    prior_evidence: Tuple[BranchEvidence, ...] = ()
    level = 0

    try:
        while level < max_depth:
            # Wall-cap on the entire tree.
            elapsed = time.monotonic() - started_mono
            if elapsed >= total_wall:
                logger.debug(
                    "[sbt_runner] tree wall cap %.2fs hit at level "
                    "%d after %.2fs",
                    total_wall, level, elapsed,
                )
                break

            level_budget = level_budgets[level] if level < len(level_budgets) else 1.0
            level_branches = await _run_one_level(
                prober=resolved_prober,
                target=target,
                level=level,
                breadth=max_breadth,
                prior_evidence=prior_evidence,
                level_budget_seconds=level_budget,
                per_branch_wall_seconds=per_branch_wall_floor,
                diminishing_returns_threshold=dim_threshold,
            )
            all_branches.extend(level_branches)

            # Compute current verdict over union.
            current_verdict = compute_tree_verdict(all_branches)

            # Spawn tie-breaker only on DIVERGED.
            if current_verdict is not TreeVerdict.DIVERGED:
                break

            # Prepare for tie-breaker — aggregate prior evidence.
            prior_evidence = _aggregate_prior_evidence(all_branches)
            level += 1

        # 5. Compose final result via Slice 1.
        wall_elapsed = time.monotonic() - started_mono

        # If we hit the wall cap mid-execution AND have an unresolved
        # verdict, return TRUNCATED (override Slice 1's verdict).
        if wall_elapsed >= total_wall and all_branches:
            current = compute_tree_verdict(all_branches)
            if current not in (
                TreeVerdict.CONVERGED, TreeVerdict.FAILED,
            ):
                return _truncated_result(target, all_branches, wall_elapsed)

        result = compute_tree_outcome(
            target, all_branches, enabled_override=True,
        )
        # Stamp wall-clock token in detail for observability.
        extended_detail = (
            f"{result.detail or ''} "
            f"wall_elapsed={wall_elapsed:.2f}s "
            f"levels_run={level + 1}"
        ).strip()
        return replace(result, detail=extended_detail)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[sbt_runner] run_speculative_tree raised: %s", exc,
        )
        return TreeVerdictResult(
            outcome=TreeVerdict.FAILED,
            target=target,
            branches=tuple(all_branches),
            detail=f"runner_error:{type(exc).__name__}",
        )


def _allocate_level_budgets(
    total_wall: float, max_depth: int,
) -> List[float]:
    """Split ``total_wall`` across ``max_depth`` levels.

    Level 0 gets the largest share (50%); each subsequent level
    gets half of the remaining budget (so deep trees still fit
    inside the cap). Floor at 1.0s per level so even the deepest
    tie-breaker has a chance.

    Returns a list of ``max_depth`` positive floats summing to
    approximately ``total_wall``. NEVER raises."""
    try:
        if max_depth < 1:
            return []
        if max_depth == 1:
            return [max(1.0, float(total_wall))]
        budgets: List[float] = []
        remaining = float(total_wall)
        for i in range(max_depth):
            if i == max_depth - 1:
                # Last level gets whatever's left.
                budgets.append(max(1.0, remaining))
            else:
                share = remaining * 0.5
                budgets.append(max(1.0, share))
                remaining -= share
        return budgets
    except Exception:  # noqa: BLE001 — defensive
        return [max(1.0, float(total_wall) / max(1, max_depth))] * max_depth


def _truncated_result(
    target: BranchTreeTarget,
    branches: List[BranchResult],
    wall_elapsed: float,
) -> TreeVerdictResult:
    """Compose a TRUNCATED result with whatever branches were
    collected. Used when the tree wall-cap is hit and the verdict
    isn't resolved."""
    return TreeVerdictResult(
        outcome=TreeVerdict.TRUNCATED,
        target=target,
        branches=tuple(branches),
        detail=(
            f"verdict=truncated branches={len(branches)} "
            f"wall_elapsed={wall_elapsed:.2f}s"
        ),
    )


# ---------------------------------------------------------------------------
# Cost-contract authority constant (AST-pin target for Slice 5)
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "BranchProber",
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "READONLY_TOOL_ALLOWLIST",  # re-export for convenience
    "SBT_RUNNER_SCHEMA_VERSION",
    "is_tool_allowlisted",  # re-export
    "run_speculative_tree",
    "sbt_runner_enabled",
]
