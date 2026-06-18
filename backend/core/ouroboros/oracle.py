"""
The Oracle - GraphRAG Codebase Knowledge Graph v1.0
====================================================

"God Mode" Pillar 1: Structural Understanding

This module provides JARVIS with omniscient knowledge of codebase structure
through a directed graph that captures not just what code exists, but how
it's connected - imports, calls, inheritance, and data flow.

Key Differences from Text-Based AI:
- Claude Code sees: "bag of files with similar words"
- JARVIS Oracle sees: "living web of connected logic"

When you say "Fix the auth bug", standard AI checks files with "auth" in name.
Oracle finds auth file AND the database it imports AND middleware that calls it.
It sees the BLAST RADIUS of every change.

Architecture:
- Nodes: Files, Classes, Functions, Variables, Imports
- Edges: IMPORTS, CALLS, INHERITS, USES, DEFINES, OVERRIDES
- Storage: NetworkX DiGraph with persistent JSON/pickle caching
- Querying: Graph traversal, shortest paths, subgraph extraction

Features:
- Async parallel file indexing
- Incremental updates (only re-index changed files)
- Cross-repo graph connectivity (JARVIS + Prime + Reactor)
- Blast radius analysis for change impact
- Dependency chain visualization
- Dead code detection
- Circular dependency detection

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import pickle
import sys
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import enum
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Generator,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

try:
    import networkx as nx
    NETWORKX_AVAILABLE = True
except ImportError:
    NETWORKX_AVAILABLE = False
    nx = None

logger = logging.getLogger("Ouroboros.Oracle")


# ---------------------------------------------------------------------------
# Boot-timing phase wrapper — observability only.
#
# Sub-instruments TheOracle.initialize() so operators can see where
# the wall-clock goes (cache_load vs full_index vs semantic_index_init).
#
# **Pure observability** — does NOT reorder, parallelize, or alter any
# init step. The macOS ARM64 libmalloc-safe ordering documented at
# the existing in-line comments (graph BEFORE Chroma, sync cache load
# never offloaded to asyncio.to_thread) is preserved verbatim. Wrapping
# the existing sequential calls only records ``time.monotonic()`` deltas;
# failure is silent and never raises into the boot path.
# ---------------------------------------------------------------------------


class _OraclePhase:
    """Defensive boot-timing phase wrapper.

    Lazy-imports :mod:`backend.core.ouroboros.battle_test.boot_timing`
    so this module stays importable when the timing helper is missing.
    NEVER raises into the boot path. Pure observability — does not
    affect ordering or behavior.
    """

    __slots__ = ("_name", "_timer")

    def __init__(self, name: str) -> None:
        self._name = name
        self._timer = None

    def __enter__(self) -> "_OraclePhase":
        try:
            from backend.core.ouroboros.battle_test.boot_timing import (
                get_default_timer,
            )
            self._timer = get_default_timer()
            self._timer.begin(self._name)
        except Exception:  # noqa: BLE001 — defensive
            self._timer = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            if self._timer is not None:
                self._timer.end(self._name)
        except Exception:  # noqa: BLE001
            pass
        return False  # never swallow exceptions


# =============================================================================
# CONFIGURATION
# =============================================================================

class OracleConfig:
    """Dynamic configuration for the Oracle."""

    # Repo paths (auto-detected from environment)
    JARVIS_PATH = Path(os.getenv("JARVIS_PATH", Path.home() / "Documents/repos/JARVIS-AI-Agent"))
    JARVIS_PRIME_PATH = Path(os.getenv("JARVIS_PRIME_PATH", Path.home() / "Documents/repos/jarvis-prime"))
    REACTOR_CORE_PATH = Path(os.getenv("REACTOR_CORE_PATH", Path.home() / "Documents/repos/reactor-core"))

    # Cache paths
    ORACLE_CACHE_DIR = Path(os.getenv("ORACLE_CACHE_DIR", Path.home() / ".jarvis/oracle"))
    GRAPH_CACHE_FILE = ORACLE_CACHE_DIR / "codebase_graph.pkl"
    INDEX_CACHE_FILE = ORACLE_CACHE_DIR / "file_index.json"
    # Phase 2 — incremental SQLite persistence (gated; see oracle_persistence.py). Sibling of
    # the legacy pickle so sandbox_fallback resolves both to the same dir symmetrically.
    SQLITE_DB_FILE = ORACLE_CACHE_DIR / "oracle.db"

    # Semantic index (ChromaDB)
    CHROMA_PERSIST_DIR: Path = Path(os.getenv(
        "ORACLE_CHROMA_DIR",
        str(Path.home() / ".jarvis/oracle/chroma"),
    ))
    CHROMA_COLLECTION_NAME: str = os.getenv(
        "ORACLE_CHROMA_COLLECTION", "jarvis_oracle_symbols"
    )
    SEMANTIC_EMBED_MODEL: str = os.getenv(
        "ORACLE_EMBED_MODEL", "all-MiniLM-L6-v2"
    )
    SEMANTIC_EMBED_BATCH_SIZE: int = int(os.getenv("ORACLE_EMBED_BATCH", "128"))

    # Indexing settings
    MAX_PARALLEL_FILES = int(os.getenv("ORACLE_MAX_PARALLEL", "50"))
    EXCLUDE_PATTERNS = [
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        "*.pyc",
        "*.pyo",
        ".eggs",
        "*.egg-info",
        "build",
        "dist",
        # Slice 44 — never index worktree checkouts (each is a full repo
        # copy → 62 of them = 492k duplicate .py files; the recursive
        # scan_dir walk held the GIL and starved the asyncio loop) or the
        # session/telemetry tree.
        ".worktrees",
        ".ouroboros",
        # Slice 49 — the substring match below (``pattern in path_str``)
        # means ``.worktrees`` never matched ``.jarvis/swe_bench_pro/
        # worktrees`` (437MB / 26,839 files), so v44 walked + ast-parsed
        # that whole tree at 107% CPU. ``.jarvis`` is the repo-local state
        # dir (swe_bench checkouts, session telemetry, locks) — never source
        # to index. Substring ``.jarvis`` covers the entire subtree.
        ".jarvis",
        # Slice 257 — Claude Code's agent worktrees live under
        # ``.claude/worktrees/<name>`` (each a full 29k-file checkout). The
        # ``.worktrees`` pattern above does NOT match ``.claude/worktrees``
        # (slash, not dot), so the Oracle recursed into all of them and
        # ast-parsed 6× the tree → process-pool saturation → loop
        # starvation → stale heartbeat → ExternalWatchdog SIGKILL
        # (bt-2026-06-16-042304, exit 137). ``.claude`` is tooling state,
        # never source to index — substring covers the whole subtree.
        ".claude",
    ]

    # File types to index
    SUPPORTED_EXTENSIONS = {".py"}  # Start with Python, can extend

    # Graph analysis
    MAX_BLAST_RADIUS_DEPTH = int(os.getenv("ORACLE_BLAST_DEPTH", "5"))
    MAX_CALL_CHAIN_DEPTH = int(os.getenv("ORACLE_CALL_DEPTH", "10"))


# =============================================================================
# Slice 32 — process-pool master switch (escape hatch only)
# =============================================================================
#
# Default-FALSE per operator binding: the new process-pool path
# (composing ast_compile_helper.analyze_python_source_for_oracle) is
# the active path. Setting JARVIS_ORACLE_LEGACY_THREAD_MODE=1
# restores the pre-Slice-32 asyncio.to_thread path byte-identically —
# emergency rollback only. Slice 32 closes v25
# (bt-2026-05-27-194342) 25-min asyncio loop wedge.

_ORACLE_LEGACY_THREAD_MODE_ENV: str = "JARVIS_ORACLE_LEGACY_THREAD_MODE"


def _is_oracle_legacy_thread_mode() -> bool:
    """Slice 32 — return True iff the operator has explicitly opted
    back into the pre-Slice-32 threadpool path (escape hatch). Empty
    / unset / unrecognized → False (new path active)."""
    raw = os.environ.get(_ORACLE_LEGACY_THREAD_MODE_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _is_linked_git_worktree(path: Path) -> bool:
    """Return True iff ``path`` is the root of a *linked* git worktree.

    Slice 257 — the static EXCLUDE_PATTERNS substring list can only catch
    worktrees whose parent directory we already know to name (``.worktrees``,
    ``.claude``, ``.jarvis/.../worktrees``). This is the general guard for any
    future ``git worktree add`` location, with no hardcoded names.

    The discriminator is exact, not heuristic: ``git worktree add`` creates a
    ``.git`` **file** holding ``gitdir: <path>`` — never a directory. So:

      * linked worktree  → ``<dir>/.git`` is a FILE   → skip (duplicate
        checkout, 0 files tracked by the main repo)
      * embedded clone /
        first-class source → ``<dir>/.git`` is a DIR  → DO NOT skip
        (e.g. ``backend/vision`` is a stray nested repo but its 229 ``.py``
        files are tracked source the organism reasons about)
      * the main repo root → ``.git`` is a DIR        → never skipped

    Indexing 6× duplicate worktree checkouts is what saturated the process
    pool, starved the asyncio loop, and tripped the ExternalWatchdog SIGKILL
    in bt-2026-06-16-042304. Targeting *only* the file-``.git`` case removes
    that load without dropping any real source from the index.

    Fail-soft by construction: any OSError (permission, race, odd path) is
    swallowed and treated as "not a worktree", so the guard can only ever
    *add* exclusions for true linked worktrees — never crash the walk or skip
    legitimate source.
    """
    try:
        return (path / ".git").is_file()
    except OSError:
        return False


# Slice 33 Arc 2 Phase 3 — async graph-write queue master switch.
# Default TRUE — closes the v28 LoopSink-confirmed sink:
#   oracle._index_file.graph_write_bulk 76 occurrences peak 3,580 ms
# Setting JARVIS_ORACLE_GRAPH_QUEUE_ENABLED=0 restores the pre-
# Slice-33-Arc-2-P3 inline-write path byte-identically (escape hatch).

_ORACLE_GRAPH_QUEUE_ENABLED_ENV: str = "JARVIS_ORACLE_GRAPH_QUEUE_ENABLED"
_ORACLE_GRAPH_QUEUE_MAX_SIZE_ENV: str = "JARVIS_ORACLE_GRAPH_QUEUE_MAX_SIZE"
_ORACLE_GRAPH_QUEUE_BATCH_SIZE_ENV: str = "JARVIS_ORACLE_GRAPH_QUEUE_BATCH_SIZE"
_DEFAULT_GRAPH_QUEUE_MAX_SIZE: int = 1000
_DEFAULT_GRAPH_QUEUE_BATCH_SIZE: int = 50


def _is_oracle_graph_queue_enabled() -> bool:
    """Slice 33 Arc 2 Phase 3 — default TRUE. Explicit falsy values
    restore inline-write path (escape hatch only)."""
    raw = os.environ.get(_ORACLE_GRAPH_QUEUE_ENABLED_ENV, "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def _oracle_graph_queue_max_size() -> int:
    try:
        raw = os.environ.get(_ORACLE_GRAPH_QUEUE_MAX_SIZE_ENV, "").strip()
        if not raw:
            return _DEFAULT_GRAPH_QUEUE_MAX_SIZE
        return max(10, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_GRAPH_QUEUE_MAX_SIZE


def _oracle_graph_queue_batch_size() -> int:
    try:
        raw = os.environ.get(_ORACLE_GRAPH_QUEUE_BATCH_SIZE_ENV, "").strip()
        if not raw:
            return _DEFAULT_GRAPH_QUEUE_BATCH_SIZE
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_GRAPH_QUEUE_BATCH_SIZE


# =============================================================================
# ENUMS AND TYPES
# =============================================================================

class NodeType(Enum):
    """Types of nodes in the knowledge graph."""
    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    IMPORT = "import"
    CONSTANT = "constant"
    DECORATOR = "decorator"


class EdgeType(Enum):
    """Types of edges (relationships) in the knowledge graph."""
    IMPORTS = "imports"           # File/module imports another
    IMPORTS_FROM = "imports_from" # from X import Y
    CALLS = "calls"               # Function/method calls another
    INHERITS = "inherits"         # Class inherits from another
    USES = "uses"                 # Uses a variable/constant
    DEFINES = "defines"           # File/class defines function/variable
    CONTAINS = "contains"         # File contains class/function
    OVERRIDES = "overrides"       # Method overrides parent method
    DECORATES = "decorates"       # Decorator decorates function/class
    INSTANTIATES = "instantiates" # Creates instance of class
    RETURNS = "returns"           # Function returns type
    PARAMETER = "parameter"       # Function has parameter of type


@dataclass(frozen=True)
class NodeID:
    """Unique identifier for a node in the graph."""
    repo: str           # Repository name (jarvis, prime, reactor)
    file_path: str      # Relative file path
    name: str           # Entity name (class/function/variable)
    node_type: NodeType
    line_number: int = 0

    def __str__(self) -> str:
        return f"{self.repo}:{self.file_path}:{self.name}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo": self.repo,
            "file_path": self.file_path,
            "name": self.name,
            "node_type": self.node_type.value,
            "line_number": self.line_number,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NodeID":
        return cls(
            repo=data["repo"],
            file_path=data["file_path"],
            name=data["name"],
            node_type=NodeType(data["node_type"]),
            line_number=data.get("line_number", 0),
        )


@dataclass
class NodeData:
    """Metadata for a node in the graph."""
    node_id: NodeID
    docstring: Optional[str] = None
    signature: Optional[str] = None
    decorators: List[str] = field(default_factory=list)
    base_classes: List[str] = field(default_factory=list)
    complexity: int = 0  # Cyclomatic complexity
    line_count: int = 0
    last_modified: float = 0.0
    source_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id.to_dict(),
            "docstring": self.docstring,
            "signature": self.signature,
            "decorators": self.decorators,
            "base_classes": self.base_classes,
            "complexity": self.complexity,
            "line_count": self.line_count,
            "last_modified": self.last_modified,
            "source_hash": self.source_hash,
        }


@dataclass
class EdgeData:
    """Metadata for an edge in the graph."""
    edge_type: EdgeType
    line_number: int = 0
    context: str = ""  # Additional context (e.g., how it's used)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_type": self.edge_type.value,
            "line_number": self.line_number,
            "context": self.context,
        }


@dataclass
class BlastRadius:
    """Impact analysis of a change to a node."""
    source_node: NodeID
    directly_affected: Set[NodeID]       # Nodes that directly use this
    transitively_affected: Set[NodeID]   # Nodes affected through dependencies
    broken_imports: List[Tuple[NodeID, str]]  # (node, import_name)
    broken_calls: List[Tuple[NodeID, str]]    # (node, call_name)
    risk_level: str = "low"              # low, medium, high, critical

    @property
    def total_affected(self) -> int:
        return len(self.directly_affected) + len(self.transitively_affected)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": str(self.source_node),
            "directly_affected": [str(n) for n in self.directly_affected],
            "transitively_affected": [str(n) for n in self.transitively_affected],
            "broken_imports": [(str(n), name) for n, name in self.broken_imports],
            "broken_calls": [(str(n), name) for n, name in self.broken_calls],
            "total_affected": self.total_affected,
            "risk_level": self.risk_level,
        }


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
    semantic_support: List[str] = field(default_factory=list)
    # Cross-repo files discovered via semantic similarity seeding (same repo:path format)
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
                "semantic_support": self.semantic_support,
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
            + self.semantic_support
        ):
            if path not in target_set and path not in seen:
                seen.add(path)
                result.append(path)
        return result


# =============================================================================
# AST VISITOR FOR CODE EXTRACTION
# =============================================================================

class CodeStructureVisitor(ast.NodeVisitor):
    """
    Advanced AST visitor that extracts structural information from Python code.

    Extracts:
    - Classes and their methods/attributes
    - Functions and their calls
    - Imports and their aliases
    - Variable assignments
    - Decorators
    - Type hints
    """

    def __init__(self, repo: str, file_path: str, source: str):
        self.repo = repo
        self.file_path = file_path
        self.source = source
        self.source_lines = source.split('\n')

        # Extracted data
        self.nodes: List[NodeData] = []
        self.edges: List[Tuple[NodeID, NodeID, EdgeData]] = []

        # Tracking state
        self._current_class: Optional[str] = None
        self._current_function: Optional[str] = None
        self._scope_stack: List[str] = []
        self._imported_names: Dict[str, str] = {}  # alias -> full path

        # Create file node
        file_node = NodeID(
            repo=repo,
            file_path=file_path,
            name=Path(file_path).stem,
            node_type=NodeType.FILE,
        )
        self.nodes.append(NodeData(
            node_id=file_node,
            line_count=len(self.source_lines),
            source_hash=hashlib.md5(source.encode()).hexdigest(),
        ))
        self._file_node = file_node

    def _make_node_id(
        self,
        name: str,
        node_type: NodeType,
        line_number: int = 0,
    ) -> NodeID:
        """Create a node ID with current scope context."""
        full_name = name
        if self._current_class:
            full_name = f"{self._current_class}.{name}"

        return NodeID(
            repo=self.repo,
            file_path=self.file_path,
            name=full_name,
            node_type=node_type,
            line_number=line_number,
        )

    def _get_docstring(self, node: ast.AST) -> Optional[str]:
        """Extract docstring from a node if present."""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            return ast.get_docstring(node)
        return None

    def _get_function_signature(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> str:
        """Extract function signature as string."""
        args = []

        # Regular arguments
        for arg in node.args.args:
            arg_str = arg.arg
            if arg.annotation:
                arg_str += f": {ast.unparse(arg.annotation)}"
            args.append(arg_str)

        # *args
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")

        # **kwargs
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")

        sig = f"({', '.join(args)})"

        # Return type
        if node.returns:
            sig += f" -> {ast.unparse(node.returns)}"

        return sig

    def _get_decorators(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef]) -> List[str]:
        """Extract decorator names."""
        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                decorators.append(dec.id)
            elif isinstance(dec, ast.Attribute):
                decorators.append(ast.unparse(dec))
            elif isinstance(dec, ast.Call):
                if isinstance(dec.func, ast.Name):
                    decorators.append(dec.func.id)
                elif isinstance(dec.func, ast.Attribute):
                    decorators.append(ast.unparse(dec.func))
        return decorators

    def _estimate_complexity(self, node: ast.AST) -> int:
        """Estimate cyclomatic complexity of a function/method."""
        complexity = 1  # Base complexity

        for child in ast.walk(node):
            # Decision points that increase complexity
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                complexity += 1
            elif isinstance(child, ast.ExceptHandler):
                complexity += 1
            elif isinstance(child, (ast.And, ast.Or)):
                complexity += 1
            elif isinstance(child, ast.comprehension):
                complexity += 1
            elif isinstance(child, ast.Assert):
                complexity += 1

        return complexity

    def visit_Import(self, node: ast.Import) -> None:
        """Handle 'import X' statements."""
        for alias in node.names:
            import_name = alias.asname or alias.name
            self._imported_names[import_name] = alias.name

            # Create import node
            import_node = self._make_node_id(
                alias.name,
                NodeType.IMPORT,
                node.lineno,
            )

            # Edge: File imports module
            self.edges.append((
                self._file_node,
                import_node,
                EdgeData(EdgeType.IMPORTS, node.lineno),
            ))

        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Handle 'from X import Y' statements."""
        module = node.module or ""

        for alias in node.names:
            import_name = alias.asname or alias.name
            full_path = f"{module}.{alias.name}" if module else alias.name
            self._imported_names[import_name] = full_path

            # Create import node
            import_node = self._make_node_id(
                full_path,
                NodeType.IMPORT,
                node.lineno,
            )

            # Edge: File imports from module
            self.edges.append((
                self._file_node,
                import_node,
                EdgeData(EdgeType.IMPORTS_FROM, node.lineno, context=module),
            ))

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Handle class definitions."""
        class_node = self._make_node_id(
            node.name,
            NodeType.CLASS,
            node.lineno,
        )

        # Extract base classes
        base_classes = []
        for base in node.bases:
            base_name = ast.unparse(base)
            base_classes.append(base_name)

            # Edge: Class inherits from base
            base_node = self._make_node_id(
                base_name,
                NodeType.CLASS,
                node.lineno,
            )
            self.edges.append((
                class_node,
                base_node,
                EdgeData(EdgeType.INHERITS, node.lineno),
            ))

        # Create class node data
        self.nodes.append(NodeData(
            node_id=class_node,
            docstring=self._get_docstring(node),
            decorators=self._get_decorators(node),
            base_classes=base_classes,
            line_count=node.end_lineno - node.lineno + 1 if node.end_lineno else 0,
        ))

        # Edge: File contains class
        self.edges.append((
            self._file_node,
            class_node,
            EdgeData(EdgeType.CONTAINS, node.lineno),
        ))

        # Visit class body with context
        old_class = self._current_class
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = old_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Handle function/method definitions."""
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Handle async function/method definitions."""
        self._visit_function(node)

    def _visit_function(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> None:
        """Common handler for function definitions."""
        node_type = NodeType.METHOD if self._current_class else NodeType.FUNCTION

        func_node = self._make_node_id(
            node.name,
            node_type,
            node.lineno,
        )

        # Create function node data
        self.nodes.append(NodeData(
            node_id=func_node,
            docstring=self._get_docstring(node),
            signature=self._get_function_signature(node),
            decorators=self._get_decorators(node),
            complexity=self._estimate_complexity(node),
            line_count=node.end_lineno - node.lineno + 1 if node.end_lineno else 0,
        ))

        # Edge: Parent contains function
        if self._current_class:
            parent_node = self._make_node_id(
                self._current_class,
                NodeType.CLASS,
            )
        else:
            parent_node = self._file_node

        self.edges.append((
            parent_node,
            func_node,
            EdgeData(EdgeType.CONTAINS, node.lineno),
        ))

        # Check for override
        if self._current_class and node.name not in ("__init__", "__new__"):
            for dec in self._get_decorators(node):
                if "override" in dec.lower():
                    # This is an override - we'll link to parent later
                    pass

        # Visit function body
        old_func = self._current_function
        self._current_function = node.name

        # Extract calls within function
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                self._visit_call_in_function(child, func_node)

        self._current_function = old_func

    def _visit_call_in_function(self, call: ast.Call, caller: NodeID) -> None:
        """Extract function calls."""
        # Get the called function name
        callee_name = None

        if isinstance(call.func, ast.Name):
            callee_name = call.func.id
        elif isinstance(call.func, ast.Attribute):
            # Could be self.method() or module.function()
            callee_name = ast.unparse(call.func)

        if callee_name:
            # Resolve imported names
            base_name = callee_name.split('.')[0]
            if base_name in self._imported_names:
                full_name = self._imported_names[base_name]
                if '.' in callee_name:
                    callee_name = full_name + '.' + '.'.join(callee_name.split('.')[1:])
                else:
                    callee_name = full_name

            # Create callee node (may not exist in graph yet)
            callee_node = NodeID(
                repo=self.repo,
                file_path="<external>",  # May be external
                name=callee_name,
                node_type=NodeType.FUNCTION,
                line_number=call.lineno,
            )

            # Edge: Caller calls callee
            self.edges.append((
                caller,
                callee_node,
                EdgeData(EdgeType.CALLS, call.lineno),
            ))

    def visit_Assign(self, node: ast.Assign) -> None:
        """Handle variable assignments at module level."""
        if not self._current_function and not self._current_class:
            # Module-level assignment
            for target in node.targets:
                if isinstance(target, ast.Name):
                    var_node = self._make_node_id(
                        target.id,
                        NodeType.VARIABLE,
                        node.lineno,
                    )
                    self.nodes.append(NodeData(node_id=var_node))

                    # Edge: File defines variable
                    self.edges.append((
                        self._file_node,
                        var_node,
                        EdgeData(EdgeType.DEFINES, node.lineno),
                    ))

        self.generic_visit(node)


# =============================================================================
# KNOWLEDGE GRAPH
# =============================================================================

class CodebaseKnowledgeGraph:
    """
    The Oracle's core: A directed graph representing the entire codebase structure.

    This is what makes JARVIS see code as connected logic, not just text.
    """

    def __init__(self):
        if not NETWORKX_AVAILABLE:
            raise ImportError("networkx is required for Oracle. Install with: pip install networkx")

        self._graph: nx.DiGraph = nx.DiGraph()
        self._node_index: Dict[str, NodeID] = {}  # str(node_id) -> NodeID
        self._file_index: Dict[str, Set[str]] = defaultdict(set)  # file_path -> node_ids
        self._repo_index: Dict[str, Set[str]] = defaultdict(set)  # repo -> node_ids
        self._type_index: Dict[NodeType, Set[str]] = defaultdict(set)  # type -> node_ids

        # Metrics
        self._metrics = {
            "total_nodes": 0,
            "total_edges": 0,
            "files_indexed": 0,
            "last_full_index": 0.0,
            "last_incremental_update": 0.0,
        }

    def add_node(self, node_data: NodeData) -> None:
        """Add a node to the graph."""
        node_id = node_data.node_id
        node_key = str(node_id)

        # Task #106 — O(1) incremental node count.  NetworkX
        # add_node on an EXISTING key updates attributes without
        # changing the node count, so only genuinely-new keys are
        # counted.  ``node_key not in self._graph`` is O(1)
        # (``__contains__`` → ``n in self._node`` dict membership).
        # Symmetric with the edge counter below; robust against
        # future NetworkX ``NodeView.__len__`` semantics.
        _is_new_node = node_key not in self._graph

        self._graph.add_node(
            node_key,
            **node_data.to_dict(),
        )

        # Update indices
        self._node_index[node_key] = node_id
        self._file_index[node_id.file_path].add(node_key)
        self._repo_index[node_id.repo].add(node_key)
        self._type_index[node_id.node_type].add(node_key)

        if _is_new_node:
            self._metrics["total_nodes"] += 1

    def prune_isolated_nodes(self) -> int:
        """Slice 112 graph hygiene — drop degree-0 (isolated) nodes: symbols
        with NO callers and NO callees. They are pure serialization bloat —
        ``shortest_path`` / ``simple_cycles`` traverse *edges*, so a node with
        zero edges can never appear in any path or cycle. Removing them shrinks
        the cache to the minimum required for graph traversal without changing
        a single traversal result. Keeps every index + the node/edge metrics
        consistent. Returns the count pruned. NEVER raises."""
        try:
            import networkx as nx
            isolated = list(nx.isolates(self._graph))
            if not isolated:
                return 0
            for node_key in isolated:
                try:
                    self._graph.remove_node(node_key)
                except Exception:  # noqa: BLE001
                    continue
                nid = self._node_index.pop(node_key, None)
                if nid is not None:
                    self._file_index.get(nid.file_path, set()).discard(node_key)
                    self._repo_index.get(nid.repo, set()).discard(node_key)
                    self._type_index.get(nid.node_type, set()).discard(node_key)
            # Drop now-empty index buckets so they don't re-bloat the pickle.
            for idx in (self._file_index, self._repo_index, self._type_index):
                for k in [k for k, v in idx.items() if not v]:
                    idx.pop(k, None)
            # Recompute authoritative counts from the live graph.
            self._metrics["total_nodes"] = self._graph.number_of_nodes()
            self._metrics["total_edges"] = self._graph.number_of_edges()
            logger.info("[GraphHygiene] pruned %d isolated node(s) → %d nodes / %d edges",
                        len(isolated), self._metrics["total_nodes"], self._metrics["total_edges"])
            return len(isolated)
        except Exception as exc:  # noqa: BLE001 — hygiene must never break save
            logger.warning("[GraphHygiene] prune_isolated_nodes failed (non-fatal): %s", exc)
            return 0

    def add_edge(
        self,
        source: NodeID,
        target: NodeID,
        edge_data: EdgeData,
    ) -> None:
        """Add an edge to the graph."""
        source_key = str(source)
        target_key = str(target)

        # Ensure nodes exist
        if source_key not in self._graph:
            self.add_node(NodeData(node_id=source))
        if target_key not in self._graph:
            self.add_node(NodeData(node_id=target))

        # Task #106 — ROOT FIX for the 22→1 files/s cold-index decay.
        # ``len(self._graph.edges)`` (and ``number_of_edges()``) are
        # O(N_nodes) for a DiGraph: NetworkX ``OutEdgeView.__len__`` /
        # ``DiGraph.size()`` sum out-degree across EVERY node.
        # Recomputing it on every add_edge made the 29,424-file cold
        # index O(N²).  Empirically (this repo, networkx 3.6.1):
        # len(G.edges) = 9.7µs @200 nodes → 87µs @1850 → 1341µs @29k
        # (perfectly linear) — exactly the observed standalone
        # 22 files/s → 1 file/s degradation.  Fix: maintain
        # total_edges as an O(1) incremental counter.  NetworkX
        # add_edge on an existing (u, v) updates attributes WITHOUT
        # changing the edge count, so only genuinely-new edges are
        # counted.  ``has_edge`` is O(1) (dict-of-dicts lookup).
        _is_new_edge = not self._graph.has_edge(source_key, target_key)

        self._graph.add_edge(
            source_key,
            target_key,
            **edge_data.to_dict(),
        )

        if _is_new_edge:
            self._metrics["total_edges"] += 1

    def get_node(self, node_id: Union[NodeID, str]) -> Optional[Dict[str, Any]]:
        """Get node data by ID."""
        node_key = str(node_id)
        if node_key in self._graph:
            return dict(self._graph.nodes[node_key])
        return None

    def get_edges_from(self, node_id: Union[NodeID, str]) -> List[Tuple[str, Dict[str, Any]]]:
        """Get all outgoing edges from a node."""
        node_key = str(node_id)
        if node_key not in self._graph:
            return []

        return [
            (target, dict(self._graph.edges[node_key, target]))
            for target in self._graph.successors(node_key)
        ]

    def get_edges_to(self, node_id: Union[NodeID, str]) -> List[Tuple[str, Dict[str, Any]]]:
        """Get all incoming edges to a node."""
        node_key = str(node_id)
        if node_key not in self._graph:
            return []

        return [
            (source, dict(self._graph.edges[source, node_key]))
            for source in self._graph.predecessors(node_key)
        ]

    def find_nodes_by_name(self, name: str, fuzzy: bool = False) -> List[NodeID]:
        """Find nodes by name (exact or fuzzy match)."""
        results = []
        name_lower = name.lower()

        for node_key, node_id in self._node_index.items():
            if fuzzy:
                if name_lower in node_id.name.lower():
                    results.append(node_id)
            else:
                if node_id.name == name or node_id.name.endswith(f".{name}"):
                    results.append(node_id)

        return results

    def find_nodes_by_type(self, node_type: NodeType) -> List[NodeID]:
        """Find all nodes of a specific type."""
        return [
            self._node_index[key]
            for key in self._type_index[node_type]
        ]

    def find_nodes_in_file(self, file_path: str) -> List[NodeID]:
        """Find all nodes in a specific file."""
        return [
            self._node_index[key]
            for key in self._file_index[file_path]
        ]

    def get_all_nodes(self) -> List["NodeData"]:
        """Return all NodeData objects stored in the graph.

        Used by the semantic index to embed all nodes after a full index.
        """
        result: List[NodeData] = []
        for node_key, node_id in self._node_index.items():
            attrs = self._graph.nodes.get(node_key, {})
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

    def find_nodes_in_repo(self, repo: str) -> List[NodeID]:
        """Find all nodes in a specific repository."""
        return [
            self._node_index[key]
            for key in self._repo_index[repo]
        ]

    def get_callers(self, node_id: Union[NodeID, str]) -> List[NodeID]:
        """Get all nodes that call this node."""
        callers = []
        for source, edge_data in self.get_edges_to(node_id):
            if edge_data.get("edge_type") == EdgeType.CALLS.value:
                if source in self._node_index:
                    callers.append(self._node_index[source])
        return callers

    def get_callees(self, node_id: Union[NodeID, str]) -> List[NodeID]:
        """Get all nodes that this node calls."""
        callees = []
        for target, edge_data in self.get_edges_from(node_id):
            if edge_data.get("edge_type") == EdgeType.CALLS.value:
                if target in self._node_index:
                    callees.append(self._node_index[target])
        return callees

    def get_importers(self, node_id: Union[NodeID, str]) -> List[NodeID]:
        """Get all nodes that import this node."""
        importers = []
        for source, edge_data in self.get_edges_to(node_id):
            if edge_data.get("edge_type") in (EdgeType.IMPORTS.value, EdgeType.IMPORTS_FROM.value):
                if source in self._node_index:
                    importers.append(self._node_index[source])
        return importers

    def get_dependencies(self, node_id: Union[NodeID, str]) -> List[NodeID]:
        """Get all nodes that this node depends on (imports, calls, inherits)."""
        deps = []
        for target, edge_data in self.get_edges_from(node_id):
            edge_type = edge_data.get("edge_type")
            if edge_type in (
                EdgeType.IMPORTS.value,
                EdgeType.IMPORTS_FROM.value,
                EdgeType.CALLS.value,
                EdgeType.INHERITS.value,
            ):
                if target in self._node_index:
                    deps.append(self._node_index[target])
        return deps

    def get_dependents(self, node_id: Union[NodeID, str]) -> List[NodeID]:
        """Get all nodes that depend on this node."""
        dependents = []
        for source, edge_data in self.get_edges_to(node_id):
            edge_type = edge_data.get("edge_type")
            if edge_type in (
                EdgeType.IMPORTS.value,
                EdgeType.IMPORTS_FROM.value,
                EdgeType.CALLS.value,
                EdgeType.INHERITS.value,
            ):
                if source in self._node_index:
                    dependents.append(self._node_index[source])
        return dependents

    def compute_blast_radius(
        self,
        node_id: Union[NodeID, str],
        max_depth: int = OracleConfig.MAX_BLAST_RADIUS_DEPTH,
    ) -> BlastRadius:
        """
        Compute the blast radius of changing a node.

        This is THE killer feature - shows impact of changes.
        """
        node_key = str(node_id)
        if node_key not in self._graph:
            if isinstance(node_id, str):
                # Try to find by name
                found = self.find_nodes_by_name(node_id)
                if found:
                    node_id = found[0]
                    node_key = str(node_id)
                else:
                    return BlastRadius(
                        source_node=NodeID("", "", node_id, NodeType.FILE),
                        directly_affected=set(),
                        transitively_affected=set(),
                        broken_imports=[],
                        broken_calls=[],
                        risk_level="unknown",
                    )
            else:
                return BlastRadius(
                    source_node=node_id,
                    directly_affected=set(),
                    transitively_affected=set(),
                    broken_imports=[],
                    broken_calls=[],
                    risk_level="unknown",
                )

        source_node = self._node_index[node_key]
        directly_affected: Set[NodeID] = set()
        transitively_affected: Set[NodeID] = set()
        broken_imports: List[Tuple[NodeID, str]] = []
        broken_calls: List[Tuple[NodeID, str]] = []

        # BFS to find affected nodes
        visited = {node_key}
        current_level = {node_key}

        for depth in range(max_depth):
            next_level: Set[str] = set()

            for current_key in current_level:
                # Find all nodes that depend on current node
                for source, edge_data in self.get_edges_to(current_key):
                    if source not in visited:
                        visited.add(source)
                        next_level.add(source)

                        if source in self._node_index:
                            affected_node = self._node_index[source]
                            edge_type = edge_data.get("edge_type")

                            if depth == 0:
                                directly_affected.add(affected_node)
                            else:
                                transitively_affected.add(affected_node)

                            # Track specific breakages
                            if edge_type in (EdgeType.IMPORTS.value, EdgeType.IMPORTS_FROM.value):
                                broken_imports.append((affected_node, source_node.name))
                            elif edge_type == EdgeType.CALLS.value:
                                broken_calls.append((affected_node, source_node.name))

            current_level = next_level
            if not current_level:
                break

        # Calculate risk level
        total_affected = len(directly_affected) + len(transitively_affected)
        if total_affected == 0:
            risk_level = "low"
        elif total_affected <= 3:
            risk_level = "low"
        elif total_affected <= 10:
            risk_level = "medium"
        elif total_affected <= 25:
            risk_level = "high"
        else:
            risk_level = "critical"

        return BlastRadius(
            source_node=source_node,
            directly_affected=directly_affected,
            transitively_affected=transitively_affected,
            broken_imports=broken_imports,
            broken_calls=broken_calls,
            risk_level=risk_level,
        )

    def find_call_chain(
        self,
        source: Union[NodeID, str],
        target: Union[NodeID, str],
        max_depth: int = OracleConfig.MAX_CALL_CHAIN_DEPTH,
    ) -> Optional[List[NodeID]]:
        """Find the call chain from source to target."""
        source_key = str(source)
        target_key = str(target)

        if source_key not in self._graph or target_key not in self._graph:
            return None

        try:
            # Use networkx shortest path
            path = nx.shortest_path(
                self._graph,
                source_key,
                target_key,
            )
            return [self._node_index[key] for key in path if key in self._node_index]
        except nx.NetworkXNoPath:
            return None

    def find_circular_dependencies(self) -> List[List[NodeID]]:
        """Find all circular dependencies in the graph."""
        cycles = []

        try:
            for cycle in nx.simple_cycles(self._graph):
                if len(cycle) > 1:  # Ignore self-loops
                    cycle_nodes = [
                        self._node_index[key]
                        for key in cycle
                        if key in self._node_index
                    ]
                    if cycle_nodes:
                        cycles.append(cycle_nodes)
        except Exception as e:
            logger.warning(f"Error finding cycles: {e}")

        return cycles

    def find_dead_code(self) -> List[NodeID]:
        """Find potentially dead code (unreferenced functions/classes)."""
        dead_code = []

        for node_key in self._type_index[NodeType.FUNCTION] | self._type_index[NodeType.METHOD]:
            node_id = self._node_index[node_key]

            # Skip special methods
            if node_id.name.startswith("__"):
                continue

            # Check if anything calls this
            callers = self.get_callers(node_id)
            importers = self.get_importers(node_id)

            if not callers and not importers:
                # Also check if it's contained by something that's referenced
                edges_to = self.get_edges_to(node_id)
                if not any(e[1].get("edge_type") != EdgeType.CONTAINS.value for e in edges_to):
                    dead_code.append(node_id)

        return dead_code

    def get_subgraph(
        self,
        root: Union[NodeID, str],
        depth: int = 2,
        direction: str = "both",  # "in", "out", "both"
    ) -> "CodebaseKnowledgeGraph":
        """Extract a subgraph centered on a node."""
        root_key = str(root)
        if root_key not in self._graph:
            return CodebaseKnowledgeGraph()

        # Collect nodes within depth
        nodes_to_include = {root_key}
        current_level = {root_key}

        for _ in range(depth):
            next_level: Set[str] = set()

            for node_key in current_level:
                if direction in ("out", "both"):
                    next_level.update(self._graph.successors(node_key))
                if direction in ("in", "both"):
                    next_level.update(self._graph.predecessors(node_key))

            nodes_to_include.update(next_level)
            current_level = next_level

        # Create subgraph
        subgraph = CodebaseKnowledgeGraph()
        nx_subgraph = self._graph.subgraph(nodes_to_include)
        subgraph._graph = nx_subgraph.copy()

        # Rebuild indices
        for node_key in subgraph._graph.nodes:
            if node_key in self._node_index:
                node_id = self._node_index[node_key]
                subgraph._node_index[node_key] = node_id
                subgraph._file_index[node_id.file_path].add(node_key)
                subgraph._repo_index[node_id.repo].add(node_key)
                subgraph._type_index[node_id.node_type].add(node_key)

        return subgraph

    def to_dict(self) -> Dict[str, Any]:
        """Serialize graph to dictionary."""
        return {
            "nodes": [
                {
                    "key": node_key,
                    "data": dict(self._graph.nodes[node_key]),
                }
                for node_key in self._graph.nodes
            ],
            "edges": [
                {
                    "source": source,
                    "target": target,
                    "data": dict(self._graph.edges[source, target]),
                }
                for source, target in self._graph.edges
            ],
            "metrics": self._metrics,
        }

    def clear(self) -> None:
        """Clear the graph."""
        self._graph.clear()
        self._node_index.clear()
        self._file_index.clear()
        self._repo_index.clear()
        self._type_index.clear()
        self._metrics = {
            "total_nodes": 0,
            "total_edges": 0,
            "files_indexed": 0,
            "last_full_index": 0.0,
            "last_incremental_update": 0.0,
        }


# =============================================================================
# ORACLE SEMANTIC INDEX — ChromaDB + SentenceTransformer
# =============================================================================

class OracleSemanticBackendStatus(str, enum.Enum):
    """Closed-taxonomy status of the Oracle's semantic backend.

    Slice 10 — operator-bound telemetry contract:
    ``oracle_semantic_backend={chroma|stdlib|disabled|degraded}``.

    The closed 5-value taxonomy + the AST pin in
    ``tests/governance/test_slice10_oracle_semantic_isolation.py``
    guarantees that downstream consumers can route deterministically
    on the status. Adding a 6th value requires bumping the pin +
    every readback caller."""

    PENDING   = "pending"     # constructed; ChromaDB not yet attempted
    CHROMA    = "chroma"      # ChromaDB loaded + ready
    STDLIB    = "stdlib"      # fallback (reserved — Slice 11 wires)
    DISABLED  = "disabled"    # operator opt-out via env
    DEGRADED  = "degraded"    # init failed / timed out — queries return empty


# Backwards-compat alias for any near-term caller that migrates
# incrementally to the qualified enum name.
BackendStatus = OracleSemanticBackendStatus


def _stdlib_backend_available() -> bool:
    """Slice 155 — True if the STDLIB in-memory vector backend may take over when
    chromadb is unavailable. Gated ``JARVIS_ORACLE_STDLIB_BACKEND_ENABLED``
    (default TRUE — failure-path-only graceful degradation, can't affect the CHROMA
    happy path; set =0 to force DEGRADED). Requires numpy (the cosine substrate).
    NEVER raises."""
    try:
        if os.getenv("JARVIS_ORACLE_STDLIB_BACKEND_ENABLED", "true").strip().lower() \
                not in ("1", "true", "yes", "on"):
            return False
        import importlib.util
        return importlib.util.find_spec("numpy") is not None
    except Exception:  # noqa: BLE001
        return False


class OracleSemanticIndex:
    """Manages ChromaDB embeddings for code symbols (functions, methods, classes).

    Embedded text per node: ``"{name} {signature} {docstring}"`` (truncated to 512 chars).
    Indexed node types: CLASS, FUNCTION, METHOD only.

    **Fault isolation guarantee:** ``__init__`` never raises. All public methods
    return empty results silently when the backend is not in ``CHROMA`` status.

    Slice 10 — Oracle semantic native-runtime isolation (operator-bound,
    empirical from bt-2026-05-22-010120):

      ChromaDB's Rust extension (``chromadb_rust_bindings``) spawns tokio
      worker threads that compete with the main Python asyncio thread for
      the GIL. The pre-Slice-10 constructor loaded ChromaDB synchronously
      during Oracle boot, which starved the asyncio loop for minutes (the
      sample showed the main thread blocked at
      ``PyEval_RestoreThread → _pthread_cond_wait`` while ~6
      tokio-runtime-workers ran in ``chromadb_rust_bindings.abi3.so``).

    Slice 10 fix:
      * ``__init__`` is LIGHTWEIGHT — config stash only, NO chromadb import.
      * ``initialize_backend()`` runs the ChromaDB + embedder load in
        ``loop.run_in_executor`` under ``asyncio.wait_for`` with bounded
        timeout (env: ``JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S``, default 30s).
      * Backend status is the closed-taxonomy
        ``OracleSemanticBackendStatus`` enum.
      * Boot path NEVER touches chromadb. Oracle.initialize() returns
        immediately after constructing this object; ChromaDB loads
        lazily on the first semantic query or via explicit
        ``initialize_backend()`` call.

    AST pin: the SOLE ``import chromadb`` site in this module is
    ``_load_chromadb_sync`` (the executor-thread loader).
    """

    # Node types worth embedding — others carry no useful semantic content
    _EMBEDDABLE_TYPES = {NodeType.CLASS, NodeType.FUNCTION, NodeType.METHOD}
    # Max chars fed to the embedding model per node
    _MAX_EMBED_CHARS: int = 512

    # Slice 10 — env knobs (operational; closed taxonomy is structural)
    _ENV_BACKEND: str = "JARVIS_ORACLE_SEMANTIC_BACKEND"
    _ENV_INIT_TIMEOUT_S: str = "JARVIS_ORACLE_SEMANTIC_BACKEND_TIMEOUT_S"
    _DEFAULT_INIT_TIMEOUT_S: float = 30.0
    _MIN_INIT_TIMEOUT_S: float = 1.0
    _MAX_INIT_TIMEOUT_S: float = 300.0

    def __init__(
        self,
        persist_dir: Optional[Path] = None,
        collection_name: Optional[str] = None,
    ) -> None:
        # Slice 10 — LIGHTWEIGHT constructor. NO chromadb import,
        # NO embedder load, NO disk writes (persist_dir.mkdir is
        # deferred to _load_chromadb_sync). Boot path is bounded
        # by trivial config-stash time.
        #
        # AST pin (Slice 10): the body of __init__ MUST NOT
        # contain ``import chromadb`` or any chromadb attribute
        # access. The executor-isolated ``_load_chromadb_sync`` is
        # the SOLE permitted import site in this module.
        self._available: bool = False  # legacy property; True iff CHROMA
        self._collection: Optional[Any] = None
        self._embedder: Optional[Any] = None
        self._persist_dir = persist_dir or OracleConfig.CHROMA_PERSIST_DIR
        self._collection_name = collection_name or OracleConfig.CHROMA_COLLECTION_NAME
        self._status: OracleSemanticBackendStatus = (
            OracleSemanticBackendStatus.PENDING
        )
        # One-shot init guard. Lock constructed lazily inside the
        # async path (we may be on the main thread at __init__ time
        # but not yet inside an event loop).
        self._init_lock: Optional[asyncio.Lock] = None
        self._init_attempted: bool = False
        # Slice 155 — STDLIB in-memory vector backend (numpy cosine), used when
        # chromadb is unavailable so semantic search still works (non-persistent)
        # instead of DEGRADED-empty. id -> (embedding floats, metadata). NO chromadb
        # import here (AST pin honored) — pure numpy.
        self._stdlib_store: Dict[str, Tuple[List[float], Dict[str, Any]]] = {}

    # ---- Slice 10 status surface ----

    @property
    def backend_status(self) -> "OracleSemanticBackendStatus":
        """Closed-taxonomy status. Operator readback API."""
        return self._status

    @property
    def backend_status_value(self) -> str:
        """String form for the telemetry-bound log line
        ``oracle_semantic_backend=<value>``."""
        return self._status.value

    # ---- Backend resolution + bounded executor init ----

    @classmethod
    def _resolve_backend_choice(cls) -> str:
        """Returns the configured backend preference. Operator
        opt-out via ``JARVIS_ORACLE_SEMANTIC_BACKEND=disabled``."""
        return os.environ.get(
            cls._ENV_BACKEND, "chroma",
        ).strip().lower()

    @classmethod
    def _resolve_init_timeout_s(cls) -> float:
        """Clamped init timeout for the executor-isolated load."""
        try:
            raw = os.environ.get(cls._ENV_INIT_TIMEOUT_S, "").strip()
            if not raw:
                return cls._DEFAULT_INIT_TIMEOUT_S
            v = float(raw)
        except (TypeError, ValueError):
            return cls._DEFAULT_INIT_TIMEOUT_S
        return max(
            cls._MIN_INIT_TIMEOUT_S,
            min(cls._MAX_INIT_TIMEOUT_S, v),
        )

    async def initialize_backend(self) -> "OracleSemanticBackendStatus":
        """Slice 10 — async one-shot bounded executor init.

        Loads ChromaDB + embedder in ``loop.run_in_executor`` under
        ``asyncio.wait_for`` so the main asyncio loop continues
        ticking even when the chromadb Rust workers misbehave.

        On timeout / exception → ``status=DEGRADED``. Queries
        return empty without raising. Idempotent — subsequent calls
        return the cached status."""
        # Slice 33 Arc 1+ widening — ChromaDB lazy init is a known
        # bootstrap-phase heavyweight; instrument so we know exactly
        # how much loop time it costs even with the executor wrapper.
        from backend.core.ouroboros.telemetry.loop_sink import (
            sink_async as _ls_sink_async,
        )
        async with _ls_sink_async(
            "oracle.OracleSemanticIndex.initialize_backend",
        ):
            return await self._initialize_backend_impl()

    async def _initialize_backend_impl(self) -> "OracleSemanticBackendStatus":
        if self._init_attempted:
            return self._status
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._init_attempted:
                return self._status
            choice = self._resolve_backend_choice()
            if choice == "disabled":
                self._status = OracleSemanticBackendStatus.DISABLED
                self._init_attempted = True
                logger.info(
                    "[OracleSemanticIndex] backend=disabled "
                    "(JARVIS_ORACLE_SEMANTIC_BACKEND=disabled) — "
                    "semantic queries return empty; graph "
                    "readiness is unaffected"
                )
                return self._status
            timeout_s = self._resolve_init_timeout_s()
            t0 = time.monotonic()
            try:
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None, self._load_chromadb_sync,
                    ),
                    timeout=timeout_s,
                )
                self._status = OracleSemanticBackendStatus.CHROMA
                self._available = True  # legacy compat
                elapsed = time.monotonic() - t0
                logger.info(
                    "[OracleSemanticIndex] backend=chroma READY "
                    "(lazy + executor-isolated, elapsed=%.2fs) — "
                    "collection '%s' at %s",
                    elapsed, self._collection_name, self._persist_dir,
                )
            except asyncio.TimeoutError:
                self._status = OracleSemanticBackendStatus.DEGRADED
                logger.warning(
                    "[OracleSemanticIndex] backend=degraded — init "
                    "exceeded %.1fs timeout; queries return empty; "
                    "graph readiness is unaffected",
                    timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 — fault isolation
                # Slice 155 — chromadb unavailable (e.g. ModuleNotFoundError in an
                # Oracle-capable image without the vector store). Fall back to the
                # STDLIB in-memory numpy backend so semantic search still WORKS
                # (non-persistent) instead of DEGRADED-empty. Last resort = DEGRADED.
                if _stdlib_backend_available():
                    self._status = OracleSemanticBackendStatus.STDLIB
                    self._available = True
                    logger.warning(
                        "[OracleSemanticIndex] backend=stdlib (in-memory numpy) — "
                        "chromadb unavailable (%s: %s); semantic search works "
                        "in-memory (non-persistent); graph readiness is unaffected",
                        type(exc).__name__, exc,
                    )
                else:
                    self._status = OracleSemanticBackendStatus.DEGRADED
                    logger.warning(
                        "[OracleSemanticIndex] backend=degraded — init "
                        "raised %s: %s; queries return empty; graph "
                        "readiness is unaffected",
                        type(exc).__name__, exc,
                    )
            self._init_attempted = True
            return self._status

    def _load_chromadb_sync(self) -> None:
        """SYNCHRONOUS executor-thread loader. The SOLE permitted
        ``import chromadb`` site in this module (AST-pinned).

        Runs in a thread pool worker via ``loop.run_in_executor``.
        Heavy C-extension allocation + Rust worker spawn happens
        here, NOT on the main asyncio thread. The main loop ticks
        under the surrounding ``asyncio.wait_for``."""
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
        # Embedder construction via the centralized EmbeddingService
        # singleton — prevents multiple SentenceTransformer instances
        # from spawning competing PyTorch/BLAS thread pools (the
        # libmalloc heap corruption on macOS ARM64 documented above
        # the original construction).
        from backend.core.embedding_service import EmbeddingService

        class _SharedEmbedder:
            """Adapter: wraps centralized EmbeddingService for Oracle."""

            def __init__(self) -> None:
                self._service = EmbeddingService()  # singleton — no model load yet

            async def embed(self, text: str) -> Any:
                result = await self._service.encode(
                    text, normalize=True,
                )
                if result is not None and len(result) > 0:
                    return result[0]
                return None

            async def embed_batch(self, texts: List[str]) -> List[Any]:
                result = await self._service.encode(
                    texts, normalize=True,
                )
                return list(result) if result is not None else []

        self._embedder = _SharedEmbedder()

    async def _ensure_initialized(self) -> None:
        """Internal — called by every async query method. No-op
        when already settled."""
        if self._init_attempted:
            return
        await self.initialize_backend()

    def is_ready(self) -> bool:
        """Slice 10 — returns True iff the backend has settled to a
        terminal status (queries won't hang). Equivalent to legacy
        ``self._available`` only when status is ``CHROMA``; under
        ``DEGRADED`` / ``DISABLED`` / ``STDLIB`` we ALSO return True
        so consumers don't hang — they'll get empty results, not
        wait."""
        return self._status != OracleSemanticBackendStatus.PENDING

    def _build_embed_text(self, node: "NodeData") -> Optional[str]:
        """Build the text to embed for a node. Returns None if nothing to embed."""
        parts: List[str] = [node.node_id.name]
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

        Slice 10 — calls ``_ensure_initialized()`` so the first
        semantic operation triggers the bounded executor load.
        Subsequent calls hit the cached status with no I/O.

        Silently skips nodes with no embeddable content.
        Silently returns if not available (DEGRADED / DISABLED /
        STDLIB statuses all yield no-op).
        Never raises.
        """
        await self._ensure_initialized()
        # Slice 155 — CHROMA (persistent) OR STDLIB (in-memory) both embed.
        _chroma = self._status == OracleSemanticBackendStatus.CHROMA
        _stdlib = self._status == OracleSemanticBackendStatus.STDLIB
        if (not (_chroma or _stdlib)) or self._embedder is None:
            return
        if _chroma and self._collection is None:
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
                metadatas: List[Dict[str, Any]] = [
                    {
                        "repo": n.node_id.repo,
                        "file_path": n.node_id.file_path,
                        "name": n.node_id.name,
                        "node_type": n.node_id.node_type.value,
                    }
                    for n in batch
                ]

                if _stdlib:
                    for _eid, _emb, _meta in zip(ids, embeddings, metadatas):
                        _vec = _emb.tolist() if hasattr(_emb, "tolist") else list(_emb)
                        self._stdlib_store[_eid] = ([float(x) for x in _vec], _meta)
                else:
                    self._collection.upsert(
                        ids=ids,
                        embeddings=[e.tolist() for e in embeddings],
                        metadatas=metadatas,
                    )

            logger.debug("[OracleSemanticIndex] Embedded %d nodes", len(embeddable))
        except Exception as exc:
            logger.warning("[OracleSemanticIndex] embed_nodes failed: %s", exc)

    async def semantic_search(
        self, query: str, k: int = 5
    ) -> List[Tuple[str, float]]:
        """Search for semantically similar code symbols.

        Slice 10 — first semantic query triggers the bounded
        executor load via ``_ensure_initialized()``. The boot
        path stays untouched.

        Returns ``("repo:file_path", similarity_score)`` tuples sorted by
        similarity descending.  Deduplicates to unique file paths.
        Returns empty list if not available (DEGRADED / DISABLED /
        STDLIB) or on any error.
        """
        await self._ensure_initialized()
        # Slice 155 — CHROMA (persistent) OR STDLIB (in-memory) both search.
        _chroma = self._status == OracleSemanticBackendStatus.CHROMA
        _stdlib = self._status == OracleSemanticBackendStatus.STDLIB
        if (not (_chroma or _stdlib)) or self._embedder is None:
            return []
        if _chroma and self._collection is None:
            return []

        try:
            query_embedding = await self._embedder.embed(query)
            if query_embedding is None:
                # Embedder returned no vector (empty query, model not ready,
                # upstream encode() returned empty). This is a known "couldn't
                # embed" state, not an error — return empty to match the
                # "not available" fast-path above instead of raising into
                # the outer except (which would log a misleading WARNING).
                return []

            if _stdlib:
                return self._semantic_search_stdlib(query_embedding, k)

            results = self._collection.query(
                query_embeddings=[query_embedding.tolist()],
                n_results=min(k * 4, 100),  # over-fetch to allow file-level dedup
                include=["metadatas", "distances"],
            )

            distances: List[float] = results.get("distances", [[]])[0]
            metadatas: List[Dict[str, Any]] = results.get("metadatas", [[]])[0]

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

    def _semantic_search_stdlib(
        self, query_embedding: Any, k: int = 5,
    ) -> List[Tuple[str, float]]:
        """Slice 155 — in-memory cosine search over the STDLIB store (no chromadb).
        Mirrors the CHROMA path's file-level dedup + top-k contract. NEVER raises."""
        try:
            import numpy as np
            q = np.asarray(query_embedding, dtype="float32")
            qn = float(np.linalg.norm(q)) or 1.0
            best: Dict[str, float] = {}
            for _id, (emb, meta) in self._stdlib_store.items():
                v = np.asarray(emb, dtype="float32")
                vn = float(np.linalg.norm(v)) or 1.0
                sim = max(0.0, min(1.0, float(np.dot(q, v) / (qn * vn))))
                file_key = f"{meta['repo']}:{meta['file_path']}"
                if file_key not in best or sim > best[file_key]:
                    best[file_key] = sim
            return sorted(best.items(), key=lambda x: x[1], reverse=True)[:k]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[OracleSemanticIndex] stdlib search failed: %s", exc)
            return []


# =============================================================================
# Teardown-coherence — Oracle shutdown deadline knob
# =============================================================================
#
# bt-2026-05-25-020602 wedged in ``Shutting down The Oracle...`` because a
# 1.1GB ``codebase_graph.pkl`` serialization held the Python GIL + the
# process I/O slot in uninterruptible kernel state past the
# ``BoundedShutdownWatchdog`` 30s window. With every Python thread starved
# (incl. the watchdog daemon thread), ``os._exit(75)`` could not be
# scheduled — the only OS-level escape from uninterruptible I/O is the I/O
# completing or kernel timeout. Preventive fix: bound the cache save.
#
# Defaults to 5s — enough for graphs up to ~50K nodes on local SSD with
# room for jitter; abandon on bigger payloads (cache rebuilds on next
# boot from index; abandoned save is slower start, NOT correctness loss).

_ORACLE_SHUTDOWN_DEADLINE_ENV = "JARVIS_ORACLE_SHUTDOWN_DEADLINE_S"
_ORACLE_SHUTDOWN_DEADLINE_DEFAULT_S = 5.0


def _oracle_shutdown_deadline_s() -> float:
    """``JARVIS_ORACLE_SHUTDOWN_DEADLINE_S`` — bound on shutdown cache save.

    Default 5s. Set to ``0`` to skip ``_save_cache`` entirely on shutdown.
    Set higher than 5s only if the graph genuinely needs more time AND
    the deployment can tolerate longer teardown — the
    ``BoundedShutdownWatchdog`` default deadline is 30s, so values above
    ~25s leave little margin for other teardown steps.
    """
    raw = os.environ.get(
        _ORACLE_SHUTDOWN_DEADLINE_ENV,
        str(_ORACLE_SHUTDOWN_DEADLINE_DEFAULT_S),
    )
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _ORACLE_SHUTDOWN_DEADLINE_DEFAULT_S


_ORACLE_POOL_TEARDOWN_DEADLINE_ENV = "JARVIS_ORACLE_POOL_TEARDOWN_DEADLINE_S"
_ORACLE_POOL_TEARDOWN_DEADLINE_DEFAULT_S = 5.0


def _oracle_backpressure_enabled() -> bool:
    """``JARVIS_ORACLE_BACKPRESSURE_ENABLED`` (default true) — AIMD throttle on the
    index batch loop so cold indexing yields to the FSM control plane instead of
    starving it (the ControlPlaneStarvation cold-boot wedge). Kill switch ``=0``
    restores the fixed-batch firehose."""
    raw = os.environ.get("JARVIS_ORACLE_BACKPRESSURE_ENABLED", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _oracle_backpressure_lag_ms() -> float:
    """``JARVIS_ORACLE_BACKPRESSURE_LAG_MS`` — event-loop lag (ms) above which the
    index throttles its concurrency. Default 50ms (control-plane comfort band)."""
    try:
        return max(1.0, float(os.environ.get("JARVIS_ORACLE_BACKPRESSURE_LAG_MS", "50")))
    except (TypeError, ValueError):
        return 50.0


def _oracle_backpressure_min_batch() -> int:
    """``JARVIS_ORACLE_BACKPRESSURE_MIN_BATCH`` — floor for the throttled batch so
    progress never fully stalls. Default 4."""
    try:
        return max(1, int(os.environ.get("JARVIS_ORACLE_BACKPRESSURE_MIN_BATCH", "4")))
    except (TypeError, ValueError):
        return 4


def _oracle_memory_armor_enabled() -> bool:
    """``JARVIS_ORACLE_MEMORY_ARMOR_ENABLED`` (default true) — second AIMD axis: fold host
    memory pressure into the index throttle so the cold build defends a constrained RAM boundary
    (16GB host). No-op unless memory actually elevates; the kill switch ``=0`` disables it."""
    raw = os.environ.get("JARVIS_ORACLE_MEMORY_ARMOR_ENABLED", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _oracle_memory_armor_max_yields() -> int:
    """``JARVIS_ORACLE_MEMORY_ARMOR_MAX_YIELDS`` — at CRITICAL pressure the index forces a
    GC + loop-yield this many times waiting for pressure to clear before SUSPENDING the build
    (durable — every SQLite commit is a checkpoint, so it resumes next boot). Default 3."""
    try:
        return max(1, int(os.environ.get("JARVIS_ORACLE_MEMORY_ARMOR_MAX_YIELDS", "3")))
    except (TypeError, ValueError):
        return 3


def _oracle_memory_armor_yield_s() -> float:
    """``JARVIS_ORACLE_MEMORY_ARMOR_YIELD_S`` — seconds to yield to the GC/allocator per attempt
    when CRITICAL. Default 0.5s."""
    try:
        return max(0.05, float(os.environ.get("JARVIS_ORACLE_MEMORY_ARMOR_YIELD_S", "0.5")))
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------------------- Adaptive Local Subtree Scoper
def _oracle_scoper_enabled() -> bool:
    """``JARVIS_ORACLE_ADAPTIVE_SCOPER_ENABLED`` (default false) — partition the cold index into
    decoupled package subtrees and traverse them sequentially with between-subtree RAM reclaim, so a
    full 29k-file brain BUILDS on a constrained host without ever holding the whole graph resident.
    OFF → single un-partitioned pass (byte-identical to the pre-scoper path)."""
    raw = os.environ.get("JARVIS_ORACLE_ADAPTIVE_SCOPER_ENABLED", "false")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _oracle_scoper_safety_frac() -> float:
    """``JARVIS_ORACLE_SCOPER_SAFETY_FRAC`` — fraction of *available* host RAM to budget per
    partition (no hardcoded file cap; the cap is derived live from this × free RAM). Default 0.30."""
    try:
        return min(0.9, max(0.05, float(os.environ.get("JARVIS_ORACLE_SCOPER_SAFETY_FRAC", "0.30"))))
    except (TypeError, ValueError):
        return 0.30


def _oracle_scoper_per_node_kb() -> float:
    """``JARVIS_ORACLE_SCOPER_PER_NODE_KB`` — empirical resident cost per graph node (soak-measured
    slope ≈ 2.34 MB / 1000 nodes). Used only to convert a RAM budget into a node/file target."""
    try:
        return max(0.1, float(os.environ.get("JARVIS_ORACLE_SCOPER_PER_NODE_KB", "2.4")))
    except (TypeError, ValueError):
        return 2.4


def _oracle_scoper_nodes_per_file() -> float:
    """``JARVIS_ORACLE_SCOPER_NODES_PER_FILE`` — empirical nodes produced per source file
    (soak-measured ≈ 68). Converts the node target into a file target for partition sizing."""
    try:
        return max(1.0, float(os.environ.get("JARVIS_ORACLE_SCOPER_NODES_PER_FILE", "68")))
    except (TypeError, ValueError):
        return 68.0


def _oracle_scoper_min_partition_files() -> int:
    """``JARVIS_ORACLE_SCOPER_MIN_PARTITION_FILES`` — floor on partition size so a tiny free-RAM
    reading can't fragment the index into thousands of micro-partitions. Default 200."""
    try:
        return max(1, int(os.environ.get("JARVIS_ORACLE_SCOPER_MIN_PARTITION_FILES", "200")))
    except (TypeError, ValueError):
        return 200


def _cluster_by_package(files: "List[Path]", repo_path: Path, target_files: int) -> "List[List[Path]]":
    """Cluster files into package-aligned partitions each ≤ ``target_files`` (Phase 1 — pure, no I/O,
    testable). Logical-boundary analysis: group by directory (package) depth, recursively splitting a
    package that alone exceeds the target into its sub-packages; then first-fit-decreasing bin-pack
    the resulting groups so intra-package edges stay within a partition and only cross-package edges
    become the (durable, by-key) inter-partition stubs."""
    def _rel_parts(f: Path):
        try:
            return f.relative_to(repo_path).parts
        except ValueError:
            return (f.name,)

    def _group(file_list, depth):
        buckets: "Dict[str, List[Path]]" = {}
        for f in file_list:
            parts = _rel_parts(f)
            key = "/".join(parts[:depth]) if len(parts) > depth else "/".join(parts[:-1]) or "."
            buckets.setdefault(key, []).append(f)
        out: "List[List[Path]]" = []
        for grp in buckets.values():
            # Recurse only if the group is over target AND there is deeper structure to split on.
            if len(grp) > target_files and any(len(_rel_parts(f)) > depth + 1 for f in grp):
                out.extend(_group(grp, depth + 1))
            else:
                out.append(grp)
        return out

    groups = _group(files, 1)
    groups.sort(key=len, reverse=True)  # first-fit-decreasing
    partitions: "List[List[Path]]" = []
    for grp in groups:
        placed = False
        for p in partitions:
            if len(p) + len(grp) <= target_files:
                p.extend(grp)
                placed = True
                break
        if not placed:
            partitions.append(list(grp))
    return partitions


class _AdaptiveIndexThrottle:
    """AIMD backpressure for the Oracle index batch loop.

    Multiplicative-DECREASE the batch size (= per-cycle ProcessPool concurrency) when
    measured event-loop lag exceeds the threshold; additive-INCREASE back toward the
    ceiling when the loop is responsive. The classic congestion-control shape:
    background indexing gracefully yields cores to the primary FSM, then reclaims them
    once the control plane has breathing room. Pure decision logic — no I/O, testable.
    """

    def __init__(self, *, max_batch: int, min_batch: int = 4, lag_threshold_ms: float = 50.0):
        self.max_batch = max(1, int(max_batch))
        self.min_batch = max(1, min(int(min_batch), self.max_batch))
        self.lag_threshold_ms = max(1.0, float(lag_threshold_ms))
        self.batch = self.max_batch
        self._increment = max(1, self.max_batch // 8)

    def update(self, lag_ms: float) -> int:
        """Fold one lag observation into the batch size; return the new size."""
        if lag_ms > self.lag_threshold_ms:
            self.batch = max(self.min_batch, self.batch // 2)               # MD
        else:
            self.batch = min(self.max_batch, self.batch + self._increment)  # AI
        return self.batch

    def backoff_s(self, lag_ms: float) -> float:
        """Seconds to yield the loop when lagging — proportional to the overshoot,
        capped so one bad reading can't park indexing for long."""
        if lag_ms <= self.lag_threshold_ms:
            return 0.0
        return min(0.5, (lag_ms - self.lag_threshold_ms) / 1000.0)


def _oracle_pool_teardown_deadline_s() -> float:
    """``JARVIS_ORACLE_POOL_TEARDOWN_DEADLINE_S`` — bound on the AST process-pool
    teardown at shutdown. Default 5s: workers get this long to finish in-flight
    parses gracefully before they are force-terminated. Set ``0`` to skip the
    graceful drain and force-terminate immediately. Keep under the
    BoundedShutdownWatchdog 30s budget (shared with the cache-save deadline)."""
    raw = os.environ.get(
        _ORACLE_POOL_TEARDOWN_DEADLINE_ENV,
        str(_ORACLE_POOL_TEARDOWN_DEADLINE_DEFAULT_S),
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _ORACLE_POOL_TEARDOWN_DEADLINE_DEFAULT_S


# =============================================================================
# THE ORACLE - MAIN INDEXER
# =============================================================================

class TheOracle:
    """
    The Oracle - Omniscient Codebase Knowledge System.

    This is what transforms JARVIS from a "text processor" into a
    "structural analyst" that understands code connections.
    """

    def __init__(self):
        self._graph = CodebaseKnowledgeGraph()
        self._file_hashes: Dict[str, str] = {}  # file_path -> content hash
        self._lock = asyncio.Lock()
        self._running = False
        self._shutting_down = False  # cancellation token: stops the indexer mid-build on shutdown
        self._last_indexed_monotonic_ns: int = 0  # set after each full index build

        # Repository configurations
        self._repos: Dict[str, Path] = {
            "jarvis": OracleConfig.JARVIS_PATH,
            "prime": OracleConfig.JARVIS_PRIME_PATH,
            "reactor": OracleConfig.REACTOR_CORE_PATH,
        }

        # Ensure cache directory exists
        OracleConfig.ORACLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Semantic index — DEFERRED until after graph loading to prevent
        # concurrent C-extension heap allocation (ChromaDB + cache
        # deserialization). This avoids libmalloc memory corruption on
        # macOS ARM64.
        self._semantic_index: "OracleSemanticIndex" = None  # type: ignore[assignment]

        # Granular readiness — composed primitive. Lets the harness
        # spawn ``initialize()`` as a background task while consumers
        # gate on first-class graph/semantic events instead of polling
        # ``_running`` (which is a single coarse flag).
        from backend.core.ouroboros.oracle_readiness import OracleReadiness
        self._readiness = OracleReadiness()

        # Slice 33 Arc 2 Phase 3 — async graph-write queue substrate.
        # When the master flag JARVIS_ORACLE_GRAPH_QUEUE_ENABLED is
        # enabled (default TRUE), _index_file enqueues
        # (nodes, edges, cache_key, content_hash) batches and the
        # consumer task drains+applies them via asyncio.to_thread —
        # the NetworkX bulk mutations happen on a worker thread
        # instead of the asyncio main loop. Closes v28 LoopSink
        # graph_write_bulk pressure (76 occurrences, peak 3,580 ms).
        self._graph_write_queue: Optional[asyncio.Queue] = None
        self._graph_write_consumer_task: Optional[asyncio.Task] = None
        self._graph_write_consumer_started: bool = False
        self._graph_write_consumer_stop_event: Optional[asyncio.Event] = None
        self._graph_writes_enqueued: int = 0
        self._graph_writes_applied: int = 0
        self._graph_writes_dropped: int = 0

        # Phase 2 — storage-agnostic persistence provider (lazy; built on first cache touch so
        # env flips after import still apply). ``None`` == legacy pickle path (byte-identical).
        self._persistence: Any = None
        self._persistence_built: bool = False

        # Sovereign Memory Armor — observability counters (the index throttle's memory axis).
        self._memory_gate_ref: Any = None
        self._mem_armor_contractions: int = 0   # batches contracted under WARN/HIGH/CRITICAL
        self._mem_armor_yields: int = 0          # GC+sleep yields performed at CRITICAL
        self._mem_armor_suspended: bool = False  # index suspended because CRITICAL never cleared
        self._scoper_partitions: int = 0          # subtree partitions the last index ran over
        self._scoper_evictions: int = 0           # between-subtree RAM-reclaim evictions performed

        logger.info("The Oracle initialized")

    # ------------------------------------------------------------------
    # Readiness — public delegates to the composed primitive.
    # ------------------------------------------------------------------

    @property
    def readiness(self):
        """Return the composed :class:`OracleReadiness`. Consumers
        gate on this primitive when the harness defers init to the
        background — no silent half-graph answers."""
        return self._readiness

    def is_graph_ready(self) -> bool:
        """Sync probe — codebase graph is loaded (cache or full index)."""
        from backend.core.ouroboros.oracle_readiness import OracleReadinessScope
        return self._readiness.is_ready(OracleReadinessScope.GRAPH)

    def is_semantic_ready(self) -> bool:
        """Sync probe — Chroma-backed semantic index is initialized."""
        from backend.core.ouroboros.oracle_readiness import OracleReadinessScope
        return self._readiness.is_ready(OracleReadinessScope.SEMANTIC)

    def is_fully_ready(self) -> bool:
        """Sync probe — both graph and semantic index ready."""
        from backend.core.ouroboros.oracle_readiness import OracleReadinessScope
        return self._readiness.is_ready(OracleReadinessScope.FULL)

    async def wait_until_ready(
        self, scope: str = "full", *, timeout: Optional[float] = None,
    ) -> None:
        """Async wait — block the caller until the requested scope is
        ready. ``scope`` is one of ``"graph"``, ``"semantic"``, ``"full"``.
        Raises :class:`OracleInitFailed` if init failed."""
        from backend.core.ouroboros.oracle_readiness import OracleReadinessScope
        try:
            scope_enum = OracleReadinessScope(scope)
        except ValueError:
            scope_enum = OracleReadinessScope.FULL
        await self._readiness.wait_until_ready(scope_enum, timeout=timeout)

    async def initialize(self) -> bool:
        """Initialize the Oracle, loading cached data if available.

        **Ordering is intentional** — the graph cache loads BEFORE the
        semantic index is constructed to prevent concurrent C-extension
        heap allocation (ChromaDB + cache deserialization) which causes
        libmalloc memory corruption on macOS ARM64. Sub-phases are
        wrapped with :class:`_OraclePhase` for boot-timing visibility
        but the original sequential ordering is preserved verbatim.

        **Readiness signaling** — after each phase boundary the
        composed :class:`OracleReadiness` primitive is signaled so
        consumers awaiting graph-only or semantic-only readiness
        unblock at the earliest correct moment. On exception, the
        failure is recorded so all pending waiters surface a clear
        error instead of hanging.
        """
        logger.info("Initializing The Oracle...")
        _t_start = time.monotonic()
        _cache_loaded = False
        try:
            # Phase 1 — graph cache load (synchronous to avoid libmalloc crash)
            with _OraclePhase("oracle_load_cache"):
                _cache_loaded = await self._load_cache()
            if _cache_loaded:
                logger.info(f"Loaded cached graph: {self._graph._metrics['total_nodes']} nodes, "
                           f"{self._graph._metrics['total_edges']} edges")
            else:
                # Phase 2 — no cache, do full index (only on cache miss).
                #
                # Falsification B1 gate (operator-bound 2026-05-14):
                # ``JARVIS_GOVERNED_ORACLE_INDEXER_ENABLED=false`` ALSO
                # skips this boot-time 29k-file cold index — the SAME
                # single knob that suppresses GovernedLoopService's
                # periodic ``_oracle_index_loop``.  Run A proved the
                # GLS-loop gate alone was insufficient: the harness
                # constructs its OWN ``TheOracle()`` whose
                # ``initialize()`` → ``full_index()`` is the dominant
                # event-loop offender and was NOT covered.  With this,
                # B1 maps to the exact + complete code path (operator
                # binding: "B1 must map to the exact code path").  The
                # graph stays empty; readiness still resolves below so
                # consumers degrade gracefully instead of hanging.
                _b1_indexer_enabled = os.environ.get(
                    "JARVIS_GOVERNED_ORACLE_INDEXER_ENABLED", "true",
                ).strip().lower() != "false"
                if not _b1_indexer_enabled:
                    logger.warning(
                        "[Oracle.boot] full_index SKIPPED "
                        "(JARVIS_GOVERNED_ORACLE_INDEXER_ENABLED=false) "
                        "— falsification B1: graph stays empty, "
                        "readiness still resolves; consumers degrade "
                        "gracefully.  This is a diagnostic gate, NOT a "
                        "production default."
                    )
                else:
                    logger.info("No cache found, performing full index...")
                    with _OraclePhase("oracle_full_index"):
                        await self.full_index()

            # Graph is ready — emit the granular signal so any consumer
            # gated on GRAPH-scope readiness (blast-radius, dependency
            # traversal) unblocks immediately rather than waiting for
            # the still-pending semantic index.
            self._readiness.mark_graph_ready()

            # libmalloc-safe ordering invariant (macOS ARM64): the ChromaDB /
            # torch / embedder backend is initialized ONLY AFTER graph loading
            # has completed (and its readiness signaled above). Loading the
            # graph cache and spinning up ChromaDB's C-extension heap
            # concurrently triggered libmalloc corruption on macOS ARM64 — so
            # graph load and semantic-backend init must never overlap. This
            # ordering is preserved by the synchronous graph load + the
            # mark_graph_ready() gate here. (Slice 112 additionally isolates
            # the whole Oracle into its own process, so the graph load never
            # touches the engine's event loop at all.)
            # Phase 3 — Slice 10: Construct the OracleSemanticIndex
            # WITHOUT loading ChromaDB. The constructor is now
            # lightweight (config stash only); the actual ChromaDB
            # PersistentClient + tokio Rust workers + embedder load
            # happens lazily on the first semantic query via
            # ``await self._semantic_index.initialize_backend()``
            # (or ``await self._semantic_index._ensure_initialized()``
            # from any query method). The graph→semantic ordering
            # invariant is preserved at the readiness-signaling
            # boundary, but the boot path no longer pays for the
            # chromadb_rust_bindings tokio-runtime-worker GIL
            # contention (the empirical bt-2026-05-22-010120 wedge
            # source). Net effect: Oracle boot completes in
            # milliseconds for the semantic phase; first query pays
            # the bounded executor-isolated init cost (default 30s
            # cap, env-knobbed) and falls through to DEGRADED on
            # timeout without hanging the asyncio loop.
            if self._semantic_index is None:
                with _OraclePhase("oracle_semantic_index_init"):
                    self._ensure_semantic_index()

            self._readiness.mark_semantic_ready()
            self._running = True
            self._last_indexed_monotonic_ns = time.monotonic_ns()
        except BaseException as exc:
            # Record failure so all wait_until_ready waiters surface an
            # OracleInitFailed instead of hanging forever. Re-raise to
            # let the harness's caller log the warning (legacy contract).
            try:
                self._readiness.mark_failed(exc)
            except Exception:  # noqa: BLE001 — defensive
                pass
            raise
        # Structured boot-timing log — single line, parseable, low-noise
        _elapsed_ms = (time.monotonic() - _t_start) * 1000.0
        logger.info(
            "[Oracle.boot] initialize complete elapsed_ms=%.1f cache_loaded=%s "
            "graph_nodes=%d graph_edges=%d",
            _elapsed_ms, bool(_cache_loaded),
            self._graph._metrics.get('total_nodes', 0),
            self._graph._metrics.get('total_edges', 0),
        )
        return True

    async def shutdown(self) -> None:
        """Shutdown the Oracle, saving cache (bounded).

        Teardown-coherence (2026-05-24) — closes bt-2026-05-25-020602
        wedge: a 1.1GB ``codebase_graph.pkl`` synchronous serialization
        held the Python GIL + the process's I/O slot in uninterruptible
        kernel state past the ``BoundedShutdownWatchdog`` 30s window,
        so ``os._exit(75)`` could not be scheduled. Two-layer defense:

        * Layer 1 (``_save_cache`` lifted to ``asyncio.to_thread``)
          releases the event loop + lets the watchdog daemon thread run.
        * Layer 2 (this ``asyncio.wait_for``) bounds the cache-save
          regardless — if the traversal still holds the GIL past
          the deadline, the asyncio side abandons and returns control
          so the harness teardown chain completes within the
          ``BoundedShutdownWatchdog`` budget.

        The graph cache is rebuildable on next boot from index. An
        abandoned save means slower cold start, NOT correctness loss.
        Master knob: ``JARVIS_ORACLE_SHUTDOWN_DEADLINE_S`` (default 5s).
        Set to ``0`` to skip ``_save_cache`` entirely on shutdown.
        """
        logger.info("Shutting down The Oracle...")
        # Cancellation token FIRST: stop the indexer from submitting new batches so
        # the AST pool can drain instead of fighting fresh work during teardown.
        self._shutting_down = True
        deadline_s = _oracle_shutdown_deadline_s()
        if deadline_s <= 0.0:
            logger.info(
                "[Oracle.shutdown] cache-save SKIPPED — "
                "JARVIS_ORACLE_SHUTDOWN_DEADLINE_S=%.2f", deadline_s,
            )
        else:
            try:
                await asyncio.wait_for(
                    self._save_cache(), timeout=deadline_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[Oracle.shutdown] _save_cache exceeded %.2fs "
                    "deadline — abandoning save (cache rebuilds on "
                    "next boot). Set JARVIS_ORACLE_SHUTDOWN_DEADLINE_S "
                    "higher if your graph genuinely needs more time, "
                    "or =0 to skip saves on shutdown entirely.",
                    deadline_s,
                )
        # Tear down the AST-indexing ProcessPoolExecutor deterministically.
        # Closes the bt-2026-06-16 "Shutting down The Oracle..." wedge: an in-flight
        # index left pool workers running, which blocked clean exit so
        # session_outcome never reached "complete". Bounded drain + force-terminate
        # (see ast_compile_helper.shutdown_pool). Runs in a thread because the
        # graceful join is blocking; the event loop keeps ticking. NEVER raises.
        try:
            from backend.core.ouroboros.governance.ast_compile_helper import (
                shutdown_pool as _shutdown_ast_pool,
            )
            verdict = await asyncio.to_thread(
                _shutdown_ast_pool,
                deadline_s=_oracle_pool_teardown_deadline_s(),
            )
            if verdict == "escalated":
                logger.warning(
                    "[Oracle.shutdown] ORACLE_TEARDOWN_ESCALATION — AST pool "
                    "force-terminated to guarantee bounded shutdown",
                )
            else:
                logger.info("[Oracle.shutdown] AST pool teardown: %s", verdict)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[Oracle.shutdown] AST pool teardown best-effort failed",
                exc_info=True,
            )
        # Phase 2 — release the persistence connection (closes the aiosqlite worker thread).
        # No-op when the legacy pickle path is active (provider is None). NEVER raises.
        if self._persistence is not None:
            try:
                await self._persistence.close()
            except Exception:  # noqa: BLE001
                logger.debug("[Oracle.shutdown] persistence close best-effort failed", exc_info=True)
        self._running = False
        logger.info("The Oracle shutdown complete")

    def _ensure_semantic_index(self) -> "OracleSemanticIndex":
        """Slice 154 — lazily construct the OracleSemanticIndex if it isn't up yet.

        ``initialize_backend`` pre-warms ``self._semantic_index``, but full_index /
        incremental_update can run before that semantic phase — leaving it None and
        crashing ``embed_nodes`` with 'NoneType has no attribute embed_nodes'. This
        guarantees a non-None index at every embed call site (single construction
        point; OracleSemanticIndex.__init__ is lazy + never raises). NEVER returns None."""
        if self._semantic_index is None:
            self._semantic_index = OracleSemanticIndex()
        return self._semantic_index

    async def full_index(self) -> None:
        """Perform a full index of all repositories."""
        logger.info("Starting full codebase index...")
        start_time = time.time()

        async with self._lock:
            self._graph.clear()
            self._file_hashes.clear()

            # Index each repository
            for repo_name, repo_path in self._repos.items():
                if repo_path.exists():
                    await self._index_repository(repo_name, repo_path)
                else:
                    logger.warning(f"Repository not found: {repo_name} at {repo_path}")

            self._graph._metrics["last_full_index"] = time.time()

        elapsed = time.time() - start_time
        logger.info(f"Full index complete in {elapsed:.2f}s: "
                   f"{self._graph._metrics['total_nodes']} nodes, "
                   f"{self._graph._metrics['total_edges']} edges")

        # Persist. When SQLite is on, every batch already committed its files incrementally, so a
        # final whole-graph rewrite would be redundant (and re-introduce the monolithic write we
        # eliminated) — flush only the metrics meta row. Legacy pickle path takes the full save.
        from backend.core.ouroboros import oracle_persistence as _op_fi
        if _op_fi.sqlite_persistence_enabled():
            await self._sqlite_persist_metrics()
        else:
            await self._save_cache()

        # Embed all nodes into semantic index (fault-isolated)
        try:
            all_nodes = self._graph.get_all_nodes()
            await self._ensure_semantic_index().embed_nodes(all_nodes)
        except Exception as exc:
            logger.warning("[Oracle] Semantic embedding after full_index failed: %s", exc)

    async def incremental_update(self, changed_files: Optional[List[Path]] = None) -> None:
        """
        Incrementally update the index for changed files.

        If no files specified, scans all repos for changes.
        """
        logger.info("Starting incremental update...")
        start_time = time.time()

        async with self._lock:
            if changed_files:
                # Update specific files
                for file_path in changed_files:
                    await self._update_file(file_path)
            else:
                # Scan for changes
                for repo_name, repo_path in self._repos.items():
                    if repo_path.exists():
                        await self._scan_for_changes(repo_name, repo_path)

            self._graph._metrics["last_incremental_update"] = time.time()

        # Embed changed nodes into semantic index (fault-isolated)
        try:
            if changed_files:
                changed_rel_paths: set = set()
                for fp in changed_files:
                    for _repo_name, repo_root in self._repos.items():
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
            await self._ensure_semantic_index().embed_nodes(changed_nodes)
        except Exception as exc:
            logger.warning("[Oracle] Semantic embedding after incremental_update failed: %s", exc)

        elapsed = time.time() - start_time
        logger.info(f"Incremental update complete in {elapsed:.2f}s")

    async def _measure_loop_lag_ms(self, probe_s: float = 0.02) -> float:
        """Cheap event-loop lag probe: how far a short ``asyncio.sleep`` overshoots
        is how backed-up the loop is right now. Pure timing; never raises."""
        try:
            t0 = time.monotonic()
            await asyncio.sleep(probe_s)
            return max(0.0, (time.monotonic() - t0 - probe_s) * 1000.0)
        except Exception:  # noqa: BLE001
            return 0.0

    async def _drain_graph_write_queue(self, timeout_s: float = 30.0) -> None:
        """Bounded wait until the async graph-write consumer has APPLIED all enqueued writes, so a
        subsequent read of the live graph reflects the just-submitted batch. No-op when the queue
        is disabled (legacy inline-write path). Never raises."""
        q = self._graph_write_queue
        if q is None:
            return
        try:
            await asyncio.wait_for(q.join(), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "[Oracle] graph-write queue drain exceeded %.1fs — checkpoint may lag a batch",
                timeout_s,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _sqlite_incremental_checkpoint(
        self, batch_files: List[Path], repo_name: str, repo_path: Path,
    ) -> float:
        """Phase-2 incremental hot path. Drains the graph-write queue so the batch's nodes are
        materialized, extracts per-file rows from the live graph, and commits ONLY this batch's
        dirty files in one ACID transaction via ``upsert_files`` — *every commit is a checkpoint*,
        replacing the monolithic per-batch rewrite that caused cold-index starvation.

        Returns the commit wall-time in ms (fed into the AIMD throttle as a disk-I/O-latency
        signal). Fail-soft: a checkpoint failure NEVER breaks the index."""
        from backend.core.ouroboros import oracle_persistence as _op

        prov = self._persistence_provider()
        if prov is None or not hasattr(prov, "upsert_files"):
            return 0.0
        try:
            await self._drain_graph_write_queue()
            records: Dict[str, Dict[str, Any]] = {}
            for fp in batch_files:
                try:
                    rel = str(fp.relative_to(repo_path))
                except ValueError:
                    rel = str(fp)
                cache_key = f"{repo_name}:{rel}"
                # _file_index is keyed by bare relative path; filter to THIS repo's node keys
                # (node_key == "repo:relative:name") so a relative path shared across repos
                # doesn't cross-contaminate the per-file dirty replace.
                keys = [
                    k for k in self._graph._file_index.get(rel, ())
                    if k.startswith(repo_name + ":")
                ]
                if not keys:
                    continue
                records[rel] = {
                    "repo": repo_name,
                    "hash_key": cache_key,
                    "source_hash": self._file_hashes.get(cache_key, ""),
                    "node_rows": _op.node_rows_for_keys(self._graph._graph, keys),
                    "edge_rows": _op.edge_rows_for_keys(self._graph._graph, keys),
                }
            if not records:
                return 0.0
            t0 = time.monotonic()
            await prov.upsert_files(records)
            return (time.monotonic() - t0) * 1000.0
        except Exception as exc:  # noqa: BLE001 — durability is best-effort, never break the index
            logger.warning("[Oracle] sqlite incremental checkpoint failed (non-fatal): %s", exc)
            return 0.0

    async def _sqlite_persist_metrics(self) -> None:
        """Flush ONLY the metrics meta row at end of a full index — the nodes/edges were already
        persisted incrementally per batch, so this avoids a redundant whole-graph rewrite."""
        prov = self._persistence_provider()
        if prov is None or not hasattr(prov, "set_meta"):
            return
        try:
            await prov.set_meta("metrics", json.dumps(self._graph._metrics or {}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Oracle] sqlite metrics flush failed (non-fatal): %s", exc)

    def _memory_gate(self):
        """Lazily resolve the shared MemoryPressureGate (the same advisory probe the SensorGovernor
        uses — no duplication). Returns ``None`` if the gate is disabled or unavailable."""
        if self._memory_gate_ref is None:
            try:
                from backend.core.ouroboros.governance.memory_pressure_gate import (
                    get_default_gate, is_enabled,
                )
                if not is_enabled():
                    return None
                self._memory_gate_ref = get_default_gate()
            except Exception:  # noqa: BLE001 — armor must never break the index
                return None
        return self._memory_gate_ref

    async def _memory_armor_check(self) -> str:
        """Phase-1 memory axis of the multi-axis throttle. Probes host RAM pressure; under
        CRITICAL it actively DEFENDS the host boundary — forces ``gc.collect()`` + yields the loop
        to the GC/allocator, re-probing up to ``_oracle_memory_armor_max_yields()`` times. Returns
        one of ``ok|warn|high|critical|critical_persist``:
          - ``warn|high|critical`` → caller contracts the next batch's concurrency footprint
          - ``critical_persist``   → pressure would not clear → caller SUSPENDS the build (safe:
            every SQLite commit is a checkpoint, so it resumes next boot via the file_hashes skip)
        Never raises."""
        gate = self._memory_gate()
        if gate is None:
            return "ok"
        try:
            from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
            lvl = gate.pressure()
        except Exception:  # noqa: BLE001
            return "ok"
        if lvl != PressureLevel.CRITICAL:
            return lvl.value  # ok / warn / high
        # CRITICAL: proactive suspend-and-yield to reclaim transient memory before proceeding.
        import gc
        for _ in range(_oracle_memory_armor_max_yields()):
            self._mem_armor_yields += 1
            gc.collect()
            await asyncio.sleep(_oracle_memory_armor_yield_s())
            try:
                if gate.pressure() != PressureLevel.CRITICAL:
                    return "critical"   # cleared — but recently critical → still contract hard
            except Exception:  # noqa: BLE001
                return "critical"
        return "critical_persist"

    def _partition_subtrees(self, files: "List[Path]", repo_path: Path) -> "List[List[Path]]":
        """Phase 1 — predictive topology partition. Returns a single partition (the whole list) when
        the scoper is off or the work fits the RAM budget; otherwise package-aligned subtrees each
        sized to a *live-derived* RAM budget (no hardcoded cap)."""
        if not _oracle_scoper_enabled() or len(files) <= _oracle_scoper_min_partition_files():
            return [files]
        gate = self._memory_gate()
        try:
            avail_mb = (gate.probe().available_bytes / 1e6) if gate is not None else 4096.0
        except Exception:  # noqa: BLE001
            avail_mb = 4096.0
        budget_mb = max(64.0, avail_mb * _oracle_scoper_safety_frac())
        per_node_mb = _oracle_scoper_per_node_kb() / 1024.0
        target_nodes = budget_mb / max(per_node_mb, 1e-6)
        target_files = max(
            _oracle_scoper_min_partition_files(),
            int(target_nodes / max(_oracle_scoper_nodes_per_file(), 1.0)),
        )
        if len(files) <= target_files:
            return [files]
        parts = _cluster_by_package(files, repo_path, target_files)
        return parts or [files]

    def _evict_partition(self, partition_files: "List[Path]", repo_path: Path, repo_name: str) -> int:
        """Phase 2 — reclaim a committed subtree's RAM. Removes its nodes from the in-memory DiGraph
        + inverted indices (the durable copy is already in SQLite; cross-partition edges persist
        by-key). NEVER touches ``_file_hashes`` (the skip-unchanged signal must survive for resume).
        Returns the node count evicted."""
        g = self._graph
        rels = set()
        keys = set()
        for f in partition_files:
            try:
                rel = str(f.relative_to(repo_path))
            except ValueError:
                rel = str(f)
            rels.add(rel)
            keys.update(k for k in g._file_index.get(rel, ()) if k.startswith(repo_name + ":"))
        keys = {k for k in keys if k in g._graph}
        if not keys:
            return 0
        for k in keys:
            nid = g._node_index.pop(k, None)
            if nid is not None:
                g._file_index.get(nid.file_path, set()).discard(k)
                g._repo_index.get(nid.repo, set()).discard(k)
                g._type_index.get(nid.node_type, set()).discard(k)
        g._graph.remove_nodes_from(keys)  # in-memory only — drops incident edges from the cache
        for fp in rels:
            if fp in g._file_index and not g._file_index[fp]:
                g._file_index.pop(fp, None)
        return len(keys)

    async def _refresh_metrics_from_sqlite(self) -> None:
        """After a scoped+evicted index the in-memory counters are partial — pull the authoritative
        node/edge totals from SQLite (the canonical store holds the full graph)."""
        prov = self._persistence_provider()
        if prov is None or not hasattr(prov, "count_nodes_edges"):
            return
        try:
            n, e = await prov.count_nodes_edges()
            self._graph._metrics["total_nodes"] = n
            self._graph._metrics["total_edges"] = e
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Oracle.scoper] sqlite metric refresh failed (non-fatal): %s", exc)

    async def _index_repository(self, repo_name: str, repo_path: Path) -> None:
        """Index all Python files in a repository.

        Adaptive Local Subtree Scoper: when ``JARVIS_ORACLE_ADAPTIVE_SCOPER_ENABLED`` is on, the
        repo's files are partitioned into decoupled package subtrees (Phase 1) and indexed
        sequentially; between subtrees, if host RAM is pressured, the engine structurally checkpoints
        SQLite, evicts the just-committed subtree from the in-memory DiGraph, and forces a GC before
        the next partition (Phase 2). The full graph still lands in SQLite (byte-identical), so a
        constrained host BUILDS the whole brain without ever holding it all resident. Scoper off →
        a single partition = the pre-scoper path, byte-identical."""
        logger.info(f"Indexing repository: {repo_name} at {repo_path}")

        python_files = await self._find_python_files(repo_path)
        total_files = len(python_files)
        logger.info(f"Found {total_files} Python files in {repo_name}")

        partitions = self._partition_subtrees(python_files, repo_path)
        self._scoper_partitions = len(partitions)
        from backend.core.ouroboros import oracle_persistence as _op_part
        _sqlite_on_part = _op_part.sqlite_persistence_enabled()
        if len(partitions) > 1:
            logger.info(
                "[Oracle.scoper] %s: %d files → %d package subtrees (RAM-bounded sequential build)",
                repo_name, total_files, len(partitions),
            )
        gate = self._memory_gate() if _oracle_scoper_enabled() else None

        for pidx, part in enumerate(partitions):
            label = f"{pidx + 1}/{len(partitions)}" if len(partitions) > 1 else ""
            suspended = await self._run_index_batches(part, repo_name, repo_path, label)
            if suspended:
                break
            # Phase 2 — between-subtree RAM reclaim (only when partitioned + pressured + durable).
            if len(partitions) > 1 and gate is not None and _sqlite_on_part and pidx < len(partitions) - 1:
                try:
                    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
                    lvl = gate.pressure()
                except Exception:  # noqa: BLE001
                    lvl = None
                if lvl in (PressureLevel.HIGH, PressureLevel.CRITICAL):
                    prov = self._persistence_provider()
                    if prov is not None and hasattr(prov, "checkpoint_wal"):
                        try:
                            await prov.checkpoint_wal()   # fold WAL → partition is durable, bound -wal
                        except Exception:  # noqa: BLE001
                            pass
                    evicted = self._evict_partition(part, repo_path, repo_name)
                    import gc
                    gc.collect()
                    self._scoper_evictions += 1
                    logger.info(
                        "[Oracle.scoper] subtree %s done — RAM %s → checkpoint + evicted %d nodes + GC",
                        label, lvl.value, evicted,
                    )

        self._graph._metrics["files_indexed"] = total_files
        if _sqlite_on_part and self._scoper_evictions > 0:
            await self._refresh_metrics_from_sqlite()

    async def _run_index_batches(
        self, python_files: "List[Path]", repo_name: str, repo_path: Path, partition_label: str = "",
    ) -> bool:
        """Inner adaptive batch loop over ONE partition's files. Returns True if the build was
        SUSPENDED (shutdown or CRITICAL-persist memory) so the caller stops; False if it completed.

        This is the pre-scoper ``_index_repository`` body verbatim (AIMD throttle + Memory Armor +
        incremental SQLite checkpoint), parameterized by ``python_files`` so the outer scoper can
        drive it subtree-by-subtree."""
        total_files = len(python_files)
        if total_files == 0:
            return False
        _plabel = f" [subtree {partition_label}]" if partition_label else ""

        # Process files in parallel batches.  Per-batch progress logging
        # so operators have visibility into long-running indexing — the
        # silent ~24-minute index in stage-1 wiring soak 2026-05-13 had
        # zero between-boundary logs, making "still indexing" vs "hung"
        # indistinguishable from outside the process.
        batch_size = OracleConfig.MAX_PARALLEL_FILES
        batch_count = (total_files + batch_size - 1) // batch_size
        _t_batch_start = time.monotonic()
        _last_progress_log = _t_batch_start
        # Monotonic checkpoint cadence.  A cold build of ~24k files used
        # to be a single all-or-nothing write at the very end — if the
        # process died (OOM / memory watchdog / SIGKILL) mid-build, the
        # entire unbounded partial graph was lost and the NEXT boot
        # restarted the full reindex from zero (the 52GB OOM loop).
        # Periodic _save_cache() makes partial progress durable; composed
        # with the load/save path-symmetry fix the next boot loads this
        # checkpoint as a cache HIT and GLS's incremental loop tops it
        # up via the _file_hashes skip.  Quiescence parking is untouched
        # (still awaited first each batch) — this adds durability only,
        # it does not alter the index schedule.  Default 1 (every batch)
        # per operator mandate; raise via env on huge graphs where the
        # per-checkpoint pickle cost dominates.
        _ck_raw = os.getenv("JARVIS_ORACLE_CHECKPOINT_EVERY_N_BATCHES")
        try:
            _ck_every_n = max(0, int(_ck_raw)) if _ck_raw is not None else 1
        except (TypeError, ValueError):
            _ck_every_n = 1
        # Task #104 — Quiescence Protocol checkpoint.  The B1
        # falsification campaign proved this boot index is the
        # dominant event-loop suffocator (disabling it flipped Claude
        # stream first_raw_event 0→24).  Even gated, when it DOES run
        # it must yield the loop the instant a core stream engages.
        # Lazy import (same governance package; avoid load cycle).
        try:
            from backend.core.ouroboros.governance.quiescence import (
                await_quiescence_clearance as _await_quiescence,
            )
        except Exception:  # noqa: BLE001
            _await_quiescence = None  # type: ignore[assignment]
        # Adaptive backpressure (Phase 1): AIMD throttle so cold indexing yields to the
        # FSM control plane instead of starving it (ControlPlaneStarvation cold-boot
        # wedge). When disabled, ``effective`` stays pinned at the full batch_size — the
        # legacy fixed-batch firehose, behaviorally unchanged.
        _throttle = (
            _AdaptiveIndexThrottle(
                max_batch=batch_size,
                min_batch=_oracle_backpressure_min_batch(),
                lag_threshold_ms=_oracle_backpressure_lag_ms(),
            )
            if _oracle_backpressure_enabled()
            else None
        )
        # Phase 2 — when SQLite persistence is on, the per-batch checkpoint becomes an INCREMENTAL
        # upsert of just this batch's files (every commit is a checkpoint) instead of a monolithic
        # whole-graph rewrite. The commit window IS the adaptive AIMD batch (no hardcoded interval).
        from backend.core.ouroboros import oracle_persistence as _op_mod
        _sqlite_on = _op_mod.sqlite_persistence_enabled()
        # Sovereign Memory Armor — second throttle axis. Maps host RAM pressure onto the AIMD
        # throttle's lag scale so an elevated memory level contracts the next batch's process-pool
        # fan-out exactly as event-loop lag does (multiplier × the throttle's lag threshold).
        _armor_on = _oracle_memory_armor_enabled()
        # Multiplier × the throttle's lag threshold → synthetic "lag" fed to the AIMD. >1.0 so each
        # elevated level actually breaches (the throttle halves on >threshold) and higher levels
        # also yield the loop longer (backoff_s is proportional to the overshoot). Graded defense.
        _MEM_LAG_MULT = {"ok": 0.0, "warn": 1.5, "high": 2.5, "critical": 4.0}
        i = 0
        _batch_no = 0
        while i < total_files:
            # Cancellation token: abort the build if shutdown was requested, so the
            # AST pool can drain + teardown instead of being abandoned mid-index
            # (the bt-2026-06-16 "Shutting down The Oracle..." wedge).
            if self._shutting_down:
                logger.info(
                    "[Oracle] index of %s%s cancelled — shutdown requested "
                    "(%d/%d files submitted)", repo_name, _plabel, i, total_files,
                )
                return True
            # Park at 0% CPU here if a Claude SDK stream is in flight —
            # deterministic containment, not best-effort sleep(0).
            if _await_quiescence is not None:
                try:
                    await _await_quiescence(label="oracle_index_repository")
                except Exception:  # noqa: BLE001 — never break the index
                    pass
            # --- Sovereign Memory Armor (top-of-loop, multi-axis) ---
            # Probe host RAM BEFORE submitting the batch. CRITICAL → defend the 16GB boundary:
            # gc-yield, and if it won't clear, SUSPEND with a durable checkpoint (resumes next
            # boot). WARN/HIGH → fold into the AIMD throttle so this batch contracts its fan-out.
            if _armor_on and not self._shutting_down:
                _mem_lvl = await self._memory_armor_check()
                if _mem_lvl == "critical_persist":
                    self._mem_armor_suspended = True
                    logger.warning(
                        "[Oracle.index] CRITICAL memory pressure persisted after GC yields — "
                        "SUSPENDING %s%s index at %d/%d files. Partial graph is durable (every "
                        "SQLite commit is a checkpoint); it resumes next boot via the file_hashes "
                        "skip.", repo_name, _plabel, i, total_files,
                    )
                    return True
                if _throttle is not None and _MEM_LAG_MULT.get(_mem_lvl, 0.0) > 0.0:
                    self._mem_armor_contractions += 1
                    _throttle.update(_MEM_LAG_MULT[_mem_lvl] * _throttle.lag_threshold_ms)
            effective = _throttle.batch if _throttle is not None else batch_size
            batch = python_files[i:i + effective]
            tasks = [
                self._index_file(repo_name, repo_path, file_path)
                for file_path in batch
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            i += len(batch)
            _batch_no += 1
            _is_last_batch = i >= total_files
            # Durable partial checkpoint (see cadence note above).  Never
            # let a checkpoint failure break the index — durability is a
            # best-effort enhancement, not a correctness dependency.
            _commit_ms = 0.0
            if _ck_every_n > 0 and (_is_last_batch or _batch_no % _ck_every_n == 0):
                if _sqlite_on:
                    # Incremental ACID commit of THIS batch's dirty files only — no whole-graph
                    # rewrite. Commit latency feeds the throttle below (I/O-adaptive batch window).
                    _commit_ms = await self._sqlite_incremental_checkpoint(
                        batch, repo_name, repo_path,
                    )
                else:
                    try:
                        await self._save_cache()
                    except Exception:  # noqa: BLE001 — never break the index
                        pass
            # Emit progress at most every 5s OR on the last batch — bounds
            # log volume on fast SSDs while still surfacing forward motion.
            _now = time.monotonic()
            files_done = min(i, total_files)
            if _is_last_batch or (_now - _last_progress_log) >= 5.0:
                rate = files_done / max(_now - _t_batch_start, 0.001)
                pct = 100.0 * files_done / max(total_files, 1)
                _bp_note = f" batch={effective}" if _throttle is not None else ""
                logger.info(
                    "[Oracle.index] %s%s: %d/%d files (%.1f%%) "
                    "rate=%.0f files/s elapsed=%.1fs%s",
                    repo_name, _plabel, files_done, total_files, pct,
                    rate, _now - _t_batch_start, _bp_note,
                )
                _last_progress_log = _now
            # Adaptive backpressure: measure loop lag, throttle next-batch concurrency,
            # and yield the loop proportional to the overshoot so the FSM can breathe.
            if _throttle is not None and not _is_last_batch:
                lag_ms = await self._measure_loop_lag_ms()
                # I/O-adaptive batch window: a slow incremental commit (disk write throttled)
                # is folded into the effective lag so the next batch CONTRACTS under write
                # pressure — and expands again when commits are fast. The commit window thus
                # tracks both event-loop responsiveness AND disk I/O latency, no hardcoding.
                eff_lag = max(lag_ms, _commit_ms)
                _throttle.update(eff_lag)
                _backoff = _throttle.backoff_s(eff_lag)
                if _backoff > 0.0:
                    await asyncio.sleep(_backoff)

        return False  # partition completed (not suspended)

    async def _find_python_files(self, root: Path) -> List[Path]:
        """Find all Python files in a directory, excluding patterns."""
        python_files = []

        def should_exclude(path: Path) -> bool:
            path_str = str(path)
            for pattern in OracleConfig.EXCLUDE_PATTERNS:
                if pattern in path_str:
                    return True
            return False

        def scan_dir(directory: Path) -> None:
            try:
                for item in directory.iterdir():
                    if should_exclude(item):
                        continue
                    if item.is_dir():
                        # Slice 257 — never descend into a linked git worktree
                        # (a duplicate checkout whose files are not tracked
                        # source). General, no-hardcoding guard that catches
                        # any future `git worktree add` location the static
                        # EXCLUDE_PATTERNS list does not yet name, while
                        # preserving embedded clones that ARE real source
                        # (e.g. backend/vision).
                        if _is_linked_git_worktree(item):
                            continue
                        scan_dir(item)
                    elif item.suffix in OracleConfig.SUPPORTED_EXTENSIONS:
                        python_files.append(item)
            except PermissionError:
                pass

        await asyncio.to_thread(scan_dir, root)
        return python_files

    async def _index_file(self, repo_name: str, repo_path: Path, file_path: Path) -> None:
        """Index a single Python file.

        Slice 32 (2026-05-27, closes v25 bt-2026-05-27-194342 wedge):
        the CPU-heavy parse + visitor walk now routes through
        ``ast_compile_helper.analyze_python_source_for_oracle`` —
        which dispatches to the existing module-singleton
        ``ProcessPoolExecutor`` (spawn context). The asyncio main
        thread keeps ticking during the await; GIL contention from
        ``asyncio.to_thread`` workers (which wedged v25 for 25
        minutes between 13:34 and 14:00) is no longer possible —
        the workers live in child processes with their own GILs.

        The skip-unchanged check stays on the main thread (cheap
        dict lookup + md5 of pre-read content) so the IPC overhead
        only pays when work is actually required.

        Operator escape hatch: ``JARVIS_ORACLE_LEGACY_THREAD_MODE=1``
        restores the legacy ``asyncio.to_thread`` path byte-identically
        for emergency rollback. Default off.

        Legacy ``_read_parse_visit_blocking`` is retained verbatim
        below as the fallback's worker; the new path does NOT call it.

        Stage-1 wiring soak 2026-05-13 historical context: the
        original implementation moved ONLY ``file_path.read_text``
        into a thread and ran parse + visit synchronously on the
        event loop. The threadpool fix (~14 workers) reduced cold-
        cache time to single-digit minutes but introduced the GIL-
        starvation wedge above. Slice 32 closes that.
        """
        # Lazy import — avoid main-process cycle (ast_compile_helper
        # is not imported at oracle.py module init).
        from backend.core.ouroboros.governance.ast_compile_helper import (
            AnalyzeOutcome as _AC_AnalyzeOutcome,
            analyze_python_source_for_oracle as _ac_analyze_for_oracle,
        )

        if _is_oracle_legacy_thread_mode():
            # Legacy path — byte-identical pre-Slice-32. Only fires
            # when operator sets JARVIS_ORACLE_LEGACY_THREAD_MODE=1.
            try:
                parse_result = await asyncio.to_thread(
                    self._read_parse_visit_blocking,
                    repo_name, repo_path, file_path,
                )
            except Exception as e:
                logger.warning(f"Error indexing {file_path}: {e}")
                return
            if parse_result is None:
                return
            nodes, edges, cache_key, content_hash = parse_result
            # Graph mutations on the event loop — fast, atomic per op.
            self._file_hashes[cache_key] = content_hash
            for node_data in nodes:
                self._graph.add_node(node_data)
            for source, target, edge_data in edges:
                self._graph.add_edge(source, target, edge_data)
            return

        # Slice 32 — process-pool path (default).
        # File read stays on a worker thread (I/O — releases GIL).
        try:
            content = await asyncio.to_thread(
                file_path.read_text, encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Error reading {file_path}: {e}")
            return

        # Skip-unchanged check on the main thread — cheap dict
        # lookup + md5. If we hash matches the cached value, we skip
        # the IPC roundtrip entirely (incremental-update fast path).
        content_hash = hashlib.md5(content.encode()).hexdigest()
        try:
            relative_path = str(file_path.relative_to(repo_path))
        except ValueError:
            relative_path = str(file_path)
        cache_key = f"{repo_name}:{relative_path}"
        existing = self._file_hashes.get(cache_key)
        if existing is not None and existing == content_hash:
            return

        # Dispatch the heavy ast.parse + CodeStructureVisitor walk to
        # the spawn-context process pool. No ast.AST crosses IPC; the
        # worker returns NodeData/EdgeData/NodeID lists directly.
        result = await _ac_analyze_for_oracle(
            caller="oracle._index_file",
            source=content,
            filename=str(file_path),
            repo_name=repo_name,
            relative_path=relative_path,
        )

        if result.outcome != _AC_AnalyzeOutcome.OK:
            if result.outcome == _AC_AnalyzeOutcome.SYNTAX_ERROR:
                logger.warning(
                    f"Syntax error in {file_path}: {result.error_detail}"
                )
            elif result.outcome != _AC_AnalyzeOutcome.TOO_LARGE:
                logger.warning(
                    f"Oracle analyze failed for {file_path}: "
                    f"{result.outcome.value}: {result.error_detail}"
                )
            return

        # Use the hash the worker computed — defensive consistency
        # check against the main-thread hash (they MUST match, but if
        # they don't we trust the worker's view of what it parsed).
        _effective_hash = result.content_hash or content_hash
        # Slice 33 Arc 2 Phase 3 — async graph-write queue (default
        # TRUE per JARVIS_ORACLE_GRAPH_QUEUE_ENABLED). When enabled,
        # enqueue the write batch and return; the consumer task
        # drains and applies via asyncio.to_thread off-loop. Closes
        # the v28 LoopSink 76-occurrence graph_write_bulk pressure
        # (peak 3,580 ms on large files).
        if _is_oracle_graph_queue_enabled():
            await self._ensure_graph_write_consumer()
            assert self._graph_write_queue is not None
            try:
                # put_nowait if there's room, otherwise await to
                # apply backpressure. Backpressure here means
                # _index_file slows to match consumer drain rate —
                # which is appropriate; otherwise we'd OOM on a 29k-
                # file index burst.
                self._graph_write_queue.put_nowait(
                    (result.nodes, result.edges,
                     cache_key, _effective_hash),
                )
                self._graph_writes_enqueued += 1
            except asyncio.QueueFull:
                # Backpressure path: await the put. The asyncio loop
                # still ticks because we're awaiting.
                try:
                    await self._graph_write_queue.put(
                        (result.nodes, result.edges,
                         cache_key, _effective_hash),
                    )
                    self._graph_writes_enqueued += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"Failed to enqueue graph write for "
                        f"{file_path}: {exc}"
                    )
                    self._graph_writes_dropped += 1
            return

        # Legacy inline-write path (escape hatch
        # JARVIS_ORACLE_GRAPH_QUEUE_ENABLED=0). Slice 33 Arc 0
        # LoopSink instrumentation retained for parity with the
        # async-queue path's telemetry.
        from backend.core.ouroboros.telemetry.loop_sink import (  # noqa: WPS433
            sink_sync as _ls_sink_sync,
        )
        self._file_hashes[cache_key] = _effective_hash
        with _ls_sink_sync(
            "oracle._index_file.graph_write_bulk",
        ):
            for node_data in result.nodes:
                self._graph.add_node(node_data)
            for source, target, edge_data in result.edges:
                self._graph.add_edge(source, target, edge_data)

    def _read_parse_visit_blocking(
        self,
        repo_name: str,
        repo_path: Path,
        file_path: Path,
    ) -> "Optional[Tuple[list, list, str, str]]":
        """Read + AST-parse + visitor-walk a single Python file.

        Designed to run ENTIRELY in a worker thread (called via
        ``asyncio.to_thread``).  Returns ``(nodes, edges, cache_key,
        content_hash)`` or ``None`` to signal "skip" (unchanged file
        in incremental mode, syntax error, or unreadable file).

        Reads ``self._file_hashes`` for the unchanged-skip check.  In
        Python with the GIL, individual dict reads are atomic; the
        incremental-update path holds ``self._lock`` (asyncio.Lock) at
        the caller level so concurrent writes can't tear the read.
        """
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Error reading {file_path}: {e}")
            return None

        content_hash = hashlib.md5(content.encode()).hexdigest()
        relative_path = str(file_path.relative_to(repo_path))
        cache_key = f"{repo_name}:{relative_path}"

        # Skip if unchanged (incremental-update fast path)
        existing = self._file_hashes.get(cache_key)
        if existing is not None and existing == content_hash:
            return None

        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError as e:
            logger.warning(f"Syntax error in {file_path}: {e}")
            return None

        visitor = CodeStructureVisitor(repo_name, relative_path, content)
        visitor.visit(tree)

        return visitor.nodes, visitor.edges, cache_key, content_hash

    async def _update_file(self, file_path: Path) -> None:
        """Update index for a specific file."""
        # Determine which repo this file belongs to
        for repo_name, repo_path in self._repos.items():
            try:
                relative = file_path.relative_to(repo_path)
                await self._index_file(repo_name, repo_path, file_path)
                return
            except ValueError:
                continue

        logger.warning(f"File not in any known repository: {file_path}")

    async def _scan_for_changes(self, repo_name: str, repo_path: Path) -> None:
        """Scan repository for changed files.

        Task #102 (2026-05-14) — Autonomous Event-Loop Governance.
        v14-rev18 isolated H11 (event-loop starvation) as the final-mile
        cause of Claude stream first_token=NEVER under harness conditions.
        This loop iterates 29k+ files on the event loop; even though the
        read + index work is offloaded via asyncio.to_thread, the per-
        iteration hash + dict-lookup + scheduling churn cumulatively
        starves higher-priority coroutines (notably the Claude SDK
        stream consumer).

        Fix: read + hash compute now ALSO offloaded via
        ``offload_blocking`` (composes the shared event_loop_governance
        substrate), and the iterator is wrapped in
        ``cooperative_yield_every_n_async`` which inserts ``asyncio.
        sleep(0)`` every N items (default 64) — gives Claude's stream
        consumer guaranteed scheduling slots even during the heaviest
        Oracle scan.  Master switch
        ``JARVIS_EVENT_LOOP_GOVERNANCE_ENABLED`` (default true);
        flip-off restores legacy byte-identical behavior.
        """
        # Lazy import — substrate lives under .governance/ and oracle.py
        # is imported eagerly at module-import time of the governance
        # tree; avoid a top-level import cycle.
        from backend.core.ouroboros.governance.event_loop_governance import (
            cooperative_yield_every_n_async,
            offload_blocking,
        )
        # Slice 33 Arc 0 — Loop-Sink instrumentation (diagnostic only).
        from backend.core.ouroboros.telemetry.loop_sink import (
            sink_sync as _ls_sink_sync,
        )

        python_files = await self._find_python_files(repo_path)

        def _read_and_hash(p: Path) -> "tuple[str, str]":
            """Sync helper — read + hash in ONE thread hop instead of
            two.  Each separate await asyncio.to_thread costs a
            scheduler round-trip; bundling halves the overhead and
            keeps the hash CPU off the event loop entirely."""
            _content = p.read_text(encoding="utf-8")
            return _content, hashlib.md5(_content.encode()).hexdigest()

        async for file_path in cooperative_yield_every_n_async(python_files):
            try:
                content, content_hash = await offload_blocking(
                    _read_and_hash, file_path,
                    label="oracle._scan_for_changes.read_and_hash",
                )
                # Slice 33 Arc 0 — measure the sync between-await chunk
                # (relative_to + dict lookup + cache_key compose). At
                # 29k iterations even tiny per-iter cost compounds; if
                # this site shows in the v27 leaderboard, the iterator
                # itself is the sink.
                with _ls_sink_sync(
                    "oracle._scan_for_changes.between_await_chunk",
                ):
                    relative_path = str(file_path.relative_to(repo_path))
                    cache_key = f"{repo_name}:{relative_path}"
                    needs_index = (
                        cache_key not in self._file_hashes
                        or self._file_hashes[cache_key] != content_hash
                    )

                if needs_index:
                    await self._index_file(repo_name, repo_path, file_path)
            except Exception as e:
                logger.warning(f"Error scanning {file_path}: {e}")

    # ------------------------------------------------------------------
    # Slice 33 Arc 2 Phase 3 — async graph-write queue
    # ------------------------------------------------------------------

    async def _ensure_graph_write_consumer(self) -> None:
        """Lazy-start the graph-write consumer task on first enqueue.
        Idempotent — safe to call concurrently."""
        if self._graph_write_consumer_started:
            return
        # Lock-free initialization is fine here: _index_file is the
        # only enqueue caller, runs in a single asyncio loop, so
        # concurrent first-calls don't happen in practice. We use
        # the flag check + immediate assignment for forward safety.
        self._graph_write_consumer_started = True
        max_size = _oracle_graph_queue_max_size()
        self._graph_write_queue = asyncio.Queue(maxsize=max_size)
        self._graph_write_consumer_stop_event = asyncio.Event()
        self._graph_write_consumer_task = asyncio.create_task(
            self._graph_write_consumer_loop(),
            name="oracle.graph_write_consumer",
        )
        logger.info(
            "[Oracle] graph_write_consumer started max_size=%d batch_size=%d",
            max_size, _oracle_graph_queue_batch_size(),
        )

    async def _graph_write_consumer_loop(self) -> None:
        """Drain the graph-write queue and apply batches off-loop.

        Uses ``asyncio.to_thread`` to run the actual NetworkX bulk
        mutations in a worker thread — the asyncio main loop is free
        to schedule other coroutines during the apply window. Each
        batch is up to ``JARVIS_ORACLE_GRAPH_QUEUE_BATCH_SIZE``
        (default 50) records to amortize the per-call scheduling
        overhead.
        """
        assert self._graph_write_queue is not None
        assert self._graph_write_consumer_stop_event is not None
        batch_size = _oracle_graph_queue_batch_size()
        stop_event = self._graph_write_consumer_stop_event
        queue = self._graph_write_queue
        while True:
            try:
                # Wait for the first item OR a stop signal — whichever
                # comes first. asyncio.wait drops the loser.
                get_task = asyncio.create_task(queue.get())
                stop_task = asyncio.create_task(stop_event.wait())
                done, pending = await asyncio.wait(
                    {get_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
                if stop_task in done and get_task not in done:
                    # Stop signal received with no pending item — exit
                    break
                if get_task not in done:
                    continue
                first = get_task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Oracle] graph_write_consumer wait error: %s", exc,
                )
                continue
            # Coalesce additional ready items into the batch
            batch: List[Any] = [first]
            while len(batch) < batch_size:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            # Apply batch off-loop. Worker thread holds GIL during
            # mutations but asyncio loop ticks during the await.
            try:
                await asyncio.to_thread(self._apply_graph_batch_sync, batch)
                self._graph_writes_applied += len(batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Oracle] graph_write_consumer apply error: %s "
                    "batch_size=%d", exc, len(batch),
                )
            finally:
                for _ in batch:
                    try:
                        queue.task_done()
                    except ValueError:
                        # task_done() called more times than get() —
                        # defensive; shouldn't happen but keep going.
                        pass

    def _apply_graph_batch_sync(self, batch: List[Any]) -> None:
        """Sync NetworkX mutation worker — runs in asyncio.to_thread.

        Each batch item is the tuple ``(nodes, edges, cache_key,
        content_hash)`` produced by ``_index_file``. We apply the
        cache hash + nodes + edges atomically per item, then move
        to the next item. NEVER raises — per-item failures are
        logged and the batch continues.
        """
        for item in batch:
            try:
                nodes, edges, cache_key, content_hash = item
                self._file_hashes[cache_key] = content_hash
                for node_data in nodes:
                    self._graph.add_node(node_data)
                for source, target, edge_data in edges:
                    self._graph.add_edge(source, target, edge_data)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Oracle] graph batch item apply failed: %s", exc,
                )

    async def stop_graph_write_consumer(self) -> None:
        """Signal the consumer to stop + drain remaining queue.
        NEVER raises. Idempotent."""
        if not self._graph_write_consumer_started:
            return
        if self._graph_write_consumer_stop_event is not None:
            self._graph_write_consumer_stop_event.set()
        if self._graph_write_consumer_task is not None:
            try:
                await asyncio.wait_for(
                    self._graph_write_consumer_task, timeout=5.0,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._graph_write_consumer_task.cancel()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Oracle] graph_write_consumer stop error: %s", exc,
                )

    @staticmethod
    def _resolved_graph_cache_path() -> Path:
        """Single source of truth for the graph cache location.

        Load AND save MUST resolve through this method so they never
        disagree.  Historically ``_load_cache`` read the *primary* path
        (``~/.jarvis/oracle/codebase_graph.pkl``) directly while
        ``_save_cache`` wrote through ``sandbox_fallback`` — under the
        Iron Gate the primary is non-writable, so every save landed in
        ``.ouroboros/state/sandbox_fallback/oracle/`` while every load
        looked at the (stale/absent) primary.  The result was a cold
        full reindex of the entire codebase on *every* sandboxed boot,
        whose unbounded partial graph never converged and accreted to
        an OOM over a multi-hour soak.

        ``sandbox_fallback`` is idempotent and cached: when the primary
        is writable (normal dev) it returns the primary unchanged, so
        both load and save use the primary with zero behavior change.
        Under the Iron Gate both deterministically use the same
        fallback path — symmetry restored without lowering shields.
        """
        from backend.core.ouroboros.governance.sandbox_paths import sandbox_fallback

        return sandbox_fallback(OracleConfig.GRAPH_CACHE_FILE)

    @staticmethod
    def _resolved_sqlite_path() -> Path:
        """SQLite db location — resolved through the SAME ``sandbox_fallback`` as the pickle so
        load/save/migrate never disagree under the Iron Gate (the symmetry that fixed the
        cold-reindex-every-boot bug for the legacy cache)."""
        from backend.core.ouroboros.governance.sandbox_paths import sandbox_fallback

        return sandbox_fallback(OracleConfig.SQLITE_DB_FILE)

    def _persistence_provider(self):
        """Lazily build (once) the storage backend via the factory. Returns ``None`` when the
        master switch is off OR aiosqlite is unavailable — callers then use the legacy pickle
        path verbatim. No hardcoding: the Oracle asks the factory and adapts."""
        if not self._persistence_built:
            from backend.core.ouroboros import oracle_persistence as _op

            self._persistence = _op.build_provider(
                db_path=self._resolved_sqlite_path(),
                pickle_path=self._resolved_graph_cache_path(),
            )
            self._persistence_built = True
        return self._persistence

    async def _load_cache_via_provider(self) -> bool:
        """Provider-backed load. DB present → load it. DB absent → return ``False`` so the FSM
        cold-indexes FRESH (Phase-1-throttled + memory-armored), never a wedge.

        The legacy ``.pkl`` auto-migration is DEPRECATED and gated behind an explicit opt-in
        (``JARVIS_ORACLE_SQLITE_MIGRATE_PKL``, default off) — materializing a large accreted pickle
        into a live DiGraph is the ~10GB memory monster this layer replaces and would OOM a
        constrained host. Default-ON persistence cold-indexes fresh instead."""
        from backend.core.ouroboros import oracle_persistence as _op

        prov = self._persistence_provider()
        if prov is None:
            return False
        try:
            if not await prov.exists():
                if _op.sqlite_migrate_pkl_enabled():
                    await _op.migrate_pickle_to_sqlite(self._resolved_graph_cache_path(), prov)
                else:
                    return False  # no db + migration deprecated → cold index fresh
            state = await prov.load()
        except Exception as exc:  # noqa: BLE001 — load must never crash boot
            logger.warning("[Oracle] sqlite load failed (cold index will rebuild): %s", exc)
            return False
        if state is None:
            return False
        self._graph._graph = state.graph
        self._graph._node_index = state.node_index
        self._graph._file_index = defaultdict(set, state.file_index)
        self._graph._repo_index = defaultdict(set, state.repo_index)
        self._graph._type_index = defaultdict(set, state.type_index)
        self._graph._metrics = state.metrics
        self._file_hashes = state.file_hashes
        return True

    async def _save_cache_via_provider(self) -> None:
        """Provider-backed full-snapshot save (shutdown path). The incremental hot path is the
        provider's ``upsert_files`` driven from the index loop; this keeps a correct full write."""
        from backend.core.ouroboros import oracle_persistence as _op

        prov = self._persistence_provider()
        if prov is None:
            return
        # Same gated graph hygiene as the legacy path.
        try:
            if os.environ.get("JARVIS_ORACLE_GRAPH_PRUNE_ENABLED", "").strip().lower() in (
                "1", "true", "yes", "on",
            ):
                self._graph.prune_isolated_nodes()
        except Exception:  # noqa: BLE001
            pass
        state = _op.GraphState(
            graph=self._graph._graph,
            node_index=self._graph._node_index,
            file_index=dict(self._graph._file_index),
            repo_index=dict(self._graph._repo_index),
            type_index=dict(self._graph._type_index),
            metrics=self._graph._metrics,
            file_hashes=self._file_hashes,
        )
        try:
            await prov.save(state)
        except Exception as exc:  # noqa: BLE001 — never raise from save
            logger.error("[Oracle] sqlite save failed: %s", exc)

    async def _load_cache(self) -> bool:
        """Load cached graph from disk.

        Loads synchronously (not asyncio.to_thread) to prevent concurrent
        C-extension heap allocation with ChromaDB/torch/numpy.  At boot
        time nothing else needs the event loop — blocking for 2-5s is
        acceptable to avoid libmalloc memory corruption on macOS ARM64.

        NOTE (Slice 111 negative result, retired in Slice 112): threading
        this load via ``asyncio.to_thread`` does NOT unblock the event loop —
        ``pickle.loads`` is GIL-bound, so the worker thread starves the loop
        identically to an inline load (empirically: 165 s of total loop
        silence). The real fix is process isolation (Slice 112): this method
        runs in the Oracle's OWN OS process, so the deserialize never touches
        the engine's event loop at all.

        Note: pickle is used here for internal cache only (never untrusted
        data) — the cache file is written by this same process.

        Phase 2: when ``JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED`` is on, this delegates to the
        provider (with seamless one-time pickle migration). Off → legacy path verbatim.
        """
        from backend.core.ouroboros import oracle_persistence as _op

        if _op.sqlite_persistence_enabled():
            return await self._load_cache_via_provider()
        try:
            cache_path = self._resolved_graph_cache_path()
            if cache_path.exists():
                _raw = cache_path.read_bytes()
                data = pickle.loads(_raw)  # noqa: S301 — trusted internal cache
                del _raw  # free the raw bytes immediately

                self._graph._graph = data["graph"]
                self._graph._node_index = data["node_index"]
                self._graph._file_index = defaultdict(set, data["file_index"])
                self._graph._repo_index = defaultdict(set, data["repo_index"])
                self._graph._type_index = defaultdict(set, data["type_index"])
                self._graph._metrics = data["metrics"]
                self._file_hashes = data.get("file_hashes", {})

                return True
        except Exception as e:
            logger.warning(f"Error loading cache: {e}")

        return False

    async def _save_cache(self) -> None:
        """Save graph to cache on disk (event-loop-safe).

        Builds the data dict on the asyncio thread (cheap reference
        copies), then dispatches the heavy work — pickle serialization +
        ``write_bytes`` + ``os.replace`` — to ``asyncio.to_thread``.

        Why ``to_thread`` matters for teardown coherence
        (bt-2026-05-25-020602): doing the serialization on the asyncio
        thread held the Python GIL + the process I/O slot in
        uninterruptible kernel state for the full duration of a 1.1GB
        cache write. Every other Python thread — including the
        ``BoundedShutdownWatchdog`` daemon thread — was starved. The
        watchdog's ``os._exit(75)`` could not be scheduled because the
        kernel can't deliver a syscall to a thread that isn't running.

        Lifting to ``to_thread`` lets the asyncio thread + watchdog
        thread make progress while the worker thread does the I/O.
        Combined with the ``asyncio.wait_for`` bound in ``shutdown``,
        even a worker thread that stays in kernel I/O past the deadline
        cannot block the harness exit chain.

        Uses highest available protocol for better ARM64 alignment.

        Note: ``pickle`` is used here for internal cache only — the
        graph contains only our own dataclasses, not untrusted data.

        Iron Gate compliance: writes to ``~/.jarvis/oracle/`` may be
        blocked by the sandbox; ``sandbox_fallback`` routes to
        ``.ouroboros/state/sandbox_fallback/oracle/`` without lowering
        shields.

        Phase 2: when ``JARVIS_ORACLE_SQLITE_PERSISTENCE_ENABLED`` is on, this delegates to the
        provider's transactional write. Off → legacy monolithic pickle path verbatim.
        """
        from backend.core.ouroboros import oracle_persistence as _op

        if _op.sqlite_persistence_enabled():
            await self._save_cache_via_provider()
            return

        # Slice 112 graph hygiene (gated, default-OFF): crush serialization
        # bloat by pruning isolated (degree-0) nodes BEFORE the snapshot. Pure
        # bloat removal — traversal results (shortest_path / simple_cycles) are
        # invariant under it. Cheap (O(nodes)); never raises.
        try:
            if os.environ.get(
                "JARVIS_ORACLE_GRAPH_PRUNE_ENABLED", "",
            ).strip().lower() in ("1", "true", "yes", "on"):
                self._graph.prune_isolated_nodes()
        except Exception:  # noqa: BLE001 — hygiene must never break save
            pass

        # Snapshot the data dict on the asyncio thread. These are cheap
        # reference copies — heavy work is the serialization below.
        data = {
            "graph": self._graph._graph,
            "node_index": self._graph._node_index,
            "file_index": dict(self._graph._file_index),
            "repo_index": dict(self._graph._repo_index),
            "type_index": dict(self._graph._type_index),
            "metrics": self._graph._metrics,
            "file_hashes": self._file_hashes,
        }
        _final_cache_path = self._resolved_graph_cache_path()
        try:
            await asyncio.to_thread(
                self._write_cache_blocking, data, _final_cache_path,
            )
        except Exception as e:  # noqa: BLE001 — never raise from save
            logger.error(f"Error saving cache: {e}")

    @staticmethod
    def _write_cache_blocking(
        data: Dict[str, Any], _final_cache_path: Path,
    ) -> None:
        """Synchronous serialization + atomic write — runs in a worker
        thread so the asyncio event loop + the
        ``BoundedShutdownWatchdog`` daemon thread can make progress.

        Arc B.1 — atomic durability. Serialize into a temp file in the
        SAME directory, then ``os.replace`` (POSIX-atomic rename) so a
        crash / SIGKILL / ProcessMemoryWatchdog ``os._exit`` mid-write
        can NEVER leave a torn cache (the bt-2026-05-18-062703
        'invalid load key \\x00' that defeated checkpoint durability +
        blocked graduation #6). Mirrors
        ``dw_heavy_probe._atomic_write`` / ``dataset_loader``.
        """
        _final_cache_path.parent.mkdir(parents=True, exist_ok=True)
        import tempfile as _tempfile
        _tmp_fd, _tmp_name = _tempfile.mkstemp(
            prefix=_final_cache_path.name + ".",
            suffix=".tmp",
            dir=str(_final_cache_path.parent),
        )
        os.close(_tmp_fd)
        try:
            cache_path = Path(_tmp_name)
            cache_path.write_bytes(
                pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL),  # noqa: S301
            )
            os.replace(_tmp_name, str(_final_cache_path))
            _tmp_name = None  # promoted — nothing left to clean up
            logger.info(f"Saved cache to {_final_cache_path}")
        finally:
            if _tmp_name is not None:
                try:
                    os.unlink(_tmp_name)
                except OSError:
                    pass

    # =========================================================================
    # QUERY INTERFACE
    # =========================================================================

    def find(self, name: str, fuzzy: bool = True) -> List[NodeID]:
        """Find nodes by name."""
        return self._graph.find_nodes_by_name(name, fuzzy)

    def get_blast_radius(self, target: str) -> BlastRadius:
        """
        Get the blast radius of changing a target.

        This is the KILLER FEATURE that no other AI tool has.
        """
        return self._graph.compute_blast_radius(target)

    def get_call_chain(self, source: str, target: str) -> Optional[List[NodeID]]:
        """Find how source reaches target through calls."""
        return self._graph.find_call_chain(source, target)

    def get_dependencies(self, target: str) -> List[NodeID]:
        """Get everything the target depends on."""
        nodes = self.find(target, fuzzy=False)
        if not nodes:
            nodes = self.find(target, fuzzy=True)

        all_deps: Set[NodeID] = set()
        for node in nodes:
            deps = self._graph.get_dependencies(node)
            all_deps.update(deps)

        return list(all_deps)

    def get_dependents(self, target: str) -> List[NodeID]:
        """Get everything that depends on the target."""
        nodes = self.find(target, fuzzy=False)
        if not nodes:
            nodes = self.find(target, fuzzy=True)

        all_dependents: Set[NodeID] = set()
        for node in nodes:
            dependents = self._graph.get_dependents(node)
            all_dependents.update(dependents)

        return list(all_dependents)

    def get_context_for_improvement(
        self,
        target: str,
        max_depth: int = 2,
    ) -> Dict[str, Any]:
        """
        Get rich context for improving a target.

        This provides the Oracle's insights to Ouroboros for smarter improvements.
        """
        nodes = self.find(target, fuzzy=True)
        if not nodes:
            return {
                "target": target,
                "found": False,
                "context": "Target not found in codebase graph.",
            }

        primary_node = nodes[0]

        # Get blast radius
        blast_radius = self._graph.compute_blast_radius(primary_node)

        # Get dependencies and dependents
        dependencies = self._graph.get_dependencies(primary_node)
        dependents = self._graph.get_dependents(primary_node)

        # Get callers and callees
        callers = self._graph.get_callers(primary_node)
        callees = self._graph.get_callees(primary_node)

        # Get subgraph
        subgraph = self._graph.get_subgraph(primary_node, depth=max_depth)

        return {
            "target": target,
            "found": True,
            "primary_node": primary_node.to_dict(),
            "blast_radius": blast_radius.to_dict(),
            "dependencies": [n.to_dict() for n in dependencies],
            "dependents": [n.to_dict() for n in dependents],
            "callers": [n.to_dict() for n in callers],
            "callees": [n.to_dict() for n in callees],
            "related_files": list({n.file_path for n in dependencies + dependents + callers + callees}),
            "risk_assessment": {
                "risk_level": blast_radius.risk_level,
                "total_affected": blast_radius.total_affected,
                "recommendation": self._generate_risk_recommendation(blast_radius),
            },
        }

    def _generate_risk_recommendation(self, blast_radius: BlastRadius) -> str:
        """Generate a recommendation based on blast radius."""
        if blast_radius.risk_level == "critical":
            return (
                "CRITICAL RISK: This change affects many components. "
                "Consider incremental changes with thorough testing. "
                "Review all broken imports and calls before proceeding."
            )
        elif blast_radius.risk_level == "high":
            return (
                "HIGH RISK: Multiple components depend on this. "
                "Ensure comprehensive test coverage before changes."
            )
        elif blast_radius.risk_level == "medium":
            return (
                "MEDIUM RISK: Some components affected. "
                "Run related tests after modifications."
            )
        else:
            return (
                "LOW RISK: Isolated change with minimal dependencies. "
                "Safe to proceed with standard testing."
            )

    def get_circular_dependencies(self) -> List[List[str]]:
        """Find all circular dependencies (code smell)."""
        cycles = self._graph.find_circular_dependencies()
        return [[str(n) for n in cycle] for cycle in cycles]

    def get_dead_code(self) -> List[str]:
        """Find potentially unreferenced code."""
        dead = self._graph.find_dead_code()
        return [str(n) for n in dead]

    def get_metrics(self) -> Dict[str, Any]:
        """Get Oracle metrics."""
        return {
            **self._graph._metrics,
            "repos_configured": len(self._repos),
            "repos_indexed": [
                name for name, path in self._repos.items()
                if path.exists()
            ],
        }

    def is_ready(self) -> bool:
        """Return True when the oracle has completed initialisation and is running."""
        return self._running

    def index_age_s(self) -> float:
        """Return seconds since last full index build. 0.0 if never indexed."""
        if self._last_indexed_monotonic_ns == 0:
            return 0.0
        return (time.monotonic_ns() - self._last_indexed_monotonic_ns) / 1_000_000_000

    def get_status(self) -> Dict[str, Any]:
        """Get Oracle status."""
        return {
            "running": self._running,
            "metrics": self.get_metrics(),
            "cache_file": str(self._resolved_graph_cache_path()),
            "cache_exists": self._resolved_graph_cache_path().exists(),
        }

    # =========================================================================
    # v1.1: Smart Context Integration
    # =========================================================================

    async def query_relevant_nodes(
        self,
        query: str,
        limit: int = 20,
        node_types: Optional[List[NodeType]] = None,
    ) -> List[NodeID]:
        """
        v1.1: Query for nodes relevant to a natural language query.

        This powers the SmartContextSelector for surgical context extraction.

        Algorithm:
        1. Extract keywords from query
        2. Find nodes matching keywords (name, file, docstring)
        3. Include blast radius nodes for matched nodes
        4. Score and rank by relevance

        Args:
            query: Natural language query (e.g., "authentication login bug")
            limit: Maximum nodes to return
            node_types: Filter to specific types (None = all)

        Returns:
            List of relevant NodeIDs sorted by relevance
        """
        if not self._running:
            await self.initialize()

        # Extract keywords from query
        keywords = self._extract_keywords(query)
        if not keywords:
            return []

        matched_nodes: Dict[NodeID, float] = {}  # node -> score

        # Phase 1: Direct name matches (highest weight)
        for keyword in keywords:
            nodes = self._graph.find_nodes_by_name(keyword, fuzzy=True)
            for node in nodes:
                if node_types and node.node_type not in node_types:
                    continue
                # Score based on match quality
                name_lower = node.name.lower()
                keyword_lower = keyword.lower()
                if name_lower == keyword_lower:
                    matched_nodes[node] = matched_nodes.get(node, 0) + 1.0
                elif keyword_lower in name_lower:
                    matched_nodes[node] = matched_nodes.get(node, 0) + 0.7
                else:
                    matched_nodes[node] = matched_nodes.get(node, 0) + 0.3

        # Phase 2: File path matches
        for keyword in keywords:
            for repo, repo_path in self._repos.items():
                nodes = self._graph.find_nodes_in_file(keyword)
                for node in nodes:
                    if node_types and node.node_type not in node_types:
                        continue
                    matched_nodes[node] = matched_nodes.get(node, 0) + 0.5

        # Phase 3: Add blast radius for top matches (connected nodes)
        top_matches = sorted(matched_nodes.items(), key=lambda x: x[1], reverse=True)[:5]
        for node, score in top_matches:
            # Get direct dependencies and dependents
            deps = self._graph.get_dependencies(node)
            for dep in deps[:3]:  # Limit expansion
                if dep not in matched_nodes:
                    matched_nodes[dep] = score * 0.5

            dependents = self._graph.get_dependents(node)
            for dependent in dependents[:3]:
                if dependent not in matched_nodes:
                    matched_nodes[dependent] = score * 0.4

        # Phase 4: Sort and limit
        sorted_nodes = sorted(
            matched_nodes.items(),
            key=lambda x: x[1],
            reverse=True
        )[:limit]

        return [node for node, score in sorted_nodes]

    def _extract_keywords(self, query: str) -> List[str]:
        """
        Extract meaningful keywords from a query.

        Filters out common stop words and splits on non-alpha characters.
        """
        import re

        # Common stop words
        stop_words = {
            "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or",
            "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
            "do", "does", "did", "will", "would", "could", "should", "may", "might",
            "must", "shall", "can", "need", "it", "its", "this", "that", "these",
            "those", "i", "you", "he", "she", "we", "they", "what", "which", "who",
            "when", "where", "why", "how", "fix", "bug", "error", "issue", "problem",
            "add", "update", "change", "modify", "create", "delete", "remove",
        }

        # Split and clean
        words = re.split(r'[^a-zA-Z0-9_]+', query.lower())

        # Filter
        keywords = [
            w for w in words
            if len(w) >= 2 and w not in stop_words
        ]

        # Deduplicate while preserving order
        seen = set()
        result = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                result.append(kw)

        return result

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
                # Outgoing edges: imports→imports, calls→callees, inherits→base_classes
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

                # Incoming edges: imports→importers, calls→callers, inherits→inheritors
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
                basename = Path(rel_path).name          # e.g. "foo.py"
                test_name = f"test_{basename}"          # e.g. "test_foo.py"
                for file_index_key in self._graph._file_index.keys():
                    if Path(file_index_key).name == test_name:
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
           - Structural-origin files: ``graph_proximity=1.0``
           - Seed-origin files: ``graph_proximity=0.5``
        5. Partition: structural-origin → structural categories;
           seed-origin → ``semantic_support``.

        **Degradation:**
        - Semantic search fails → return structural neighborhood only.
        - Graph fails → return semantic seeds in ``semantic_support`` only.
        - Both fail → return empty ``FileNeighborhood``.
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
        structural_nh: "FileNeighborhood" = empty
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
                logger.warning(
                    "[Oracle] Semantic search failed: %s; degrading to structural-only", exc
                )

        if not raw_seeds and not structural_set:
            return empty

        # Build seed_score lookup: file_key → semantic_similarity
        seed_scores: Dict[str, float] = {fk: sc for fk, sc in raw_seeds}

        # ── Step 3: Seed graph expansion ──────────────────────────────────
        seed_nh: "FileNeighborhood" = empty
        if raw_seeds:
            try:
                seed_abs: List[Path] = []
                for file_key, _ in raw_seeds:
                    parts = file_key.split(":", 1)
                    if len(parts) == 2:
                        repo_name, rel_path = parts
                        repo_root = self._repos.get(repo_name)
                        if repo_root:
                            seed_abs.append(repo_root / rel_path)
                if seed_abs:
                    seed_nh = self.get_file_neighborhood(seed_abs)
            except Exception as exc:
                logger.warning("[Oracle] Seed graph expansion failed: %s", exc)

        # ── Step 4: Score helper ───────────────────────────────────────────
        def _score(file_key: str, is_structural: bool) -> float:
            graph_prox = 1.0 if is_structural else 0.5
            semantic_sim = seed_scores.get(file_key, 0.0)
            recency = 1.0  # not yet tracked
            return 0.55 * graph_prox + 0.35 * semantic_sim + 0.10 * recency

        # ── Step 5: Partition ─────────────────────────────────────────────
        target_key_set = set(structural_nh.target_files)

        # Semantic-support: seed-derived files NOT in structural
        semantic_candidates: List[str] = []
        seen_semantic: set = set()
        for fk in seed_nh.all_unique_files():
            if fk not in structural_set and fk not in target_key_set and fk not in seen_semantic:
                semantic_candidates.append(fk)
                seen_semantic.add(fk)
        for fk, _ in raw_seeds:
            if fk not in structural_set and fk not in target_key_set and fk not in seen_semantic:
                semantic_candidates.append(fk)
                seen_semantic.add(fk)

        semantic_candidates_scored = sorted(
            semantic_candidates,
            key=lambda fk: _score(fk, is_structural=False),
            reverse=True,
        )

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

    async def get_relevant_files_for_query(
        self,
        query: str,
        limit: int = 20,
    ) -> List[Path]:
        """
        v1.1: Get file paths relevant to a query.

        Convenience method for SmartContextSelector.
        """
        nodes = await self.query_relevant_nodes(query, limit=limit * 2)

        # Extract unique file paths
        files = []
        seen = set()
        for node in nodes:
            file_path = node.file_path
            if file_path and file_path not in seen and file_path != "<external>":
                seen.add(file_path)
                # Resolve relative to repo
                repo_path = self._repos.get(node.repo, Path.cwd())
                full_path = repo_path / file_path
                if full_path.exists():
                    files.append(full_path)

        return files[:limit]


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_oracle: Optional[TheOracle] = None


def get_oracle() -> TheOracle:
    """Get global Oracle instance."""
    global _oracle
    if _oracle is None:
        _oracle = TheOracle()
    return _oracle


async def shutdown_oracle() -> None:
    """Shutdown global Oracle."""
    global _oracle
    if _oracle:
        await _oracle.shutdown()
        _oracle = None


# =============================================================================
# INTEGRATION WITH OUROBOROS
# =============================================================================

class OuroborosOracleIntegration:
    """
    Integration layer between Oracle and Ouroboros.

    Provides Oracle's structural insights to Ouroboros for:
    - Smarter code context when improving files
    - Blast radius warnings before applying changes
    - Dependency-aware test selection
    """

    def __init__(self, oracle: Optional[TheOracle] = None):
        self._oracle = oracle or get_oracle()

    async def get_improvement_context(
        self,
        target_file: Path,
        goal: str,
    ) -> Dict[str, Any]:
        """
        Get rich context for improving a file.

        Returns structural information that helps Ouroboros make
        smarter improvements.
        """
        # Find the file in the graph
        file_name = target_file.stem
        context = self._oracle.get_context_for_improvement(file_name)

        if not context["found"]:
            # Try with full relative path
            for repo_name, repo_path in self._oracle._repos.items():
                try:
                    relative = target_file.relative_to(repo_path)
                    context = self._oracle.get_context_for_improvement(str(relative))
                    break
                except ValueError:
                    continue

        # Add goal-specific analysis
        context["goal"] = goal
        context["goal_keywords"] = self._extract_keywords(goal)

        # Find related entities mentioned in goal
        related_from_goal = []
        for keyword in context["goal_keywords"]:
            found = self._oracle.find(keyword, fuzzy=True)
            related_from_goal.extend(found[:3])  # Top 3 matches

        context["related_from_goal"] = [n.to_dict() for n in related_from_goal]

        return context

    def _extract_keywords(self, text: str) -> List[str]:
        """Extract relevant keywords from text."""
        # Simple keyword extraction (could use NLP for better results)
        import re
        words = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', text)

        # Filter common words
        stopwords = {
            "the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
            "for", "of", "with", "by", "from", "up", "about", "into", "over",
            "after", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "must", "shall", "can",
            "this", "that", "these", "those", "i", "you", "he", "she", "it",
            "we", "they", "what", "which", "who", "when", "where", "why",
            "how", "all", "each", "every", "both", "few", "more", "most",
            "other", "some", "such", "no", "nor", "not", "only", "own",
            "same", "so", "than", "too", "very", "just", "fix", "improve",
            "add", "remove", "update", "change", "make", "code", "function",
        }

        return [w for w in words if w.lower() not in stopwords and len(w) > 2]

    async def check_change_safety(
        self,
        target_file: Path,
        proposed_changes: str,
    ) -> Dict[str, Any]:
        """
        Check if proposed changes are safe to apply.

        Returns warnings if changes might break other components.
        """
        # Get blast radius
        file_name = target_file.stem
        blast_radius = self._oracle.get_blast_radius(file_name)

        warnings = []

        if blast_radius.total_affected > 0:
            warnings.append({
                "level": blast_radius.risk_level,
                "message": f"This change will affect {blast_radius.total_affected} other components.",
                "directly_affected": [str(n) for n in list(blast_radius.directly_affected)[:5]],
            })

        if blast_radius.broken_imports:
            warnings.append({
                "level": "high",
                "message": f"{len(blast_radius.broken_imports)} files import from this module.",
                "files": [str(n) for n, _ in blast_radius.broken_imports[:5]],
            })

        if blast_radius.broken_calls:
            warnings.append({
                "level": "medium",
                "message": f"{len(blast_radius.broken_calls)} functions call into this module.",
                "callers": [str(n) for n, _ in blast_radius.broken_calls[:5]],
            })

        return {
            "safe": blast_radius.risk_level in ("low", "unknown"),
            "risk_level": blast_radius.risk_level,
            "warnings": warnings,
            "recommendation": self._oracle._generate_risk_recommendation(blast_radius),
        }

    async def suggest_test_files(self, target_file: Path) -> List[str]:
        """
        Suggest test files to run based on dependencies.

        Uses the graph to find what tests might be affected.
        """
        file_name = target_file.stem

        # Find test files that depend on target
        dependents = self._oracle.get_dependents(file_name)

        test_files = []
        for dep in dependents:
            if "test" in dep.file_path.lower():
                test_files.append(dep.file_path)

        # Also look for conventional test file names
        test_patterns = [
            f"test_{file_name}.py",
            f"{file_name}_test.py",
            f"tests/test_{file_name}.py",
        ]

        for pattern in test_patterns:
            nodes = self._oracle.find(pattern.replace(".py", ""), fuzzy=True)
            for node in nodes:
                if "test" in node.file_path.lower():
                    test_files.append(node.file_path)

        return list(set(test_files))


# =============================================================================
# CLI FOR TESTING
# =============================================================================

async def main():
    """CLI for testing the Oracle."""
    import argparse

    parser = argparse.ArgumentParser(description="The Oracle - Codebase Knowledge Graph")
    parser.add_argument("command", choices=["index", "find", "blast", "deps", "status"])
    parser.add_argument("target", nargs="?", default=None)
    parser.add_argument("--depth", type=int, default=3)

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%H:%M:%S'
    )

    oracle = get_oracle()

    if args.command == "index":
        await oracle.full_index()
        print(f"\nIndexed {oracle._graph._metrics['total_nodes']} nodes")

    elif args.command == "find":
        if not args.target:
            print("Please specify a target to find")
            return 1

        await oracle.initialize()
        nodes = oracle.find(args.target)

        print(f"\nFound {len(nodes)} matches for '{args.target}':")
        for node in nodes[:10]:
            print(f"  - {node}")

    elif args.command == "blast":
        if not args.target:
            print("Please specify a target for blast radius")
            return 1

        await oracle.initialize()
        radius = oracle.get_blast_radius(args.target)

        print(f"\nBlast Radius for '{args.target}':")
        print(f"  Risk Level: {radius.risk_level.upper()}")
        print(f"  Directly Affected: {len(radius.directly_affected)}")
        print(f"  Transitively Affected: {len(radius.transitively_affected)}")

        if radius.directly_affected:
            print("\n  Directly affected components:")
            for node in list(radius.directly_affected)[:5]:
                print(f"    - {node}")

    elif args.command == "deps":
        if not args.target:
            print("Please specify a target for dependencies")
            return 1

        await oracle.initialize()
        deps = oracle.get_dependencies(args.target)
        dependents = oracle.get_dependents(args.target)

        print(f"\nDependencies of '{args.target}':")
        for dep in deps[:10]:
            print(f"  - {dep}")

        print(f"\nDependents on '{args.target}':")
        for dep in dependents[:10]:
            print(f"  - {dep}")

    elif args.command == "status":
        await oracle.initialize()
        status = oracle.get_status()

        print("\nOracle Status:")
        for key, value in status.items():
            print(f"  {key}: {value}")

    await oracle.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
