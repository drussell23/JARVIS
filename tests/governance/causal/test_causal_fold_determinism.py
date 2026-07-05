"""Domain-1 Staging-2 Task 4 -- the tear-down/spin-up determinism PROOF.

MANDATE-4, load-bearing: a live ``CausalGraph`` (folded through the
``CausalGraphIngestor``) is byte-for-byte reconstructible from the durable
truth on disk (WAL + snapshot) after a simulated Brain-VM crash that destroys
ALL in-memory state.

    live_fingerprint  ==  WAL/snapshot-reconstructed fingerprint

WHY THIS IS A TRUE-CRASH PROOF (write-ahead): the ingestor is append-BEFORE-fold
-- the single worker durably appends each envelope to the WAL FIRST and folds it
into the live graph ONLY after that append lands. So the live graph reflects
ONLY durably-appended deltas: at quiescence (``flush()`` drained) the WAL on disk
is exactly ``fold(WAL) == live graph``. Therefore capturing ``live_fp`` at
quiescence and then annihilating all in-memory state loses NOTHING that isn't
already on disk -- ``recovered == live`` is a genuine crash-determinism result,
not an artifact of a graceful pre-crash flush. (Under the old write-BEHIND design
a delta could be folded-but-not-yet-appended, so a true crash would yield
recovered != live; append-before-fold closes that gap.)

The equality witness is ``graph.state_fingerprint()`` (sha256 over the canonical
snapshot: every node field + the full per-symbol Lamport high-water map),
cross-checked by a second independent witness ``graph.snapshot() == ...``.

*** THE BINDING CONSTRAINT (from the Task-1 opus review -- honored here) ***

The fold is order-independent for FULL-WRITE ops (add / remove -- disjoint or
tombstoned, they commute under ANY shuffle) but PARTIAL-WRITE fields
(resignature-only signature, import-only edge merge, the ``imports`` merge a
subsequent add performs) are last-writer-wins PER field-source and converge
ONLY under per-repo ``emit_seq`` order (the ingestor's WAL-append invariant).

THEREFORE every determinism/order-independence claim in this file shuffles the
CROSS-REPO interleaving ONLY -- the three repos' delta streams (disjoint
``symbol_id`` namespaces) genuinely commute against each other -- while PRESERVING
each single repo's internal ``emit_seq`` order. Free-shuffling same-repo deltas
would step outside the event model: it would either flake or FALSELY pass. A
genuine same-symbol "stale lower-seq" delta CANNOT occur inside a per-repo-ordered
live stream (a symbol's hwm is only ever set by strictly-earlier, lower-seq
deltas), so the honest "stale/duplicate" coverage here is (1) an equal-seq
DUPLICATE replay (strict-``>`` guard makes it a no-op) and (2) the idempotent
WAL-tail re-fold after compaction -- both fully inside the event model.
"""
from __future__ import annotations

import asyncio
import copy
import random
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.causal.causal_graph import CausalGraph
from backend.core.ouroboros.governance.causal.causal_graph_ingestor import (
    CausalGraphIngestor,
)
from backend.core.ouroboros.governance.causal.structural_delta import (
    ImportEdge,
    StructuralDelta,
    SymbolRecord,
)

REPOS = ("jarvis", "prime", "reactor")


# --------------------------------------------------------------------------- #
# builders (real StructuralDelta / envelope shape -- no fold mocks)
# --------------------------------------------------------------------------- #
def _sym(symbol_id: str, kind: str, sig: str) -> SymbolRecord:
    return SymbolRecord(symbol_id=symbol_id, kind=kind, signature_hash=sig)


def _delta(repo, file_path, *, added=(), removed=(), resig=(), imp_added=(),
           imp_removed=(), churn=False) -> StructuralDelta:
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


def _env(delta: StructuralDelta, repo: str, emit_seq: int) -> dict:
    """The publish-ready Staging-1 envelope: content-free delta dict + lineage.
    ``head_sha`` is deterministic in (repo, seq) so it folds into a stable
    ``last_head_sha`` -- part of the fingerprint."""
    return {
        "delta": delta.to_dict(),
        "lineage": {
            "repo": repo,
            "head_sha": "%sh%d" % (repo, emit_seq),
            "parent_sha": "%sp%d" % (repo, emit_seq),
            "merge_base": "%sbase" % repo,
            "emit_seq": emit_seq,
        },
    }


def _build_repo_stream(repo: str, rng: random.Random) -> list:
    """ONE repo's ordered (emit_seq 1..N, strictly increasing) envelope stream.

    Mixes EVERY op the graph fold supports: add / resignature (partial) /
    remove / import-edge (partial) / file_level_churn / re-add-after-tombstone /
    no-home resignature (bumps hwm, materializes no node) / equal-seq
    duplicate-replay. Content (sigs, extra-symbol count) is seed-varied so the
    three test iterations fold DIFFERENT arbitrary sequences, but the per-repo
    emit_seq order is ALWAYS monotonic (the binding invariant)."""
    file_a = "core.py"
    file_b = "util.py"
    A = "%s:%s:alpha" % (repo, file_a)
    B = "%s:%s:beta" % (repo, file_a)
    C = "%s:%s:gamma" % (repo, file_b)

    envs: list = []
    seq = 0

    def push(delta: StructuralDelta) -> None:
        nonlocal seq
        seq += 1
        envs.append(_env(delta, repo, seq))

    s = lambda tag: "%s_%s_%d" % (repo, tag, rng.randint(0, 1 << 30))  # noqa: E731

    # 1 add A (function)
    push(_delta(repo, file_a, added=(_sym(A, "function", s("a")),)))
    # 2 add B (class)
    push(_delta(repo, file_a, added=(_sym(B, "class", s("b")),)))
    # 3 import-ONLY on A (partial write; merges onto A's node)
    push(_delta(repo, file_a,
                imp_added=(ImportEdge(A, "os.path", "imports"),)))
    # 4 resignature-ONLY A (partial write)
    push(_delta(repo, file_a, resig=((A, "old", s("a2")),)))
    # 5 add C (function) in the second file
    push(_delta(repo, file_b, added=(_sym(C, "function", s("c")),)))
    # 6 remove B (tombstones B's hwm)
    push(_delta(repo, file_a, removed=(_sym(B, "class", "old"),)))
    # 7 re-add B at a higher seq -> beats the tombstone hwm (re-add proof)
    push(_delta(repo, file_a, added=(_sym(B, "class", s("b2")),)))
    # 8 import-ONLY on C (partial write, imports_from)
    push(_delta(repo, file_b,
                imp_added=(ImportEdge(C, "typing.List", "imports_from"),)))
    # 9 file_level_churn on file_a -> bumps ONLY A + B lineage (never C)
    push(_delta(repo, file_a, churn=True))
    # 10 resignature C
    push(_delta(repo, file_b, resig=((C, "old", s("c2")),)))

    # seed-varied extra symbols (different delta mix per iteration)
    for i in range(rng.randint(0, 3)):
        X = "%s:%s:x%d" % (repo, file_b, i)
        push(_delta(repo, file_b, added=(_sym(X, "function", s("x%d" % i)),)))
        if rng.random() < 0.5:
            push(_delta(repo, file_b, resig=((X, "old", s("x%dr" % i)),)))

    # 11 remove C, then 12 a NO-HOME resignature of C at a still-higher seq:
    # wins the hwm guard (bumps the tombstone watermark) yet materializes no
    # node -- the fold records the watermark deterministically, live or replay.
    push(_delta(repo, file_b, removed=(_sym(C, "function", "old"),)))
    push(_delta(repo, file_b, resig=((C, "old", s("c3")),)))

    # equal-seq DUPLICATE replay of the tail envelope -> strict-`>` guard makes
    # it a pure no-op. Keeps the per-repo stream monotonic NON-decreasing.
    envs.append(copy.deepcopy(envs[-1]))

    return envs


def _build_streams(seed: int) -> list:
    """Three per-repo ordered streams for one iteration (seed-varied content)."""
    rng = random.Random(seed)
    return [_build_repo_stream(repo, rng) for repo in REPOS]


def _interleave(streams: list, rng: random.Random) -> list:
    """Merge the per-repo ordered streams into ONE cross-repo-interleaved
    sequence, PRESERVING each stream's internal order (cursor pop) while
    randomizing WHICH stream advances next. Non-mutating (index cursors).

    This is the ONLY legal shuffle: it randomizes the cross-repo interleave
    (disjoint symbol namespaces commute) but never reorders a single repo's
    emit_seq subsequence (partial-writes are order-sensitive there)."""
    cursors = [0] * len(streams)
    total = sum(len(s) for s in streams)
    out: list = []
    for _ in range(total):
        live = [i for i, s in enumerate(streams) if cursors[i] < len(s)]
        i = rng.choice(live)
        out.append(streams[i][cursors[i]])
        cursors[i] += 1
    return out


async def _drain(cond, *, timeout_s: float = 5.0, interval_s: float = 0.01):
    """Bounded condition-poll (no bare sleep-as-sync)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval_s)
    return cond()


def _paths(tmp_path):
    return str(tmp_path / "causal_wal.jsonl"), str(tmp_path / "causal_snap.json")


# --------------------------------------------------------------------------- #
# THE PROOF -- 3x, different arbitrary sequences, streamed fingerprints
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_teardown_spinup_fold_determinism(tmp_path, seed):
    """Live graph == WAL-reconstructed graph after a simulated Brain crash,
    PLUS the cross-repo order-independence corollary. Mathematically asserted
    on both the fingerprint AND the raw snapshot dict."""
    wal_path, snap_path = _paths(tmp_path)
    streams = _build_streams(seed)

    async def scenario():
        # -- LIVE ingest (cross-repo-interleaved, per-repo order preserved) ----
        graph = CausalGraph()
        ing = CausalGraphIngestor(
            graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing.start()
        live_order = _interleave(streams, random.Random(seed * 31 + 1))
        for env in live_order:
            ing.ingest(env)
        await ing.flush()                     # quiesce: append-then-fold drained
        # write-AHEAD invariant: at quiescence the live graph reflects ONLY
        # durably-appended deltas, so fold(WAL) == this live graph exactly.
        live_fp = graph.state_fingerprint()
        live_snap = graph.snapshot()

        # -- SIMULATE BRAIN VM CRASH -------------------------------------------
        # Quiesce reached above => the WAL on disk is the durable truth and is
        # byte-equal to the live graph. Annihilate ALL in-memory state. stop()
        # here only unwinds the (already-idle) worker task cleanly; it adds NO
        # durability a hard kill wouldn't have -- everything folded was already
        # appended (write-ahead). Nothing but the WAL/snapshot files survive.
        await ing.stop()
        del ing, graph                        # no in-memory state survives

        n_wal = await _drain(
            lambda: Path(wal_path).exists()
            and Path(wal_path).stat().st_size > 0)
        assert n_wal, "WAL must be durable on disk after the crash"

        # -- RECONSTRUCT from the durable log (fresh objects, SAME paths) -------
        recovered_graph = CausalGraph()
        ing2 = CausalGraphIngestor(
            recovered_graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing2.replay_from_wal()
        # replay_from_wal may REASSIGN ing2.graph (from_snapshot path); the
        # authoritative reconstructed graph is ALWAYS ing2.graph.
        recovered = ing2.graph
        recovered_fp = recovered.state_fingerprint()
        recovered_snap = recovered.snapshot()

        # -- CROSS-REPO ORDER-INDEPENDENCE COROLLARY ---------------------------
        # Re-fold the SAME per-repo-ordered entries in a DIFFERENT cross-repo
        # interleaving (per-repo order still preserved) directly via apply_delta
        # -> identical fingerprint. Proves the fold commutes across repos.
        shuffled_graph = CausalGraph()
        shuffle_order = _interleave(streams, random.Random(seed * 97 + 5))
        assert [id(e) for e in shuffle_order] != [id(e) for e in live_order], (
            "the corollary must use a DIFFERENT cross-repo interleaving")
        for env in shuffle_order:
            shuffled_graph.apply_delta(env)
        shuffle_fp = shuffled_graph.state_fingerprint()

        return live_fp, recovered_fp, live_snap, recovered_snap, shuffle_fp

    live_fp, recovered_fp, live_snap, recovered_snap, shuffle_fp = asyncio.run(
        scenario())

    print(
        "[determinism seed=%2d] live=%s recovered=%s %s" % (
            seed, live_fp[:16], recovered_fp[:16],
            "MATCH" if live_fp == recovered_fp else "MISMATCH"),
        flush=True)

    # MANDATE-4 primary witness: fingerprint equality.
    assert recovered_fp == live_fp, (
        "WAL-reconstructed fingerprint must equal the live fingerprint")
    # Second INDEPENDENT witness: the raw canonical snapshot dict.
    assert recovered_snap == live_snap, (
        "WAL-reconstructed snapshot must equal the live snapshot")
    # Non-triviality: the sequence actually built graph state.
    assert live_snap["nodes"], "the arbitrary sequence must fold real nodes"
    # Cross-repo order-independence corollary.
    assert shuffle_fp == live_fp, (
        "a DIFFERENT cross-repo interleaving (per-repo order preserved) must "
        "fold to the identical fingerprint")


# --------------------------------------------------------------------------- #
# COMPACTION CROSS-CHECK -- snapshot fires mid-sequence, crash, replay still ==
# --------------------------------------------------------------------------- #
def test_compaction_lossless_under_crash(tmp_path, monkeypatch):
    """A tiny SNAPSHOT_EVERY_N forces fold-to-snapshot compaction mid-sequence.
    After the crash, recovery loads snapshot + re-folds the WAL tail (idempotent
    under the emit_seq guard) and STILL reconstructs the exact live fingerprint
    -- compaction is loss-free across a crash."""
    monkeypatch.setenv("JARVIS_CAUSAL_SNAPSHOT_EVERY_N", "4")
    wal_path, snap_path = _paths(tmp_path)
    streams = _build_streams(seed=1234)

    async def scenario():
        graph = CausalGraph()
        ing = CausalGraphIngestor(
            graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing.start()
        for env in _interleave(streams, random.Random(3)):
            ing.ingest(env)
        await ing.flush()
        # compaction must have fired at least once (snapshot file on disk)
        snapshotted = await _drain(lambda: Path(snap_path).exists())
        assert snapshotted, "small EVERY_N must trigger a mid-sequence snapshot"
        live_fp = graph.state_fingerprint()

        # crash
        await ing.stop()
        del ing, graph

        # recover from snapshot + WAL tail
        recovered_graph = CausalGraph()
        ing2 = CausalGraphIngestor(
            recovered_graph, wal_path=wal_path, snapshot_path=snap_path)
        await ing2.replay_from_wal()
        return live_fp, ing2.graph.state_fingerprint()

    live_fp, recovered_fp = asyncio.run(scenario())
    print(
        "[compaction     ] live=%s recovered=%s %s" % (
            live_fp[:16], recovered_fp[:16],
            "MATCH" if live_fp == recovered_fp else "MISMATCH"),
        flush=True)
    assert recovered_fp == live_fp, (
        "snapshot + WAL-tail recovery must equal the live fingerprint "
        "(compaction is loss-free under crash)")
