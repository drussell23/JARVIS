"""Slice 25 — throughput: fastembed durable cache + Oracle upsert off-loop.

Soak bt-2026-07-15-180437 survived (loop stable, Slices 22-24) but stopped on
idle_timeout: throughput collapsed. Three isolated root causes:
  * fastembed ONNX weights re-downloaded into an ephemeral tempdir the harness
    (HF_HUB_OFFLINE) blocks → the GIL-holding stdlib fallback took over;
  * the chromadb HNSW upsert/compaction ran SYNC on the asyncio loop (57.8s
    ControlPlaneStarvation spike + a swallowed "Error in compaction");
  * DW RT p50 TTFT ~67s parked workers — but a flat sock_read would kill
    legitimate slow-first-token streams, so the fix is arming the existing
    phase-aware watchdog + TTFT demotion, not a new socket timeout.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from backend.core.ouroboros.governance import semantic_index as SI


# ── fastembed durable cache ──────────────────────────────────────────


def test_fastembed_cache_dir_env_driven(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FASTEMBED_CACHE_DIR", str(tmp_path / "fe"))
    d = SI._fastembed_cache_dir()
    assert d == (tmp_path / "fe") and d.is_dir()


def test_fastembed_cache_dir_default(monkeypatch):
    monkeypatch.delenv("JARVIS_FASTEMBED_CACHE_DIR", raising=False)
    d = SI._fastembed_cache_dir()
    assert d is not None and d.name == "fastembed_cache"


def test_lazy_init_passes_cache_dir():
    src = inspect.getsource(SI._Embedder._lazy_init)
    assert "cache_dir=str(_cache_dir)" in src
    assert "_fastembed_cache_dir()" in src


def test_prefetch_script_exists_and_verifies():
    p = Path("scripts/prefetch_fastembed.py")
    assert p.is_file()
    src = p.read_text()
    # salvages a warm tempdir cache (zero network) AND proves with a real encode
    assert "fastembed_cache" in src
    assert "model.embed(" in src
    assert "_fastembed_cache_dir" in src


# ── Oracle chroma upsert off-loop ────────────────────────────────────


def test_oracle_upsert_offloaded_to_thread_not_process():
    import backend.core.ouroboros.oracle as O
    src = inspect.getsource(O.OracleSemanticIndex.embed_nodes)
    # the upsert is awaited through the unified offload substrate...
    assert "await _cr_offload(" in src
    assert "self._collection.upsert" in src
    # ...on the THREAD path (process pool would corrupt the shared persist dir)
    assert "cpu_bound=False" in src
    assert "cpu_bound=True" not in src
    # fail-soft preserved: an OffloadError re-raises into the existing except
    assert "_cr_is_err(_up)" in src


def test_oracle_process_pool_rejection_documented():
    """The bulletproof rationale (why NOT a process pool) must be in the code
    so a future refactor doesn't reintroduce segment corruption."""
    import backend.core.ouroboros.oracle as O
    src = inspect.getsource(O.OracleSemanticIndex.embed_nodes)
    assert "PROCESS pool is deliberately NOT" in src
    assert "CORRUPTS" in src or "corrupt" in src.lower()


# ── DW: the correct (existing, phase-aware) mechanism, not a flat sock_read ──


def test_dw_flat_sock_read_rejected_with_rationale():
    import backend.core.ouroboros.governance.doubleword_provider as DW
    src = inspect.getsource(DW)
    # a flat sock_read was explicitly considered and rejected (would kill the
    # legitimate ~67s TTFT wait); no _dw_sock_read_timeout_s helper shipped.
    assert "sock_read" in src and "REJECTED" in src
    assert "def _dw_sock_read_timeout_s" not in src
