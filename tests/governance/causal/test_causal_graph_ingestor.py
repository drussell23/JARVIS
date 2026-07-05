"""TDD spine for Domain-1 Staging-2 Task 3 -- the CausalGraphIngestor.

Event-sourced fold: subscriber callback -> O(1) graph fold + a single
per-repo-ordered offloaded WAL append + deterministic snapshot compaction.

Cases:
  (a) ingest -> graph folded + a WAL entry appended (read the WAL back)
  (b) NON-BLOCKING: ingest returns immediately; the offloaded append lands
  (c) PER-REPO-ORDER invariant: [add@1, import@2, resig@3] rapid -> WAL entries
      for that repo are in emit_seq order (the load-bearing pin)
  (d) compaction: after SNAPSHOT_EVERY_N ingests a snapshot exists + WAL empty
  (e) snapshot+tail == full-replay (compaction loss-free)
  (f) organism_bus_host wires ingestor+subscriber when armed; byte-identical off
  (g) malformed envelope -> dropped, graph + WAL unaffected, no raise

Real ``StructuralDelta`` / ``CausalGraph`` -- no fold mocks. Uses tmp_path WALs.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance.causal.causal_graph import CausalGraph
from backend.core.ouroboros.governance.causal.causal_graph_ingestor import (
    CausalGraphIngestor,
)
from backend.core.ouroboros.governance.intake.wal import WAL
from backend.core.ouroboros.governance.causal.structural_delta import (
    ImportEdge,
    StructuralDelta,
    SymbolRecord,
)


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _delta(repo, file_path, *, added=(), removed=(), resig=(), imp_added=(),
           imp_removed=(), churn=False):
    return StructuralDelta(
        repo=repo,
        file_path=file_path,
        symbols_added=tuple(added),
        symbols_removed=tuple(removed),
        symbols_resignatured=tuple(resig),
        import_edges_added=tuple(imp_added),
        import_edges_removed=tuple(imp_removed),
        file_level_churn=churn,
        churn_counts={},
    )


def _env(delta, repo, emit_seq, head_sha="sha"):
    return {
        "delta": delta.to_dict(),
        "lineage": {
            "repo": repo,
            "head_sha": head_sha,
            "parent_sha": "parent",
            "merge_base": "base",
            "emit_seq": emit_seq,
        },
    }


def _sym(symbol_id, kind, sig):
    return SymbolRecord(symbol_id=symbol_id, kind=kind, signature_hash=sig)


def _add_env(repo, name, emit_seq, sig="h1"):
    sid = "%s:mod.py:%s" % (repo, name)
    d = _delta(repo, "mod.py", added=(_sym(sid, "function", sig),))
    return _env(d, repo, emit_seq)


def _paths(tmp_path):
    return str(tmp_path / "wal.jsonl"), str(tmp_path / "snap.json")


async def _poll_until(cond, *, timeout_s=5.0, interval_s=0.01):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval_s)
    return cond()


# --------------------------------------------------------------------------- #
# (a) ingest -> graph folded + WAL entry appended
# --------------------------------------------------------------------------- #
def test_ingest_folds_graph_and_appends_wal(tmp_path):
    wal_path, snap_path = _paths(tmp_path)

    async def scenario():
        graph = CausalGraph()
        ing = CausalGraphIngestor(
            graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing.start()
        try:
            ing.ingest(_add_env("brain", "f", 1))
            await ing.flush()
        finally:
            await ing.stop()
        # graph folded
        assert graph.node("brain:mod.py:f") is not None
        # WAL entry appended + readable back
        entries = WAL(tmp_path / "wal.jsonl").pending_entries()
        return entries

    entries = asyncio.run(scenario())
    assert len(entries) == 1
    assert entries[0].envelope_dict["lineage"]["emit_seq"] == 1


# --------------------------------------------------------------------------- #
# (b) NON-BLOCKING + WRITE-AHEAD: ingest returns immediately; the durable
#     append lands FIRST, and the graph fold is DEFERRED until after it.
# --------------------------------------------------------------------------- #
def test_ingest_is_non_blocking_and_fold_is_deferred_write_ahead(tmp_path):
    wal_path, snap_path = _paths(tmp_path)

    async def scenario():
        graph = CausalGraph()
        ing = CausalGraphIngestor(
            graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing.start()
        try:
            t0 = time.monotonic()
            ing.ingest(_add_env("brain", "f", 1))
            elapsed = time.monotonic() - t0
            # ingest itself does not block: it is a pure enqueue.
            assert elapsed < 0.25, "ingest blocked (should be a pure enqueue)"
            # WRITE-AHEAD: the fold is DEFERRED to the append worker (runs only
            # after the durable append lands). No await has occurred since
            # ingest returned, so the worker has NOT yet folded -> graph empty.
            assert graph.node("brain:mod.py:f") is None, (
                "fold must be deferred until AFTER the durable append "
                "(write-ahead), not applied inside ingest")
            # Both the durable append AND the deferred fold land asynchronously.
            landed = await _poll_until(
                lambda: len(WAL(tmp_path / "wal.jsonl").pending_entries()) == 1
                and graph.node("brain:mod.py:f") is not None)
            assert landed, "the durable append + deferred fold must both land"
        finally:
            await ing.stop()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# (c) PER-REPO-ORDER invariant (the load-bearing pin)
# --------------------------------------------------------------------------- #
def test_wal_preserves_per_repo_emit_seq_order(tmp_path):
    wal_path, snap_path = _paths(tmp_path)

    async def scenario():
        graph = CausalGraph()
        ing = CausalGraphIngestor(
            graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing.start()
        try:
            sid = "brain:mod.py:f"
            # add@1, import@2, resig@3 -- same repo, rapid, in emit_seq order.
            add = _delta("brain", "mod.py",
                         added=(_sym(sid, "function", "h1"),))
            imp = _delta("brain", "mod.py",
                         imp_added=(ImportEdge(sid, "os.path", "imports"),))
            resig = _delta("brain", "mod.py", resig=((sid, "h1", "h2"),))
            ing.ingest(_env(add, "brain", 1))
            ing.ingest(_env(imp, "brain", 2))
            ing.ingest(_env(resig, "brain", 3))
            await ing.flush()
        finally:
            await ing.stop()
        entries = WAL(tmp_path / "wal.jsonl").pending_entries()
        return [e.envelope_dict["lineage"]["emit_seq"] for e in entries]

    seqs = asyncio.run(scenario())
    assert seqs == [1, 2, 3], (
        "per-repo WAL append order must equal emit_seq order, got %r" % seqs)


# --------------------------------------------------------------------------- #
# (d) compaction: snapshot exists + WAL truncated after SNAPSHOT_EVERY_N
# --------------------------------------------------------------------------- #
def test_compaction_snapshots_and_truncates(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CAUSAL_SNAPSHOT_EVERY_N", "5")
    wal_path, snap_path = _paths(tmp_path)

    async def scenario():
        graph = CausalGraph()
        ing = CausalGraphIngestor(
            graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing.start()
        try:
            for i in range(5):
                ing.ingest(_add_env("brain", "f%d" % i, i + 1))
            await ing.flush()
        finally:
            await ing.stop()

    asyncio.run(scenario())
    from pathlib import Path
    # snapshot written
    assert Path(snap_path).exists()
    # WAL truncated to empty after the fold-to-snapshot
    assert WAL(tmp_path / "wal.jsonl").pending_entries() == []


# --------------------------------------------------------------------------- #
# (e) snapshot + tail == full-replay (compaction loss-free)
# --------------------------------------------------------------------------- #
def test_snapshot_plus_tail_equals_full_replay(tmp_path):
    wal_path, snap_path = _paths(tmp_path)
    N = 8

    async def scenario():
        graph = CausalGraph()
        # every_n large -> only the explicit snapshot_now() compacts.
        ing = CausalGraphIngestor(
            graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing.start()
        try:
            for i in range(N // 2):
                ing.ingest(_add_env("brain", "f%d" % i, i + 1))
            await ing.flush()
            await ing.snapshot_now()  # force snapshot at N/2 + truncate WAL
            for i in range(N // 2, N):
                ing.ingest(_add_env("brain", "f%d" % i, i + 1))
            await ing.flush()
            live_fp = graph.state_fingerprint()
        finally:
            await ing.stop()

        # fresh graph + fresh ingestor on the SAME paths -> replay
        fresh = CausalGraph()
        ing2 = CausalGraphIngestor(
            fresh, wal_path=wal_path, snapshot_path=snap_path)
        await ing2.replay_from_wal()
        return live_fp, ing2.graph.state_fingerprint(), ing2.graph.node_count()

    live_fp, replay_fp, count = asyncio.run(scenario())
    assert count == N, "replay must reconstruct all N nodes"
    assert replay_fp == live_fp, (
        "snapshot + WAL-tail replay must equal the live fingerprint")


# --------------------------------------------------------------------------- #
# (f) organism_bus_host wires ingestor+subscriber when armed; off byte-identical
# --------------------------------------------------------------------------- #
def test_organism_host_wires_ingestor_and_subscriber(monkeypatch):
    import backend.core.ouroboros.governance.transport.organism_bus_host as obh

    recorded = {}

    class _RecordingSubscriber:
        def __init__(self, bus, *, on_delta=None):
            recorded["bus"] = bus
            recorded["on_delta"] = on_delta

        async def start(self):
            recorded["subscriber_started"] = True

        async def stop(self):
            recorded["subscriber_stopped"] = True

    class _RecordingIngestor:
        def __init__(self, graph, **kwargs):
            recorded["graph"] = graph
            recorded["ingestor_kwargs"] = kwargs

        def ingest(self, envelope):
            recorded.setdefault("ingested", []).append(envelope)

        async def start(self):
            recorded["ingestor_started"] = True

        async def stop(self):
            recorded["ingestor_stopped"] = True

    import backend.core.ouroboros.governance.causal.causal_delta_subscriber as cds
    import backend.core.ouroboros.governance.causal.causal_graph_ingestor as cgi
    monkeypatch.setattr(cds, "CausalDeltaSubscriber", _RecordingSubscriber)
    monkeypatch.setattr(cgi, "CausalGraphIngestor", _RecordingIngestor)

    host = obh.OrganismBusHost()

    async def scenario():
        await host._start_causal_subscriber(object())

    asyncio.run(scenario())

    # ingestor constructed + started (replay), subscriber wired to ingest sink
    assert recorded.get("ingestor_started") is True
    assert recorded.get("subscriber_started") is True
    assert recorded.get("on_delta") is not None, (
        "the subscriber must receive the ingestor.ingest sink as on_delta")
    # the on_delta is the ingestor's ingest bound method
    assert callable(recorded["on_delta"])

    # stop in reverse: subscriber then ingestor
    asyncio.run(host.stop())
    assert recorded.get("subscriber_stopped") is True
    assert recorded.get("ingestor_stopped") is True


def test_organism_host_causal_wiring_dark_when_master_off(monkeypatch):
    """Master-off: start() returns False and constructs NOTHING (no causal
    subscriber, no ingestor). Byte-identical to pre-Task-3."""
    import backend.core.ouroboros.governance.transport.organism_bus_host as obh

    for key in ("JARVIS_DISTRIBUTED_BUS_ENABLED", "JARVIS_BRAIN_WS_PORT"):
        monkeypatch.delenv(key, raising=False)

    host = obh.OrganismBusHost()
    assert asyncio.run(host.start()) is False
    assert host.started is False
    assert host._causal_subscriber is None
    assert getattr(host, "_causal_ingestor", None) is None


# --------------------------------------------------------------------------- #
# (g) malformed envelope -> dropped, graph + WAL unaffected, no raise
# --------------------------------------------------------------------------- #
def test_malformed_envelope_dropped_no_raise(tmp_path):
    wal_path, snap_path = _paths(tmp_path)

    async def scenario():
        graph = CausalGraph()
        ing = CausalGraphIngestor(
            graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing.start()
        try:
            # a grab-bag of malformed shapes -- none may raise, fold, or append
            ing.ingest(None)  # type: ignore[arg-type]
            ing.ingest({})
            ing.ingest({"delta": "not-a-dict", "lineage": {}})
            ing.ingest({"delta": {}, "lineage": "not-a-dict"})
            ing.ingest({"delta": {}, "lineage": {"emit_seq": "not-int"}})
            ing.ingest({"delta": {}})  # missing lineage
            await ing.flush()
        finally:
            await ing.stop()
        return graph.node_count()

    count = asyncio.run(scenario())
    assert count == 0, "malformed envelopes must not mutate the graph"
    assert WAL(tmp_path / "wal.jsonl").pending_entries() == [], (
        "malformed envelopes must not be appended to the WAL")
