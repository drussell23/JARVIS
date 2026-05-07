"""Move 6.5 Slice 4 — Multi-prior dispatch observer.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Observability slice: mirror Move 7/8 — §33.3 REPL auto-
   mount, §33.4 flock'd JSONL ledger, chatter-suppressed
   observer, §33.5 versioned rows, §33.1 graduation contract
   harness (Slice 6 of your table). Cancelled rolls may
   leave partial session artifacts — must be observable in
   ledger, not silent."

Architecturally distinct from Move 7's polling observer: Move
6.5's substrate fires per-op (event-driven), so this observer
is a **caller-invoked recorder**. The orchestrator's
eventual call site invokes :func:`record_dispatch_outcome`
with each :class:`DispatchVerdict`; the observer:

  1. Persists a §33.5-versioned row to the bounded JSONL
     ledger via :func:`cross_process_jsonl.flock_append_line`
     (§33.4 Per-Cluster Flock'd JSONL Persistence pattern —
     same primitive ``cross_op_semantic_recorder`` uses).

  2. Emits the canonical SSE event
     :data:`EVENT_TYPE_MULTI_PRIOR_DISPATCH` via
     :func:`publish_multi_prior_dispatch_event` — composes the
     canonical broker (no parallel publisher; AST-pinned).

  3. **Chatter-suppression** — operator binding's load-bearing
     discipline: SSE fires ONLY when one of:
       * action_recommendation transitioned vs the prior op
         (e.g. ACCEPT_CANONICAL → ESCALATE_TO_OPERATOR_REVIEW)
       * verdict carries cancelled_count > 0 (operator binding
         requires cancellations to be ledger-observable, not
         silent)
       * verdict carries error_count > 0 (defensive)

     Same-action ticks with no cancellations / errors are
     silent on the SSE channel (the JSONL ledger row is still
     persisted unconditionally — that's the audit substrate;
     SSE is the operator-attention surface).

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / candidate_generator / change_engine /
semantic_guardian / plan_generator / urgency_router /
direction_inferrer / policy imports. Pure substrate observer.

**Master flag** ``JARVIS_MULTI_PRIOR_OBSERVER_ENABLED``
default-FALSE per §33.1. When OFF, :func:`record_dispatch_outcome`
short-circuits (zero filesystem touch, zero SSE emission).

**§33.4 composition** (AST-pinned): the observer MUST persist
via :func:`cross_process_jsonl.flock_append_line` — no raw
``open(..., "a")`` for the JSONL ledger. Forbidden alternates
are caught structurally.

**§33.5 versioned rows** — every row carries
:data:`MULTI_PRIOR_OBSERVER_SCHEMA_VERSION`; readers bail on
schema mismatch returning empty (NEVER raise on legacy data).

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Dict, FrozenSet, List, Mapping, Optional, Tuple,
)


logger = logging.getLogger(
    "Ouroboros.MultiPriorObserver",
)


MULTI_PRIOR_OBSERVER_SCHEMA_VERSION: str = (
    "multi_prior_observer.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


# Default ledger path — composed under .jarvis/ root by
# convention. Operator can override via env knob; tests pass
# their own path.
_DEFAULT_LEDGER_FILENAME: str = (
    "multi_prior_dispatch.jsonl"
)
# Per-op cap on how many rows the read API surfaces by
# default. Operator-tunable; ledger file itself is
# unbounded-ish (size cap below acts as a defensive ceiling).
_DEFAULT_READ_LIMIT: int = 50
# 50 MiB defensive ceiling on JSONL file size — same shape as
# cross_op_semantic_recorder. Reader bails when exceeded so a
# pathological writer doesn't OOM the read path.
_DEFAULT_LEDGER_SIZE_CAP_BYTES: int = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Master flag + tunable knobs
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_MULTI_PRIOR_OBSERVER_ENABLED`` master switch.
    Default-FALSE per §33.1: when OFF,
    :func:`record_dispatch_outcome` short-circuits (zero
    filesystem touch, zero SSE emission). Pure read; NEVER
    raises."""
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


def ledger_path() -> Path:
    """Resolve the canonical ledger path. Pure read; NEVER
    raises."""
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_DISPATCH_LEDGER_PATH", "",
    ).strip()
    if raw:
        return Path(raw)
    return Path(".jarvis") / _DEFAULT_LEDGER_FILENAME


def read_limit_default() -> int:
    """Operator-tunable read-API limit."""
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_READ_LIMIT", "",
    ).strip()
    if not raw:
        return _DEFAULT_READ_LIMIT
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_READ_LIMIT
    if v < 1:
        return _DEFAULT_READ_LIMIT
    if v > 1000:
        return 1000
    return v


# ---------------------------------------------------------------------------
# Frozen artifact (§33.5 versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiPriorObservation:
    """One ledger row. §33.5 versioned-artifact contract.

    Stores essentials only — full DispatchVerdict (with
    PriorSet + per-roll diffs) is too heavy for a bounded
    JSONL ring. Operators who want full per-roll detail invoke
    Slice 5's ``/canvas`` REPL or replay the original op.
    """

    op_id: str
    decision: str
    action_recommendation: str
    consensus_outcome: str
    completed_count: int
    cancelled_count: int
    timeout_count: int
    error_count: int
    cost_total_usd: float
    wall_clock_s: float
    rationale_preview: str
    """First 256 chars of the operator-facing rationale.
    Slice 5's diff renderer composes the full thing live;
    ledger preview is for quick scan."""

    ts_unix: float
    schema_version: str = field(
        default=MULTI_PRIOR_OBSERVER_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op_id": str(self.op_id),
            "decision": str(self.decision),
            "action_recommendation": str(
                self.action_recommendation,
            ),
            "consensus_outcome": str(self.consensus_outcome),
            "completed_count": int(self.completed_count),
            "cancelled_count": int(self.cancelled_count),
            "timeout_count": int(self.timeout_count),
            "error_count": int(self.error_count),
            "cost_total_usd": float(self.cost_total_usd),
            "wall_clock_s": float(self.wall_clock_s),
            "rationale_preview": str(
                self.rationale_preview,
            )[:256],
            "ts_unix": float(self.ts_unix),
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["MultiPriorObservation"]:
        """Reconstruct from a ``to_dict`` payload. Returns
        None on schema mismatch / malformed shape. NEVER
        raises."""
        try:
            schema = payload.get("schema_version")
            if schema != MULTI_PRIOR_OBSERVER_SCHEMA_VERSION:
                return None
            return cls(
                op_id=str(payload["op_id"]),
                decision=str(payload["decision"]),
                action_recommendation=str(
                    payload["action_recommendation"],
                ),
                consensus_outcome=str(
                    payload.get("consensus_outcome", ""),
                ),
                completed_count=int(
                    payload.get("completed_count", 0),
                ),
                cancelled_count=int(
                    payload.get("cancelled_count", 0),
                ),
                timeout_count=int(
                    payload.get("timeout_count", 0),
                ),
                error_count=int(
                    payload.get("error_count", 0),
                ),
                cost_total_usd=float(
                    payload.get("cost_total_usd", 0.0),
                ),
                wall_clock_s=float(
                    payload.get("wall_clock_s", 0.0),
                ),
                rationale_preview=str(
                    payload.get("rationale_preview", ""),
                ),
                ts_unix=float(payload["ts_unix"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Singleton observer — event-driven (caller invokes recorder)
# ---------------------------------------------------------------------------


class MultiPriorDispatchObserver:
    """Singleton observer. State: the prior op's
    action_recommendation (for chatter suppression). Recorder
    is event-driven — caller invokes :meth:`record` with each
    :class:`DispatchVerdict`.

    Thread-safe via simple lock; the recorder is fire-and-
    forget from the orchestrator's perspective."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prev_action: Optional[str] = None
        # Counters are best-effort process-local telemetry —
        # the JSONL ledger is the cross-process source of truth.
        self._record_count: int = 0
        self._sse_emitted_count: int = 0
        self._suppressed_count: int = 0

    def record(
        self,
        *,
        op_id: str,
        decision: str,
        action_recommendation: str,
        consensus_outcome: str,
        completed_count: int,
        cancelled_count: int,
        timeout_count: int,
        error_count: int,
        cost_total_usd: float,
        wall_clock_s: float,
        rationale: str,
        ledger_path_override: Optional[Path] = None,
    ) -> Optional[MultiPriorObservation]:
        """Record one dispatch outcome. Persists to JSONL +
        emits chatter-suppressed SSE. Returns the observation
        when persisted, None when master flag off / persist
        failed. NEVER raises."""
        if not master_enabled():
            return None
        try:
            obs = MultiPriorObservation(
                op_id=str(op_id),
                decision=str(decision),
                action_recommendation=str(
                    action_recommendation,
                ),
                consensus_outcome=str(consensus_outcome),
                completed_count=int(completed_count),
                cancelled_count=int(cancelled_count),
                timeout_count=int(timeout_count),
                error_count=int(error_count),
                cost_total_usd=float(cost_total_usd),
                wall_clock_s=float(wall_clock_s),
                rationale_preview=(
                    str(rationale or "")[:256]
                ),
                ts_unix=time.time(),
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[MultiPriorObserver] artifact build failed",
                exc_info=True,
            )
            return None

        # §33.4 flock'd persistence — composes canonical
        # primitive (AST-pinned no-parallel-persistence).
        persisted = _flock_persist(
            obs=obs,
            target=ledger_path_override or ledger_path(),
        )
        if not persisted:
            return None

        with self._lock:
            self._record_count += 1
            prev_action = self._prev_action
            self._prev_action = str(action_recommendation)

        # Chatter-suppressed SSE — operator-binding load-bearing.
        # Emit when:
        #   * action transitioned vs prior op, OR
        #   * cancelled_count > 0 (cancellations MUST be visible)
        #   * error_count > 0 (defensive)
        # Same-action with no cancels / errors → silent.
        emit = (
            prev_action != str(action_recommendation)
            or cancelled_count > 0
            or error_count > 0
        )
        if emit:
            self._publish_sse(
                obs=obs, prev_action=prev_action or "",
            )
            with self._lock:
                self._sse_emitted_count += 1
        else:
            with self._lock:
                self._suppressed_count += 1
        return obs

    def telemetry(self) -> Dict[str, int]:
        """Process-local counters. NEVER raises."""
        with self._lock:
            return {
                "record_count": self._record_count,
                "sse_emitted_count": self._sse_emitted_count,
                "suppressed_count": self._suppressed_count,
            }

    def _publish_sse(
        self,
        *,
        obs: MultiPriorObservation,
        prev_action: str,
    ) -> bool:
        """Emit via the canonical broker. Composition
        discipline AST-pinned — NEVER call broker.publish
        directly. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                publish_multi_prior_dispatch_event,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[MultiPriorObserver] publisher import "
                "failed: %s", exc,
            )
            return False
        try:
            publish_multi_prior_dispatch_event(
                op_id=obs.op_id,
                decision=obs.decision,
                action_recommendation=(
                    obs.action_recommendation
                ),
                prev_action_recommendation=prev_action,
                consensus_outcome=obs.consensus_outcome,
                completed_count=obs.completed_count,
                cancelled_count=obs.cancelled_count,
                timeout_count=obs.timeout_count,
                error_count=obs.error_count,
                cost_total_usd=obs.cost_total_usd,
                wall_clock_s=obs.wall_clock_s,
                ts_unix=obs.ts_unix,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[MultiPriorObserver] publish raised: %s",
                exc,
            )
            return False


_DEFAULT_OBSERVER: Optional[MultiPriorDispatchObserver] = None
_DEFAULT_OBSERVER_LOCK = threading.Lock()


def get_default_observer() -> MultiPriorDispatchObserver:
    """Singleton accessor. NEVER raises."""
    global _DEFAULT_OBSERVER
    with _DEFAULT_OBSERVER_LOCK:
        if _DEFAULT_OBSERVER is None:
            _DEFAULT_OBSERVER = MultiPriorDispatchObserver()
        return _DEFAULT_OBSERVER


def reset_default_observer_for_test() -> None:
    """Test helper — drops the singleton so each test fresh-
    starts the observer state. NEVER raises."""
    global _DEFAULT_OBSERVER
    with _DEFAULT_OBSERVER_LOCK:
        _DEFAULT_OBSERVER = None


# ---------------------------------------------------------------------------
# Public recorder — the function the orchestrator's call site invokes
# ---------------------------------------------------------------------------


def record_dispatch_outcome(
    dispatch_verdict: Any,
    *,
    ledger_path_override: Optional[Path] = None,
) -> Optional[MultiPriorObservation]:
    """Record a Slice 3 :class:`DispatchVerdict` outcome.
    Composes the singleton observer + extracts essentials
    from the verdict. NEVER raises.

    Caller-invoked from the orchestrator's eventual
    integration point. When master flag off, returns None
    immediately."""
    if not master_enabled():
        return None
    if dispatch_verdict is None:
        return None
    try:
        op_id = str(getattr(dispatch_verdict, "op_id", ""))
        decision = str(
            getattr(
                dispatch_verdict.decision, "value", "",
            ),
        )
        action = str(
            getattr(
                dispatch_verdict.action_recommendation,
                "value",
                "",
            ),
        )
        rationale = str(
            getattr(dispatch_verdict, "rationale", ""),
        )
        verdict_result = getattr(
            dispatch_verdict, "verdict_result", None,
        )
        if verdict_result is None:
            consensus_outcome = ""
            completed = 0
            cancelled = 0
            timed_out = 0
            errored = 0
            cost = 0.0
            wall = 0.0
        else:
            consensus = getattr(
                verdict_result, "consensus_verdict", None,
            )
            try:
                consensus_outcome = str(
                    consensus.outcome.value,
                )
            except (AttributeError, TypeError):
                consensus_outcome = ""
            completed = int(
                getattr(verdict_result, "completed_count", 0),
            )
            cancelled = int(
                getattr(verdict_result, "cancelled_count", 0),
            )
            timed_out = int(
                getattr(verdict_result, "timeout_count", 0),
            )
            errored = int(
                getattr(verdict_result, "error_count", 0),
            )
            cost = float(
                getattr(
                    verdict_result, "cost_total_usd", 0.0,
                ),
            )
            wall = float(
                getattr(
                    verdict_result, "wall_clock_s", 0.0,
                ),
            )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorObserver] verdict extraction "
            "failed", exc_info=True,
        )
        return None
    return get_default_observer().record(
        op_id=op_id,
        decision=decision,
        action_recommendation=action,
        consensus_outcome=consensus_outcome,
        completed_count=completed,
        cancelled_count=cancelled,
        timeout_count=timed_out,
        error_count=errored,
        cost_total_usd=cost,
        wall_clock_s=wall,
        rationale=rationale,
        ledger_path_override=ledger_path_override,
    )


# ---------------------------------------------------------------------------
# §33.4 Per-Cluster Flock'd JSONL Persistence
# ---------------------------------------------------------------------------


def _flock_persist(
    *,
    obs: MultiPriorObservation,
    target: Path,
) -> bool:
    """Append one row via the canonical
    :func:`cross_process_jsonl.flock_append_line`. Returns
    True on success, False on import / I/O failure. NEVER
    raises.

    Composes the canonical persistence primitive — same shape
    as ``cross_op_semantic_recorder._persist_centroid`` per
    §33.4 Per-Cluster Flock'd JSONL Persistence Pattern."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MultiPriorObserver] flock primitive "
            "unavailable: %s", exc,
        )
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        import json
        line = json.dumps(
            obs.to_dict(), ensure_ascii=True,
            separators=(",", ":"),
        )
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        return bool(flock_append_line(target, line))
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MultiPriorObserver] flock_append_line "
            "raised: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Read API — for /multi_prior REPL + GET /observability/multi-prior
# ---------------------------------------------------------------------------


def read_recent_observations(
    *,
    limit: Optional[int] = None,
    path: Optional[Path] = None,
) -> Tuple[MultiPriorObservation, ...]:
    """Read the most recent :class:`MultiPriorObservation`
    rows from the JSONL ledger in append-order (newest LAST).
    NEVER raises; returns empty tuple on missing file / I/O
    error / schema-mismatch.

    Composes the canonical
    :func:`cross_process_jsonl.flock_critical_section`
    read primitive — same shape as
    ``cross_op_semantic_recorder.read_recent_centroids``."""
    if limit is None:
        limit = read_limit_default()
    target = path if path is not None else ledger_path()
    if not target.exists():
        return ()
    try:
        size = target.stat().st_size
    except OSError:
        return ()
    if size > _DEFAULT_LEDGER_SIZE_CAP_BYTES:
        # Defensive: pathological writer; bail clean.
        logger.debug(
            "[MultiPriorObserver] ledger exceeds "
            "%d bytes, returning empty",
            _DEFAULT_LEDGER_SIZE_CAP_BYTES,
        )
        return ()
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_critical_section,
        )
    except Exception:  # noqa: BLE001
        return ()
    rows_raw: List[str] = []
    try:
        with flock_critical_section(target) as acquired:
            if not acquired:
                return ()
            try:
                with target.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rows_raw.append(line)
            except OSError:
                return ()
    except Exception:  # noqa: BLE001 — defensive
        return ()
    if limit > 0 and len(rows_raw) > limit:
        rows_raw = rows_raw[-limit:]
    out: List[MultiPriorObservation] = []
    import json
    for raw in rows_raw:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        obs = MultiPriorObservation.from_dict(payload)
        if obs is not None:
            out.append(obs)
    return tuple(out)


def find_by_op_id(
    op_id: str,
    *,
    path: Optional[Path] = None,
) -> Optional[MultiPriorObservation]:
    """Return the most recent observation for ``op_id`` (None
    on miss). Pure read; NEVER raises."""
    name = str(op_id or "").strip()
    if not name:
        return None
    rows = read_recent_observations(limit=1000, path=path)
    for obs in reversed(rows):  # newest first via reverse
        if obs.op_id == name:
            return obs
    return None


def action_distribution(
    *,
    path: Optional[Path] = None,
) -> Dict[str, int]:
    """Distribution of action_recommendations across all
    rows in the ledger. Used by Slice 6 graduation contract.
    Pure read; NEVER raises."""
    rows = read_recent_observations(limit=10000, path=path)
    out: Dict[str, int] = {}
    for r in rows:
        key = r.action_recommendation
        out[key] = out.get(key, 0) + 1
    return out


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the flags this module reads."""
    try:
        registry.register(
            name="JARVIS_MULTI_PRIOR_OBSERVER_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for Move 6.5 Slice 4 "
                "dispatch observer. Default-FALSE per §33.1; "
                "when off, record_dispatch_outcome returns "
                "None and emits no SSE / writes no ledger row."
            ),
            category="Observability",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/multi_prior_observer.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorObserver] master-flag seeding "
            "failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name=(
                "JARVIS_MULTI_PRIOR_DISPATCH_LEDGER_PATH"
            ),
            type_="path",
            default=str(
                Path(".jarvis") / _DEFAULT_LEDGER_FILENAME
            ),
            description=(
                "JSONL ledger path for the Move 6.5 dispatch "
                "observer. §33.4 flock'd persistence; one "
                "row per dispatch_multi_prior call."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/multi_prior_observer.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_DISPATCH_LEDGER_PATH="
                ".jarvis/multi_prior_dispatch.jsonl"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorObserver] ledger-path seeding "
            "failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name="JARVIS_MULTI_PRIOR_READ_LIMIT",
            type_="int",
            default=str(_DEFAULT_READ_LIMIT),
            description=(
                "Default limit for read_recent_observations + "
                "/multi_prior REPL bare overview. Clamped "
                "[1, 1000]."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/multi_prior_observer.py"
            ),
            example="JARVIS_MULTI_PRIOR_READ_LIMIT=50",
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorObserver] read-limit seeding "
            "failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``multi_prior_observer_master_default_false``
      2. ``multi_prior_observer_authority_asymmetry``
      3. ``multi_prior_observer_chatter_suppression``
         — emit gate MUST require ``prev != current`` OR
         cancelled / error nonzero. Bytes-pinned via AST
         BoolOp inspection on the publish gate.
      4. ``multi_prior_observer_composes_canonical_jsonl``
         — MUST use ``flock_append_line`` /
         ``flock_critical_section``; NO raw ``open(...,
         "a")`` for the JSONL ledger.
      5. ``multi_prior_observer_composes_canonical_publisher``
         — MUST call ``publish_multi_prior_dispatch_event``;
         no parallel ``broker.publish`` invocations.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/verification/"
        "multi_prior_observer.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                target_func = node
                break
        if target_func is None:
            violations.append("master_enabled() missing")
            return tuple(violations)
        empty_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            for cmp_node in ast.walk(sub.test):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                operand_empty = False
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        operand_empty = True
                        break
                if not operand_empty:
                    continue
                for stmt in sub.body:
                    if isinstance(stmt, ast.Return) and (
                        isinstance(stmt.value, ast.Constant)
                        and stmt.value.value is False
                    ):
                        empty_returns_false = True
                        break
                if empty_returns_false:
                    break
            if empty_returns_false:
                break
        if not empty_returns_false:
            violations.append(
                "master_enabled() MUST return False on "
                "empty env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "multi_prior_observer" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"multi_prior_observer.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"multi_prior_observer.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_chatter_suppression(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """Slice 4's emit gate MUST compose:
          * action transition (prev_action != current), OR
          * cancelled_count > 0, OR
          * error_count > 0
        Asserted via AST BoolOp inspection on the BoolOp
        whose result feeds the ``emit`` assignment.
        """
        violations: list = []
        # Search for ``emit = ... or ... or ...`` BoolOp.
        emit_assign: Optional[ast.Assign] = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id == "emit"
                    ):
                        emit_assign = node
                        break
                if emit_assign is not None:
                    break
        if emit_assign is None:
            violations.append(
                "chatter-suppression: ``emit = ...`` "
                "assignment missing"
            )
            return tuple(violations)
        # Check the BoolOp contains references to all three:
        # prev_action (NotEq comparison), cancelled_count,
        # error_count.
        src_segment = ast.unparse(emit_assign.value)
        if "prev_action" not in src_segment:
            violations.append(
                "chatter-suppression: emit gate MUST check "
                "``prev_action`` for transition detection"
            )
        if "cancelled_count" not in src_segment:
            violations.append(
                "chatter-suppression: emit gate MUST check "
                "``cancelled_count > 0`` per operator binding "
                "(cancelled rolls must be ledger-observable)"
            )
        if "error_count" not in src_segment:
            violations.append(
                "chatter-suppression: emit gate MUST check "
                "``error_count > 0`` for defensive emission"
            )
        return tuple(violations)

    def _validate_composes_canonical_jsonl(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        # Composition: ``flock_append_line`` AND
        # ``flock_critical_section`` MUST appear in source.
        if "flock_append_line" not in source:
            violations.append(
                "composes-canonical-jsonl: source MUST use "
                "cross_process_jsonl.flock_append_line "
                "(§33.4)"
            )
        if "flock_critical_section" not in source:
            violations.append(
                "composes-canonical-jsonl: source MUST use "
                "cross_process_jsonl.flock_critical_section "
                "(§33.4)"
            )
        # Forbidden: raw ``open(... "a"...)`` for ledger
        # writes. Walk for any Call whose func is Name("open")
        # with second arg starting with "a".
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_open = (
                isinstance(func, ast.Name)
                and func.id == "open"
            )
            if not is_open:
                continue
            # Look at second positional arg (or "mode" kw)
            # to detect append modes.
            mode_arg: Optional[str] = None
            if len(node.args) >= 2:
                if isinstance(node.args[1], ast.Constant):
                    mode_arg = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(
                    kw.value, ast.Constant,
                ):
                    mode_arg = str(kw.value.value)
            if mode_arg and mode_arg.startswith("a"):
                violations.append(
                    f"composes-canonical-jsonl: raw "
                    f"``open(..., {mode_arg!r})`` is "
                    f"forbidden — use flock_append_line "
                    f"(line {node.lineno})"
                )
        return tuple(violations)

    def _validate_composes_canonical_publisher(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "publish_multi_prior_dispatch_event" not in source:
            violations.append(
                "composes-canonical-publisher: source MUST "
                "import + invoke "
                "publish_multi_prior_dispatch_event"
            )
        # Forbid direct ``broker.publish(`` or ``.publish(`` —
        # composition discipline says go through the helper.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr == "publish":
                    # Best-effort: the canonical helper
                    # ``publish_multi_prior_dispatch_event`` is
                    # a Name call (not Attribute), so any
                    # Attribute ``.publish`` is suspect.
                    # Allow it ONLY when invoked on an
                    # explicit ``broker`` name (caller chose
                    # the canonical publisher path); however
                    # to remain conservative we just flag
                    # any Attribute .publish call.
                    violations.append(
                        f"composes-canonical-publisher: "
                        f"direct ``.publish(...)`` call is "
                        f"forbidden — use "
                        f"publish_multi_prior_dispatch_event "
                        f"(line {node.lineno})"
                    )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_observer_master_default_false"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_observer_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_observer_chatter_suppression"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — chatter-suppression "
                "structural: emit gate composes "
                "prev_action transition + cancelled / "
                "error counts (operator binding)."
            ),
            validate=_validate_chatter_suppression,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_observer_"
                "composes_canonical_jsonl"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — §33.4 Per-Cluster "
                "Flock'd JSONL: persistence composes "
                "flock_append_line + flock_critical_section; "
                "no raw open(..., 'a') for ledger."
            ),
            validate=_validate_composes_canonical_jsonl,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_observer_"
                "composes_canonical_publisher"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 4 — SSE emission composes "
                "publish_multi_prior_dispatch_event; no "
                "direct broker.publish / .publish calls."
            ),
            validate=_validate_composes_canonical_publisher,
        ),
    ]


__all__ = [
    "MULTI_PRIOR_OBSERVER_SCHEMA_VERSION",
    "MultiPriorDispatchObserver",
    "MultiPriorObservation",
    "action_distribution",
    "find_by_op_id",
    "get_default_observer",
    "ledger_path",
    "master_enabled",
    "read_limit_default",
    "read_recent_observations",
    "record_dispatch_outcome",
    "register_flags",
    "register_shipped_invariants",
    "reset_default_observer_for_test",
]
