# ContextExpander File Manifest Enhancement Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the keyword-based oracle query in ContextExpander with a structural graph-topology neighborhood lookup so J-Prime receives a categorized, labeled manifest of real related files instead of keyword-matched guesses.

**Architecture:** Add a `FileNeighborhood` dataclass to `oracle.py` and a synchronous `get_file_neighborhood(file_paths)` method to `TheOracle` that traverses the NetworkX DiGraph at depth=1, classifying edges into semantic categories (imports, importers, callers, callees, inheritors, base_classes, test_counterparts). Replace the single `get_relevant_files_for_query` call in `ContextExpander` with a call to this new method, rendering the structured neighborhood as a labeled multi-section prompt section with hard truncation at 10 paths per category.

**Tech Stack:** Python 3.11, NetworkX DiGraph (already in oracle.py), dataclasses, pytest with asyncio_mode=auto

---

## Context for Implementer

This project is the JARVIS AI Agent. The governance subsystem lives in `backend/core/ouroboros/governance/`. The oracle lives in `backend/core/ouroboros/oracle.py` (1877 lines). Tests live in `tests/test_ouroboros_governance/`.

**NEVER use `@pytest.mark.asyncio`** — the project uses `asyncio_mode = auto` in `pytest.ini`. Async test functions are discovered automatically.

**Engineering Mandate constraints (non-negotiable):**
1. **Edge type specificity** — categorize edges into `imports`, `importers`, `callers`, `callees`, `inheritors`, `base_classes`, `test_counterparts`
2. **Cross-repo boundary traversal** — format paths as `"{repo}:{relative_path}"` (e.g., `"jarvis:backend/core/foo.py"`)
3. **Depth=1 hard limit** — immediate neighbors only; never recurse
4. **Deterministic structured payload** — `FileNeighborhood` dataclass, not a flat list
5. **Token Explosion Trap mitigation** — hard truncation at 10 paths per category with `"... (and N more)"` indicator in rendered output

**Key oracle internals:**
- `TheOracle._graph` is a `CodebaseKnowledgeGraph` instance (NOT `_knowledge_graph`)
- `TheOracle._repos: Dict[str, Path]` maps `"jarvis"` / `"prime"` / `"reactor"` → absolute `Path`
- `CodebaseKnowledgeGraph.find_nodes_in_file(file_path: str) -> List[NodeID]` — takes relative path string, uses `_file_index`
- `CodebaseKnowledgeGraph.get_edges_from(node_id) -> List[Tuple[str, Dict[str, Any]]]` — returns `(target_key, edge_data_dict)` where `edge_data["edge_type"]` is a string value of `EdgeType` enum (e.g., `"imports"`, `"calls"`, `"inherits"`)
- `CodebaseKnowledgeGraph.get_edges_to(node_id) -> List[Tuple[str, Dict[str, Any]]]` — returns `(source_key, edge_data_dict)`
- `CodebaseKnowledgeGraph._node_index: Dict[str, NodeID]` — key is `"{repo}:{file_path}:{name}"`
- `CodebaseKnowledgeGraph._file_index: Dict[str, Set[str]]` — key is relative path string (no repo prefix)
- `NodeID` fields: `repo: str`, `file_path: str` (relative), `name: str`, `node_type: NodeType`, `line_number: int`

**EdgeType string values relevant to us:**
- `"imports"` and `"imports_from"` → outgoing = `imports`, incoming = `importers`
- `"calls"` → outgoing = `callees`, incoming = `callers`
- `"inherits"` → outgoing = `base_classes`, incoming = `inheritors`

---

### Task 1: Add FileNeighborhood Dataclass to oracle.py

**Files:**
- Modify: `backend/core/ouroboros/oracle.py` (insert after line 254)
- Test: `tests/test_ouroboros_governance/test_oracle_neighborhood.py`

---

**Step 1: Write the failing test**

Create `tests/test_ouroboros_governance/test_oracle_neighborhood.py`:

```python
"""Tests for FileNeighborhood dataclass."""
from __future__ import annotations

import pytest
from backend.core.ouroboros.oracle import FileNeighborhood


class TestFileNeighborhoodDataclass:
    def test_to_dict_omits_empty_categories(self):
        nh = FileNeighborhood(
            target_files=["jarvis:backend/core/foo.py"],
            imports=["jarvis:backend/core/bar.py"],
            importers=[],
            callers=[],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=[],
        )
        result = nh.to_dict()
        assert "imports" in result
        assert "importers" not in result
        assert "callers" not in result
        assert result["imports"] == ["jarvis:backend/core/bar.py"]

    def test_all_unique_files_deduplicates_and_excludes_targets(self):
        nh = FileNeighborhood(
            target_files=["jarvis:backend/core/foo.py"],
            imports=["jarvis:backend/core/bar.py", "jarvis:backend/core/baz.py"],
            importers=["jarvis:backend/core/bar.py"],  # duplicate
            callers=[],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=["jarvis:tests/test_foo.py"],
        )
        unique = nh.all_unique_files()
        assert "jarvis:backend/core/bar.py" in unique
        assert "jarvis:backend/core/baz.py" in unique
        assert "jarvis:tests/test_foo.py" in unique
        # targets excluded
        assert "jarvis:backend/core/foo.py" not in unique
        # deduped (bar appears twice but once in result)
        assert unique.count("jarvis:backend/core/bar.py") == 1

    def test_empty_neighborhood_produces_empty_structures(self):
        nh = FileNeighborhood(
            target_files=[],
            imports=[],
            importers=[],
            callers=[],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=[],
        )
        assert nh.to_dict() == {}
        assert nh.all_unique_files() == []
```

**Step 2: Run test to verify it fails**

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
python -m pytest tests/test_ouroboros_governance/test_oracle_neighborhood.py::TestFileNeighborhoodDataclass -v
```

Expected: `ImportError: cannot import name 'FileNeighborhood' from 'backend.core.ouroboros.oracle'`

**Step 3: Add FileNeighborhood dataclass**

Open `backend/core/ouroboros/oracle.py`. The `BlastRadius` dataclass ends at line 254. Insert the following at line **256** (after one blank line following BlastRadius):

```python
@dataclass
class FileNeighborhood:
    """Structural neighborhood of a set of files in the codebase graph.

    All paths are formatted as ``"{repo}:{relative_path}"``, e.g.
    ``"jarvis:backend/core/foo.py"`` or ``"reactor:interfaces/base.py"``.
    """

    target_files: List[str]           # normalized "repo:path" of input files
    imports: List[str]                # outgoing IMPORTS / IMPORTS_FROM edges
    importers: List[str]              # incoming IMPORTS / IMPORTS_FROM edges
    callers: List[str]                # incoming CALLS edges (who calls us)
    callees: List[str]                # outgoing CALLS edges (who we call)
    inheritors: List[str]             # incoming INHERITS edges
    base_classes: List[str]           # outgoing INHERITS edges
    test_counterparts: List[str]      # heuristic: test_{basename}.py match
    local_repo: str = "jarvis"        # repo of input files (for rendering)

    def to_dict(self) -> Dict[str, List[str]]:
        """Return non-empty categories only."""
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
            }.items()
            if v
        }

    def all_unique_files(self) -> List[str]:
        """Flat deduplicated list of all neighbor files, excluding targets."""
        target_set = set(self.target_files)
        seen: set = set()
        result: List[str] = []
        for path in (
            self.imports
            + self.importers
            + self.callers
            + self.callees
            + self.inheritors
            + self.base_classes
            + self.test_counterparts
        ):
            if path not in target_set and path not in seen:
                seen.add(path)
                result.append(path)
        return result
```

You need `Dict` and `List` in scope. Check the existing imports at the top of `oracle.py`; `List` and `Dict` are already imported from `typing` (lines ~47-50). No new imports needed.

**Step 4: Run test to verify it passes**

```bash
python -m pytest tests/test_ouroboros_governance/test_oracle_neighborhood.py::TestFileNeighborhoodDataclass -v
```

Expected: 3 passed.

**Step 5: Commit**

```bash
git add backend/core/ouroboros/oracle.py tests/test_ouroboros_governance/test_oracle_neighborhood.py
git commit -m "feat(oracle): add FileNeighborhood dataclass for structural file manifest"
```

---

### Task 2: Add TheOracle.get_file_neighborhood() Method

**Files:**
- Modify: `backend/core/ouroboros/oracle.py` (insert before `get_relevant_files_for_query` — currently at ~line 1583, shifts by ~35 lines after Task 1 insert)
- Test: `tests/test_ouroboros_governance/test_oracle_neighborhood.py`

---

**Step 1: Write the failing tests**

Append to `tests/test_ouroboros_governance/test_oracle_neighborhood.py`:

```python
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestGetFileNeighborhood:
    """Unit tests for TheOracle.get_file_neighborhood."""

    def _make_oracle(self, repos=None):
        """Build a minimal TheOracle-like object with a mock graph."""
        from backend.core.ouroboros.oracle import TheOracle
        oracle = object.__new__(TheOracle)
        oracle._running = True
        oracle._repos = repos or {
            "jarvis": Path("/repo/jarvis"),
            "prime": Path("/repo/prime"),
        }
        oracle._graph = MagicMock()
        return oracle

    def test_returns_empty_neighborhood_when_not_running(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood
        oracle = object.__new__(TheOracle)
        oracle._running = False
        oracle._repos = {"jarvis": tmp_path}
        oracle._graph = MagicMock()

        result = oracle.get_file_neighborhood([tmp_path / "foo.py"])
        assert isinstance(result, FileNeighborhood)
        assert result.target_files == []
        assert result.all_unique_files() == []

    def test_classifies_outgoing_imports_edge(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood
        from unittest.mock import MagicMock

        oracle = self._make_oracle({"jarvis": tmp_path})

        # Create a fake file
        target = tmp_path / "core" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# foo")

        # Mock: find_nodes_in_file returns one node
        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/foo.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]

        # Mock: outgoing edge is an "imports" edge to bar.py
        bar_node_key = "jarvis:core/bar.py:bar_module"
        bar_node = MagicMock()
        bar_node.repo = "jarvis"
        bar_node.file_path = "core/bar.py"
        oracle._graph._node_index = {bar_node_key: bar_node}
        oracle._graph.get_edges_from.return_value = [
            (bar_node_key, {"edge_type": "imports"})
        ]
        oracle._graph.get_edges_to.return_value = []
        oracle._graph._file_index = {}

        result = oracle.get_file_neighborhood([target])

        assert "jarvis:core/bar.py" in result.imports
        assert result.importers == []

    def test_classifies_incoming_calls_edge(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle, FileNeighborhood

        oracle = self._make_oracle({"jarvis": tmp_path})

        target = tmp_path / "core" / "service.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# service")

        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/service.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]

        caller_node_key = "jarvis:core/main.py:main_func"
        caller_node = MagicMock()
        caller_node.repo = "jarvis"
        caller_node.file_path = "core/main.py"
        oracle._graph._node_index = {caller_node_key: caller_node}
        oracle._graph.get_edges_from.return_value = []
        oracle._graph.get_edges_to.return_value = [
            (caller_node_key, {"edge_type": "calls"})
        ]
        oracle._graph._file_index = {}

        result = oracle.get_file_neighborhood([target])

        assert "jarvis:core/main.py" in result.callers
        assert result.callees == []

    def test_cross_repo_edge_uses_repo_prefix(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle

        prime_root = tmp_path / "prime"
        prime_root.mkdir()
        oracle = self._make_oracle({"jarvis": tmp_path / "jarvis", "prime": prime_root})

        jarvis_root = tmp_path / "jarvis"
        jarvis_root.mkdir()
        target = jarvis_root / "core" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# foo")

        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/foo.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]

        # Edge goes to a node in the "prime" repo
        prime_node_key = "prime:interfaces/base.py:BaseInterface"
        prime_node = MagicMock()
        prime_node.repo = "prime"
        prime_node.file_path = "interfaces/base.py"
        oracle._graph._node_index = {prime_node_key: prime_node}
        oracle._graph.get_edges_from.return_value = [
            (prime_node_key, {"edge_type": "inherits"})
        ]
        oracle._graph.get_edges_to.return_value = []
        oracle._graph._file_index = {}

        result = oracle.get_file_neighborhood([target])

        # Should be "prime:interfaces/base.py", not "jarvis:interfaces/base.py"
        assert "prime:interfaces/base.py" in result.base_classes

    def test_target_file_excluded_from_all_categories(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle

        oracle = self._make_oracle({"jarvis": tmp_path})

        target = tmp_path / "core" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# foo")

        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/foo.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]

        # Self-referential edge (edge to itself)
        self_key = "jarvis:core/foo.py:foo_func"
        self_node = MagicMock()
        self_node.repo = "jarvis"
        self_node.file_path = "core/foo.py"
        oracle._graph._node_index = {self_key: self_node}
        oracle._graph.get_edges_from.return_value = [(self_key, {"edge_type": "calls"})]
        oracle._graph.get_edges_to.return_value = []
        oracle._graph._file_index = {}

        result = oracle.get_file_neighborhood([target])

        # foo.py should NOT appear in any category
        assert "jarvis:core/foo.py" not in result.callees
        assert "jarvis:core/foo.py" not in result.all_unique_files()

    def test_test_counterpart_detected_by_basename(self, tmp_path):
        from backend.core.ouroboros.oracle import TheOracle

        oracle = self._make_oracle({"jarvis": tmp_path})

        target = tmp_path / "core" / "foo.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# foo")

        fake_node = MagicMock()
        fake_node.repo = "jarvis"
        fake_node.file_path = "core/foo.py"
        oracle._graph.find_nodes_in_file.return_value = [fake_node]
        oracle._graph.get_edges_from.return_value = []
        oracle._graph.get_edges_to.return_value = []

        # _file_index has "tests/test_foo.py"
        oracle._graph._file_index = {
            "tests/test_foo.py": {"jarvis:tests/test_foo.py:test_something"},
            "tests/test_bar.py": {"jarvis:tests/test_bar.py:test_other"},
        }

        result = oracle.get_file_neighborhood([target])

        assert "jarvis:tests/test_foo.py" in result.test_counterparts
        assert "jarvis:tests/test_bar.py" not in result.test_counterparts
```

**Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_ouroboros_governance/test_oracle_neighborhood.py::TestGetFileNeighborhood -v
```

Expected: All fail with `AttributeError: 'TheOracle' object has no attribute 'get_file_neighborhood'`

**Step 3: Add get_file_neighborhood() to TheOracle**

In `backend/core/ouroboros/oracle.py`, find `get_relevant_files_for_query` (currently around line 1583 after Task 1's insert, now shifted to ~1618). Insert the following immediately **before** that method:

```python
    def get_file_neighborhood(
        self,
        file_paths: List[Path],
    ) -> "FileNeighborhood":
        """Return the depth-1 structural neighborhood for a set of files.

        Traverses the codebase graph and classifies edges into semantic
        categories: imports, importers, callers, callees, inheritors,
        base_classes, test_counterparts.

        All returned paths are formatted as ``"{repo}:{relative_path}"``.

        This method is **synchronous** — it performs only in-memory graph
        traversal with no I/O.  It is safe to call from any context.

        Returns an empty ``FileNeighborhood`` if the oracle is not running
        or the graph is not yet indexed.
        """
        from backend.core.ouroboros.oracle import FileNeighborhood

        empty = FileNeighborhood(
            target_files=[],
            imports=[],
            importers=[],
            callers=[],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=[],
        )

        if not getattr(self, "_running", False):
            return empty

        # ── Resolve each abs_path to (repo_name, relative_path) ──────────
        resolved: List[tuple] = []  # (repo_name, relative_path_str, "repo:rel" key)
        for abs_path in file_paths:
            try:
                abs_path = Path(abs_path).resolve()
            except Exception:
                continue
            for repo_name, repo_root in self._repos.items():
                try:
                    rel = abs_path.relative_to(repo_root)
                    rel_str = str(rel)
                    resolved.append((repo_name, rel_str, f"{repo_name}:{rel_str}"))
                    break
                except ValueError:
                    continue

        if not resolved:
            return empty

        target_keys = {key for _, _, key in resolved}

        # ── Collect edge-classified neighbors ─────────────────────────────
        imports_set: set = set()
        importers_set: set = set()
        callers_set: set = set()
        callees_set: set = set()
        inheritors_set: set = set()
        base_classes_set: set = set()

        _import_edges = {"imports", "imports_from"}
        _call_edges = {"calls"}
        _inherit_edges = {"inherits"}

        for repo_name, rel_path, _ in resolved:
            try:
                nodes = self._graph.find_nodes_in_file(rel_path)
            except Exception:
                nodes = []

            for node in nodes:
                # Outgoing edges: imports → imports, calls → callees, inherits → base_classes
                try:
                    for target_key, edge_data in self._graph.get_edges_from(node):
                        edge_type = edge_data.get("edge_type", "")
                        target_node = self._graph._node_index.get(target_key)
                        if target_node is None:
                            continue
                        path_key = f"{target_node.repo}:{target_node.file_path}"
                        if path_key in target_keys:
                            continue
                        if edge_type in _import_edges:
                            imports_set.add(path_key)
                        elif edge_type in _call_edges:
                            callees_set.add(path_key)
                        elif edge_type in _inherit_edges:
                            base_classes_set.add(path_key)
                except Exception:
                    pass

                # Incoming edges: imports → importers, calls → callers, inherits → inheritors
                try:
                    for source_key, edge_data in self._graph.get_edges_to(node):
                        edge_type = edge_data.get("edge_type", "")
                        source_node = self._graph._node_index.get(source_key)
                        if source_node is None:
                            continue
                        path_key = f"{source_node.repo}:{source_node.file_path}"
                        if path_key in target_keys:
                            continue
                        if edge_type in _import_edges:
                            importers_set.add(path_key)
                        elif edge_type in _call_edges:
                            callers_set.add(path_key)
                        elif edge_type in _inherit_edges:
                            inheritors_set.add(path_key)
                except Exception:
                    pass

        # ── Test counterpart detection (basename heuristic) ───────────────
        test_counterparts: List[str] = []
        try:
            for repo_name, rel_path, _ in resolved:
                basename = Path(rel_path).name  # e.g. "foo.py"
                test_name = f"test_{basename}"  # e.g. "test_foo.py"
                for file_index_key in self._graph._file_index.keys():
                    if Path(file_index_key).name == test_name:
                        # Use local_repo for file_index entries (no repo prefix in _file_index)
                        candidate_key = f"{repo_name}:{file_index_key}"
                        if candidate_key not in target_keys:
                            test_counterparts.append(candidate_key)
        except Exception:
            pass

        # ── Determine local_repo for rendering ────────────────────────────
        local_repo = resolved[0][0] if resolved else "jarvis"

        return FileNeighborhood(
            target_files=sorted(target_keys),
            imports=sorted(imports_set),
            importers=sorted(importers_set),
            callers=sorted(callers_set),
            callees=sorted(callees_set),
            inheritors=sorted(inheritors_set),
            base_classes=sorted(base_classes_set),
            test_counterparts=sorted(set(test_counterparts)),
            local_repo=local_repo,
        )
```

**Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_ouroboros_governance/test_oracle_neighborhood.py -v
```

Expected: All 9 tests pass.

**Step 5: Run full test suite to verify no regressions**

```bash
python -m pytest tests/test_ouroboros_governance/ -v --tb=short 2>&1 | tail -20
```

Expected: All existing tests still pass.

**Step 6: Commit**

```bash
git add backend/core/ouroboros/oracle.py tests/test_ouroboros_governance/test_oracle_neighborhood.py
git commit -m "feat(oracle): add get_file_neighborhood() with depth-1 edge classification"
```

---

### Task 3: Update ContextExpander to Use get_file_neighborhood

**Files:**
- Modify: `backend/core/ouroboros/governance/context_expander.py`
- Test: `tests/test_ouroboros_governance/test_context_expander.py`

---

**Step 1: Write the failing tests**

Open `tests/test_ouroboros_governance/test_context_expander.py` and **append** a new test class at the end of the file:

```python
class TestContextExpanderNeighborhoodManifest:
    """Tests for the new get_file_neighborhood integration in ContextExpander."""

    def _make_ctx(self, description="fix the service", target_files=("backend/core/service.py",)):
        from backend.core.ouroboros.governance.op_context import OperationContext
        return OperationContext(
            op_id="test-op-1",
            description=description,
            target_files=list(target_files),
        )

    def _make_oracle(self, neighborhood=None):
        from backend.core.ouroboros.oracle import FileNeighborhood
        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}
        default_nh = neighborhood or FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=["jarvis:backend/core/base.py"],
            importers=[],
            callers=["jarvis:backend/core/main.py"],
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=["jarvis:tests/test_service.py"],
        )
        oracle.get_file_neighborhood.return_value = default_nh
        return oracle

    async def test_neighborhood_section_appears_in_prompt(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        oracle = self._make_oracle()
        # Create target file so _resolve_files confirms it exists
        svc = tmp_path / "backend" / "core" / "service.py"
        svc.parent.mkdir(parents=True, exist_ok=True)
        svc.write_text("# service")

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)

        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], oracle=oracle)

        assert "imports" in prompt.lower() or "neighborhood" in prompt.lower() or "related files" in prompt.lower()
        assert "jarvis:backend/core/base.py" in prompt

    async def test_neighborhood_section_truncates_at_10(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.oracle import FileNeighborhood

        # 15 callers — should be truncated to 10 with indicator
        callers = [f"jarvis:backend/core/caller_{i}.py" for i in range(15)]
        neighborhood = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=[],
            importers=[],
            callers=callers,
            callees=[],
            inheritors=[],
            base_classes=[],
            test_counterparts=[],
        )
        oracle = self._make_oracle(neighborhood=neighborhood)

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], oracle=oracle)

        # Exactly 10 callers shown, plus the "and N more" indicator
        shown = sum(1 for i in range(15) if f"caller_{i}.py" in prompt)
        assert shown == 10
        assert "and 5 more" in prompt

    async def test_no_neighborhood_section_when_oracle_not_running(self, tmp_path):
        from backend.core.ouroboros.governance.context_expander import ContextExpander

        oracle = MagicMock()
        oracle.get_status.return_value = {"running": False}

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)
        ctx = self._make_ctx()
        prompt = expander._build_expansion_prompt(ctx, [], oracle=oracle)

        # get_file_neighborhood should NOT be called
        oracle.get_file_neighborhood.assert_not_called()
        # The "Available files" / neighborhood section should be absent
        assert "jarvis:" not in prompt
```

Add `from unittest.mock import MagicMock` at the top of the test file if not already imported (check first).

**Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_ouroboros_governance/test_context_expander.py::TestContextExpanderNeighborhoodManifest -v
```

Expected: All 3 fail with signature mismatches or missing attributes.

**Step 3: Update context_expander.py**

Open `backend/core/ouroboros/governance/context_expander.py`.

**3a. Add module-level constant** (after the existing `MAX_FILES_PER_ROUND` constant):

```python
MAX_FILES_PER_CATEGORY: int = 10          # Token Explosion Trap limit per category
```

**3b. Add `_render_neighborhood_section` private method** (add as a new method after `_build_expansion_prompt`):

```python
    def _render_neighborhood_section(self, neighborhood: Any) -> str:
        """Render a FileNeighborhood as a labeled multi-section string.

        Each category is limited to MAX_FILES_PER_CATEGORY entries.
        Truncated categories append ``"  ... (and N more)"``.
        Returns empty string if neighborhood has no neighbors.
        """
        try:
            categories = neighborhood.to_dict()
        except Exception:
            return ""
        if not categories:
            return ""

        lines = ["\nStructural file neighborhood (real codebase graph edges):"]
        for category, paths in categories.items():
            label = category.replace("_", " ").title()
            shown = paths[:MAX_FILES_PER_CATEGORY]
            hidden = len(paths) - len(shown)
            lines.append(f"  {label}:")
            for p in shown:
                lines.append(f"    - {p}")
            if hidden > 0:
                lines.append(f"    ... (and {hidden} more)")
        lines.append(
            "\nWhich of these (if any) would help you understand the context?\n"
        )
        return "\n".join(lines)
```

**3c. Update `_build_expansion_prompt` signature** — change:

```python
    def _build_expansion_prompt(
        self,
        ctx: OperationContext,
        already_fetched: List[str],
        oracle_files: Optional[List[str]] = None,
    ) -> str:
```

to:

```python
    def _build_expansion_prompt(
        self,
        ctx: OperationContext,
        already_fetched: List[str],
        oracle_files: Optional[List[str]] = None,
        oracle: Optional[Any] = None,
    ) -> str:
```

**3d. Replace the `available_section` block inside `_build_expansion_prompt`** — replace the existing block that sets `available_section`:

```python
        available_section = ""
        if oracle_files:
            available_section = (
                "\nAvailable files related to this task (real paths — choose from these):\n"
                + "".join(f"  - {f}\n" for f in oracle_files)
                + "\nWhich of these (if any) would help you generate a correct patch?\n"
            )
```

with:

```python
        available_section = ""
        if oracle is not None:
            try:
                status = oracle.get_status()
                if status.get("running", False):
                    target_abs = [
                        self._repo_root / f for f in ctx.target_files
                    ]
                    neighborhood = oracle.get_file_neighborhood(target_abs)
                    available_section = self._render_neighborhood_section(neighborhood)
            except Exception:
                available_section = ""  # fall back silently
        elif oracle_files:
            # Legacy fallback: flat file list from keyword query
            available_section = (
                "\nAvailable files related to this task (real paths — choose from these):\n"
                + "".join(f"  - {f}\n" for f in oracle_files)
                + "\nWhich of these (if any) would help you generate a correct patch?\n"
            )
```

**3e. Update the call site in `expand()`** — find the call to `_build_expansion_prompt` inside the `for round_num` loop:

```python
            prompt = self._build_expansion_prompt(ctx, accumulated, oracle_files)
```

Change it to:

```python
            prompt = self._build_expansion_prompt(ctx, accumulated, oracle=self._oracle)
```

(The `oracle_files` keyword argument is now unused at the call site — `_build_expansion_prompt` uses the oracle directly.)

**Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_ouroboros_governance/test_context_expander.py -v
```

Expected: All tests pass (including the 3 existing oracle manifest tests from Phase 1 and the 3 new neighborhood tests).

**Step 5: Run full test suite**

```bash
python -m pytest tests/test_ouroboros_governance/ -v --tb=short 2>&1 | tail -25
```

Expected: All tests pass.

**Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/context_expander.py tests/test_ouroboros_governance/test_context_expander.py
git commit -m "feat(context-expander): use structural graph neighborhood instead of keyword oracle query"
```

---

### Task 4: Final Integration Smoke-Check

**Files:**
- Test: `tests/test_ouroboros_governance/test_oracle_neighborhood.py`

---

**Step 1: Write integration smoke-check test**

Append to `tests/test_ouroboros_governance/test_oracle_neighborhood.py`:

```python
class TestFileNeighborhoodEndToEnd:
    """Smoke-check: FileNeighborhood flows through ContextExpander._build_expansion_prompt."""

    async def test_neighborhood_flows_into_expansion_prompt(self, tmp_path):
        """Full pipeline: oracle.get_file_neighborhood → _render_neighborhood_section → prompt."""
        from backend.core.ouroboros.governance.context_expander import ContextExpander
        from backend.core.ouroboros.governance.op_context import OperationContext
        from backend.core.ouroboros.oracle import FileNeighborhood
        from unittest.mock import MagicMock

        # Build a neighborhood with multiple categories
        neighborhood = FileNeighborhood(
            target_files=["jarvis:backend/core/service.py"],
            imports=["jarvis:backend/core/base_service.py"],
            importers=["jarvis:backend/core/main.py"],
            callers=["jarvis:backend/core/handler.py"],
            callees=[],
            inheritors=[],
            base_classes=["jarvis:backend/core/abstract.py"],
            test_counterparts=["jarvis:tests/test_service.py"],
        )

        oracle = MagicMock()
        oracle.get_status.return_value = {"running": True}
        oracle.get_file_neighborhood.return_value = neighborhood

        expander = ContextExpander(generator=MagicMock(), repo_root=tmp_path, oracle=oracle)

        ctx = OperationContext(
            op_id="e2e-test",
            description="refactor the service layer",
            target_files=["backend/core/service.py"],
        )
        prompt = expander._build_expansion_prompt(ctx, [], oracle=oracle)

        # All categories with data should appear in the prompt
        assert "Imports" in prompt or "imports" in prompt
        assert "jarvis:backend/core/base_service.py" in prompt
        assert "Importers" in prompt or "importers" in prompt
        assert "jarvis:backend/core/main.py" in prompt
        assert "Callers" in prompt or "callers" in prompt
        assert "jarvis:backend/core/handler.py" in prompt
        assert "Base Classes" in prompt or "base_classes" in prompt
        assert "jarvis:backend/core/abstract.py" in prompt
        assert "Test Counterparts" in prompt or "test_counterparts" in prompt
        assert "jarvis:tests/test_service.py" in prompt

        # Empty categories (callees, inheritors) should NOT appear
        assert "callees" not in prompt.lower()
        assert "inheritors" not in prompt.lower()
```

**Step 2: Run smoke-check to verify it passes**

```bash
python -m pytest tests/test_ouroboros_governance/test_oracle_neighborhood.py::TestFileNeighborhoodEndToEnd -v
```

Expected: 1 passed.

**Step 3: Run complete test suite one final time**

```bash
python -m pytest tests/test_ouroboros_governance/ -v 2>&1 | tail -30
```

Expected: All tests pass. Note the count — it should be at least 144+ (141 from Phase 1 + at least 10 new tests from this feature).

**Step 4: Final commit**

```bash
git add tests/test_ouroboros_governance/test_oracle_neighborhood.py
git commit -m "test(oracle): add end-to-end neighborhood integration smoke-check"
```

---

## Verification Checklist

Before finishing this branch:

- [ ] `FileNeighborhood` dataclass in `oracle.py` — `to_dict()` omits empty categories, `all_unique_files()` deduplicates and excludes targets
- [ ] `TheOracle.get_file_neighborhood()` is **synchronous** (no `async def`, no `await`)
- [ ] Edge classification correct: `imports`/`imports_from` → `imports`/`importers`; `calls` → `callees`/`callers`; `inherits` → `base_classes`/`inheritors`
- [ ] Cross-repo edges use `target_node.repo` / `source_node.repo`, NOT hard-coded `"jarvis"`
- [ ] Input files excluded from all output categories
- [ ] Test counterpart detection uses `Path(file_index_key).name == f"test_{basename}"`
- [ ] `MAX_FILES_PER_CATEGORY = 10` constant in `context_expander.py`
- [ ] `_render_neighborhood_section()` truncates at 10 per category, appends `"... (and N more)"`
- [ ] `_build_expansion_prompt` uses `oracle.get_file_neighborhood()` (sync call, no `await`)
- [ ] `expand()` call site updated to pass `oracle=self._oracle`
- [ ] Oracle failures in `_build_expansion_prompt` are caught silently (fall back to empty section)
- [ ] All tests pass: `python -m pytest tests/test_ouroboros_governance/ -v`
