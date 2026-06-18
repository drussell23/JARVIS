"""Oracle persistence layer — interface-segregated, storage-agnostic.

Phase 2 of the Sovereign Oracle Architecture (gated by the §9 benchmark verdict in
``docs/architecture/ORACLE_PERSISTENCE_BENCHMARK_RESULTS.md``: aiosqlite won as the only
backend without a fatal axis — ~10x cheaper incremental checkpoint than the monolithic
pickle, scatter-resilient because row-level updates are decoupled from bulk reads).

Design (the three constraints, enforced structurally):

1. **Interface segregation.** ``TheOracle`` never touches SQLite. It speaks only to the
   ``PersistenceProvider`` ABC via a ``GraphState`` value object. Swapping in a cloud /
   Postgres / object-store backend later is a new ``PersistenceProvider`` subclass and a
   factory line — zero changes to the Oracle's business logic. No hardcoded storage.

2. **Concurrency.** ``AioSqliteProvider`` opens with ``WAL`` + ``synchronous=NORMAL`` +
   ``busy_timeout`` (ADD §3). Every read/write is ``aiosqlite``-offloaded onto a per-
   connection worker thread, so the FSM event loop never blocks on serialization or fsync.
   A single cached writer connection + an ``asyncio.Lock`` realizes WAL's "1 writer + N
   readers" sweet spot.

3. **Migration.** ``migrate_pickle_to_sqlite`` ingests a legacy ``codebase_graph.pkl`` once,
   streams it into the normalized schema in batched transactions, marks ``meta.migrated_from_pkl``,
   and archives (does not delete) the ``.pkl`` as a one-release rollback escape hatch. An empty
   state boots cold and is throttled by the Phase-1 AIMD backpressure already in ``oracle.py``.

``pickle`` here is internal-cache only (our own dataclasses, written by our own process) and is
read solely to migrate the legacy file forward — never untrusted data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pickle  # noqa: S403 — internal legacy cache migration only (trusted, self-written)
import shutil
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

try:
    import aiosqlite  # type: ignore

    AIOSQLITE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on stripped installs
    aiosqlite = None  # type: ignore
    AIOSQLITE_AVAILABLE = False


# ---------------------------------------------------------------------------- env knobs
def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def sqlite_persistence_enabled() -> bool:
    """Master switch. **Default ON** (graduated 2026-06-16): SQLite is the canonical Oracle memory
    layer. The Sovereign Memory Armor (AIMD memory-pressure throttle + streaming warm-load) makes a
    fresh cold index 16GB-safe by construction — it contracts and, at CRITICAL, suspends-durably
    rather than OOM. Kill switch ``=0`` restores the legacy monolithic-pickle path."""
    return _env_truthy("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", True)


def sqlite_migrate_pkl_enabled() -> bool:
    """**Default OFF** — the legacy ``.pkl`` → SQLite auto-migration is DEPRECATED. Materializing a
    large accreted pickle into a live DiGraph is the ~10GB memory monster this whole layer replaces;
    on a constrained host it OOMs. Default-ON persistence therefore cold-indexes FRESH, never auto-
    loads the pickle. This opt-in is for a provisioned host with headroom that explicitly wants the
    one-time ingest (``JARVIS_ORACLE_SQLITE_MIGRATE_PKL=1``)."""
    return _env_truthy("JARVIS_ORACLE_SQLITE_MIGRATE_PKL", False)


def sqlite_busy_timeout_ms() -> int:
    try:
        return max(0, int(os.environ.get("JARVIS_ORACLE_SQLITE_BUSY_TIMEOUT_MS", "5000")))
    except (TypeError, ValueError):
        return 5000


def sqlite_commit_every_n_files() -> int:
    """Coalesce small batches so we don't fsync per file; bounds at-most-one-batch loss."""
    try:
        return max(1, int(os.environ.get("JARVIS_ORACLE_SQLITE_COMMIT_EVERY_N_FILES", "50")))
    except (TypeError, ValueError):
        return 50


def sqlite_integrity_timeout_s() -> float:
    try:
        return max(0.0, float(os.environ.get("JARVIS_ORACLE_SQLITE_INTEGRITY_TIMEOUT_S", "10")))
    except (TypeError, ValueError):
        return 10.0


def sqlite_load_chunk() -> int:
    """``JARVIS_ORACLE_SQLITE_LOAD_CHUNK`` — warm-load streaming chunk size. The DiGraph is
    rebuilt incrementally via ``fetchmany(chunk)`` so the transient footprint is graph + ONE chunk
    of rows, never graph + the entire result set materialized as a Python list (the warm-boot
    spike). Default 2000."""
    try:
        return max(1, int(os.environ.get("JARVIS_ORACLE_SQLITE_LOAD_CHUNK", "2000")))
    except (TypeError, ValueError):
        return 2000


# ---------------------------------------------------------------------------- value object
@dataclass
class GraphState:
    """Storage-agnostic snapshot of everything ``TheOracle`` needs to persist/restore.

    Mirrors the seven fields the legacy ``_save_cache``/``_load_cache`` round-tripped, so the
    Oracle wiring is a thin assignment regardless of backend. ``graph`` is the live
    ``networkx.DiGraph``; the four indices + metrics are derived data a provider MAY rebuild
    on load rather than store (the SQLite provider rebuilds them from node rows).
    """

    graph: Any  # networkx.DiGraph
    node_index: Dict[str, Any] = field(default_factory=dict)       # node_key -> NodeID
    file_index: Dict[str, Set[str]] = field(default_factory=dict)  # file_path -> {node_key}
    repo_index: Dict[str, Set[str]] = field(default_factory=dict)  # repo -> {node_key}
    type_index: Dict[Any, Set[str]] = field(default_factory=dict)  # NodeType -> {node_key}
    metrics: Dict[str, Any] = field(default_factory=dict)
    file_hashes: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------- interface
class PersistenceProvider(ABC):
    """The seam the Oracle depends on. Backends implement this; the Oracle never imports one
    directly (it receives one from :func:`build_provider`)."""

    @abstractmethod
    async def exists(self) -> bool:
        """True if a persisted store is present on disk (not necessarily non-empty)."""

    @abstractmethod
    async def load(self) -> Optional[GraphState]:
        """Restore a full ``GraphState`` or ``None`` if the store is absent/empty/corrupt
        (``None`` deterministically triggers a Phase-1-throttled cold index — never a wedge)."""

    @abstractmethod
    async def save(self, state: GraphState) -> None:
        """Persist a full snapshot atomically (full overwrite)."""

    async def close(self) -> None:
        """Release any held resources. Default no-op."""
        return None


# ---------------------------------------------------------------------------- pickle (legacy)
class PickleProvider(PersistenceProvider):
    """Legacy monolithic-pickle backend — the exact pre-Phase-2 format. Retained as (a) the
    migration *source* and (b) a parity/rollback reference. Atomic write via temp + os.replace
    (mirrors the original ``_write_cache_blocking``)."""

    def __init__(self, path: Path | str):
        self._path = Path(path)

    async def exists(self) -> bool:
        return self._path.exists()

    async def load(self) -> Optional[GraphState]:
        if not self._path.exists():
            return None
        return await asyncio.to_thread(self._load_blocking)

    def _load_blocking(self) -> Optional[GraphState]:
        try:
            from collections import defaultdict

            raw = self._path.read_bytes()
            data = pickle.loads(raw)  # noqa: S301 — trusted internal cache
            del raw
            return GraphState(
                graph=data["graph"],
                node_index=data["node_index"],
                file_index=defaultdict(set, data["file_index"]),
                repo_index=defaultdict(set, data["repo_index"]),
                type_index=defaultdict(set, data["type_index"]),
                metrics=data["metrics"],
                file_hashes=data.get("file_hashes", {}),
            )
        except Exception as exc:  # noqa: BLE001 — corrupt legacy cache must never wedge boot
            logger.warning("[OraclePersist] legacy pickle load failed (non-fatal): %s", exc)
            return None

    async def save(self, state: GraphState) -> None:
        await asyncio.to_thread(self._save_blocking, state)

    def _save_blocking(self, state: GraphState) -> None:
        import tempfile

        data = {
            "graph": state.graph,
            "node_index": state.node_index,
            "file_index": dict(state.file_index),
            "repo_index": dict(state.repo_index),
            "type_index": dict(state.type_index),
            "metrics": state.metrics,
            "file_hashes": state.file_hashes,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self._path.name + ".", suffix=".tmp", dir=str(self._path.parent))
        os.close(fd)
        try:
            Path(tmp).write_bytes(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))
            os.replace(tmp, str(self._path))
            tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------------------- aiosqlite
class AioSqliteProvider(PersistenceProvider):
    """Normalized aiosqlite backend (ADD §4 schema). WAL + NORMAL + busy_timeout; one cached
    writer connection serialized by an ``asyncio.Lock``; every operation aiosqlite-offloaded."""

    SCHEMA_VERSION = 1

    _SCHEMA: Tuple[str, ...] = (
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
        (
            "CREATE TABLE IF NOT EXISTS nodes ("
            " node_key TEXT PRIMARY KEY, repo TEXT NOT NULL, file_path TEXT NOT NULL,"
            " name TEXT NOT NULL, node_type TEXT NOT NULL, line_number INTEGER NOT NULL DEFAULT 0,"
            " docstring TEXT, signature TEXT, decorators TEXT, base_classes TEXT,"
            " complexity INTEGER NOT NULL DEFAULT 0, line_count INTEGER NOT NULL DEFAULT 0,"
            " last_modified REAL NOT NULL DEFAULT 0, source_hash TEXT NOT NULL DEFAULT '')"
        ),
        "CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path)",
        "CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type)",
        (
            "CREATE TABLE IF NOT EXISTS edges ("
            " src_key TEXT NOT NULL, dst_key TEXT NOT NULL, edge_type TEXT NOT NULL,"
            " line_number INTEGER NOT NULL DEFAULT 0, context TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (src_key, dst_key, edge_type, line_number))"
        ),
        "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_key)",
        "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_key)",
        (
            "CREATE TABLE IF NOT EXISTS file_hashes ("
            " file_path TEXT PRIMARY KEY, source_hash TEXT NOT NULL, indexed_at REAL NOT NULL)"
        ),
    )

    def __init__(
        self,
        db_path: Path | str,
        *,
        busy_timeout_ms: Optional[int] = None,
        integrity_timeout_s: Optional[float] = None,
    ):
        if not AIOSQLITE_AVAILABLE:
            raise RuntimeError("aiosqlite is not installed — cannot use AioSqliteProvider")
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms if busy_timeout_ms is not None else sqlite_busy_timeout_ms()
        self._integrity_timeout_s = (
            integrity_timeout_s if integrity_timeout_s is not None else sqlite_integrity_timeout_s()
        )
        self._conn: Any = None
        self._wlock: Optional[asyncio.Lock] = None
        self._schema_ready: bool = False

    # -- connection lifecycle ------------------------------------------------
    def _lock(self) -> asyncio.Lock:
        if self._wlock is None:
            self._wlock = asyncio.Lock()
        return self._wlock

    async def _conn_or_open(self):
        if self._conn is None:
            assert aiosqlite is not None  # guaranteed by the __init__ availability check
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(self._db_path))
            # ADD §3 concurrency pragmas — WAL: N readers never block the 1 writer;
            # NORMAL: crash-durable under WAL; busy_timeout: retry transient overlap.
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            await conn.commit()
            self._conn = conn
        return self._conn

    async def _ensure_schema_once(self, conn) -> None:
        """Create the schema lazily — ONLY on write paths, ONCE per provider instance. Read paths
        (``load``/``get_meta``) never call this, so a reader connection stays truly read-only and
        cannot collide with a concurrent writer's lock (WAL's 1-writer/N-reader invariant)."""
        if self._schema_ready:
            return
        for stmt in self._SCHEMA:
            await conn.execute(stmt)
        await conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(self.SCHEMA_VERSION),),
        )
        await conn.commit()
        self._schema_ready = True

    @asynccontextmanager
    async def _write_txn(self, conn):
        """ACID write boundary (Phase 2). Serializes writers (asyncio.Lock), ensures schema,
        opens with ``BEGIN IMMEDIATE`` (grabs the write lock upfront so ``busy_timeout`` applies
        cleanly), and guarantees a deterministic async ``ROLLBACK`` if the body raises — no
        partial / orphaned rows ever survive an exception or mid-batch interrupt. The block that
        last cleanly committed is the rollback target."""
        async with self._lock():
            await self._ensure_schema_once(conn)
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                await conn.commit()
            except BaseException:
                try:
                    await conn.rollback()
                except Exception:  # noqa: BLE001 — rollback best-effort; original error wins
                    logger.warning("[OraclePersist] rollback failed after txn error", exc_info=True)
                raise

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    # -- integrity ladder ----------------------------------------------------
    async def _integrity_ok(self, conn) -> bool:
        """Bounded ``quick_check`` (ADD §6). Exceeding the deadline or a non-``ok`` result is
        treated as corrupt → caller quarantines + cold-rebuilds. Never wedges (wait_for-bound)."""
        try:
            async def _check() -> bool:
                async with conn.execute("PRAGMA quick_check") as cur:
                    row = await cur.fetchone()
                return bool(row) and str(row[0]).lower() == "ok"

            if self._integrity_timeout_s <= 0:
                return await _check()
            return await asyncio.wait_for(_check(), timeout=self._integrity_timeout_s)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            logger.warning("[OraclePersist] integrity check failed/timed out (treating as corrupt): %s", exc)
            return False

    def _quarantine(self) -> None:
        try:
            if self._db_path.exists():
                victim = self._db_path.with_suffix(self._db_path.suffix + f".corrupt.{int(time.time())}")
                shutil.move(str(self._db_path), str(victim))
                logger.warning("[OraclePersist] quarantined corrupt db → %s", victim)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[OraclePersist] quarantine failed (non-fatal): %s", exc)

    # -- interface -----------------------------------------------------------
    async def exists(self) -> bool:
        return self._db_path.exists()

    async def load(self) -> Optional[GraphState]:
        if not self._db_path.exists():
            return None
        try:
            conn = await self._conn_or_open()
        except Exception as exc:  # noqa: BLE001 — unopenable db == corrupt
            logger.warning("[OraclePersist] db open failed (treating as corrupt): %s", exc)
            await self.close()
            self._quarantine()
            return None

        if not await self._integrity_ok(conn):
            await self.close()
            self._quarantine()
            return None

        return await self._load_rows(conn)

    async def _load_rows(self, conn) -> Optional[GraphState]:
        # Lazy import to avoid a circular import (oracle imports this module).
        import networkx as nx

        from backend.core.ouroboros.oracle import NodeID, NodeType

        from collections import defaultdict

        graph = nx.DiGraph()
        node_index: Dict[str, Any] = {}
        file_index: Dict[str, Set[str]] = defaultdict(set)
        repo_index: Dict[str, Set[str]] = defaultdict(set)
        type_index: Dict[Any, Set[str]] = defaultdict(set)

        # A db file that exists but has no schema yet (created, not yet written) → cold index.
        try:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'"
            ) as cur:
                if await cur.fetchone() is None:
                    return None
        except Exception:  # noqa: BLE001
            return None

        # Phase-2 streaming warm-load: build the DiGraph incrementally via fetchmany(chunk) so the
        # transient footprint is graph + ONE chunk, never graph + the entire result set as a list
        # (the warm-boot RAM spike). Flat memory profile during reconstruction.
        chunk = sqlite_load_chunk()
        n_nodes = 0
        async with conn.execute(
            "SELECT node_key, repo, file_path, name, node_type, line_number, docstring, signature,"
            " decorators, base_classes, complexity, line_count, last_modified, source_hash FROM nodes"
        ) as cur:
            while True:
                rows = await cur.fetchmany(chunk)
                if not rows:
                    break
                for r in rows:
                    (node_key, repo, file_path, name, node_type, line_number, docstring, signature,
                     decorators, base_classes, complexity, line_count, last_modified, source_hash) = r
                    # Rebuild the EXACT attr shape add_node stored (NodeData.to_dict()) so the rest
                    # of the Oracle (get_node → dict(graph.nodes[k])) is byte-for-byte identical.
                    attrs = {
                        "node_id": {
                            "repo": repo, "file_path": file_path, "name": name,
                            "node_type": node_type, "line_number": line_number,
                        },
                        "docstring": docstring,
                        "signature": signature,
                        "decorators": json.loads(decorators) if decorators else [],
                        "base_classes": json.loads(base_classes) if base_classes else [],
                        "complexity": complexity,
                        "line_count": line_count,
                        "last_modified": last_modified,
                        "source_hash": source_hash,
                    }
                    graph.add_node(node_key, **attrs)
                    node_index[node_key] = NodeID(
                        repo=repo, file_path=file_path, name=name,
                        node_type=NodeType(node_type), line_number=line_number,
                    )
                    file_index[file_path].add(node_key)
                    repo_index[repo].add(node_key)
                    type_index[NodeType(node_type)].add(node_key)
                    n_nodes += 1

        if n_nodes == 0:
            return None  # empty store → cold index

        async with conn.execute(
            "SELECT src_key, dst_key, edge_type, line_number, context FROM edges"
        ) as cur:
            while True:
                rows = await cur.fetchmany(chunk)
                if not rows:
                    break
                for src_key, dst_key, edge_type, line_number, context in rows:
                    graph.add_edge(
                        src_key, dst_key,
                        edge_type=edge_type, line_number=line_number, context=context,
                    )

        async with conn.execute("SELECT file_path, source_hash FROM file_hashes") as cur:
            fh_rows = await cur.fetchall()
        file_hashes = {fp: sh for fp, sh in fh_rows}

        metrics = await self._read_metrics(conn)
        # Counts are authoritative from the live graph, not stored values.
        metrics["total_nodes"] = graph.number_of_nodes()
        metrics["total_edges"] = graph.number_of_edges()
        metrics.setdefault("files_indexed", len(file_index))

        return GraphState(
            graph=graph, node_index=node_index, file_index=file_index,
            repo_index=repo_index, type_index=type_index,
            metrics=metrics, file_hashes=file_hashes,
        )

    async def _read_metrics(self, conn) -> Dict[str, Any]:
        async with conn.execute("SELECT value FROM meta WHERE key='metrics'") as cur:
            row = await cur.fetchone()
        if row and row[0]:
            try:
                return dict(json.loads(row[0]))
            except (ValueError, TypeError):
                pass
        return {
            "total_nodes": 0, "total_edges": 0, "files_indexed": 0,
            "last_full_index": 0.0, "last_incremental_update": 0.0,
        }

    async def save(self, state: GraphState) -> None:
        """Full transactional snapshot overwrite. For the incremental hot path use
        :meth:`upsert_files` — but ``save`` keeps a correct full-write path for shutdown."""
        conn = await self._conn_or_open()
        node_rows = list(_iter_node_rows(state.graph))
        edge_rows = list(_iter_edge_rows(state.graph))
        now = time.time()
        fh_rows = [
            (fp, sh, state.metrics.get("last_incremental_update") or now)
            for fp, sh in state.file_hashes.items()
        ]
        metrics_json = json.dumps(state.metrics or {})
        async with self._write_txn(conn):
            await conn.execute("DELETE FROM nodes")
            await conn.execute("DELETE FROM edges")
            await conn.execute("DELETE FROM file_hashes")
            if node_rows:
                await conn.executemany(_INSERT_NODE_SQL, node_rows)
            if edge_rows:
                await conn.executemany(_INSERT_EDGE_SQL, edge_rows)
            if fh_rows:
                await conn.executemany(
                    "INSERT OR REPLACE INTO file_hashes (file_path, source_hash, indexed_at)"
                    " VALUES (?,?,?)",
                    fh_rows,
                )
            await conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES('metrics', ?)", (metrics_json,)
            )

    async def upsert_files(
        self,
        files: Dict[str, Dict[str, Any]],
        *,
        indexed_at: Optional[float] = None,
    ) -> None:
        """Phase-2 incremental hot path (ADD §5): per-file dirty replace in ONE ACID transaction —
        *every commit is a checkpoint*, no monolithic rewrite. The whole set commits or rolls back
        atomically (``_write_txn``).

        ``files`` maps the bare relative ``file_path`` (matches ``nodes.file_path``) → payload:
          - ``node_rows`` / ``edge_rows``: rows in the column order of the INSERTs
          - ``source_hash``: content hash for the file_hashes table
          - ``repo`` (optional): when present, the dirty-replace is scoped by ``(repo, file_path)``
            so two repos sharing a relative path don't clobber each other's nodes
          - ``hash_key`` (optional): the file_hashes key (the Oracle's ``repo:relative`` cache_key);
            defaults to ``file_path`` for the single-repo case
        """
        if not files:
            return
        conn = await self._conn_or_open()
        ts = indexed_at if indexed_at is not None else time.time()
        async with self._write_txn(conn):
            for file_path, payload in files.items():
                repo = payload.get("repo")
                # Edges have no file_path column (ADD §4) — resolve the file's *current* node
                # keys, then purge every edge touching them (both directions). Deleting by the OLD
                # keys is load-bearing: a stale outgoing edge would resurrect its deleted source
                # node as a bare stub on the next load. Scope by (repo, file_path) when repo given.
                if repo is not None:
                    sel = ("SELECT node_key FROM nodes WHERE file_path = ? AND repo = ?", (file_path, repo))
                    deln = ("DELETE FROM nodes WHERE file_path = ? AND repo = ?", (file_path, repo))
                else:
                    sel = ("SELECT node_key FROM nodes WHERE file_path = ?", (file_path,))
                    deln = ("DELETE FROM nodes WHERE file_path = ?", (file_path,))
                async with conn.execute(*sel) as cur:
                    old_keys = [r[0] for r in await cur.fetchall()]
                if old_keys:
                    ph = ",".join("?" * len(old_keys))
                    await conn.execute(
                        f"DELETE FROM edges WHERE src_key IN ({ph}) OR dst_key IN ({ph})",
                        old_keys + old_keys,
                    )
                await conn.execute(*deln)
                node_rows = payload.get("node_rows") or []
                edge_rows = payload.get("edge_rows") or []
                if node_rows:
                    await conn.executemany(_INSERT_NODE_SQL, node_rows)
                if edge_rows:
                    await conn.executemany(_INSERT_EDGE_SQL, edge_rows)
                await conn.execute(
                    "INSERT OR REPLACE INTO file_hashes (file_path, source_hash, indexed_at)"
                    " VALUES (?,?,?)",
                    (payload.get("hash_key", file_path), payload.get("source_hash", ""), ts),
                )

    async def set_meta(self, key: str, value: str) -> None:
        conn = await self._conn_or_open()
        async with self._lock():
            await self._ensure_schema_once(conn)
            await conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
            await conn.commit()

    async def get_meta(self, key: str) -> Optional[str]:
        conn = await self._conn_or_open()
        async with conn.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def checkpoint_wal(self) -> None:
        """Structural checkpoint — fold the ``-wal`` back into the main db (``TRUNCATE`` mode). Used by
        the Adaptive Scoper between subtree partitions so a partition is fully durable on disk and the
        ``-wal`` can't grow unbounded across a long sequential build. Serialized by the write lock."""
        conn = await self._conn_or_open()
        async with self._lock():
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await conn.commit()

    async def count_nodes_edges(self) -> Tuple[int, int]:
        """Authoritative ``(nodes, edges)`` counts from the canonical store — refreshes the in-memory
        metrics after a scoped+evicted build (where the resident counters are partial)."""
        conn = await self._conn_or_open()
        try:
            async with conn.execute("SELECT count(*) FROM nodes") as cur:
                n = (await cur.fetchone())[0]
            async with conn.execute("SELECT count(*) FROM edges") as cur:
                e = (await cur.fetchone())[0]
            return int(n), int(e)
        except Exception:  # noqa: BLE001 — no schema yet → zero
            return 0, 0


# ---------------------------------------------------------------------------- row marshalling
_INSERT_NODE_SQL = (
    "INSERT OR REPLACE INTO nodes (node_key, repo, file_path, name, node_type, line_number,"
    " docstring, signature, decorators, base_classes, complexity, line_count, last_modified,"
    " source_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_INSERT_EDGE_SQL = (
    "INSERT OR REPLACE INTO edges (src_key, dst_key, edge_type, line_number, context)"
    " VALUES (?,?,?,?,?)"
)


def _node_row(node_key: str, attrs: Dict[str, Any]):
    """One SQLite node row from a graph node's attr dict (column order matches _INSERT_NODE_SQL)."""
    nid = attrs.get("node_id") or {}
    return (
        node_key,
        nid.get("repo", ""),
        nid.get("file_path", ""),
        nid.get("name", ""),
        nid.get("node_type", "function"),
        int(nid.get("line_number", 0) or 0),
        attrs.get("docstring"),
        attrs.get("signature"),
        json.dumps(attrs.get("decorators") or []),
        json.dumps(attrs.get("base_classes") or []),
        int(attrs.get("complexity", 0) or 0),
        int(attrs.get("line_count", 0) or 0),
        float(attrs.get("last_modified", 0.0) or 0.0),
        attrs.get("source_hash", "") or "",
    )


def _edge_row(src: str, dst: str, attrs: Dict[str, Any]):
    """One SQLite edge row (column order matches _INSERT_EDGE_SQL)."""
    return (
        src,
        dst,
        attrs.get("edge_type", "calls"),
        int(attrs.get("line_number", 0) or 0),
        attrs.get("context", "") or "",
    )


def _iter_node_rows(graph):
    """Yield SQLite node rows for every node in a live DiGraph."""
    for node_key, attrs in graph.nodes(data=True):
        yield _node_row(node_key, attrs)


def _iter_edge_rows(graph):
    """Yield SQLite edge rows for every edge in a live DiGraph."""
    for src, dst, attrs in graph.edges(data=True):
        yield _edge_row(src, dst, attrs)


def node_rows_for_keys(graph, keys: Iterable[str]) -> List[tuple]:
    """SQLite node rows for a specific set of node keys (the incremental per-file extraction)."""
    rows: List[tuple] = []
    for k in keys:
        if k in graph.nodes:
            rows.append(_node_row(k, graph.nodes[k]))
    return rows


def edge_rows_for_keys(graph, keys: Iterable[str]) -> List[tuple]:
    """SQLite edge rows OUTGOING from a set of node keys (a file owns its outgoing edges)."""
    rows: List[tuple] = []
    for k in keys:
        if k in graph.nodes:
            for dst in graph.successors(k):
                rows.append(_edge_row(k, dst, graph.edges[k, dst]))
    return rows


# ---------------------------------------------------------------------------- migration
async def migrate_pickle_to_sqlite(pickle_path: Path | str, provider: AioSqliteProvider) -> bool:
    """One-time, idempotent legacy ingest (ADD §6). Streams the pickle into SQLite, marks
    ``meta.migrated_from_pkl``, and *archives* (renames, never deletes) the ``.pkl`` as a
    one-release rollback hatch. Returns True iff a migration was performed."""
    pickle_path = Path(pickle_path)
    if not pickle_path.exists():
        return False
    state = await PickleProvider(pickle_path).load()
    if state is None or state.graph is None or state.graph.number_of_nodes() == 0:
        logger.info("[OraclePersist] legacy pickle absent/empty — nothing to migrate")
        return False

    logger.info(
        "[OraclePersist] migrating legacy pickle → sqlite (%d nodes, %d edges)",
        state.graph.number_of_nodes(), state.graph.number_of_edges(),
    )
    await provider.save(state)
    await provider.set_meta("migrated_from_pkl", str(pickle_path))
    await provider.set_meta("migrated_at", str(time.time()))

    try:
        archive = pickle_path.with_suffix(pickle_path.suffix + ".migrated")
        shutil.move(str(pickle_path), str(archive))
        logger.info("[OraclePersist] archived legacy pickle → %s", archive)
    except Exception as exc:  # noqa: BLE001 — archive failure must not undo a good migration
        logger.warning("[OraclePersist] pickle archive failed (non-fatal): %s", exc)
    return True


# ---------------------------------------------------------------------------- factory
def build_provider(
    *,
    db_path: Path | str,
    pickle_path: Path | str,
    enabled: Optional[bool] = None,
) -> Optional[PersistenceProvider]:
    """Single decision point for the storage backend. Returns an ``AioSqliteProvider`` when the
    master switch is on AND aiosqlite is importable; otherwise ``None`` so the Oracle keeps its
    legacy pickle path verbatim (byte-identical rollback). No hardcoding — the Oracle asks the
    factory and adapts."""
    use = sqlite_persistence_enabled() if enabled is None else enabled
    if not use:
        return None
    if not AIOSQLITE_AVAILABLE:
        logger.warning("[OraclePersist] sqlite persistence requested but aiosqlite missing — using legacy pickle")
        return None
    return AioSqliteProvider(db_path)
