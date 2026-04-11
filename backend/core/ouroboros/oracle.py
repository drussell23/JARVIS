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
    ]

    # File types to index
    SUPPORTED_EXTENSIONS = {".py"}  # Start with Python, can extend

    # Graph analysis
    MAX_BLAST_RADIUS_DEPTH = int(os.getenv("ORACLE_BLAST_DEPTH", "5"))
    MAX_CALL_CHAIN_DEPTH = int(os.getenv("ORACLE_CALL_DEPTH", "10"))


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

        self._graph.add_node(
            node_key,
            **node_data.to_dict(),
        )

        # Update indices
        self._node_index[node_key] = node_id
        self._file_index[node_id.file_path].add(node_key)
        self._repo_index[node_id.repo].add(node_key)
        self._type_index[node_id.node_type].add(node_key)

        self._metrics["total_nodes"] = len(self._graph.nodes)

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

        self._graph.add_edge(
            source_key,
            target_key,
            **edge_data.to_dict(),
        )

        self._metrics["total_edges"] = len(self._graph.edges)

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

    def __init__(
        self,
        persist_dir: Optional[Path] = None,
        collection_name: Optional[str] = None,
    ) -> None:
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
            # Use centralized EmbeddingService singleton — prevents multiple
            # SentenceTransformer instances from spawning competing PyTorch/BLAS
            # thread pools, which causes libmalloc heap corruption on macOS ARM64.
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
            self._available = True
            logger.info(
                "[OracleSemanticIndex] Ready (shared EmbeddingService) — "
                "collection '%s' at %s",
                self._collection_name, self._persist_dir,
            )
        except Exception as exc:
            logger.warning(
                "[OracleSemanticIndex] EmbeddingService unavailable: %s; "
                "semantic search disabled",
                exc,
            )

    def is_ready(self) -> bool:
        """Return True if ChromaDB and embedder are both available."""
        return self._available

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
                metadatas: List[Dict[str, Any]] = [
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

            logger.debug("[OracleSemanticIndex] Embedded %d nodes", len(embeddable))
        except Exception as exc:
            logger.warning("[OracleSemanticIndex] embed_nodes failed: %s", exc)

    async def semantic_search(
        self, query: str, k: int = 5
    ) -> List[Tuple[str, float]]:
        """Search for semantically similar code symbols.

        Returns ``("repo:file_path", similarity_score)`` tuples sorted by
        similarity descending.  Deduplicates to unique file paths.
        Returns empty list if not available or on any error.
        """
        if not self._available or self._collection is None or self._embedder is None:
            return []

        try:
            query_embedding = await self._embedder.embed(query)

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
        # concurrent C-extension heap allocation (ChromaDB + pickle).
        # This avoids libmalloc memory corruption on macOS ARM64.
        self._semantic_index: "OracleSemanticIndex" = None  # type: ignore[assignment]

        logger.info("The Oracle initialized")

    async def initialize(self) -> bool:
        """Initialize the Oracle, loading cached data if available."""
        logger.info("Initializing The Oracle...")

        # Try to load cached graph (synchronous to avoid libmalloc crash)
        if await self._load_cache():
            logger.info(f"Loaded cached graph: {self._graph._metrics['total_nodes']} nodes, "
                       f"{self._graph._metrics['total_edges']} edges")
        else:
            # No cache, do full index
            logger.info("No cache found, performing full index...")
            await self.full_index()

        # Initialize semantic index AFTER graph loading to prevent
        # concurrent C-extension heap allocation (ChromaDB + pickle).
        if self._semantic_index is None:
            self._semantic_index = OracleSemanticIndex()

        self._running = True
        self._last_indexed_monotonic_ns = time.monotonic_ns()
        return True

    async def shutdown(self) -> None:
        """Shutdown the Oracle, saving cache."""
        logger.info("Shutting down The Oracle...")
        await self._save_cache()
        self._running = False
        logger.info("The Oracle shutdown complete")

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

        await self._save_cache()

        # Embed all nodes into semantic index (fault-isolated)
        try:
            all_nodes = self._graph.get_all_nodes()
            await self._semantic_index.embed_nodes(all_nodes)
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
            await self._semantic_index.embed_nodes(changed_nodes)
        except Exception as exc:
            logger.warning("[Oracle] Semantic embedding after incremental_update failed: %s", exc)

        elapsed = time.time() - start_time
        logger.info(f"Incremental update complete in {elapsed:.2f}s")

    async def _index_repository(self, repo_name: str, repo_path: Path) -> None:
        """Index all Python files in a repository."""
        logger.info(f"Indexing repository: {repo_name} at {repo_path}")

        # Find all Python files
        python_files = await self._find_python_files(repo_path)
        logger.info(f"Found {len(python_files)} Python files in {repo_name}")

        # Process files in parallel batches
        batch_size = OracleConfig.MAX_PARALLEL_FILES
        for i in range(0, len(python_files), batch_size):
            batch = python_files[i:i + batch_size]
            tasks = [
                self._index_file(repo_name, repo_path, file_path)
                for file_path in batch
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        self._graph._metrics["files_indexed"] = len(python_files)

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
                        scan_dir(item)
                    elif item.suffix in OracleConfig.SUPPORTED_EXTENSIONS:
                        python_files.append(item)
            except PermissionError:
                pass

        await asyncio.to_thread(scan_dir, root)
        return python_files

    async def _index_file(self, repo_name: str, repo_path: Path, file_path: Path) -> None:
        """Index a single Python file."""
        try:
            # Read file content
            content = await asyncio.to_thread(file_path.read_text, encoding="utf-8")

            # Calculate hash for change detection
            content_hash = hashlib.md5(content.encode()).hexdigest()
            relative_path = str(file_path.relative_to(repo_path))

            # Skip if unchanged
            cache_key = f"{repo_name}:{relative_path}"
            if cache_key in self._file_hashes and self._file_hashes[cache_key] == content_hash:
                return

            self._file_hashes[cache_key] = content_hash

            # Parse AST
            try:
                tree = ast.parse(content, filename=str(file_path))
            except SyntaxError as e:
                logger.warning(f"Syntax error in {file_path}: {e}")
                return

            # Extract structure
            visitor = CodeStructureVisitor(repo_name, relative_path, content)
            visitor.visit(tree)

            # Add to graph
            for node_data in visitor.nodes:
                self._graph.add_node(node_data)

            for source, target, edge_data in visitor.edges:
                self._graph.add_edge(source, target, edge_data)

        except Exception as e:
            logger.warning(f"Error indexing {file_path}: {e}")

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
        """Scan repository for changed files."""
        python_files = await self._find_python_files(repo_path)

        for file_path in python_files:
            try:
                content = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
                content_hash = hashlib.md5(content.encode()).hexdigest()
                relative_path = str(file_path.relative_to(repo_path))
                cache_key = f"{repo_name}:{relative_path}"

                if cache_key not in self._file_hashes or self._file_hashes[cache_key] != content_hash:
                    await self._index_file(repo_name, repo_path, file_path)
            except Exception as e:
                logger.warning(f"Error scanning {file_path}: {e}")

    async def _load_cache(self) -> bool:
        """Load cached graph from disk.

        Loads synchronously (not asyncio.to_thread) to prevent concurrent
        C-extension heap allocation with ChromaDB/torch/numpy.  At boot
        time nothing else needs the event loop — blocking for 2-5s is
        acceptable to avoid libmalloc memory corruption on macOS ARM64.

        Note: pickle is used here for internal cache only (never untrusted
        data) — the cache file is written by this same process.
        """
        try:
            if OracleConfig.GRAPH_CACHE_FILE.exists():
                _raw = OracleConfig.GRAPH_CACHE_FILE.read_bytes()
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
        """Save graph to cache on disk.

        Serializes synchronously to avoid concurrent heap allocation
        with C extensions (same rationale as _load_cache).
        Uses highest available protocol for better ARM64 alignment.

        Note: pickle is used here for internal cache only — the graph
        contains only our own dataclasses, not untrusted data.

        Iron Gate compliance: writes to ``~/.jarvis/oracle/`` may be blocked
        by the sandbox; ``sandbox_fallback`` routes to
        ``.ouroboros/state/sandbox_fallback/oracle/`` without lowering shields.
        """
        # Lazy import avoids any risk of circular deps at module init.
        from backend.core.ouroboros.governance.sandbox_paths import sandbox_fallback

        try:
            data = {
                "graph": self._graph._graph,
                "node_index": self._graph._node_index,
                "file_index": dict(self._graph._file_index),
                "repo_index": dict(self._graph._repo_index),
                "type_index": dict(self._graph._type_index),
                "metrics": self._graph._metrics,
                "file_hashes": self._file_hashes,
            }

            cache_path = sandbox_fallback(OracleConfig.GRAPH_CACHE_FILE)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(
                pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL),  # noqa: S301
            )

            logger.info(f"Saved cache to {cache_path}")
        except Exception as e:
            logger.error(f"Error saving cache: {e}")

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
            "cache_file": str(OracleConfig.GRAPH_CACHE_FILE),
            "cache_exists": OracleConfig.GRAPH_CACHE_FILE.exists(),
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
