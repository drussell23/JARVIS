#!/usr/bin/env python3
"""Oracle Persistence Tri-Modal Benchmark — the ADD §9 decision gate.

Measures three persistence paradigms against a synthetic graph that mirrors the cold-index
statistics (~24k file nodes + entity nodes + ~200k edges), using the REAL
NodeID/NodeData/EdgeData dataclasses:

  A) Monolithic pickle      — current state / baseline
  B) Chunked pickle (shards)— per-file shards + manifest (incremental saves, mmap-ish load)
  C) aiosqlite normalized   — the ADD's proposed relational design (WAL + NORMAL)

Per mode it reports: cold save time + on-disk size, TTFR (full load → reconstructed
networkx DiGraph), peak load RAM (tracemalloc), event-loop block (max heartbeat lag while
the load runs ON the loop) + the to_thread/async-offloaded lag, and the incremental
checkpoint cost (persist 500 new nodes + edges).

Pure stdlib + aiosqlite + networkx. Writes only to a temp dir. Deterministic-ish (seeded
content sizing; no Math.random reliance).
"""
from __future__ import annotations

import asyncio
import gc
import json
import os
import pickle
import shutil
import sqlite3
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Dict, List, Tuple

import networkx as nx

from backend.core.ouroboros.oracle import (
    EdgeData, EdgeType, NodeData, NodeID, NodeType,
)

# ── scale (representative-but-runnable; real graph ~1.4GB — see report extrapolation) ──
N_FILES = int(os.getenv("BENCH_N_FILES", "24000"))
ENTITIES_PER_FILE = int(os.getenv("BENCH_ENTITIES", "4"))      # → ~120k entity+file nodes
DOCSTRING_LEN = int(os.getenv("BENCH_DOCLEN", "512"))           # bytes/node payload knob
CHECKPOINT_NODES = 500
_DOC = "x" * DOCSTRING_LEN
_SIG = "(self, a: int, b: str = 'z') -> Optional[Dict[str, Any]]"


# ── synthetic payload (real dataclasses) ─────────────────────────────────────────────────
def build_payload() -> Tuple[List[NodeData], List[Tuple[str, str, EdgeData]], Dict[str, str]]:
    nodes: List[NodeData] = []
    edges: List[Tuple[str, str, EdgeData]] = []
    file_hashes: Dict[str, str] = {}
    ntypes = [NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD, NodeType.IMPORT]
    etypes = [EdgeType.CONTAINS, EdgeType.CALLS, EdgeType.IMPORTS, EdgeType.INHERITS]
    for f in range(N_FILES):
        fp = f"backend/pkg{f % 50}/module_{f}.py"
        file_hashes[fp] = f"hash{f:08d}"
        file_id = NodeID(repo="jarvis", file_path=fp, name=f"module_{f}", node_type=NodeType.FILE)
        fkey = str(file_id)
        nodes.append(NodeData(node_id=file_id, docstring=_DOC, signature="", source_hash=file_hashes[fp]))
        for e in range(ENTITIES_PER_FILE):
            nt = ntypes[e % len(ntypes)]
            nid = NodeID(repo="jarvis", file_path=fp, name=f"sym_{f}_{e}", node_type=nt, line_number=e * 7)
            nkey = str(nid)
            nodes.append(NodeData(
                node_id=nid, docstring=_DOC, signature=_SIG,
                decorators=["staticmethod"] if e % 3 else [],
                base_classes=["Base"] if nt is NodeType.CLASS else [],
                complexity=e, line_count=20 + e, source_hash=file_hashes[fp],
            ))
            edges.append((fkey, nkey, EdgeData(EdgeType.CONTAINS, e * 7, "")))
            # a cross-entity call edge → ~2 edges/entity → ~200k total
            tgt = f"jarvis:{fp}:sym_{f}_{(e + 1) % ENTITIES_PER_FILE}"
            edges.append((nkey, tgt, EdgeData(etypes[e % len(etypes)], e * 7 + 3, "call")))
    return nodes, edges, file_hashes


def reconstruct(nodes_dicts: List[dict], edges_rows: List[tuple]) -> nx.DiGraph:
    """Rebuild the in-memory DiGraph — the shared end-state all 3 modes must reach."""
    g = nx.DiGraph()
    for nd in nodes_dicts:
        nid = NodeID.from_dict(nd["node_id"])
        g.add_node(str(nid), data=NodeData(
            node_id=nid, docstring=nd["docstring"], signature=nd["signature"],
            decorators=nd["decorators"], base_classes=nd["base_classes"],
            complexity=nd["complexity"], line_count=nd["line_count"],
            last_modified=nd["last_modified"], source_hash=nd["source_hash"],
        ))
    for (src, dst, et, ln, ctx) in edges_rows:
        g.add_edge(src, dst, data=EdgeData(EdgeType(et), ln, ctx))
    return g


# ── metric helpers ───────────────────────────────────────────────────────────────────────
async def _loop_block_ms(load_coro_factory) -> Tuple[float, float]:
    """Run a load while a 10ms heartbeat measures its own scheduling lag. Returns
    (max_lag_ms, wall_ms). A sync load on the loop spikes the lag to ~wall; an offloaded
    load keeps the lag near zero."""
    stop = False
    max_lag = 0.0

    async def heartbeat():
        nonlocal max_lag
        while not stop:
            t = time.monotonic()
            await asyncio.sleep(0.01)
            max_lag = max(max_lag, (time.monotonic() - t - 0.01) * 1000.0)

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    t0 = time.monotonic()
    await load_coro_factory()
    wall = (time.monotonic() - t0) * 1000.0
    stop = True
    await hb
    return max_lag, wall


def _dir_size_mb(p: Path) -> float:
    if p.is_file():
        return p.stat().st_size / 1e6
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


# ── Mode A: monolithic pickle ────────────────────────────────────────────────────────────
def save_pickle(path: Path, nodes, edges, fh) -> float:
    t0 = time.monotonic()
    data = {
        "nodes": [n.to_dict() for n in nodes],
        "edges": [(s, d, e.to_dict()) for (s, d, e) in edges],
        "file_hashes": fh,
    }
    with open(path, "wb") as fobj:
        pickle.dump(data, fobj, protocol=pickle.HIGHEST_PROTOCOL)
    return time.monotonic() - t0


def load_pickle(path: Path) -> nx.DiGraph:
    with open(path, "rb") as fobj:
        data = pickle.load(fobj)
    edges = [(s, d, e["edge_type"], e["line_number"], e["context"]) for (s, d, e) in data["edges"]]
    return reconstruct(data["nodes"], edges)


# ── Mode B: chunked pickle (per-file shards + manifest) ──────────────────────────────────
def save_chunked(root: Path, nodes, edges, fh) -> float:
    t0 = time.monotonic()
    root.mkdir(parents=True, exist_ok=True)
    by_file_nodes: Dict[str, list] = {}
    by_file_edges: Dict[str, list] = {}
    for n in nodes:
        by_file_nodes.setdefault(n.node_id.file_path, []).append(n.to_dict())
    for (s, d, e) in edges:
        fp = s.split(":", 2)[1]
        by_file_edges.setdefault(fp, []).append((s, d, e.to_dict()))
    manifest = {}
    for i, fp in enumerate(by_file_nodes):
        shard = root / f"shard_{i}.pkl"
        with open(shard, "wb") as fobj:
            pickle.dump({"nodes": by_file_nodes[fp], "edges": by_file_edges.get(fp, [])},
                        fobj, protocol=pickle.HIGHEST_PROTOCOL)
        manifest[fp] = shard.name
    (root / "manifest.json").write_text(json.dumps({"shards": manifest, "file_hashes": fh}))
    return time.monotonic() - t0


def load_chunked(root: Path) -> nx.DiGraph:
    manifest = json.loads((root / "manifest.json").read_text())
    all_nodes, all_edges = [], []
    for shard_name in manifest["shards"].values():
        with open(root / shard_name, "rb") as fobj:
            sd = pickle.load(fobj)
        all_nodes.extend(sd["nodes"])
        all_edges.extend((s, d, e["edge_type"], e["line_number"], e["context"]) for (s, d, e) in sd["edges"])
    return reconstruct(all_nodes, all_edges)


# ── Mode D: chunked-COARSE (shard by package dir, ~50 shards) ────────────────────────────
def _pkg_of(fp: str) -> str:
    parts = fp.split("/")
    return parts[1] if len(parts) > 1 else "root"


def save_chunked_coarse(root: Path, nodes, edges, fh) -> float:
    t0 = time.monotonic()
    root.mkdir(parents=True, exist_ok=True)
    by_pkg_n: Dict[str, list] = {}
    by_pkg_e: Dict[str, list] = {}
    for n in nodes:
        by_pkg_n.setdefault(_pkg_of(n.node_id.file_path), []).append(n.to_dict())
    for (s, d, e) in edges:
        by_pkg_e.setdefault(_pkg_of(s.split(":", 2)[1]), []).append((s, d, e.to_dict()))
    manifest = {}
    for i, pkg in enumerate(by_pkg_n):
        shard = root / f"pkg_{i}.pkl"
        with open(shard, "wb") as fobj:
            pickle.dump({"nodes": by_pkg_n[pkg], "edges": by_pkg_e.get(pkg, [])},
                        fobj, protocol=pickle.HIGHEST_PROTOCOL)
        manifest[pkg] = shard.name
    (root / "manifest.json").write_text(json.dumps({"shards": manifest, "file_hashes": fh}))
    return time.monotonic() - t0


def load_chunked_coarse(root: Path) -> nx.DiGraph:
    manifest = json.loads((root / "manifest.json").read_text())
    all_nodes, all_edges = [], []
    for shard_name in manifest["shards"].values():
        with open(root / shard_name, "rb") as fobj:
            sd = pickle.load(fobj)
        all_nodes.extend(sd["nodes"])
        all_edges.extend((s, d, e["edge_type"], e["line_number"], e["context"]) for (s, d, e) in sd["edges"])
    return reconstruct(all_nodes, all_edges)


# ── Mode C: aiosqlite normalized (WAL + NORMAL) ──────────────────────────────────────────
_SCHEMA = """
CREATE TABLE nodes(node_key TEXT PRIMARY KEY, repo TEXT, file_path TEXT, name TEXT,
  node_type TEXT, line_number INT, docstring TEXT, signature TEXT, decorators TEXT,
  base_classes TEXT, complexity INT, line_count INT, last_modified REAL, source_hash TEXT);
CREATE INDEX idx_nodes_file ON nodes(file_path);
CREATE TABLE edges(src_key TEXT, dst_key TEXT, edge_type TEXT, line_number INT, context TEXT,
  PRIMARY KEY(src_key,dst_key,edge_type,line_number));
CREATE INDEX idx_edges_src ON edges(src_key);
CREATE INDEX idx_edges_dst ON edges(dst_key);
CREATE TABLE file_hashes(file_path TEXT PRIMARY KEY, source_hash TEXT, indexed_at REAL);
"""


def save_sqlite(path: Path, nodes, edges, fh) -> float:
    t0 = time.monotonic()
    con = sqlite3.connect(path)
    con.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;" + _SCHEMA)
    con.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(str(n.node_id), n.node_id.repo, n.node_id.file_path, n.node_id.name,
          n.node_id.node_type.value, n.node_id.line_number, n.docstring, n.signature,
          json.dumps(n.decorators), json.dumps(n.base_classes), n.complexity,
          n.line_count, n.last_modified, n.source_hash) for n in nodes],
    )
    con.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)",
                    [(s, d, e.edge_type.value, e.line_number, e.context) for (s, d, e) in edges])
    con.executemany("INSERT INTO file_hashes VALUES (?,?,?)",
                    [(k, v, 0.0) for k, v in fh.items()])
    con.commit()
    con.close()
    return time.monotonic() - t0


def load_sqlite_sync(path: Path) -> nx.DiGraph:
    """sqlite3 (sync) loader — for the FAIR loop-lag test, all 3 modes load via the same
    asyncio.to_thread offload (production uses to_thread). The DiGraph reconstruction is
    GIL-bound Python work regardless of format, so this isolates the format's deserialize
    cost without the on-loop-vs-offloaded confound."""
    con = sqlite3.connect(path)
    node_rows = con.execute(
        "SELECT node_key,repo,file_path,name,node_type,line_number,docstring,signature,"
        "decorators,base_classes,complexity,line_count,last_modified,source_hash FROM nodes"
    ).fetchall()
    edge_rows = con.execute("SELECT src_key,dst_key,edge_type,line_number,context FROM edges").fetchall()
    con.close()
    g = nx.DiGraph()
    for r in node_rows:
        nid = NodeID(repo=r[1], file_path=r[2], name=r[3], node_type=NodeType(r[4]), line_number=r[5])
        g.add_node(r[0], data=NodeData(
            node_id=nid, docstring=r[6], signature=r[7], decorators=json.loads(r[8]),
            base_classes=json.loads(r[9]), complexity=r[10], line_count=r[11],
            last_modified=r[12], source_hash=r[13]))
    for r in edge_rows:
        g.add_edge(r[0], r[1], data=EdgeData(EdgeType(r[2]), r[3], r[4]))
    return g


async def load_sqlite_async(path: Path) -> nx.DiGraph:
    import aiosqlite
    async with aiosqlite.connect(path) as con:
        cur = await con.execute("SELECT node_key,repo,file_path,name,node_type,line_number,"
                                "docstring,signature,decorators,base_classes,complexity,"
                                "line_count,last_modified,source_hash FROM nodes")
        node_rows = await cur.fetchall()
        cur = await con.execute("SELECT src_key,dst_key,edge_type,line_number,context FROM edges")
        edge_rows = await cur.fetchall()
    g = nx.DiGraph()
    for r in node_rows:
        nid = NodeID(repo=r[1], file_path=r[2], name=r[3], node_type=NodeType(r[4]), line_number=r[5])
        g.add_node(r[0], data=NodeData(
            node_id=nid, docstring=r[6], signature=r[7], decorators=json.loads(r[8]),
            base_classes=json.loads(r[9]), complexity=r[10], line_count=r[11],
            last_modified=r[12], source_hash=r[13]))
    for r in edge_rows:
        g.add_edge(r[0], r[1], data=EdgeData(EdgeType(r[2]), r[3], r[4]))
    return g


def checkpoint_sqlite(path: Path, new_nodes, new_edges) -> float:
    t0 = time.monotonic()
    con = sqlite3.connect(path)
    con.execute("PRAGMA synchronous=NORMAL;")
    con.executemany("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(str(n.node_id), n.node_id.repo, n.node_id.file_path, n.node_id.name,
                      n.node_id.node_type.value, n.node_id.line_number, n.docstring, n.signature,
                      json.dumps(n.decorators), json.dumps(n.base_classes), n.complexity,
                      n.line_count, n.last_modified, n.source_hash) for n in new_nodes])
    con.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)",
                    [(s, d, e.edge_type.value, e.line_number, e.context) for (s, d, e) in new_edges])
    con.commit()
    con.close()
    return time.monotonic() - t0


# ── driver ───────────────────────────────────────────────────────────────────────────────
async def run():
    tmp = Path(tempfile.mkdtemp(prefix="oracle_bench_"))
    print(f"# Oracle Persistence Tri-Modal Benchmark")
    print(f"# scale: {N_FILES} files x {ENTITIES_PER_FILE} entities, doclen={DOCSTRING_LEN}B")
    nodes, edges, fh = build_payload()
    n_nodes, n_edges = len(nodes), len(edges)
    print(f"# payload: {n_nodes} nodes, {n_edges} edges")
    # 500 new nodes for the incremental-checkpoint test
    new_nodes = nodes[:CHECKPOINT_NODES]
    new_edges = edges[:CHECKPOINT_NODES]
    results = {}

    # A — monolithic pickle
    pk = tmp / "graph.pkl"
    a_save = save_pickle(pk, nodes, edges, fh)
    gc.collect(); tracemalloc.start()
    a_lag, a_wall = await _loop_block_ms(lambda: asyncio.to_thread(load_pickle, pk))
    _, a_peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    a_ckpt = save_pickle(pk, nodes, edges, fh)  # monolithic checkpoint = FULL rewrite
    results["A monolithic-pickle"] = dict(save=a_save, size=_dir_size_mb(pk), ttfr=a_wall,
                                          loop_lag=a_lag, ram=a_peak / 1e6, ckpt=a_ckpt)

    # B — chunked pickle
    ch = tmp / "chunks"
    b_save = save_chunked(ch, nodes, edges, fh)
    gc.collect(); tracemalloc.start()
    b_lag, b_wall = await _loop_block_ms(lambda: asyncio.to_thread(load_chunked, ch))
    _, b_peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    # incremental checkpoint: rewrite only the shards touched by 500 new nodes' files
    t0 = time.monotonic()
    touched = {n.node_id.file_path for n in new_nodes}
    bf_n = {fp: [n.to_dict() for n in new_nodes if n.node_id.file_path == fp] for fp in touched}
    for i, fp in enumerate(touched):
        with open(ch / f"_ck_{i}.pkl", "wb") as fobj:
            pickle.dump({"nodes": bf_n[fp], "edges": []}, fobj, protocol=pickle.HIGHEST_PROTOCOL)
    b_ckpt = time.monotonic() - t0
    results["B chunked-pickle"] = dict(save=b_save, size=_dir_size_mb(ch), ttfr=b_wall,
                                       loop_lag=b_lag, ram=b_peak / 1e6, ckpt=b_ckpt)

    # D — chunked COARSE (shard by package dir, ~50 shards) — the hypothesis fix for B's load
    chc = tmp / "chunks_coarse"
    d_save = save_chunked_coarse(chc, nodes, edges, fh)
    gc.collect(); tracemalloc.start()
    d_lag, d_wall = await _loop_block_ms(lambda: asyncio.to_thread(load_chunked_coarse, chc))
    _, d_peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    # incremental checkpoint: rewrite only the package shards touched by 500 new nodes
    t0 = time.monotonic()
    touched_pkgs: Dict[str, list] = {}
    for n in new_nodes:
        touched_pkgs.setdefault(_pkg_of(n.node_id.file_path), []).append(n.to_dict())
    manifest = json.loads((chc / "manifest.json").read_text())
    for pkg, nlist in touched_pkgs.items():
        shard_name = manifest["shards"].get(pkg)
        if shard_name is None:
            continue
        with open(chc / shard_name, "rb") as fobj:
            sd = pickle.load(fobj)
        sd["nodes"].extend(nlist)  # read-modify-write the affected shard
        with open(chc / shard_name, "wb") as fobj:
            pickle.dump(sd, fobj, protocol=pickle.HIGHEST_PROTOCOL)
    d_ckpt = time.monotonic() - t0
    results["D chunked-coarse"] = dict(save=d_save, size=_dir_size_mb(chc), ttfr=d_wall,
                                       loop_lag=d_lag, ram=d_peak / 1e6, ckpt=d_ckpt)

    # C — aiosqlite normalized
    db = tmp / "oracle.db"
    c_save = save_sqlite(db, nodes, edges, fh)
    gc.collect(); tracemalloc.start()
    # FAIR loop-lag: load via to_thread like A/B (production uses to_thread).
    c_lag, c_wall = await _loop_block_ms(lambda: asyncio.to_thread(load_sqlite_sync, db))
    _, c_peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    c_ckpt = checkpoint_sqlite(db, new_nodes, new_edges)
    results["C aiosqlite-normalized"] = dict(save=c_save, size=_dir_size_mb(db), ttfr=c_wall,
                                             loop_lag=c_lag, ram=c_peak / 1e6, ckpt=c_ckpt)

    # ── report ──
    print("\n" + "=" * 92)
    print(f"{'MODE':<26}{'save_s':>9}{'disk_MB':>9}{'TTFR_s':>9}{'loop_lag_ms':>13}"
          f"{'load_RAM_MB':>13}{'ckpt500_ms':>12}")
    print("-" * 92)
    for mode, r in results.items():
        print(f"{mode:<26}{r['save']:>9.2f}{r['size']:>9.0f}{r['ttfr']/1000:>9.2f}"
              f"{r['loop_lag']:>13.1f}{r['ram']:>13.0f}{r['ckpt']*1000:>12.1f}")
    print("=" * 92)
    shutil.rmtree(tmp, ignore_errors=True)
    return results


if __name__ == "__main__":
    asyncio.run(run())
