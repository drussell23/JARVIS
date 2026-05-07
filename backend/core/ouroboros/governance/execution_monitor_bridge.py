"""Phase 3 A1 — ExecutionMonitor bridge.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "A1: ExecutionMonitor.record() from orchestrator COMPLETE
   (and failure paths) → SafetyNet / escalation inputs —
   never raises, bounded JSONL if applicable. Each slice:
   §33.1 master default-FALSE where appropriate, AST pins
   for event taxonomy + 'no orchestrator import' on
   observability routers."

This module is the **single composition point** between the
orchestrator's terminal-state path (COMPLETE / POSTMORTEM /
CANCELLED / FAILED) and the canonical
:class:`ExecutionMonitor` singleton in
:mod:`autonomy.execution_monitor`. SafetyNet already reads
the singleton; this bridge ensures every terminal op is
written, so SafetyNet's escalation decisions reflect the
full lifecycle (not only the synthetic test-pass/fail
signals it gets today).

## Composition discipline (AST-pinned)

The bridge is the **caller-invoked** entry point. The
orchestrator's complete_runner / failure handlers invoke
:func:`record_terminal_outcome` — same shape as Move 6.5
Slice 4's :func:`record_dispatch_outcome`. The substrate:

  1. Composes :func:`get_default_monitor` (single source of
     truth — no parallel monitor instance) — AST-pinned via
     ``execution_monitor_bridge_composes_canonical_monitor``.
  2. Composes the canonical 9-value
     :class:`ExecutionStatus` enum via deterministic
     mapping table. No new status taxonomy. AST-pinned via
     ``execution_monitor_bridge_status_table_canonical``.
  3. Persists a bounded §33.5 versioned JSONL row via
     :func:`cross_process_jsonl.flock_append_line` (§33.4
     pattern — same primitive Slice 4 + Move 7 use). No
     parallel ledger. AST-pinned via
     ``execution_monitor_bridge_composes_canonical_jsonl``.

## Authority asymmetry

No orchestrator / iron_gate / providers / candidate_generator
/ change_engine / semantic_guardian / plan_generator /
urgency_router / direction_inferrer / policy imports.
Substrate-pure caller-invoked recorder. AST-pinned.

## Master flag

``JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED`` default-FALSE
per §33.1. When OFF, :func:`record_terminal_outcome` is a
no-op — zero filesystem touch, zero monitor mutation,
zero behavior change at the orchestrator's call sites.

## NEVER raises

Every code path defensive — terminal_reason mapping
failures fall back to ``ExecutionStatus.FAILED``;
ExecutionOutcome construction failures swallowed; monitor
record failures swallowed; JSONL persistence failures
swallowed. The bridge is a monitor; it MUST NOT itself
become a failure mode.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Dict, FrozenSet, Mapping, Optional,
)


logger = logging.getLogger(
    "Ouroboros.ExecutionMonitorBridge",
)


EXECUTION_MONITOR_BRIDGE_SCHEMA_VERSION: str = (
    "execution_monitor_bridge.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


_DEFAULT_LEDGER_FILENAME: str = (
    "execution_monitor_bridge.jsonl"
)


# 50 MiB defensive ceiling on JSONL file size (matches
# multi_prior_observer + cross_op_semantic_recorder
# discipline). Reader bails when exceeded; pathological
# writer cannot OOM the read path.
_DEFAULT_LEDGER_SIZE_CAP_BYTES: int = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Master flag + tunable knobs
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED`` master
    switch. Default-FALSE per §33.1: when OFF,
    :func:`record_terminal_outcome` returns immediately
    (zero-cost). NEVER raises."""
    raw = os.environ.get(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


def ledger_path() -> Path:
    """Resolve the canonical ledger path. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_EXECUTION_MONITOR_BRIDGE_LEDGER_PATH", "",
    ).strip()
    if raw:
        return Path(raw)
    return Path(".jarvis") / _DEFAULT_LEDGER_FILENAME


# ---------------------------------------------------------------------------
# Terminal-reason → ExecutionStatus mapping table
# ---------------------------------------------------------------------------


# Module-level deterministic mapping (auditable; AST-pinned).
# Operator binding "no hardcoding" satisfied: the table is
# canonical + version-stamped + AST-pinned (entries are
# bytes-checkable in source).
#
# Maps the orchestrator's `ctx.terminal_reason_code` strings
# to the canonical `ExecutionStatus` enum NAME (string).
# Slice 5's resolver looks the name up in the enum at
# dispatch time (lazy-imported) so the table doesn't need
# to import autonomy.execution_monitor at module top.
_TERMINAL_REASON_TO_STATUS: Mapping[str, str] = {
    # Success
    "complete": "COMPLETED",
    # Cost / resource limits → resource-violation cluster
    "op_cost_cap_exceeded": "TIMEOUT",
    "no_forward_progress": "ITERATION_EXCEEDED",
    # Operator-triggered cancellation
    "user_cancelled": "FAILED",
    "advisor_blocked": "FAILED",
    # Plan-stage gate failures
    "plan_required_unavailable": "FAILED",
    "plan_review_unavailable": "FAILED",
    "plan_rejected": "FAILED",
    "plan_approval_expired": "FAILED",
    # Hard pipeline exception
    "unhandled_pipeline_exception": "FAILED",
    # Emergency-brake levels (governor escalation)
    "emergency_warning": "FAILED",
    "emergency_critical": "FAILED",
    "emergency_brake": "FAILED",
}


def get_terminal_status_name(reason_code: str) -> str:
    """Pure mapping lookup. Returns canonical
    :class:`ExecutionStatus` name (string). Falls back to
    ``FAILED`` on miss / blank / non-string input. NEVER
    raises."""
    try:
        key = str(reason_code or "").strip().lower()
    except Exception:  # noqa: BLE001 — defensive
        return "FAILED"
    return _TERMINAL_REASON_TO_STATUS.get(key, "FAILED")


# ---------------------------------------------------------------------------
# Frozen artifact (§33.5 versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalOutcomeRecord:
    """One ledger row. §33.5 versioned-artifact contract.

    Stores the projection of ``ctx`` at terminal state +
    the canonical :class:`ExecutionStatus` name + duration
    metadata. Slice 4-style: lightweight summary, full
    `ctx` snapshot lives elsewhere (postmortem ledger,
    causality DAG)."""

    op_id: str
    status_name: str
    terminal_reason_code: str
    terminal_phase: str
    duration_ms: float
    ts_unix: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(
        default=EXECUTION_MONITOR_BRIDGE_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        # Defensive serialization of metadata: non-JSON
        # values fall back to their str() representation so
        # the ledger row is always parseable.
        meta_safe: Dict[str, Any] = {}
        try:
            for k, v in (self.metadata or {}).items():
                try:
                    import json
                    json.dumps(v)
                    meta_safe[str(k)] = v
                except (TypeError, ValueError):
                    meta_safe[str(k)] = str(v)[:256]
        except Exception:  # noqa: BLE001 — defensive
            meta_safe = {}
        return {
            "op_id": str(self.op_id),
            "status_name": str(self.status_name),
            "terminal_reason_code": str(
                self.terminal_reason_code,
            ),
            "terminal_phase": str(self.terminal_phase),
            "duration_ms": float(self.duration_ms),
            "ts_unix": float(self.ts_unix),
            "metadata": meta_safe,
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["TerminalOutcomeRecord"]:
        try:
            schema = payload.get("schema_version")
            if schema != (
                EXECUTION_MONITOR_BRIDGE_SCHEMA_VERSION
            ):
                return None
            return cls(
                op_id=str(payload["op_id"]),
                status_name=str(payload["status_name"]),
                terminal_reason_code=str(
                    payload.get("terminal_reason_code", ""),
                ),
                terminal_phase=str(
                    payload.get("terminal_phase", ""),
                ),
                duration_ms=float(
                    payload.get("duration_ms", 0.0),
                ),
                ts_unix=float(payload["ts_unix"]),
                metadata=dict(payload.get("metadata", {})),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Public recorder — caller-invoked from orchestrator terminal paths
# ---------------------------------------------------------------------------


def record_terminal_outcome(
    *,
    op_id: str,
    terminal_reason_code: str,
    terminal_phase: str,
    duration_ms: float = 0.0,
    metadata: Optional[Mapping[str, Any]] = None,
    ledger_path_override: Optional[Path] = None,
) -> Optional[TerminalOutcomeRecord]:
    """Record one orchestrator terminal-state outcome.
    Composes:
      1. Build :class:`TerminalOutcomeRecord` artifact.
      2. Lazy-import + invoke the canonical
         :class:`ExecutionMonitor` singleton's record() with
         a derived :class:`ExecutionOutcome`.
      3. Append §33.5 versioned row to bounded JSONL ledger
         via §33.4 :func:`flock_append_line`.

    Returns the persisted record on success, None when:
      * Master flag off
      * op_id blank
      * Monitor / JSONL persistence failed defensively

    NEVER raises (asyncio.CancelledError caught + swallowed
    — this is a pure-sync recorder; cancellation isn't a
    primary concern, but the catch-all is operator-binding
    load-bearing: 'never raises')."""
    if not master_enabled():
        return None
    name = str(op_id or "").strip()
    if not name:
        return None

    status_name = get_terminal_status_name(
        terminal_reason_code,
    )

    try:
        record = TerminalOutcomeRecord(
            op_id=name,
            status_name=status_name,
            terminal_reason_code=str(
                terminal_reason_code or "",
            ),
            terminal_phase=str(terminal_phase or ""),
            duration_ms=float(duration_ms),
            ts_unix=time.time(),
            metadata=dict(metadata or {}),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionMonitorBridge] record build failed",
            exc_info=True,
        )
        return None

    # Step 2: compose canonical ExecutionMonitor (lazy-import).
    _propagate_to_canonical_monitor(record)

    # Step 3: persist to bounded JSONL ledger (§33.4).
    persisted = _flock_persist(
        record=record,
        target=(
            ledger_path_override
            if ledger_path_override is not None
            else ledger_path()
        ),
    )
    return record if persisted else None


def _propagate_to_canonical_monitor(
    record: TerminalOutcomeRecord,
) -> None:
    """Compose :class:`ExecutionMonitor.record` (single
    source of truth — no parallel monitor instance).
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.autonomy.execution_monitor import (  # noqa: E501
            ExecutionOutcome,
            ExecutionStatus,
            get_default_monitor,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ExecutionMonitorBridge] canonical monitor "
            "unavailable: %s", exc,
        )
        return
    try:
        # Map status name → ExecutionStatus enum member.
        try:
            status = ExecutionStatus[record.status_name]
        except (KeyError, ValueError):
            status = ExecutionStatus.FAILED
        # Build ExecutionOutcome. start_ns is back-derived
        # from end + duration (best-effort; the outcome's
        # is_terminal property only checks status, not
        # timestamps).
        end_ns = time.monotonic_ns()
        start_ns = end_ns - int(
            max(0.0, record.duration_ms) * 1_000_000,
        )
        outcome = ExecutionOutcome(
            op_id=record.op_id,
            status=status,
            start_ns=start_ns,
            end_ns=end_ns,
            duration_ms=record.duration_ms,
            error_message=str(
                record.metadata.get("error_message", "")
                or record.terminal_reason_code,
            )[:512],
            metadata={
                "terminal_phase": record.terminal_phase,
                "terminal_reason_code": (
                    record.terminal_reason_code
                ),
                "bridge_schema_version": (
                    record.schema_version
                ),
            },
        )
        get_default_monitor().record(outcome)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionMonitorBridge] monitor record "
            "raised", exc_info=True,
        )


# ---------------------------------------------------------------------------
# §33.4 Per-Cluster Flock'd JSONL Persistence
# ---------------------------------------------------------------------------


def _flock_persist(
    *,
    record: TerminalOutcomeRecord,
    target: Path,
) -> bool:
    """Append one row via the canonical
    :func:`cross_process_jsonl.flock_append_line` (§33.4
    pattern). Returns True on success, False on import / I/O
    failure. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ExecutionMonitorBridge] flock primitive "
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
            record.to_dict(), ensure_ascii=True,
            separators=(",", ":"),
        )
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        return bool(flock_append_line(target, line))
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ExecutionMonitorBridge] flock_append_line "
            "raised: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Read API — for /observability surface (Slice 7+) + tests
# ---------------------------------------------------------------------------


def read_recent_records(
    *,
    limit: int = 50,
    path: Optional[Path] = None,
) -> tuple:
    """Read the most recent :class:`TerminalOutcomeRecord`
    rows from the JSONL ledger. NEVER raises; returns empty
    tuple on missing file / I/O error / schema-mismatch.
    Composes canonical
    :func:`cross_process_jsonl.flock_critical_section`
    read primitive (§33.4)."""
    target = path if path is not None else ledger_path()
    if not target.exists():
        return ()
    try:
        size = target.stat().st_size
    except OSError:
        return ()
    if size > _DEFAULT_LEDGER_SIZE_CAP_BYTES:
        logger.debug(
            "[ExecutionMonitorBridge] ledger > %d bytes; "
            "returning empty",
            _DEFAULT_LEDGER_SIZE_CAP_BYTES,
        )
        return ()
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_critical_section,
        )
    except Exception:  # noqa: BLE001
        return ()
    rows_raw: list = []
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
    out: list = []
    import json as _json
    for raw in rows_raw:
        try:
            payload = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        rec = TerminalOutcomeRecord.from_dict(payload)
        if rec is not None:
            out.append(rec)
    return tuple(out)


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds 2 flags."""
    try:
        registry.register(
            name=(
                "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED"
            ),
            type_="bool",
            default="false",
            description=(
                "Master switch for Phase 3 A1 — wires "
                "orchestrator terminal-state path into the "
                "canonical ExecutionMonitor singleton + "
                "bounded JSONL ledger via §33.4. Default-"
                "FALSE per §33.1; when off, the bridge is "
                "a no-op (zero behavior change at the "
                "orchestrator call sites)."
            ),
            category="Observability",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "execution_monitor_bridge.py"
            ),
            example=(
                "JARVIS_EXECUTION_MONITOR_BRIDGE_ENABLED="
                "true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionMonitorBridge] master-flag seeding "
            "failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name=(
                "JARVIS_EXECUTION_MONITOR_BRIDGE_"
                "LEDGER_PATH"
            ),
            type_="path",
            default=str(
                Path(".jarvis") / _DEFAULT_LEDGER_FILENAME
            ),
            description=(
                "JSONL ledger path for Phase 3 A1 "
                "terminal-outcome records (§33.4 flock'd "
                "persistence). One row per terminal op."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "execution_monitor_bridge.py"
            ),
            example=(
                "JARVIS_EXECUTION_MONITOR_BRIDGE_"
                "LEDGER_PATH=.jarvis/"
                "execution_monitor_bridge.jsonl"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionMonitorBridge] ledger-path seeding "
            "failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``execution_monitor_bridge_master_default_false``
      2. ``execution_monitor_bridge_authority_asymmetry``
      3. ``execution_monitor_bridge_composes_canonical_monitor``
         — ``_propagate_to_canonical_monitor`` MUST lazy-
         import :func:`get_default_monitor` from
         ``autonomy.execution_monitor`` (single source of
         truth; no parallel monitor instance).
      4. ``execution_monitor_bridge_composes_canonical_jsonl``
         — MUST use canonical flock primitives; no raw
         ``open(..., "a")`` for the ledger.
      5. ``execution_monitor_bridge_status_table_canonical``
         — :data:`_TERMINAL_REASON_TO_STATUS` values MUST
         be valid :class:`ExecutionStatus` member names
         (canonical 9-value taxonomy; no parallel taxonomy).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "execution_monitor_bridge.py"
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
            violations.append(
                "master_enabled() missing"
            )
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
                    "execution_monitor_bridge" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"execution_monitor_bridge.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"execution_monitor_bridge.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_composes_canonical_monitor(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func: Optional[ast.FunctionDef] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name
                == "_propagate_to_canonical_monitor"
            ):
                target_func = node
                break
        if target_func is None:
            violations.append(
                "_propagate_to_canonical_monitor missing"
            )
            return tuple(violations)
        composes = False
        for sub in ast.walk(target_func):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if "autonomy.execution_monitor" in module:
                    names = {n.name for n in sub.names}
                    if "get_default_monitor" in names:
                        composes = True
                        break
        if not composes:
            violations.append(
                "composes-canonical-monitor: MUST lazy-"
                "import get_default_monitor from "
                "autonomy.execution_monitor (single source "
                "of truth)"
            )
        return tuple(violations)

    def _validate_composes_canonical_jsonl(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        # Must reference the canonical primitives in source.
        for fn_name in (
            "flock_append_line",
            "flock_critical_section",
        ):
            if fn_name not in source:
                violations.append(
                    f"composes-canonical-jsonl: source "
                    f"MUST use cross_process_jsonl."
                    f"{fn_name} (§33.4 pattern)"
                )
        # Must NOT use raw open(..., "a") for the ledger.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id == "open"
            ):
                mode_arg: Optional[str] = None
                if len(node.args) >= 2 and isinstance(
                    node.args[1], ast.Constant,
                ):
                    mode_arg = str(node.args[1].value)
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(
                        kw.value, ast.Constant,
                    ):
                        mode_arg = str(kw.value.value)
                if mode_arg and mode_arg.startswith("a"):
                    violations.append(
                        f"composes-canonical-jsonl: raw "
                        f"open(..., {mode_arg!r}) is "
                        f"forbidden — use flock_append_line"
                        f" (line {node.lineno})"
                    )
        return tuple(violations)

    def _validate_status_table_canonical(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Every value in :data:`_TERMINAL_REASON_TO_STATUS`
        MUST be a valid :class:`ExecutionStatus` member name
        (canonical 9-value taxonomy)."""
        violations: list = []
        # Canonical taxonomy — bytes-pinned at AST scan time
        # (mirrors the enum in autonomy.execution_monitor).
        canonical = {
            "PENDING", "RUNNING", "COMPLETED", "FAILED",
            "TIMEOUT", "MEMORY_EXCEEDED", "DEPTH_EXCEEDED",
            "ITERATION_EXCEEDED", "SECURITY_VIOLATION",
        }
        # Walk both Assign + AnnAssign — operator may use
        # ``_TERMINAL_REASON_TO_STATUS = {...}`` OR
        # ``_TERMINAL_REASON_TO_STATUS: Mapping[str, str] =
        # {...}``. AST shape differs.
        table_value: Optional[ast.expr] = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id
                        == "_TERMINAL_REASON_TO_STATUS"
                    ):
                        table_value = node.value
                        break
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id
                    == "_TERMINAL_REASON_TO_STATUS"
                ):
                    table_value = node.value
            if table_value is not None:
                break
        if table_value is None:
            violations.append(
                "_TERMINAL_REASON_TO_STATUS table missing"
            )
            return tuple(violations)
        if not isinstance(table_value, ast.Dict):
            violations.append(
                "status-table-canonical: "
                "_TERMINAL_REASON_TO_STATUS MUST be a "
                "dict literal"
            )
            return tuple(violations)
        for v in table_value.values:
            if not isinstance(v, ast.Constant):
                violations.append(
                    "status-table-canonical: values MUST "
                    "be string literals"
                )
                continue
            if (
                not isinstance(v.value, str)
                or v.value not in canonical
            ):
                violations.append(
                    f"status-table-canonical: value "
                    f"{v.value!r} is not in the canonical "
                    f"ExecutionStatus 9-value taxonomy"
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "execution_monitor_bridge_"
                "master_default_false"
            ),
            target_file=target,
            description=(
                "Phase 3 A1 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_monitor_bridge_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Phase 3 A1 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_monitor_bridge_"
                "composes_canonical_monitor"
            ),
            target_file=target,
            description=(
                "Phase 3 A1 — bridge composes the canonical "
                "ExecutionMonitor singleton via lazy-import "
                "(no parallel monitor instance)."
            ),
            validate=_validate_composes_canonical_monitor,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_monitor_bridge_"
                "composes_canonical_jsonl"
            ),
            target_file=target,
            description=(
                "Phase 3 A1 — §33.4 Per-Cluster Flock'd "
                "JSONL: persistence composes "
                "flock_append_line + flock_critical_section;"
                " no raw open(..., 'a') for ledger."
            ),
            validate=_validate_composes_canonical_jsonl,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_monitor_bridge_"
                "status_table_canonical"
            ),
            target_file=target,
            description=(
                "Phase 3 A1 — terminal-reason mapping "
                "values MUST be valid ExecutionStatus enum "
                "names (canonical 9-value taxonomy; no "
                "parallel taxonomy)."
            ),
            validate=_validate_status_table_canonical,
        ),
    ]


__all__ = [
    "EXECUTION_MONITOR_BRIDGE_SCHEMA_VERSION",
    "TerminalOutcomeRecord",
    "get_terminal_status_name",
    "ledger_path",
    "master_enabled",
    "read_recent_records",
    "record_terminal_outcome",
    "register_flags",
    "register_shipped_invariants",
]
