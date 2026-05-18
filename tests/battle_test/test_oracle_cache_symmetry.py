"""Arc A regression spine — Oracle graph-cache load/save symmetry.

Root cause this pins shut (see memory: 52GB soak OOM):

    ``_load_cache`` read the *primary* path
    ``~/.jarvis/oracle/codebase_graph.pkl`` directly while
    ``_save_cache`` wrote through ``sandbox_fallback``. Under the Iron
    Gate the primary is non-writable, so every save landed in
    ``.ouroboros/state/sandbox_fallback/oracle/`` while every load
    looked at the (stale/absent) primary -> cold full reindex of
    24,735 files on EVERY sandboxed boot, never converging, accreting
    an unbounded partial graph until OOM.

The fix introduces ``TheOracle._resolved_graph_cache_path()`` as the
single source of truth that BOTH load and save resolve through.

Pins:
  * resolver returns primary when writable (zero-change for dev)
  * resolver returns fallback when primary non-writable (Iron Gate)
  * save->load round-trip succeeds under sandbox (the actual bug)
  * save->load round-trip succeeds with writable primary (no regression)
  * AST pin: _load_cache & _save_cache go through the resolver and
    never touch raw OracleConfig.GRAPH_CACHE_FILE for I/O
  * AST pin: resolver composes the existing sandbox_fallback substrate
  * AST pin: harness legacy FileHandler add is gated behind the
    silent_boot marker (no 2x debug.log emission)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.oracle import OracleConfig, TheOracle
from backend.core.ouroboros.governance import sandbox_paths

_REPO = Path(__file__).resolve().parents[2]
_ORACLE_SRC = _REPO / "backend/core/ouroboros/oracle.py"
_HARNESS_SRC = _REPO / "backend/core/ouroboros/battle_test/harness.py"


@pytest.fixture(autouse=True)
def _clean_sandbox_state(monkeypatch):
    """Ensure each test starts from a clean sandbox-resolution cache."""
    monkeypatch.delenv("JARVIS_SANDBOX_FALLBACK_DISABLED", raising=False)
    sandbox_paths.reset_cache()
    yield
    sandbox_paths.reset_cache()


# ---------------------------------------------------------------------------
# Resolver invariants
# ---------------------------------------------------------------------------

def test_resolver_returns_primary_when_writable(monkeypatch):
    monkeypatch.setattr(sandbox_paths, "_is_writable", lambda p: True)
    sandbox_paths.reset_cache()
    resolved = TheOracle._resolved_graph_cache_path()
    assert resolved == OracleConfig.GRAPH_CACHE_FILE


def test_resolver_returns_fallback_when_not_writable(monkeypatch, tmp_path):
    fb_root = tmp_path / "sandbox_fallback"
    monkeypatch.setattr(sandbox_paths, "_is_writable", lambda p: False)
    monkeypatch.setattr(sandbox_paths, "_fallback_root", lambda: fb_root)
    sandbox_paths.reset_cache()
    resolved = TheOracle._resolved_graph_cache_path()
    assert resolved != OracleConfig.GRAPH_CACHE_FILE
    assert str(resolved).startswith(str(fb_root))


# ---------------------------------------------------------------------------
# Round-trip: the actual leak. Before the fix, the sandbox round-trip
# returned False (load looked at the stale primary) -> cold reindex.
# ---------------------------------------------------------------------------

def _seed(oracle: TheOracle) -> None:
    oracle._graph._graph.add_node("sentinel::node", kind="file")
    oracle._file_hashes["sentinel/key.py"] = "deadbeef"


def _assert_loaded(oracle: TheOracle) -> None:
    assert "sentinel::node" in oracle._graph._graph
    assert oracle._file_hashes.get("sentinel/key.py") == "deadbeef"


async def test_roundtrip_under_sandbox(monkeypatch, tmp_path):
    """Primary non-writable -> save+load BOTH use the same fallback."""
    fb_root = tmp_path / "sandbox_fallback"
    monkeypatch.setattr(sandbox_paths, "_is_writable", lambda p: False)
    monkeypatch.setattr(sandbox_paths, "_fallback_root", lambda: fb_root)
    sandbox_paths.reset_cache()

    writer = TheOracle()
    _seed(writer)
    await writer._save_cache()

    resolved = TheOracle._resolved_graph_cache_path()
    assert resolved.exists()
    assert str(resolved).startswith(str(fb_root))
    # The stale primary must NOT be what carried the data.
    assert resolved != OracleConfig.GRAPH_CACHE_FILE

    reader = TheOracle()
    loaded = await reader._load_cache()
    assert loaded is True, "sandbox round-trip must hit (the OOM root cause)"
    _assert_loaded(reader)


async def test_roundtrip_primary_writable(monkeypatch, tmp_path):
    """Non-sandbox dev: both use the primary, no regression."""
    primary = tmp_path / "oracle" / "codebase_graph.pkl"
    monkeypatch.setattr(OracleConfig, "GRAPH_CACHE_FILE", primary)
    monkeypatch.setattr(sandbox_paths, "_is_writable", lambda p: True)
    sandbox_paths.reset_cache()

    writer = TheOracle()
    _seed(writer)
    await writer._save_cache()
    assert primary.exists()

    reader = TheOracle()
    loaded = await reader._load_cache()
    assert loaded is True
    _assert_loaded(reader)


# ---------------------------------------------------------------------------
# AST pins — structural guarantees that survive refactors
# ---------------------------------------------------------------------------

def _func(src: str, name: str) -> ast.AST:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    pytest.fail(f"{name} not found")
    raise RuntimeError("unreachable")


def test_ast_pin_load_and_save_use_resolver_not_raw_path():
    src = _ORACLE_SRC.read_text()
    for fn_name in ("_load_cache", "_save_cache"):
        node = _func(src, fn_name)
        unparsed = ast.unparse(node)
        assert "_resolved_graph_cache_path" in unparsed, (
            f"{fn_name} must resolve the cache path through the single "
            f"source of truth"
        )
        assert "GRAPH_CACHE_FILE" not in unparsed, (
            f"{fn_name} must NOT touch the raw primary path for I/O — "
            f"that asymmetry was the 52GB OOM root cause"
        )


def test_ast_pin_resolver_composes_sandbox_fallback():
    node = _func(_ORACLE_SRC.read_text(), "_resolved_graph_cache_path")
    unparsed = ast.unparse(node)
    assert "sandbox_fallback" in unparsed
    assert "GRAPH_CACHE_FILE" in unparsed


def test_ast_pin_harness_legacy_filehandler_gated_by_silent_boot_marker():
    """The legacy debug.log FileHandler add must be guarded by the
    silent_boot marker so it does NOT double-emit every log line."""
    src = _HARNESS_SRC.read_text()
    # The silent_boot marker must be imported and consulted, and the
    # raw FileHandler add must be reachable only when it is absent.
    assert "_HANDLER_MARKER as _SB_MARKER" in src
    assert "_sb_installed" in src
    marker_idx = src.index("_sb_installed = any(")
    add_idx = src.index("_root.addHandler(_file_handler)")
    assert marker_idx < add_idx, (
        "silent_boot marker guard must precede the legacy addHandler"
    )
