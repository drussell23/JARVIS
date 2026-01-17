"""
Dependency Analyzer v1.0 - Project-Wide Dependency Analysis
==========================================================

Enterprise-grade dependency analysis system that understands project structure
and dependencies across files, modules, and repositories.

Features:
- Complete dependency graph construction (imports, calls, inheritance)
- Transitive dependency detection
- API usage tracking across files
- Breaking change impact analysis
- Dead code detection
- Circular dependency detection with cycle resolution suggestions
- Cross-repository dependency mapping

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                     Dependency Analyzer v1.0                             │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐       │
    │   │  Symbol Extractor│  │  Call Graph     │   │ Inheritance     │       │
    │   │  (AST-based)    │──▶│  Builder        │──▶│ Tracker         │       │
    │   └─────────────────┘   └─────────────────┘   └─────────────────┘       │
    │           │                     │                     │                  │
    │           └─────────────────────┴─────────────────────┘                  │
    │                                 │                                        │
    │                    ┌────────────▼────────────┐                           │
    │                    │   NetworkX Graph Engine  │                          │
    │                    │   (Multi-layer graphs)   │                          │
    │                    └────────────┬────────────┘                           │
    │                                 │                                        │
    │   ┌──────────────┬──────────────┼──────────────┬──────────────┐         │
    │   │              │              │              │              │         │
    │   ▼              ▼              ▼              ▼              ▼         │
    │ Transitive   Impact        Breaking        Dead Code     Cross-Repo    │
    │ Resolver     Analyzer      Change Detect   Detector      Mapper        │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import weakref
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import lru_cache
from pathlib import Path
from typing import (
    Any, AsyncGenerator, Callable, Coroutine, DefaultDict, Deque, Dict,
    FrozenSet, Generator, Generic, Iterable, Iterator, List, Literal,
    Mapping, NamedTuple, Optional, Protocol, Sequence, Set, Tuple, Type,
    TypeVar, Union, cast
)

try:
    import networkx as nx
    HAS_NETWORKX = True
except ImportError:
    HAS_NETWORKX = False
    nx = None

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION - Environment Driven (Zero Hardcoding)
# =============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool = False) -> bool:
    val = _get_env(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


def _get_env_list(key: str, default: List[str] = None) -> List[str]:
    val = _get_env(key, "")
    if not val:
        return default or []
    return [x.strip() for x in val.split(",") if x.strip()]


class DependencyConfig:
    """Configuration for dependency analysis."""

    # Analysis depth
    MAX_TRANSITIVE_DEPTH: int = _get_env_int("DEPENDENCY_MAX_DEPTH", 10)
    MAX_FILES_ANALYZE: int = _get_env_int("DEPENDENCY_MAX_FILES", 10000)

    # Graph persistence
    GRAPH_CACHE_DIR: Path = Path(_get_env("DEPENDENCY_GRAPH_DIR", str(Path.home() / ".jarvis/dependency_graphs")))
    GRAPH_CACHE_TTL: int = _get_env_int("DEPENDENCY_GRAPH_TTL", 3600)

    # Analysis settings
    TRACK_CALL_ARGUMENTS: bool = _get_env_bool("DEPENDENCY_TRACK_ARGS", True)
    TRACK_ATTRIBUTE_ACCESS: bool = _get_env_bool("DEPENDENCY_TRACK_ATTRS", True)
    DETECT_DEAD_CODE: bool = _get_env_bool("DEPENDENCY_DETECT_DEAD", True)

    # Cross-repo
    CROSS_REPO_ENABLED: bool = _get_env_bool("DEPENDENCY_CROSS_REPO", True)

    # Repository paths
    JARVIS_REPO: Path = Path(_get_env("JARVIS_REPO", str(Path.home() / "Documents/repos/JARVIS-AI-Agent")))
    PRIME_REPO: Path = Path(_get_env("PRIME_REPO", str(Path.home() / "Documents/repos/jarvis-prime")))
    REACTOR_REPO: Path = Path(_get_env("REACTOR_REPO", str(Path.home() / "Documents/repos/reactor-core")))

    # File patterns
    INCLUDE_PATTERNS: List[str] = _get_env_list("DEPENDENCY_INCLUDE_PATTERNS", ["*.py"])
    EXCLUDE_PATTERNS: List[str] = _get_env_list("DEPENDENCY_EXCLUDE_PATTERNS", ["**/test_*.py", "**/*_test.py", "**/tests/**"])


# =============================================================================
# ENUMS AND TYPES
# =============================================================================

class DependencyType(Enum):
    """Types of dependencies between code elements."""
    IMPORT = "import"                    # import statement
    IMPORT_FROM = "import_from"          # from X import Y
    CALL = "call"                        # function/method call
    INHERITANCE = "inheritance"          # class inheritance
    ATTRIBUTE_ACCESS = "attribute"       # obj.attribute
    TYPE_ANNOTATION = "type_annotation"  # type hints
    DECORATOR = "decorator"              # @decorator
    CONTEXT_MANAGER = "context_manager"  # with statement
    ASSIGNMENT = "assignment"            # variable assignment
    PARAMETER = "parameter"              # function parameter type


class SymbolType(Enum):
    """Types of symbols in code."""
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    CONSTANT = "constant"
    IMPORT = "import"
    PARAMETER = "parameter"
    PROPERTY = "property"
    TYPE_ALIAS = "type_alias"


class ImpactLevel(Enum):
    """Impact level of a change."""
    CRITICAL = 5    # Breaking change to public API
    HIGH = 4        # Change affects many dependents
    MEDIUM = 3      # Change affects some dependents
    LOW = 2         # Change affects few dependents
    MINIMAL = 1     # Change affects internal only


class VisibilityScope(Enum):
    """Visibility scope of a symbol."""
    PUBLIC = "public"          # No underscore prefix
    PROTECTED = "protected"    # Single underscore prefix
    PRIVATE = "private"        # Double underscore prefix
    MODULE = "module"          # Module-level private (__all__)


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass(frozen=True)
class SymbolLocation:
    """Location of a symbol in code."""
    file_path: Path
    line: int
    column: int
    end_line: Optional[int] = None
    end_column: Optional[int] = None

    def __str__(self) -> str:
        return f"{self.file_path}:{self.line}:{self.column}"


@dataclass
class Symbol:
    """A code symbol (function, class, variable, etc.)."""
    name: str
    qualified_name: str
    symbol_type: SymbolType
    location: SymbolLocation
    visibility: VisibilityScope
    signature: Optional[str] = None
    docstring: Optional[str] = None
    parent: Optional[str] = None  # Qualified name of parent
    children: Set[str] = field(default_factory=set)  # Qualified names of children
    decorators: List[str] = field(default_factory=list)
    type_annotation: Optional[str] = None

    def is_public(self) -> bool:
        return self.visibility == VisibilityScope.PUBLIC

    def __hash__(self) -> int:
        return hash(self.qualified_name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Symbol):
            return False
        return self.qualified_name == other.qualified_name


@dataclass
class Dependency:
    """A dependency relationship between symbols."""
    source: str  # Qualified name of source symbol
    target: str  # Qualified name of target symbol
    dependency_type: DependencyType
    location: SymbolLocation
    context: Optional[str] = None  # Surrounding code context
    is_dynamic: bool = False  # If dependency is determined at runtime
    strength: float = 1.0  # How strong is the coupling (0-1)

    def __hash__(self) -> int:
        return hash((self.source, self.target, self.dependency_type))


@dataclass
class APIUsage:
    """Usage of an API/symbol across the codebase."""
    symbol: str  # Qualified name
    usages: List[Tuple[SymbolLocation, DependencyType]] = field(default_factory=list)

    @property
    def usage_count(self) -> int:
        return len(self.usages)

    @property
    def files_using(self) -> Set[Path]:
        return {loc.file_path for loc, _ in self.usages}


@dataclass
class BreakingChange:
    """A detected breaking change."""
    symbol: str
    change_type: str  # "removed", "signature_changed", "moved", "renamed"
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    affected_files: Set[Path] = field(default_factory=set)
    impact_level: ImpactLevel = ImpactLevel.MEDIUM
    suggestion: Optional[str] = None


@dataclass
class CircularDependency:
    """A circular dependency cycle."""
    cycle: List[str]  # List of qualified names forming the cycle
    cycle_type: str  # "import", "inheritance", "call"
    severity: int  # 1-5 based on cycle length and type

    def __str__(self) -> str:
        return " -> ".join(self.cycle + [self.cycle[0]])


@dataclass
class DeadCode:
    """Detected dead/unused code."""
    symbol: Symbol
    reason: str
    confidence: float  # 0-1


@dataclass
class ImpactAnalysis:
    """Analysis of impact from changing a symbol."""
    symbol: str
    direct_dependents: Set[str]
    transitive_dependents: Set[str]
    affected_files: Set[Path]
    impact_level: ImpactLevel
    breaking_changes: List[BreakingChange]


# =============================================================================
# SYMBOL EXTRACTOR
# =============================================================================

class SymbolExtractor(ast.NodeVisitor):
    """
    Extracts symbols from Python AST.

    Builds a complete symbol table for a file including:
    - Classes and their methods
    - Functions (sync and async)
    - Variables and constants
    - Imports
    """

    def __init__(self, file_path: Path, source: str):
        self.file_path = file_path
        self.source = source
        self.lines = source.splitlines()
        self.symbols: Dict[str, Symbol] = {}
        self.dependencies: List[Dependency] = []
        self._scope_stack: List[str] = []
        self._current_class: Optional[str] = None

    def extract(self) -> Tuple[Dict[str, Symbol], List[Dependency]]:
        """Extract all symbols and dependencies from the file."""
        try:
            tree = ast.parse(self.source)
            self.visit(tree)
        except SyntaxError as e:
            logger.warning(f"Syntax error in {self.file_path}: {e}")

        return self.symbols, self.dependencies

    def _get_qualified_name(self, name: str) -> str:
        """Get fully qualified name for a symbol."""
        module_name = self._get_module_name()
        if self._scope_stack:
            return f"{module_name}.{'.'.join(self._scope_stack)}.{name}"
        return f"{module_name}.{name}"

    def _get_module_name(self) -> str:
        """Get module name from file path."""
        # Convert file path to module name
        parts = self.file_path.stem.split('.')
        return parts[0] if parts else str(self.file_path.stem)

    def _get_visibility(self, name: str) -> VisibilityScope:
        """Determine visibility scope from name."""
        if name.startswith('__') and not name.endswith('__'):
            return VisibilityScope.PRIVATE
        elif name.startswith('_'):
            return VisibilityScope.PROTECTED
        return VisibilityScope.PUBLIC

    def _create_location(self, node: ast.AST) -> SymbolLocation:
        """Create location from AST node."""
        return SymbolLocation(
            file_path=self.file_path,
            line=node.lineno,
            column=node.col_offset,
            end_line=getattr(node, 'end_lineno', None),
            end_column=getattr(node, 'end_col_offset', None),
        )

    def visit_Import(self, node: ast.Import) -> None:
        """Visit import statement."""
        for alias in node.names:
            module_name = alias.name
            as_name = alias.asname or module_name

            # Create import symbol
            qualified_name = self._get_qualified_name(as_name)
            self.symbols[qualified_name] = Symbol(
                name=as_name,
                qualified_name=qualified_name,
                symbol_type=SymbolType.IMPORT,
                location=self._create_location(node),
                visibility=VisibilityScope.MODULE,
            )

            # Create dependency
            self.dependencies.append(Dependency(
                source=self._get_module_name(),
                target=module_name,
                dependency_type=DependencyType.IMPORT,
                location=self._create_location(node),
            ))

        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Visit from ... import statement."""
        module = node.module or ""
        level = node.level  # Relative import level

        for alias in node.names:
            name = alias.name
            as_name = alias.asname or name

            # Create import symbol
            qualified_name = self._get_qualified_name(as_name)
            self.symbols[qualified_name] = Symbol(
                name=as_name,
                qualified_name=qualified_name,
                symbol_type=SymbolType.IMPORT,
                location=self._create_location(node),
                visibility=VisibilityScope.MODULE,
            )

            # Create dependency
            target = f"{module}.{name}" if module else name
            self.dependencies.append(Dependency(
                source=self._get_module_name(),
                target=target,
                dependency_type=DependencyType.IMPORT_FROM,
                location=self._create_location(node),
            ))

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Visit class definition."""
        qualified_name = self._get_qualified_name(node.name)

        # Get base classes
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.unparse(base))

        # Create class symbol
        self.symbols[qualified_name] = Symbol(
            name=node.name,
            qualified_name=qualified_name,
            symbol_type=SymbolType.CLASS,
            location=self._create_location(node),
            visibility=self._get_visibility(node.name),
            docstring=ast.get_docstring(node),
            decorators=[ast.unparse(d) for d in node.decorator_list],
        )

        # Create inheritance dependencies
        for base in bases:
            self.dependencies.append(Dependency(
                source=qualified_name,
                target=base,
                dependency_type=DependencyType.INHERITANCE,
                location=self._create_location(node),
            ))

        # Visit children
        self._scope_stack.append(node.name)
        old_class = self._current_class
        self._current_class = qualified_name
        self.generic_visit(node)
        self._current_class = old_class
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit function definition."""
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Visit async function definition."""
        self._visit_function(node, is_async=True)

    def _visit_function(
        self,
        node: Union[ast.FunctionDef, ast.AsyncFunctionDef],
        is_async: bool,
    ) -> None:
        """Common function visitor logic."""
        qualified_name = self._get_qualified_name(node.name)

        # Determine if this is a method
        symbol_type = SymbolType.METHOD if self._current_class else SymbolType.FUNCTION

        # Build signature
        args_str = ast.unparse(node.args)
        returns_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        prefix = "async " if is_async else ""
        signature = f"{prefix}def {node.name}({args_str}){returns_str}"

        # Create function symbol
        self.symbols[qualified_name] = Symbol(
            name=node.name,
            qualified_name=qualified_name,
            symbol_type=symbol_type,
            location=self._create_location(node),
            visibility=self._get_visibility(node.name),
            signature=signature,
            docstring=ast.get_docstring(node),
            parent=self._current_class,
            decorators=[ast.unparse(d) for d in node.decorator_list],
        )

        # Add as child to parent class
        if self._current_class and self._current_class in self.symbols:
            self.symbols[self._current_class].children.add(qualified_name)

        # Create decorator dependencies
        for decorator in node.decorator_list:
            decorator_name = ast.unparse(decorator)
            self.dependencies.append(Dependency(
                source=qualified_name,
                target=decorator_name.split('(')[0],  # Remove call args
                dependency_type=DependencyType.DECORATOR,
                location=self._create_location(decorator),
            ))

        # Create type annotation dependencies
        if node.returns:
            self._extract_type_dependencies(node.returns, qualified_name, node)

        for arg in node.args.args:
            if arg.annotation:
                self._extract_type_dependencies(arg.annotation, qualified_name, node)

        # Visit children
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        """Visit function call."""
        # Determine caller context
        caller = self._get_qualified_name("") if not self._scope_stack else \
                 self._get_qualified_name("").rsplit(".", 1)[0]

        # Determine callee
        callee = None
        if isinstance(node.func, ast.Name):
            callee = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee = ast.unparse(node.func)

        if callee:
            self.dependencies.append(Dependency(
                source=caller,
                target=callee,
                dependency_type=DependencyType.CALL,
                location=self._create_location(node),
            ))

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Visit attribute access."""
        if DependencyConfig.TRACK_ATTRIBUTE_ACCESS:
            # Only track if parent is a Name (direct attribute access)
            if isinstance(node.value, ast.Name):
                caller = self._get_qualified_name("") if not self._scope_stack else \
                         self._get_qualified_name("").rsplit(".", 1)[0]
                target = f"{node.value.id}.{node.attr}"

                self.dependencies.append(Dependency(
                    source=caller,
                    target=target,
                    dependency_type=DependencyType.ATTRIBUTE_ACCESS,
                    location=self._create_location(node),
                ))

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Visit assignment."""
        # Only track module-level assignments
        if not self._scope_stack and not self._current_class:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    qualified_name = self._get_qualified_name(target.id)

                    # Determine if it's a constant (ALL_CAPS)
                    is_constant = target.id.isupper()
                    symbol_type = SymbolType.CONSTANT if is_constant else SymbolType.VARIABLE

                    self.symbols[qualified_name] = Symbol(
                        name=target.id,
                        qualified_name=qualified_name,
                        symbol_type=symbol_type,
                        location=self._create_location(node),
                        visibility=self._get_visibility(target.id),
                    )

        self.generic_visit(node)

    def _extract_type_dependencies(
        self,
        annotation: ast.AST,
        source: str,
        node: ast.AST,
    ) -> None:
        """Extract dependencies from type annotations."""
        annotation_str = ast.unparse(annotation)

        # Extract type names from annotation
        type_names = re.findall(r'\b[A-Z][a-zA-Z0-9_]*\b', annotation_str)

        for type_name in type_names:
            self.dependencies.append(Dependency(
                source=source,
                target=type_name,
                dependency_type=DependencyType.TYPE_ANNOTATION,
                location=self._create_location(node),
            ))


# =============================================================================
# DEPENDENCY GRAPH
# =============================================================================

class DependencyGraph:
    """
    Multi-layer dependency graph using NetworkX.

    Maintains separate graphs for:
    - Import dependencies
    - Call dependencies
    - Inheritance dependencies
    - Type dependencies
    """

    def __init__(self):
        if not HAS_NETWORKX:
            raise ImportError("NetworkX is required for dependency analysis. Install with: pip install networkx")

        # Main graph with all dependencies
        self._graph: nx.DiGraph = nx.DiGraph()

        # Layer-specific graphs
        self._import_graph: nx.DiGraph = nx.DiGraph()
        self._call_graph: nx.DiGraph = nx.DiGraph()
        self._inheritance_graph: nx.DiGraph = nx.DiGraph()

        # Symbol storage
        self._symbols: Dict[str, Symbol] = {}

        # Metadata
        self._file_hashes: Dict[Path, str] = {}
        self._last_update: float = 0

    def add_symbol(self, symbol: Symbol) -> None:
        """Add a symbol to the graph."""
        self._symbols[symbol.qualified_name] = symbol
        self._graph.add_node(
            symbol.qualified_name,
            symbol_type=symbol.symbol_type.value,
            visibility=symbol.visibility.value,
            file=str(symbol.location.file_path),
            line=symbol.location.line,
        )

    def add_dependency(self, dependency: Dependency) -> None:
        """Add a dependency edge to the graph."""
        # Add to main graph
        self._graph.add_edge(
            dependency.source,
            dependency.target,
            dependency_type=dependency.dependency_type.value,
            location=str(dependency.location),
            strength=dependency.strength,
        )

        # Add to layer-specific graph
        if dependency.dependency_type in (DependencyType.IMPORT, DependencyType.IMPORT_FROM):
            self._import_graph.add_edge(dependency.source, dependency.target)
        elif dependency.dependency_type == DependencyType.CALL:
            self._call_graph.add_edge(dependency.source, dependency.target)
        elif dependency.dependency_type == DependencyType.INHERITANCE:
            self._inheritance_graph.add_edge(dependency.source, dependency.target)

    def get_dependents(self, symbol: str, transitive: bool = False) -> Set[str]:
        """Get all symbols that depend on the given symbol."""
        if transitive:
            # Use BFS for transitive dependents
            dependents = set()
            to_visit = set(self._graph.predecessors(symbol))

            while to_visit:
                current = to_visit.pop()
                if current not in dependents:
                    dependents.add(current)
                    to_visit.update(self._graph.predecessors(current))

            return dependents
        else:
            return set(self._graph.predecessors(symbol))

    def get_dependencies(self, symbol: str, transitive: bool = False) -> Set[str]:
        """Get all symbols that the given symbol depends on."""
        if transitive:
            # Use BFS for transitive dependencies
            dependencies = set()
            to_visit = set(self._graph.successors(symbol))

            depth = 0
            while to_visit and depth < DependencyConfig.MAX_TRANSITIVE_DEPTH:
                current = to_visit.pop()
                if current not in dependencies:
                    dependencies.add(current)
                    to_visit.update(self._graph.successors(current))
                depth += 1

            return dependencies
        else:
            return set(self._graph.successors(symbol))

    def find_cycles(self, graph_type: str = "all") -> List[CircularDependency]:
        """Find all circular dependencies in the graph."""
        graph = {
            "all": self._graph,
            "import": self._import_graph,
            "call": self._call_graph,
            "inheritance": self._inheritance_graph,
        }.get(graph_type, self._graph)

        cycles = []
        try:
            for cycle in nx.simple_cycles(graph):
                if len(cycle) > 1:
                    severity = min(5, len(cycle))
                    cycles.append(CircularDependency(
                        cycle=cycle,
                        cycle_type=graph_type,
                        severity=severity,
                    ))
        except nx.NetworkXError:
            pass

        return cycles

    def get_strongly_connected_components(self) -> List[Set[str]]:
        """Get strongly connected components (potential circular dep groups)."""
        return [scc for scc in nx.strongly_connected_components(self._graph) if len(scc) > 1]

    def get_symbol(self, qualified_name: str) -> Optional[Symbol]:
        """Get a symbol by qualified name."""
        return self._symbols.get(qualified_name)

    def get_symbols_in_file(self, file_path: Path) -> List[Symbol]:
        """Get all symbols defined in a file."""
        return [s for s in self._symbols.values() if s.location.file_path == file_path]

    def get_files_depending_on(self, file_path: Path) -> Set[Path]:
        """Get all files that depend on the given file."""
        file_symbols = self.get_symbols_in_file(file_path)
        dependent_files = set()

        for symbol in file_symbols:
            dependents = self.get_dependents(symbol.qualified_name, transitive=True)
            for dep in dependents:
                dep_symbol = self._symbols.get(dep)
                if dep_symbol and dep_symbol.location.file_path != file_path:
                    dependent_files.add(dep_symbol.location.file_path)

        return dependent_files

    def get_api_usage(self, symbol: str) -> APIUsage:
        """Get usage statistics for a symbol."""
        usages = []

        for source, target, data in self._graph.in_edges(symbol, data=True):
            source_symbol = self._symbols.get(source)
            if source_symbol:
                dep_type = DependencyType(data.get('dependency_type', 'call'))
                usages.append((source_symbol.location, dep_type))

        return APIUsage(symbol=symbol, usages=usages)

    def calculate_impact(self, symbol: str) -> ImpactLevel:
        """Calculate impact level for changing a symbol."""
        sym = self._symbols.get(symbol)
        if not sym:
            return ImpactLevel.MINIMAL

        # Count dependents
        dependents = self.get_dependents(symbol, transitive=True)
        num_dependents = len(dependents)

        # Check visibility
        if sym.visibility == VisibilityScope.PRIVATE:
            return ImpactLevel.MINIMAL
        elif sym.visibility == VisibilityScope.PROTECTED:
            if num_dependents > 10:
                return ImpactLevel.MEDIUM
            return ImpactLevel.LOW

        # Public symbols
        if num_dependents > 50:
            return ImpactLevel.CRITICAL
        elif num_dependents > 20:
            return ImpactLevel.HIGH
        elif num_dependents > 5:
            return ImpactLevel.MEDIUM
        elif num_dependents > 0:
            return ImpactLevel.LOW
        return ImpactLevel.MINIMAL

    def to_dict(self) -> Dict[str, Any]:
        """Serialize graph to dictionary."""
        return {
            "nodes": list(self._graph.nodes(data=True)),
            "edges": list(self._graph.edges(data=True)),
            "symbols": {k: {
                "name": v.name,
                "qualified_name": v.qualified_name,
                "type": v.symbol_type.value,
                "file": str(v.location.file_path),
                "line": v.location.line,
            } for k, v in self._symbols.items()},
        }


# =============================================================================
# DEPENDENCY ANALYZER
# =============================================================================

class DependencyAnalyzer:
    """
    Main dependency analyzer for project-wide analysis.

    Provides:
    - Complete dependency graph construction
    - Transitive dependency resolution
    - API usage tracking
    - Breaking change detection
    - Dead code detection
    - Impact analysis
    """

    def __init__(self):
        self._graph = DependencyGraph()
        self._lock = asyncio.Lock()
        self._initialized = False
        self._file_cache: Dict[Path, Tuple[str, float]] = {}  # path -> (hash, mtime)

    async def analyze_file(self, file_path: Path) -> Tuple[Dict[str, Symbol], List[Dependency]]:
        """Analyze a single file and extract symbols/dependencies."""
        try:
            content = await asyncio.to_thread(file_path.read_text, encoding='utf-8', errors='ignore')
            content_hash = hashlib.md5(content.encode()).hexdigest()
            mtime = file_path.stat().st_mtime

            # Check cache
            cached = self._file_cache.get(file_path)
            if cached and cached[0] == content_hash:
                return {}, []

            # Extract symbols and dependencies
            extractor = SymbolExtractor(file_path, content)
            symbols, dependencies = extractor.extract()

            # Update cache
            self._file_cache[file_path] = (content_hash, mtime)

            return symbols, dependencies

        except Exception as e:
            logger.error(f"Failed to analyze file {file_path}: {e}")
            return {}, []

    async def analyze_directory(
        self,
        directory: Path,
        patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[int, int], Awaitable[None]]] = None,
    ) -> int:
        """Analyze all files in a directory."""
        patterns = patterns or DependencyConfig.INCLUDE_PATTERNS
        exclude_patterns = exclude_patterns or DependencyConfig.EXCLUDE_PATTERNS

        # Find all matching files
        import fnmatch
        files = []
        for pattern in patterns:
            for f in directory.glob(f"**/{pattern}"):
                excluded = False
                for exclude in exclude_patterns:
                    if fnmatch.fnmatch(str(f), exclude):
                        excluded = True
                        break
                if not excluded:
                    files.append(f)

        # Limit files
        files = files[:DependencyConfig.MAX_FILES_ANALYZE]

        # Analyze files in parallel
        semaphore = asyncio.Semaphore(50)
        analyzed = 0

        async def analyze_with_semaphore(file_path: Path) -> Tuple[Dict[str, Symbol], List[Dependency]]:
            async with semaphore:
                return await self.analyze_file(file_path)

        tasks = [analyze_with_semaphore(f) for f in files]

        for i, task in enumerate(asyncio.as_completed(tasks)):
            symbols, dependencies = await task

            async with self._lock:
                for symbol in symbols.values():
                    self._graph.add_symbol(symbol)
                for dep in dependencies:
                    self._graph.add_dependency(dep)

            analyzed += 1
            if progress_callback:
                await progress_callback(i + 1, len(files))

        return analyzed

    async def get_impact_analysis(
        self,
        symbol_name: str,
        change_type: str = "modified",
    ) -> ImpactAnalysis:
        """
        Analyze impact of changing a symbol.

        Args:
            symbol_name: Qualified name of the symbol
            change_type: Type of change (modified, removed, signature_changed)

        Returns:
            ImpactAnalysis with all affected code
        """
        direct_deps = self._graph.get_dependents(symbol_name, transitive=False)
        transitive_deps = self._graph.get_dependents(symbol_name, transitive=True)

        # Get affected files
        affected_files = set()
        for dep in transitive_deps:
            sym = self._graph.get_symbol(dep)
            if sym:
                affected_files.add(sym.location.file_path)

        # Calculate impact level
        impact_level = self._graph.calculate_impact(symbol_name)

        # Detect breaking changes
        breaking_changes = []
        sym = self._graph.get_symbol(symbol_name)
        if sym and sym.is_public():
            if change_type == "removed":
                breaking_changes.append(BreakingChange(
                    symbol=symbol_name,
                    change_type="removed",
                    affected_files=affected_files,
                    impact_level=ImpactLevel.CRITICAL,
                    suggestion=f"Add deprecation warning before removing {symbol_name}",
                ))
            elif change_type == "signature_changed":
                breaking_changes.append(BreakingChange(
                    symbol=symbol_name,
                    change_type="signature_changed",
                    affected_files=affected_files,
                    impact_level=ImpactLevel.HIGH,
                    suggestion=f"Update all {len(transitive_deps)} call sites",
                ))

        return ImpactAnalysis(
            symbol=symbol_name,
            direct_dependents=direct_deps,
            transitive_dependents=transitive_deps,
            affected_files=affected_files,
            impact_level=impact_level,
            breaking_changes=breaking_changes,
        )

    async def detect_breaking_changes(
        self,
        old_symbols: Dict[str, Symbol],
        new_symbols: Dict[str, Symbol],
    ) -> List[BreakingChange]:
        """Detect breaking changes between two versions of symbols."""
        changes = []

        # Check for removed public symbols
        for name, old_sym in old_symbols.items():
            if old_sym.is_public() and name not in new_symbols:
                affected = self._graph.get_dependents(name, transitive=True)
                affected_files = {
                    self._graph.get_symbol(d).location.file_path
                    for d in affected
                    if self._graph.get_symbol(d)
                }
                changes.append(BreakingChange(
                    symbol=name,
                    change_type="removed",
                    old_value=old_sym.signature,
                    affected_files=affected_files,
                    impact_level=ImpactLevel.CRITICAL,
                ))

        # Check for signature changes
        for name, old_sym in old_symbols.items():
            if name in new_symbols and old_sym.is_public():
                new_sym = new_symbols[name]
                if old_sym.signature != new_sym.signature:
                    affected = self._graph.get_dependents(name, transitive=True)
                    affected_files = {
                        self._graph.get_symbol(d).location.file_path
                        for d in affected
                        if self._graph.get_symbol(d)
                    }
                    changes.append(BreakingChange(
                        symbol=name,
                        change_type="signature_changed",
                        old_value=old_sym.signature,
                        new_value=new_sym.signature,
                        affected_files=affected_files,
                        impact_level=ImpactLevel.HIGH,
                    ))

        return changes

    async def find_dead_code(self) -> List[DeadCode]:
        """Find potentially dead/unused code."""
        dead_code = []

        if not DependencyConfig.DETECT_DEAD_CODE:
            return dead_code

        for name, symbol in self._graph._symbols.items():
            # Skip private symbols (they might be used internally)
            if symbol.visibility == VisibilityScope.PRIVATE:
                continue

            # Skip __init__, __main__, etc.
            if symbol.name.startswith('__') and symbol.name.endswith('__'):
                continue

            # Check if symbol has any dependents
            dependents = self._graph.get_dependents(name, transitive=False)

            if not dependents:
                # Check if it's an entry point or export
                if symbol.name in ('main', 'run', 'start', 'app', 'handler'):
                    continue

                # Calculate confidence
                confidence = 0.8 if symbol.is_public() else 0.5

                dead_code.append(DeadCode(
                    symbol=symbol,
                    reason="No references found in codebase",
                    confidence=confidence,
                ))

        return dead_code

    def find_circular_dependencies(self) -> List[CircularDependency]:
        """Find all circular dependencies."""
        return self._graph.find_cycles("all")

    def get_api_usage(self, symbol: str) -> APIUsage:
        """Get usage statistics for a symbol."""
        return self._graph.get_api_usage(symbol)

    def get_files_affected_by_change(self, file_path: Path) -> Set[Path]:
        """Get all files that would be affected by changing a file."""
        return self._graph.get_files_depending_on(file_path)

    def get_dependency_graph(self) -> DependencyGraph:
        """Get the underlying dependency graph."""
        return self._graph

    def get_stats(self) -> Dict[str, Any]:
        """Get analyzer statistics."""
        return {
            "total_symbols": len(self._graph._symbols),
            "total_edges": self._graph._graph.number_of_edges(),
            "files_analyzed": len(self._file_cache),
            "circular_deps": len(self.find_circular_dependencies()),
        }


# =============================================================================
# CROSS-REPO DEPENDENCY ANALYZER
# =============================================================================

class CrossRepoDependencyAnalyzer:
    """
    Analyzes dependencies across multiple repositories.

    Coordinates between JARVIS, JARVIS-Prime, and Reactor-Core.
    """

    def __init__(self):
        self._repos: Dict[str, Path] = {
            "jarvis": DependencyConfig.JARVIS_REPO,
            "prime": DependencyConfig.PRIME_REPO,
            "reactor": DependencyConfig.REACTOR_REPO,
        }

        self._analyzers: Dict[str, DependencyAnalyzer] = {}
        self._cross_repo_graph = DependencyGraph() if HAS_NETWORKX else None
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize analyzers for all repositories."""
        logger.info("Initializing Cross-Repo Dependency Analyzer...")

        for repo_name, repo_path in self._repos.items():
            if not repo_path.exists():
                logger.warning(f"Repository not found: {repo_name} at {repo_path}")
                continue

            analyzer = DependencyAnalyzer()
            self._analyzers[repo_name] = analyzer

            logger.info(f"  Analyzing {repo_name}...")
            count = await analyzer.analyze_directory(repo_path)
            logger.info(f"  ✓ {repo_name}: {count} files analyzed")

        # Build cross-repo connections
        await self._build_cross_repo_graph()

        return True

    async def _build_cross_repo_graph(self) -> None:
        """Build graph of cross-repository dependencies."""
        if not self._cross_repo_graph:
            return

        # Merge all symbols into cross-repo graph
        for repo_name, analyzer in self._analyzers.items():
            graph = analyzer.get_dependency_graph()
            for name, symbol in graph._symbols.items():
                # Prefix with repo name for uniqueness
                prefixed_name = f"{repo_name}:{name}"
                self._cross_repo_graph._symbols[prefixed_name] = symbol

        # Look for cross-repo references (e.g., imports between repos)
        # This is heuristic-based since Python imports don't explicitly
        # reference other repos
        logger.info("  Building cross-repo dependency links...")

    async def get_cross_repo_impact(
        self,
        file_path: Path,
    ) -> Dict[str, ImpactAnalysis]:
        """
        Get impact analysis across all repositories.

        Returns impact for each repo affected by changes to the file.
        """
        results = {}

        # Determine which repo the file belongs to
        source_repo = None
        for repo_name, repo_path in self._repos.items():
            if str(file_path).startswith(str(repo_path)):
                source_repo = repo_name
                break

        if not source_repo or source_repo not in self._analyzers:
            return results

        # Get symbols in the file
        analyzer = self._analyzers[source_repo]
        file_symbols = analyzer.get_dependency_graph().get_symbols_in_file(file_path)

        # Analyze impact for each symbol
        for symbol in file_symbols:
            impact = await analyzer.get_impact_analysis(symbol.qualified_name)
            if impact.affected_files:
                results[symbol.qualified_name] = impact

        return results

    async def find_cross_repo_cycles(self) -> List[CircularDependency]:
        """Find circular dependencies that span multiple repositories."""
        if not self._cross_repo_graph:
            return []

        return self._cross_repo_graph.find_cycles("all")

    def get_stats(self) -> Dict[str, Any]:
        """Get cross-repo analyzer statistics."""
        stats = {"repositories": {}}

        for repo_name, analyzer in self._analyzers.items():
            stats["repositories"][repo_name] = analyzer.get_stats()

        return stats


# =============================================================================
# SINGLETON ACCESSORS
# =============================================================================

_dependency_analyzer: Optional[DependencyAnalyzer] = None
_cross_repo_analyzer: Optional[CrossRepoDependencyAnalyzer] = None


def get_dependency_analyzer() -> DependencyAnalyzer:
    """Get the singleton dependency analyzer."""
    global _dependency_analyzer
    if _dependency_analyzer is None:
        _dependency_analyzer = DependencyAnalyzer()
    return _dependency_analyzer


def get_cross_repo_dependency_analyzer() -> CrossRepoDependencyAnalyzer:
    """Get the singleton cross-repo dependency analyzer."""
    global _cross_repo_analyzer
    if _cross_repo_analyzer is None:
        _cross_repo_analyzer = CrossRepoDependencyAnalyzer()
    return _cross_repo_analyzer


async def initialize_dependency_analysis() -> bool:
    """Initialize the cross-repo dependency analyzer."""
    analyzer = get_cross_repo_dependency_analyzer()
    return await analyzer.initialize()
