"""M10 Producer-Bridge — Slice 1
================================

§32.4 / §40.1 #4 — closes the "0 proposals fired end-to-end"
gap by composing the canonical M10 substrate surfaces into a
single fire-once entry point.

Pre-Slice-1 state: ``UnhandledPatternMiner.mine()`` exists +
``proposal_store.append_proposal()`` exists, but **nothing in
the codebase composes them**. The miner is a pure producer —
it emits ``MineResult.proposals_emitted`` (a tuple of
``M10ProposalRecord``) but never writes them to the ledger.
The store is a pure persistence layer — it accepts
``StoredProposal`` rows but has no upstream producer.

Slice 1 ships the producer-bridge that fuses them:

  miner.mine() → [for each emitted record: append_proposal(record→StoredProposal)]

The bridge is operator-initiated only (no orchestrator wiring
yet — Slice 3 will add automatic cadence). REPL surface in
:mod:`m10.repl` adds a ``/m10 fire`` subcommand that calls
:func:`fire_mining_cycle_sync` so operators can prove the
end-to-end lifecycle works before any automatic firing.

Composition contract (operator-binding 2026-05-11):

* NO parallel state — composes
  :func:`unhandled_pattern_miner.get_default_miner` +
  :func:`proposal_store.append_proposal` +
  :class:`proposal_store.StoredProposal` exclusively.
* NO hardcoded triggers — caller supplies (or omits) ``now_unix``.
* NO bypassed gates — :func:`primitives.m10_arch_proposer_enabled`
  is checked at the bridge boundary, and the miner re-checks it
  internally (defense-in-depth).
* NEVER raises — top-level try/except yields a structured
  failure :class:`MineCycleResult` instead of propagating.

Authority asymmetry (AST-pinned): the bridge composes only the
canonical m10 substrates + stdlib. It MUST NOT import
orchestrator / iron_gate / policy / candidate_generator /
urgency_router / change_engine / semantic_guardian /
auto_committer / risk_tier_floor / tool_executor /
plan_generator / providers — those are decision authorities,
and m10 stays decoupled from them. Lifecycle bridges (Slice 2)
will inject those via Protocol; the bridge itself does NOT
import them.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


M10_PRODUCER_BRIDGE_SCHEMA_VERSION: str = "m10_producer_bridge.1"

# Per-cycle wall-clock ceiling for fire_mining_cycle_sync. The
# miner's authoritative entry point doesn't impose its own
# wall-clock cap; this is the sync-bridge's defense against a
# wedged async path blocking the REPL. Operator-tunable via
# JARVIS_M10_PRODUCER_BRIDGE_TIMEOUT_S (clamped [5, 600]).
_DEFAULT_BRIDGE_TIMEOUT_S: float = 120.0


# ---------------------------------------------------------------------------
# Frozen result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MineCycleResult:
    """Aggregate result of one fire-mining-cycle invocation.

    Frozen + JSON-projectable so the REPL renderer + Slice 5
    observability surface can consume it without invented
    intermediate shapes.

    Composes the canonical :class:`MineResult` (the miner's
    own structured output) + a per-bridge ``rows_stored`` count
    that reflects how many proposals actually made it to the
    ledger. The two counts can diverge — e.g., miner emits 2
    records but the store flock fails for 1 → rows_stored=1.
    """

    ok: bool
    """True iff every emitted miner record was persisted to the
    ledger. False on bridge failure, miner failure, or partial
    persist."""

    outcome: str
    """Mirrors :class:`MineOutcome` value (``emitted`` /
    ``decided_skip`` / ``deduped`` / ``daily_cap_reached`` /
    ``no_patterns`` / ``disabled``) or ``"error"`` on
    bridge-level failure (NEVER-raises contract)."""

    proposals_emitted_count: int = 0
    """Count from :attr:`MineResult.proposals_emitted` — what the
    miner produced before the bridge attempted persistence."""

    rows_stored: int = 0
    """Count of rows actually appended to the proposal ledger.
    May be less than ``proposals_emitted_count`` if append_proposal
    returned False for any row."""

    proposal_ids: Tuple[str, ...] = field(default_factory=tuple)
    """Stable IDs of proposals that successfully reached the
    ledger. Operator-readable in REPL output + correlatable
    with :func:`proposal_store.find_proposal_by_id`."""

    elapsed_s: float = 0.0

    diagnostic: str = ""
    """Operator-readable summary. Empty on clean success;
    populated with cause on partial / error outcomes."""

    schema_version: str = field(
        default=M10_PRODUCER_BRIDGE_SCHEMA_VERSION,
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "ok": bool(self.ok),
            "outcome": str(self.outcome),
            "proposals_emitted_count": int(
                self.proposals_emitted_count,
            ),
            "rows_stored": int(self.rows_stored),
            "proposal_ids": list(self.proposal_ids),
            "elapsed_s": float(self.elapsed_s),
            "diagnostic": str(self.diagnostic)[:512],
        }


# ---------------------------------------------------------------------------
# Env-knob accessor — bridge timeout (sync wrapper only)
# ---------------------------------------------------------------------------


def _bridge_timeout_s() -> float:
    """JARVIS_M10_PRODUCER_BRIDGE_TIMEOUT_S — sync-bridge
    wall-clock ceiling. Clamped [5, 600]. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_M10_PRODUCER_BRIDGE_TIMEOUT_S", "",
    ).strip()
    if not raw:
        return _DEFAULT_BRIDGE_TIMEOUT_S
    try:
        v = float(raw)
        if v < 5.0:
            return 5.0
        if v > 600.0:
            return 600.0
        return v
    except (TypeError, ValueError):
        return _DEFAULT_BRIDGE_TIMEOUT_S


# ---------------------------------------------------------------------------
# Conversion helper — M10ProposalRecord → StoredProposal
# ---------------------------------------------------------------------------


def _record_to_stored(record: Any) -> Optional[Any]:
    """Project a miner-emitted ``M10ProposalRecord`` into a
    persistence-layer ``StoredProposal``. Duck-typed reads so a
    foreign object doesn't crash the bridge.

    Returns the StoredProposal on success, None on projection
    failure. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (  # noqa: E501
            StoredProposal,
        )
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        # Extract every field StoredProposal needs via attribute
        # access. Missing fields fall through to StoredProposal's
        # dataclass defaults (empty string / 0.0 / empty tuple).
        proposal_id = str(
            getattr(record, "proposal_id", "") or "",
        )[:128]
        if not proposal_id:
            return None  # required field — refuse silent ID drift
        kind_raw = getattr(record, "kind", "") or ""
        kind = (
            kind_raw.value if hasattr(kind_raw, "value")
            else str(kind_raw)
        )[:32]
        phase_raw = getattr(record, "phase", "") or ""
        phase = (
            phase_raw.value if hasattr(phase_raw, "value")
            else str(phase_raw)
        )[:32]
        if not kind or not phase:
            return None  # required fields
        evidence_raw = (
            getattr(record, "detection_evidence", ()) or ()
        )
        evidence = tuple(
            str(e)[:256] for e in evidence_raw
            if isinstance(e, (str, int, float))
        )[:32]
        return StoredProposal(
            proposal_id=proposal_id,
            kind=kind,
            phase=phase,
            pattern_signature=str(
                getattr(record, "pattern_signature", "") or "",
            )[:128],
            detection_evidence=evidence,
            proposed_module_path=str(
                getattr(record, "proposed_module_path", "") or "",
            )[:256],
            proposed_class_name=str(
                getattr(record, "proposed_class_name", "") or "",
            )[:128],
            proposed_ast_pin_name=str(
                getattr(record, "proposed_ast_pin_name", "") or "",
            )[:128],
            pr_url=str(getattr(record, "pr_url", "") or "")[:512],
            pr_branch=str(
                getattr(record, "pr_branch", "") or "",
            )[:128],
            failure_reason=str(
                getattr(record, "failure_reason", "") or "",
            )[:256],
            cost_usd=float(
                getattr(record, "cost_usd", 0.0) or 0.0,
            ),
            consensus_signature=str(
                getattr(record, "consensus_signature", "") or "",
            )[:128],
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[m10_producer_bridge] _record_to_stored failed: %r",
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Canonical async entry point
# ---------------------------------------------------------------------------


async def fire_mining_cycle(
    *,
    now_unix: Optional[float] = None,
    miner: Optional[Any] = None,
) -> MineCycleResult:
    """Run ONE end-to-end fire cycle: mine → persist.

    The authoritative async entry point. Composes the canonical
    :func:`get_default_miner` (or operator-injected ``miner``
    for hermetic testing) + :func:`append_proposal`. NEVER raises
    — every failure surfaces as a structured
    :class:`MineCycleResult` with ``ok=False`` + diagnostic.

    Parameters
    ----------
    now_unix:
        Time override forwarded to ``miner.mine()`` for
        deterministic tests. None → miner uses ``time.time()``.
    miner:
        Operator-injectable miner instance. None →
        :func:`get_default_miner` singleton.

    Returns
    -------
    :class:`MineCycleResult`
        Aggregate outcome — ``ok=True`` iff every emitted record
        persisted cleanly. ``rows_stored`` may be less than
        ``proposals_emitted_count`` on partial persistence.
    """
    started_mono = time.monotonic()

    # Master-flag gate — defense-in-depth (the miner also checks).
    try:
        from backend.core.ouroboros.governance.m10.primitives import (  # noqa: E501
            m10_arch_proposer_enabled,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return MineCycleResult(
            ok=False,
            outcome="error",
            diagnostic=(
                f"primitives import failed: "
                f"{type(exc).__name__}"
            ),
            elapsed_s=max(0.0, time.monotonic() - started_mono),
        )
    try:
        if not m10_arch_proposer_enabled():
            return MineCycleResult(
                ok=True,  # gated-off is success, not error
                outcome="disabled",
                diagnostic=(
                    "JARVIS_M10_ARCH_PROPOSER_ENABLED=false — "
                    "bridge returned without firing miner"
                ),
                elapsed_s=max(
                    0.0, time.monotonic() - started_mono,
                ),
            )
    except Exception as exc:  # noqa: BLE001 — defensive
        return MineCycleResult(
            ok=False,
            outcome="error",
            diagnostic=(
                f"master-flag gate raised: "
                f"{type(exc).__name__}"
            ),
            elapsed_s=max(0.0, time.monotonic() - started_mono),
        )

    # Resolve miner singleton.
    if miner is None:
        try:
            from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
                get_default_miner,
            )
            miner = get_default_miner()
        except Exception as exc:  # noqa: BLE001 — defensive
            return MineCycleResult(
                ok=False,
                outcome="error",
                diagnostic=(
                    f"get_default_miner failed: "
                    f"{type(exc).__name__}"
                ),
                elapsed_s=max(
                    0.0, time.monotonic() - started_mono,
                ),
            )

    # Fire the miner.
    try:
        mine_result = await miner.mine(now_unix=now_unix)
    except Exception as exc:  # noqa: BLE001 — defensive
        return MineCycleResult(
            ok=False,
            outcome="error",
            diagnostic=f"miner.mine raised: {type(exc).__name__}",
            elapsed_s=max(0.0, time.monotonic() - started_mono),
        )

    # Extract outcome string (handles enum or string).
    outcome_attr = getattr(mine_result, "outcome", "")
    outcome_str = (
        outcome_attr.value if hasattr(outcome_attr, "value")
        else str(outcome_attr or "")
    )

    emitted = tuple(
        getattr(mine_result, "proposals_emitted", ()) or (),
    )
    emitted_count = len(emitted)

    # No emissions → nothing to persist. Bridge returns ok with
    # the miner's outcome value verbatim.
    if not emitted:
        return MineCycleResult(
            ok=True,
            outcome=outcome_str or "no_patterns",
            proposals_emitted_count=0,
            rows_stored=0,
            elapsed_s=max(0.0, time.monotonic() - started_mono),
            diagnostic=(
                "miner emitted no records "
                f"(outcome={outcome_str!r})"
            ),
        )

    # Persist each emitted record. Partial persistence is
    # tolerated but flagged via ok=False.
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (  # noqa: E501
            append_proposal,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return MineCycleResult(
            ok=False,
            outcome="error",
            proposals_emitted_count=emitted_count,
            rows_stored=0,
            diagnostic=(
                f"proposal_store import failed: "
                f"{type(exc).__name__}"
            ),
            elapsed_s=max(0.0, time.monotonic() - started_mono),
        )

    stored_ids: list = []
    failed: list = []
    for record in emitted:
        stored = _record_to_stored(record)
        if stored is None:
            pid_fallback = str(
                getattr(record, "proposal_id", "") or "<unknown>",
            )
            failed.append(pid_fallback)
            continue
        try:
            persisted = bool(append_proposal(stored))
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[m10_producer_bridge] append_proposal raised "
                "for %s: %r", stored.proposal_id, exc,
            )
            failed.append(stored.proposal_id)
            continue
        if persisted:
            stored_ids.append(stored.proposal_id)
        else:
            failed.append(stored.proposal_id)

    rows_stored = len(stored_ids)
    all_ok = (rows_stored == emitted_count)
    diagnostic = (
        f"miner outcome={outcome_str!r}, "
        f"emitted={emitted_count}, stored={rows_stored}"
    )
    if failed:
        diagnostic += f"; failed_ids={failed[:5]!r}"

    return MineCycleResult(
        ok=all_ok,
        outcome=outcome_str or "emitted",
        proposals_emitted_count=emitted_count,
        rows_stored=rows_stored,
        proposal_ids=tuple(stored_ids),
        elapsed_s=max(0.0, time.monotonic() - started_mono),
        diagnostic=diagnostic,
    )


# ---------------------------------------------------------------------------
# Sync bridge — REPL-friendly wrapper
# ---------------------------------------------------------------------------


def fire_mining_cycle_sync(
    *,
    now_unix: Optional[float] = None,
    miner: Optional[Any] = None,
    timeout_s: Optional[float] = None,
) -> MineCycleResult:
    """Synchronous entry point for the REPL.

    The :mod:`repl_dispatch_registry` signature validator
    requires ``dispatch_<verb>_command`` to be sync (single
    positional ``line: str``). Since :func:`fire_mining_cycle`
    is async (composes the async miner), the REPL needs a sync
    bridge.

    Detects a running event loop and routes accordingly:

    * **No running loop** → ``asyncio.run(fire_mining_cycle(...))``
      executes the coroutine directly.
    * **Running loop** → schedule on a worker thread via
      :class:`concurrent.futures.ThreadPoolExecutor` so the
      caller's loop isn't disturbed.

    NEVER raises — wraps everything in try/except returning a
    structured :class:`MineCycleResult` with ``ok=False`` on any
    failure (loop-detection error, timeout, executor crash).
    """
    deadline = (
        float(timeout_s) if timeout_s is not None
        else _bridge_timeout_s()
    )
    try:
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
    except Exception as exc:  # noqa: BLE001 — defensive
        return MineCycleResult(
            ok=False,
            outcome="error",
            diagnostic=(
                f"loop detection raised: {type(exc).__name__}"
            ),
        )

    coro_factory = lambda: fire_mining_cycle(  # noqa: E731
        now_unix=now_unix, miner=miner,
    )

    try:
        if running:
            # Running loop — bridge via worker thread.
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="m10-bridge",
            ) as ex:
                future = ex.submit(
                    asyncio.run, coro_factory(),
                )
                return future.result(timeout=deadline)
        # No loop — run directly.
        return asyncio.run(coro_factory())
    except concurrent.futures.TimeoutError:
        return MineCycleResult(
            ok=False,
            outcome="error",
            diagnostic=(
                f"fire_mining_cycle_sync timed out after "
                f"{deadline:.0f}s"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return MineCycleResult(
            ok=False,
            outcome="error",
            diagnostic=(
                f"fire_mining_cycle_sync raised: "
                f"{type(exc).__name__}: {exc!r}"[:256]
            ),
        )


# ===========================================================================
# §33.1 — register_shipped_invariants self-registration
# ===========================================================================


def register_shipped_invariants() -> list:
    """M10 producer-bridge substrate invariants. Pins:

      * ``MineCycleResult`` frozen dataclass present at module level.
      * Canonical composition: imports ``get_default_miner`` +
        ``append_proposal`` + ``StoredProposal`` (no parallel state).
      * Authority asymmetry: NEVER imports orchestrator /
        iron_gate / policy / candidate_generator / urgency_router /
        change_engine / semantic_guardian / auto_committer /
        risk_tier_floor / tool_executor / plan_generator / providers.
      * Master-flag gate: composes ``m10_arch_proposer_enabled``
        at the bridge boundary.
      * Async + sync entry points: ``fire_mining_cycle`` (async)
        + ``fire_mining_cycle_sync`` (sync REPL bridge).
    """
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/m10/"
        "m10_producer_bridge.py"
    )

    _FORBIDDEN_IMPORT_MODULES = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.policy_engine",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.urgency_router",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.semantic_guardian",
        "backend.core.ouroboros.governance.auto_committer",
        "backend.core.ouroboros.governance.risk_tier_floor",
        "backend.core.ouroboros.governance.tool_executor",
        "backend.core.ouroboros.governance.plan_generator",
        "backend.core.ouroboros.governance.providers",
    )

    def _validate_taxonomy(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.ClassDef)
                and node.name == "MineCycleResult"
            ):
                return ()
        return (
            "MineCycleResult frozen dataclass missing — "
            "Slice 1 bridge contract requires it as the "
            "structured return type",
        )

    def _validate_canonical_composition(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        # Source-string match: each symbol MUST appear so the
        # composition is visible to refactor-time greppers + AST
        # walkers. Using a substring check here because the
        # composition happens via lazy imports inside functions.
        violations: list = []
        if "get_default_miner" not in source:
            violations.append(
                "must compose get_default_miner "
                "(canonical miner singleton — no parallel "
                "miner instance)"
            )
        if "append_proposal" not in source:
            violations.append(
                "must compose append_proposal "
                "(canonical persistence — no parallel ledger)"
            )
        if "StoredProposal" not in source:
            violations.append(
                "must compose StoredProposal "
                "(canonical row shape — no invented projection)"
            )
        if "m10_arch_proposer_enabled" not in source:
            violations.append(
                "must compose m10_arch_proposer_enabled "
                "(master-flag gate — substrate must respect §33.1)"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod in _FORBIDDEN_IMPORT_MODULES:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden import {mod!r} — m10 "
                        f"producer-bridge MUST stay decoupled "
                        f"from decision authorities"
                    )
        return tuple(violations)

    def _validate_dual_entry_points(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        saw_async = False
        saw_sync = False
        for node in _ast.walk(tree):
            if isinstance(node, _ast.AsyncFunctionDef):
                if node.name == "fire_mining_cycle":
                    saw_async = True
            elif isinstance(node, _ast.FunctionDef):
                if node.name == "fire_mining_cycle_sync":
                    saw_sync = True
        violations: list = []
        if not saw_async:
            violations.append(
                "async fire_mining_cycle entry point missing "
                "(canonical async composition seam)"
            )
        if not saw_sync:
            violations.append(
                "sync fire_mining_cycle_sync wrapper missing "
                "(REPL-friendly sync bridge — registry dispatcher "
                "contract requires sync)"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "m10_producer_bridge_result_taxonomy"
            ),
            target_file=target,
            description=(
                "MineCycleResult frozen dataclass is the "
                "structured return type for both bridge entry "
                "points. Drift here breaks the REPL renderer + "
                "Slice 5 observability projection."
            ),
            validate=_validate_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "m10_producer_bridge_composes_canonical"
            ),
            target_file=target,
            description=(
                "Composes canonical M10 substrates: "
                "get_default_miner + append_proposal + "
                "StoredProposal + m10_arch_proposer_enabled. "
                "Operator binding 2026-05-11: no parallel state, "
                "no duplicate ledger, leverage existing files."
            ),
            validate=_validate_canonical_composition,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "m10_producer_bridge_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Bridge MUST NOT import orchestrator / "
                "iron_gate / policy / candidate_generator / "
                "etc. M10 stays decoupled from decision "
                "authorities; Slice 2 lifecycle bridges will "
                "inject those via Protocol."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "m10_producer_bridge_dual_entry_points"
            ),
            target_file=target,
            description=(
                "Bridge ships BOTH async fire_mining_cycle "
                "(canonical) AND sync fire_mining_cycle_sync "
                "(REPL bridge). Registry dispatcher signature "
                "validator requires sync; canonical "
                "composition with async miner.mine() requires "
                "async."
            ),
            validate=_validate_dual_entry_points,
        ),
    ]


# ===========================================================================
# Slice 2 — full-lifecycle composer (mine → synthesize → advance)
# ===========================================================================
#
# Slice 1 stops at DETECTING-phase persistence. Slice 2 extends the
# bridge to compose ProposalSynthesizer.synthesize() + Proposal-
# LifecycleOrchestrator.advance() via the 5 adapters in
# m10.bridge_adapters. Gated by JARVIS_M10_BRIDGE_FULL_LIFECYCLE_ENABLED
# (default-FALSE — Slice 1 byte-identical behavior preserved when off).


def full_lifecycle_enabled() -> bool:
    """``JARVIS_M10_BRIDGE_FULL_LIFECYCLE_ENABLED`` — Slice 2 gate.
    Default-FALSE. When on, ``fire_full_lifecycle_cycle`` composes
    synthesizer + lifecycle for each mined record. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_M10_BRIDGE_FULL_LIFECYCLE_ENABLED", "",
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ProposalLifecyclePersistResult:
    """Per-proposal outcome of one mine → advance cycle. Frozen."""

    proposal_id: str
    initial_phase: str = "detecting"
    synth_verdict: str = ""
    final_phase: str = ""
    pr_url: str = ""
    pr_branch: str = ""
    failure_reason: str = ""
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    schema_version: str = field(
        default=M10_PRODUCER_BRIDGE_SCHEMA_VERSION,
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "initial_phase": self.initial_phase,
            "synth_verdict": self.synth_verdict,
            "final_phase": self.final_phase,
            "pr_url": self.pr_url,
            "pr_branch": self.pr_branch,
            "failure_reason": (self.failure_reason or "")[:512],
            "cost_usd": float(self.cost_usd),
            "elapsed_s": float(self.elapsed_s),
        }


@dataclass(frozen=True)
class FullLifecycleCycleResult:
    """Aggregate result of one fire-full-lifecycle invocation.

    Wraps the :class:`MineCycleResult` from the underlying mining
    step + adds per-proposal advance outcomes. Frozen."""

    mining_result: Optional[MineCycleResult]
    """The mining step's result. May be None on early-exit
    (master flag off / sub-flag off)."""

    advanced_proposals: Tuple[
        ProposalLifecyclePersistResult, ...
    ] = field(default_factory=tuple)
    """Per-proposal advance outcomes — one entry per mined
    record that the bridge attempted to advance."""

    ok: bool = True
    outcome: str = "no_op"
    """``no_op`` when master-flag off / sub-flag off; ``mined``
    when mining produced no records to advance; ``advanced``
    when at least one proposal completed an advance attempt
    (regardless of terminal phase); ``error`` on bridge-level
    failure."""

    elapsed_s: float = 0.0
    diagnostic: str = ""
    schema_version: str = field(
        default=M10_PRODUCER_BRIDGE_SCHEMA_VERSION,
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "ok": bool(self.ok),
            "outcome": str(self.outcome),
            "elapsed_s": float(self.elapsed_s),
            "diagnostic": str(self.diagnostic)[:512],
            "mining_result": (
                self.mining_result.to_dict()
                if self.mining_result is not None else None
            ),
            "advanced_proposals": [
                p.to_dict() for p in self.advanced_proposals
            ],
        }


async def _advance_proposal(
    record: Any,
    *,
    synthesizer: Optional[Any] = None,
    lifecycle: Optional[Any] = None,
    provider: Optional[Any] = None,
    layers: Optional[Any] = None,
    worktree_bridge: Optional[Any] = None,
    commit_bridge: Optional[Any] = None,
    pr_bridge: Optional[Any] = None,
) -> ProposalLifecyclePersistResult:
    """Synthesize + advance ONE mined record.

    Composes:
      1. ProposalSynthesizer.synthesize(record, provider)
      2. ProposalLifecycleOrchestrator.advance(synthesized, ...)

    Updates the proposal_store ledger with the final terminal
    phase + PR URL. NEVER raises."""
    started = time.monotonic()
    proposal_id = str(
        getattr(record, "proposal_id", "") or "",
    )
    # Resolve singletons / adapters lazily.
    if synthesizer is None:
        try:
            from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
                get_default_synthesizer,
            )
            synthesizer = get_default_synthesizer()
        except Exception as err:  # noqa: BLE001
            return ProposalLifecyclePersistResult(
                proposal_id=proposal_id,
                failure_reason=(
                    f"synthesizer import failed: "
                    f"{type(err).__name__}"
                ),
                elapsed_s=time.monotonic() - started,
            )
    if lifecycle is None:
        try:
            from backend.core.ouroboros.governance.m10.lifecycle import (  # noqa: E501
                get_default_lifecycle,
            )
            lifecycle = get_default_lifecycle()
        except Exception as err:  # noqa: BLE001
            return ProposalLifecyclePersistResult(
                proposal_id=proposal_id,
                failure_reason=(
                    f"lifecycle import failed: "
                    f"{type(err).__name__}"
                ),
                elapsed_s=time.monotonic() - started,
            )
    if any(
        a is None for a in (
            provider, layers, worktree_bridge,
            commit_bridge, pr_bridge,
        )
    ):
        try:
            from backend.core.ouroboros.governance.m10.bridge_adapters import (  # noqa: E501
                CommitBridgeAdapter,
                OrangePRBridgeAdapter,
                SynthesisProviderAdapter,
                ValidationLayersAdapter,
                WorktreeBridgeAdapter,
            )
        except Exception as err:  # noqa: BLE001
            return ProposalLifecyclePersistResult(
                proposal_id=proposal_id,
                failure_reason=(
                    f"bridge_adapters import failed: "
                    f"{type(err).__name__}"
                ),
                elapsed_s=time.monotonic() - started,
            )
        if provider is None:
            provider = SynthesisProviderAdapter()
        if layers is None:
            layers = ValidationLayersAdapter()
        if worktree_bridge is None:
            worktree_bridge = WorktreeBridgeAdapter()
        if commit_bridge is None:
            commit_bridge = CommitBridgeAdapter()
        if pr_bridge is None:
            pr_bridge = OrangePRBridgeAdapter()

    # Step 1: synthesize.
    try:
        synth = await synthesizer.synthesize(
            record, provider=provider,
        )
    except Exception as err:  # noqa: BLE001
        return ProposalLifecyclePersistResult(
            proposal_id=proposal_id,
            failure_reason=(
                f"synthesize raised: {type(err).__name__}: {err}"
            )[:256],
            elapsed_s=time.monotonic() - started,
        )

    synth_verdict_attr = getattr(synth, "verdict", "")
    synth_verdict_str = (
        synth_verdict_attr.value
        if hasattr(synth_verdict_attr, "value")
        else str(synth_verdict_attr or "")
    )
    synth_cost = float(getattr(synth, "cost_usd", 0.0) or 0.0)

    # Only "synthesized" verdict advances; everything else is
    # terminal at synthesis.
    if synth_verdict_str != "synthesized":
        # Mark FAILED in store (matches lifecycle's contract for
        # non-synthesized verdicts when advance is skipped).
        try:
            from backend.core.ouroboros.governance.m10.proposal_store import (  # noqa: E501
                append_proposal,
                StoredProposal,
            )
            update_row = StoredProposal(
                proposal_id=proposal_id,
                kind=str(
                    getattr(getattr(record, "kind", ""), "value", "")
                    or "",
                ),
                phase="decided_skip",
                failure_reason=(
                    f"synth verdict={synth_verdict_str!r}"
                )[:256],
                cost_usd=synth_cost,
            )
            append_proposal(update_row)
        except Exception:  # noqa: BLE001 — defensive
            pass
        return ProposalLifecyclePersistResult(
            proposal_id=proposal_id,
            synth_verdict=synth_verdict_str,
            final_phase="decided_skip",
            cost_usd=synth_cost,
            elapsed_s=time.monotonic() - started,
        )

    # Step 2: advance through lifecycle.
    try:
        lifecycle_result = await lifecycle.advance(
            synth,
            layers=layers,
            worktree_bridge=worktree_bridge,
            commit_bridge=commit_bridge,
            pr_bridge=pr_bridge,
        )
    except Exception as err:  # noqa: BLE001
        return ProposalLifecyclePersistResult(
            proposal_id=proposal_id,
            synth_verdict=synth_verdict_str,
            failure_reason=(
                f"advance raised: {type(err).__name__}: {err}"
            )[:256],
            cost_usd=synth_cost,
            elapsed_s=time.monotonic() - started,
        )

    final_phase_attr = getattr(lifecycle_result, "final_phase", "")
    final_phase_str = (
        final_phase_attr.value
        if hasattr(final_phase_attr, "value")
        else str(final_phase_attr or "")
    )
    pr_result = getattr(lifecycle_result, "pr_result", None)
    pr_url = ""
    pr_branch = ""
    if pr_result is not None:
        pr_url = str(getattr(pr_result, "pr_url", "") or "")
        pr_branch = str(
            getattr(pr_result, "branch_name", "") or "",
        )

    # Persist the terminal-phase row.
    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (  # noqa: E501
            append_proposal,
            StoredProposal,
        )
        update_row = StoredProposal(
            proposal_id=proposal_id,
            kind=str(
                getattr(getattr(record, "kind", ""), "value", "")
                or "",
            ),
            phase=final_phase_str or "failed",
            proposed_module_path=str(
                getattr(synth, "module_path", "") or "",
            ),
            proposed_class_name=str(
                getattr(synth, "class_name", "") or "",
            ),
            proposed_ast_pin_name=str(
                getattr(synth, "ast_pin_name", "") or "",
            ),
            pr_url=pr_url,
            pr_branch=pr_branch,
            failure_reason=str(
                getattr(lifecycle_result, "failure_reason", "")
                or "",
            )[:256],
            cost_usd=synth_cost,
            consensus_signature=str(
                getattr(synth, "consensus_signature", "") or "",
            ),
        )
        append_proposal(update_row)
    except Exception:  # noqa: BLE001 — defensive
        pass

    return ProposalLifecyclePersistResult(
        proposal_id=proposal_id,
        synth_verdict=synth_verdict_str,
        final_phase=final_phase_str,
        pr_url=pr_url,
        pr_branch=pr_branch,
        failure_reason=str(
            getattr(lifecycle_result, "failure_reason", "") or "",
        )[:256],
        cost_usd=synth_cost,
        elapsed_s=time.monotonic() - started,
    )


async def fire_full_lifecycle_cycle(
    *,
    now_unix: Optional[float] = None,
    miner: Optional[Any] = None,
    synthesizer: Optional[Any] = None,
    lifecycle: Optional[Any] = None,
    provider: Optional[Any] = None,
    layers: Optional[Any] = None,
    worktree_bridge: Optional[Any] = None,
    commit_bridge: Optional[Any] = None,
    pr_bridge: Optional[Any] = None,
) -> FullLifecycleCycleResult:
    """Slice 2 — mine → synthesize → advance ALL emitted records.

    Composes ``fire_mining_cycle`` (Slice 1) + ``_advance_proposal``
    for each emitted record. Gated by
    ``JARVIS_M10_BRIDGE_FULL_LIFECYCLE_ENABLED``. NEVER raises.
    """
    started = time.monotonic()
    if not full_lifecycle_enabled():
        return FullLifecycleCycleResult(
            mining_result=None,
            ok=True,
            outcome="no_op",
            diagnostic=(
                "JARVIS_M10_BRIDGE_FULL_LIFECYCLE_ENABLED=false — "
                "Slice 2 sub-flag off; use fire_mining_cycle for "
                "Slice 1 behavior"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    mine_res = await fire_mining_cycle(
        now_unix=now_unix, miner=miner,
    )
    # Re-resolve miner singleton to walk its in-flight records.
    # The mining_result's proposal_ids only carries IDs after
    # successful append_proposal — we want the ORIGINAL miner-
    # emitted records (M10ProposalRecord objects) to feed the
    # synthesizer.
    if miner is None:
        try:
            from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
                get_default_miner,
            )
            miner = get_default_miner()
        except Exception:  # noqa: BLE001
            return FullLifecycleCycleResult(
                mining_result=mine_res,
                ok=False,
                outcome="error",
                diagnostic="miner re-resolve failed",
                elapsed_s=max(0.0, time.monotonic() - started),
            )

    # Re-mine is wasteful; instead the mining result carries
    # proposal_ids and we re-construct records by reading from
    # the store. But the store row is a StoredProposal, not the
    # original M10ProposalRecord with detection_evidence. For
    # Slice 2 we re-fire the miner to get fresh records — the
    # miner's idempotency guard (dedup window) protects against
    # double-emission of the same pattern signature.
    try:
        fresh = await miner.mine(now_unix=now_unix)
        emitted_records = tuple(
            getattr(fresh, "proposals_emitted", ()) or (),
        )
    except Exception as err:  # noqa: BLE001
        emitted_records = ()
        logger.debug(
            "[m10_producer_bridge] re-mine for advance "
            "raised: %r", err,
        )

    if not emitted_records:
        return FullLifecycleCycleResult(
            mining_result=mine_res,
            advanced_proposals=(),
            ok=mine_res.ok,
            outcome="mined" if mine_res.proposals_emitted_count > 0 else "no_op",
            diagnostic=(
                f"mining_outcome={mine_res.outcome!r}, "
                f"no records to advance"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    # Advance each emitted record.
    advance_results: list = []
    for record in emitted_records:
        result = await _advance_proposal(
            record,
            synthesizer=synthesizer,
            lifecycle=lifecycle,
            provider=provider,
            layers=layers,
            worktree_bridge=worktree_bridge,
            commit_bridge=commit_bridge,
            pr_bridge=pr_bridge,
        )
        advance_results.append(result)

    all_ok = all(
        r.synth_verdict in ("synthesized", "")
        and "raised" not in r.failure_reason
        for r in advance_results
    )

    return FullLifecycleCycleResult(
        mining_result=mine_res,
        advanced_proposals=tuple(advance_results),
        ok=all_ok,
        outcome="advanced",
        diagnostic=(
            f"advanced {len(advance_results)} proposal(s); "
            f"phases={tuple(r.final_phase for r in advance_results)}"
        )[:512],
        elapsed_s=max(0.0, time.monotonic() - started),
    )


def fire_full_lifecycle_cycle_sync(
    *,
    now_unix: Optional[float] = None,
    timeout_s: Optional[float] = None,
) -> FullLifecycleCycleResult:
    """Sync wrapper for REPL. Same loop-detection bridge as
    :func:`fire_mining_cycle_sync`. NEVER raises."""
    deadline = (
        float(timeout_s) if timeout_s is not None
        else _bridge_timeout_s()
    )
    try:
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
    except Exception as err:  # noqa: BLE001
        return FullLifecycleCycleResult(
            mining_result=None,
            ok=False,
            outcome="error",
            diagnostic=(
                f"loop detection raised: {type(err).__name__}"
            ),
        )

    coro_factory = lambda: fire_full_lifecycle_cycle(  # noqa: E731
        now_unix=now_unix,
    )

    try:
        if running:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="m10-full-bridge",
            ) as ex:
                future = ex.submit(
                    asyncio.run, coro_factory(),
                )
                return future.result(timeout=deadline)
        return asyncio.run(coro_factory())
    except concurrent.futures.TimeoutError:
        return FullLifecycleCycleResult(
            mining_result=None,
            ok=False,
            outcome="error",
            diagnostic=(
                f"fire_full_lifecycle_cycle_sync timed out "
                f"after {deadline:.0f}s"
            ),
        )
    except Exception as err:  # noqa: BLE001
        return FullLifecycleCycleResult(
            mining_result=None,
            ok=False,
            outcome="error",
            diagnostic=(
                f"fire_full_lifecycle_cycle_sync raised: "
                f"{type(err).__name__}: {err}"
            )[:256],
        )


__all__ = [
    "M10_PRODUCER_BRIDGE_SCHEMA_VERSION",
    "FullLifecycleCycleResult",
    "MineCycleResult",
    "ProposalLifecyclePersistResult",
    "fire_full_lifecycle_cycle",
    "fire_full_lifecycle_cycle_sync",
    "fire_mining_cycle",
    "fire_mining_cycle_sync",
    "full_lifecycle_enabled",
    "register_shipped_invariants",
]
