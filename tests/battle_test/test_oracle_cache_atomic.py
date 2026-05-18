"""Arc B.1 regression spine — atomic Oracle graph-cache write.

Closes the criterion-4 failure from soak bt-2026-05-18-062703:

    `_save_cache` wrote the serialized graph directly onto the
    resolved cache path. The ProcessMemoryWatchdog's own bounded-
    shutdown `os._exit(75)` (and SIGKILL / power-loss) could land
    mid-write, leaving a torn cache blob -> next boot `_load_cache`
    failed `invalid load key '\\x00'` -> cold full reindex again.
    The checkpoint cadence (Arc B) made that torn-write window recur
    every batch, defeating durability and blocking graduation #6.

Fix: serialize into a same-directory temp file, then `os.replace`
(POSIX-atomic rename). A kill before the rename leaves the prior
good cache untouched; a kill after it sees the fully-written new
cache. There is no torn-blob window. Mirrors the established
`dw_heavy_probe._atomic_write` / `dataset_loader` pattern (no new
shared helper — bytes variant inline at the single call site).

Pins:
  * AST: _save_cache uses mkstemp + os.replace; never writes the
    serialized bytes directly to the resolved final path
  * behavioural: a kill simulated AT the rename boundary leaves the
    prior good cache byte-identical and still loadable (no '\\x00')
  * leftover temp files are cleaned up on failure
  * normal save->load round-trip still works (no Arc A regression)
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.oracle import TheOracle
from backend.core.ouroboros.governance import sandbox_paths

_REPO = Path(__file__).resolve().parents[2]
_ORACLE_SRC = _REPO / "backend/core/ouroboros/oracle.py"


@pytest.fixture(autouse=True)
def _clean_sandbox_state(monkeypatch):
    monkeypatch.delenv("JARVIS_SANDBOX_FALLBACK_DISABLED", raising=False)
    sandbox_paths.reset_cache()
    yield
    sandbox_paths.reset_cache()


@pytest.fixture
def _forced_fallback(monkeypatch, tmp_path):
    """Force the resolved cache path into a controlled tmp dir."""
    fb_root = tmp_path / "sandbox_fallback"
    monkeypatch.setattr(sandbox_paths, "_is_writable", lambda p: False)
    monkeypatch.setattr(sandbox_paths, "_fallback_root", lambda: fb_root)
    sandbox_paths.reset_cache()
    return fb_root


def _seed(o: TheOracle, marker: str) -> None:
    o._graph._graph.add_node(marker, kind="file")
    o._file_hashes[f"{marker}/k.py"] = marker


# ---------------------------------------------------------------------------
# Behavioural: kill at the rename boundary preserves the prior cache
# ---------------------------------------------------------------------------

async def test_kill_mid_write_prior_cache_survives(_forced_fallback, monkeypatch):
    # 1. Write a known-good v1 cache atomically.
    w1 = TheOracle()
    _seed(w1, "GOOD_V1")
    await w1._save_cache()
    final = TheOracle._resolved_graph_cache_path()
    assert final.exists()
    good_bytes = final.read_bytes()
    assert good_bytes[:1] != b"\x00"

    # 2. Simulate the process being killed exactly at the rename
    #    boundary (the watchdog os._exit / SIGKILL window): os.replace
    #    never completes.
    real_replace = os.replace

    def _boom(src, dst):  # noqa: ANN001
        raise RuntimeError("simulated kill at rename boundary")

    monkeypatch.setattr(os, "replace", _boom)

    w2 = TheOracle()
    _seed(w2, "TORN_V2")
    await w2._save_cache()  # must NOT raise (defensive) and must NOT corrupt

    monkeypatch.setattr(os, "replace", real_replace)

    # 3. The prior good cache is byte-identical and still loads.
    assert final.read_bytes() == good_bytes, "prior cache was clobbered"
    reader = TheOracle()
    ok = await reader._load_cache()
    assert ok is True, "prior good cache must still load (no '\\x00')"
    assert "GOOD_V1" in reader._graph._graph
    assert "TORN_V2" not in reader._graph._graph

    # 4. No leftover .tmp turds in the cache dir.
    leftovers = list(final.parent.glob(final.name + ".*.tmp"))
    assert leftovers == [], f"temp files not cleaned up: {leftovers}"


async def test_atomic_roundtrip_no_regression(_forced_fallback):
    """Normal atomic save -> fresh load still works (Arc A intact)."""
    w = TheOracle()
    _seed(w, "ROUNDTRIP")
    await w._save_cache()
    r = TheOracle()
    assert await r._load_cache() is True
    assert "ROUNDTRIP" in r._graph._graph
    assert r._file_hashes.get("ROUNDTRIP/k.py") == "ROUNDTRIP"


async def test_successful_save_leaves_no_temp(_forced_fallback):
    w = TheOracle()
    _seed(w, "CLEAN")
    await w._save_cache()
    final = TheOracle._resolved_graph_cache_path()
    assert list(final.parent.glob(final.name + ".*.tmp")) == []


# ---------------------------------------------------------------------------
# AST pin — atomic discipline survives refactors
# ---------------------------------------------------------------------------

def _save_cache_node() -> ast.AST:
    tree = ast.parse(_ORACLE_SRC.read_text())
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_save_cache":
            return n
    pytest.fail("_save_cache not found")
    raise RuntimeError("unreachable")


def test_ast_pin_save_cache_is_atomic():
    src = ast.unparse(_save_cache_node())
    assert "mkstemp" in src, "_save_cache must serialize into a temp file"
    assert "os.replace" in src, "_save_cache must promote via os.replace"
    # The serialized bytes must be written to the temp path, never
    # straight onto the resolved final path (that was the torn-write).
    assert "_resolved_graph_cache_path().write_bytes" not in src
    assert "_final_cache_path.write_bytes" not in src
    assert "_final_cache_path.write_text" not in src
