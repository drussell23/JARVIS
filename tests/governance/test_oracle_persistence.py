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
    monkeypatch.delenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", raising=False)
    assert P.build_provider(db_path=tmp_path / "o.db", pickle_path=tmp_path / "o.pkl") is None


def test_factory_on_returns_sqlite(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "1")
    prov = P.build_provider(db_path=tmp_path / "o.db", pickle_path=tmp_path / "o.pkl")
    assert isinstance(prov, P.AioSqliteProvider)


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", raising=False)
    assert P.sqlite_persistence_enabled() is False


def test_flag_kill_switch(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "1")
    assert P.sqlite_persistence_enabled() is True


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
    """Master OFF → the .pkl is written, the provider is never even built (byte-identical path)."""
    monkeypatch.delenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", raising=False)

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


def test_oracle_on_migrates_legacy_pickle_on_first_load(monkeypatch, tmp_path):
    """Master ON + a legacy .pkl present + no db → first load migrates (ingest + archive)."""
    monkeypatch.setenv("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "1")

    async def run():
        # seed a legacy pickle via the legacy (OFF) save path
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
        # migrated: db created, pickle archived
        assert (tmp_path / "oracle.db").exists()
        assert not (tmp_path / "codebase_graph.pkl").exists()
        assert (tmp_path / "codebase_graph.pkl.migrated").exists()
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


def test_extraction_helpers(tmp_path):
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
