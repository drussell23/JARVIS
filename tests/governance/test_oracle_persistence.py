"""Phase 2 — Interface-segregated SQLite persistence for the Oracle.

Validates the three mandated constraints against REAL aiosqlite + a REAL networkx graph built
from the production dataclasses (no mocks):
  1. Interface segregation — provider round-trips a GraphState the Oracle can consume verbatim.
  2. Concurrency — WAL/NORMAL/busy_timeout actually set; reads don't block under a write.
  3. Migration — legacy pickle ingested, archived, idempotent; empty boots cold.
"""
from __future__ import annotations

import asyncio

import pytest

import backend.core.ouroboros.oracle_persistence as P
from backend.core.ouroboros.oracle import (
    CodebaseKnowledgeGraph,
    EdgeData,
    EdgeType,
    NodeData,
    NodeID,
    NodeType,
)


# --------------------------------------------------------------------------- fixtures
def _build_graph(n_files: int = 3) -> CodebaseKnowledgeGraph:
    g = CodebaseKnowledgeGraph()
    prev = None
    for f in range(n_files):
        fp = f"backend/pkg/module_{f}.py"
        for e in range(2):
            nid = NodeID(repo="jarvis", file_path=fp, name=f"sym_{f}_{e}",
                         node_type=NodeType.FUNCTION, line_number=e + 1)
            g.add_node(NodeData(node_id=nid, docstring=f"doc {f}.{e}", signature="(x: int)",
                                decorators=["@cache"], base_classes=[], complexity=e,
                                line_count=10 + e, last_modified=float(f), source_hash=f"h{f}{e}"))
            if prev is not None:
                g.add_edge(prev, nid, EdgeData(EdgeType.CALLS, line_number=e, context="ctx"))
            prev = nid
    return g


def _state_from_graph(g: CodebaseKnowledgeGraph, file_hashes=None) -> P.GraphState:
    return P.GraphState(
        graph=g._graph, node_index=g._node_index, file_index=g._file_index,
        repo_index=g._repo_index, type_index=g._type_index, metrics=g._metrics,
        file_hashes=file_hashes or {"backend/pkg/module_0.py": "h00"},
    )


# --------------------------------------------------------------------------- 1. round-trip
def test_sqlite_roundtrip_preserves_graph(tmp_path):
    async def run():
        g = _build_graph(3)
        n_nodes, n_edges = g._graph.number_of_nodes(), g._graph.number_of_edges()
        prov = P.AioSqliteProvider(tmp_path / "oracle.db")
        await prov.save(_state_from_graph(g))
        loaded = await prov.load()
        await prov.close()
        assert loaded is not None
        assert loaded.graph.number_of_nodes() == n_nodes
        assert loaded.graph.number_of_edges() == n_edges
        # indices rebuilt
        assert set(loaded.node_index) == set(g._node_index)
        assert set(loaded.file_index) == set(g._file_index)
        assert loaded.type_index[NodeType.FUNCTION] == g._type_index[NodeType.FUNCTION]
        # node attrs byte-identical to what add_node stored
        for k in g._graph.nodes:
            assert dict(loaded.graph.nodes[k]) == dict(g._graph.nodes[k])
        # NodeID objects faithfully reconstructed
        for k, nid in g._node_index.items():
            assert loaded.node_index[k] == nid
    asyncio.run(run())


def test_sqlite_roundtrip_preserves_edges_and_hashes(tmp_path):
    async def run():
        g = _build_graph(2)
        prov = P.AioSqliteProvider(tmp_path / "oracle.db")
        await prov.save(_state_from_graph(g, file_hashes={"a.py": "h1", "b.py": "h2"}))
        loaded = await prov.load()
        await prov.close()
        assert loaded.file_hashes == {"a.py": "h1", "b.py": "h2"}
        for s, d, attrs in g._graph.edges(data=True):
            assert dict(loaded.graph.edges[s, d]) == dict(attrs)
    asyncio.run(run())


def test_empty_db_loads_as_none(tmp_path):
    async def run():
        prov = P.AioSqliteProvider(tmp_path / "oracle.db")
        # save an empty graph
        import networkx as nx
        await prov.save(P.GraphState(graph=nx.DiGraph()))
        loaded = await prov.load()
        await prov.close()
        assert loaded is None  # empty store → cold index
    asyncio.run(run())


def test_absent_db_loads_as_none(tmp_path):
    async def run():
        prov = P.AioSqliteProvider(tmp_path / "nope.db")
        assert await prov.load() is None
        assert await prov.exists() is False
        await prov.close()
    asyncio.run(run())


# --------------------------------------------------------------------------- 2. concurrency
def test_pragmas_are_set(tmp_path):
    async def run():
        prov = P.AioSqliteProvider(tmp_path / "oracle.db", busy_timeout_ms=4321)
        conn = await prov._conn_or_open()
        async with conn.execute("PRAGMA journal_mode") as cur:
            jm = (await cur.fetchone())[0]
        async with conn.execute("PRAGMA synchronous") as cur:
            sync = (await cur.fetchone())[0]
        async with conn.execute("PRAGMA busy_timeout") as cur:
            bt = (await cur.fetchone())[0]
        await prov.close()
        assert str(jm).lower() == "wal"
        assert int(sync) == 1          # NORMAL == 1
        assert int(bt) == 4321
    asyncio.run(run())


def test_concurrent_read_during_write_no_lock_error(tmp_path):
    """WAL's real guarantee: a SEPARATE reader connection sees committed data while the writer
    connection is mid-flight, with zero 'database is locked'. Two providers = two aiosqlite
    connections = two worker threads (the production-faithful 1-writer + N-readers shape)."""
    db = tmp_path / "oracle.db"

    async def run():
        g = _build_graph(4)
        writer_prov = P.AioSqliteProvider(db)
        await writer_prov.save(_state_from_graph(g))
        reader_prov = P.AioSqliteProvider(db)

        async def writer():
            for i in range(8):
                await writer_prov.upsert_files({
                    f"backend/pkg/new_{i}.py": {
                        "node_rows": list(P._iter_node_rows(_one_node_graph(i))),
                        "edge_rows": [],
                        "source_hash": f"nh{i}",
                    }
                })

        async def reader():
            errs = 0
            for _ in range(20):
                try:
                    loaded = await reader_prov.load()
                    assert loaded is not None
                except Exception:  # noqa: BLE001
                    errs += 1
                await asyncio.sleep(0)
            return errs

        _, errs = await asyncio.gather(writer(), reader())
        await writer_prov.close()
        await reader_prov.close()
        return errs

    assert asyncio.run(run()) == 0


def _one_node_graph(i: int):
    import networkx as nx
    g = nx.DiGraph()
    nid = NodeID(repo="jarvis", file_path=f"backend/pkg/new_{i}.py", name=f"n{i}",
                 node_type=NodeType.FUNCTION, line_number=1)
    g.add_node(str(nid), **NodeData(node_id=nid).to_dict())
    return g


# --------------------------------------------------------------------------- incremental
def test_upsert_files_is_per_file_dirty_replace(tmp_path):
    async def run():
        g = _build_graph(3)
        prov = P.AioSqliteProvider(tmp_path / "oracle.db")
        await prov.save(_state_from_graph(g))
        before = await prov.load()
        n0 = before.graph.number_of_nodes()

        # re-index ONE file with a single replacement node
        import networkx as nx
        fg = nx.DiGraph()
        nid = NodeID(repo="jarvis", file_path="backend/pkg/module_0.py", name="only",
                     node_type=NodeType.FUNCTION, line_number=1)
        fg.add_node(str(nid), **NodeData(node_id=nid, source_hash="new").to_dict())
        await prov.upsert_files({
            "backend/pkg/module_0.py": {
                "node_rows": list(P._iter_node_rows(fg)),
                "edge_rows": [],
                "source_hash": "newhash",
            }
        })
        after = await prov.load()
        await prov.close()
        # module_0 had 2 nodes, now 1 → net -1
        assert after.graph.number_of_nodes() == n0 - 1
        assert after.file_hashes["backend/pkg/module_0.py"] == "newhash"
        assert str(nid) in after.graph.nodes
    asyncio.run(run())


# --------------------------------------------------------------------------- 3. migration
def test_migration_ingests_and_archives_pickle(tmp_path):
    async def run():
        g = _build_graph(3)
        pkl = tmp_path / "codebase_graph.pkl"
        await P.PickleProvider(pkl).save(_state_from_graph(g))
        assert pkl.exists()

        db = tmp_path / "oracle.db"
        prov = P.AioSqliteProvider(db)
        did = await P.migrate_pickle_to_sqlite(pkl, prov)
        assert did is True

        loaded = await prov.load()
        assert loaded.graph.number_of_nodes() == g._graph.number_of_nodes()
        assert await prov.get_meta("migrated_from_pkl") == str(pkl)
        await prov.close()

        # pickle archived, not deleted
        assert not pkl.exists()
        assert (tmp_path / "codebase_graph.pkl.migrated").exists()
    asyncio.run(run())


def test_migration_idempotent_when_no_pickle(tmp_path):
    async def run():
        prov = P.AioSqliteProvider(tmp_path / "oracle.db")
        did = await P.migrate_pickle_to_sqlite(tmp_path / "absent.pkl", prov)
        await prov.close()
        assert did is False
    asyncio.run(run())


def test_pickle_provider_roundtrip(tmp_path):
    async def run():
        g = _build_graph(2)
        pkl = tmp_path / "c.pkl"
        pp = P.PickleProvider(pkl)
        await pp.save(_state_from_graph(g))
        loaded = await pp.load()
        assert loaded.graph.number_of_nodes() == g._graph.number_of_nodes()
        assert set(loaded.node_index) == set(g._node_index)
    asyncio.run(run())


# --------------------------------------------------------------------------- corruption ladder
def test_corrupt_db_is_quarantined_and_returns_none(tmp_path):
    async def run():
        db = tmp_path / "oracle.db"
        # write garbage that isn't a valid sqlite file
        db.write_bytes(b"this is not a sqlite database at all \x00\x01\x02")
        prov = P.AioSqliteProvider(db, integrity_timeout_s=5.0)
        loaded = await prov.load()
        await prov.close()
        assert loaded is None
        # original quarantined away
        assert not db.exists()
        assert any(p.name.startswith("oracle.db.corrupt.") for p in tmp_path.iterdir())
    asyncio.run(run())


# --------------------------------------------------------------------------- factory + flags
def test_factory_off_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "0")  # explicit kill switch
    assert P.build_provider(db_path=tmp_path / "o.db", pickle_path=tmp_path / "o.pkl") is None


def test_factory_on_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", raising=False)  # graduated default-ON
    prov = P.build_provider(db_path=tmp_path / "o.db", pickle_path=tmp_path / "o.pkl")
    assert isinstance(prov, P.AioSqliteProvider)


def test_flag_default_on(monkeypatch):
    monkeypatch.delenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", raising=False)
    assert P.sqlite_persistence_enabled() is True   # graduated 2026-06-16


def test_flag_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "0")
    assert P.sqlite_persistence_enabled() is False


def test_migrate_pkl_default_off(monkeypatch):
    """The legacy .pkl auto-migration is DEPRECATED — default OFF (opt-in only)."""
    monkeypatch.delenv("JARVIS_ORACLE_SQLITE_MIGRATE_PKL", raising=False)
    assert P.sqlite_migrate_pkl_enabled() is False
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_MIGRATE_PKL", "1")
    assert P.sqlite_migrate_pkl_enabled() is True


def test_busy_timeout_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_BUSY_TIMEOUT_MS", "9999")
    assert P.sqlite_busy_timeout_ms() == 9999


# --------------------------------------------------------------------------- Oracle wiring
def _fresh_oracle(monkeypatch, tmp_path):
    """A bare TheOracle with its persistence paths redirected to tmp (no heavy init)."""
    from backend.core.ouroboros.oracle import TheOracle

    monkeypatch.setattr(TheOracle, "_resolved_sqlite_path",
                        staticmethod(lambda: tmp_path / "oracle.db"))
    monkeypatch.setattr(TheOracle, "_resolved_graph_cache_path",
                        staticmethod(lambda: tmp_path / "codebase_graph.pkl"))
    return TheOracle()


def _populate(oracle, n=3):
    g = _build_graph(n)
    oracle._graph = g
    oracle._file_hashes = {"backend/pkg/module_0.py": "h00"}
    return g


def test_oracle_off_uses_legacy_pickle(monkeypatch, tmp_path):
    """Kill switch =0 → the .pkl is written, the provider is never even built (legacy path)."""
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "0")

    async def run():
        o = _fresh_oracle(monkeypatch, tmp_path)
        _populate(o)
        await o._save_cache()
        assert (tmp_path / "codebase_graph.pkl").exists()
        assert not (tmp_path / "oracle.db").exists()
        assert o._persistence is None and o._persistence_built is False
    asyncio.run(run())


def test_oracle_on_roundtrips_through_sqlite(monkeypatch, tmp_path):
    """Master ON → save writes the db (no .pkl), a fresh Oracle restores the graph from it."""
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "1")

    async def run():
        o = _fresh_oracle(monkeypatch, tmp_path)
        g = _populate(o)
        nn = g._graph.number_of_nodes()
        await o._save_cache()
        await o._persistence.close()
        assert (tmp_path / "oracle.db").exists()
        assert not (tmp_path / "codebase_graph.pkl").exists()

        o2 = _fresh_oracle(monkeypatch, tmp_path)
        ok = await o2._load_cache()
        await o2._persistence.close()
        assert ok is True
        assert o2._graph._graph.number_of_nodes() == nn
        assert set(o2._graph._node_index) == set(g._node_index)
    asyncio.run(run())


def test_oracle_on_migrates_legacy_pickle_only_with_optin(monkeypatch, tmp_path):
    """Master ON + legacy .pkl + no db + MIGRATE opt-in → first load migrates (ingest + archive)."""
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_MIGRATE_PKL", "1")  # explicit opt-in

    async def run():
        seed = _fresh_oracle(monkeypatch, tmp_path)
        g = _populate(seed)
        nn = g._graph.number_of_nodes()
        await P.PickleProvider(tmp_path / "codebase_graph.pkl").save(_state_from_graph(g))
        assert (tmp_path / "codebase_graph.pkl").exists()

        o = _fresh_oracle(monkeypatch, tmp_path)
        ok = await o._load_cache()
        await o._persistence.close()
        assert ok is True
        assert o._graph._graph.number_of_nodes() == nn
        assert (tmp_path / "oracle.db").exists()
        assert not (tmp_path / "codebase_graph.pkl").exists()
        assert (tmp_path / "codebase_graph.pkl.migrated").exists()
    asyncio.run(run())


def test_oracle_default_on_does_NOT_auto_load_legacy_pickle(monkeypatch, tmp_path):
    """THE TRAP ERADICATION: master ON (default) + a legacy .pkl present + no db + migration NOT
    opted-in → the Oracle must NOT touch the pickle (no ~10GB load). _load_cache returns False so
    the FSM cold-indexes fresh, and the .pkl is left untouched."""
    monkeypatch.delenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", raising=False)  # default ON
    monkeypatch.delenv("JARVIS_ORACLE_SQLITE_MIGRATE_PKL", raising=False)          # default OFF

    async def run():
        seed = _fresh_oracle(monkeypatch, tmp_path)
        g = _populate(seed)
        await P.PickleProvider(tmp_path / "codebase_graph.pkl").save(_state_from_graph(g))
        assert (tmp_path / "codebase_graph.pkl").exists()

        o = _fresh_oracle(monkeypatch, tmp_path)
        ok = await o._load_cache()           # db absent + migration off → cold-index signal
        if o._persistence is not None:
            await o._persistence.close()
        assert ok is False                                       # no load → fresh cold index
        assert (tmp_path / "codebase_graph.pkl").exists()        # pickle untouched (NOT migrated)
        assert not (tmp_path / "codebase_graph.pkl.migrated").exists()
    asyncio.run(run())


# --------------------------------------------------------------------------- Phase 2 hot path
def test_write_txn_rolls_back_on_error(tmp_path):
    """ACID guard: an exception inside `_write_txn` rolls the whole batch back to the last
    clean commit — no partial/orphaned rows survive."""
    async def run():
        g = _build_graph(3)
        prov = P.AioSqliteProvider(tmp_path / "o.db")
        await prov.save(_state_from_graph(g))
        n0 = (await prov.load()).graph.number_of_nodes()
        conn = await prov._conn_or_open()
        with pytest.raises(RuntimeError):
            async with prov._write_txn(conn):
                await conn.execute("DELETE FROM nodes")   # destructive...
                raise RuntimeError("boom mid-batch")        # ...then blow up
        after = await prov.load()
        await prov.close()
        assert after.graph.number_of_nodes() == n0          # rolled back, nothing lost
    asyncio.run(run())


def test_upsert_files_is_repo_scoped(tmp_path):
    """Two repos sharing a relative path must not clobber each other on per-file dirty replace."""
    import networkx as nx

    def one(repo, rel, name):
        g = nx.DiGraph()
        nid = NodeID(repo=repo, file_path=rel, name=name, node_type=NodeType.FUNCTION, line_number=1)
        g.add_node(str(nid), **NodeData(node_id=nid).to_dict())
        return g

    async def run():
        # seed: repo A and repo B both own "shared.py"
        combined = nx.compose(one("A", "shared.py", "a_fn"), one("B", "shared.py", "b_fn"))
        prov = P.AioSqliteProvider(tmp_path / "o.db")
        await prov.save(P.GraphState(graph=combined))
        # re-index ONLY repo A's shared.py (replace a_fn -> a_fn2)
        await prov.upsert_files({
            "shared.py": {
                "repo": "A",
                "hash_key": "A:shared.py",
                "source_hash": "newA",
                "node_rows": list(P._iter_node_rows(one("A", "shared.py", "a_fn2"))),
                "edge_rows": [],
            }
        })
        loaded = await prov.load()
        await prov.close()
        keys = set(loaded.graph.nodes)
        assert "B:shared.py:b_fn" in keys      # repo B untouched
        assert "A:shared.py:a_fn2" in keys     # repo A replaced
        assert "A:shared.py:a_fn" not in keys  # old A node gone
    asyncio.run(run())


def test_upsert_files_hash_key_distinct_from_file_path(tmp_path):
    """file_hashes is keyed by the Oracle cache_key (repo:relative) while nodes.file_path is the
    bare relative path — the upsert must honor both via `hash_key`."""
    import networkx as nx

    def one(repo, rel, name):
        g = nx.DiGraph()
        nid = NodeID(repo=repo, file_path=rel, name=name, node_type=NodeType.FUNCTION, line_number=1)
        g.add_node(str(nid), **NodeData(node_id=nid).to_dict())
        return g

    async def run():
        prov = P.AioSqliteProvider(tmp_path / "o.db")
        await prov.upsert_files({
            "pkg/mod.py": {
                "repo": "jarvis", "hash_key": "jarvis:pkg/mod.py", "source_hash": "h1",
                "node_rows": list(P._iter_node_rows(one("jarvis", "pkg/mod.py", "f"))),
                "edge_rows": [],
            }
        })
        loaded = await prov.load()
        await prov.close()
        assert loaded.file_hashes == {"jarvis:pkg/mod.py": "h1"}   # cache_key form preserved
    asyncio.run(run())


def test_extraction_helpers():
    g = _build_graph(2)._graph
    keys = list(g.nodes)[:2]
    nrows = P.node_rows_for_keys(g, keys)
    erows = P.edge_rows_for_keys(g, keys)
    assert len(nrows) == 2
    assert all(r[0] in keys for r in nrows)
    assert all(r[0] in keys for r in erows)  # only OUTGOING edges from the given keys


def test_oracle_incremental_checkpoint_extracts_and_commits(monkeypatch, tmp_path):
    """The Oracle's `_sqlite_incremental_checkpoint` extracts the batch's files from the live graph
    and commits them incrementally (no full rewrite)."""
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "1")
    from backend.core.ouroboros.oracle import TheOracle

    monkeypatch.setattr(TheOracle, "_resolved_sqlite_path",
                        staticmethod(lambda: tmp_path / "oracle.db"))
    monkeypatch.setattr(TheOracle, "_resolved_graph_cache_path",
                        staticmethod(lambda: tmp_path / "codebase_graph.pkl"))

    async def run():
        o = TheOracle()
        # populate the live graph as if a batch had been indexed
        g = _build_graph(3)
        o._graph = g
        o._file_hashes = {f"jarvis:backend/pkg/module_{f}.py": f"h{f}" for f in range(3)}
        o._graph_write_queue = None  # queue disabled → drain is a no-op
        batch = [tmp_path / "repo" / "backend/pkg/module_0.py",
                 tmp_path / "repo" / "backend/pkg/module_1.py"]
        commit_ms = await o._sqlite_incremental_checkpoint(batch, "jarvis", tmp_path / "repo")
        assert commit_ms >= 0.0
        prov = o._persistence_provider()
        loaded = await prov.load()
        await prov.close()
        # only module_0 + module_1 committed (2 files x 2 nodes). module_2 is NOT a committed row
        # — it only appears as an in-memory STUB (no node_id attr) auto-vivified from module_1's
        # cross-file edge on load; it gains a real row only when module_2 is itself indexed.
        assert loaded is not None
        files = {
            loaded.graph.nodes[k]["node_id"]["file_path"]
            for k in loaded.graph.nodes if "node_id" in loaded.graph.nodes[k]
        }
        assert "backend/pkg/module_0.py" in files
        assert "backend/pkg/module_1.py" in files
        assert "backend/pkg/module_2.py" not in files  # not committed — only an edge-target stub
    asyncio.run(run())


# --------------------------------------------------------------------------- streaming warm-load
def test_streaming_load_chunk_boundary(tmp_path, monkeypatch):
    """Warm-load via fetchmany must reconstruct identically across chunk boundaries (chunk smaller
    than the row count exercises the multi-fetch path)."""
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_LOAD_CHUNK", "7")
    async def run():
        g = _build_graph(10)  # 20 nodes >> chunk of 7 → ≥3 fetchmany rounds
        prov = P.AioSqliteProvider(tmp_path / "o.db")
        await prov.save(_state_from_graph(g))
        loaded = await prov.load()
        await prov.close()
        assert loaded.graph.number_of_nodes() == g._graph.number_of_nodes()
        assert loaded.graph.number_of_edges() == g._graph.number_of_edges()
        assert set(loaded.node_index) == set(g._node_index)
    asyncio.run(run())


def test_load_chunk_env_default():
    import os as _os
    _os.environ.pop("JARVIS_ORACLE_SQLITE_LOAD_CHUNK", None)
    assert P.sqlite_load_chunk() == 2000


# --------------------------------------------------------------------------- memory armor
class _FakeGate:
    """A MemoryPressureGate stand-in that returns a scripted sequence of pressure levels."""
    def __init__(self, levels):
        self._levels = list(levels)
        self._i = 0

    def pressure(self):
        lvl = self._levels[min(self._i, len(self._levels) - 1)]
        self._i += 1
        return lvl


def _oracle_with_gate(monkeypatch, tmp_path, levels):
    from backend.core.ouroboros.oracle import TheOracle
    monkeypatch.setattr(TheOracle, "_resolved_sqlite_path", staticmethod(lambda: tmp_path / "o.db"))
    monkeypatch.setattr(TheOracle, "_resolved_graph_cache_path", staticmethod(lambda: tmp_path / "c.pkl"))
    o = TheOracle()
    o._memory_gate_ref = _FakeGate(levels)   # pre-seed so _memory_gate() returns the fake
    return o


def test_armor_maps_levels(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel as L
    for lvl, expect in [(L.OK, "ok"), (L.WARN, "warn"), (L.HIGH, "high")]:
        o = _oracle_with_gate(monkeypatch, tmp_path, [lvl])
        assert asyncio.run(o._memory_armor_check()) == expect


def test_armor_critical_persist_when_never_clears(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel as L
    monkeypatch.setenv("JARVIS_ORACLE_MEMORY_ARMOR_MAX_YIELDS", "2")
    monkeypatch.setenv("JARVIS_ORACLE_MEMORY_ARMOR_YIELD_S", "0.05")
    o = _oracle_with_gate(monkeypatch, tmp_path, [L.CRITICAL] * 10)
    assert asyncio.run(o._memory_armor_check()) == "critical_persist"
    assert o._mem_armor_yields == 2  # forced GC+yield exactly max_yields times


def test_armor_critical_then_clears(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel as L
    monkeypatch.setenv("JARVIS_ORACLE_MEMORY_ARMOR_MAX_YIELDS", "3")
    monkeypatch.setenv("JARVIS_ORACLE_MEMORY_ARMOR_YIELD_S", "0.05")
    # first probe CRITICAL → enter yield loop; next probe OK → clears as "critical" (contract hard)
    o = _oracle_with_gate(monkeypatch, tmp_path, [L.CRITICAL, L.OK])
    assert asyncio.run(o._memory_armor_check()) == "critical"


def test_armor_disabled_via_gate(monkeypatch, tmp_path):
    from backend.core.ouroboros.oracle import TheOracle
    monkeypatch.setattr(TheOracle, "_resolved_sqlite_path", staticmethod(lambda: tmp_path / "o.db"))
    monkeypatch.setattr(TheOracle, "_resolved_graph_cache_path", staticmethod(lambda: tmp_path / "c.pkl"))
    o = TheOracle()
    monkeypatch.setattr("backend.core.ouroboros.governance.memory_pressure_gate.is_enabled",
                        lambda: False)
    # _memory_gate() returns None → armor is a clean no-op
    assert asyncio.run(o._memory_armor_check()) == "ok"


def test_armor_flag_default_on(monkeypatch):
    import backend.core.ouroboros.oracle as O
    monkeypatch.delenv("JARVIS_ORACLE_MEMORY_ARMOR_ENABLED", raising=False)
    assert O._oracle_memory_armor_enabled() is True
    monkeypatch.setenv("JARVIS_ORACLE_MEMORY_ARMOR_ENABLED", "0")
    assert O._oracle_memory_armor_enabled() is False


# --------------------------------------------------------------------------- Adaptive Subtree Scoper
def test_cluster_by_package_respects_target_and_covers_all():
    from pathlib import Path
    import backend.core.ouroboros.oracle as O
    repo = Path("/repo")
    files = []
    for pkg in ("a", "b", "c"):
        for i in range(40):
            files.append(repo / "backend" / pkg / f"m{i}.py")
    parts = O._cluster_by_package(files, repo, target_files=50)
    # every partition within target (these packages are 40 each, splittable into the 50 budget)
    assert all(len(p) <= 50 for p in parts)
    # lossless: union of partitions == input set
    flat = [f for p in parts for f in p]
    assert sorted(flat) == sorted(files)
    assert len(parts) >= 2  # 120 files / 50 budget → multiple partitions


def test_cluster_oversized_single_package_splits_deeper():
    from pathlib import Path
    import backend.core.ouroboros.oracle as O
    repo = Path("/repo")
    # one package, but with deeper sub-structure → must split on depth+1
    files = [repo / "pkg" / sub / f"m{i}.py" for sub in ("x", "y", "z") for i in range(30)]
    parts = O._cluster_by_package(files, repo, target_files=40)
    assert all(len(p) <= 40 for p in parts)
    assert sorted(f for p in parts for f in p) == sorted(files)


def test_partition_subtrees_off_returns_single(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_ORACLE_ADAPTIVE_SCOPER_ENABLED", raising=False)
    o = _fresh_oracle(monkeypatch, tmp_path)
    files = [tmp_path / f"m{i}.py" for i in range(1000)]
    assert o._partition_subtrees(files, tmp_path) == [files]


def test_partition_subtrees_on_splits(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORACLE_ADAPTIVE_SCOPER_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ORACLE_SCOPER_MIN_PARTITION_FILES", "10")
    # force a tiny budget so partitioning definitely triggers, regardless of host RAM
    monkeypatch.setenv("JARVIS_ORACLE_SCOPER_SAFETY_FRAC", "0.05")
    monkeypatch.setenv("JARVIS_ORACLE_SCOPER_PER_NODE_KB", "5000")  # huge per-node cost → tiny target
    o = _fresh_oracle(monkeypatch, tmp_path)
    files = [tmp_path / "pkg" / f"p{i % 6}" / f"m{i}.py" for i in range(600)]
    parts = o._partition_subtrees(files, tmp_path)
    assert len(parts) >= 2
    assert sorted(f for p in parts for f in p) == sorted(files)


def test_evict_partition_drops_nodes_keeps_file_hashes(monkeypatch, tmp_path):
    o = _fresh_oracle(monkeypatch, tmp_path)
    g = _build_graph(3)            # files backend/pkg/module_0..2.py, 2 nodes each
    o._graph = g
    o._file_hashes = {f"jarvis:backend/pkg/module_{i}.py": f"h{i}" for i in range(3)}
    repo = tmp_path
    f0 = repo / "backend/pkg/module_0.py"
    n_before = g._graph.number_of_nodes()
    evicted = o._evict_partition([f0], repo, "jarvis")
    assert evicted == 2                                   # module_0 had 2 nodes
    assert g._graph.number_of_nodes() == n_before - 2     # removed from in-memory graph
    assert "backend/pkg/module_0.py" not in g._file_index # index bucket cleaned
    assert o._file_hashes == {f"jarvis:backend/pkg/module_{i}.py": f"h{i}" for i in range(3)}  # PRESERVED


def test_scoped_eviction_preserves_cross_partition_edge_in_sqlite(monkeypatch, tmp_path):
    """THE correctness proof (Phase 3): index package A → commit → EVICT A → index package B with a
    cross-edge B→A → commit. The full graph (A real node + B + B→A edge) must reconstruct from
    SQLite byte-identically, proving eviction never loses durable cross-partition structure."""
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "1")
    from backend.core.ouroboros.oracle import TheOracle
    monkeypatch.setattr(TheOracle, "_resolved_sqlite_path", staticmethod(lambda: tmp_path / "oracle.db"))
    monkeypatch.setattr(TheOracle, "_resolved_graph_cache_path", staticmethod(lambda: tmp_path / "c.pkl"))
    repo = tmp_path

    async def run():
        o = TheOracle()
        o._graph_write_queue = None
        g = o._graph
        a1 = NodeID(repo="jarvis", file_path="pkg_a/a.py", name="fa", node_type=NodeType.FUNCTION, line_number=1)
        b1 = NodeID(repo="jarvis", file_path="pkg_b/b.py", name="fb", node_type=NodeType.FUNCTION, line_number=1)

        # --- partition A: index + commit + EVICT ---
        g.add_node(NodeData(node_id=a1, source_hash="ha"))
        o._file_hashes["jarvis:pkg_a/a.py"] = "ha"
        await o._sqlite_incremental_checkpoint([repo / "pkg_a/a.py"], "jarvis", repo)
        assert o._evict_partition([repo / "pkg_a/a.py"], repo, "jarvis") == 1
        assert str(a1) not in g._graph                        # A evicted from RAM

        # --- partition B: index with a cross-edge B->A (A is currently a RAM stub) + commit ---
        g.add_node(NodeData(node_id=b1, source_hash="hb"))
        g.add_edge(b1, a1, EdgeData(EdgeType.CALLS, line_number=2, context="cross"))
        o._file_hashes["jarvis:pkg_b/b.py"] = "hb"
        await o._sqlite_incremental_checkpoint([repo / "pkg_b/b.py"], "jarvis", repo)

        # --- reconstruct the FULL graph from SQLite ---
        prov = o._persistence_provider()
        loaded = await prov.load()
        await prov.close()
        keys = set(loaded.graph.nodes)
        assert str(a1) in keys and str(b1) in keys               # both real nodes present
        assert "node_id" in loaded.graph.nodes[str(a1)]          # A is a REAL node (not lost to eviction)
        assert loaded.graph.has_edge(str(b1), str(a1))           # cross-partition edge survived
        assert loaded.graph.number_of_nodes() == 2 and loaded.graph.number_of_edges() == 1
    asyncio.run(run())


def test_scoper_flag_default_on(monkeypatch):
    import backend.core.ouroboros.oracle as O
    monkeypatch.delenv("JARVIS_ORACLE_ADAPTIVE_SCOPER_ENABLED", raising=False)
    assert O._oracle_scoper_enabled() is True        # graduated default-ON 2026-06-18
    monkeypatch.setenv("JARVIS_ORACLE_ADAPTIVE_SCOPER_ENABLED", "0")
    assert O._oracle_scoper_enabled() is False        # kill switch


def test_provider_checkpoint_wal_and_counts(tmp_path):
    async def run():
        g = _build_graph(3)
        prov = P.AioSqliteProvider(tmp_path / "o.db")
        await prov.save(_state_from_graph(g))
        await prov.checkpoint_wal()                               # must not raise; folds WAL
        n, e = await prov.count_nodes_edges()
        await prov.close()
        assert n == g._graph.number_of_nodes() and e == g._graph.number_of_edges()
    asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
