"""Domain-1 Staging-2 Task 3 -- the EVENT-SOURCED graph ingestor.

The sink for the Staging-1 ``CausalDeltaSubscriber.on_delta`` callback. One
stamped structural-delta envelope arrives; it is durably appended to an intake
WAL FIRST, and only AFTER that append lands is it folded into the in-memory
``CausalGraph`` (Task 1, O(1) fold). This is a genuine write-AHEAD log: the
live graph never reflects a delta that is not already on disk, so the graph is
deterministically RE-FOLDABLE after a Brain crash with ``recovered == live``.

*** WRITE-AHEAD ORDERING (append-before-fold) -- the durability contract ***

``ingest`` is a PURE non-blocking enqueue: it validates the envelope and pushes
it onto the single ordered queue -- it does NOT fold. The SINGLE append worker
then, per envelope, IN THIS ORDER: (a) ``await offload(WAL.append)`` -- DURABLE
first; (b) only if that append succeeded, ``graph.apply_delta`` -- fold into the
live graph. If the append FAILS, the delta is NOT folded (a non-durable delta
must never enter the live graph) -- logged loudly, worker continues (fail-soft).

Consequence: the WAL on disk is ALWAYS a superset-or-equal of the live graph.
The live graph is EVENTUALLY-consistent with ingest -- it lags by the worker's
append latency -- which is correct for an advisory graph (a reader only ever
observes durable state). At quiescence (queue drained, worker idle): live graph
== fold(WAL). ``flush()`` awaits that quiescence.

Two durability surfaces compose into a loss-free, deterministic recovery:

  * **WAL** (``JARVIS_CAUSAL_WAL_PATH``): an append-only JSONL log of every
    accepted envelope, in ingest order. Replayed by folding each entry through
    ``graph.apply_delta`` in FILE ORDER.

  * **Snapshot** (``JARVIS_CAUSAL_SNAPSHOT_PATH``): a periodic
    ``graph.snapshot()`` (DETERMINISTIC fold-to-snapshot -- NOT heuristic GC).
    Written every ``JARVIS_CAUSAL_SNAPSHOT_EVERY_N`` appends OR after
    ``JARVIS_CAUSAL_SNAPSHOT_IDLE_S`` of no ingest. After a snapshot lands
    durably the WAL is truncated to empty: recovery = ``from_snapshot`` + fold
    the WAL tail.

*** THE BINDING INVARIANT (per-repo emit_seq order) ***

The ``CausalGraph`` fold is fully order-independent for FULL-WRITE ops
(symbols_added / symbols_removed commute under any shuffle), but PARTIAL-WRITE
fields (a resignature-only signature update, an import-only edge merge, the
``imports`` merge symbols_added performs) are last-writer-wins per field-source
and converge ONLY under per-repo ``emit_seq`` order. THEREFORE the WAL append
MUST preserve per-repo emit_seq order: for any one repo, WAL append order ==
ingest order == emit_seq order.

This is guaranteed here by a SINGLE serialized append path -- NOT N racing
fire-and-forget offload tasks (which could reorder same-repo appends). Every
accepted envelope is pushed onto ONE ordered ``asyncio.Queue`` (FIFO) that a
SINGLE append worker drains, ``await``-ing each offloaded append before it
pulls the next item. FIFO queue + single-flight worker => append order == push
order == ingest order. Cross-repo interleave is fine (disjoint ``symbol_id``s
commute); only the per-repo subsequence order is load-bearing.

Determinism note: the fold depends ONLY on envelope content + ``emit_seq``,
NEVER on the WAL's ``ts_monotonic`` / ``ts_utc`` metadata (those are wall-clock
bookkeeping and do not touch graph state). Because the worker folds ONLY what
it has already appended, ``graph.snapshot()`` at compaction time == the durable
log state exactly (no queued-ahead divergence); the ``emit_seq``-monotonic
guard additionally makes any WAL-tail re-fold after a snapshot idempotent, so
compaction is loss-free across a crash.

Fail-soft throughout: ``ingest`` never raises into the bus loop, a malformed
envelope is dropped (never enqueued -> never appended, never folded), a failed
append drops its delta from the live graph (non-durable), and every offloaded
filesystem op degrades to a logged no-op rather than propagating.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.core.ouroboros.governance.causal.causal_graph import CausalGraph
from backend.core.ouroboros.governance.cooperative_fs_io import (
    is_offload_error,
    offload,
)
from backend.core.ouroboros.governance.intake.wal import WAL, WALEntry

logger = logging.getLogger(__name__)

_ENV_WAL_PATH = "JARVIS_CAUSAL_WAL_PATH"
_ENV_SNAPSHOT_PATH = "JARVIS_CAUSAL_SNAPSHOT_PATH"
_ENV_SNAPSHOT_EVERY_N = "JARVIS_CAUSAL_SNAPSHOT_EVERY_N"
_ENV_SNAPSHOT_IDLE_S = "JARVIS_CAUSAL_SNAPSHOT_IDLE_S"

_DEFAULT_SNAPSHOT_EVERY_N = 500
_DEFAULT_SNAPSHOT_IDLE_S = 3600


def _repo_root() -> Path:
    """<repo_root>, derived from this file's location: causal_graph_ingestor.py
    -> causal -> governance -> ouroboros -> core -> backend -> <repo_root>
    (parents[5]). Mirrors ``structural_delta._default_emit_seq_path``."""
    return Path(__file__).resolve().parents[5]


def _default_wal_path() -> Path:
    return _repo_root() / ".jarvis" / "causal_graph_wal.jsonl"


def _default_snapshot_path() -> Path:
    return _repo_root() / ".jarvis" / "causal_graph_snapshot.json"


def _env_int(name: str, default: int) -> int:
    """Env-resolved positive int; unset/blank/unparseable/<=0 -> default.
    NEVER raises."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# --------------------------------------------------------------------------- #
# module-level offload workers (detached -- safe to hand to the executor)
# --------------------------------------------------------------------------- #
def _write_json_atomic(path_str: str, payload: dict) -> bool:
    """Atomically write ``payload`` as JSON to ``path_str`` (temp + os.replace).
    Returns True on success; raises on failure so ``offload`` wraps it in an
    OffloadError (never truncates the WAL on a failed snapshot)."""
    p = Path(path_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return True


def _truncate_file(path_str: str) -> bool:
    """Truncate ``path_str`` to empty (durably). No-op if absent."""
    p = Path(path_str)
    if p.exists():
        with p.open("w", encoding="utf-8") as f:
            f.flush()
            os.fsync(f.fileno())
    return True


def _read_json(path_str: str) -> Optional[dict]:
    """Read + parse a JSON object from ``path_str``. Absent/corrupt -> None."""
    p = Path(path_str)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 -- corrupt snapshot -> cold replay
        return None


class CausalGraphIngestor:
    """Event-sourced fold: subscriber callback -> graph fold + ordered WAL.

    Parameters
    ----------
    graph:
        The in-memory :class:`CausalGraph` (Task 1). Folded synchronously in
        :meth:`ingest`; rebuilt from a snapshot during :meth:`replay_from_wal`.
    wal_path / snapshot_path:
        Override the durable paths (defaults from
        ``JARVIS_CAUSAL_WAL_PATH`` / ``JARVIS_CAUSAL_SNAPSHOT_PATH``, then the
        repo-local ``.jarvis`` defaults). ``None`` -> resolve from env.
    """

    def __init__(
        self,
        graph: CausalGraph,
        *,
        wal_path: Optional[str] = None,
        snapshot_path: Optional[str] = None,
    ) -> None:
        self.graph = graph

        wp = wal_path or os.environ.get(_ENV_WAL_PATH) or str(_default_wal_path())
        sp = (
            snapshot_path
            or os.environ.get(_ENV_SNAPSHOT_PATH)
            or str(_default_snapshot_path())
        )
        self._wal_path = Path(wp)
        self._snapshot_path = Path(sp)
        self._wal = WAL(self._wal_path)

        self._snapshot_every_n = _env_int(
            _ENV_SNAPSHOT_EVERY_N, _DEFAULT_SNAPSHOT_EVERY_N
        )
        self._snapshot_idle_s = _env_int(
            _ENV_SNAPSHOT_IDLE_S, _DEFAULT_SNAPSHOT_IDLE_S
        )

        # SINGLE serialized append path (the binding invariant): one ordered
        # FIFO queue drained by one worker. Created in start() so the queue
        # binds to the running loop (py3.9-safe).
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        # Serializes WAL file ops (append vs compact) so an out-of-band
        # snapshot_now() cannot interleave bytes with an in-flight append.
        self._file_lock = asyncio.Lock()
        self._appends_since_snapshot = 0
        self._started = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Replay the durable log into ``self.graph`` then arm the single
        ordered append worker. Idempotent; fail-soft."""
        if self._started:
            return
        try:
            await self.replay_from_wal()
        except Exception:  # noqa: BLE001 -- replay is best-effort
            logger.warning(
                "[CausalGraphIngestor] replay_from_wal failed (cold start)",
                exc_info=True,
            )
        self._queue = asyncio.Queue()
        self._worker = asyncio.ensure_future(self._append_worker())
        self._started = True

    async def stop(self) -> None:
        """Flush pending appends then stop the worker. Never raises."""
        self._started = False
        try:
            await self.flush()
        except Exception:  # noqa: BLE001 -- best-effort flush
            logger.debug("[CausalGraphIngestor] flush on stop failed",
                         exc_info=True)
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 -- fail-soft teardown
                logger.debug("[CausalGraphIngestor] worker teardown raised",
                             exc_info=True)
        self._queue = None

    async def flush(self) -> None:
        """Block until every queued append (and any compaction it triggered)
        has drained. Fail-soft."""
        q = self._queue
        if q is None:
            return
        try:
            await q.join()
        except Exception:  # noqa: BLE001
            logger.debug("[CausalGraphIngestor] queue.join failed",
                         exc_info=True)

    # ------------------------------------------------------------------ #
    # the sink -- CausalDeltaSubscriber.on_delta target
    # ------------------------------------------------------------------ #
    def ingest(self, envelope: dict) -> None:
        """PURE non-blocking enqueue onto the SINGLE ordered append queue -- the
        write-AHEAD contract. ingest does NOT fold: the durable WAL append AND
        the subsequent graph fold both happen on the append worker (append
        FIRST, then fold), so a delta enters the live graph ONLY AFTER it is
        durable on disk. Malformed envelopes are dropped (never enqueued ->
        never appended, never folded). NEVER blocks, NEVER raises (this runs
        inside the bus delivery loop)."""
        if not _looks_valid(envelope):
            logger.debug("[CausalGraphIngestor] dropping malformed envelope")
            return
        # Push onto the single ordered queue. put_nowait on an unbounded queue
        # is non-blocking; FIFO ordering + the single worker's append-then-fold
        # discipline preserve per-repo emit_seq order in the WAL (the binding
        # invariant) AND the write-ahead guarantee.
        q = self._queue
        if q is None:
            # Not started -> no durable path -> we MUST NOT fold (write-ahead:
            # nothing enters the graph that isn't durable). Drop it.
            logger.debug("[CausalGraphIngestor] ingest before start() -- "
                         "envelope dropped (no durable path, not folded)")
            return
        try:
            q.put_nowait(envelope)
        except Exception:  # noqa: BLE001 -- never raise into the bus loop
            logger.debug("[CausalGraphIngestor] enqueue failed (swallowed)",
                         exc_info=True)

    # ------------------------------------------------------------------ #
    # the single ordered append worker (the serialization pin)
    # ------------------------------------------------------------------ #
    async def _append_worker(self) -> None:
        """Drain the ordered queue ONE envelope at a time (``await`` each item's
        durable append before pulling the next). Per envelope, in ORDER:
        (a) durably append to the WAL; (b) ONLY if the append landed, fold into
        the live graph via ``apply_delta`` (write-AHEAD -- a non-durable delta
        never enters the graph). This single-flight, append-then-fold discipline
        preserves per-repo emit_seq order in the WAL AND the durability
        contract. Every ``JARVIS_CAUSAL_SNAPSHOT_EVERY_N`` durable folds OR
        after ``JARVIS_CAUSAL_SNAPSHOT_IDLE_S`` idle, folds to a snapshot +
        truncates the WAL."""
        idle_timeout: Optional[float] = (
            float(self._snapshot_idle_s) if self._snapshot_idle_s > 0 else None
        )
        while True:
            q = self._queue
            if q is None:
                return
            try:
                if idle_timeout is None:
                    envelope = await q.get()
                else:
                    envelope = await asyncio.wait_for(q.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                # Idle compaction: fold-to-snapshot if anything accrued since
                # the last snapshot.
                if self._appends_since_snapshot > 0:
                    await self._compact()
                continue
            except asyncio.CancelledError:
                raise
            try:
                durable = await self._append_one(envelope)
                if durable:
                    # WRITE-AHEAD: fold ONLY after the append landed durably.
                    try:
                        self.graph.apply_delta(envelope)
                    except Exception:  # noqa: BLE001 -- apply_delta is fail-soft
                        logger.debug(
                            "[CausalGraphIngestor] apply_delta raised "
                            "(swallowed)", exc_info=True)
                    self._appends_since_snapshot += 1
                    if self._appends_since_snapshot >= self._snapshot_every_n:
                        await self._compact()
                # not durable -> NOT folded (already logged loudly in _append_one)
            except Exception:  # noqa: BLE001 -- fail-soft per item
                logger.warning(
                    "[CausalGraphIngestor] append/fold/compact failed "
                    "(swallowed)", exc_info=True)
            finally:
                q.task_done()

    async def _append_one(self, envelope: dict) -> bool:
        """Offload ONE WAL append under the file lock. Returns True iff the
        append landed durably (the worker folds ONLY on True -- the write-ahead
        gate). The lock only prevents byte-interleave with an out-of-band
        snapshot_now(); ordering is already pinned by the single FIFO worker.
        ``lease_id`` is a unique uuid (never updated -> the entry stays
        'pending' -> replay reads the full ordered log); ``ts_*`` are metadata
        only and do NOT affect the fold. A failed append is logged loudly and
        returns False so the non-durable delta is dropped from the live graph.

        Durability is gated on ``WAL.append``'s HONEST bool return, NOT merely
        on the absence of an exception: ``flock_append_line`` returns False on
        lock-timeout / write-error (no bytes on disk) while still returning
        normally, so a falsy return must be treated as NOT durable:
        ``durable = (not is_offload_error(res)) and bool(res)``."""
        try:
            entry = WALEntry(
                lease_id=uuid.uuid4().hex,
                envelope_dict=envelope,
                status="pending",
                ts_monotonic=time.monotonic(),
                ts_utc=datetime.now(timezone.utc).isoformat(),
            )
            async with self._file_lock:
                res = await offload(self._wal.append, entry)
        except Exception as exc:  # noqa: BLE001 -- never fold a non-durable delta
            logger.error(
                "[CausalGraphIngestor] WAL append raised (%s) -- delta NOT "
                "folded (non-durable)", exc, exc_info=True)
            return False
        if is_offload_error(res):
            logger.error(
                "[CausalGraphIngestor] WAL append offload failed (%s) -- delta "
                "NOT folded (non-durable, dropped from live graph)", res)
            return False
        # WAL.append returns True iff bytes durably landed on disk. A False
        # (lock timeout / OSError inside flock_append_line) returns normally
        # but is a NON-DURABLE write -- it must NOT be folded.
        if not bool(res):
            logger.error(
                "[CausalGraphIngestor] WAL append reported NOT durable "
                "(write failed) -- delta NOT folded (dropped from live graph)")
            return False
        return True

    async def snapshot_now(self) -> None:
        """Force a fold-to-snapshot + WAL truncation now (deterministic
        compaction). Serialized against the append worker via the file lock."""
        await self._compact()

    async def _compact(self) -> None:
        """Deterministic fold-to-snapshot (spec Q4): write ``graph.snapshot()``
        durably, THEN truncate the WAL. Under write-ahead the worker folds ONLY
        what it has already appended, so the live graph at this point == the
        durable log exactly -- the snapshot IS the durable state (no queued-ahead
        divergence). The snapshot is computed synchronously on the loop (a
        detached dict -- no shared mutable state) and only its serialization/
        write is offloaded, so it never races a concurrent fold. Truncation
        happens ONLY after the snapshot lands durably -- a failed snapshot leaves
        the WAL intact (no data loss); any WAL-tail re-fold after replay is
        idempotent (emit_seq guard)."""
        try:
            snap = self.graph.snapshot()
        except Exception:  # noqa: BLE001
            logger.warning("[CausalGraphIngestor] snapshot() failed -- "
                           "WAL left intact", exc_info=True)
            return
        async with self._file_lock:
            wrote = await offload(
                _write_json_atomic, str(self._snapshot_path), snap)
            if is_offload_error(wrote):
                logger.warning(
                    "[CausalGraphIngestor] snapshot write failed (%s) -- "
                    "WAL left intact, no truncation", wrote)
                return
            truncated = await offload(_truncate_file, str(self._wal_path))
            if is_offload_error(truncated):
                logger.warning(
                    "[CausalGraphIngestor] WAL truncate failed (%s) -- "
                    "snapshot durable, WAL tail will re-fold idempotently",
                    truncated)
                return
        self._appends_since_snapshot = 0

    # ------------------------------------------------------------------ #
    # crash recovery
    # ------------------------------------------------------------------ #
    async def replay_from_wal(self) -> None:
        """Deterministically re-fold the durable log into ``self.graph``:
        load the snapshot (if present) via ``from_snapshot``, then fold every
        WAL entry through ``apply_delta`` in FILE ORDER (== per-repo emit_seq
        order by the append invariant). The emit_seq-monotonic fold makes this
        idempotent, so a WAL tail that overlaps the snapshot re-folds to the
        same state. After replay, ``state_fingerprint()`` equals the live
        graph's. Fail-soft."""
        snap = await offload(_read_json, str(self._snapshot_path))
        if not is_offload_error(snap) and isinstance(snap, dict) and (
            "nodes" in snap
        ):
            try:
                self.graph = CausalGraph.from_snapshot(snap)
            except Exception:  # noqa: BLE001 -- corrupt snapshot -> cold fold
                logger.warning(
                    "[CausalGraphIngestor] from_snapshot failed -- folding WAL "
                    "onto the existing graph", exc_info=True)

        entries = await offload(self._wal.pending_entries)
        if is_offload_error(entries) or not isinstance(entries, list):
            return
        for entry in entries:
            try:
                self.graph.apply_delta(entry.envelope_dict)
            except Exception:  # noqa: BLE001 -- apply_delta is already fail-soft
                logger.debug("[CausalGraphIngestor] replay fold raised "
                             "(swallowed)", exc_info=True)


def _looks_valid(envelope: Any) -> bool:
    """Cheap structural gate mirroring ``apply_delta``'s parse contract: a dict
    with a dict ``delta`` and a dict ``lineage`` carrying an int-able
    ``emit_seq``. Malformed -> dropped from BOTH graph and WAL; a well-formed
    envelope that folds zero nodes (a lost emit_seq race, a resignature ahead
    of its add) is STILL logged so replay determinism is preserved."""
    if not isinstance(envelope, dict):
        return False
    if not isinstance(envelope.get("delta"), dict):
        return False
    lineage = envelope.get("lineage")
    if not isinstance(lineage, dict):
        return False
    try:
        int(lineage["emit_seq"])
    except (KeyError, TypeError, ValueError):
        return False
    return True
