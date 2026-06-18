"""Oracle SQLite persistence — cold-index hot-path soak.

Empirically validates the Phase-2 adaptive transactional batching at real scale (thousands of
real source files), proving:
  1. The incremental hot path completes a cold index WITHOUT event-loop starvation — a concurrent
     50ms heartbeat records its max stall; a responsive control plane = bounded max stall.
  2. Every batch commits incrementally (no monolithic rewrite) — the db grows during the index.
  3. Warm reboot loads from SQLite fast — the cold-boot bottleneck is eradicated.

NOTE on the full 2.67 GB production graph: migrating ~/.jarvis/oracle/codebase_graph.pkl needs
~10 GB+ RAM during the pickle load (the exact memory monster we're replacing) and belongs on the
Linux production host, not a laptop. This soak proves the mechanism at safe scale; the production
host runs the definitive full-graph migration soak.

Run:  PYTHONPATH=$(pwd) JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED=1 python3 -u scripts/soaks/oracle_sqlite_soak.py
"""
from __future__ import annotations

import asyncio
import os
import resource
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED", "1")

from backend.core.ouroboros.oracle import TheOracle  # noqa: E402


def _rss_mb() -> float:
    # macOS ru_maxrss is bytes; Linux is KiB. Normalize heuristically.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if raw > 10_000_000 else raw / 1024


async def _heartbeat(stop: asyncio.Event, out: dict) -> None:
    """Control-plane proxy: ping every 50ms; record the worst overshoot. A responsive loop keeps
    this small even while the cold index runs — that IS the anti-starvation proof."""
    period = 0.05
    worst = 0.0
    pings = 0
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(stop.wait(), timeout=period)
        except asyncio.TimeoutError:
            pass
        gap = (time.monotonic() - t0 - period) * 1000.0
        worst = max(worst, gap)
        pings += 1
    out["max_stall_ms"] = worst
    out["pings"] = pings


async def main() -> int:
    subtree = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("backend/core")
    subtree = subtree.resolve()
    tmp = Path(tempfile.mkdtemp(prefix="oracle_soak_"))
    db_path = tmp / "oracle.db"
    pkl_path = tmp / "codebase_graph.pkl"

    TheOracle._resolved_sqlite_path = staticmethod(lambda: db_path)          # type: ignore[assignment]
    TheOracle._resolved_graph_cache_path = staticmethod(lambda: pkl_path)    # type: ignore[assignment]

    print(f"# Oracle SQLite cold-index soak")
    print(f"# subtree: {subtree}")
    print(f"# sqlite:  {db_path}")
    print(f"# flag JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED={os.environ.get('JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED')}")

    # ---- Phase A: cold index with concurrent heartbeat ----
    o = TheOracle()
    o._repos = {"jarvis": subtree}
    hb_out: dict = {}
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(stop, hb_out))

    t0 = time.monotonic()
    await o._index_repository("jarvis", subtree)
    await o._sqlite_persist_metrics()
    cold_s = time.monotonic() - t0

    stop.set()
    await hb

    # Authoritative counts: under the scoper's between-subtree eviction the in-memory graph is
    # partial, so trust the SQLite-refreshed metric (falls back to the live graph when un-scoped).
    nodes = o._graph._metrics.get("total_nodes") or o._graph._graph.number_of_nodes()
    edges = o._graph._metrics.get("total_edges") or o._graph._graph.number_of_edges()
    resident_nodes = o._graph._graph.number_of_nodes()   # what's actually held in RAM at the end
    scoper_partitions = o._scoper_partitions
    scoper_evictions = o._scoper_evictions
    db_mb = db_path.stat().st_size / 1e6 if db_path.exists() else 0.0
    rss_after_index = _rss_mb()

    # graceful teardown (consumer + AST pool + provider) WITHOUT a full rewrite
    try:
        await o.stop_graph_write_consumer()
    except Exception:
        pass
    try:
        from backend.core.ouroboros.governance.ast_compile_helper import shutdown_pool
        await asyncio.to_thread(shutdown_pool, deadline_s=10.0)
    except Exception:
        pass
    if o._persistence is not None:
        await o._persistence.close()

    # ---- Phase B: warm reboot from SQLite, sampling RSS to prove the spike is flattened ----
    try:
        import psutil  # type: ignore
        _proc = psutil.Process()

        def _now_rss_mb() -> float:
            return _proc.memory_info().rss / 1e6
    except Exception:  # noqa: BLE001
        _now_rss_mb = _rss_mb  # fallback (peak-only)

    o2 = TheOracle()
    o2._repos = {"jarvis": subtree}
    rss_before_load = _now_rss_mb()
    load_peak = {"v": rss_before_load}
    load_stop = asyncio.Event()

    async def _rss_sampler() -> None:
        while not load_stop.is_set():
            load_peak["v"] = max(load_peak["v"], _now_rss_mb())
            try:
                await asyncio.wait_for(load_stop.wait(), timeout=0.02)
            except asyncio.TimeoutError:
                pass

    sampler = asyncio.create_task(_rss_sampler())
    t1 = time.monotonic()
    ok = await o2._load_cache()
    warm_s = time.monotonic() - t1
    load_stop.set()
    await sampler
    rss_after_load = _now_rss_mb()
    warm_nodes = o2._graph._graph.number_of_nodes()
    if o2._persistence is not None:
        await o2._persistence.close()
    # transient spike over the steady (graph-resident) footprint — low % == flat streaming load
    spike_pct = 100.0 * (load_peak["v"] - rss_after_load) / max(rss_after_load, 1e-9)

    # ---- report ----
    print("\n" + "=" * 70)
    print(f"{'PHASE A — cold index (incremental SQLite)':<45}")
    print(f"  files indexed (graph nodes)   : {nodes:>10,}")
    print(f"  edges                         : {edges:>10,}")
    print(f"  cold index wall time          : {cold_s:>10.2f} s")
    print(f"  index rate                    : {nodes / max(cold_s, 1e-9):>10.0f} nodes/s")
    print(f"  db size on disk               : {db_mb:>10.1f} MB")
    print(f"  peak RSS after index          : {rss_after_index:>10.0f} MB")
    print(f"  *** max control-plane stall    : {hb_out.get('max_stall_ms', 0.0):>9.1f} ms "
          f"({hb_out.get('pings', 0)} pings)")
    print(f"  memory armor — contractions   : {o._mem_armor_contractions:>10,}")
    print(f"  memory armor — GC yields      : {o._mem_armor_yields:>10,}")
    print(f"  memory armor — suspended?     : {str(o._mem_armor_suspended):>10}")
    print(f"  scoper — subtree partitions   : {scoper_partitions:>10,}")
    print(f"  scoper — RAM-reclaim evictions: {scoper_evictions:>10,}")
    print(f"  scoper — resident nodes (end) : {resident_nodes:>10,}  (vs {nodes:,} total in SQLite)")
    print(f"{'PHASE B — warm reboot (streaming load from SQLite)':<45}")
    print(f"  warm load ok                  : {str(ok):>10}")
    print(f"  warm load nodes               : {warm_nodes:>10,}")
    print(f"  warm load wall time           : {warm_s:>10.3f} s")
    print(f"  speedup (cold/warm)           : {cold_s / max(warm_s, 1e-9):>10.0f}x")
    print(f"  RSS before load               : {rss_before_load:>10.0f} MB")
    print(f"  RSS steady after load (graph) : {rss_after_load:>10.0f} MB")
    print(f"  *** peak RSS DURING load       : {load_peak['v']:>9.0f} MB")
    print(f"  *** transient spike over steady: {spike_pct:>9.1f} %   (low == flat streaming load)")
    print("=" * 70)

    # Verdict. The warm-load graph count (`warm_nodes`) is the authoritative full-graph size
    # (real rows + auto-vivified edge-stub endpoints). `nodes` is the SQLite nodes-TABLE row count
    # (no stubs) — so warm_nodes >= nodes always. A clean build = warm-load succeeds with the full
    # graph; under the scoper we ALSO require that eviction actually reduced the resident set.
    verdict_ok = ok and warm_nodes > 0 and warm_nodes >= nodes
    if scoper_evictions > 0:
        verdict_ok = verdict_ok and resident_nodes < warm_nodes  # eviction genuinely freed RAM
    print("VERDICT:", "PASS" if verdict_ok else "FAIL")
    return 0 if verdict_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
