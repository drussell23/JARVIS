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
    import os
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


__all__ = [
    "M10_PRODUCER_BRIDGE_SCHEMA_VERSION",
    "MineCycleResult",
    "fire_mining_cycle",
    "fire_mining_cycle_sync",
    "register_shipped_invariants",
]
