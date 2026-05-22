"""Slice 10 — Oracle semantic native-runtime isolation tests.

Closes the chromadb GIL-starvation wedge from bt-2026-05-22-010120
(Slice 7f acceptance soak). The pre-Slice-10 ``OracleSemanticIndex``
constructor synchronously loaded ChromaDB during Oracle boot; the
``chromadb_rust_bindings`` tokio worker threads competed with the
main Python asyncio thread for the GIL, starving the event loop
for minutes (sample showed the main thread blocked at
``PyEval_RestoreThread → _pthread_cond_wait`` while ~6
tokio-runtime-workers ran in ``chromadb_rust_bindings.abi3.so``).

Slice 10 — lazy + bounded-executor isolation + closed-taxonomy
backend status. The boot path NEVER touches chromadb; the first
semantic query triggers a bounded executor load with timeout +
graceful DEGRADED fallback.

Test surface:
  * Closed-taxonomy AST pin — ``OracleSemanticBackendStatus``
    5-value enum.
  * **AST cage Pin 1** — ``import chromadb`` in oracle.py appears
    in EXACTLY ONE function body: ``_load_chromadb_sync``. The
    Oracle boot path / Oracle.__init__ / Oracle.initialize /
    OracleSemanticIndex.__init__ all forbidden.
  * **AST cage Pin 2** — ``OracleSemanticIndex.__init__`` body
    contains no chromadb references at all.
  * **Constructor-PENDING pin** — fresh instance is
    ``status=PENDING``.
  * **DISABLED-fast-path pin** — env opt-out resolves without
    touching chromadb.
  * **Timeout-DEGRADED pin** — controllable slow loader exceeds
    the bounded timeout; status flips to DEGRADED; queries return
    empty.
  * **Exception-DEGRADED pin** — controllable raising loader →
    DEGRADED; queries return empty; no exception propagates.
  * **Lazy semantic_search pin** — query triggers init; query
    returns empty on DEGRADED.
  * **Lazy embed_nodes pin** — same lazy-init + no-op on DEGRADED.
  * **Idempotency pin** — multiple ``initialize_backend()`` calls
    return cached status without re-running loader.
  * **Graph readiness independence pin** — Oracle init signals
    graph_ready REGARDLESS of semantic status (semantic cannot
    block graph readiness — operator binding).
  * Env-knob clamp — timeout bounded to [1.0, 300.0] s.
  * Public surface pin.
"""

from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import time
import unittest
from typing import List, Tuple
from unittest.mock import MagicMock, patch

from backend.core.ouroboros.oracle import (
    BackendStatus,
    OracleSemanticBackendStatus,
    OracleSemanticIndex,
)
from backend.core.ouroboros import oracle as oracle_mod


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_ORACLE_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "oracle.py"
)


def _parse_oracle() -> ast.Module:
    return ast.parse(_ORACLE_FILE.read_text())


# ============================================================================
# Closed-taxonomy enum
# ============================================================================


class TestBackendStatusClosedTaxonomy(unittest.TestCase):
    """``OracleSemanticBackendStatus`` is a closed 5-value taxonomy.
    Adding a 6th value requires bumping this pin + every consumer
    branch."""

    def test_exactly_five_members(self) -> None:
        self.assertEqual(
            len(list(OracleSemanticBackendStatus)), 5,
            f"Closed taxonomy: found "
            f"{[m.name for m in OracleSemanticBackendStatus]}",
        )

    def test_member_names_and_values(self) -> None:
        self.assertEqual(
            {m.name for m in OracleSemanticBackendStatus},
            {"PENDING", "CHROMA", "STDLIB", "DISABLED", "DEGRADED"},
        )
        # Telemetry contract — values are lower-case.
        for m in OracleSemanticBackendStatus:
            self.assertEqual(m.value, m.name.lower())

    def test_legacy_backendstatus_alias(self) -> None:
        """``BackendStatus`` is a backward-compat alias."""
        self.assertIs(BackendStatus, OracleSemanticBackendStatus)


# ============================================================================
# AST cage Pin 1 — import chromadb in EXACTLY one function body
# ============================================================================


class TestChromadbImportCage(unittest.TestCase):
    """Operator binding: ``import chromadb`` only allowed inside
    isolated backend/worker boundary. In Slice 10's lazy + executor
    architecture, the SOLE permitted import site is
    ``OracleSemanticIndex._load_chromadb_sync`` (the executor-
    thread loader). Any other import site fails the cage."""

    def _find_chromadb_imports(
        self, tree: ast.Module,
    ) -> List[Tuple[ast.AST, int]]:
        """Return (node, lineno) pairs for every ``import chromadb``
        or ``from chromadb ...`` reference in the tree."""
        out: List[Tuple[ast.AST, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "chromadb" or alias.name.startswith(
                        "chromadb."
                    ):
                        out.append((node, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module == "chromadb"
                    or node.module.startswith("chromadb.")
                ):
                    out.append((node, node.lineno))
        return out

    def _enclosing_function(
        self, tree: ast.Module, target_lineno: int,
    ) -> str:
        """Return the name of the smallest function enclosing
        ``target_lineno``, or ``"<module>"`` for top-level."""
        best_name = "<module>"
        best_span = float("inf")
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            if not hasattr(node, "lineno"):
                continue
            end = getattr(node, "end_lineno", None) or target_lineno
            if node.lineno <= target_lineno <= end:
                span = end - node.lineno
                if span < best_span:
                    best_span = span
                    best_name = node.name
        return best_name

    def test_chromadb_import_only_in_executor_loader(self) -> None:
        tree = _parse_oracle()
        imports = self._find_chromadb_imports(tree)
        self.assertEqual(
            len(imports), 1,
            f"oracle.py must contain EXACTLY ONE import of "
            f"chromadb (in _load_chromadb_sync). Found "
            f"{len(imports)} at line(s) "
            f"{[ln for _, ln in imports]}.",
        )
        node, lineno = imports[0]
        enclosing = self._enclosing_function(tree, lineno)
        self.assertEqual(
            enclosing, "_load_chromadb_sync",
            f"Slice 10 cage violated — chromadb imported in "
            f"function {enclosing!r} at L{lineno}. The SOLE "
            f"permitted import site is _load_chromadb_sync (the "
            f"executor-thread loader). Move the import or fail the "
            f"asyncio loop again under chromadb's tokio workers.",
        )

    def test_no_chromadb_in_oracle_init(self) -> None:
        """The OracleSemanticIndex constructor MUST NOT reference
        chromadb (the Slice 10 lightweight-constructor invariant)."""
        tree = _parse_oracle()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ClassDef)
                and node.name == "OracleSemanticIndex"
            ):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.FunctionDef)
                    and sub.name == "__init__"
                ):
                    src = ast.unparse(sub)
                    self.assertNotIn(
                        "chromadb", src,
                        "OracleSemanticIndex.__init__ MUST NOT "
                        "reference chromadb. Slice 10 boot-path "
                        "isolation broken.",
                    )

    def test_no_chromadb_in_the_oracle_class(self) -> None:
        """``TheOracle`` class (the boot orchestrator) MUST NOT
        directly import chromadb anywhere in its body. All
        chromadb access flows through OracleSemanticIndex."""
        tree = _parse_oracle()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ClassDef)
                and node.name == "TheOracle"
            ):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    for alias in sub.names:
                        self.assertNotIn(
                            "chromadb", alias.name,
                            f"TheOracle class MUST NOT import "
                            f"chromadb directly (found at L"
                            f"{sub.lineno}).",
                        )
                elif (
                    isinstance(sub, ast.ImportFrom)
                    and sub.module
                ):
                    self.assertNotIn(
                        "chromadb", sub.module,
                        f"TheOracle class MUST NOT import from "
                        f"chromadb (found at L{sub.lineno}).",
                    )


# ============================================================================
# Constructor-PENDING + lightweight pin
# ============================================================================


class TestConstructorLightweight(unittest.TestCase):
    """Slice 10 invariant: ``OracleSemanticIndex()`` returns
    immediately with status=PENDING. No chromadb load, no
    disk I/O, no embedder construction."""

    def test_fresh_instance_is_pending(self) -> None:
        idx = OracleSemanticIndex()
        self.assertEqual(
            idx.backend_status, OracleSemanticBackendStatus.PENDING,
        )
        self.assertEqual(idx.backend_status_value, "pending")
        # Legacy compat — is_ready returns False until init attempted.
        self.assertFalse(idx.is_ready())

    def test_constructor_does_not_load_chromadb(self) -> None:
        """Assert the constructor completes in well under any
        plausible chromadb load time (chromadb PersistentClient
        init typically takes hundreds of ms; we bound to 200ms
        with margin)."""
        t0 = time.monotonic()
        OracleSemanticIndex()
        elapsed = time.monotonic() - t0
        self.assertLess(
            elapsed, 0.2,
            f"Constructor took {elapsed*1000:.0f}ms — too slow; "
            f"Slice 10 lightweight invariant broken (suggests "
            f"chromadb is being loaded eagerly)",
        )


# ============================================================================
# DISABLED fast-path
# ============================================================================


class _EnvGuard:
    def __init__(self, **overrides):
        self._o = overrides
        self._prior = {}

    def __enter__(self):
        for k, v in self._o.items():
            self._prior[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, p in self._prior.items():
            if p is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = p


class TestDisabledFastPath(unittest.IsolatedAsyncioTestCase):
    """When ``JARVIS_ORACLE_SEMANTIC_BACKEND=disabled``, the
    backend resolves to DISABLED without touching chromadb. Boot
    is bounded by the env-read cost."""

    async def test_disabled_env_resolves_without_chromadb(self) -> None:
        with _EnvGuard(JARVIS_ORACLE_SEMANTIC_BACKEND="disabled"):
            idx = OracleSemanticIndex()
            t0 = time.monotonic()
            status = await idx.initialize_backend()
            elapsed = time.monotonic() - t0
        self.assertEqual(status, OracleSemanticBackendStatus.DISABLED)
        self.assertEqual(idx.backend_status_value, "disabled")
        # Should be near-instant — definitely not "30s timeout".
        self.assertLess(elapsed, 1.0)

    async def test_disabled_semantic_search_returns_empty(self) -> None:
        with _EnvGuard(JARVIS_ORACLE_SEMANTIC_BACKEND="disabled"):
            idx = OracleSemanticIndex()
            results = await idx.semantic_search("anything")
        self.assertEqual(results, [])

    async def test_disabled_embed_nodes_is_noop(self) -> None:
        with _EnvGuard(JARVIS_ORACLE_SEMANTIC_BACKEND="disabled"):
            idx = OracleSemanticIndex()
            await idx.embed_nodes([])  # empty list; just exercise the no-op


# ============================================================================
# Timeout-DEGRADED pin — controllable slow loader
# ============================================================================


class TestTimeoutDegradesGracefully(unittest.IsolatedAsyncioTestCase):
    """A controllable slow loader exceeds the bounded timeout;
    status flips to DEGRADED; queries return empty; no exception
    propagates to the caller."""

    async def test_slow_loader_times_out_to_degraded(self) -> None:
        with _EnvGuard(JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S="1.0"):
            idx = OracleSemanticIndex()
            # Replace the executor-thread loader with one that
            # sleeps far past the 1.0s timeout.
            def _slow_loader(_self=idx):
                time.sleep(5.0)
            with patch.object(
                idx, "_load_chromadb_sync", _slow_loader,
            ):
                t0 = time.monotonic()
                status = await idx.initialize_backend()
                elapsed = time.monotonic() - t0
        self.assertEqual(
            status, OracleSemanticBackendStatus.DEGRADED,
        )
        # Helper must terminate within the bounded grace.
        self.assertLess(
            elapsed, 4.0,
            f"Helper should terminate within ~1s + grace; "
            f"elapsed={elapsed:.2f}s",
        )

    async def test_degraded_semantic_search_returns_empty(self) -> None:
        with _EnvGuard(JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S="1.0"):
            idx = OracleSemanticIndex()
            def _slow_loader(_self=idx):
                time.sleep(5.0)
            with patch.object(idx, "_load_chromadb_sync", _slow_loader):
                results = await idx.semantic_search("anything")
        self.assertEqual(results, [])


# ============================================================================
# Exception-DEGRADED pin
# ============================================================================


class TestExceptionDegradesGracefully(unittest.IsolatedAsyncioTestCase):
    """A loader that raises (e.g. chromadb import fails / persist
    dir unwritable / Settings rejected) flips status to DEGRADED
    without propagating the exception."""

    async def test_raising_loader_degrades(self) -> None:
        idx = OracleSemanticIndex()
        def _raising_loader(_self=idx):
            raise RuntimeError("simulated chromadb load failure")
        with patch.object(idx, "_load_chromadb_sync", _raising_loader):
            status = await idx.initialize_backend()
        self.assertEqual(status, OracleSemanticBackendStatus.DEGRADED)

    async def test_raising_loader_does_not_propagate(self) -> None:
        """The caller of initialize_backend() MUST NOT see an
        exception. This is the fault-isolation guarantee."""
        idx = OracleSemanticIndex()
        def _raising_loader(_self=idx):
            raise ValueError("simulated catastrophic chromadb failure")
        with patch.object(idx, "_load_chromadb_sync", _raising_loader):
            # No assertRaises — the call MUST return cleanly.
            status = await idx.initialize_backend()
        self.assertEqual(status, OracleSemanticBackendStatus.DEGRADED)


# ============================================================================
# Idempotency
# ============================================================================


class TestInitializeIdempotency(unittest.IsolatedAsyncioTestCase):
    """Multiple ``initialize_backend()`` calls return the cached
    status; the loader is invoked AT MOST ONCE."""

    async def test_multiple_calls_load_once(self) -> None:
        idx = OracleSemanticIndex()
        load_count = [0]
        def _counting_loader(_self=idx):
            load_count[0] += 1
            # Fast successful load — but won't fully succeed because
            # we don't set _collection / _embedder. The status stays
            # CHROMA (since loader didn't raise) — but for this test
            # we only care about call count, not final status.
            idx._collection = MagicMock()  # type: ignore[assignment]
            idx._embedder = MagicMock()  # type: ignore[assignment]
        with patch.object(idx, "_load_chromadb_sync", _counting_loader):
            s1 = await idx.initialize_backend()
            s2 = await idx.initialize_backend()
            s3 = await idx.initialize_backend()
        self.assertEqual(load_count[0], 1, "Loader must be one-shot")
        self.assertEqual(s1, s2)
        self.assertEqual(s2, s3)


# ============================================================================
# Lazy query path — semantic_search triggers init
# ============================================================================


class TestLazyQueryPath(unittest.IsolatedAsyncioTestCase):
    """semantic_search() and embed_nodes() MUST call
    _ensure_initialized() so the first query triggers the
    bounded executor load (the lazy-init invariant)."""

    async def test_semantic_search_triggers_init(self) -> None:
        with _EnvGuard(JARVIS_ORACLE_SEMANTIC_BACKEND="disabled"):
            idx = OracleSemanticIndex()
            self.assertEqual(
                idx.backend_status,
                OracleSemanticBackendStatus.PENDING,
            )
            await idx.semantic_search("query")
            self.assertEqual(
                idx.backend_status,
                OracleSemanticBackendStatus.DISABLED,
            )

    async def test_embed_nodes_triggers_init(self) -> None:
        with _EnvGuard(JARVIS_ORACLE_SEMANTIC_BACKEND="disabled"):
            idx = OracleSemanticIndex()
            self.assertEqual(
                idx.backend_status,
                OracleSemanticBackendStatus.PENDING,
            )
            await idx.embed_nodes([])
            self.assertEqual(
                idx.backend_status,
                OracleSemanticBackendStatus.DISABLED,
            )


# ============================================================================
# Env-knob clamp
# ============================================================================


class TestEnvKnobClamp(unittest.TestCase):
    """``JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S`` is clamped to
    [1.0, 300.0]. Below the floor → 1.0; above the ceiling → 300.0;
    malformed → default 30.0."""

    def test_default(self) -> None:
        os.environ.pop("JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S", None)
        self.assertEqual(
            OracleSemanticIndex._resolve_init_timeout_s(), 30.0,
        )

    def test_clamp_floor(self) -> None:
        os.environ["JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S"] = "0.1"
        self.assertEqual(
            OracleSemanticIndex._resolve_init_timeout_s(), 1.0,
        )

    def test_clamp_ceiling(self) -> None:
        os.environ["JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S"] = "9999"
        self.assertEqual(
            OracleSemanticIndex._resolve_init_timeout_s(), 300.0,
        )

    def test_invalid_uses_default(self) -> None:
        os.environ["JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S"] = "garbage"
        self.assertEqual(
            OracleSemanticIndex._resolve_init_timeout_s(), 30.0,
        )

    def tearDown(self) -> None:
        os.environ.pop("JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S", None)


# ============================================================================
# Graph-readiness independence (operator binding)
# ============================================================================


class TestGraphReadinessIndependence(unittest.TestCase):
    """Operator binding: *"semantic cannot block graph readiness"*.
    AST inspection of ``TheOracle.initialize`` proves the call
    order — graph readiness is marked BEFORE the semantic-index
    construction line, AND the semantic-index construction is now
    lightweight (no chromadb load at this site)."""

    def test_graph_ready_marked_before_semantic_index_construction(self) -> None:
        tree = _parse_oracle()
        # Find TheOracle.initialize
        method = None
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ClassDef)
                and node.name == "TheOracle"
            ):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.AsyncFunctionDef)
                    and sub.name == "initialize"
                ):
                    method = sub
                    break
            if method is not None:
                break
        self.assertIsNotNone(
            method,
            "TheOracle.initialize must exist",
        )
        src = ast.unparse(method)  # type: ignore[arg-type]
        mark_graph_idx = src.find("mark_graph_ready")
        semantic_construct_idx = src.find("OracleSemanticIndex()")
        self.assertGreater(mark_graph_idx, 0)
        self.assertGreater(semantic_construct_idx, 0)
        self.assertLess(
            mark_graph_idx, semantic_construct_idx,
            "TheOracle.initialize MUST call mark_graph_ready() "
            "BEFORE constructing OracleSemanticIndex(). The "
            "graph→semantic ordering invariant + Slice 10 "
            "lightweight constructor together mean semantic "
            "cannot block graph readiness.",
        )

    def test_initialize_does_not_await_initialize_backend(self) -> None:
        """TheOracle.initialize must NOT await
        ``self._semantic_index.initialize_backend()`` — if it did,
        the boot path would pay the chromadb load cost. The lazy
        load triggers on first query, not at boot."""
        tree = _parse_oracle()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ClassDef)
                and node.name == "TheOracle"
            ):
                continue
            for sub in ast.walk(node):
                if not (
                    isinstance(sub, ast.AsyncFunctionDef)
                    and sub.name == "initialize"
                ):
                    continue
                src = ast.unparse(sub)
                self.assertNotIn(
                    "initialize_backend",
                    src,
                    "TheOracle.initialize MUST NOT call "
                    "initialize_backend() at boot. The lazy load "
                    "triggers on first query.",
                )
                self.assertNotIn(
                    "_ensure_initialized",
                    src,
                    "TheOracle.initialize MUST NOT call "
                    "_ensure_initialized() at boot.",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
