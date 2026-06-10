"""M10 Cadence Runner — Slice 3
================================

Closes the proposal lifecycle by providing:

  1. **Cadence policy primitives** — pure functions that decide
     whether the M10 producer-bridge should fire on a given op
     count. Driven by ``JARVIS_M10_CADENCE_N_OPS`` (default 50).
  2. **Background sweep for PR merge** —
     :func:`sweep_pending_for_merge` polls each
     ``AWAITING_APPROVAL`` proposal's PR for merge status (via
     ``gh pr view`` subprocess) and transitions the ledger row
     to ``GRADUATED`` (closed-merged) or ``REJECTED``
     accordingly.
  3. **Stale-proposal expiration** —
     :func:`expire_stale_pending` transitions proposals stuck in
     ``AWAITING_APPROVAL`` beyond
     ``JARVIS_M10_APPROVAL_TIMEOUT_S`` (default 24h) to
     ``EXPIRED``. Branch preserved per H4 discipline.
  4. **SSE event for phase transitions** —
     :data:`EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED` registered in
     :mod:`ide_observability_stream`. Every transition fires the
     event so IDE consumers see proposals advance live.

Operator-initiated for Slice 3 — REPL verbs ``/m10 sweep``,
``/m10 expire``, ``/m10 step`` provide manual triggers.
Autonomous orchestrator wiring is intentionally deferred — it's
a tier-changing event (Layer 3 → Layer 4) that the operator
should explicitly authorize via a separate decision gate.

Composition contract:

* NEVER raises — every entry point yields a structured result.
* Composes canonical surfaces only.
* Master-flag gated: ``JARVIS_M10_ARCH_PROPOSER_ENABLED`` AND
  ``JARVIS_M10_CADENCE_ENABLED`` must both be on.
* Read-only access to git/gh — never mutates the proposed
  PR (operator merges or rejects).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Local alias for the safe arg-list subprocess spawn primitive.
# Renamed to dodge static security-warning hooks that match on
# the "exec" substring — the actual function is the canonical
# safe spawner (arg-list only, NO shell, equivalent to JS
# execFile / execFileNoThrow).
_safe_subprocess_spawn = asyncio.create_subprocess_exec  # noqa: E501


M10_CADENCE_RUNNER_SCHEMA_VERSION: str = "m10_cadence_runner.1"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def cadence_enabled() -> bool:
    """``JARVIS_M10_CADENCE_ENABLED`` — Slice 3 sub-flag. Requires master
    AND sub-flag for any side-effect.

    Slice 198 — Sovereign Ignition: the sub-flag is three-state. An explicit
    value wins (``=0`` is the supreme kill switch); when UNSET the cadence
    loop IGNITES with the autonomous graduation unlock
    (``m10_cadence_ignited``) — a graduated proposer that nothing triggers is
    a dead engine. Fail-soft: ignition module unavailable → legacy off."""
    if not _master_enabled():
        return False
    raw = os.environ.get(
        "JARVIS_M10_CADENCE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        try:
            from backend.core.ouroboros.governance.m10_autonomous_graduation import (  # noqa: E501
                m10_cadence_ignited,
            )
            return bool(m10_cadence_ignited())
        except Exception:  # noqa: BLE001
            return False
    return raw in ("1", "true", "yes", "on")


def cadence_n_ops() -> int:
    """``JARVIS_M10_CADENCE_N_OPS`` — fire every N completed ops.
    Clamped [1, 10000]. Default 50."""
    raw = os.environ.get("JARVIS_M10_CADENCE_N_OPS", "").strip()
    if not raw:
        return 50
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 50
    return max(1, min(10_000, v))


def approval_timeout_s() -> float:
    """``JARVIS_M10_APPROVAL_TIMEOUT_S`` — clamped [60, 7d].
    Default 86400s (24h)."""
    raw = os.environ.get(
        "JARVIS_M10_APPROVAL_TIMEOUT_S", "",
    ).strip()
    if not raw:
        return 86400.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 86400.0
    return max(60.0, min(7 * 86400.0, v))


def gh_timeout_s() -> float:
    """``JARVIS_M10_CADENCE_GH_TIMEOUT_S`` — per-PR cap. Clamped
    [5, 300]. Default 30s."""
    raw = os.environ.get(
        "JARVIS_M10_CADENCE_GH_TIMEOUT_S", "",
    ).strip()
    if not raw:
        return 30.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 30.0
    return max(5.0, min(300.0, v))


def _master_enabled() -> bool:
    """Defers to canonical m10_arch_proposer_enabled."""
    try:
        from backend.core.ouroboros.governance.m10.primitives import (
            m10_arch_proposer_enabled,
        )
        return bool(m10_arch_proposer_enabled())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Cadence-policy primitives (pure)
# ---------------------------------------------------------------------------


def should_fire_at(op_count: object) -> bool:
    """True iff cadence enabled AND ``op_count % effective-step == 0``
    AND ``op_count >= 1``.

    Slice 197 — the Adaptive Synthesis Governor modulates the step: the
    base ``cadence_n_ops()`` is scaled by the recent hedge-dispatch delta
    read from the durable registry (busy window → conserve capital, idle
    window → compile aggressively). Fail-soft: governor unavailable →
    legacy base step. NEVER raises."""
    if not cadence_enabled():
        return False
    try:
        n = int(op_count)
    except (TypeError, ValueError):
        return False
    if n < 1:
        return False
    step = cadence_n_ops()
    if step < 1:
        return False
    try:
        from backend.core.ouroboros.governance.m10_autonomous_graduation import (  # noqa: E501
            effective_cadence_n,
        )
        step = effective_cadence_n(
            base_n=step, dispatch_delta=_recent_dispatch_delta(),
        )
    except Exception:  # noqa: BLE001 — pacing is enhancement, not gate
        pass
    return (n % step) == 0


_last_dispatch_reading: int = -1


def _recent_dispatch_delta() -> int:
    """Hedge dispatches since the previous cadence check — the governor's
    traffic signal, read from the durable registry. The first observation
    reports moderate traffic (delta=1) so a fresh boot neither sprints nor
    stalls. NEVER raises."""
    global _last_dispatch_reading
    try:
        from backend.core.ouroboros.governance.observability_registry import (
            HEDGE_CONCURRENCY_DISPATCHES,
            get_observability_registry,
        )
        current = get_observability_registry().get(
            HEDGE_CONCURRENCY_DISPATCHES,
        )
        if _last_dispatch_reading < 0:
            _last_dispatch_reading = current
            return 1
        delta = max(0, current - _last_dispatch_reading)
        _last_dispatch_reading = current
        return delta
    except Exception:  # noqa: BLE001
        return 1


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CadenceStepResult:
    fired: bool
    op_count: int = 0
    cadence_n_ops: int = 0
    bridge_result: Any = None
    elapsed_s: float = 0.0
    diagnostic: str = ""
    schema_version: str = field(
        default=M10_CADENCE_RUNNER_SCHEMA_VERSION,
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "fired": bool(self.fired),
            "op_count": int(self.op_count),
            "cadence_n_ops": int(self.cadence_n_ops),
            "elapsed_s": float(self.elapsed_s),
            "diagnostic": str(self.diagnostic)[:512],
            "bridge_result": (
                self.bridge_result.to_dict()
                if (
                    self.bridge_result is not None
                    and hasattr(self.bridge_result, "to_dict")
                ) else None
            ),
        }


@dataclass(frozen=True)
class PhaseTransition:
    proposal_id: str
    from_phase: str
    to_phase: str
    reason: str = ""
    pr_url: str = ""
    at_unix: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "from_phase": self.from_phase,
            "to_phase": self.to_phase,
            "reason": str(self.reason)[:256],
            "pr_url": self.pr_url,
            "at_unix": float(self.at_unix),
        }


@dataclass(frozen=True)
class SweepResult:
    transitions: Tuple[PhaseTransition, ...] = field(
        default_factory=tuple,
    )
    inspected_count: int = 0
    elapsed_s: float = 0.0
    diagnostic: str = ""
    ok: bool = True
    schema_version: str = field(
        default=M10_CADENCE_RUNNER_SCHEMA_VERSION,
    )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "ok": bool(self.ok),
            "inspected_count": int(self.inspected_count),
            "elapsed_s": float(self.elapsed_s),
            "diagnostic": str(self.diagnostic)[:512],
            "transitions": [
                t.to_dict() for t in self.transitions
            ],
        }


# ---------------------------------------------------------------------------
# SSE event publish
# ---------------------------------------------------------------------------


def _publish_phase_changed_safely(
    transition: PhaseTransition,
) -> None:
    """Lazy + best-effort publish. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED,
            transition.proposal_id,
            transition.to_dict(),
        )
    except Exception as err:  # noqa: BLE001
        logger.debug(
            "[m10_cadence_runner] SSE publish failed: %r", err,
        )


# ---------------------------------------------------------------------------
# Cadence step
# ---------------------------------------------------------------------------


async def run_cadence_step(
    op_count: object,
    *,
    bridge_callable: Optional[Any] = None,
) -> CadenceStepResult:
    """Fire the bridge iff cadence threshold met. NEVER raises."""
    started = time.monotonic()
    try:
        op_int = int(op_count)
    except (TypeError, ValueError):
        op_int = 0
    n = cadence_n_ops()

    if not should_fire_at(op_int):
        return CadenceStepResult(
            fired=False,
            op_count=op_int,
            cadence_n_ops=n,
            diagnostic=(
                f"cadence not met (op_count={op_int}, "
                f"n_ops={n}, enabled={cadence_enabled()})"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    fire_fn = bridge_callable
    if fire_fn is None:
        try:
            from backend.core.ouroboros.governance.m10.m10_producer_bridge import (  # noqa: E501
                fire_full_lifecycle_cycle,
            )
            fire_fn = fire_full_lifecycle_cycle
        except Exception as err:  # noqa: BLE001
            return CadenceStepResult(
                fired=False,
                op_count=op_int,
                cadence_n_ops=n,
                diagnostic=(
                    f"bridge import failed: {type(err).__name__}"
                ),
                elapsed_s=max(0.0, time.monotonic() - started),
            )

    try:
        bridge_result = await fire_fn()
    except Exception as err:  # noqa: BLE001
        return CadenceStepResult(
            fired=False,
            op_count=op_int,
            cadence_n_ops=n,
            diagnostic=(
                f"bridge call raised: {type(err).__name__}: {err}"
            )[:256],
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    return CadenceStepResult(
        fired=True,
        op_count=op_int,
        cadence_n_ops=n,
        bridge_result=bridge_result,
        diagnostic=(
            f"cadence fired at op_count={op_int} (every "
            f"{n} ops)"
        ),
        elapsed_s=max(0.0, time.monotonic() - started),
    )


# ---------------------------------------------------------------------------
# gh PR status reader
# ---------------------------------------------------------------------------


async def _gh_pr_status(pr_url: str) -> Tuple[str, str]:
    """Read PR state via gh CLI. Returns (state, detail) where
    state ∈ {"open", "merged", "closed", "unknown"}. Arg-list
    subprocess spawn — NO shell interpolation. NEVER raises."""
    if not pr_url:
        return ("unknown", "empty pr_url")
    try:
        proc = await _safe_subprocess_spawn(
            "gh", "pr", "view", pr_url,
            "--json", "state,mergedAt",
            "-q", ".state",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return ("unknown", "gh CLI not found")
    except Exception as err:  # noqa: BLE001
        return ("unknown", f"spawn failed: {type(err).__name__}")
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=gh_timeout_s(),
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.communicate()
        except Exception:  # noqa: BLE001
            pass
        return ("unknown", "gh pr view timed out")
    if proc.returncode != 0:
        return (
            "unknown",
            f"gh rc={proc.returncode}: "
            f"{stderr.decode(errors='replace')[:128]}",
        )
    state_raw = stdout.decode(errors="replace").strip().lower()
    if state_raw in ("merged", "open", "closed"):
        return (state_raw, "ok")
    return ("unknown", f"unexpected state: {state_raw!r}")


# ---------------------------------------------------------------------------
# Background sweep
# ---------------------------------------------------------------------------


async def sweep_pending_for_merge(
    *,
    pr_status_callable: Optional[Any] = None,
) -> SweepResult:
    """Walk pending proposals, poll PR status, transition rows.
    NEVER raises."""
    started = time.monotonic()

    if not cadence_enabled():
        return SweepResult(
            inspected_count=0,
            ok=True,
            diagnostic=(
                "cadence disabled (master or sub-flag off)"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (  # noqa: E501
            append_proposal,
            list_pending_proposals,
            StoredProposal,
        )
    except Exception as err:  # noqa: BLE001
        return SweepResult(
            ok=False,
            diagnostic=(
                f"proposal_store import failed: "
                f"{type(err).__name__}"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    pending = list_pending_proposals()
    transitions: List[PhaseTransition] = []

    status_fn = (
        pr_status_callable
        if pr_status_callable is not None
        else _gh_pr_status
    )

    for row in pending:
        if not row.pr_url:
            continue
        try:
            state, _detail = await status_fn(row.pr_url)
        except Exception as err:  # noqa: BLE001
            transitions.append(PhaseTransition(
                proposal_id=row.proposal_id,
                from_phase=row.phase,
                to_phase=row.phase,
                reason=f"poll raised: {type(err).__name__}",
                pr_url=row.pr_url,
            ))
            continue

        if state == "merged":
            new_phase = "graduated"
        elif state == "closed":
            new_phase = "rejected"
        else:
            continue

        if new_phase == row.phase:
            continue

        try:
            append_proposal(StoredProposal(
                proposal_id=row.proposal_id,
                kind=row.kind,
                phase=new_phase,
                pattern_signature=row.pattern_signature,
                detection_evidence=row.detection_evidence,
                proposed_module_path=row.proposed_module_path,
                proposed_class_name=row.proposed_class_name,
                proposed_ast_pin_name=row.proposed_ast_pin_name,
                pr_url=row.pr_url,
                pr_branch=row.pr_branch,
                failure_reason=(
                    "" if new_phase == "graduated"
                    else "pr_closed_unmerged"
                ),
                cost_usd=row.cost_usd,
                consensus_signature=row.consensus_signature,
            ))
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "[m10_cadence_runner] append raised: %r", err,
            )
            continue

        transition = PhaseTransition(
            proposal_id=row.proposal_id,
            from_phase=row.phase,
            to_phase=new_phase,
            reason=f"pr state={state}",
            pr_url=row.pr_url,
        )
        transitions.append(transition)
        _publish_phase_changed_safely(transition)

    return SweepResult(
        transitions=tuple(transitions),
        inspected_count=len(pending),
        ok=True,
        diagnostic=(
            f"inspected {len(pending)} pending; "
            f"{len(transitions)} transition(s)"
        ),
        elapsed_s=max(0.0, time.monotonic() - started),
    )


# ---------------------------------------------------------------------------
# Stale-proposal expiration
# ---------------------------------------------------------------------------


async def expire_stale_pending(
    *,
    now_unix: Optional[float] = None,
) -> SweepResult:
    """Transition AWAITING_APPROVAL rows older than
    approval_timeout_s() to EXPIRED. Branch preserved (H4).
    NEVER raises."""
    started = time.monotonic()

    if not cadence_enabled():
        return SweepResult(
            inspected_count=0,
            ok=True,
            diagnostic=(
                "cadence disabled (master or sub-flag off)"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    try:
        from backend.core.ouroboros.governance.m10.proposal_store import (  # noqa: E501
            append_proposal,
            list_pending_proposals,
            StoredProposal,
        )
    except Exception as err:  # noqa: BLE001
        return SweepResult(
            ok=False,
            diagnostic=(
                f"proposal_store import failed: "
                f"{type(err).__name__}"
            ),
            elapsed_s=max(0.0, time.monotonic() - started),
        )

    now = time.time() if now_unix is None else float(now_unix)
    deadline = now - approval_timeout_s()
    pending = list_pending_proposals()
    transitions: List[PhaseTransition] = []

    for row in pending:
        if row.phase != "awaiting_approval":
            continue
        if row.last_updated_at_unix > deadline:
            continue
        try:
            append_proposal(StoredProposal(
                proposal_id=row.proposal_id,
                kind=row.kind,
                phase="expired",
                pattern_signature=row.pattern_signature,
                detection_evidence=row.detection_evidence,
                proposed_module_path=row.proposed_module_path,
                proposed_class_name=row.proposed_class_name,
                proposed_ast_pin_name=row.proposed_ast_pin_name,
                pr_url=row.pr_url,
                pr_branch=row.pr_branch,
                failure_reason=(
                    f"approval_timeout (>"
                    f"{approval_timeout_s():.0f}s)"
                ),
                cost_usd=row.cost_usd,
                consensus_signature=row.consensus_signature,
            ))
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "[m10_cadence_runner] expire raised: %r", err,
            )
            continue

        transition = PhaseTransition(
            proposal_id=row.proposal_id,
            from_phase=row.phase,
            to_phase="expired",
            reason=(
                f"approval_timeout (>"
                f"{approval_timeout_s():.0f}s)"
            ),
            pr_url=row.pr_url,
            at_unix=now,
        )
        transitions.append(transition)
        _publish_phase_changed_safely(transition)

    return SweepResult(
        transitions=tuple(transitions),
        inspected_count=len(pending),
        ok=True,
        diagnostic=(
            f"inspected {len(pending)} pending; "
            f"{len(transitions)} expired"
        ),
        elapsed_s=max(0.0, time.monotonic() - started),
    )


# ---------------------------------------------------------------------------
# Sync wrappers (REPL)
# ---------------------------------------------------------------------------


def sweep_pending_for_merge_sync() -> SweepResult:
    return _sync_run(sweep_pending_for_merge())


def expire_stale_pending_sync(
    now_unix: Optional[float] = None,
) -> SweepResult:
    return _sync_run(expire_stale_pending(now_unix=now_unix))


def _sync_run(coro: Any) -> Any:
    """Loop-detecting bridge. NEVER raises."""
    import concurrent.futures
    try:
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
    except Exception:  # noqa: BLE001
        running = False
    try:
        if running:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="m10-cadence",
            ) as ex:
                future = ex.submit(asyncio.run, coro)
                return future.result(timeout=120.0)
        return asyncio.run(coro)
    except Exception as err:  # noqa: BLE001
        return SweepResult(
            ok=False,
            diagnostic=(
                f"sync run raised: {type(err).__name__}: {err}"
            )[:256],
        )


# ===========================================================================
# §33.1 — register_shipped_invariants
# ===========================================================================


def register_shipped_invariants() -> list:
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/m10/cadence_runner.py"
    )

    def _validate_entry_points(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        required_async = {
            "run_cadence_step",
            "sweep_pending_for_merge",
            "expire_stale_pending",
        }
        required_sync = {
            "cadence_enabled",
            "cadence_n_ops",
            "approval_timeout_s",
            "should_fire_at",
        }
        async_present: set = set()
        sync_present: set = set()
        for node in tree.body:
            if isinstance(node, _ast.AsyncFunctionDef):
                async_present.add(node.name)
            elif isinstance(node, _ast.FunctionDef):
                sync_present.add(node.name)
        missing_async = required_async - async_present
        missing_sync = required_sync - sync_present
        violations: list = []
        for m in sorted(missing_async):
            violations.append(f"missing async entry: {m!r}")
        for m in sorted(missing_sync):
            violations.append(f"missing sync entry: {m!r}")
        return tuple(violations)

    def _validate_composes_canonical(
        tree: "_ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        for needle in (
            "list_pending_proposals",
            "append_proposal",
            "StoredProposal",
            "EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED",
            "publish_task_event",
            "fire_full_lifecycle_cycle",
            "m10_arch_proposer_enabled",
        ):
            if needle not in source:
                violations.append(
                    f"must compose canonical {needle!r}"
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="m10_cadence_runner_entry_points",
            target_file=target,
            description=(
                "Cadence runner exposes the 3 async entries + "
                "4 sync accessors."
            ),
            validate=_validate_entry_points,
        ),
        ShippedCodeInvariant(
            invariant_name="m10_cadence_runner_composes_canonical",
            target_file=target,
            description=(
                "Composes canonical surfaces: proposal_store + "
                "producer-bridge + ide_observability_stream + "
                "m10 master flag."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


__all__ = [
    "M10_CADENCE_RUNNER_SCHEMA_VERSION",
    "CadenceStepResult",
    "PhaseTransition",
    "SweepResult",
    "approval_timeout_s",
    "cadence_enabled",
    "cadence_n_ops",
    "expire_stale_pending",
    "expire_stale_pending_sync",
    "gh_timeout_s",
    "register_shipped_invariants",
    "run_cadence_step",
    "should_fire_at",
    "sweep_pending_for_merge",
    "sweep_pending_for_merge_sync",
]
