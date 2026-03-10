# Oracle Structural + Semantic Fusion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fuse ChromaDB semantic similarity with the existing depth-1 graph neighborhood so J-Prime receives a ranked, dual-section file manifest: structurally connected files (from graph edges) plus semantically similar code (discovered via embedding search).

**Architecture:** `OracleSemanticIndex` class added to `oracle.py` (handles ChromaDB + SentenceTransformer); `FileNeighborhood` extended with `semantic_support` field; `TheOracle.get_fused_neighborhood()` async fusion method; `ContextExpander.expand()` awaits fusion and renders two labeled sections with strict 10-file-per-category token guardrail.

**Tech Stack:** ChromaDB `PersistentClient` (v1.0+ API), `sentence-transformers` (`all-MiniLM-L6-v2`, 384-dim), NetworkX DiGraph (existing), Python 3.11 asyncio, pytest with `asyncio_mode = auto`

---

## Key File Locations (read before editing)

- `backend/core/ouroboros/oracle.py` — 2070 lines
  - `OracleConfig`: line 85 (add `CHROMA_PERSIST_DIR` here)
  - `FileNeighborhood` dataclass: line 258 (add `semantic_support` field)
  - `OracleSemanticIndex` class: insert at line 1098 (before `# THE ORACLE` section comment)
  - `TheOracle.__init__`: line 1110 (add `self._semantic_index = OracleSemanticIndex()`)
  - `TheOracle.full_index()`: line 1153 (add embedding call after indexing)
  - `TheOracle.incremental_update()`: line 1178 (add embedding call after update)
  - `get_file_neighborhood()`: line 1637 (unchanged — existing method)
  - `get_fused_neighborhood()`: insert before `get_relevant_files_for_query` at line 1776
- `backend/core/ouroboros/governance/context_expander.py` — update `expand()` and `_render_neighborhood_section()`
- `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py` — new test file

## CRITICAL Rules

- **NEVER `@pytest.mark.asyncio`** — project uses `asyncio_mode = auto` in `pytest.ini`
- Run tests: `python3 -m pytest <test_file> -v` from `/Users/djrussell23/Documents/repos/JARVIS-AI-Agent`
- `OracleSemanticIndex.__init__` must NEVER raise — ChromaDB/SentenceTransformer import failures set `_available = False`
- `get_fused_neighborhood` is `async def` (needs to await embedding search)
- `get_file_neighborhood` stays `def` (sync, unchanged)
- All semantic operations wrapped in `try/except` — oracle failures never block the pipeline

---

### Task 1: Add OracleSemanticIndex Class and OracleConfig Extension

**Files:**
- Modify: `backend/core/ouroboros/oracle.py`
- Create: `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py`

---

**Step 1: Write failing tests**

Create `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py`:

```python
"""Tests for OracleSemanticIndex and semantic fusion."""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class TestOracleSemanticIndex:
    """Unit tests for OracleSemanticIndex."""

    def test_is_ready_false_when_unavailable(self):
        from backend.core.ouroboros.oracle import OracleSemanticIndex
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = False
        assert idx.is_ready() is False

    def test_is_ready_true_when_available(self):
        from backend.core.ouroboros.oracle import OracleSemanticIndex
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = True
        assert idx.is_ready() is True

    async def test_semantic_search_returns_empty_when_not_available(self):
        from backend.core.ouroboros.oracle import OracleSemanticIndex
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = False
        result = await idx.semantic_search("find authentication code", k=5)
        assert result == []

    async def test_embed_nodes_skips_when_not_available(self):
        """embed_nodes must not raise when unavailable."""
        from backend.core.ouroboros.oracle import OracleSemanticIndex, NodeData, NodeID, NodeType
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = False
        node = MagicMock()
        # Should not raise
        await idx.embed_nodes([node])

    async def test_embed_nodes_skips_nodes_without_content(self, tmp_path):
        """Nodes with no docstring AND no signature are skipped."""
        from backend.core.ouroboros.oracle import OracleSemanticIndex, NodeData, NodeID, NodeType
        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = True
        idx._collection = MagicMock()
        idx._embedder = AsyncMock()
        idx._embedder.embed_batch = AsyncMock(return_value=[])

        node_id = NodeID(repo="jarvis", file_path="core/foo.py", name="foo",
                         node_type=NodeType.FUNCTION, line_number=1)
        node = NodeData(node_id=node_id, docstring=None, signature=None)

        # Should not raise, and embedder should not be called
        await idx.embed_nodes([node])
        idx._embedder.embed_batch.assert_not_called()

    async def test_semantic_search_returns_file_score_tuples(self):
        """semantic_search returns List[Tuple[repo:file_path, float]]."""
        from backend.core.ouroboros.oracle import OracleSemanticIndex
        import numpy as np

        idx = OracleSemanticIndex.__new__(OracleSemanticIndex)
        idx._available = True
        idx._embedder = AsyncMock()
        idx._embedder.embed = AsyncMock(return_value=np.zeros(384))

        mock_collection = MagicMock()
        mock_collection.query.return_value = {
            "ids": [["id1", "id2"]],
            "distances": [[0.1, 0.3]],
            "metadatas": [[
                {"repo": "jarvis", "file_path": "core/auth.py"},
                {"repo": "prime", "file_path": "services/auth.py"},
            ]],
        }
        idx._collection = mock_collection

        results = await idx.semantic_search("authentication logic", k=2)

        assert len(results) == 2
        assert results[0][0] == "jarvis:core/auth.py"
        assert results[1][0] == "prime:services/auth.py"
        # distance 0.1 → similarity 0.9; distance 0.3 → similarity 0.7
        assert abs(results[0][1] - 0.9) < 0.01
        assert abs(results[1][1] - 0.7) < 0.01
```

**Step 2: Run tests to verify they fail**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python3 -m pytest tests/test_ouroboros_governance/test_oracle_semantic_fusion.py::TestOracleSemanticIndex -v
```

Expected: `ImportError: cannot import name 'OracleSemanticIndex'`

**Step 3: Add CHROMA_PERSIST_DIR to OracleConfig**

In `oracle.py`, find `OracleConfig` at line 85. After the `INDEX_CACHE_FILE` line (line 96), add:

```python
    # Semantic index (ChromaDB)
    CHROMA_PERSIST_DIR = Path(os.getenv(
        "ORACLE_CHROMA_DIR",
        Path.home() / ".jarvis/oracle/chroma",
    ))
    CHROMA_COLLECTION_NAME: str = os.getenv(
        "ORACLE_CHROMA_COLLECTION", "jarvis_oracle_symbols"
    )
    SEMANTIC_EMBED_MODEL: str = os.getenv(
        "ORACLE_EMBED_MODEL", "all-MiniLM-L6-v2"
    )
    SEMANTIC_EMBED_BATCH_SIZE: int = int(os.getenv("ORACLE_EMBED_BATCH", "128"))
```

**Step 4: Add OracleSemanticIndex class**

Insert the following class at line 1098 (before the `# =============================================================================` comment that precedes `THE ORACLE`). Use Edit tool — match on the section comment text:

```python
# =============================================================================
# ORACLE SEMANTIC INDEX — ChromaDB + SentenceTransformer
# =============================================================================

class OracleSemanticIndex:
    """Manages ChromaDB embeddings for code symbols (functions, methods, classes).

    Embedded text per node: ``"{name} {signature} {docstring}"`` (truncated to 512 chars).
    Indexed node types: CLASS, FUNCTION, METHOD only.

    **Fault isolation guarantee:** ``__init__`` never raises.  All public methods
    return empty results silently when ``_available`` is ``False``.
    """

    # Node types worth embedding — others carry no useful semantic content
    _EMBEDDABLE_TYPES = {NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD}
    # Max chars fed to the embedding model per node
    _MAX_EMBED_CHARS: int = 512

    def __init__(self, persist_dir: Optional[Path] = None, collection_name: Optional[str] = None) -> None:
        self._available: bool = False
        self._collection: Optional[Any] = None
        self._embedder: Optional[Any] = None
        self._persist_dir = persist_dir or OracleConfig.CHROMA_PERSIST_DIR
        self._collection_name = collection_name or OracleConfig.CHROMA_COLLECTION_NAME

        try:
            import chromadb  # type: ignore[import]
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(self._persist_dir),
                settings=chromadb.Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                ),
            )
            self._collection = client.get_or_create_collection(
                name=self._collection_name,
                metadata={
                    "hnsw:space": "cosine",
                    "hnsw:construction_ef": 200,
                    "hnsw:M": 16,
                },
            )
        except Exception as exc:
            logger.warning(
                "[OracleSemanticIndex] ChromaDB unavailable: %s; semantic search disabled", exc
            )
            return

        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import]

            class _STEmbedder:
                def __init__(self, model_name: str) -> None:
                    self._model = SentenceTransformer(model_name)

                async def embed(self, text: str) -> Any:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    return await loop.run_in_executor(
                        None, lambda: self._model.encode(text, normalize_embeddings=True)
                    )

                async def embed_batch(self, texts: List[str]) -> List[Any]:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    results = await loop.run_in_executor(
                        None,
                        lambda: self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False),
                    )
                    return list(results)

            self._embedder = _STEmbedder(OracleConfig.SEMANTIC_EMBED_MODEL)
            self._available = True
            logger.info(
                "[OracleSemanticIndex] Ready — collection '%s' at %s",
                self._collection_name, self._persist_dir,
            )
        except Exception as exc:
            logger.warning(
                "[OracleSemanticIndex] sentence-transformers unavailable: %s; semantic search disabled", exc
            )

    def is_ready(self) -> bool:
        """Return True if ChromaDB and embedder are both available."""
        return self._available

    def _build_embed_text(self, node: "NodeData") -> Optional[str]:
        """Build the text to embed for a node. Returns None if nothing to embed."""
        parts = [node.node_id.name]
        if node.signature:
            parts.append(node.signature)
        if node.docstring:
            parts.append(node.docstring)
        if len(parts) == 1:
            # Only the name — not worth embedding
            return None
        return " ".join(parts)[: self._MAX_EMBED_CHARS]

    async def embed_nodes(self, nodes: List["NodeData"]) -> None:
        """Embed a batch of nodes into ChromaDB.

        Silently skips nodes with no embeddable content.
        Silently returns if not available.
        Never raises.
        """
        if not self._available or self._collection is None or self._embedder is None:
            return

        try:
            embeddable = [
                n for n in nodes
                if n.node_id.node_type in self._EMBEDDABLE_TYPES
                and self._build_embed_text(n) is not None
            ]
            if not embeddable:
                return

            batch_size = OracleConfig.SEMANTIC_EMBED_BATCH_SIZE
            for i in range(0, len(embeddable), batch_size):
                batch = embeddable[i : i + batch_size]
                texts = [self._build_embed_text(n) for n in batch]  # type: ignore[misc]
                embeddings = await self._embedder.embed_batch(texts)

                ids = [str(n.node_id) for n in batch]
                metadatas = [
                    {
                        "repo": n.node_id.repo,
                        "file_path": n.node_id.file_path,
                        "name": n.node_id.name,
                        "node_type": n.node_id.node_type.value,
                    }
                    for n in batch
                ]

                self._collection.upsert(
                    ids=ids,
                    embeddings=[e.tolist() for e in embeddings],
                    metadatas=metadatas,
                )

            logger.debug(
                "[OracleSemanticIndex] Embedded %d nodes", len(embeddable)
            )
        except Exception as exc:
            logger.warning("[OracleSemanticIndex] embed_nodes failed: %s", exc)

    async def semantic_search(
        self, query: str, k: int = 5
    ) -> List[Tuple[str, float]]:
        """Search for semantically similar code symbols.

        Returns a list of ``("repo:file_path", similarity_score)`` tuples,
        sorted by similarity descending.  Deduplicates to unique file paths.
        Returns empty list if not available or on any error.
        """
        if not self._available or self._collection is None or self._embedder is None:
            return []

        try:
            import numpy as np
            query_embedding = await self._embedder.embed(query)

            results = self._collection.query(
                query_embeddings=[query_embedding.tolist()],
                n_results=min(k * 4, 100),  # over-fetch to allow file-level dedup
                include=["metadatas", "distances"],
            )

            ids_list = results.get("ids", [[]])[0]
            distances = results.get("distances", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]

            # Deduplicate to file level, keep best score per file
            best: Dict[str, float] = {}
            for dist, meta in zip(distances, metadatas):
                file_key = f"{meta['repo']}:{meta['file_path']}"
                # ChromaDB cosine distance in [0, 2]; similarity = 1 - distance (clamped)
                similarity = max(0.0, min(1.0, 1.0 - dist))
                if file_key not in best or similarity > best[file_key]:
                    best[file_key] = similarity

            # Sort by similarity desc, return top-k unique files
            return sorted(best.items(), key=lambda x: x[1], reverse=True)[:k]

        except Exception as exc:
            logger.warning("[OracleSemanticIndex] semantic_search failed: %s", exc)
            return []

```

**Step 5: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_oracle_semantic_fusion.py::TestOracleSemanticIndex -v
```

Expected: 6 passed.

**Step 6: Run full governance suite**

```bash
python3 -m pytest tests/test_ouroboros_governance/ --tb=short 2>&1 | tail -5
```

Expected: Same 681 passed, 27 pre-existing failures.

**Step 7: Self-review**

- [ ] `__init__` never raises — both ChromaDB and SentenceTransformer failures set `_available = False`
- [ ] `is_ready()` returns bool
- [ ] `embed_nodes` skips non-embeddable types and nodes with no content
- [ ] `semantic_search` converts distance to similarity correctly (`1.0 - dist`)
- [ ] `semantic_search` deduplicates to file level
- [ ] No new imports added at module top (all inside `__init__` / methods)

**Step 8: Commit**

```bash
git add backend/core/ouroboros/oracle.py tests/test_ouroboros_governance/test_oracle_semantic_fusion.py
git commit -m "feat(oracle): add OracleSemanticIndex with ChromaDB + SentenceTransformer embedding"
```

---

### Task 2: Wire Embedding into Oracle Indexing Lifecycle

**Files:**
- Modify: `backend/core/ouroboros/oracle.py` — `__init__`, `full_index()`, `incremental_update()`
- Modify: `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py` — append new test class

---

**Step 1: Write failing tests**

Append to `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py`:

```python
class TestOracleEmbeddingLifecycle:
    """Tests that embedding is called during oracle indexing."""

    def _make_oracle(self):
        from backend.core.ouroboros.oracle import TheOracle
        oracle = object.__new__(TheOracle)
        oracle._running = False
        oracle._lock = __import__("asyncio").Lock()
        oracle._file_hashes = {}
        oracle._graph = MagicMock()
        oracle._graph._metrics = {
            "total_nodes": 0, "total_edges": 0,
            "files_indexed": 0, "last_full_index": 0.0,
            "last_incremental_update": 0.0,
        }
        oracle._repos = {}
        oracle._semantic_index = MagicMock()
        oracle._semantic_index.embed_nodes = AsyncMock(return_value=None)
        oracle._semantic_index.is_ready = MagicMock(return_value=True)
        return oracle

    async def test_full_index_calls_embed_nodes(self):
        """full_index() must call _semantic_index.embed_nodes after indexing."""
        from backend.core.ouroboros.oracle import TheOracle
        oracle = self._make_oracle()

        # Patch _index_repository and _save_cache to no-ops
        oracle._index_repository = AsyncMock(return_value=None)
        oracle._save_cache = AsyncMock(return_value=None)
        # _graph.get_all_nodes() returns a small list
        oracle._graph.get_all_nodes = MagicMock(return_value=[MagicMock(), MagicMock()])

        await oracle.full_index()

        oracle._semantic_index.embed_nodes.assert_called_once()
        nodes_arg = oracle._semantic_index.embed_nodes.call_args[0][0]
        assert len(nodes_arg) == 2

    async def test_full_index_embed_failure_does_not_raise(self):
        """embed_nodes failure during full_index must not propagate."""
        from backend.core.ouroboros.oracle import TheOracle
        oracle = self._make_oracle()
        oracle._index_repository = AsyncMock(return_value=None)
        oracle._save_cache = AsyncMock(return_value=None)
        oracle._graph.get_all_nodes = MagicMock(return_value=[MagicMock()])
        oracle._semantic_index.embed_nodes = AsyncMock(side_effect=RuntimeError("chroma down"))

        # Must not raise
        await oracle.full_index()

    async def test_semantic_index_initialized_in_constructor(self):
        """TheOracle.__init__ must create _semantic_index attribute."""
        from backend.core.ouroboros.oracle import TheOracle, OracleSemanticIndex
        oracle = object.__new__(TheOracle)
        # Call __init__ manually
        TheOracle.__init__(oracle)
        assert hasattr(oracle, "_semantic_index")
        assert isinstance(oracle._semantic_index, OracleSemanticIndex)
```

**Note:** `CodebaseKnowledgeGraph` does not yet have `get_all_nodes()`. You will add it in the next step. These tests mock the graph, so they will test the oracle's calling convention before the graph method exists.

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_oracle_semantic_fusion.py::TestOracleEmbeddingLifecycle -v
```

Expected: `AttributeError: 'TheOracle' object has no attribute '_semantic_index'`

**Step 3: Add `_semantic_index` to `TheOracle.__init__`**

In `oracle.py`, find `TheOracle.__init__` at line 1110. After `OracleConfig.ORACLE_CACHE_DIR.mkdir(...)` (line 1124), add:

```python
        # Semantic index — fault isolated, never raises in __init__
        self._semantic_index: OracleSemanticIndex = OracleSemanticIndex()
```

**Step 4: Add `get_all_nodes()` to `CodebaseKnowledgeGraph`**

Find `CodebaseKnowledgeGraph` (line 664). Add this method after the existing `find_nodes_in_file` method (which is around line 729 in original, now shifted). The method returns all `NodeData`-like objects from the graph:

```python
    def get_all_nodes(self) -> List[NodeData]:
        """Return all NodeData objects stored in the graph.

        Used by the semantic index to embed all nodes after a full index.
        """
        result: List[NodeData] = []
        for node_key in self._node_index:
            attrs = self._graph.nodes.get(node_key, {})
            node_id = self._node_index[node_key]
            result.append(NodeData(
                node_id=node_id,
                docstring=attrs.get("docstring"),
                signature=attrs.get("signature"),
                decorators=attrs.get("decorators", []),
                base_classes=attrs.get("base_classes", []),
                complexity=attrs.get("complexity", 0),
                line_count=attrs.get("line_count", 0),
                last_modified=attrs.get("last_modified", 0.0),
                source_hash=attrs.get("source_hash", ""),
            ))
        return result
```

**Step 5: Wire embedding into `full_index()`**

In `oracle.py`, find `full_index()` at line ~1153. After `await self._save_cache()` (the last line of the method), add:

```python
        # Embed all nodes into semantic index (fault-isolated)
        try:
            all_nodes = self._graph.get_all_nodes()
            await self._semantic_index.embed_nodes(all_nodes)
        except Exception as exc:
            logger.warning("[Oracle] Semantic embedding after full_index failed: %s", exc)
```

**Step 6: Wire embedding into `incremental_update()`**

In `oracle.py`, find `incremental_update()` at line ~1178. After the lock block closes (after `self._graph._metrics["last_incremental_update"] = time.time()`), add before the `elapsed` log line:

```python
        # Embed changed nodes into semantic index (fault-isolated)
        try:
            if changed_files:
                # Re-embed nodes from changed files only
                changed_rel_paths = set()
                for fp in changed_files:
                    for repo_name, repo_root in self._repos.items():
                        try:
                            changed_rel_paths.add(str(Path(fp).relative_to(repo_root)))
                        except ValueError:
                            pass
                changed_nodes = [
                    n for n in self._graph.get_all_nodes()
                    if n.node_id.file_path in changed_rel_paths
                ]
            else:
                changed_nodes = self._graph.get_all_nodes()
            await self._semantic_index.embed_nodes(changed_nodes)
        except Exception as exc:
            logger.warning("[Oracle] Semantic embedding after incremental_update failed: %s", exc)
```

**Step 7: Run tests**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_oracle_semantic_fusion.py -v
```

Expected: All 9 tests pass (6 from Task 1 + 3 new).

**Step 8: Run full suite**

```bash
python3 -m pytest tests/test_ouroboros_governance/ --tb=short 2>&1 | tail -5
```

Expected: 681+ passed, 27 pre-existing failures.

**Step 9: Self-review**

- [ ] `_semantic_index` initialized in `__init__` as `OracleSemanticIndex()`
- [ ] `get_all_nodes()` returns `List[NodeData]` from graph attributes
- [ ] `full_index()` calls `embed_nodes(all_nodes)` AFTER `_save_cache()`
- [ ] `incremental_update()` calls `embed_nodes(changed_nodes)` with file-filtered nodes
- [ ] Both embedding calls are wrapped in `try/except Exception` — never propagate

**Step 10: Commit**

```bash
git add backend/core/ouroboros/oracle.py tests/test_ouroboros_governance/test_oracle_semantic_fusion.py
git commit -m "feat(oracle): wire OracleSemanticIndex embedding into full_index and incremental_update"
```

---

### Task 3: Add FileNeighborhood.semantic_support and get_fused_neighborhood()

**Files:**
- Modify: `backend/core/ouroboros/oracle.py` — `FileNeighborhood` dataclass + `TheOracle.get_fused_neighborhood()`
- Modify: `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py` — append new test classes

---

**Step 1: Write failing tests**

Append to `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py`:

```python
class TestFileNeighborhoodSemanticSupport:
    """Tests for FileNeighborhood.semantic_support field."""

    def test_semantic_support_included_in_to_dict(self):
        from backend.core.ouroboros.oracle import FileNeighborhood
        nh = FileNeighborhood(
            target_files=["jarvis:core/foo.py"],
            imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth.py"],
        )
        d = nh.to_dict()
        assert "semantic_support" in d
        assert d["semantic_support"] == ["prime:services/auth.py"]

    def test_semantic_support_omitted_when_empty(self):
        from backend.core.ouroboros.oracle import FileNeighborhood
        nh = FileNeighborhood(
            target_files=["jarvis:core/foo.py"],
            imports=["jarvis:core/bar.py"], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=[],
        )
        d = nh.to_dict()
        assert "semantic_support" not in d

    def test_all_unique_files_includes_semantic_support(self):
        from backend.core.ouroboros.oracle import FileNeighborhood
        nh = FileNeighborhood(
            target_files=["jarvis:core/foo.py"],
            imports=["jarvis:core/bar.py"], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth.py"],
        )
        unique = nh.all_unique_files()
        assert "prime:services/auth.py" in unique
        assert "jarvis:core/bar.py" in unique


class TestGetFusedNeighborhood:
    """Tests for TheOracle.get_fused_neighborhood()."""

    def _make_oracle_with_mocks(self):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood
        oracle = object.__new__(TheOracle)
        oracle._running = True
        oracle._repos = {"jarvis": Path("/repo/jarvis"), "prime": Path("/repo/prime")}
        oracle._graph = MagicMock()

        # Default: get_file_neighborhood returns an empty neighborhood
        oracle._semantic_index = MagicMock()
        oracle._semantic_index.is_ready = MagicMock(return_value=True)
        oracle._semantic_index.semantic_search = AsyncMock(return_value=[])
        return oracle

    async def test_returns_empty_when_not_running(self):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood
        oracle = object.__new__(TheOracle)
        oracle._running = False
        oracle._repos = {}
        oracle._graph = MagicMock()
        oracle._semantic_index = MagicMock()
        oracle._semantic_index.is_ready = MagicMock(return_value=False)

        result = await oracle.get_fused_neighborhood([], "some query")
        assert isinstance(result, FileNeighborhood)
        assert result.all_unique_files() == []

    async def test_structural_files_go_into_structural_categories(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle_with_mocks()
        oracle._repos = {"jarvis": tmp_path}

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        structural_nh = FileNeighborhood(
            target_files=["jarvis:core/service.py"],
            imports=["jarvis:core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )

        # Patch get_file_neighborhood to return our structural_nh
        oracle.get_file_neighborhood = MagicMock(return_value=structural_nh)
        oracle._semantic_index.semantic_search = AsyncMock(return_value=[])

        result = await oracle.get_fused_neighborhood([target], "fix the service")

        assert "jarvis:core/base.py" in result.imports
        assert result.semantic_support == []

    async def test_semantic_seeds_go_into_semantic_support(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle_with_mocks()
        oracle._repos = {"jarvis": tmp_path}

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        empty_nh = FileNeighborhood(
            target_files=[], imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )
        oracle.get_file_neighborhood = MagicMock(return_value=empty_nh)

        # Semantic search finds auth.py in prime
        oracle._semantic_index.semantic_search = AsyncMock(
            return_value=[("prime:services/auth.py", 0.91)]
        )

        result = await oracle.get_fused_neighborhood([target], "authentication logic")

        assert "prime:services/auth.py" in result.semantic_support

    async def test_semantic_failure_degrades_to_structural_only(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle_with_mocks()
        oracle._repos = {"jarvis": tmp_path}

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        structural_nh = FileNeighborhood(
            target_files=["jarvis:core/service.py"],
            imports=["jarvis:core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )
        oracle.get_file_neighborhood = MagicMock(return_value=structural_nh)
        oracle._semantic_index.semantic_search = AsyncMock(
            side_effect=RuntimeError("chroma connection failed")
        )

        result = await oracle.get_fused_neighborhood([target], "fix service")

        # Structural results preserved
        assert "jarvis:core/base.py" in result.imports
        # Semantic support empty (degraded)
        assert result.semantic_support == []

    async def test_fusion_score_ranks_structural_above_low_semantic(self, tmp_path):
        """Structural files (graph_proximity=1.0) must outscore low-similarity seeds."""
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle_with_mocks()
        oracle._repos = {"jarvis": tmp_path}

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        structural_nh = FileNeighborhood(
            target_files=["jarvis:core/service.py"],
            imports=["jarvis:core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )
        oracle.get_file_neighborhood = MagicMock(return_value=structural_nh)
        # Very low similarity seed
        oracle._semantic_index.semantic_search = AsyncMock(
            return_value=[("prime:utils/misc.py", 0.10)]
        )

        result = await oracle.get_fused_neighborhood([target], "fix service")

        # structural base.py score: 0.55*1.0 + 0.35*0.0 + 0.10*1.0 = 0.65
        # semantic misc.py score:   0.55*0.5 + 0.35*0.10 + 0.10*1.0 = 0.375
        # base.py must appear in structural (not displaced to semantic)
        assert "jarvis:core/base.py" in result.imports
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_oracle_semantic_fusion.py::TestFileNeighborhoodSemanticSupport tests/test_ouroboros_governance/test_oracle_semantic_fusion.py::TestGetFusedNeighborhood -v
```

Expected: `ImportError` or `AttributeError` — `semantic_support` and `get_fused_neighborhood` don't exist yet.

**Step 3: Add `semantic_support` to `FileNeighborhood`**

In `oracle.py`, find `FileNeighborhood` dataclass at line 258. After `test_counterparts: List[str]` and before `local_repo: str = "jarvis"`, add:

```python
    semantic_support: List[str] = field(default_factory=list)
    # Cross-repo files discovered via semantic similarity seeding (same repo:path format)
```

Update `to_dict()` to include `semantic_support` (it's already handled by the `if v` dict comprehension — just add `"semantic_support": self.semantic_support` to the inner dict):

The full inner dict should become:
```python
        return {
            k: v
            for k, v in {
                "imports": self.imports,
                "importers": self.importers,
                "callers": self.callers,
                "callees": self.callees,
                "inheritors": self.inheritors,
                "base_classes": self.base_classes,
                "test_counterparts": self.test_counterparts,
                "semantic_support": self.semantic_support,
            }.items()
            if v
        }
```

Update `all_unique_files()` to include `semantic_support` in the chain:
```python
        for path in (
            self.imports
            + self.importers
            + self.callers
            + self.callees
            + self.inheritors
            + self.base_classes
            + self.test_counterparts
            + self.semantic_support
        ):
```

**Step 4: Add `get_fused_neighborhood()` to `TheOracle`**

Insert the following method in `oracle.py` immediately before `get_relevant_files_for_query` (currently around line 1776). Add it as a new async method of `TheOracle`:

```python
    async def get_fused_neighborhood(
        self,
        file_paths: List[Path],
        query: str,
        k_semantic: int = 5,
    ) -> "FileNeighborhood":
        """Return a fused depth-1 structural + semantic neighborhood.

        **Algorithm (Engineering Mandate — Fuse Strategy):**

        1. Structural expansion: depth-1 graph neighborhood from ``file_paths``.
        2. Semantic seeds: top-K files from ChromaDB semantic search on ``query``.
        3. Seed expansion: depth-1 graph neighborhood from seed files.
        4. Scoring: ``final = 0.55 * graph_proximity + 0.35 * semantic_sim + 0.10 * recency``
           - Structural files: ``graph_proximity=1.0``, ``semantic_sim=0.0``
           - Seed/seed-derived files: ``graph_proximity=0.5``, ``semantic_sim`` from ChromaDB
        5. Partition: structural-origin → structural categories; seed-origin → ``semantic_support``.

        **Degradation:**
        - Semantic search fails → return structural neighborhood only.
        - Graph fails → return semantic seeds in ``semantic_support`` only.
        - Both fail → return empty ``FileNeighborhood``.

        This method is **async** due to the ChromaDB embedding search.
        ``get_file_neighborhood`` (sync) is unchanged.
        """
        empty = FileNeighborhood(
            target_files=[],
            imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=[],
        )

        if not getattr(self, "_running", False):
            return empty

        # ── Step 1: Structural expansion ──────────────────────────────────
        structural_nh: FileNeighborhood = empty
        structural_set: set = set()
        try:
            structural_nh = self.get_file_neighborhood(file_paths)
            structural_set = set(structural_nh.all_unique_files())
        except Exception as exc:
            logger.warning("[Oracle] Structural expansion failed: %s", exc)

        # ── Step 2: Semantic seeds ─────────────────────────────────────────
        raw_seeds: List[Tuple[str, float]] = []
        semantic_index = getattr(self, "_semantic_index", None)
        if semantic_index is not None and semantic_index.is_ready():
            try:
                raw_seeds = await semantic_index.semantic_search(query, k=k_semantic)
            except Exception as exc:
                logger.warning("[Oracle] Semantic search failed: %s; degrading to structural-only", exc)

        if not raw_seeds and not structural_set:
            return empty

        # Build seed_score lookup: file_key → semantic_similarity
        seed_scores: Dict[str, float] = {fk: sc for fk, sc in raw_seeds}

        # ── Step 3: Seed graph expansion ─────────────────────────────────
        seed_nh: FileNeighborhood = empty
        if raw_seeds:
            try:
                seed_abs: List[Path] = []
                for file_key, _ in raw_seeds:
                    repo, rel_path = file_key.split(":", 1)
                    repo_root = self._repos.get(repo)
                    if repo_root:
                        seed_abs.append(repo_root / rel_path)
                if seed_abs:
                    seed_nh = self.get_file_neighborhood(seed_abs)
            except Exception as exc:
                logger.warning("[Oracle] Seed graph expansion failed: %s", exc)

        # ── Step 4: Score all candidates ─────────────────────────────────
        def _score(file_key: str, is_structural: bool) -> float:
            graph_prox = 1.0 if is_structural else 0.5
            semantic_sim = seed_scores.get(file_key, 0.0)
            recency = 1.0  # not yet tracked
            return 0.55 * graph_prox + 0.35 * semantic_sim + 0.10 * recency

        # ── Step 5: Partition ─────────────────────────────────────────────
        target_key_set = set(structural_nh.target_files)

        # Semantic-support candidates: seed files + seed-derived files NOT in structural
        semantic_candidates: List[str] = []
        for fk in seed_nh.all_unique_files():
            if fk not in structural_set and fk not in target_key_set:
                semantic_candidates.append(fk)
        for fk, _ in raw_seeds:
            if fk not in structural_set and fk not in target_key_set and fk not in semantic_candidates:
                semantic_candidates.append(fk)

        semantic_candidates_scored = sorted(
            semantic_candidates,
            key=lambda fk: _score(fk, is_structural=False),
            reverse=True,
        )

        # Rebuild structural categories (same as structural_nh, unchanged)
        # Token Guardrail is enforced at rendering — categories already bounded by
        # MAX_FILES_PER_CATEGORY in _render_neighborhood_section.
        return FileNeighborhood(
            target_files=structural_nh.target_files,
            imports=structural_nh.imports,
            importers=structural_nh.importers,
            callers=structural_nh.callers,
            callees=structural_nh.callees,
            inheritors=structural_nh.inheritors,
            base_classes=structural_nh.base_classes,
            test_counterparts=structural_nh.test_counterparts,
            semantic_support=semantic_candidates_scored,
            local_repo=structural_nh.local_repo,
        )
```

**Step 5: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_oracle_semantic_fusion.py -v
```

Expected: All 17 tests pass (9 prior + 3 semantic_support + 5 fused_neighborhood).

**Step 6: Run full governance suite**

```bash
python3 -m pytest tests/test_ouroboros_governance/ --tb=short 2>&1 | tail -5
```

Expected: 681+ passed, 27 pre-existing failures. The 3 existing `TestFileNeighborhoodDataclass` tests must still pass (backward-compatible `semantic_support` default is `[]`).

**Step 7: Self-review**

- [ ] `semantic_support` field added with `field(default_factory=list)` — backward compatible default
- [ ] `to_dict()` updated to include `semantic_support` in the comprehension
- [ ] `all_unique_files()` includes `semantic_support` in the chain
- [ ] `get_fused_neighborhood` is `async def`
- [ ] Degradation: semantic failure → returns `structural_nh` with empty `semantic_support`
- [ ] Degradation: both fail → returns `empty`
- [ ] Scoring formula: `0.55 * graph_proximity + 0.35 * semantic_sim + 0.10 * recency`
- [ ] Structural categories NOT altered — they come directly from `structural_nh`
- [ ] Target files excluded from `semantic_support` via `target_key_set`

**Step 8: Commit**

```bash
git add backend/core/ouroboros/oracle.py tests/test_ouroboros_governance/test_oracle_semantic_fusion.py
git commit -m "feat(oracle): add FileNeighborhood.semantic_support and get_fused_neighborhood() fusion"
```

---

### Task 4: Update ContextExpander to Use get_fused_neighborhood

**Files:**
- Modify: `backend/core/ouroboros/governance/context_expander.py`
- Modify: `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py` — append new test class

---

**Step 1: Write failing tests**

Append to `tests/test_ouroboros_governance/test_oracle_semantic_fusion.py`:

```python
class TestContextExpanderFusedRendering:
    """Tests for dual-section rendering of fused neighborhood."""

    def _make_ctx(self, description="fix the auth service", target_files=("backend/core/service.py",)):
        from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
        return OperationContext.create(
            op_id="fusion-test",
            description=description,
            target_files=tuple(target_files),
        ).advance(OperationPhase.ROUTE).advance(OperationPhase.CONTEXT_EXPANSION)

    async def test_two_sections_rendered_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        fused_nh = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=["jarvis:backend/core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth_service.py"],
        )

        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}
        oracle.get_fused_neighborhood = AsyncMock(return_value=fused_nh)

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()

        # Trigger full expand() to exercise the async oracle call
        generator = MagicMock()
        generator.plan = AsyncMock(return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "none"}')
        expander._generator = generator

        from datetime import datetime, timedelta
        deadline = datetime.now() + timedelta(seconds=30)
        await expander.expand(ctx, deadline)

        # Verify get_fused_neighborhood was called (not get_file_neighborhood)
        oracle.get_fused_neighborhood.assert_called_once()
        oracle.get_file_neighborhood.assert_not_called()

    async def test_structural_section_labeled_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        fused_nh = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=["jarvis:backend/core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth_service.py"],
        )
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}
        oracle.get_fused_neighborhood = AsyncMock(return_value=fused_nh)

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], neighborhood=fused_nh)

        assert "Structural" in prompt or "structural" in prompt
        assert "jarvis:backend/core/base.py" in prompt

    async def test_semantic_support_section_labeled_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        fused_nh = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=["prime:services/auth_service.py", "reactor:handlers/base.py"],
        )
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], neighborhood=fused_nh)

        assert "Semantic" in prompt or "semantic" in prompt
        assert "prime:services/auth_service.py" in prompt
        assert "reactor:handlers/base.py" in prompt

    async def test_semantic_support_truncated_at_10(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        # 15 semantic support files
        semantic = [f"prime:services/svc_{i}.py" for i in range(15)]
        fused_nh = FileNeighborhood(
            target_files=[], imports=[], importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
            semantic_support=semantic,
        )
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], neighborhood=fused_nh)

        shown = sum(1 for line in prompt.splitlines() if "svc_" in line and line.strip().startswith("- "))
        assert shown == 10
        assert "and 5 more" in prompt

    async def test_degrades_gracefully_when_fused_fails(self, tmp_path):
        """If get_fused_neighborhood raises, falls back to get_file_neighborhood."""
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        structural_nh = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=["jarvis:backend/core/base.py"],
            importers=[], callers=[], callees=[],
            inheritors=[], base_classes=[], test_counterparts=[],
        )

        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}
        oracle.get_fused_neighborhood = AsyncMock(side_effect=RuntimeError("chroma down"))
        oracle.get_file_neighborhood = MagicMock(return_value=structural_nh)

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()

        from datetime import datetime, timedelta
        deadline = datetime.now() + timedelta(seconds=30)
        generator = MagicMock()
        generator.plan = AsyncMock(return_value='{"schema_version": "expansion.1", "additional_files_needed": [], "reasoning": "none"}')
        expander._generator = generator

        # Must not raise
        result_ctx = await expander.expand(ctx, deadline)
        # Fallback structural result used
        oracle.get_file_neighborhood.assert_called_once()
```

**Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_oracle_semantic_fusion.py::TestContextExpanderFusedRendering -v
```

Expected: Failures — `get_fused_neighborhood` not called, `neighborhood` param unknown.

**Step 3: Read context_expander.py in full before editing**

```bash
# In your shell:
# Read the file to understand current structure before making changes
```

Use the Read tool on `backend/core/ouroboros/governance/context_expander.py`.

**Step 4: Update `_render_neighborhood_section` to render two distinct sections**

Replace the current `_render_neighborhood_section` body. The method must now render:
1. **Structural section** — edge-typed categories (imports, importers, callers, etc.)
2. **Semantic support section** — flat list of `semantic_support` files

New implementation:

```python
    def _render_neighborhood_section(self, neighborhood: Any) -> str:
        """Render a FileNeighborhood as two labeled sections.

        Section 1 — Structural file neighborhood: edge-typed categories
          (imports, importers, callers, callees, inheritors, base_classes,
          test_counterparts). Each category capped at MAX_FILES_PER_CATEGORY.

        Section 2 — Semantic support: flat list of cross-repo similar code,
          also capped at MAX_FILES_PER_CATEGORY.

        Truncated categories append ``"  ... (and N more)"``.
        Returns empty string if neighborhood has no neighbors at all.
        """
        try:
            categories = neighborhood.to_dict()
        except Exception:
            return ""
        if not categories:
            return ""

        # Split into structural and semantic sections
        semantic_support = categories.pop("semantic_support", [])
        structural_categories = categories  # remaining keys are structural

        lines: List[str] = []

        # ── Structural section ────────────────────────────────────────────
        if structural_categories:
            lines.append("\nStructural file neighborhood (real codebase graph edges):")
            for category, paths in structural_categories.items():
                label = category.replace("_", " ").title()
                shown = paths[:MAX_FILES_PER_CATEGORY]
                hidden = len(paths) - len(shown)
                lines.append(f"  {label}:")
                for p in shown:
                    lines.append(f"    - {p}")
                if hidden > 0:
                    lines.append(f"    ... (and {hidden} more)")

        # ── Semantic support section ──────────────────────────────────────
        if semantic_support:
            lines.append("\nSemantic support (cross-repo similar code):")
            shown = semantic_support[:MAX_FILES_PER_CATEGORY]
            hidden = len(semantic_support) - len(shown)
            for p in shown:
                lines.append(f"    - {p}")
            if hidden > 0:
                lines.append(f"    ... (and {hidden} more)")

        if not lines:
            return ""

        lines.append(
            "\nWhich of these (if any) would help you understand the context?\n"
        )
        return "\n".join(lines)
```

**Step 5: Update `_build_expansion_prompt` signature**

Add `neighborhood: Optional[Any] = None` parameter (replaces the `oracle` parameter for this specific purpose):

```python
    def _build_expansion_prompt(
        self,
        ctx: OperationContext,
        already_fetched: List[str],
        oracle: Optional[Any] = None,
        neighborhood: Optional[Any] = None,
    ) -> str:
```

Update the `available_section` block to use `neighborhood` if provided, falling back to oracle query:

```python
        available_section = ""
        if neighborhood is not None:
            # Pre-computed fused neighborhood passed in directly
            available_section = self._render_neighborhood_section(neighborhood)
        elif oracle is not None:
            try:
                status = oracle.get_status()
                if status.get("running", False):
                    target_abs = [self._repo_root / f for f in ctx.target_files]
                    sync_nh = oracle.get_file_neighborhood(target_abs)
                    available_section = self._render_neighborhood_section(sync_nh)
            except Exception:
                available_section = ""
```

**Step 6: Update `expand()` to await `get_fused_neighborhood`**

In `expand()`, find the line that calls `_build_expansion_prompt`. Before the loop, add an async oracle call that fetches the fused neighborhood once and passes it into each prompt:

Change the current call at the top of `expand()` (in the oracle block before the loop) to:

```python
        # Pre-fetch fused neighborhood once before rounds (async, fault-isolated)
        fused_neighborhood: Optional[Any] = None
        if self._oracle is not None:
            try:
                status = self._oracle.get_status()
                if status.get("running", False):
                    target_abs = [self._repo_root / f for f in ctx.target_files]
                    if hasattr(self._oracle, "get_fused_neighborhood"):
                        fused_neighborhood = await self._oracle.get_fused_neighborhood(
                            target_abs, ctx.description
                        )
                    else:
                        fused_neighborhood = self._oracle.get_file_neighborhood(target_abs)
            except Exception as exc:
                logger.warning(
                    "[ContextExpander] op=%s oracle neighborhood failed: %s; continuing without",
                    ctx.op_id, exc,
                )
                fused_neighborhood = None
```

Then update the call inside the `for round_num` loop from:
```python
            prompt = self._build_expansion_prompt(ctx, accumulated, oracle=self._oracle)
```
to:
```python
            prompt = self._build_expansion_prompt(ctx, accumulated, neighborhood=fused_neighborhood)
```

**Step 7: Run tests**

```bash
python3 -m pytest tests/test_ouroboros_governance/test_oracle_semantic_fusion.py -v
```

Expected: All 22 tests pass (17 prior + 5 new).

**Step 8: Run full governance suite**

```bash
python3 -m pytest tests/test_ouroboros_governance/ --tb=short 2>&1 | tail -5
```

Expected: All context_expander tests still pass, 681+ passed.

**Step 9: Self-review**

- [ ] `_render_neighborhood_section` renders **two labeled sections** (Structural / Semantic support)
- [ ] `semantic_support` is `pop`-ed from `categories` dict before structural rendering
- [ ] Token Guardrail: both structural categories AND `semantic_support` capped at `MAX_FILES_PER_CATEGORY=10`
- [ ] `"... (and N more)"` appended when truncated
- [ ] `expand()` awaits `get_fused_neighborhood` ONCE before the round loop
- [ ] Fallback to `get_file_neighborhood` (sync) if `get_fused_neighborhood` not available
- [ ] `_build_expansion_prompt` accepts both `neighborhood=` and legacy `oracle=` paths
- [ ] `_build_expansion_prompt` call site inside round loop passes `neighborhood=fused_neighborhood`

**Step 10: Commit**

```bash
git add backend/core/ouroboros/governance/context_expander.py tests/test_ouroboros_governance/test_oracle_semantic_fusion.py
git commit -m "feat(context-expander): render dual-section fused neighborhood (structural + semantic support)"
```

---

## Verification Checklist

Before finishing this branch:

- [ ] `OracleSemanticIndex.__init__` never raises — ChromaDB/ST failures set `_available=False`
- [ ] `is_ready()` guards all public methods
- [ ] `embed_nodes` skips CLASS/FUNCTION/METHOD only; skips nodes with no docstring+signature
- [ ] `semantic_search` converts cosine distance to similarity (`1.0 - dist`), deduplicates to file level
- [ ] `get_all_nodes()` returns all `NodeData` from graph
- [ ] `full_index()` calls `embed_nodes(all_nodes)` after `_save_cache()`
- [ ] `incremental_update()` calls `embed_nodes(changed_nodes)` with file-filtered nodes
- [ ] `FileNeighborhood.semantic_support` defaults to `[]`, backward compatible
- [ ] `to_dict()` includes `semantic_support` only when non-empty
- [ ] `all_unique_files()` includes `semantic_support`
- [ ] `get_fused_neighborhood` scoring: `0.55 * graph_proximity + 0.35 * semantic_sim + 0.10 * recency`
- [ ] Structural-origin files → structural categories; seed-origin → `semantic_support`
- [ ] Token Guardrail: 10 paths max per category, `"... (and N more)"`
- [ ] Two labeled sections in rendered prompt
- [ ] `expand()` awaits `get_fused_neighborhood` once before round loop
- [ ] All 681 existing tests still pass
