"""TreeArchive — bounded ring + §33.4 flock'd persistence for the
Treefinement L2 cross-substrate ``/expand b-N`` family.

Closes Phase 4's foundational gap: without an archive ring, the SSE
producer has nothing to project, the REPL ``/repair tree`` verb has
nothing to query, the IDE GET routes have nothing to serve, and
``/expand b-N`` has no destination. This module is the substrate
those surfaces compose.

Architecture
------------

Mirrors :class:`backend.core.ouroboros.governance.
permission_decision_archive.BoundedDecisionArchive` (the canonical
v2.89 ring shape) — drop-oldest FIFO, monotonic ``b-N`` refs that
NEVER rewind, dual master-flag gating (in-memory ring + on-disk
JSONL persistence are independent toggles).

The ``b-`` prefix joins the cross-substrate family — ``t-N`` (Gap #2
tool bodies) / ``d-N`` (Gap #4 diff archive) / ``o-N`` (Gap #3 op
blocks) / ``n-N`` (Gap #6 narrative frames) / ``p-N`` (v2.89
permission decisions). The unified ``/expand <ref>`` REPL verb in
serpent_flow dispatches by prefix.

Reference scheme
----------------

A ``RepairTreeResult`` carries multiple branches across multiple
layers. The archive flattens this — each :class:`RepairBranch` gets
its OWN ``b-N`` ref. The same ref always resolves to the same branch
or to ``None`` (evicted). Refs are issued in record-time order, so
``b-1`` is the FIRST branch ever archived in this process.

Authority boundary
------------------

* §1 deterministic — pure container; no LLM, no network, no console
* §7 fail-closed — every public method returns degraded sentinel on
  failure; ``record`` NEVER raises into the runner path
* §8 observable — :class:`ArchiveSnapshot` projection lets the IDE
  GET layer report capacity / utilization without exposing internal
  state
* §33.4 — when ``persistence_enabled()`` is ``True``, every
  archived ``RepairTreeResult`` is also serialized as one JSONL line
  to ``.jarvis/ouroboros/repair_tree.jsonl`` via the canonical
  ``cross_process_jsonl.flock_append_line`` (no parallel flock
  primitive)
* §33.1 — both master flags default-FALSE; flip via the Phase 9
  graduation soak ladder

What this module does NOT do
----------------------------

* Decide policy — the runner (Phase 1) and validator (Phase 2) are
  authoritative on outcomes; this module is read-only telemetry on
  their results
* Re-implement the ring primitive — composes the canonical
  ``BoundedBodyStore`` shape (FIFO + drop-oldest + monotonic refs)
* Re-implement flock — composes
  ``cross_process_jsonl.flock_append_line``
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_append_line,
)
from backend.core.ouroboros.governance.repair_tree import (
    BranchOutcome,
    RepairBranch,
    RepairTreeResult,
)

logger = logging.getLogger("Ouroboros.RepairTreeArchive")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


REPAIR_TREE_ARCHIVE_SCHEMA_VERSION: str = "repair_tree_archive.v1"


# Master flag for the in-memory ring (descriptive surface — record()
# becomes a no-op when off).
ARCHIVE_MASTER_FLAG_ENV_VAR: str = "JARVIS_L2_TREE_ARCHIVE_ENABLED"
# Capacity of the b-N ring (drop-oldest at this size).
ARCHIVE_SIZE_ENV_VAR: str = "JARVIS_L2_TREE_ARCHIVE_SIZE"
# Master flag for §33.4 JSONL persistence (independent of in-memory
# ring — operator may want one without the other).
PERSISTENCE_MASTER_FLAG_ENV_VAR: str = (
    "JARVIS_L2_TREE_PERSISTENCE_ENABLED"
)
# JSONL persistence path override (default
# ``.jarvis/ouroboros/repair_tree.jsonl`` relative to cwd).
PERSISTENCE_PATH_ENV_VAR: str = "JARVIS_L2_TREE_PERSISTENCE_PATH"


_DEFAULT_ARCHIVE_SIZE: int = 30
_MIN_ARCHIVE_SIZE: int = 1
_MAX_ARCHIVE_SIZE: int = 10_000
_DEFAULT_PERSISTENCE_PATH: str = ".jarvis/ouroboros/repair_tree.jsonl"


# ===========================================================================
# Master-flag accessors — descriptive, NEVER raise
# ===========================================================================


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "false", "0", "no", "off")


def archive_enabled() -> bool:
    """In-memory ring master flag. Default FALSE per §33.1."""
    return _env_bool(ARCHIVE_MASTER_FLAG_ENV_VAR, default=False)


def persistence_enabled() -> bool:
    """JSONL persistence master flag. Default FALSE per §33.1.

    Independent of the ring flag — operator may want disk audit
    without RAM ring (or vice versa). The producer-bridge consults
    each independently."""
    return _env_bool(PERSISTENCE_MASTER_FLAG_ENV_VAR, default=False)


def _read_capacity_from_env() -> int:
    raw = os.environ.get(ARCHIVE_SIZE_ENV_VAR)
    if raw is None:
        return _DEFAULT_ARCHIVE_SIZE
    try:
        n = int(raw)
    except (ValueError, TypeError):
        logger.warning(
            "[RepairTreeArchive] invalid %s=%r — using default %d",
            ARCHIVE_SIZE_ENV_VAR, raw, _DEFAULT_ARCHIVE_SIZE,
        )
        return _DEFAULT_ARCHIVE_SIZE
    return max(_MIN_ARCHIVE_SIZE, min(_MAX_ARCHIVE_SIZE, n))


def _resolve_persistence_path() -> Path:
    raw = os.environ.get(PERSISTENCE_PATH_ENV_VAR)
    if raw and raw.strip():
        return Path(raw.strip())
    return Path(_DEFAULT_PERSISTENCE_PATH)


# ===========================================================================
# Frozen projections
# ===========================================================================


@dataclass(frozen=True)
class ArchivedBranch:
    """One branch's archive entry — frozen post-construction.

    ``ref`` is the cross-substrate ``b-N`` identifier, monotonic per
    archive instance. ``op_id`` lets operators query "all branches
    for op X" via :meth:`TreeArchive.by_op`.
    """

    ref: str
    op_id: str
    layer_index: int
    branch: RepairBranch
    archived_at_unix: float
    schema_version: str = REPAIR_TREE_ARCHIVE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ref": self.ref,
            "op_id": self.op_id,
            "layer_index": self.layer_index,
            "branch": self.branch.to_dict(),
            "archived_at_unix": self.archived_at_unix,
        }


@dataclass(frozen=True)
class ArchiveSnapshot:
    """Cheap read-only projection for the IDE GET / REPL stats layer."""

    capacity: int
    size: int
    next_seq: int
    schema_version: str = REPAIR_TREE_ARCHIVE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "capacity": self.capacity,
            "size": self.size,
            "next_seq": self.next_seq,
            "utilization": (
                self.size / self.capacity if self.capacity else 0.0
            ),
        }


# ===========================================================================
# TreeArchive — the ring
# ===========================================================================


class TreeArchive:
    """Thread-safe bounded FIFO of archived branches.

    Mirrors :class:`permission_decision_archive.BoundedDecisionArchive`
    semantics exactly — drop-oldest on overflow, monotonic ``b-N``
    refs that NEVER rewind, single :class:`threading.RLock` serializes
    all operations.
    """

    def __init__(self, *, capacity: Optional[int] = None) -> None:
        if capacity is None:
            cap = _read_capacity_from_env()
        else:
            try:
                int_cap = int(capacity)
            except (TypeError, ValueError):
                int_cap = _DEFAULT_ARCHIVE_SIZE
            cap = max(
                _MIN_ARCHIVE_SIZE,
                min(_MAX_ARCHIVE_SIZE, int_cap),
            )
        self._capacity: int = cap
        self._items: "OrderedDict[str, ArchivedBranch]" = OrderedDict()
        # Secondary index: op_id → set of refs (for fast by_op lookup)
        self._by_op: Dict[str, List[str]] = {}
        # Tertiary index: branch_id → ref (for /expand b-N lookup by
        # underlying branch hash; useful when operators paste a branch_id
        # from telemetry)
        self._by_branch_id: Dict[str, str] = {}
        self._next_seq: int = 1
        self._lock = threading.RLock()

    # ---- introspection ----------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> ArchiveSnapshot:
        with self._lock:
            return ArchiveSnapshot(
                capacity=self._capacity,
                size=len(self._items),
                next_seq=self._next_seq,
            )

    # ---- mutating API -----------------------------------------------

    def record_result(
        self,
        result: RepairTreeResult,
    ) -> Tuple[ArchivedBranch, ...]:
        """Archive every branch in ``result``. Master-flag-gated:
        when :func:`archive_enabled` is FALSE this is a no-op
        returning empty tuple (callers should treat as 'telemetry
        skipped, not an error'). NEVER raises into the runner path.

        Returns the freshly-archived ``ArchivedBranch`` projections
        (one per branch in the result) for downstream SSE producers.
        """
        if not archive_enabled():
            return ()

        archived: List[ArchivedBranch] = []
        try:
            with self._lock:
                op_id = self._safe_str(result.root_op_id)
                now = time.time()
                for layer in result.layers:
                    for branch in layer.branches:
                        ref = f"b-{self._next_seq}"
                        self._next_seq += 1
                        entry = ArchivedBranch(
                            ref=ref,
                            op_id=op_id,
                            layer_index=branch.layer_index,
                            branch=branch,
                            archived_at_unix=now,
                        )
                        self._items[ref] = entry
                        self._by_op.setdefault(op_id, []).append(ref)
                        # Branch IDs may collide across results (same
                        # diff appearing in two different ops). We keep
                        # the most recent — older entries still resolve
                        # by ref but not by branch_id.
                        self._by_branch_id[branch.branch_id] = ref
                        archived.append(entry)
                # Drop-oldest eviction
                while len(self._items) > self._capacity:
                    evicted_ref, evicted = self._items.popitem(last=False)
                    self._evict_indices(evicted_ref, evicted)
        except Exception:  # noqa: BLE001 — fail-closed
            logger.warning(
                "[RepairTreeArchive] record_result raised; "
                "telemetry dropped",
                exc_info=True,
            )
            return tuple(archived)
        return tuple(archived)

    def _evict_indices(
        self, ref: str, evicted: ArchivedBranch,
    ) -> None:
        """Best-effort secondary index cleanup. Wrapped at caller in
        the master try/except so partial cleanup failures don't crash
        the ring."""
        op_refs = self._by_op.get(evicted.op_id)
        if op_refs and ref in op_refs:
            op_refs.remove(ref)
            if not op_refs:
                self._by_op.pop(evicted.op_id, None)
        if self._by_branch_id.get(evicted.branch.branch_id) == ref:
            self._by_branch_id.pop(evicted.branch.branch_id, None)

    # ---- query API --------------------------------------------------

    def get_by_ref(self, ref: str) -> Optional[ArchivedBranch]:
        """Return the entry for ``b-N`` ref, or None if evicted /
        unknown. NEVER raises."""
        if not isinstance(ref, str):
            return None
        with self._lock:
            return self._items.get(ref)

    def get_by_branch_id(
        self, branch_id: str,
    ) -> Optional[ArchivedBranch]:
        """Look up by underlying branch hash (the patch_signature_hash
        from Phase 1). Returns the most-recently-archived entry for
        that hash."""
        if not isinstance(branch_id, str):
            return None
        with self._lock:
            ref = self._by_branch_id.get(branch_id)
            if ref is None:
                return None
            return self._items.get(ref)

    def by_op(self, op_id: str) -> Tuple[ArchivedBranch, ...]:
        """All archived branches for an op_id, in record-time order."""
        if not isinstance(op_id, str):
            return ()
        with self._lock:
            refs = list(self._by_op.get(op_id, ()))
            return tuple(
                self._items[r] for r in refs if r in self._items
            )

    def recent(
        self, limit: int = 20,
    ) -> Tuple[ArchivedBranch, ...]:
        """Most recent ``limit`` entries, newest first."""
        if limit <= 0:
            return ()
        with self._lock:
            entries = list(self._items.values())
        return tuple(reversed(entries[-limit:]))

    # ---- internal ----------------------------------------------------

    @staticmethod
    def _safe_str(value: Any) -> str:
        if value is None:
            return ""
        try:
            return str(value)
        except Exception:  # noqa: BLE001
            return ""


# ===========================================================================
# Process-wide default archive (lazy singleton)
# ===========================================================================


_DEFAULT: Optional[TreeArchive] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_archive() -> TreeArchive:
    """Lazy default archive singleton. Capacity loaded from env on
    first construction; stays stable thereafter (env changes during
    runtime don't resize the ring — that would require eviction
    semantics outside this slice's scope)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = TreeArchive()
        return _DEFAULT


def reset_default_archive_for_tests() -> None:
    """Reset the singleton — tests use this between cases."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None


# ===========================================================================
# §33.4 JSONL persistence (composes cross_process_jsonl.flock_append_line)
# ===========================================================================


def persist_tree_result(
    result: RepairTreeResult,
    *,
    path: Optional[Path] = None,
) -> bool:
    """Append one JSONL record per branch in ``result`` to the
    canonical ``.jarvis/ouroboros/repair_tree.jsonl`` (or
    ``path`` override). Master-flag-gated: when
    :func:`persistence_enabled` is FALSE this is a no-op returning
    False. NEVER raises.

    Composes the canonical ``cross_process_jsonl.flock_append_line``
    primitive — no parallel flock implementation. Each branch is
    one JSONL line; multiple branches produce multiple lines, all
    appended atomically per-line (cross-process safe).

    Returns True on full success (all branches written), False
    on any failure (master flag off, lock timeout, write error,
    serialization failure). Partial success is logged as a
    warning and counted as False.
    """
    if not persistence_enabled():
        return False

    target = path if path is not None else _resolve_persistence_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 — defensive
        logger.warning(
            "[RepairTreeArchive] could not mkdir %s — persistence skipped",
            target.parent,
        )
        return False

    now = time.time()
    op_id = TreeArchive._safe_str(result.root_op_id)
    success_count = 0
    total_count = 0
    for layer in result.layers:
        for branch in layer.branches:
            total_count += 1
            try:
                payload = {
                    "schema_version": (
                        REPAIR_TREE_ARCHIVE_SCHEMA_VERSION
                    ),
                    "op_id": op_id,
                    "layer_index": branch.layer_index,
                    "archived_at_unix": now,
                    "branch": branch.to_dict(),
                }
                line = json.dumps(payload, ensure_ascii=True)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[RepairTreeArchive] could not serialize branch %s",
                    branch.branch_id,
                    exc_info=True,
                )
                continue
            ok = flock_append_line(target, line)
            if ok:
                success_count += 1
    if success_count != total_count:
        logger.warning(
            "[RepairTreeArchive] partial persistence: %d/%d "
            "branches written to %s",
            success_count, total_count, target,
        )
        return False
    return True


# ===========================================================================
# Producer-bridge — composes record_result + persist_tree_result
# ===========================================================================


def maybe_archive_tree_result(
    result: RepairTreeResult,
) -> Tuple[ArchivedBranch, ...]:
    """§33.2 producer-bridge — invoked at the end of
    ``RepairTreeRunner.run_tree`` (Phase 5 wires the call) to
    archive + persist + publish SSE events for the result.

    Three independent stages, each independently gated:
      * ring archive (JARVIS_L2_TREE_ARCHIVE_ENABLED)
      * JSONL persistence (JARVIS_L2_TREE_PERSISTENCE_ENABLED)
      * SSE publish (JARVIS_IDE_STREAM_ENABLED — checked inside
        publish_task_event itself)

    Failure of any one MUST NOT block the others.
    Returns the freshly-archived branches (empty tuple when ring
    is master-off). NEVER raises into the runner.
    """
    archived: Tuple[ArchivedBranch, ...] = ()
    try:
        archived = get_default_archive().record_result(result)
    except Exception:  # noqa: BLE001 — fail-closed
        logger.warning(
            "[RepairTreeArchive] ring archive raised",
            exc_info=True,
        )
    try:
        persist_tree_result(result)
    except Exception:  # noqa: BLE001 — fail-closed
        logger.warning(
            "[RepairTreeArchive] persistence raised",
            exc_info=True,
        )
    try:
        _publish_branch_lifecycle_events(result, archived)
    except Exception:  # noqa: BLE001 — fail-closed
        logger.warning(
            "[RepairTreeArchive] SSE publish raised",
            exc_info=True,
        )
    return archived


def _publish_branch_lifecycle_events(
    result: RepairTreeResult,
    archived: Tuple[ArchivedBranch, ...],
) -> None:
    """Fire one SSE event per branch + one per layer + one per WON
    terminal. Best-effort: each publish wrapped in its own try/except
    via the canonical ``publish_task_event`` (already best-effort);
    this wrapper is defense in depth.

    Composes the canonical 4-event taxonomy registered in
    ``ide_observability_stream._VALID_EVENT_TYPES``. No parallel
    SSE infrastructure — reuses the existing broker.
    """
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_REPAIR_BRANCH_PROMOTED,
            EVENT_TYPE_REPAIR_BRANCH_PRUNED,
            EVENT_TYPE_REPAIR_LAYER_COMPLETED,
            EVENT_TYPE_REPAIR_TREE_WON,
            publish_task_event,
        )
    except ImportError:
        return

    op_id = TreeArchive._safe_str(result.root_op_id)
    # Build a branch_id → archived_ref index for payload enrichment
    ref_by_branch_id: Dict[str, str] = {
        a.branch.branch_id: a.ref for a in archived
    }

    for layer in result.layers:
        for branch in layer.branches:
            if branch.outcome == BranchOutcome.WON:
                # Event surfaced via tree_won (below) — don't double-fire
                continue
            event_type = (
                EVENT_TYPE_REPAIR_BRANCH_PROMOTED
                if branch.outcome == BranchOutcome.PROMOTED
                else EVENT_TYPE_REPAIR_BRANCH_PRUNED
            )
            payload = {
                "ref": ref_by_branch_id.get(branch.branch_id),
                "branch_id": branch.branch_id[:16],
                "layer_index": branch.layer_index,
                "outcome": branch.outcome.value,
                "validator_score": branch.validator_score,
                "prune_reason": (
                    branch.prune_reason.value
                    if branch.prune_reason else None
                ),
            }
            publish_task_event(event_type, op_id, payload)

        # One event per layer
        publish_task_event(
            EVENT_TYPE_REPAIR_LAYER_COMPLETED,
            op_id,
            {
                "layer_index": layer.layer_index,
                "verdict": layer.verdict.value,
                "wall_ms": layer.wall_ms,
                "branch_count": len(layer.branches),
                "parallel_units_actual": layer.parallel_units_actual,
            },
        )

    # WON terminal event (fires once per tree, only when there's a winner)
    if result.winning_branch_path:
        won_id = result.winning_branch_path[-1]
        publish_task_event(
            EVENT_TYPE_REPAIR_TREE_WON,
            op_id,
            {
                "winning_branch_path": list(result.winning_branch_path),
                "won_branch_id": won_id[:16],
                "won_ref": ref_by_branch_id.get(won_id),
                "layer_count": len(result.layers),
            },
        )




# ===========================================================================
# FlagRegistry self-registration (auto-discovered by walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration. Picked up zero-edit
    by ``flag_registry_seed._discover_module_provided_flags``.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=ARCHIVE_MASTER_FLAG_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master kill switch for the in-memory tree archive "
                "ring (Phase 4 substrate). When FALSE, "
                "record_result() is a no-op + REPL /repair tree + "
                "/expand b-N + IDE GET routes return empty. "
                "Default FALSE per §33.1 graduation contract — flip "
                "via Phase 9 soak ladder."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "repair_tree_archive.py"
            ),
            example="true",
            since="Treefinement Phase 4 (2026-05-11)",
        ),
        FlagSpec(
            name=ARCHIVE_SIZE_ENV_VAR,
            type=FlagType.INT,
            default=_DEFAULT_ARCHIVE_SIZE,
            description=(
                "Capacity of the b-N ring. Drop-oldest at this size. "
                "Monotonic b-N counter NEVER rewinds even after "
                "eviction. Clamped [1, 10000]."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "repair_tree_archive.py"
            ),
            example="30",
            since="Treefinement Phase 4 (2026-05-11)",
        ),
        FlagSpec(
            name=PERSISTENCE_MASTER_FLAG_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master kill switch for §33.4 JSONL persistence to "
                ".jarvis/ouroboros/repair_tree.jsonl (or path "
                "override). Independent of the ring flag — operator "
                "may want disk audit without RAM ring (or vice "
                "versa). Composes cross_process_jsonl."
                "flock_append_line. Default FALSE per §33.1."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "repair_tree_archive.py"
            ),
            example="true",
            since="Treefinement Phase 4 (2026-05-11)",
        ),
        FlagSpec(
            name=PERSISTENCE_PATH_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_PERSISTENCE_PATH,
            description=(
                "Override path for the JSONL persistence file. "
                "Relative paths resolve against process cwd. "
                "Parent directory created if missing. Default "
                ".jarvis/ouroboros/repair_tree.jsonl."
            ),
            category=Category.OBSERVABILITY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "repair_tree_archive.py"
            ),
            example=".jarvis/ouroboros/repair_tree.jsonl",
            since="Treefinement Phase 4 (2026-05-11)",
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[RepairTreeArchive] flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "REPAIR_TREE_ARCHIVE_SCHEMA_VERSION",
    "ARCHIVE_MASTER_FLAG_ENV_VAR",
    "ARCHIVE_SIZE_ENV_VAR",
    "PERSISTENCE_MASTER_FLAG_ENV_VAR",
    "PERSISTENCE_PATH_ENV_VAR",
    "ArchivedBranch",
    "ArchiveSnapshot",
    "TreeArchive",
    "archive_enabled",
    "persistence_enabled",
    "get_default_archive",
    "reset_default_archive_for_tests",
    "persist_tree_result",
    "maybe_archive_tree_result",
    "register_flags",
]
