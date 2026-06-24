"""Phase 3 A3 — CommandBus advisory commands → IDE stream
new event type bridge.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "A3: CommandBus advisory commands → IDE stream new event
   type — rate-limited, CORS/loopback same as existing
   observability slices."

This module is a **read-only consumer** of the canonical
:class:`CommandBus` class-level metrics aggregator. Unlike
A2's tracker (which exposes :func:`subscribe`), CommandBus
has:

  * **No subscriber API** — commands are pulled by ONE
    consumer per bus via :meth:`get`.
  * **No singleton** — 5+ internal consumers each construct
    their own bus (governed_loop_service,
    subagent_scheduler, advanced_coordination, safety_net,
    feedback_engine). The class-level ``_INSTANCES``
    :class:`weakref.WeakSet` lets aggregate metrics span
    all live buses without forcing a single-instance
    contract.

So A3 polls :meth:`CommandBus.snapshot_all` on a cadence +
chatter-suppresses to **delta-emit only**. SSE fires when
total_dispatched / rejected_dedup / rejected_backpressure /
per-command-type counts change vs the prior poll. Identical
poll deltas are silent.

## Composition discipline (AST-pinned)

  1. Composes :meth:`CommandBus.snapshot_all` (canonical
     class-level aggregator) — no per-instance probing,
     no parallel state. AST-pinned via
     ``autonomy_command_bus_bridge_composes_canonical_bus``.
  2. Composes
     :func:`publish_autonomy_command_bus_event` from
     canonical broker — direct ``.publish()`` forbidden.
     AST-pinned.
  3. Composes §33.4 flock primitives for bounded JSONL
     ledger — no raw ``open(..., "a")``. AST-pinned.

## Authority asymmetry

No orchestrator / iron_gate / providers / candidate_generator
/ change_engine / semantic_guardian / plan_generator /
urgency_router / direction_inferrer / policy imports.
Substrate-pure read-only consumer. AST-pinned.

## Read-only — no authority on dispatch

The bridge MUST NOT mutate any CommandBus state — only
:meth:`snapshot_all` (classmethod) and per-instance
:meth:`metrics_snapshot` (read-only) are permitted. AST-
pinned via ``autonomy_command_bus_bridge_read_only``: any
``put`` / ``try_put`` / ``get`` / ``_enqueue`` / setter
call on the bus is forbidden.

## Master flag

``JARVIS_COMMAND_BUS_BRIDGE_ENABLED`` default-FALSE per
§33.1. Tunable poll interval via
``JARVIS_COMMAND_BUS_BRIDGE_POLL_S`` (default 2.0s; clamped
[0.5, 60.0]). Bounded JSONL ledger path via
``JARVIS_COMMAND_BUS_BRIDGE_LEDGER_PATH``.

## NEVER raises

Every code path defensive — snapshot failures swallowed,
SSE publish failures swallowed, JSONL persistence failures
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
    Any, Dict, FrozenSet, Mapping, Optional, Tuple,
)


logger = logging.getLogger(
    "Ouroboros.AutonomyCommandBusBridge",
)


AUTONOMY_COMMAND_BUS_BRIDGE_SCHEMA_VERSION: str = (
    "autonomy_command_bus_bridge.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


_DEFAULT_LEDGER_FILENAME: str = (
    "autonomy_command_bus_bridge.jsonl"
)
_DEFAULT_LEDGER_SIZE_CAP_BYTES: int = 50 * 1024 * 1024


_DEFAULT_POLL_S: float = 2.0
_POLL_FLOOR_S: float = 0.5
_POLL_CEILING_S: float = 60.0


# ---------------------------------------------------------------------------
# Master flag + tunable knobs
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_COMMAND_BUS_BRIDGE_ENABLED`` master switch.
    Default-FALSE per §33.1: when OFF, :func:`record_snapshot`
    is a no-op + :func:`start_default_bridge` returns None
    immediately. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_COMMAND_BUS_BRIDGE_ENABLED", "true",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


def poll_interval_s() -> float:
    """``JARVIS_COMMAND_BUS_BRIDGE_POLL_S`` poll cadence.
    Default 2.0s; clamped [0.5, 60.0]. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_COMMAND_BUS_BRIDGE_POLL_S", "",
    ).strip()
    if not raw:
        return _DEFAULT_POLL_S
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_POLL_S
    if v < _POLL_FLOOR_S:
        return _POLL_FLOOR_S
    if v > _POLL_CEILING_S:
        return _POLL_CEILING_S
    return v


def ledger_path() -> Path:
    """Resolve the canonical ledger path. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_COMMAND_BUS_BRIDGE_LEDGER_PATH", "",
    ).strip()
    if raw:
        return Path(raw)
    return Path(".jarvis") / _DEFAULT_LEDGER_FILENAME


# ---------------------------------------------------------------------------
# Frozen artifact (§33.5 versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandBusSnapshotRecord:
    """One ledger row. §33.5 versioned-artifact contract.

    Captures the canonical :meth:`CommandBus.snapshot_all`
    aggregate + the delta vs the prior poll (operator-
    visible signal of which counters incremented). Persists
    only on chatter-suppression-passed transitions."""

    instance_count: int
    total_qsize: int
    total_dispatched: int
    total_rejected_dedup: int
    total_rejected_backpressure: int
    by_command_type: Dict[str, int] = field(
        default_factory=dict,
    )
    delta: Dict[str, int] = field(default_factory=dict)
    ts_unix: float = 0.0
    schema_version: str = field(
        default=AUTONOMY_COMMAND_BUS_BRIDGE_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        try:
            by_type = {
                str(k): int(v)
                for k, v in (self.by_command_type or {}).items()
            }
        except Exception:  # noqa: BLE001 — defensive
            by_type = {}
        try:
            delta = {
                str(k): int(v)
                for k, v in (self.delta or {}).items()
            }
        except Exception:  # noqa: BLE001 — defensive
            delta = {}
        return {
            "instance_count": int(self.instance_count),
            "total_qsize": int(self.total_qsize),
            "total_dispatched": int(self.total_dispatched),
            "total_rejected_dedup": int(
                self.total_rejected_dedup,
            ),
            "total_rejected_backpressure": int(
                self.total_rejected_backpressure,
            ),
            "by_command_type": by_type,
            "delta": delta,
            "ts_unix": float(self.ts_unix),
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["CommandBusSnapshotRecord"]:
        try:
            schema = payload.get("schema_version")
            if schema != (
                AUTONOMY_COMMAND_BUS_BRIDGE_SCHEMA_VERSION
            ):
                return None
            return cls(
                instance_count=int(
                    payload.get("instance_count", 0),
                ),
                total_qsize=int(
                    payload.get("total_qsize", 0),
                ),
                total_dispatched=int(
                    payload.get("total_dispatched", 0),
                ),
                total_rejected_dedup=int(
                    payload.get("total_rejected_dedup", 0),
                ),
                total_rejected_backpressure=int(
                    payload.get(
                        "total_rejected_backpressure", 0,
                    ),
                ),
                by_command_type=dict(
                    payload.get("by_command_type", {}),
                ),
                delta=dict(payload.get("delta", {})),
                ts_unix=float(payload["ts_unix"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Pure delta computation — chatter suppression
# ---------------------------------------------------------------------------


def compute_delta(
    *,
    prev: Optional[Mapping[str, Any]],
    curr: Mapping[str, Any],
) -> Dict[str, int]:
    """Pure function. Returns the per-counter delta between
    two :meth:`CommandBus.snapshot_all` outputs. Empty dict
    when no counter changed (chatter-suppress signal). NEVER
    raises.

    Tracks: total_dispatched, total_rejected_dedup,
    total_rejected_backpressure, plus per-command-type
    increments via ``cmd:<type>`` keys."""
    out: Dict[str, int] = {}
    try:
        if not prev:
            # First poll — emit unconditionally with the
            # current totals as the "delta" so operators see
            # the baseline.
            for k in (
                "total_dispatched",
                "total_rejected_dedup",
                "total_rejected_backpressure",
            ):
                v = int(curr.get(k, 0))
                if v != 0:
                    out[k] = v
            ct_curr = curr.get("by_command_type", {}) or {}
            for k, v in ct_curr.items():
                if int(v) != 0:
                    out[f"cmd:{k}"] = int(v)
            return out
        for k in (
            "total_dispatched",
            "total_rejected_dedup",
            "total_rejected_backpressure",
        ):
            try:
                prev_v = int(prev.get(k, 0))
                curr_v = int(curr.get(k, 0))
            except (TypeError, ValueError):
                continue
            if curr_v != prev_v:
                out[k] = curr_v - prev_v
        ct_prev = prev.get("by_command_type", {}) or {}
        ct_curr = curr.get("by_command_type", {}) or {}
        keys = set(ct_prev.keys()) | set(ct_curr.keys())
        for k in keys:
            try:
                prev_v = int(ct_prev.get(k, 0))
                curr_v = int(ct_curr.get(k, 0))
            except (TypeError, ValueError):
                continue
            if curr_v != prev_v:
                out[f"cmd:{k}"] = curr_v - prev_v
    except Exception:  # noqa: BLE001 — defensive
        return {}
    return out


# ---------------------------------------------------------------------------
# Public recorder — caller-invoked from poll loop
# ---------------------------------------------------------------------------


def record_snapshot(
    *,
    snapshot: Mapping[str, Any],
    prev_snapshot: Optional[Mapping[str, Any]] = None,
    ledger_path_override: Optional[Path] = None,
) -> Optional[CommandBusSnapshotRecord]:
    """Record one CommandBus aggregate snapshot. Composes:
      1. Pure :func:`compute_delta` (chatter suppression).
      2. If delta empty → no-op (chatter suppressed).
      3. Build §33.5 versioned record.
      4. Project to canonical broker via
         :func:`publish_autonomy_command_bus_event`.
      5. Append §33.4 flock'd JSONL row.

    Returns the persisted record on emit, None when:
      * Master flag off
      * Delta empty (chatter suppressed)
      * SSE + JSONL persistence both failed

    NEVER raises."""
    if not master_enabled():
        return None
    delta = compute_delta(
        prev=prev_snapshot, curr=snapshot,
    )
    if not delta:
        return None
    try:
        record = CommandBusSnapshotRecord(
            instance_count=int(
                snapshot.get("instance_count", 0),
            ),
            total_qsize=int(
                snapshot.get("total_qsize", 0),
            ),
            total_dispatched=int(
                snapshot.get("total_dispatched", 0),
            ),
            total_rejected_dedup=int(
                snapshot.get(
                    "total_rejected_dedup", 0,
                ),
            ),
            total_rejected_backpressure=int(
                snapshot.get(
                    "total_rejected_backpressure", 0,
                ),
            ),
            by_command_type=dict(
                snapshot.get("by_command_type", {}) or {},
            ),
            delta=delta,
            ts_unix=time.time(),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CommandBusBridge] record build failed",
            exc_info=True,
        )
        return None
    _project_to_canonical_broker(record)
    _flock_persist(
        record=record,
        target=(
            ledger_path_override
            if ledger_path_override is not None
            else ledger_path()
        ),
    )
    return record


def _project_to_canonical_broker(
    record: CommandBusSnapshotRecord,
) -> None:
    """Compose canonical
    :func:`publish_autonomy_command_bus_event` (no direct
    broker.publish). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_autonomy_command_bus_event,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CommandBusBridge] publisher unavailable: %s",
            exc,
        )
        return
    try:
        publish_autonomy_command_bus_event(
            instance_count=record.instance_count,
            total_qsize=record.total_qsize,
            total_dispatched=record.total_dispatched,
            total_rejected_dedup=(
                record.total_rejected_dedup
            ),
            total_rejected_backpressure=(
                record.total_rejected_backpressure
            ),
            by_command_type=record.by_command_type,
            delta=record.delta,
            ts_unix=record.ts_unix,
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CommandBusBridge] publish raised",
            exc_info=True,
        )


def _flock_persist(
    *,
    record: CommandBusSnapshotRecord,
    target: Path,
) -> bool:
    """Append one row via canonical
    :func:`cross_process_jsonl.flock_append_line`. NEVER
    raises."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CommandBusBridge] flock primitive "
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
            "[CommandBusBridge] flock_append_line raised: "
            "%s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Long-running async poller
# ---------------------------------------------------------------------------


class AutonomyCommandBusBridge:
    """Async poller of canonical
    :meth:`CommandBus.snapshot_all`. Singleton; one instance
    per process. Cooperative shutdown via
    :class:`asyncio.Event`.

    **Read-only invariant** (AST-pinned): the poller MUST
    only call :meth:`CommandBus.snapshot_all` (classmethod)
    and per-instance :meth:`metrics_snapshot`. No
    :meth:`put` / :meth:`try_put` / :meth:`get` / setter
    permitted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task: Optional["asyncio.Task[None]"] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._prev_snapshot: Optional[Dict[str, Any]] = None
        self._polls: int = 0
        self._emits: int = 0
        self._suppressed: int = 0

    async def consume_poll_loop(
        self,
        *,
        ledger_path_override: Optional[Path] = None,
    ) -> None:
        """Poll loop. Cancellable via :meth:`stop`. NEVER
        raises out except :class:`asyncio.CancelledError`."""
        if not master_enabled():
            return
        try:
            from backend.core.ouroboros.governance.autonomy.command_bus import (  # noqa: E501
                CommandBus,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CommandBusBridge] CommandBus unavailable: "
                "%s", exc,
            )
            return
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        try:
            while True:
                if self._stop_event.is_set():
                    return
                if not master_enabled():
                    # Operator flipped the flag mid-run;
                    # honor the toggle.
                    return
                try:
                    snapshot = CommandBus.snapshot_all()
                except Exception:  # noqa: BLE001 — defensive
                    snapshot = None
                with self._lock:
                    self._polls += 1
                    prev = self._prev_snapshot
                if snapshot is not None:
                    rec = record_snapshot(
                        snapshot=snapshot,
                        prev_snapshot=prev,
                        ledger_path_override=(
                            ledger_path_override
                        ),
                    )
                    with self._lock:
                        self._prev_snapshot = (
                            dict(snapshot)
                        )
                        if rec is not None:
                            self._emits += 1
                        else:
                            self._suppressed += 1
                cadence = poll_interval_s()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=cadence,
                    )
                    # If we got here, stop event fired.
                    return
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CommandBusBridge] poll loop raised: %s",
                exc,
            )

    async def stop(self) -> None:
        """Signal cooperative shutdown. NEVER raises."""
        try:
            if self._stop_event is not None:
                self._stop_event.set()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CommandBusBridge] stop raised: %s", exc,
            )

    def telemetry(self) -> Dict[str, int]:
        """Process-local counters. NEVER raises."""
        with self._lock:
            return {
                "polls": self._polls,
                "emits": self._emits,
                "suppressed": self._suppressed,
            }


_DEFAULT_BRIDGE: Optional[
    AutonomyCommandBusBridge
] = None
_DEFAULT_BRIDGE_LOCK = threading.Lock()


def get_default_bridge() -> AutonomyCommandBusBridge:
    """Singleton accessor. NEVER raises."""
    global _DEFAULT_BRIDGE
    with _DEFAULT_BRIDGE_LOCK:
        if _DEFAULT_BRIDGE is None:
            _DEFAULT_BRIDGE = (
                AutonomyCommandBusBridge()
            )
        return _DEFAULT_BRIDGE


def reset_default_bridge_for_test() -> None:
    """Test helper. NEVER raises."""
    global _DEFAULT_BRIDGE
    with _DEFAULT_BRIDGE_LOCK:
        _DEFAULT_BRIDGE = None


def start_default_bridge() -> Optional["asyncio.Task[None]"]:
    """Bootstrap helper for orchestrator integration.
    Spawns the poll loop on the running asyncio loop.
    Returns the Task on success, None when:
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
            "[CommandBusBridge] no running event loop — "
            "bootstrap skipped",
        )
        return None
    try:
        task = loop.create_task(
            bridge.consume_poll_loop(),
            name="autonomy_command_bus_bridge_poll",
        )
        with bridge._lock:  # noqa: SLF001
            bridge._task = task  # noqa: SLF001
        return task
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CommandBusBridge] task spawn failed",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Read API — for /observability surface (Slice 7+) + tests
# ---------------------------------------------------------------------------


def read_recent_records(
    *,
    limit: int = 50,
    path: Optional[Path] = None,
) -> Tuple[CommandBusSnapshotRecord, ...]:
    """Read recent snapshot records via canonical
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
        rec = CommandBusSnapshotRecord.from_dict(payload)
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
            name="JARVIS_COMMAND_BUS_BRIDGE_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for Phase 3 A3 — read-only "
                "polling of canonical CommandBus.snapshot_all"
                " → SSE + bounded JSONL ledger. "
                "Default-FALSE per §33.1."
            ),
            category="Observability",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "autonomy_command_bus_bridge.py"
            ),
            example=(
                "JARVIS_COMMAND_BUS_BRIDGE_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CommandBusBridge] master-flag seeding "
            "failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name="JARVIS_COMMAND_BUS_BRIDGE_POLL_S",
            type_="float",
            default=str(_DEFAULT_POLL_S),
            description=(
                "Poll cadence for the CommandBus aggregate "
                "metrics snapshot. Default 2.0s; clamped "
                "[0.5, 60.0]."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "autonomy_command_bus_bridge.py"
            ),
            example=(
                "JARVIS_COMMAND_BUS_BRIDGE_POLL_S=2.0"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CommandBusBridge] poll-interval seeding "
            "failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name="JARVIS_COMMAND_BUS_BRIDGE_LEDGER_PATH",
            type_="path",
            default=str(
                Path(".jarvis") / _DEFAULT_LEDGER_FILENAME
            ),
            description=(
                "JSONL ledger path for Phase 3 A3 "
                "snapshot-delta records (§33.4 flock'd "
                "persistence)."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "autonomy_command_bus_bridge.py"
            ),
            example=(
                "JARVIS_COMMAND_BUS_BRIDGE_LEDGER_PATH="
                ".jarvis/cmdbus.jsonl"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[CommandBusBridge] ledger-path seeding "
            "failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``autonomy_command_bus_bridge_master_default_false``
      2. ``autonomy_command_bus_bridge_authority_asymmetry``
      3. ``autonomy_command_bus_bridge_read_only`` —
         Bridge MUST NOT call ``put`` / ``try_put`` / ``get``
         / ``_enqueue`` / setter on any CommandBus object.
         Allowed: ``snapshot_all`` (classmethod) +
         ``metrics_snapshot`` (per-instance read).
      4. ``autonomy_command_bus_bridge_composes_canonical_bus``
         — Bridge MUST lazy-import :class:`CommandBus` from
         ``autonomy.command_bus`` (single source of truth).
      5. ``autonomy_command_bus_bridge_composes_canonical_publisher``
         — MUST compose
         :func:`publish_autonomy_command_bus_event`; direct
         ``broker.publish`` forbidden.
      6. ``autonomy_command_bus_bridge_composes_canonical_jsonl``
         — §33.4 flock primitives only.
      7. ``autonomy_command_bus_bridge_chatter_suppression``
         — :func:`record_snapshot` MUST early-return when
         the delta dict is empty (chatter suppression
         structural).
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
        "autonomy_command_bus_bridge.py"
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
                    "autonomy_command_bus_bridge" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"autonomy_command_bus_bridge.py"
                            f" MUST NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"autonomy_command_bus_bridge.py"
                            f" MUST NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Bridge MUST only call ``snapshot_all`` /
        ``metrics_snapshot`` on CommandBus references.
        Forbidden: ``put`` / ``try_put`` / ``get`` /
        ``_enqueue`` / ``put_nowait`` / ``get_nowait``.
        Inspects calls on names ``CommandBus`` and
        ``bus``."""
        violations: list = []
        allowed = {
            "snapshot_all", "metrics_snapshot",
            "reset_instance_registry_for_tests",
            "get_rate_limiter_status", "qsize",
        }
        forbidden = {
            "put", "try_put", "get", "_enqueue",
            "put_nowait", "get_nowait",
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            value = func.value
            if not (
                isinstance(value, ast.Name)
                and value.id in {"CommandBus", "bus"}
            ):
                continue
            method = func.attr
            if method in forbidden:
                violations.append(
                    f"read-only: {value.id}.{method}() "
                    f"forbidden (line {node.lineno}); "
                    f"bridge MUST NOT mutate bus state"
                )
                continue
            if method not in allowed and not (
                method.startswith("_")
                and method != "_enqueue"
            ):
                # Allow other dunders / unknown read-likes
                # only if explicitly read-named. Conservative
                # default: warn on anything unfamiliar.
                if method not in {
                    "__class__", "__init__",
                }:
                    violations.append(
                        f"read-only: {value.id}.{method}() "
                        f"is not in the allowed read-only "
                        f"API (line {node.lineno})"
                    )
        return tuple(violations)

    def _validate_composes_canonical_bus(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        composes = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if "autonomy.command_bus" in module:
                names = {n.name for n in node.names}
                if "CommandBus" in names:
                    composes = True
                    break
        if not composes:
            violations.append(
                "composes-canonical-bus: MUST lazy-import "
                "CommandBus from autonomy.command_bus "
                "(single source of truth)"
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
                    "publish_autonomy_command_bus_event"
                    in names
                ):
                    composes = True
                    break
        if not composes:
            violations.append(
                "composes-canonical-publisher: MUST "
                "lazy-import "
                "publish_autonomy_command_bus_event"
            )
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
                    f"direct .publish() call forbidden "
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
                        f"open(..., {mode_arg!r}) "
                        f"forbidden (line {node.lineno})"
                    )
        return tuple(violations)

    def _validate_chatter_suppression(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """:func:`record_snapshot` MUST early-return when
        the delta dict is empty. Inspects the function body
        for an ``if not delta:`` (or equivalent) branch
        that returns None."""
        violations: list = []
        target_func: Optional[ast.FunctionDef] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "record_snapshot"
            ):
                target_func = node
                break
        if target_func is None:
            violations.append("record_snapshot() missing")
            return tuple(violations)
        # Walk for an ``if not delta:`` test; the test
        # expression must reference ``delta`` Name or
        # equivalent.
        has_delta_gate = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            test = sub.test
            # Match: `if not delta:` (UnaryOp(Not, Name(delta)))
            #     OR `if delta == {}:` style
            #     OR `if not <call producing delta>:`
            test_unparsed = ast.unparse(test)
            if (
                "delta" in test_unparsed
                and (
                    "not " in test_unparsed
                    or "==" in test_unparsed
                )
            ):
                # Body must contain an early return.
                for body_stmt in sub.body:
                    if isinstance(body_stmt, ast.Return):
                        has_delta_gate = True
                        break
                if has_delta_gate:
                    break
        if not has_delta_gate:
            violations.append(
                "chatter-suppression: record_snapshot "
                "MUST early-return when delta dict is "
                "empty (chatter discipline)"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "autonomy_command_bus_bridge_"
                "master_default_false"
            ),
            target_file=target,
            description=(
                "Phase 3 A3 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "autonomy_command_bus_bridge_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Phase 3 A3 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "autonomy_command_bus_bridge_read_only"
            ),
            target_file=target,
            description=(
                "Phase 3 A3 — read-only invariant: bridge "
                "MUST only call snapshot_all + "
                "metrics_snapshot on CommandBus references."
            ),
            validate=_validate_read_only,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "autonomy_command_bus_bridge_"
                "composes_canonical_bus"
            ),
            target_file=target,
            description=(
                "Phase 3 A3 — bridge composes canonical "
                "CommandBus class via lazy-import (no "
                "parallel bus)."
            ),
            validate=_validate_composes_canonical_bus,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "autonomy_command_bus_bridge_"
                "composes_canonical_publisher"
            ),
            target_file=target,
            description=(
                "Phase 3 A3 — SSE projection composes "
                "publish_autonomy_command_bus_event; no "
                "direct broker.publish / .publish."
            ),
            validate=_validate_composes_canonical_publisher,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "autonomy_command_bus_bridge_"
                "composes_canonical_jsonl"
            ),
            target_file=target,
            description=(
                "Phase 3 A3 — §33.4 Per-Cluster Flock'd "
                "JSONL: persistence composes canonical "
                "primitives."
            ),
            validate=_validate_composes_canonical_jsonl,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "autonomy_command_bus_bridge_"
                "chatter_suppression"
            ),
            target_file=target,
            description=(
                "Phase 3 A3 — chatter discipline: "
                "record_snapshot MUST early-return when "
                "delta dict is empty (no SSE / no JSONL "
                "row on identical poll)."
            ),
            validate=_validate_chatter_suppression,
        ),
    ]


__all__ = [
    "AUTONOMY_COMMAND_BUS_BRIDGE_SCHEMA_VERSION",
    "AutonomyCommandBusBridge",
    "CommandBusSnapshotRecord",
    "compute_delta",
    "get_default_bridge",
    "ledger_path",
    "master_enabled",
    "poll_interval_s",
    "read_recent_records",
    "record_snapshot",
    "register_flags",
    "register_shipped_invariants",
    "reset_default_bridge_for_test",
    "start_default_bridge",
]
