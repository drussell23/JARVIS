"""Slice 28 — adaptive cooperative embed/upsert tail.

Soak bt-2026-07-15-225445 verdict: one 10.3s ControlPlaneStarvation hold at
the cold-sweep embed tail (chroma init itself was executor-isolated at
132ms per LoopSink — the hold was the tail AROUND it): `_build_embed_text`
evaluated over every node TWICE (filter pass + per-batch rebuild) as
pure-Python string joins with zero awaits before the first batch, on-loop
metadata builds + numpy tolist() conversion, and a static 128 batch with
no adaptation between batches.

Fix under test: `_embed_nodes_coop` — per-batch prep + convert-and-upsert
via the cooperative_fs_io thread offload (the Slice 25 executor; thread,
never process — persist-dir lock), AIMD batch sizing from the shared
Slice 27 `measure_loop_lag_ms` probe, memory axis via the shared
MemoryPressureGate (Slice 26 reservation dimension included) through the
shared `_ORACLE_MEM_LAG_MULT` table. Payload-size adaptation is emergent
(heavier batch → more observed lag → contraction), not configured.
"""
from __future__ import annotations

import inspect

import backend.core.ouroboros.oracle as O


# ── wiring pins ──────────────────────────────────────────────────────


def test_embed_nodes_delegates_to_coop_tail():
    src = inspect.getsource(O.OracleSemanticIndex.embed_nodes)
    assert "_oracle_embed_tail_coop_enabled" in src
    assert "_embed_nodes_coop" in src


def test_coop_tail_composes_shared_substrate():
    src = inspect.getsource(O.OracleSemanticIndex._embed_nodes_coop)
    # DRY: shared AIMD class, shared lag probe, shared pressure table,
    # shared offload substrate — no new mechanisms.
    assert "_AdaptiveIndexThrottle" in src
    assert "measure_loop_lag_ms" in src
    assert "_ORACLE_MEM_LAG_MULT" in src
    assert "_semantic_embed_pressure_level" in src
    assert "cooperative_fs_io" in src
    # Slice 25 invariant preserved: chroma writes stay on the THREAD path
    # (cpu_bound=False) — a process pool would corrupt HNSW segments.
    assert "cpu_bound=False" in src
    assert "cpu_bound=True" not in src
    assert "self._collection.upsert" in src


def test_coop_tail_offloads_prep_and_conversion():
    src = inspect.getsource(O.OracleSemanticIndex._embed_nodes_coop)
    # text build + metadata happen inside the offloaded _prep closure;
    # tolist() conversion rides the SAME thread hop as the upsert.
    assert "def _prep(" in src
    assert "def _convert_and_upsert(" in src
    assert "e.tolist() for e in" in src


def test_coop_tail_master_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_ORACLE_EMBED_TAIL_COOP_ENABLED", raising=False)
    assert O._oracle_embed_tail_coop_enabled() is True
    monkeypatch.setenv("JARVIS_ORACLE_EMBED_TAIL_COOP_ENABLED", "0")
    assert O._oracle_embed_tail_coop_enabled() is False


def test_pressure_helper_fails_open(monkeypatch):
    import backend.core.ouroboros.governance.memory_pressure_gate as MPG

    def _boom():
        raise RuntimeError("gate down")

    monkeypatch.setattr(MPG, "get_default_gate", _boom)
    assert O._semantic_embed_pressure_level() == "ok"


# ── functional: coop tail embeds via the STDLIB backend ─────────────


def _make_index(status):
    idx = O.OracleSemanticIndex.__new__(O.OracleSemanticIndex)
    idx._status = status
    idx._collection = None
    idx._stdlib_store = {}
    idx._init_attempted = True

    class _Embedder:
        async def embed_batch(self, texts):
            return [[float(len(t)), 0.5] for t in texts]

    idx._embedder = _Embedder()
    return idx


def _make_nodes(n):
    return [
        O.NodeData(
            node_id=O.NodeID(
                repo="jarvis",
                file_path=f"backend/mod_{i}.py",
                name=f"fn_{i}",
                node_type=O.NodeType.FUNCTION,
            ),
            signature=f"def fn_{i}(x)",
            docstring="does things",
        )
        for i in range(n)
    ]


async def test_coop_tail_embeds_stdlib_end_to_end(monkeypatch):
    monkeypatch.delenv("JARVIS_ORACLE_EMBED_TAIL_COOP_ENABLED", raising=False)
    idx = _make_index(O.OracleSemanticBackendStatus.STDLIB)
    await idx._embed_nodes_coop(_make_nodes(10), _chroma=False, _stdlib=True)
    assert len(idx._stdlib_store) == 10
    key = "jarvis:backend/mod_0.py:fn_0"
    vec, meta = idx._stdlib_store[key]
    assert meta["node_type"] == "function" and len(vec) == 2


async def test_coop_tail_skips_non_embeddable(monkeypatch):
    idx = _make_index(O.OracleSemanticBackendStatus.STDLIB)
    nodes = [
        O.NodeData(
            node_id=O.NodeID(
                repo="jarvis", file_path="a.py", name="v",
                node_type=O.NodeType.VARIABLE,
            ),
        )
    ]
    await idx._embed_nodes_coop(nodes, _chroma=False, _stdlib=True)
    assert idx._stdlib_store == {}


async def test_coop_tail_never_raises_on_embedder_fault(monkeypatch):
    idx = _make_index(O.OracleSemanticBackendStatus.STDLIB)

    class _Broken:
        async def embed_batch(self, texts):
            raise RuntimeError("embedder down")

    idx._embedder = _Broken()
    # Must swallow (legacy fail-soft contract) — no exception escapes.
    await idx._embed_nodes_coop(_make_nodes(3), _chroma=False, _stdlib=True)


# ── Slice 30: correlated-failure circuit breaker ─────────────────────


def test_fail_streak_abort_env_driven(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_EMBED_FAIL_STREAK_ABORT", "7")
    assert O._oracle_embed_fail_streak_abort() == 7
    monkeypatch.setenv("JARVIS_ORACLE_EMBED_FAIL_STREAK_ABORT", "junk")
    assert O._oracle_embed_fail_streak_abort() == 3


async def test_consecutive_upsert_failures_abort_the_run(monkeypatch):
    """bt-2026-07-16-004244: 16 consecutive failing chroma upserts (HNSW
    compaction broken) ballooned RSS ~10GB in 90s because the coop tail
    kept feeding the dying backend. N consecutive failures must ABORT."""
    import backend.core.ouroboros.governance.cooperative_fs_io as CFS2

    monkeypatch.setenv("JARVIS_ORACLE_EMBED_FAIL_STREAK_ABORT", "3")
    calls = {"n": 0}

    async def _always_fail_offload(fn, /, *args, **kwargs):
        # prep succeeds (inline); upsert fails
        if getattr(fn, "__name__", "") == "_prep":
            return fn()
        calls["n"] += 1
        return CFS2.OffloadError(
            fn_name="_convert_and_upsert", exc_type="InternalError",
            message="Error in compaction", cpu_bound=False,
        )

    monkeypatch.setattr(CFS2, "offload", _always_fail_offload)
    idx = _make_index(O.OracleSemanticBackendStatus.CHROMA)
    idx._collection = object()  # non-None so the chroma branch runs
    # SEMANTIC_EMBED_BATCH_SIZE is frozen at class-definition time — patch
    # the attribute, not the env.
    monkeypatch.setattr(O.OracleConfig, "SEMANTIC_EMBED_BATCH_SIZE", 1)
    # 20 nodes × batch 1 → without the breaker this is 20 failing upserts;
    # with it, exactly 3.
    await idx._embed_nodes_coop(_make_nodes(20), _chroma=True, _stdlib=False)
    assert calls["n"] == 3


async def test_streak_resets_on_success(monkeypatch):
    """A transient fault sandwiched by successes must NOT trip the breaker."""
    import backend.core.ouroboros.governance.cooperative_fs_io as CFS2

    monkeypatch.setenv("JARVIS_ORACLE_EMBED_FAIL_STREAK_ABORT", "2")
    upserts = {"n": 0}

    async def _alternating_offload(fn, /, *args, **kwargs):
        if getattr(fn, "__name__", "") == "_prep":
            return fn()
        upserts["n"] += 1
        if upserts["n"] % 2 == 1:  # odd calls fail, even succeed
            return CFS2.OffloadError(
                fn_name="_convert_and_upsert", exc_type="InternalError",
                message="transient", cpu_bound=False,
            )
        return None

    monkeypatch.setattr(CFS2, "offload", _alternating_offload)
    idx = _make_index(O.OracleSemanticBackendStatus.CHROMA)
    idx._collection = object()
    monkeypatch.setattr(O.OracleConfig, "SEMANTIC_EMBED_BATCH_SIZE", 1)
    await idx._embed_nodes_coop(_make_nodes(6), _chroma=True, _stdlib=False)
    # All 6 batches attempted (streak never reaches 2 consecutively).
    assert upserts["n"] == 6


async def test_embed_nodes_kill_switch_uses_legacy(monkeypatch):
    """Flag off → embed_nodes runs the legacy inline tail (byte-identical
    rollback), never the coop method."""
    monkeypatch.setenv("JARVIS_ORACLE_EMBED_TAIL_COOP_ENABLED", "0")
    idx = _make_index(O.OracleSemanticBackendStatus.STDLIB)

    async def _sentinel(*a, **k):
        raise AssertionError("coop tail must not run under kill switch")

    idx._embed_nodes_coop = _sentinel
    await idx.embed_nodes(_make_nodes(4))
    assert len(idx._stdlib_store) == 4
