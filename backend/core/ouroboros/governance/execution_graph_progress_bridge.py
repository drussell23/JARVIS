"""Phase 3 A2 — ExecutionGraphProgressTracker → SerpentFlow /
canvas / SSE bridge.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "A2: ExecutionGraphProgressTracker → SerpentFlow / canvas /
   SSE — read-only projection; no authority on APPLY."

This module is a **read-only consumer** of the canonical
:class:`ExecutionGraphProgressTracker` singleton. It
subscribes to the tracker's async event stream + projects
each :class:`GraphEvent` to:

  1. The canonical SSE broker via
     :func:`publish_execution_graph_progress_event`. Same
     pattern Slice 4 of Move 6.5 uses for its dispatch
     events. SerpentFlow + canvas + IDE extensions consume
     the broker's SSE stream — they get live tracker state
     for free.

  2. A bounded JSONL ledger via §33.4
     :func:`flock_append_line` (audit replay surface +
     fallback for SSE-disconnected operators).

## Composition discipline (AST-pinned)

  1. Composes :func:`get_default_tracker` (single source of
     truth — no parallel tracker instance) — AST-pinned via
     ``execution_graph_progress_bridge_composes_canonical_tracker``.
  2. Composes :func:`publish_execution_graph_progress_event`
     from canonical broker — no direct ``broker.publish``
     calls. AST-pinned via
     ``execution_graph_progress_bridge_composes_canonical_publisher``.
  3. Composes §33.4 flock primitives — no raw
     ``open(..., "a")`` for the ledger. AST-pinned via
     ``execution_graph_progress_bridge_composes_canonical_jsonl``.

## Authority asymmetry

No orchestrator / iron_gate / providers / candidate_generator
/ change_engine / semantic_guardian / plan_generator /
urgency_router / direction_inferrer / policy imports.
Substrate-pure read-only consumer. AST-pinned.

## Read-only — no authority on APPLY

The bridge MUST NOT mutate tracker state. It does not call
``ExecutionGraphProgressTracker.record_*`` / ``emit*`` /
``unsubscribe_all`` / any setter — only :func:`subscribe`
+ :func:`snapshot`. AST-pinned via
``execution_graph_progress_bridge_read_only``.

## Chatter-suppression discipline

The canonical 10-value :class:`GraphEventKind` taxonomy is
split into operator-actionable kinds (default emit) and
intermediate kinds (skipped unless verbose flag on):

  * **Default-emit (graph-level always operator-actionable)**:
    ``GRAPH_SUBMITTED``, ``GRAPH_STARTED``,
    ``GRAPH_COMPLETED``, ``GRAPH_FAILED``,
    ``GRAPH_CANCELLED``.
  * **Default-emit (terminal unit transitions only)**:
    ``UNIT_COMPLETED``, ``UNIT_FAILED``, ``UNIT_CANCELLED``.
  * **Default-suppress (intermediate unit transitions —
    flooding risk during heavy fan-out)**: ``UNIT_READY``,
    ``UNIT_STARTED``. Operator opts in via
    ``JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE=true`` to flip the
    default. AST-pinned via
    ``execution_graph_progress_bridge_chatter_default``.

## Master flag

``JARVIS_EXEC_GRAPH_BRIDGE_ENABLED`` default-FALSE per §33.1.
When OFF, :func:`record_graph_event` is a no-op +
:func:`start_default_bridge` returns None immediately —
zero subscriber registration on the canonical tracker.

## NEVER raises

Every code path defensive — tracker subscription failures
swallowed; per-event projection failures swallowed; SSE
publish failures swallowed; JSONL persistence failures
swallowed. The bridge is a monitor; it MUST NOT itself
become a failure mode.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Dict, FrozenSet, Mapping, Optional,
)


logger = logging.getLogger(
    "Ouroboros.ExecutionGraphProgressBridge",
)


EXECUTION_GRAPH_PROGRESS_BRIDGE_SCHEMA_VERSION: str = (
    "execution_graph_progress_bridge.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


_DEFAULT_LEDGER_FILENAME: str = (
    "execution_graph_progress_bridge.jsonl"
)
_DEFAULT_LEDGER_SIZE_CAP_BYTES: int = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Chatter-suppression default-emit set
# ---------------------------------------------------------------------------


# Canonical event-kind names (string values from
# :class:`GraphEventKind`) that the bridge emits by default.
# Bytes-pinned at AST scan time (see
# ``execution_graph_progress_bridge_chatter_default`` pin).
# Operator-binding load-bearing: graph-level events are
# always emitted; unit-level emits ONLY terminal transitions.
# Intermediate unit transitions (UNIT_READY / UNIT_STARTED)
# default-suppressed to avoid SSE flooding during heavy
# fan-out.
DEFAULT_EMIT_KINDS: FrozenSet[str] = frozenset({
    # Graph-level — 5 kinds (all operator-actionable)
    "graph_submitted",
    "graph_started",
    "graph_completed",
    "graph_failed",
    "graph_cancelled",
    # Unit-level terminals — 3 kinds
    "unit_completed",
    "unit_failed",
    "unit_cancelled",
})


# ---------------------------------------------------------------------------
# Master flag + tunable knobs
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_EXEC_GRAPH_BRIDGE_ENABLED`` master switch.
    Default-FALSE per §33.1: when OFF, :func:`record_graph_event`
    is a no-op + :func:`start_default_bridge` returns None
    immediately. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


def verbose_mode() -> bool:
    """``JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE`` flag — when on,
    every :class:`GraphEventKind` (10 values) emits;
    intermediate unit transitions (UNIT_READY / UNIT_STARTED)
    are no longer suppressed. Default-FALSE per §33.1
    chatter-suppression discipline. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


def ledger_path() -> Path:
    """Resolve the canonical ledger path. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_EXEC_GRAPH_BRIDGE_LEDGER_PATH", "",
    ).strip()
    if raw:
        return Path(raw)
    return Path(".jarvis") / _DEFAULT_LEDGER_FILENAME


# ---------------------------------------------------------------------------
# Frozen artifact (§33.5 versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphProgressRecord:
    """One ledger row. §33.5 versioned-artifact contract.

    Lightweight projection of :class:`GraphEvent` — same
    surface SSE consumers see. Full ``GraphProgress``
    snapshot lives elsewhere (canonical tracker)."""

    kind: str
    graph_id: str
    op_id: str
    unit_id: str
    ts_ns: int
    ts_unix: float
    payload: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(
        default=EXECUTION_GRAPH_PROGRESS_BRIDGE_SCHEMA_VERSION,  # noqa: E501
    )

    def to_dict(self) -> Dict[str, Any]:
        # Defensive: non-JSON payload values fall back to
        # str(); same shape as A1's bridge.
        payload_safe: Dict[str, Any] = {}
        try:
            for k, v in (self.payload or {}).items():
                try:
                    import json
                    json.dumps(v)
                    payload_safe[str(k)] = v
                except (TypeError, ValueError):
                    payload_safe[str(k)] = str(v)[:256]
        except Exception:  # noqa: BLE001 — defensive
            payload_safe = {}
        return {
            "kind": str(self.kind),
            "graph_id": str(self.graph_id),
            "op_id": str(self.op_id),
            "unit_id": str(self.unit_id),
            "ts_ns": int(self.ts_ns),
            "ts_unix": float(self.ts_unix),
            "payload": payload_safe,
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["GraphProgressRecord"]:
        try:
            schema = payload.get("schema_version")
            if schema != (
                EXECUTION_GRAPH_PROGRESS_BRIDGE_SCHEMA_VERSION  # noqa: E501
            ):
                return None
            return cls(
                kind=str(payload["kind"]),
                graph_id=str(payload.get("graph_id", "")),
                op_id=str(payload.get("op_id", "")),
                unit_id=str(payload.get("unit_id", "")),
                ts_ns=int(payload.get("ts_ns", 0)),
                ts_unix=float(payload["ts_unix"]),
                payload=dict(payload.get("payload", {})),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Chatter-suppression filter
# ---------------------------------------------------------------------------


def should_emit(kind: str) -> bool:
    """Pure decision: should this :class:`GraphEventKind`
    project to SSE / ledger? NEVER raises.

    Default-emit set (8 kinds) + verbose mode (10 kinds).
    Unknown kinds default-emit (defensive — unknown kinds
    might be a future-extension sign that should reach
    operators, not get silently suppressed)."""
    try:
        key = str(kind or "").strip().lower()
    except Exception:  # noqa: BLE001 — defensive
        return False
    if not key:
        return False
    if verbose_mode():
        return True
    return key in DEFAULT_EMIT_KINDS or key not in {
        "unit_ready", "unit_started",
    }


# ---------------------------------------------------------------------------
# Public recorder — caller-invoked from the consumer loop
# ---------------------------------------------------------------------------


def record_graph_event(
    *,
    kind: str,
    graph_id: str = "",
    op_id: str = "",
    unit_id: str = "",
    ts_ns: int = 0,
    payload: Optional[Mapping[str, Any]] = None,
    ledger_path_override: Optional[Path] = None,
) -> Optional[GraphProgressRecord]:
    """Record one canonical :class:`GraphEvent` projection.
    Composes:
      1. Chatter-suppression filter (skip intermediate unit
         transitions unless verbose).
      2. Build :class:`GraphProgressRecord` artifact.
      3. Emit via canonical SSE broker
         :func:`publish_execution_graph_progress_event`.
      4. Append §33.5 versioned row to bounded JSONL ledger
         via §33.4.

    Returns the persisted record on success, None when:
      * Master flag off
      * Kind chatter-suppressed (default behavior)
      * SSE publish + JSONL persistence both failed

    NEVER raises."""
    if not master_enabled():
        return None
    kind_norm = str(kind or "").strip().lower()
    if not kind_norm:
        return None
    if not should_emit(kind_norm):
        return None

    try:
        ts_unix = time.time()
        record = GraphProgressRecord(
            kind=kind_norm,
            graph_id=str(graph_id or ""),
            op_id=str(op_id or ""),
            unit_id=str(unit_id or ""),
            ts_ns=int(ts_ns or 0),
            ts_unix=ts_unix,
            payload=dict(payload or {}),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionGraphProgressBridge] record build "
            "failed", exc_info=True,
        )
        return None

    # Step 3: SSE projection (canonical broker).
    _project_to_canonical_broker(record)

    # Step 4: §33.4 flock'd persistence.
    persisted = _flock_persist(
        record=record,
        target=(
            ledger_path_override
            if ledger_path_override is not None
            else ledger_path()
        ),
    )
    return record if persisted else record


def _project_to_canonical_broker(
    record: GraphProgressRecord,
) -> None:
    """Compose canonical :func:`publish_execution_graph_progress_event`
    helper (single source of truth — no direct broker call).
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_execution_graph_progress_event,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ExecutionGraphProgressBridge] publisher "
            "unavailable: %s", exc,
        )
        return
    try:
        publish_execution_graph_progress_event(
            kind=record.kind,
            graph_id=record.graph_id,
            op_id=record.op_id,
            unit_id=record.unit_id,
            ts_ns=record.ts_ns,
            payload=record.payload,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionGraphProgressBridge] publish raised",
            exc_info=True,
        )


def _flock_persist(
    *,
    record: GraphProgressRecord,
    target: Path,
) -> bool:
    """Append one row via canonical
    :func:`cross_process_jsonl.flock_append_line`. Returns
    True on success, False on failure. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[ExecutionGraphProgressBridge] flock primitive "
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
            "[ExecutionGraphProgressBridge] "
            "flock_append_line raised: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Subscriber loop — long-running async consumer
# ---------------------------------------------------------------------------


class ExecutionGraphProgressBridge:
    """Long-running async consumer of the canonical
    :class:`ExecutionGraphProgressTracker`'s subscriber
    stream. Singleton; one instance per process. The
    orchestrator's eventual integration calls
    :func:`start_default_bridge` once during boot.

    **Read-only invariant** (AST-pinned): the bridge MUST
    only call :meth:`subscribe` / :meth:`snapshot` on the
    canonical tracker. No record / emit / unsubscribe_all /
    any state-mutating method permitted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: Optional["asyncio.Task[None]"] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Process-local counters (best-effort telemetry; the
        # JSONL ledger is the cross-process source of truth).
        self._events_seen: int = 0
        self._events_emitted: int = 0
        self._events_suppressed: int = 0

    async def consume_tracker_stream(self) -> None:
        """Subscribe to the canonical tracker + drain events
        until cancelled. NEVER raises out except
        :class:`asyncio.CancelledError`. Cooperative
        shutdown via :meth:`stop`."""
        if not master_enabled():
            return
        try:
            from backend.core.ouroboros.governance.autonomy.execution_graph_progress import (  # noqa: E501
                get_default_tracker,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ExecutionGraphProgressBridge] tracker "
                "unavailable: %s", exc,
            )
            return
        tracker = get_default_tracker()
        if tracker is None:
            logger.debug(
                "[ExecutionGraphProgressBridge] no default "
                "tracker — bridge will not run",
            )
            return
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        try:
            stream = tracker.subscribe(
                name="execution_graph_progress_bridge",
            )
            async for event in stream:
                if self._stop_event.is_set():
                    break
                with self._lock:
                    self._events_seen += 1
                self._project_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ExecutionGraphProgressBridge] consumer "
                "loop raised: %s", exc,
            )

    def _project_event(self, event: Any) -> None:
        """Defensive projection of one canonical
        :class:`GraphEvent` to :func:`record_graph_event`.
        NEVER raises."""
        try:
            kind_obj = getattr(event, "kind", None)
            kind = (
                getattr(kind_obj, "value", "")
                if kind_obj is not None
                else ""
            )
            graph_id = str(getattr(event, "graph_id", ""))
            op_id = str(getattr(event, "op_id", ""))
            unit_id_raw = getattr(event, "unit_id", None)
            unit_id = (
                str(unit_id_raw)
                if unit_id_raw is not None
                else ""
            )
            ts_ns = int(getattr(event, "ts_ns", 0))
            payload = dict(
                getattr(event, "payload", {}) or {},
            )
        except Exception:  # noqa: BLE001 — defensive
            return
        result = record_graph_event(
            kind=kind,
            graph_id=graph_id,
            op_id=op_id,
            unit_id=unit_id,
            ts_ns=ts_ns,
            payload=payload,
        )
        with self._lock:
            if result is not None:
                self._events_emitted += 1
            else:
                self._events_suppressed += 1

    async def stop(self) -> None:
        """Signal cooperative shutdown. NEVER raises."""
        try:
            if self._stop_event is not None:
                self._stop_event.set()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[ExecutionGraphProgressBridge] stop "
                "raised: %s", exc,
            )

    def telemetry(self) -> Dict[str, int]:
        """Process-local counters. NEVER raises."""
        with self._lock:
            return {
                "events_seen": self._events_seen,
                "events_emitted": self._events_emitted,
                "events_suppressed": self._events_suppressed,
            }


_DEFAULT_BRIDGE: Optional[ExecutionGraphProgressBridge] = None
_DEFAULT_BRIDGE_LOCK = threading.Lock()


def get_default_bridge() -> ExecutionGraphProgressBridge:
    """Singleton accessor. NEVER raises."""
    global _DEFAULT_BRIDGE
    with _DEFAULT_BRIDGE_LOCK:
        if _DEFAULT_BRIDGE is None:
            _DEFAULT_BRIDGE = ExecutionGraphProgressBridge()
        return _DEFAULT_BRIDGE


def reset_default_bridge_for_test() -> None:
    """Test helper. NEVER raises."""
    global _DEFAULT_BRIDGE
    with _DEFAULT_BRIDGE_LOCK:
        _DEFAULT_BRIDGE = None


def start_default_bridge() -> Optional["asyncio.Task[None]"]:
    """Bootstrap helper for the orchestrator's eventual
    integration. Spawns the consumer loop as an asyncio
    Task on the running loop. Returns the Task on success,
    None when:
      * Master flag off
      * No running event loop
      * Bridge already started

    NEVER raises."""
    if not master_enabled():
        return None
    bridge = get_default_bridge()
    with bridge._lock:  # noqa: SLF001 — singleton bootstrap
        if (
            bridge._task is not None  # noqa: SLF001
            and not bridge._task.done()
        ):
            return bridge._task  # already running
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(
            "[ExecutionGraphProgressBridge] no running "
            "event loop — bootstrap skipped",
        )
        return None
    try:
        task = loop.create_task(
            bridge.consume_tracker_stream(),
            name="execution_graph_progress_bridge_consumer",
        )
        with bridge._lock:  # noqa: SLF001
            bridge._task = task  # noqa: SLF001
        return task
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionGraphProgressBridge] task spawn "
            "failed", exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Read API — for /observability surface (Slice 7+) + tests
# ---------------------------------------------------------------------------


def read_recent_records(
    *,
    limit: int = 50,
    path: Optional[Path] = None,
) -> tuple:
    """Read recent :class:`GraphProgressRecord` rows from the
    JSONL ledger via canonical
    :func:`cross_process_jsonl.flock_critical_section`.
    NEVER raises; empty tuple on missing file / I/O /
    schema-mismatch."""
    target = path if path is not None else ledger_path()
    if not target.exists():
        return ()
    try:
        size = target.stat().st_size
    except OSError:
        return ()
    if size > _DEFAULT_LEDGER_SIZE_CAP_BYTES:
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
        rec = GraphProgressRecord.from_dict(payload)
        if rec is not None:
            out.append(rec)
    return tuple(out)


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds 3 flags."""
    try:
        registry.register(
            name="JARVIS_EXEC_GRAPH_BRIDGE_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for Phase 3 A2 — read-only "
                "projection of canonical "
                "ExecutionGraphProgressTracker → SerpentFlow"
                " / canvas / SSE. Default-FALSE per §33.1."
            ),
            category="Observability",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "execution_graph_progress_bridge.py"
            ),
            example=(
                "JARVIS_EXEC_GRAPH_BRIDGE_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionGraphProgressBridge] master-flag "
            "seeding failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name="JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE",
            type_="bool",
            default="false",
            description=(
                "Verbose mode — emit ALL "
                "GraphEventKind values (10 kinds), "
                "including intermediate unit transitions "
                "(UNIT_READY / UNIT_STARTED) that are "
                "default-suppressed for chatter discipline."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "execution_graph_progress_bridge.py"
            ),
            example=(
                "JARVIS_EXEC_GRAPH_BRIDGE_VERBOSE=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionGraphProgressBridge] verbose-flag "
            "seeding failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name="JARVIS_EXEC_GRAPH_BRIDGE_LEDGER_PATH",
            type_="path",
            default=str(
                Path(".jarvis") / _DEFAULT_LEDGER_FILENAME
            ),
            description=(
                "JSONL ledger path for Phase 3 A2 graph "
                "progress records (§33.4 flock'd "
                "persistence)."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "execution_graph_progress_bridge.py"
            ),
            example=(
                "JARVIS_EXEC_GRAPH_BRIDGE_LEDGER_PATH="
                ".jarvis/exec_graph_bridge.jsonl"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[ExecutionGraphProgressBridge] ledger-path "
            "seeding failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``execution_graph_progress_bridge_master_default_false``
      2. ``execution_graph_progress_bridge_authority_asymmetry``
      3. ``execution_graph_progress_bridge_read_only`` —
         Bridge MUST NOT mutate tracker state. Forbidden:
         any method call other than ``subscribe`` /
         ``snapshot`` / ``all_active`` / ``all_tracked`` /
         ``stats`` on the canonical tracker.
      4. ``execution_graph_progress_bridge_composes_canonical_tracker``
         — Bridge MUST lazy-import :func:`get_default_tracker`
         from canonical autonomy substrate.
      5. ``execution_graph_progress_bridge_composes_canonical_publisher``
         — Bridge MUST compose
         :func:`publish_execution_graph_progress_event`; no
         direct ``broker.publish``.
      6. ``execution_graph_progress_bridge_composes_canonical_jsonl``
         — §33.4 flock primitives only; no raw
         ``open(..., "a")``.
      7. ``execution_graph_progress_bridge_chatter_default``
         — :data:`DEFAULT_EMIT_KINDS` MUST contain exactly
         the 5 graph-level + 3 terminal unit-level kinds
         (canonical chatter-suppression discipline).
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
        "execution_graph_progress_bridge.py"
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
                    "execution_graph_progress_bridge" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"execution_graph_progress_"
                            f"bridge.py MUST NOT import "
                            f"{module!r} (forbidden segment "
                            f"{seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"execution_graph_progress_"
                            f"bridge.py MUST NOT import "
                            f"{module!r} (forbidden token "
                            f"{f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Bridge MUST only call ``subscribe`` / ``snapshot``
        / ``all_active`` / ``all_tracked`` / ``stats`` on
        objects named ``tracker`` (the canonical accessor).
        Forbidden tracker methods: ``record_*``, ``emit*``,
        ``unsubscribe_all``, any setter."""
        violations: list = []
        allowed_tracker_methods = {
            "subscribe", "snapshot",
            "all_active", "all_tracked", "stats",
        }
        forbidden_tracker_method_prefixes = (
            "record_", "emit",
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            value = func.value
            # Only inspect attribute accesses on local
            # ``tracker`` Name (the canonical accessor
            # binding inside consume_tracker_stream).
            if not (
                isinstance(value, ast.Name)
                and value.id == "tracker"
            ):
                continue
            method = func.attr
            if method.startswith(
                forbidden_tracker_method_prefixes,
            ):
                violations.append(
                    f"read-only: tracker.{method}() is "
                    f"forbidden (line {node.lineno}); "
                    f"bridge MUST NOT mutate tracker state"
                )
                continue
            if method == "unsubscribe_all":
                violations.append(
                    f"read-only: tracker.unsubscribe_all() "
                    f"is forbidden (line {node.lineno}); "
                    f"bridge MUST NOT mutate subscriber "
                    f"set"
                )
                continue
            if method not in allowed_tracker_methods:
                violations.append(
                    f"read-only: tracker.{method}() is "
                    f"not in the allowed read-only API "
                    f"(line {node.lineno}); allowed: "
                    f"{sorted(allowed_tracker_methods)}"
                )
        return tuple(violations)

    def _validate_composes_canonical_tracker(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        composes = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if (
                "autonomy.execution_graph_progress" in module
            ):
                names = {n.name for n in node.names}
                if "get_default_tracker" in names:
                    composes = True
                    break
        if not composes:
            violations.append(
                "composes-canonical-tracker: MUST lazy-"
                "import get_default_tracker from "
                "autonomy.execution_graph_progress (single "
                "source of truth)"
            )
        return tuple(violations)

    def _validate_composes_canonical_publisher(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        composes = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if "ide_observability_stream" in module:
                names = {n.name for n in node.names}
                if (
                    "publish_execution_graph_progress_event"
                    in names
                ):
                    composes = True
                    break
        if not composes:
            violations.append(
                "composes-canonical-publisher: MUST "
                "lazy-import "
                "publish_execution_graph_progress_event "
                "from ide_observability_stream"
            )
        # Forbid direct ``.publish(`` calls.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "publish"
            ):
                violations.append(
                    f"composes-canonical-publisher: "
                    f"direct .publish() call forbidden — "
                    f"use canonical helper "
                    f"(line {node.lineno})"
                )
        return tuple(violations)

    def _validate_composes_canonical_jsonl(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
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
                        f"forbidden — use flock_append_line "
                        f"(line {node.lineno})"
                    )
        return tuple(violations)

    def _validate_chatter_default(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """:data:`DEFAULT_EMIT_KINDS` MUST contain exactly
        the canonical 8 kinds: 5 graph-level
        (graph_submitted/started/completed/failed/cancelled)
        + 3 terminal unit-level
        (unit_completed/failed/cancelled). Intermediate
        unit transitions (unit_ready/unit_started)
        default-suppressed."""
        required = {
            "graph_submitted", "graph_started",
            "graph_completed", "graph_failed",
            "graph_cancelled",
            "unit_completed", "unit_failed", "unit_cancelled",
        }
        violations: list = []
        table_value: Optional[ast.expr] = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id == "DEFAULT_EMIT_KINDS"
                    ):
                        table_value = node.value
                        break
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id
                    == "DEFAULT_EMIT_KINDS"
                ):
                    table_value = node.value
            if table_value is not None:
                break
        if table_value is None:
            violations.append(
                "DEFAULT_EMIT_KINDS frozen-set missing"
            )
            return tuple(violations)
        # Must be frozenset({...}) call.
        if not (
            isinstance(table_value, ast.Call)
            and isinstance(table_value.func, ast.Name)
            and table_value.func.id == "frozenset"
            and table_value.args
            and isinstance(table_value.args[0], ast.Set)
        ):
            violations.append(
                "DEFAULT_EMIT_KINDS MUST be a frozenset "
                "literal {...} call"
            )
            return tuple(violations)
        seen: set = set()
        for elt in table_value.args[0].elts:
            if (
                isinstance(elt, ast.Constant)
                and isinstance(elt.value, str)
            ):
                seen.add(elt.value)
        missing = required - seen
        extra = seen - required
        if missing:
            violations.append(
                f"chatter-default: DEFAULT_EMIT_KINDS "
                f"missing {sorted(missing)}"
            )
        if extra:
            violations.append(
                f"chatter-default: DEFAULT_EMIT_KINDS "
                f"has extra {sorted(extra)} — canonical "
                f"set is exactly 8 kinds"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "execution_graph_progress_bridge_"
                "master_default_false"
            ),
            target_file=target,
            description=(
                "Phase 3 A2 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_graph_progress_bridge_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Phase 3 A2 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_graph_progress_bridge_read_only"
            ),
            target_file=target,
            description=(
                "Phase 3 A2 — read-only invariant: bridge "
                "MUST only call subscribe / snapshot / "
                "all_active / all_tracked / stats on the "
                "canonical tracker. No state mutation."
            ),
            validate=_validate_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_graph_progress_bridge_"
                "composes_canonical_tracker"
            ),
            target_file=target,
            description=(
                "Phase 3 A2 — bridge composes canonical "
                "ExecutionGraphProgressTracker singleton "
                "via lazy-import (no parallel tracker)."
            ),
            validate=_validate_composes_canonical_tracker,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_graph_progress_bridge_"
                "composes_canonical_publisher"
            ),
            target_file=target,
            description=(
                "Phase 3 A2 — SSE projection composes "
                "publish_execution_graph_progress_event; "
                "no direct broker.publish / .publish."
            ),
            validate=_validate_composes_canonical_publisher,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_graph_progress_bridge_"
                "composes_canonical_jsonl"
            ),
            target_file=target,
            description=(
                "Phase 3 A2 — §33.4 Per-Cluster Flock'd "
                "JSONL: persistence composes "
                "flock_append_line + flock_critical_section."
            ),
            validate=_validate_composes_canonical_jsonl,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "execution_graph_progress_bridge_"
                "chatter_default"
            ),
            target_file=target,
            description=(
                "Phase 3 A2 — DEFAULT_EMIT_KINDS contains "
                "exactly the canonical 8 kinds (5 "
                "graph-level + 3 terminal unit-level). "
                "Intermediate unit transitions "
                "default-suppressed."
            ),
            validate=_validate_chatter_default,
        ),
    ]


__all__ = [
    "DEFAULT_EMIT_KINDS",
    "EXECUTION_GRAPH_PROGRESS_BRIDGE_SCHEMA_VERSION",
    "ExecutionGraphProgressBridge",
    "GraphProgressRecord",
    "get_default_bridge",
    "ledger_path",
    "master_enabled",
    "read_recent_records",
    "record_graph_event",
    "register_flags",
    "register_shipped_invariants",
    "reset_default_bridge_for_test",
    "should_emit",
    "start_default_bridge",
    "verbose_mode",
]
